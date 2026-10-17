"""Performance checks for the source module: utilities/adaptive_throttling.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'utilities/adaptive_throttling.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('utilities.adaptive_throttling')
    assert module is not None


def test_refill_multiplier_throughput_and_bounds():
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("utilities.adaptive_throttling")
    started = time.perf_counter()
    for index in range(50000):
        value = module.compute_refill_multiplier(float(index % 8000), (index % 120) / 100)
        assert 0.1 <= value <= 1.0
    elapsed = time.perf_counter() - started
    assert elapsed < 3.0, f"50,000 refill calculations took {elapsed:.3f}s"
