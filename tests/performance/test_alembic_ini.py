"""Read/scan performance checks for project file: alembic.ini."""
from _helpers import assert_text_scan_is_fast, source_path

SOURCE_FILE = 'alembic.ini'


def test_repeated_read_and_scan_is_fast():
    assert_text_scan_is_fast(SOURCE_FILE)


def test_ini_configuration_parses_quickly():
    import configparser
    import time
    content = source_path(SOURCE_FILE).read_text(encoding="utf-8")
    started = time.perf_counter()
    for _ in range(100):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(content)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"100 INI parses took {elapsed:.3f}s"
