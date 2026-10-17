"""Read/scan performance checks for project file: Dockerfile."""
from _helpers import assert_text_scan_is_fast, source_path

SOURCE_FILE = 'Dockerfile'


def test_repeated_read_and_scan_is_fast():
    assert_text_scan_is_fast(SOURCE_FILE)


def test_dockerfile_instruction_scan_is_linear():
    content = source_path(SOURCE_FILE).read_text(encoding="utf-8")
    instructions = [line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert instructions
    assert any(line.upper().startswith("FROM ") for line in instructions)
