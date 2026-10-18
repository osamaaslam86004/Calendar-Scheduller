"""Real-time anomaly detection for latency-based performance monitoring.

Tracks P95/P99 percentiles and detects anomalies when latency spikes beyond
configured standard deviation thresholds. Can automatically trigger half-open
circuit states or throttle non-critical background jobs.
"""

import json
import logging
import math
import time
from collections import deque
from typing import Optional

import redis

from config.settings import settings
from utilities.security import (
    append_audit_event,
    append_event_stream,
    get_current_correlation_id,
)

logger = logging.getLogger(__name__)
_redis = redis.Redis.from_url(settings.redis.url, decode_responses=True)


class LatencyPercentileTracker:
    """Track rolling percentiles (P50, P95, P99) and detect anomalies."""

    def __init__(
        self,
        operation: str,
        window_size: int = 1000,
        anomaly_std_dev_threshold: float = 2.5,
    ):
        self.operation = operation
        self.window_size = window_size
        self.anomaly_std_dev_threshold = anomaly_std_dev_threshold
        self.samples = deque(maxlen=window_size)
        self.redis_key = f"latency:percentiles:{operation}"

    def record_sample(self, latency_ms: float) -> dict:
        """Record a latency sample and return current percentiles + anomaly status."""
        self.samples.append(float(latency_ms))
        return self.compute_percentiles()

    def compute_percentiles(self) -> dict:
        """Compute P50, P95, P99 and detect anomalies based on standard deviation."""
        if not self.samples:
            return {
                "operation": self.operation,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "mean": 0.0,
                "std_dev": 0.0,
                "is_anomaly": False,
                "sample_count": 0,
            }

        sorted_samples = sorted(self.samples)
        n = len(sorted_samples)
        p50 = sorted_samples[int(n * 0.50)]
        p95 = sorted_samples[int(n * 0.95)]
        p99 = sorted_samples[int(n * 0.99)]

        mean = sum(sorted_samples) / n
        variance = sum((x - mean) ** 2 for x in sorted_samples) / n
        std_dev = math.sqrt(variance)

        # Anomaly: P95 exceeds mean + (std_dev * threshold)
        anomaly_threshold = mean + (std_dev * self.anomaly_std_dev_threshold)
        is_anomaly = p95 > anomaly_threshold and n > 10

        result = {
            "operation": self.operation,
            "p50": round(p50, 2),
            "p95": round(p95, 2),
            "p99": round(p99, 2),
            "mean": round(mean, 2),
            "std_dev": round(std_dev, 2),
            "anomaly_threshold": round(anomaly_threshold, 2),
            "is_anomaly": is_anomaly,
            "sample_count": n,
        }

        # Store snapshot in Redis for cross-process visibility
        _redis.set(self.redis_key, json.dumps(result), ex=300)

        return result

    @classmethod
    def from_redis(cls, operation: str) -> Optional[dict]:
        """Load the latest percentile snapshot from Redis."""
        raw = _redis.get(f"latency:percentiles:{operation}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None


def detect_anomaly(
    operation: str,
    latency_ms: float,
    window_size: int = 1000,
    std_dev_threshold: float = 2.5,
) -> dict:
    """Detect if a latency sample is anomalous and emit event if triggered."""
    tracker = LatencyPercentileTracker(
        operation, window_size=window_size, anomaly_std_dev_threshold=std_dev_threshold
    )
    result = tracker.record_sample(latency_ms)

    if result["is_anomaly"]:
        append_event_stream(
            "metrics:anomaly_detected",
            event_type="latency_anomaly",
            payload=result,
            correlation_id=get_current_correlation_id(),
            tenant_id="system",
        )
        append_audit_event(
            "latency_anomaly_detected",
            actor_id="system",
            tenant_id="system",
            action="anomaly.latency_spike",
            resource=operation,
            metadata=result,
            correlation_id=get_current_correlation_id(),
        )
        logger.warning(
            "Latency anomaly detected for %s: P95=%.2fms (threshold=%.2fms)",
            operation,
            result["p95"],
            result["anomaly_threshold"],
        )

    return result


def should_trigger_half_open(operation: str, std_dev_threshold: float = 2.5) -> bool:
    """Check if operation latency has exceeded anomaly threshold."""
    percentiles = LatencyPercentileTracker.from_redis(operation)
    if not percentiles:
        return False
    return percentiles.get("is_anomaly", False)
