"""Performance checks for the source module: routes/feature_flags_admin.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'routes/feature_flags_admin.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('routes.feature_flags_admin')
    assert module is not None
