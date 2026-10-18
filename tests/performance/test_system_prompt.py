"""Read/scan performance checks for project file: utilities/templates/system_prompt.txt."""
from _helpers import assert_text_scan_is_fast, source_path

SOURCE_FILE = 'utilities/templates/system_prompt.txt'


def test_repeated_read_and_scan_is_fast():
    assert_text_scan_is_fast(SOURCE_FILE)
