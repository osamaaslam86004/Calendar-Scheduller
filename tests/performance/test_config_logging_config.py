"""Performance checks for the source module: config/logging_config.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'config/logging_config.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('config.logging_config')
    assert module is not None


def test_json_logging_format_throughput():
    import json
    import logging
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("config.logging_config")
    formatter = module.JSONFormatter()
    record = logging.LogRecord("perf", logging.INFO, __file__, 20, "scheduled %s", ("meeting",), None)
    started = time.perf_counter()
    for _ in range(2500):
        payload = json.loads(formatter.format(record))
        assert payload["message"] == "scheduled meeting"
    elapsed = time.perf_counter() - started
    assert elapsed < 3.0, f"2,500 JSON log records took {elapsed:.3f}s"
