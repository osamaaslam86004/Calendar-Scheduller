"""Integration tests for Redis Pub/Sub breaker state synchronization and feature flag engine."""

import json
from unittest.mock import AsyncMock, MagicMock
import pytest

from flags.engine import (
    AnomalyEngine,
    FeatureFlagEngine,
    HealthMetrics,
    IntegratedFlagAnomalyManager,
)


@pytest.mark.integration
@pytest.mark.anyio
class TestCircuitBreakerSyncIntegration:
    async def test_feature_flag_pubsub_cache_invalidation_sync(self, async_redis):
        """Simulate two application instances synchronizing feature flag invalidations via Redis Pub/Sub."""
        # Instance 1 and Instance 2 engines sharing the same Redis bus
        engine_node_1 = FeatureFlagEngine(redis_client=async_redis, pubsub_channel="test_flags_channel")
        engine_node_2 = FeatureFlagEngine(redis_client=async_redis, pubsub_channel="test_flags_channel")

        # Set initial flag in Instance 1
        await engine_node_1.set_flag("calendar_smart_scheduling", True)
        assert await engine_node_1.get_flag("calendar_smart_scheduling") is True

        # Instance 2 fetches flag -> cache miss, retrieves from Redis and caches locally
        val_node_2 = await engine_node_2.get_flag("calendar_smart_scheduling")
        assert val_node_2 is True
        assert "calendar_smart_scheduling" in engine_node_2._flags_cache

        # Now simulate PubSub invalidation message received by node 2
        invalidation_payload = json.dumps({"flag_key": "calendar_smart_scheduling"})
        pubsub_msg = {"type": "message", "data": invalidation_payload}

        class MockPubSub:
            async def listen(self):
                yield pubsub_msg

        engine_node_2._pubsub = MockPubSub()
        # Process invalidation
        await engine_node_2._pubsub_listener()

        # Local cache in node 2 should be evicted
        assert "calendar_smart_scheduling" not in engine_node_2._flags_cache

    async def test_integrated_flag_anomaly_manager_rate_limit_adaptation(self, async_redis):
        """Verify IntegratedFlagAnomalyManager updates health score and adapts rate limits."""
        manager = IntegratedFlagAnomalyManager(
            redis_client=async_redis,
            anomaly_health_threshold=70.0,
            nominal_rate_limit=100.0,
        )

        adapted_rates = []

        def rate_limiter_callback(rate: float):
            adapted_rates.append(rate)

        manager.register_rate_limiter_adapter(rate_limiter_callback)

        # Baseline: High health (Score = 100)
        healthy = HealthMetrics(
            timestamp=1.0,
            latency_p95_ms=800.0,
            error_rate=0.0,
            cpu_utilization=40.0,
            memory_utilization=50.0,
            queue_depth=5,
        )
        await manager.update_system_metrics(healthy)
        assert await manager.is_circuit_open() is False
        assert await manager.get_health_score() == 100.0

        # Degraded metrics: Health drops to ~80.0 (> 70.0 threshold) -> Rate limiter scaled
        degraded = HealthMetrics(
            timestamp=2.0,
            latency_p95_ms=1000.0,  # score = 100
            error_rate=8.0,         # score = 20
            cpu_utilization=50.0,   # score = 100
            memory_utilization=50.0,
            queue_depth=5,          # score = 100 -> composite = 80.0
        )
        await manager.update_system_metrics(degraded)
        assert await manager.is_circuit_open() is False
        assert await manager.get_health_score() == pytest.approx(80.0)
        assert len(adapted_rates) == 1
        assert adapted_rates[-1] == pytest.approx(80.0)

        # Severe degradation: Health drops to 50.0 (< 70.0) -> Circuit breaker trips, rate limit drops to 0.0
        severe = HealthMetrics(
            timestamp=3.0,
            latency_p95_ms=5000.0,
            error_rate=10.0,
            cpu_utilization=95.0,
            memory_utilization=95.0,
            queue_depth=1000,
        )
        await manager.update_system_metrics(severe)
        assert await manager.is_circuit_open() is True
        assert await manager.get_health_score() == 0.0
        assert adapted_rates[-1] == 0.0
