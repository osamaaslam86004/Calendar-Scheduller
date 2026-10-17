"""Performance checks for the source module: flags/engine.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'flags/engine.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('flags.engine')
    assert module is not None


def test_anomaly_health_scoring_throughput():
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("flags.engine")
    engine = module.AnomalyEngine(redis_client=object(), window_size=100)
    metrics = module.HealthMetrics(
        timestamp=1.0, latency_p95_ms=450.0, error_rate=0.5,
        cpu_utilization=35.0, memory_utilization=42.0, queue_depth=8,
    )
    started = time.perf_counter()
    for _ in range(20000):
        score = engine.compute_composite_health_score(metrics)
        assert 0 <= score <= 100
    elapsed = time.perf_counter() - started
    assert elapsed < 3.0, f"20,000 health-score calculations took {elapsed:.3f}s"
