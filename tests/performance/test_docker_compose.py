"""Read/scan performance checks for project file: docker-compose.yaml."""
from _helpers import assert_text_scan_is_fast, source_path

SOURCE_FILE = 'docker-compose.yaml'


def test_repeated_read_and_scan_is_fast():
    assert_text_scan_is_fast(SOURCE_FILE)


def test_compose_file_contains_expected_service_definitions():
    content = source_path(SOURCE_FILE).read_text(encoding="utf-8")
    assert "services:" in content
    assert "redis" in content.lower()
    assert "postgres" in content.lower() or "postgresql" in content.lower()
