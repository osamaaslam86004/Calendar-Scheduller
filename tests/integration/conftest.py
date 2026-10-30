"""Shared fixtures and in-memory test doubles for integration tests.

Provides:
- InMemoryRedis: Dual sync/async in-memory mock of Redis supporting keys, strings,
  hashes, sets, lists, pub/sub, locks, and atomic operations.
- Shared fixtures for TestClient, authenticated tenant headers, admin headers,
  and database sessions.
"""

import builtins
import fnmatch
import time
import warnings
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from config.settings import settings
from main import app as fastapi_app
from utilities.security import build_tenant_context

warnings.filterwarnings(
    "ignore",
    message="Using httpx with starlette.testclient is deprecated.*",
    category=DeprecationWarning,
)


class InMemoryPubSub:
    """Mock PubSub for Redis event streaming."""

    def __init__(self, in_memory_redis: "InMemoryRedis"):
        self.redis = in_memory_redis
        self.subscribed_channels: set[str] = set()

    def subscribe(self, *channels: str):
        self.subscribed_channels.update(channels)

    async def asubscribe(self, *channels: str):
        self.subscribe(*channels)

    def unsubscribe(self, *channels: str):
        for ch in channels:
            self.subscribed_channels.discard(ch)

    async def aunsubscribe(self, *channels: str):
        self.unsubscribe(*channels)

    async def listen(self):
        # Empty generator for testing
        if False:
            yield {}

    async def close(self):
        self.subscribed_channels.clear()


class InMemoryPipeline:
    """Mock Pipeline for executing batch Redis commands."""

    def __init__(self, in_memory_redis: "InMemoryRedis"):
        self.redis = in_memory_redis
        self.commands = []

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.commands.append((name, args, kwargs))
            return self

        return method

    def execute(self):
        results = []
        for name, args, kwargs in self.commands:
            func = getattr(self.redis, name)
            results.append(func(*args, **kwargs))
        self.commands.clear()
        return results

    async def aexecute(self):
        return self.execute()


class InMemoryRedisLock:
    """Mock Redis Lock."""

    def __init__(self, redis: "InMemoryRedis", name: str, timeout: float | None = None):
        self.redis = redis
        self.name = name
        self.timeout = timeout
        self.acquired = False

    def acquire(self, blocking: bool = True, token: str | None = None):
        res = self.redis.set(
            self.name, token or "locked", nx=True, ex=int(self.timeout or 30)
        )
        self.acquired = bool(res)
        return self.acquired

    def release(self):
        self.redis.delete(self.name)
        self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    async def __aenter__(self):
        self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()


