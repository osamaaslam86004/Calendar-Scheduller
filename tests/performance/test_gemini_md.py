"""Read/scan performance checks for project file: GEMINI.md."""
from _helpers import assert_text_scan_is_fast, source_path

SOURCE_FILE = 'GEMINI.md'


def test_repeated_read_and_scan_is_fast():
    assert_text_scan_is_fast(SOURCE_FILE)
