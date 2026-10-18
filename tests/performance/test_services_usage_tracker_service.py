"""Performance checks for the source module: services/usage_tracker/service.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'services/usage_tracker/service.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('services.usage_tracker.service')
    assert module is not None


def test_usage_cost_calculation_throughput():
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("services.usage_tracker.service")
    started = time.perf_counter()
    result = 0.0
    for index in range(50000):
        result += module.UsageTrackerService.calculate_cost(
            "gemini-2.5-flash-lite", 1000 + index % 200, index % 100, 400 + index % 100
        )
    elapsed = time.perf_counter() - started
    assert result > 0
    assert elapsed < 3.0, f"50,000 usage cost calculations took {elapsed:.3f}s"
