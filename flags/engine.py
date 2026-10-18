"""Feature Flags & Anomaly Detection Engine.

Features:
- In-memory feature flag cache with Redis Pub/Sub invalidation.
- Multi-lens anomaly detection (latency, error rate, resource utilization, queue depth).
- Composite health score (0-100) computed from multiple signals.
- Circuit breaker that trips when health < 70.0.
- Dynamic rate limiter adaptation (R_adapted = R_nominal * health / 100).
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


@dataclass
class HealthMetrics:
    """Snapshot of system health metrics."""

    timestamp: float
    latency_p95_ms: float  # 95th percentile latency in ms
    error_rate: float  # Percentage (0-100)
    cpu_utilization: float  # Percentage (0-100)
    memory_utilization: float  # Percentage (0-100)
    queue_depth: int  # Number of pending tasks


class AnomalyEngine:
    """Multi-lens anomaly detection with health scoring.

    Attributes:
        redis: Redis async client for Pub/Sub and shared state.
        window_size: Number of metrics to retain for trend analysis.
        health_threshold: Health score below this triggers circuit breaker (default: 70.0).
        latency_warning_ms: Latency anomaly threshold in ms (default: 5000.0).
        error_rate_warning: Error rate anomaly threshold in % (default: 10.0).
    """

    def __init__(
        self,
        redis_client: Redis,
        window_size: int = 100,
        health_threshold: float = 70.0,
        latency_warning_ms: float = 5000.0,
        error_rate_warning: float = 10.0,
    ):
        self.redis = redis_client
        self.window_size = window_size
        self.health_threshold = health_threshold
        self.latency_warning_ms = latency_warning_ms
        self.error_rate_warning = error_rate_warning

        # In-memory metrics window
        self.metrics_window: deque = deque(maxlen=window_size)

        # Circuit breaker state
        self._circuit_open = False
        self._last_health_score = 100.0

    def compute_latency_score(self, latency_p95_ms: float) -> float:
        """Compute latency health score (0-100).

        Score decreases linearly as latency increases beyond warning threshold.
        """
        if latency_p95_ms <= 1000:
            return 100.0
        if latency_p95_ms >= self.latency_warning_ms:
            return 0.0
        # Linear interpolation between 1000ms (100) and threshold (0)
        return (
            100.0
            * (self.latency_warning_ms - latency_p95_ms)
            / (self.latency_warning_ms - 1000)
        )

    def compute_error_rate_score(self, error_rate: float) -> float:
        """Compute error rate health score (0-100).

        Score decreases linearly as error rate increases.
        """
        if error_rate <= 0:
            return 100.0
        if error_rate >= self.error_rate_warning:
            return 0.0
        # Linear interpolation
        return 100.0 * (1.0 - error_rate / self.error_rate_warning)

    def compute_resource_score(
        self, cpu_utilization: float, memory_utilization: float
    ) -> float:
        """Compute resource utilization health score (0-100).

        Score decreases as resource utilization increases.
        """
        avg_util = (cpu_utilization + memory_utilization) / 2.0
        if avg_util <= 60:
            return 100.0
        if avg_util >= 95:
            return 0.0
        # Linear interpolation between 60% (100) and 95% (0)
        return 100.0 * (95.0 - avg_util) / (95.0 - 60.0)

    def compute_queue_depth_score(self, queue_depth: int) -> float:
        """Compute queue depth health score (0-100).

        Score decreases as queue grows (indicates backlog).
        Higher queue depth suggests system is struggling.
        """
        if queue_depth <= 10:
            return 100.0
        if queue_depth >= 1000:
            return 0.0
        # Log scale to be less aggressive for small queues
        return 100.0 * (1.0 - min(1.0, (queue_depth - 10) / 990.0))

    def compute_composite_health_score(self, metrics: HealthMetrics) -> float:
        """Compute composite health score from all lenses.

        Equal weight to each lens:
        - Latency (25%)
        - Error rate (25%)
        - Resource utilization (25%)
        - Queue depth (25%)

        Returns:
            Score between 0 and 100.
        """
        latency_score = self.compute_latency_score(metrics.latency_p95_ms)
        error_score = self.compute_error_rate_score(metrics.error_rate)
        resource_score = self.compute_resource_score(
            metrics.cpu_utilization, metrics.memory_utilization
        )
        queue_score = self.compute_queue_depth_score(metrics.queue_depth)

        composite = (latency_score + error_score + resource_score + queue_score) / 4.0
        return max(0.0, min(100.0, composite))

    async def record_metrics(self, metrics: HealthMetrics) -> None:
        """Record a new metrics snapshot.

        Updates circuit breaker state based on composite health score.
        """
        # Add to window
        self.metrics_window.append(metrics)

        # Compute health score
        health_score = self.compute_composite_health_score(metrics)
        self._last_health_score = health_score

        # Update circuit breaker state
        was_open = self._circuit_open
        self._circuit_open = health_score < self.health_threshold

        if was_open and not self._circuit_open:
            logger.info(
                "Circuit breaker closed; health score recovered to %.1f", health_score
            )
        elif not was_open and self._circuit_open:
            logger.warning(
                "Circuit breaker opened; health score dropped to %.1f", health_score
            )

    def is_circuit_open(self) -> bool:
        """Check if circuit breaker is open (system degraded)."""
        return self._circuit_open

    def get_latest_health_score(self) -> float:
        """Return the most recent health score."""
        return self._last_health_score

    def compute_adapted_rate_limit(self, nominal_rate: float) -> float:
        """Compute adapted rate limit based on current health.

        If circuit is open (health < threshold), returns 0 (reject all).
        Otherwise, scales rate by health / 100.

        Args:
            nominal_rate: Base rate limit (requests per minute).

        Returns:
            Adapted rate limit, or 0 if circuit is open.
        """
        if self._circuit_open:
            return 0.0
        health = self._last_health_score
        return nominal_rate * (health / 100.0)


class FeatureFlagEngine:
    """Feature flag manager with in-memory cache and Redis Pub/Sub invalidation.

    Attributes:
        redis: Redis async client for Pub/Sub and storage.
        pubsub_channel: Redis Pub/Sub channel for invalidation messages.
    """

    def __init__(self, redis_client: Redis, pubsub_channel: str = "feature_flags"):
        self.redis = redis_client
        self.pubsub_channel = pubsub_channel
        self._flags_cache: Dict[str, Any] = {}
        self._subscribed = False
        self._pubsub = None

    async def start(self) -> None:
        """Start Pub/Sub subscriber for flag invalidation."""
        try:
            self._pubsub = self.redis.pubsub()
            await self._pubsub.subscribe(self.pubsub_channel)
            self._subscribed = True
            logger.debug(
                "Feature flag Pub/Sub listener started on %s", self.pubsub_channel
            )

            # Start background listener task
            self._background_tasks = set()
            task = asyncio.create_task(self._pubsub_listener())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        except Exception:
            logger.exception("Failed to start feature flag Pub/Sub listener")

    async def stop(self) -> None:
        """Stop Pub/Sub subscriber."""
        if self._pubsub:
            try:
                await self._pubsub.unsubscribe(self.pubsub_channel)
                await self._pubsub.close()
                self._subscribed = False
                logger.debug("Feature flag Pub/Sub listener stopped")
            except Exception:
                logger.exception("Failed to stop feature flag Pub/Sub listener")

    async def _pubsub_listener(self) -> None:
        """Background task listening for flag invalidation messages."""
        if not self._pubsub:
            return
        try:
            async for message in self._pubsub.listen():
                if message["type"] == "message":
                    try:
                        payload = json.loads(message["data"])
                        flag_key = payload.get("flag_key")
                        if flag_key:
                            # Invalidate flag from cache
                            self._flags_cache.pop(flag_key, None)
                            logger.debug("Invalidated feature flag: %s", flag_key)
                    except Exception:
                        logger.exception("Failed to process flag invalidation message")
        except Exception:
            logger.exception("Feature flag Pub/Sub listener error")

    async def set_flag(self, flag_key: str, value: Any) -> None:
        """Set a feature flag in Redis and local cache.

        Args:
            flag_key: The flag identifier.
            value: The flag value (any JSON-serializable).
        """
        try:
            # Store in Redis (persisted)
            data = json.dumps(
                {"key": flag_key, "value": value, "timestamp": time.time()}
            )
            await self.redis.set(f"feature_flag:{flag_key}", data)
            # Update local cache
            self._flags_cache[flag_key] = value
            logger.debug("Set feature flag: %s = %s", flag_key, value)
        except Exception:
            logger.exception("Failed to set feature flag %s", flag_key)

    async def get_flag(self, flag_key: str, default: Any = None) -> Any:
        """Get a feature flag value (from cache if available).

        Cache miss triggers Redis lookup and local cache update.

        Args:
            flag_key: The flag identifier.
            default: Value to return if flag not found.

        Returns:
            Flag value or default.
        """
        # Check local cache first
        if flag_key in self._flags_cache:
            return self._flags_cache[flag_key]

        # Cache miss; load from Redis
        try:
            data = await self.redis.get(f"feature_flag:{flag_key}")
            if data:
                payload = json.loads(
                    data.decode("utf-8") if isinstance(data, bytes) else data
                )
                value = payload.get("value", default)
                self._flags_cache[flag_key] = value
                return value
        except Exception:
            logger.exception("Failed to get feature flag %s from Redis", flag_key)

        return default

    async def list_flags(self) -> Dict[str, Any]:
        """Return all flags currently in cache."""
        return dict(self._flags_cache)


class IntegratedFlagAnomalyManager:
    """Combines FeatureFlagEngine and AnomalyEngine for integrated management.

    Integrates anomaly health scores with feature flag logic and rate limiter adaptation.
    """

    def __init__(
        self,
        redis_client: Redis,
        anomaly_health_threshold: float = 70.0,
        nominal_rate_limit: float = 100.0,
    ):
        self.anomaly_engine = AnomalyEngine(
            redis_client, health_threshold=anomaly_health_threshold
        )
        self.flag_engine = FeatureFlagEngine(redis_client)
        self.nominal_rate_limit = nominal_rate_limit
        self._rate_limiter_adapter: Optional[Callable] = None

    async def start(self) -> None:
        """Start both engines."""
        await self.flag_engine.start()

    async def stop(self) -> None:
        """Stop both engines."""
        await self.flag_engine.stop()

    def register_rate_limiter_adapter(self, adapter: Callable) -> None:
        """Register a callback to adapt rate limiter dynamically.

        The adapter will be called with adapted_rate when health changes.

        Args:
            adapter: Callable that accepts (adapted_rate: float).
        """
        self._rate_limiter_adapter = adapter

    async def update_system_metrics(self, metrics: HealthMetrics) -> None:
        """Update anomaly engine with new metrics and adapt rate limiter if needed.

        Args:
            metrics: Current system health metrics.
        """
        old_health = self.anomaly_engine.get_latest_health_score()
        await self.anomaly_engine.record_metrics(metrics)
        new_health = self.anomaly_engine.get_latest_health_score()

        # Adapt rate limiter based on health
        adapted_rate = self.anomaly_engine.compute_adapted_rate_limit(
            self.nominal_rate_limit
        )
        if self._rate_limiter_adapter and abs(new_health - old_health) > 1.0:
            # Only call adapter on meaningful health changes (> 1.0 point)
            try:
                self._rate_limiter_adapter(adapted_rate)
                logger.info(
                    "Rate limiter adapted: health=%.1f, adapted_rate=%.2f req/min",
                    new_health,
                    adapted_rate,
                )
            except Exception:
                logger.exception("Failed to adapt rate limiter")

    async def is_circuit_open(self) -> bool:
        """Check if circuit breaker is tripped."""
        return self.anomaly_engine.is_circuit_open()

    async def get_health_score(self) -> float:
        """Get current composite health score."""
        return self.anomaly_engine.get_latest_health_score()


__all__ = [
    "HealthMetrics",
    "AnomalyEngine",
    "FeatureFlagEngine",
    "IntegratedFlagAnomalyManager",
]
