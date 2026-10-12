"""Unit tests for flags/engine.py: Feature Flags and Multi-Lens Anomaly Detection Engine.

Covers:
- AnomalyEngine:
  - Multi-lens scoring (latency, error rate, resource utilization, queue depth)
  - Boundary conditions and linear interpolation
  - Composite health score computation and [0, 100] bounding
  - Metric recording and circuit breaker state transitions (open/closed)
  - Adapted rate limit calculations (scaling vs circuit open cutoff)
- FeatureFlagEngine:
  - Cache hit vs. cache miss Redis fallback
  - Handling byte vs. string Redis responses and missing keys
  - Local cache updating and persistence in Redis (set_flag)
  - Flag listing (list_flags)
  - Pub/Sub lifecycle (start/stop)
  - Pub/Sub listener invalidation processing and error resiliency
- IntegratedFlagAnomalyManager:
  - Lifecycle orchestration (start/stop delegation)
  - Rate limiter adapter callback triggering on health changes (> 1.0 delta threshold)
  - Rate limiter adapter suppression on negligible changes (<= 1.0 delta)
  - Adapter exception resilience
  - Circuit status and health score queries
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flags.engine import (
    AnomalyEngine,
    FeatureFlagEngine,
    HealthMetrics,
    IntegratedFlagAnomalyManager,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_redis():
    """Mock redis.asyncio.Redis instance."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    pubsub_mock = AsyncMock()
    pubsub_mock.subscribe = AsyncMock()
    pubsub_mock.unsubscribe = AsyncMock()
    pubsub_mock.close = AsyncMock()
    redis.pubsub = MagicMock(return_value=pubsub_mock)
    return redis


@pytest.fixture
def anomaly_engine(mock_redis):
    """AnomalyEngine instance with default settings."""
    return AnomalyEngine(
        redis_client=mock_redis,
        window_size=10,
        health_threshold=70.0,
        latency_warning_ms=5000.0,
        error_rate_warning=10.0,
    )


@pytest.fixture
def flag_engine(mock_redis):
    """FeatureFlagEngine instance."""
    return FeatureFlagEngine(redis_client=mock_redis, pubsub_channel="test_flags")


@pytest.fixture
def integrated_manager(mock_redis):
    """IntegratedFlagAnomalyManager instance."""
    return IntegratedFlagAnomalyManager(
        redis_client=mock_redis,
        anomaly_health_threshold=70.0,
        nominal_rate_limit=100.0,
    )


# ============================================================================
# AnomalyEngine Tests
# ============================================================================