class InMemoryRedis:
    """Thread-safe, high-fidelity in-memory Redis implementation for integration tests.

    Supports both sync and async access transparently.
    """

    def __init__(self):
        self.data: dict[str, Any] = {}
        self.expires: dict[str, float] = {}
        self.channels: dict[str, list[str]] = {}

    def _is_expired(self, key: str) -> bool:
        if key in self.expires:
            if time.time() > self.expires[key]:
                self.delete(key)
                return True
        return False

    def ping(self) -> bool:
        return True

    def flushdb(self) -> bool:
        self.data.clear()
        self.expires.clear()
        self.channels.clear()
        return True

    def get(self, name: str) -> str | None:
        if self._is_expired(name):
            return None
        val = self.data.get(name)
        if val is None:
            return None
        if isinstance(val, (bytes, str)):
            return str(val) if isinstance(val, str) else val.decode("utf-8")
        return str(val)

    def set(
        self,
        name: str,
        value: Any,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
        xx: bool = False,
    ) -> bool:
        self._is_expired(name)
        exists = name in self.data

        if nx and exists:
            return False
        if xx and not exists:
            return False

        self.data[name] = str(value) if not isinstance(value, (str, bytes)) else value

        if ex is not None:
            self.expires[name] = time.time() + ex
        elif px is not None:
            self.expires[name] = time.time() + (px / 1000.0)
        elif name in self.expires:
            del self.expires[name]

        return True

    def delete(self, *names: str) -> int:
        count = 0
        for name in names:
            if name in self.data:
                del self.data[name]
                count += 1
            if name in self.expires:
                del self.expires[name]
        return count

    def exists(self, *names: str) -> int:
        count = 0
        for name in names:
            if not self._is_expired(name) and name in self.data:
                count += 1
        return count

    def expire(self, name: str, time_sec: int) -> bool:
        if name in self.data:
            self.expires[name] = time.time() + time_sec
            return True
        return False

    def ttl(self, name: str) -> int:
        if name not in self.data or self._is_expired(name):
            return -2
        if name not in self.expires:
            return -1
        remaining = int(self.expires[name] - time.time())
        return max(0, remaining)

    def incr(self, name: str, amount: int = 1) -> int:
        self._is_expired(name)
        current = int(self.data.get(name, 0))
        current += amount
        self.data[name] = str(current)
        return current

    def incrby(self, name: str, amount: int) -> int:
        return self.incr(name, amount)

    # Hashes
    def hset(
        self,
        name: str,
        key: str | None = None,
        value: Any | None = None,
        mapping: dict[str, Any] | None = None,
    ) -> int:
        self._is_expired(name)
        if name not in self.data or not isinstance(self.data[name], dict):
            self.data[name] = {}
        h = self.data[name]
        count = 0
        if mapping:
            for k, v in mapping.items():
                if k not in h:
                    count += 1
                h[str(k)] = str(v)
        if key is not None and value is not None:
            if str(key) not in h:
                count += 1
            h[str(key)] = str(value)
        return count

    def hget(self, name: str, key: str) -> str | None:
        if self._is_expired(name):
            return None
        h = self.data.get(name, {})
        if isinstance(h, dict):
            return h.get(str(key))
        return None

    def hgetall(self, name: str) -> dict[str, str]:
        if self._is_expired(name):
            return {}
        h = self.data.get(name, {})
        if isinstance(h, dict):
            return dict(h)
        return {}

    # Sets
    def sadd(self, name: str, *values: Any) -> int:
        self._is_expired(name)
        if name not in self.data or not isinstance(self.data[name], set):
            self.data[name] = set()
        s = self.data[name]
        added = 0
        for val in values:
            str_val = str(val)
            if str_val not in s:
                s.add(str_val)
                added += 1
        return added

    def srem(self, name: str, *values: Any) -> int:
        if self._is_expired(name) or name not in self.data:
            return 0
        s = self.data[name]
        if not isinstance(s, set):
            return 0
        removed = 0
        for val in values:
            str_val = str(val)
            if str_val in s:
                s.remove(str_val)
                removed += 1
        return removed

    def smembers(self, name: str) -> builtins.set[str]:
        if self._is_expired(name):
            return set()
        s = self.data.get(name, set())
        if isinstance(s, set):
            return set(s)
        return set()

    def sismember(self, name: str, value: Any) -> bool:
        if self._is_expired(name):
            return False
        s = self.data.get(name, set())
        if isinstance(s, set):
            return str(value) in s
        return False

    # Lists
    def rpush(self, name: str, *values: Any) -> int:
        self._is_expired(name)
        if name not in self.data or not isinstance(self.data[name], list):
            self.data[name] = []
        lst = self.data[name]
        for val in values:
            lst.append(str(val) if not isinstance(val, str) else val)
        return len(lst)

    def lpush(self, name: str, *values: Any) -> int:
        self._is_expired(name)
        if name not in self.data or not isinstance(self.data[name], list):
            self.data[name] = []
        lst = self.data[name]
        for val in values:
            lst.insert(0, str(val) if not isinstance(val, str) else val)
        return len(lst)

    def lpop(self, name: str) -> str | None:
        if self._is_expired(name) or name not in self.data:
            return None
        lst = self.data[name]
        if isinstance(lst, list) and lst:
            return lst.pop(0)
        return None

    def rpop(self, name: str) -> str | None:
        if self._is_expired(name) or name not in self.data:
            return None
        lst = self.data[name]
        if isinstance(lst, list) and lst:
            return lst.pop()
        return None

    def lrange(self, name: str, start: int, end: int) -> list[str]:
        if self._is_expired(name) or name not in self.data:
            return []
        lst = self.data[name]
        if not isinstance(lst, list):
            return []
        if end == -1:
            return list(lst[start:])
        return list(lst[start : end + 1])

    def llen(self, name: str) -> int:
        if self._is_expired(name) or name not in self.data:
            return 0
        lst = self.data[name]
        if isinstance(lst, list):
            return len(lst)
        return 0

    def lrem(self, name: str, count: int, value: Any) -> int:
        if self._is_expired(name) or name not in self.data:
            return 0
        lst = self.data[name]
        if not isinstance(lst, list):
            return 0
        val_str = str(value)
        removed = 0
        if count == 0:
            new_lst = [x for x in lst if x != val_str]
            removed = len(lst) - len(new_lst)
            self.data[name] = new_lst
        elif count > 0:
            new_lst = []
            for x in lst:
                if x == val_str and removed < count:
                    removed += 1
                else:
                    new_lst.append(x)
            self.data[name] = new_lst
        else:
            rev_lst = []
            for x in reversed(lst):
                if x == val_str and removed < abs(count):
                    removed += 1
                else:
                    rev_lst.append(x)
            self.data[name] = list(reversed(rev_lst))
        return removed

    # PubSub & Pipeline
    def publish(self, channel: str, message: Any) -> int:
        msg = str(message)
        if channel not in self.channels:
            self.channels[channel] = []
        self.channels[channel].append(msg)
        return 1

    def pubsub(self) -> InMemoryPubSub:
        return InMemoryPubSub(self)

    def pipeline(self) -> InMemoryPipeline:
        return InMemoryPipeline(self)

    def lock(self, name: str, timeout: float | None = None) -> InMemoryRedisLock:
        return InMemoryRedisLock(self, name, timeout)

    def keys(self, pattern: str = "*") -> list[str]:
        res = []
        for k in list(self.data.keys()):
            if not self._is_expired(k) and fnmatch.fnmatch(k, pattern):
                res.append(k)
        return res

    # Async compatibility bridges
    async def aget(self, name: str):
        return self.get(name)

    async def aset(self, *args, **kwargs):
        return self.set(*args, **kwargs)

    async def adelete(self, *args):
        return self.delete(*args)

    async def aexists(self, *args):
        return self.exists(*args)

    async def aincr(self, *args, **kwargs):
        return self.incr(*args, **kwargs)

    async def aflushdb(self):
        return self.flushdb()

    async def aclose(self):
        pass


