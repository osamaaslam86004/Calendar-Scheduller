"""In-memory Redis stand-ins shared by the offline test suites.

Both the performance and e2e suites must run without a reachable Redis server, so
they patch the sync and async ``from_url`` factories with these clients.
"""

from __future__ import annotations

from typing import Any


class FakePubSub:
    def __init__(self, messages: list[dict] | None = None):
        self.channels = []
        # Messages replayed by listen(); redis-py's async pubsub yields dicts too.
        self.messages = messages or []

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

    async def aclose(self):
        return None

    async def close(self):
        return None


class FakeAsyncPubSub(FakePubSub):
    """Async pubsub whose listen() is an async generator, like redis.asyncio."""

    def __init__(self, messages: list[dict] | None = None):
        super().__init__(messages)

    async def listen(self):
        for message in self.messages:
            yield message

    def __aiter__(self):
        return self.listen()


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
        return current[start : end + 1]

    def ltrim(self, key, start, end):
        self.store[key] = self.lrange(key, start, end)
        return True

    def llen(self, key):
        value = self.store.get(key, [])
        return len(value) if isinstance(value, list) else 0

    def sadd(self, key, *values):
        current = self.store.setdefault(key, set())
        if not isinstance(current, set):
            current = self.store[key] = {current}
        before = len(current)
        current.update(values)
        return len(current) - before

    def smembers(self, key):
        value = self.store.get(key, set())
        return value if isinstance(value, set) else set()

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

    async def lrange(self, key, start, end):
        return super().lrange(key, start, end)

    async def sadd(self, key, *values):
        return super().sadd(key, *values)

    async def xadd(self, *args, **kwargs):
        return super().xadd(*args, **kwargs)

    async def xreadgroup(self, *args, **kwargs):
        return []

    async def xgroup_create(self, *args, **kwargs):
        return True

    async def eval(self, *_args, **_kwargs):
        return [1, 1, 1]

    async def aclose(self):
        return None

    async def close(self):
        return None

    def pubsub(self, *args, **kwargs):
        return FakeAsyncPubSub()