@pytest.mark.unit
class TestAnomalyEngine:
    """Tests for AnomalyEngine scoring, health calculation, and circuit breaker."""

    def test_compute_latency_score_boundaries_and_interpolation(self, anomaly_engine):
        """Test latency scoring: <= 1000ms -> 100, >= 5000ms -> 0, linear in between."""
        # Optimal latency
        assert anomaly_engine.compute_latency_score(500.0) == 100.0
        assert anomaly_engine.compute_latency_score(1000.0) == 100.0

        # Critical / warning threshold
        assert anomaly_engine.compute_latency_score(5000.0) == 0.0
        assert anomaly_engine.compute_latency_score(6000.0) == 0.0

        # Midpoint: (5000 - 3000) / (5000 - 1000) * 100 = 50.0
        assert anomaly_engine.compute_latency_score(3000.0) == pytest.approx(50.0)

        # 2000ms: (5000 - 2000) / 4000 * 100 = 75.0
        assert anomaly_engine.compute_latency_score(2000.0) == pytest.approx(75.0)

    def test_compute_error_rate_score_boundaries_and_interpolation(
        self, anomaly_engine
    ):
        """Test error rate scoring: <= 0% -> 100, >= 10% -> 0, linear in between."""
        # Zero / negative errors
        assert anomaly_engine.compute_error_rate_score(0.0) == 100.0
        assert anomaly_engine.compute_error_rate_score(-1.0) == 100.0

        # Critical threshold
        assert anomaly_engine.compute_error_rate_score(10.0) == 0.0
        assert anomaly_engine.compute_error_rate_score(15.0) == 0.0

        # Midpoint: 5% error -> 50.0
        assert anomaly_engine.compute_error_rate_score(5.0) == pytest.approx(50.0)

        # 2% error: 100 * (1 - 2/10) = 80.0
        assert anomaly_engine.compute_error_rate_score(2.0) == pytest.approx(80.0)

    def test_compute_resource_score_boundaries_and_interpolation(self, anomaly_engine):
        """Test resource scoring: avg util <= 60% -> 100, >= 95% -> 0, linear in between."""
        # Low utilization
        assert anomaly_engine.compute_resource_score(50.0, 50.0) == 100.0
        assert anomaly_engine.compute_resource_score(60.0, 60.0) == 100.0
        assert anomaly_engine.compute_resource_score(40.0, 80.0) == 100.0  # avg = 60

        # High / saturation utilization
        assert anomaly_engine.compute_resource_score(95.0, 95.0) == 0.0
        assert anomaly_engine.compute_resource_score(98.0, 96.0) == 0.0

        # Midpoint: avg = 77.5 -> (95 - 77.5) / 35 * 100 = 50.0
        assert anomaly_engine.compute_resource_score(70.0, 85.0) == pytest.approx(50.0)

    def test_compute_queue_depth_score_boundaries_and_interpolation(
        self, anomaly_engine
    ):
        """Test queue depth scoring: <= 10 -> 100, >= 1000 -> 0, linear in between."""
        # Low queue
        assert anomaly_engine.compute_queue_depth_score(0) == 100.0
        assert anomaly_engine.compute_queue_depth_score(10) == 100.0

        # Saturated queue
        assert anomaly_engine.compute_queue_depth_score(1000) == 0.0
        assert anomaly_engine.compute_queue_depth_score(1500) == 0.0

        # Midpoint: queue = 505 -> 100 * (1 - 495 / 990) = 50.0
        assert anomaly_engine.compute_queue_depth_score(505) == pytest.approx(50.0)

    def test_compute_composite_health_score_weights_and_bounds(self, anomaly_engine):
        """Test composite health score gives 25% weight each and bounds to [0, 100]."""
        # Perfect metrics
        perfect = HealthMetrics(
            timestamp=100.0,
            latency_p95_ms=800.0,
            error_rate=0.0,
            cpu_utilization=30.0,
            memory_utilization=40.0,
            queue_depth=5,
        )
        assert anomaly_engine.compute_composite_health_score(perfect) == 100.0

        # Disastrous metrics
        disastrous = HealthMetrics(
            timestamp=100.0,
            latency_p95_ms=6000.0,
            error_rate=20.0,
            cpu_utilization=98.0,
            memory_utilization=99.0,
            queue_depth=1200,
        )
        assert anomaly_engine.compute_composite_health_score(disastrous) == 0.0

        # Mixed metrics: 100, 50, 50, 0 -> (100 + 50 + 50 + 0) / 4 = 50.0
        mixed = HealthMetrics(
            timestamp=100.0,
            latency_p95_ms=1000.0,  # 100.0
            error_rate=5.0,  # 50.0
            cpu_utilization=70.0,  # avg = 77.5 -> 50.0
            memory_utilization=85.0,
            queue_depth=1000,  # 0.0
        )
        assert anomaly_engine.compute_composite_health_score(mixed) == pytest.approx(
            50.0
        )

    @pytest.mark.anyio
    async def test_record_metrics_and_circuit_breaker_transitions(self, anomaly_engine):
        """Test recording metrics, updating window, and circuit breaker trip/recovery."""
        assert not anomaly_engine.is_circuit_open()
        assert anomaly_engine.get_latest_health_score() == 100.0

        # 1. Healthy metric snapshot
        healthy = HealthMetrics(
            timestamp=1.0,
            latency_p95_ms=900.0,
            error_rate=0.0,
            cpu_utilization=50.0,
            memory_utilization=50.0,
            queue_depth=5,
        )
        await anomaly_engine.record_metrics(healthy)
        assert len(anomaly_engine.metrics_window) == 1
        assert not anomaly_engine.is_circuit_open()
        assert anomaly_engine.get_latest_health_score() == 100.0

        # 2. Degraded metrics tripping circuit breaker (< 70.0)
        degraded = HealthMetrics(
            timestamp=2.0,
            latency_p95_ms=4000.0,  # score = 25.0
            error_rate=8.0,  # score = 20.0
            cpu_utilization=90.0,  # avg = 90.0 -> score = 100 * 5 / 35 = 14.28
            memory_utilization=90.0,
            queue_depth=800,  # score ~ 20.2
        )
        await anomaly_engine.record_metrics(degraded)
        assert len(anomaly_engine.metrics_window) == 2
        assert anomaly_engine.is_circuit_open()
        assert anomaly_engine.get_latest_health_score() < 70.0

        # 3. Recovery metric snapshot closing circuit breaker (>= 70.0)
        await anomaly_engine.record_metrics(healthy)
        assert len(anomaly_engine.metrics_window) == 3
        assert not anomaly_engine.is_circuit_open()
        assert anomaly_engine.get_latest_health_score() == 100.0

    def test_compute_adapted_rate_limit(self, anomaly_engine):
        """Test rate limit scaling when closed and complete cutoff when open."""
        nominal = 200.0
        anomaly_engine._circuit_open = False
        anomaly_engine._last_health_score = 80.0
        # 200 * 80 / 100 = 160.0
        assert anomaly_engine.compute_adapted_rate_limit(nominal) == pytest.approx(
            160.0
        )

        # Full health
        anomaly_engine._last_health_score = 100.0
        assert anomaly_engine.compute_adapted_rate_limit(nominal) == pytest.approx(
            200.0
        )

        # Circuit breaker open -> 0.0
        anomaly_engine._circuit_open = True
        assert anomaly_engine.compute_adapted_rate_limit(nominal) == 0.0


