"""Performance checks for the source module: schemas.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'schemas.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('schemas')
    assert module is not None


def test_schedule_request_validation_throughput():
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("schemas")
    started = time.perf_counter()
    for index in range(2500):
        parsed = module.UserScheduleRequest.model_validate({"request_text": f"Schedule meeting {index} with the team"})
        assert parsed.request_text.startswith("Schedule meeting")
    elapsed = time.perf_counter() - started
    assert elapsed < 3.5, f"2,500 request validations took {elapsed:.3f}s"
