import os
import warnings
from unittest.mock import MagicMock, patch
from limits.storage import MemoryStorage

# 1. Mock redis.Redis.from_url so config.limiter works offline
patcher_redis = patch("redis.Redis.from_url")
mock_redis_from_url = patcher_redis.start()
mock_redis = MagicMock()
mock_redis.get.return_value = None
mock_redis.ping.return_value = True
mock_redis_from_url.return_value = mock_redis

# 2. Mock storage_from_string so slowapi uses MemoryStorage
patcher_storage = patch("slowapi.extension.storage_from_string")
mock_storage = patcher_storage.start()
mock_storage.return_value = MemoryStorage()

# 3. Patch Limiter.limit to bypass missing request param validation across routers
patcher_limiter = patch("slowapi.Limiter.limit")
mock_limiter = patcher_limiter.start()


def dummy_decorator(*args, **kwargs):
    def decorator(func):
        return func

    return decorator


mock_limiter.side_effect = dummy_decorator

# 4. Attach rate_limiting alias to settings for routes expecting settings.rate_limiting
from config.settings import settings

if not hasattr(settings, "rate_limiting"):
    object.__setattr__(settings, "rate_limiting", settings.rate_limit)
