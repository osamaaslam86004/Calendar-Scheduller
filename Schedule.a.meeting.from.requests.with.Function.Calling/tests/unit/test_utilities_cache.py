import asyncio
import pytest
from unittest.mock import AsyncMock, patch

pytest.importorskip("utilities.cache")
from utilities.cache import (
    generate_cache_key,
    get_cached_function_call,
    set_cached_function_call,
)


@pytest.mark.unit
def test_generate_cache_key_is_deterministic():
    key1 = generate_cache_key("hello world")
    key2 = generate_cache_key("hello world")
    assert key1 == key2
    assert key1.startswith("cal_schedule:")


@pytest.mark.unit
def test_get_cached_function_call_reads_json():
    fake_redis = AsyncMock()
    fake_redis.get.return_value = b'{"status": "ok"}'

    with patch("utilities.cache.get_redis_client", AsyncMock(return_value=fake_redis)):
        result = asyncio.run(get_cached_function_call("abc"))
        assert result == {"status": "ok"}


@pytest.mark.unit
def test_set_cached_function_call_writes_value():
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock()

    with patch("utilities.cache.get_redis_client", AsyncMock(return_value=fake_redis)):

        async def run():
            await set_cached_function_call("abc", {"value": 123}, expire_seconds=60)
            return None

        asyncio.run(run())
        fake_redis.set.assert_awaited()
