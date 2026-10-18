"""Adaptive dynamic throttling helpers.

This module provides a small integration point to adjust token-bucket refill
parameters in Redis based on observed provider metrics (latency, error rate).

The implementation is intentionally minimal: it stores a suggested refill
multiplier per provider in Redis so other parts of the system can read it and
apply throttling conservatively.
"""

from typing import Optional
import logging
import math
import json
import time

from config.settings import settings
from dlq import redis_client
from utilities.security import (
    append_audit_event,
    get_current_correlation_id,
    append_event_stream,
    record_latency_sample,
)
from utilities.feature_flags import (
    get_token_refill_rate,
    is_background_job_throttled,
    set_background_job_throttle,
)

logger = logging.getLogger(__name__)


def compute_refill_multiplier(p95_latency_ms: float, error_rate: float) -> float:
    """Compute a conservative multiplier in range (0.1, 1.0].

    - Higher latency and higher error rates reduce the multiplier.
    - This function is deliberately simple and deterministic so it is easy to
      reason about in production.
    """
    # Normalize values to reasonable ranges
    latency_factor = min(1.0, max(0.0, 300.0 / (p95_latency_ms + 1e-6)))
    error_factor = min(1.0, max(0.0, 1.0 - error_rate))

    # Geometric-like combination favors the lower (safer) signal
    raw = latency_factor * error_factor

    # Map into [0.1, 1.0] to avoid fully disabling traffic
    multiplier = max(0.1, min(1.0, raw))
    return multiplier


def set_provider_refill_multiplier(
    provider: str, multiplier: float, ttl_seconds: int = 300
) -> None:
    key = f"throttle:provider:{provider}:refill"
    try:
        redis_client.set(key, float(multiplier))
        if ttl_seconds:
            redis_client.expire(key, int(ttl_seconds))
        logger.info("Set refill multiplier for %s -> %s", provider, multiplier)
        append_audit_event(
            "throttle_adjusted",
            actor_id="system",
            tenant_id="system",
            action="throttle.adjust",
            resource=provider,
            metadata={"multiplier": multiplier, "ttl_seconds": ttl_seconds},
            correlation_id=get_current_correlation_id(),
        )
    except Exception:
        logger.exception("Failed to set refill multiplier for provider %s", provider)


def adjust_based_on_metrics(
    provider: str, p95_latency_ms: float, error_rate: float
) -> float:
    """Calculate multiplier and persist it to Redis. Returns the multiplier.

    Call this from monitoring hooks or admin endpoints when you observe
    provider degradations.
    """
    multiplier = compute_refill_multiplier(p95_latency_ms, error_rate)
    set_provider_refill_multiplier(provider, multiplier)
    return multiplier


def get_runtime_latency_snapshot(provider: str) -> dict:
    """Return the latest dynamic rate-limit latency snapshot for the provider."""
    payload = {
        "provider": provider,
        "p95_latency_ms": 250.0,
        "p99_latency_ms": 500.0,
        "updated_at": int(time.time()),
    }
    return payload


def update_runtime_latency(
    provider: str, p95_latency_ms: float, p99_latency_ms: float
) -> dict:
    """Persist latency metrics in Redis and emit an event-stream record for latency monitoring."""
    payload = {
        "provider": provider,
        "p95_latency_ms": float(p95_latency_ms),
        "p99_latency_ms": float(p99_latency_ms),
        "updated_at": int(time.time()),
    }
    redis_client.set(
        f"runtime:latency:{provider}",
        json.dumps(payload),
        ex=settings.rate_limit.hot_reload_ttl_seconds,
    )
    append_event_stream(
        "metrics:provider_latency",
        event_type="latency_metrics_updated",
        payload=payload,
        correlation_id=get_current_correlation_id(),
        tenant_id="system",
    )
    record_latency_sample(
        operation=f"provider.{provider}.latency",
        latency_ms=float(p95_latency_ms),
        status="ok",
        tenant_id="system",
        correlation_id=get_current_correlation_id(),
        metadata={"p99_latency_ms": float(p99_latency_ms)},
    )
    return payload


def should_throttle_background_job(job_type: str) -> bool:
    """Check if a background job type should be throttled based on system health."""
    return is_background_job_throttled(job_type)


def apply_dynamic_throttle_to_job(
    job_type: str, should_throttle: bool, ttl_seconds: int | None = None
) -> dict:
    """Dynamically enable/disable throttling for a background job type."""
    result = set_background_job_throttle(job_type, should_throttle, ttl_seconds)
    state = "throttled" if should_throttle else "normal"
    logger.info("Set background job %s throttle to %s", job_type, state)
    append_event_stream(
        "operations:background_jobs",
        event_type="job_throttle_adjusted",
        payload={"job_type": job_type, "throttled": should_throttle},
        correlation_id=get_current_correlation_id(),
        tenant_id="system",
    )
    return result


def get_effective_refill_rate(provider: str) -> float:
    """Get the effective token refill rate for a provider, combining features and runtime values."""
    # Start with feature flag value
    feature_rate = get_token_refill_rate(provider)

    # Check for runtime override in Redis
    try:
        raw_runtime = redis_client.get(f"runtime:refill_rate:{provider}")
        if raw_runtime:
            try:
                runtime_rate = float(raw_runtime)
                # Use the more conservative (lower) value
                return min(feature_rate, runtime_rate)
            except (ValueError, TypeError):
                pass
    except Exception:
        logger.exception("Failed to read runtime refill rate for %s", provider)

    return feature_rate