# ============================================================================
# FeatureFlagEngine Tests
# ============================================================================


@pytest.mark.unit
class TestFeatureFlagEngine:
    """Tests for FeatureFlagEngine cache, Redis synchronization, and Pub/Sub."""

    @pytest.mark.anyio
    async def test_get_flag_cache_hit(self, flag_engine, mock_redis):
        """Test get_flag returns from memory cache without querying Redis."""
        flag_engine._flags_cache["beta_feature"] = True

        val = await flag_engine.get_flag("beta_feature")
        assert val is True
        mock_redis.get.assert_not_called()

    @pytest.mark.anyio
    async def test_get_flag_cache_miss_redis_hit_string(self, flag_engine, mock_redis):
        """Test get_flag on cache miss fetches string from Redis and caches locally."""
        stored_payload = json.dumps({"key": "beta_feature", "value": {"enabled": True}})
        mock_redis.get.return_value = stored_payload

        val = await flag_engine.get_flag("beta_feature")
        assert val == {"enabled": True}
        assert flag_engine._flags_cache["beta_feature"] == {"enabled": True}
        mock_redis.get.assert_awaited_once_with("feature_flag:beta_feature")

    @pytest.mark.anyio
    async def test_get_flag_cache_miss_redis_hit_bytes(self, flag_engine, mock_redis):
        """Test get_flag on cache miss decodes bytes from Redis correctly."""
        stored_payload = json.dumps({"key": "max_concurrency", "value": 42}).encode(
            "utf-8"
        )
        mock_redis.get.return_value = stored_payload

        val = await flag_engine.get_flag("max_concurrency")
        assert val == 42
        assert flag_engine._flags_cache["max_concurrency"] == 42

    @pytest.mark.anyio
    async def test_get_flag_cache_miss_redis_not_found(self, flag_engine, mock_redis):
        """Test get_flag returns default when Redis does not contain the flag."""
        mock_redis.get.return_value = None

        val = await flag_engine.get_flag("non_existent", default="fallback")
        assert val == "fallback"
        assert "non_existent" not in flag_engine._flags_cache

    @pytest.mark.anyio
    async def test_get_flag_redis_exception_returns_default(
        self, flag_engine, mock_redis
    ):
        """Test get_flag recovers gracefully on Redis error and returns default."""
        mock_redis.get.side_effect = RuntimeError("Redis connection lost")

        val = await flag_engine.get_flag("some_flag", default="safe_default")
        assert val == "safe_default"

    @pytest.mark.anyio
    async def test_set_flag_persists_redis_and_updates_cache(
        self, flag_engine, mock_redis
    ):
        """Test set_flag writes JSON to Redis and updates in-memory cache."""
        await flag_engine.set_flag("new_algorithm", "v2")

        assert flag_engine._flags_cache["new_algorithm"] == "v2"
        mock_redis.set.assert_awaited_once()
        args, _ = mock_redis.set.call_args
        assert args[0] == "feature_flag:new_algorithm"
        saved_dict = json.loads(args[1])
        assert saved_dict["key"] == "new_algorithm"
        assert saved_dict["value"] == "v2"
        assert "timestamp" in saved_dict

    @pytest.mark.anyio
    async def test_set_flag_redis_exception_handled(self, flag_engine, mock_redis):
        """Test set_flag logs exception when Redis set fails without re-raising."""
        mock_redis.set.side_effect = RuntimeError("Redis write error")

        # Does not raise
        await flag_engine.set_flag("failing_flag", True)

    @pytest.mark.anyio
    async def test_list_flags_returns_cache_copy(self, flag_engine):
        """Test list_flags returns a copy of the current cache."""
        flag_engine._flags_cache["flag_a"] = 1
        flag_engine._flags_cache["flag_b"] = "test"

        flags = await flag_engine.list_flags()
        assert flags == {"flag_a": 1, "flag_b": "test"}

        # Modifications to the result should not mutate the internal cache
        flags["flag_c"] = 999
        assert "flag_c" not in flag_engine._flags_cache

    @pytest.mark.anyio
    async def test_start_and_stop_lifecycle(self, flag_engine, mock_redis):
        """Test start creates subscription and stop cleans up cleanly."""
        pubsub_mock = mock_redis.pubsub.return_value

        with patch("asyncio.create_task") as mock_create_task:
            await flag_engine.start()
            assert flag_engine._subscribed is True
            pubsub_mock.subscribe.assert_awaited_once_with("test_flags")
            mock_create_task.assert_called_once()

        await flag_engine.stop()
        assert flag_engine._subscribed is False
        pubsub_mock.unsubscribe.assert_awaited_once_with("test_flags")
        pubsub_mock.close.assert_awaited_once()

    @pytest.mark.anyio
    async def test_start_handles_exception(self, flag_engine, mock_redis):
        """Test start handles failure to subscribe without crashing."""
        mock_redis.pubsub.side_effect = RuntimeError("PubSub failed")
        await flag_engine.start()
        assert flag_engine._subscribed is False

    @pytest.mark.anyio
    async def test_stop_handles_exception(self, flag_engine, mock_redis):
        """Test stop handles failure during close without crashing."""
        pubsub_mock = mock_redis.pubsub.return_value
        pubsub_mock.unsubscribe.side_effect = RuntimeError("Unsubscribe failed")
        flag_engine._pubsub = pubsub_mock

        await flag_engine.stop()

    @pytest.mark.anyio
    async def test_pubsub_listener_invalidates_cache(self, flag_engine):
        """Test _pubsub_listener receives messages and evicts invalidated keys."""
        flag_engine._flags_cache["stale_flag"] = "old_value"
        flag_engine._flags_cache["keep_flag"] = "preserved"

        # Mock an async iterator yielding an invalidation message, then raising to end loop
        class MockPubSub:
            def __init__(self, messages):
                self.messages = messages

            async def listen(self):
                for msg in self.messages:
                    yield msg

        messages = [
            {"type": "subscribe"},  # non-message event
            {
                "type": "message",
                "data": json.dumps({"flag_key": "stale_flag"}),
            },
            {
                "type": "message",
                "data": "invalid json {",  # invalid JSON branch
            },
        ]
        flag_engine._pubsub = MockPubSub(messages)

        await flag_engine._pubsub_listener()

        assert "stale_flag" not in flag_engine._flags_cache
        assert flag_engine._flags_cache["keep_flag"] == "preserved"

    @pytest.mark.anyio
    async def test_pubsub_listener_no_pubsub_noop(self, flag_engine):
        """Test _pubsub_listener exits early when _pubsub is None."""
        flag_engine._pubsub = None
        await flag_engine._pubsub_listener()


