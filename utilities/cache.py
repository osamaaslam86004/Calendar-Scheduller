import hashlib
import json
from typing import Optional
from config.cache import get_redis_client


def generate_cache_key(request_text: str) -> str:
    return f"cal_schedule:{hashlib.md5(request_text.strip().encode()).hexdigest()}"


async def get_cached_function_call(cache_key: str) -> Optional[dict]:
    try:
        # Async Redis Client
        redis_client = await get_redis_client()

        cached_data = await redis_client.get(cache_key)

        if cached_data:
            return json.loads(cached_data)
    except Exception as e:
        print(f"Redis Cache Read Error: {e}")
    return None


async def set_cached_function_call(
    cache_key: str, data: dict, expire_seconds: int = 86400
):
    try:
        redis_client = await get_redis_client()
        await redis_client.set(
            cache_key,
            json.dumps(data),
            ex=expire_seconds,
        )
    except Exception as e:
        print(f"Redis Cache Write Error: {e}")
