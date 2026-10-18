"""Read/scan performance checks for project file: migrations/script.py.mako."""
from _helpers import assert_text_scan_is_fast, source_path

SOURCE_FILE = 'migrations/script.py.mako'


def test_repeated_read_and_scan_is_fast():
    assert_text_scan_is_fast(SOURCE_FILE)
