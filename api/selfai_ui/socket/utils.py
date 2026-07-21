import json
import uuid

from redis.asyncio import Redis


class RedisLock:
    """Distributed lock backed by an async Redis client.

    Every method is a coroutine that awaits the underlying `redis.asyncio`
    client. Callers (the socket usage-pool cleanup loop, the GPU queue
    dispatcher) run inside the FastAPI/python-socketio event loop, so a
    blocking sync redis-py call here would stall every other coroutine
    sharing that loop — hence async throughout.
    """

    def __init__(self, redis_url, lock_name, timeout_secs):
        self.lock_name = lock_name
        self.lock_id = str(uuid.uuid4())
        self.timeout_secs = timeout_secs
        self.lock_obtained = False
        self.redis = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_keepalive=True,
            socket_connect_timeout=5,
            health_check_interval=10,
            retry_on_timeout=True,
        )

    async def aquire_lock(self):
        # nx=True will only set this key if it _hasn't_ already been set
        self.lock_obtained = await self.redis.set(self.lock_name, self.lock_id, nx=True, ex=self.timeout_secs)
        return self.lock_obtained

    async def renew_lock(self):
        # xx=True will only set this key if it _has_ already been set
        return await self.redis.set(self.lock_name, self.lock_id, xx=True, ex=self.timeout_secs)

    async def release_lock(self):
        lock_value = await self.redis.get(self.lock_name)
        if lock_value and lock_value == self.lock_id:
            await self.redis.delete(self.lock_name)


class RedisDict:
    """Redis-hash-backed dict with an async interface.

    Every accessor is a coroutine that awaits the underlying `redis.asyncio`
    client, so it's safe to call from inside async socketio handlers and the
    periodic usage-pool cleanup loop without blocking the event loop.
    """

    def __init__(self, name, redis_url):
        self.name = name
        self.redis = Redis.from_url(redis_url, decode_responses=True)

    async def aset(self, key, value):
        serialized_value = json.dumps(value)
        await self.redis.hset(self.name, key, serialized_value)

    async def aget(self, key, default=None):
        value = await self.redis.hget(self.name, key)
        if value is None:
            return default
        return json.loads(value)

    async def adelete(self, key):
        result = await self.redis.hdel(self.name, key)
        return result

    async def acontains(self, key):
        return bool(await self.redis.hexists(self.name, key))

    async def alen(self):
        return await self.redis.hlen(self.name)

    async def akeys(self):
        return await self.redis.hkeys(self.name)

    async def avalues(self):
        raw = await self.redis.hvals(self.name)
        return [json.loads(v) for v in raw]

    async def aitems(self):
        raw = await self.redis.hgetall(self.name)
        return [(k, json.loads(v)) for k, v in raw.items()]

    async def aclear(self):
        await self.redis.delete(self.name)


class InMemoryDict:
    """Drop-in async-interface stand-in for RedisDict when no Redis-backed
    websocket manager is configured (single-node deployments).

    Mirrors RedisDict's coroutine-based API over a plain dict so callers in
    selfai_ui.socket.main don't need to branch on backend — every call is
    still `await`-ed, it just resolves immediately since there's no network
    I/O involved.
    """

    def __init__(self):
        self._data = {}

    async def aset(self, key, value):
        self._data[key] = value

    async def aget(self, key, default=None):
        return self._data.get(key, default)

    async def adelete(self, key):
        return 1 if self._data.pop(key, None) is not None else 0

    async def acontains(self, key):
        return key in self._data

    async def alen(self):
        return len(self._data)

    async def akeys(self):
        return list(self._data.keys())

    async def avalues(self):
        return list(self._data.values())

    async def aitems(self):
        return list(self._data.items())

    async def aclear(self):
        self._data.clear()


class NoOpLock:
    """Stand-in for RedisLock in single-node deployments (no Redis websocket
    manager configured). Always "succeeds" without any I/O."""

    async def aquire_lock(self):
        return True

    async def renew_lock(self):
        return True

    async def release_lock(self):
        return True
