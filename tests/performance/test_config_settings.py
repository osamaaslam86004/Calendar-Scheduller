"""Performance checks for the source module: config/settings.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'config/settings.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('config.settings')
    assert module is not None


def test_master_key_validation_throughput():
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("config.settings")
    key = "0123456789abcdef" * 4
    started = time.perf_counter()
    for _ in range(10000):
        assert module.SecurityConfig.validate_master_key_hex(key) == key
    elapsed = time.perf_counter() - started
    assert elapsed < 2.5, f"10,000 key validations took {elapsed:.3f}s"
