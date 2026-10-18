"""Performance checks for the source module: utilities/tokenizer.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'utilities/tokenizer.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('utilities.tokenizer')
    assert module is not None


def test_offline_token_estimation_throughput():
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("utilities.tokenizer")
    sample = "Schedule a team meeting tomorrow at ten with the product and engineering teams."
    started = time.perf_counter()
    values = [module.count_tokens(sample, model="google/gemma-2-2b") for _ in range(5000)]
    elapsed = time.perf_counter() - started
    assert all(value > 0 for value in values)
    assert elapsed < 3.0, f"5,000 offline token estimates took {elapsed:.3f}s"
