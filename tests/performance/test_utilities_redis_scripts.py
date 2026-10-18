"""Performance checks for the source module: utilities/redis_scripts.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'utilities/redis_scripts.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('utilities.redis_scripts')
    assert module is not None


def test_atomic_consume_wrapper_throughput():
    import asyncio
    import time
    from _helpers import import_module_for_performance
    module = import_module_for_performance("utilities.redis_scripts")
    class InMemoryRedis:
        async def get(self, _key):
            return None
        async def eval(self, *_args, **_kwargs):
            return [1, 1, 1]
    async def run():
        client = InMemoryRedis()
        started = time.perf_counter()
        for index in range(1000):
            result = await module.atomic_consume(client, f"req:{index}", f"token:{index}", 100, 10000, 10, 60, 3600)
            assert result[0] is True
        elapsed = time.perf_counter() - started
        assert elapsed < 3.0, f"1,000 atomic-consume wrappers took {elapsed:.3f}s"
    asyncio.run(run())
