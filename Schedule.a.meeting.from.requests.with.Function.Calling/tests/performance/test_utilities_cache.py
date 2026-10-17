"""Performance checks for the source module: utilities/cache.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'utilities/cache.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('utilities.cache')
    assert module is not None


def test_cache_key_generation_throughput():
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("utilities.cache")
    started = time.perf_counter()
    keys = [module.generate_cache_key(f"schedule meeting number {index}") for index in range(20000)]
    elapsed = time.perf_counter() - started
    assert len(set(keys)) == len(keys)
    assert module.generate_cache_key(" same request ") == module.generate_cache_key("same request")
    assert elapsed < 3.0, f"20,000 cache keys took {elapsed:.3f}s"
