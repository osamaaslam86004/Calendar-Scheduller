"""Performance checks for the source module: utilities/anomaly_detector.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'utilities/anomaly_detector.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('utilities.anomaly_detector')
    assert module is not None


def test_latency_percentile_tracker_window_performance():
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("utilities.anomaly_detector")
    tracker = module.LatencyPercentileTracker("scheduler.synthetic", window_size=100)
    started = time.perf_counter()
    for index in range(100):
        snapshot = tracker.record_sample(float((index * 17) % 250 + 1))
    elapsed = time.perf_counter() - started
    assert snapshot["sample_count"] == 100
    assert 0 <= snapshot["p50"] <= snapshot["p99"]
    assert elapsed < 3.0, f"100 rolling percentile updates took {elapsed:.3f}s"
