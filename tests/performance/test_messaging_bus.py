"""Performance checks for the source module: messaging/bus.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'messaging/bus.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('messaging.bus')
    assert module is not None


def test_fingerprint_and_backoff_throughput():
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("messaging.bus")
    payload = {f"field_{i}": f"meeting attendee {i}" for i in range(60)}
    started = time.perf_counter()
    fingerprints = [module.AsyncMessageBus.compute_fingerprint(payload) for _ in range(3000)]
    for attempt in range(5000):
        delay = module.AsyncMessageBus.compute_backoff_delay(attempt % 8, base_delay=0.01, max_delay=0.2)
        assert 0 <= delay <= 0.2
    elapsed = time.perf_counter() - started
    assert len(set(fingerprints)) == 1
    assert elapsed < 4.0, f"3,000 fingerprints + 5,000 backoff calculations took {elapsed:.3f}s"