# ============================================================================
# IntegratedFlagAnomalyManager Tests
# ============================================================================


@pytest.mark.unit
class TestIntegratedFlagAnomalyManager:
    """Tests for IntegratedFlagAnomalyManager orchestration and rate limit adapter."""

    @pytest.mark.anyio
    async def test_start_and_stop_delegation(self, integrated_manager):
        """Test start and stop delegate to flag_engine."""
        with patch.object(
            integrated_manager.flag_engine, "start", new_callable=AsyncMock
        ) as mock_start, patch.object(
            integrated_manager.flag_engine, "stop", new_callable=AsyncMock
        ) as mock_stop:
            await integrated_manager.start()
            mock_start.assert_awaited_once()

            await integrated_manager.stop()
            mock_stop.assert_awaited_once()

    @pytest.mark.anyio
    async def test_update_system_metrics_triggers_adapter_on_meaningful_delta(
        self, integrated_manager
    ):
        """Test rate limiter adapter is invoked when health delta > 1.0 and circuit closed."""
        mock_adapter = MagicMock()
        integrated_manager.register_rate_limiter_adapter(mock_adapter)

        # Baseline starts at 100.0. Drop to health = 80.0 (> threshold 70.0, circuit remains closed)
        # Latency 1000 (100), Error 8% (20), Resource 50% (100), Queue 5 (100)
        # Composite = (100 + 20 + 100 + 100) / 4 = 80.0
        degraded = HealthMetrics(
            timestamp=1.0,
            latency_p95_ms=1000.0,
            error_rate=8.0,
            cpu_utilization=50.0,
            memory_utilization=50.0,
            queue_depth=5,
        )
        await integrated_manager.update_system_metrics(degraded)

        # nominal=100.0, health=80.0 -> adapted_rate = 80.0
        mock_adapter.assert_called_once_with(pytest.approx(80.0))

    @pytest.mark.anyio
    async def test_update_system_metrics_trips_circuit_breaker_sets_zero_rate(
        self, integrated_manager
    ):
        """Test rate limiter adapter receives 0.0 when health drops below threshold (< 70.0)."""
        mock_adapter = MagicMock()
        integrated_manager.register_rate_limiter_adapter(mock_adapter)

        # Drop to health = 50.0 (< threshold 70.0 -> circuit opens)
        severe_degraded = HealthMetrics(
            timestamp=1.0,
            latency_p95_ms=1000.0,  # 100
            error_rate=5.0,  # 50
            cpu_utilization=70.0,  # 50
            memory_utilization=85.0,
            queue_depth=1000,  # 0 -> composite = 50.0
        )
        await integrated_manager.update_system_metrics(severe_degraded)

        assert await integrated_manager.is_circuit_open() is True
        mock_adapter.assert_called_once_with(0.0)

    @pytest.mark.anyio
    async def test_update_system_metrics_skips_adapter_on_minor_delta(
        self, integrated_manager
    ):
        """Test rate limiter adapter is skipped when health delta <= 1.0."""
        mock_adapter = MagicMock()
        integrated_manager.register_rate_limiter_adapter(mock_adapter)

        # Baseline starts at 100.0. Slight variance: error_rate = 0.05%
        # Latency 1000 (100), Error 0.05% (99.5), Resource 50% (100), Queue 5 (100)
        # Composite = (100 + 99.5 + 100 + 100) / 4 = 99.875
        # Delta = |99.875 - 100.0| = 0.125 <= 1.0
        minor_variance = HealthMetrics(
            timestamp=1.0,
            latency_p95_ms=1000.0,
            error_rate=0.05,
            cpu_utilization=50.0,
            memory_utilization=50.0,
            queue_depth=5,
        )
        await integrated_manager.update_system_metrics(minor_variance)

        mock_adapter.assert_not_called()

    @pytest.mark.anyio
    async def test_update_system_metrics_adapter_exception_handled(
        self, integrated_manager
    ):
        """Test adapter exception is swallowed safely without failing the call."""
        failing_adapter = MagicMock(side_effect=RuntimeError("Adapter boom"))
        integrated_manager.register_rate_limiter_adapter(failing_adapter)

        degraded = HealthMetrics(
            timestamp=1.0,
            latency_p95_ms=5000.0,
            error_rate=10.0,
            cpu_utilization=95.0,
            memory_utilization=95.0,
            queue_depth=1000,
        )
        # Should not raise
        await integrated_manager.update_system_metrics(degraded)
        failing_adapter.assert_called_once()

    @pytest.mark.anyio
    async def test_circuit_and_health_queries(self, integrated_manager):
        """Test is_circuit_open and get_health_score query anomaly engine correctly."""
        assert await integrated_manager.is_circuit_open() is False
        assert await integrated_manager.get_health_score() == 100.0

        # Artificially trip circuit
        integrated_manager.anomaly_engine._circuit_open = True
        integrated_manager.anomaly_engine._last_health_score = 45.0

        assert await integrated_manager.is_circuit_open() is True
        assert await integrated_manager.get_health_score() == 45.0
