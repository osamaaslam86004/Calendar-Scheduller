"""Performance checks for the source module: models/google_calender_db.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'models/google_calender_db.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('models.google_calender_db')
    assert module is not None
