"""Performance-suite-only safety fixtures: synthetic settings and fake Redis clients."""
from __future__ import annotations

import json
import os
import sys
import types
import tempfile
from typing import Any

import pytest


TEST_ENV = {
    "SECURITY__SESSION_SECRET": "performance-tests-session-secret",
    "SECURITY__APP_MASTER_KEY": "0123456789abcdef" * 4,
    "DB__ASYNC_URL": "postgresql+asyncpg://perf_user:perf_password@127.0.0.1/perf_db",
    "DB__SYNC_URL": "postgresql+psycopg2://perf_user:perf_password@127.0.0.1/perf_db",
    "REDIS__URL": "redis://127.0.0.1:6379/0",
    "GOOGLE_OAUTH__CLIENT_ID": "performance-tests.apps.googleusercontent.com",
    "GOOGLE_OAUTH__CLIENT_SECRET": "performance-tests-google-secret",
    "GOOGLE_OAUTH__REDIRECT_URL": "http://localhost:8000/oauth/callback",
    "LLM__GEMINI_API_KEY": "performance-tests-gemini-key",
    "LLM__OPEN_ROUTER_API_KEY": "performance-tests-openrouter-key",
    "APP__ENV": "development",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HOME": tempfile.mkdtemp(prefix="scheduler-perf-hf-home-"),
}


class FakePubSub:
    def __init__(self):
        self.channels = []
    def subscribe(self, *channels, **_kwargs):
        self.channels.extend(channels)
        return True
    async def asubscribe(self, *channels, **_kwargs):
        self.channels.extend(channels)
        return True
    def unsubscribe(self, *_channels, **_kwargs):
        return True
    async def aunsubscribe(self, *_channels, **_kwargs):
        return True
    def listen(self):
        return iter(())
    async def close(self):
        return None
    async def aclose(self):
        return None


class FakeSyncRedis:
    store: dict[str, Any] = {}
    def __init__(self, *_args, **_kwargs):
        self.store = type(self).store
    def get(self, key, *_args, **_kwargs):
        return self.store.get(key)
    def set(self, key, value, *args, **kwargs):
        self.store[key] = value
        return True
    def setex(self, key, ttl, value):
        self.store[key] = value
        return True
    def expire(self, key, ttl):
        return key in self.store
    def delete(self, *keys):
        return sum(1 for key in keys if self.store.pop(key, None) is not None)
    def exists(self, key):
        return int(key in self.store)
    def incr(self, key, amount=1):
        self.store[key] = int(self.store.get(key, 0)) + amount
        return self.store[key]
    def incrby(self, key, amount):
        return self.incr(key, amount)
    def ping(self):
        return True
    def publish(self, *_args, **_kwargs):
        return 1
    def rpush(self, key, value):
        current = self.store.setdefault(key, [])
        if not isinstance(current, list):
            current = self.store[key] = [current]
        current.append(value)
        return len(current)
    def lpush(self, key, value):
        current = self.store.setdefault(key, [])
        if not isinstance(current, list):
            current = self.store[key] = [current]
        current.insert(0, value)
        return len(current)
    def lrange(self, key, start, end):
        current = self.store.get(key, [])
        if not isinstance(current, list):
            return []
        if end == -1:
            return current[start:]
        return current[start:end + 1]
    def ltrim(self, key, start, end):
        self.store[key] = self.lrange(key, start, end)
        return True
    def llen(self, key):
        value = self.store.get(key, [])
        return len(value) if isinstance(value, list) else 0
    def xadd(self, key, fields, *args, **kwargs):
        return str(len(self.store.setdefault(key, [])) + 1)
    def xreadgroup(self, *args, **kwargs):
        return []
    def xgroup_create(self, *args, **kwargs):
        return True
    def hgetall(self, key):
        value = self.store.get(key, {})
        return value if isinstance(value, dict) else {}
    def hset(self, key, field=None, value=None, mapping=None):
        target = self.store.setdefault(key, {})
        if not isinstance(target, dict):
            target = self.store[key] = {}
        if mapping:
            target.update(mapping)
        elif field is not None:
            target[field] = value
        return 1
    def eval(self, *_args, **_kwargs):
        return [1, 1, 1]
    def register_script(self, script):
        return lambda *_args, **_kwargs: [1, 1, 1]
    def pipeline(self, *args, **kwargs):
        return self
    def watch(self, *_keys):
        return None
    def multi(self):
        return None
    def execute(self):
        return []
    def reset(self):
        return None
    def pubsub(self, *args, **kwargs):
        return FakePubSub()
    def close(self):
        return None
    def connection_pool(self):
        return None
    def __bool__(self):
        return True


class FakeAsyncRedis(FakeSyncRedis):
    async def get(self, key, *_args, **_kwargs):
        return self.store.get(key)
    async def set(self, key, value, *args, **kwargs):
        self.store[key] = value
        return True
    async def setex(self, key, ttl, value):
        self.store[key] = value
        return True
    async def expire(self, key, ttl):
        return key in self.store
    async def delete(self, *keys):
        return super().delete(*keys)
    async def incr(self, key, amount=1):
        return super().incr(key, amount)
    async def incrby(self, key, amount):
        return super().incrby(key, amount)
    async def ping(self):
        return True
    async def publish(self, *_args, **_kwargs):
        return 1
    async def rpush(self, key, value):
        return super().rpush(key, value)
    async def lpush(self, key, value):
        return super().lpush(key, value)
    async def xadd(self, *args, **kwargs):
        return super().xadd(*args, **kwargs)
    async def xreadgroup(self, *args, **kwargs):
        return []
    async def xgroup_create(self, *args, **kwargs):
        return True
    async def eval(self, *_args, **_kwargs):
        return [1, 1, 1]
    async def close(self):
        return None
    async def aclose(self):
        return None
    def pubsub(self, *args, **kwargs):
        return FakePubSub()


@pytest.fixture(autouse=True)
def isolate_performance_runtime(monkeypatch):
    """Keep all perf tests offline and independent of a developer's local credentials."""
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    FakeSyncRedis.store = {}
    # Fake clients patch only for tests collected under tests/performance.
    try:
        import redis
        monkeypatch.setattr(
            redis.Redis, "from_url",
            classmethod(lambda _cls, *args, **kwargs: FakeSyncRedis()),
            raising=False,
        )
        monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: FakeSyncRedis(), raising=False)
        try:
            import redis.asyncio as aioredis
            monkeypatch.setattr(aioredis, "from_url", lambda *args, **kwargs: FakeAsyncRedis(), raising=False)
        except Exception:
            pass
    except Exception:
        # Import-related tests report absent Redis as an optional dependency skip.
        pass
    yield
