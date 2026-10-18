"""Performance checks for the source module: telemetry/audit_ledger.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'telemetry/audit_ledger.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('telemetry.audit_ledger')
    assert module is not None


def test_audit_pii_masking_throughput(tmp_path):
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("telemetry.audit_ledger")
    payload = "Contact alice.smith@example.com or call 212-555-0187; reference 123-45-6789."
    started = time.perf_counter()
    masked = [module.AuditLedger.mask_pii(payload) for _ in range(10000)]
    elapsed = time.perf_counter() - started
    assert all("alice.smith@" not in item for item in masked)
    assert elapsed < 3.5, f"10,000 PII masks took {elapsed:.3f}s"
