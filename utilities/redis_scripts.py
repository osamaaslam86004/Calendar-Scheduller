"""Lua-backed Redis helpers for atomic bucket checks/consumption."""

from typing import Tuple
import math
import logging

logger = logging.getLogger(__name__)

SCRIPT_ATOMIC_CONSUME = r"""
local req_key = KEYS[1]
local token_key = KEYS[2]
local req_limit = tonumber(ARGV[1])
local token_limit = tonumber(ARGV[2])
local token_cost = tonumber(ARGV[3])
local req_ttl = tonumber(ARGV[4])
local token_ttl = tonumber(ARGV[5])

local curr_req = tonumber(redis.call('get', req_key) or '0')
local curr_token = tonumber(redis.call('get', token_key) or '0')

if curr_req + 1 > req_limit then
  return {0, curr_req, curr_token}
end

if curr_token + token_cost > token_limit then
  return {0, curr_req, curr_token}
end

local new_req = redis.call('incr', req_key)
if new_req == 1 then redis.call('expire', req_key, req_ttl) end
local new_token = redis.call('incrby', token_key, token_cost)
if tonumber(redis.call('ttl', token_key)) < 0 then redis.call('expire', token_key, token_ttl) end

return {1, new_req, new_token}
"""


async def atomic_consume(
    redis_client,
    req_key: str,
    token_key: str,
    req_limit: int,
    token_limit: int,
    token_cost: int,
    req_ttl: int,
    token_ttl: int,
) -> Tuple[bool, int, int]:
    """Attempt to consume 1 request and `token_cost` tokens atomically.

    Returns (success, new_req_count, new_token_count).
    """
    # Consult adaptive throttle multiplier (optional). The multiplier is stored
    # in Redis at key `throttle:provider:llm:refill` and expected to be a
    # decimal in (0.0, 1.0]. If absent or invalid, fallback to 1.0.
    try:
        raw_mul = await redis_client.get("throttle:provider:llm:refill")
        if raw_mul is not None:
            try:
                mul = float(raw_mul)
                if mul <= 0 or math.isnan(mul):
                    raise ValueError("invalid multiplier")
            except Exception:
                mul = 1.0
                logger.exception(
                    "Invalid throttle multiplier value in Redis: %s", raw_mul
                )
        else:
            mul = 1.0
    except Exception:
        mul = 1.0
        logger.exception("Failed to read adaptive throttle multiplier from Redis")

    # Apply multiplier conservatively; ensure at least 1 capacity
    adj_req_limit = max(1, int(math.floor(req_limit * mul)))
    adj_token_limit = max(1, int(math.floor(token_limit * mul)))

    # redis-py's async client exposes `eval` which returns lists as Python lists
    res = await redis_client.eval(
        SCRIPT_ATOMIC_CONSUME,
        2,
        req_key,
        token_key,
        adj_req_limit,
        adj_token_limit,
        token_cost,
        req_ttl,
        token_ttl,
    )

    # res is a list like [1, new_req, new_token] or [0, curr_req, curr_token]
    if not res:
        return False, 0, 0

    success = bool(int(res[0]))
    return success, int(res[1]), int(res[2])
