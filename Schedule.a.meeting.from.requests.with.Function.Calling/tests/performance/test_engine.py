"""Performance checks for the source module: engine.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'engine.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('engine')
    assert module is not None


def test_prompt_building_throughput():
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("engine")
    engine = module.CalendarFunctionEngine(default_tz="UTC")
    request = "Schedule a 30 minute engineering sync tomorrow at 10:00 UTC with alex@example.com"
    started = time.perf_counter()
    for _ in range(2000):
        prompt = engine._build_prompt(request)
        assert request in prompt
    elapsed = time.perf_counter() - started
    assert elapsed < 3.0, f"2,000 scheduler prompt builds took {elapsed:.3f}s"
