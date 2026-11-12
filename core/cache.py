"""Small cache interface for state shared between gunicorn workers.

Interface: ``get(key, default=None)``, ``set(key, value, ttl=None)``,
``delete(key)``. Values must be JSON-serialisable (the Redis adapter stores
JSON). A miss, an expired entry and an unreachable backend all look the same
to callers: ``default`` comes back, so callers always keep a way to compute
the value themselves.

Adapters:
* ``RedisCache`` - production. Delegates to core.redis_client, which logs
  connection problems and degrades to "no cache" when Redis is down.
* ``MemoryCache`` - process-local dict with TTL; for tests and single-process
  tools. Not shared between workers.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

_MISSING = object()


class Cache:
    def get(self, key: str, default: Any = None) -> Any:
        raise NotImplementedError

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> bool:
        raise NotImplementedError

    def delete(self, key: str) -> bool:
        raise NotImplementedError


class MemoryCache(Cache):
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._items: dict[str, tuple[Optional[float], Any]] = {}
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            entry = self._items.get(key, _MISSING)
            if entry is _MISSING:
                return default
            expires, value = entry
            if expires is not None and self._clock() >= expires:
                del self._items[key]
                return default
            return value

    def set(self, key, value, ttl=None):
        expires = None if not ttl else self._clock() + float(ttl)
        with self._lock:
            self._items[key] = (expires, value)
        return True

    def delete(self, key):
        with self._lock:
            return self._items.pop(key, _MISSING) is not _MISSING


class RedisCache(Cache):
    def get(self, key, default=None):
        from core import redis_client
        return redis_client.get_state(key, default)

    def set(self, key, value, ttl=None):
        from core import redis_client
        return redis_client.set_state(key, value, ex=int(ttl) if ttl else None)

    def delete(self, key):
        from core import redis_client
        return redis_client.delete_state(key)
