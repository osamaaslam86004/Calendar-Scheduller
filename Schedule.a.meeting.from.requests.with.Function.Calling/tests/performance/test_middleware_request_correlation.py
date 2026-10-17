"""Performance checks for the source module: middleware/request_correlation.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'middleware/request_correlation.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('middleware.request_correlation')
    assert module is not None
