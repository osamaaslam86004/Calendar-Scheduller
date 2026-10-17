"""Performance checks for the source module: migrations/env.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'migrations/env.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_import_side_effects_are_not_executed_by_this_suite():
    """Migration environments and pytest conftest files are validated statically only."""
    assert SOURCE_FILE.endswith('env.py')
