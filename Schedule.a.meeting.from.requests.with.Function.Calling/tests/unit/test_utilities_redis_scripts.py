import asyncio
import pytest
from unittest.mock import AsyncMock

pytest.importorskip("utilities.redis_scripts")
from utilities.redis_scripts import atomic_consume


@pytest.mark.unit
def test_atomic_consume_applies_multiplier_and_returns_success():
    fake_redis = AsyncMock()
    fake_redis.get.return_value = b"0.8"
    fake_redis.eval.return_value = [1, 2, 3]

    async def run_case():
        return await atomic_consume(
            fake_redis,
            "req_key",
            "token_key",
            req_limit=10,
            token_limit=20,
            token_cost=2,
            req_ttl=60,
            token_ttl=60,
        )

    result = asyncio.run(run_case())
    assert result[0] is True
    fake_redis.eval.assert_awaited_once()


@pytest.mark.unit
def test_atomic_consume_handles_invalid_multiplier():
    fake_redis = AsyncMock()
    fake_redis.get.return_value = b"bad"
    fake_redis.eval.return_value = [0, 1, 1]

    async def run_case():
        return await atomic_consume(
            fake_redis,
            "req_key",
            "token_key",
            req_limit=10,
            token_limit=20,
            token_cost=2,
            req_ttl=60,
            token_ttl=60,
        )

    result = asyncio.run(run_case())
    assert isinstance(result, tuple)
    assert len(result) == 3
