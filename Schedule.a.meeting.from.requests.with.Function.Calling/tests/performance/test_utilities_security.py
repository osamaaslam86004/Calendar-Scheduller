"""Performance checks for the source module: utilities/security.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'utilities/security.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('utilities.security')
    assert module is not None


def test_canonical_json_and_correlation_id_throughput():
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("utilities.security")
    payload = {"tenant_id": "tenant-1", "scopes": ["calendar:write", "calendar:read"], "enabled": True}
    started = time.perf_counter()
    canonical = None
    ids = set()
    for _ in range(5000):
        canonical = module._canonical_json(payload)
        ids.add(module.generate_correlation_id())
    elapsed = time.perf_counter() - started
    assert canonical == module._canonical_json(payload)
    assert len(ids) == 5000
    assert elapsed < 3.5, f"5,000 serialization/ID operations took {elapsed:.3f}s"