class AsyncRedisBridge:
    """Async wrapper presenting native async methods for InMemoryRedis."""

    def __init__(self, sync_redis: InMemoryRedis):
        self._sync = sync_redis

    async def ping(self):
        return self._sync.ping()

    async def get(self, name: str):
        return self._sync.get(name)

    async def set(self, *args, **kwargs):
        return self._sync.set(*args, **kwargs)

    async def delete(self, *args):
        return self._sync.delete(*args)

    async def exists(self, *args):
        return self._sync.exists(*args)

    async def expire(self, *args):
        return self._sync.expire(*args)

    async def ttl(self, *args):
        return self._sync.ttl(*args)

    async def incr(self, *args, **kwargs):
        return self._sync.incr(*args, **kwargs)

    async def incrby(self, *args, **kwargs):
        return self._sync.incrby(*args, **kwargs)

    async def hset(self, *args, **kwargs):
        return self._sync.hset(*args, **kwargs)

    async def hget(self, *args, **kwargs):
        return self._sync.hget(*args, **kwargs)

    async def hgetall(self, *args, **kwargs):
        return self._sync.hgetall(*args, **kwargs)

    async def sadd(self, *args, **kwargs):
        return self._sync.sadd(*args, **kwargs)

    async def srem(self, *args, **kwargs):
        return self._sync.srem(*args, **kwargs)

    async def smembers(self, *args, **kwargs):
        return self._sync.smembers(*args, **kwargs)

    async def sismember(self, *args, **kwargs):
        return self._sync.sismember(*args, **kwargs)

    async def rpush(self, *args, **kwargs):
        return self._sync.rpush(*args, **kwargs)

    async def lpush(self, *args, **kwargs):
        return self._sync.lpush(*args, **kwargs)

    async def lpop(self, *args, **kwargs):
        return self._sync.lpop(*args, **kwargs)

    async def rpop(self, *args, **kwargs):
        return self._sync.rpop(*args, **kwargs)

    async def lrange(self, *args, **kwargs):
        return self._sync.lrange(*args, **kwargs)

    async def llen(self, *args, **kwargs):
        return self._sync.llen(*args, **kwargs)

    async def lrem(self, *args, **kwargs):
        return self._sync.lrem(*args, **kwargs)

    async def publish(self, *args, **kwargs):
        return self._sync.publish(*args, **kwargs)

    def pubsub(self):
        return self._sync.pubsub()

    def pipeline(self):
        return self._sync.pipeline()

    async def flushdb(self):
        return self._sync.flushdb()

    async def keys(self, *args, **kwargs):
        return self._sync.keys(*args, **kwargs)

    async def close(self):
        pass


# Global singleton test redis
_test_redis = InMemoryRedis()
_async_test_redis = AsyncRedisBridge(_test_redis)


@pytest.fixture(autouse=True)
def clean_redis_state():
    """Flush the in-memory redis state before and after each test."""
    _test_redis.flushdb()
    yield _test_redis
    _test_redis.flushdb()


@pytest.fixture
def sync_redis():
    """Return synchronous in-memory redis client."""
    return _test_redis


@pytest.fixture
def async_redis():
    """Return asynchronous in-memory redis client."""
    return _async_test_redis


@pytest.fixture
def tenant_jwt():
    """Generate a valid signed tenant JWT for tenant_test_001."""
    return build_tenant_context(
        tenant_id="tenant_test_001",
        scopes=["calendar:read", "calendar:write", "admin:all"],
    )


@pytest.fixture
def tenant_headers(tenant_jwt):
    """Provide standard authenticated tenant headers for integration tests."""
    return {
        "X-Tenant-ID": "tenant_test_001",
        "X-Tenant-JWT": tenant_jwt,
        "Authorization": f"Bearer {tenant_jwt}",
        "X-Correlation-ID": "corr-test-12345",
        "Content-Type": "application/json",
    }


@pytest.fixture
def admin_headers():
    """Provide admin authentication headers."""
    master_key = settings.security.app_master_key
    return {
        "X-Admin-Key": master_key,
        "Authorization": f"Bearer {master_key}",
        "X-Tenant-ID": "admin_tenant",
        "Content-Type": "application/json",
    }


@pytest.fixture
def client(async_redis, sync_redis):
    """Provide a TestClient connected to the FastAPI application with mocked cache/db."""
    with (
        patch("config.cache.get_redis_client", return_value=async_redis),
        patch("redis.Redis.from_url", return_value=sync_redis),
        patch("dlq.redis_client", sync_redis),
        patch("tasks.events.redis_client", sync_redis),
        TestClient(fastapi_app) as test_client,
    ):
        yield test_client
