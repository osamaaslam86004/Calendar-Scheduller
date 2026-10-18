"""Read/scan performance checks for project file: repomix.config.json."""
from _helpers import assert_text_scan_is_fast, source_path

SOURCE_FILE = 'repomix.config.json'


def test_repeated_read_and_scan_is_fast():
    assert_text_scan_is_fast(SOURCE_FILE)


def test_json_configuration_parses_quickly():
    import json
    import time
    content = source_path(SOURCE_FILE).read_text(encoding="utf-8")
    started = time.perf_counter()
    for _ in range(100):
        parsed = json.loads(content)
    elapsed = time.perf_counter() - started
    assert isinstance(parsed, dict)
    assert elapsed < 2.0, f"100 JSON parses took {elapsed:.3f}s"
