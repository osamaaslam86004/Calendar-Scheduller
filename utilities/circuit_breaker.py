import time
import logging
import redis
import json
from typing import Optional

from config.settings import settings
from utilities.security import (
    append_audit_event,
    get_current_correlation_id,
    append_event_stream,
)
from utilities.anomaly_detector import should_trigger_half_open
from utilities.feature_flags import get_circuit_breaker_threshold

logger = logging.getLogger(__name__)

# Sync Redis client for circuit breaker operations (fast, simple)
_redis = redis.Redis.from_url(settings.redis.url, decode_responses=True)


def _key(provider_name: str) -> str:
    return f"circuit_breaker:{provider_name}"


def is_tripped(provider_name: str) -> bool:
    """Return True if circuit for provider_name is currently tripped."""
    val = _redis.get(_key(provider_name))
    return val is not None


def trip(provider_name: str, ttl_seconds: int = 60, via_anomaly: bool = False) -> None:
    """Trip the circuit for provider_name for ttl_seconds."""
    try:
        _redis.set(_key(provider_name), int(time.time()), ex=ttl_seconds)
        trigger_source = "anomaly_detection" if via_anomaly else "manual"
        logger.warning(
            "Circuit tripped for provider %s for %s seconds (via %s)",
            provider_name,
            ttl_seconds,
            trigger_source,
        )
        append_audit_event(
            "circuit_breaker_tripped",
            actor_id="system",
            tenant_id="system",
            action="circuit_breaker.trip",
            resource=provider_name,
            metadata={
                "ttl_seconds": ttl_seconds,
                "state": "tripped",
                "trigger_source": trigger_source,
            },
            correlation_id=get_current_correlation_id(),
        )
        append_event_stream(
            "operations:circuit_breaker",
            event_type="circuit_breaker_tripped",
            payload={
                "provider": provider_name,
                "ttl_seconds": ttl_seconds,
                "trigger_source": trigger_source,
            },
            correlation_id=get_current_correlation_id(),
            tenant_id="system",
        )
    except Exception as exc:
        logger.exception("Failed to trip circuit for %s: %s", provider_name, exc)


def clear(provider_name: str) -> None:
    try:
        _redis.delete(_key(provider_name))
        append_audit_event(
            "circuit_breaker_cleared",
            actor_id="system",
            tenant_id="system",
            action="circuit_breaker.clear",
            resource=provider_name,
            metadata={"state": "cleared"},
            correlation_id=get_current_correlation_id(),
        )
    except Exception:
        pass


def set_breaker_policy(
    provider_name: str,
    *,
    open_threshold: int = 5,
    reset_timeout_seconds: int = 60,
    ttl_seconds: int | None = None,
) -> dict:
    """Store dynamic breaker policy values in Redis so the system can adapt without a redeploy."""
    ttl_seconds = ttl_seconds or settings.rate_limit.hot_reload_ttl_seconds
    policy = {
        "provider": provider_name,
        "open_threshold": int(open_threshold),
        "reset_timeout_seconds": int(reset_timeout_seconds),
        "ttl_seconds": int(ttl_seconds),
        "updated_at": int(time.time()),
    }
    _redis.set(f"runtime:breaker:{provider_name}", json.dumps(policy), ex=ttl_seconds)
    append_audit_event(
        "circuit_breaker_policy_updated",
        actor_id="system",
        tenant_id="system",
        action="circuit_breaker.hot_reload",
        resource=provider_name,
        metadata=policy,
        correlation_id=get_current_correlation_id(),
    )
    return policy


def get_breaker_policy(provider_name: str) -> dict:
    raw = _redis.get(f"runtime:breaker:{provider_name}")
    if not raw:
        # Fetch from feature flags if available
        threshold = get_circuit_breaker_threshold(provider_name)
        return {
            "provider": provider_name,
            "open_threshold": threshold,
            "reset_timeout_seconds": 60,
        }
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        threshold = get_circuit_breaker_threshold(provider_name)
        return {
            "provider": provider_name,
            "open_threshold": threshold,
            "reset_timeout_seconds": 60,
        }


def trigger_half_open_if_anomaly(provider_name: str, ttl_seconds: int = 30) -> bool:
    """Check if latency anomaly detected; if so, trigger half-open circuit state."""
    if should_trigger_half_open(provider_name):
        trip(provider_name, ttl_seconds=ttl_seconds, via_anomaly=True)
        logger.warning(
            "Half-open circuit triggered for %s due to latency anomaly", provider_name
        )
        return True
    return False
