"""
Central Redis Client & State Management for StreamVault.
Provides thread-safe in-memory caching, distributed locks, task queue, and PubSub.
Falls back gracefully if Redis is unreachable.
"""

import os
import json
import time
import logging
from typing import Any, Optional, Dict

logger = logging.getLogger("streamvault.redis")

_REDIS_CLIENT = None
_REDIS_AVAILABLE = None
_LAST_CONNECT_TRY = 0
_RETRY_INTERVAL = 10  # Seconds between reconnection attempts if offline

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", None)


def _legacy_retry():
    """Retry policy of redis-py 5: one immediate retry, on timeouts only."""
    import socket

    from redis.backoff import NoBackoff
    from redis.exceptions import TimeoutError as RedisTimeoutError
    from redis.retry import Retry

    return Retry(NoBackoff(), 1, supported_errors=(RedisTimeoutError, socket.timeout))

KEY_PREFIX = "streamvault:"
TASK_QUEUE_KEY = f"{KEY_PREFIX}tasks:pending"
TASK_INFLIGHT_KEY = f"{KEY_PREFIX}tasks:inflight"
STATE_KEY_PREFIX = f"{KEY_PREFIX}state:"
LOCK_KEY_PREFIX = f"{KEY_PREFIX}lock:"
LIVE_KEY_PREFIX = f"{KEY_PREFIX}live:"
CHAT_CHANNEL_PREFIX = f"{KEY_PREFIX}chat:"


def get_redis():
    """Get or initialize singleton Redis connection pool with fail-safe fallback."""
    global _REDIS_CLIENT, _REDIS_AVAILABLE, _LAST_CONNECT_TRY
    now = time.time()

    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT

    if _REDIS_AVAILABLE is False and (now - _LAST_CONNECT_TRY) < _RETRY_INTERVAL:
        return None

    _LAST_CONNECT_TRY = now
    try:
        import redis
        pool = redis.ConnectionPool(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            password=REDIS_PASSWORD,
            socket_timeout=3.0,
            socket_connect_timeout=3.0,
            retry_on_timeout=True,
            # redis-py 8 changed the defaults: RESP3, and connection errors are
            # retried too. Pin the redis-py 5 behaviour: RESP2 for older servers,
            # one retry on timeouts only, so an unreachable Redis still falls
            # back to local mode after a single connect attempt.
            retry=_legacy_retry(),
            protocol=2,
            max_connections=50,
            decode_responses=True,
        )
        client = redis.Redis(connection_pool=pool)
        client.ping()
        _REDIS_CLIENT = client
        _REDIS_AVAILABLE = True
        logger.info(f"Connected to Redis at {REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}")
        return _REDIS_CLIENT
    except Exception as exc:
        _REDIS_AVAILABLE = False
        _REDIS_CLIENT = None
        logger.warning(f"Redis not available ({exc}); continuing with local fallback.")
        return None


def is_available() -> bool:
    """Check if Redis connection is currently active."""
    r = get_redis()
    if not r:
        return False
    try:
        return bool(r.ping())
    except Exception:
        return False


# ---------------------------------------------------------
# Ephemeral State & Caching
# ---------------------------------------------------------

def set_state(key: str, value: Any, ex: Optional[int] = None) -> bool:
    """Store serializable state in Redis. Optional TTL in seconds (ex)."""
    r = get_redis()
    if not r:
        return False
    try:
        val_str = json.dumps(value)
        full_key = f"{STATE_KEY_PREFIX}{key}"
        if ex:
            r.set(full_key, val_str, ex=ex)
        else:
            r.set(full_key, val_str)
        return True
    except Exception as e:
        logger.error(f"Redis set_state failed for {key}: {e}")
        return False


def get_state(key: str, default: Any = None) -> Any:
    """Retrieve serializable state from Redis."""
    r = get_redis()
    if not r:
        return default
    try:
        full_key = f"{STATE_KEY_PREFIX}{key}"
        raw = r.get(full_key)
        if raw is None:
            return default
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Redis get_state failed for {key}: {e}")
        return default


def invalidate_dashboard_cache() -> bool:
    """Invalidate shared dashboard media cache in Redis and signal pubsub."""
    delete_state("dashboard_media")
    publish_event("dashboard:update", {"type": "vod_refresh", "ts": time.time()})
    try:
        shm_path = "/dev/shm/streamrecorder_dashboard_media.json"
        if os.path.exists(shm_path):
            os.remove(shm_path)
    except OSError as exc:
        logger.warning("dashboard shm cache %s not removed: %s", shm_path, exc)
    return True


def delete_state(key: str) -> bool:
    """Delete a state key from Redis."""
    r = get_redis()
    if not r:
        return False
    try:
        full_key = f"{STATE_KEY_PREFIX}{key}"
        r.delete(full_key)
        return True
    except Exception:
        return False


# ---------------------------------------------------------
# Distributed Locks (Zero-Spike Guard)
# ---------------------------------------------------------

def acquire_lock(lock_name: str, timeout_sec: int = 60, worker_id: str = "worker") -> bool:
    """Acquire a distributed lock with automatic expiration."""
    r = get_redis()
    if not r:
        return True  # Fail-open if Redis is absent to avoid deadlock
    try:
        key = f"{LOCK_KEY_PREFIX}{lock_name}"
        return bool(r.set(key, worker_id, nx=True, ex=timeout_sec))
    except Exception as e:
        logger.error(f"Redis acquire_lock failed: {e}")
        return True


def release_lock(lock_name: str, worker_id: Optional[str] = None) -> bool:
    """Release a distributed lock if owned by worker_id (or unconditionally if None)."""
    r = get_redis()
    if not r:
        return True
    try:
        key = f"{LOCK_KEY_PREFIX}{lock_name}"
        if worker_id is None:
            r.delete(key)
            return True
        val = r.get(key)
        if val == worker_id:
            r.delete(key)
            return True
        return False
    except Exception as e:
        logger.error(f"Redis release_lock failed: {e}")
        return False


# ---------------------------------------------------------
# High-Speed Task Queue (Producer / Consumer)
# ---------------------------------------------------------

def enqueue_task(task_type: str, payload: Dict[str, Any], task_id: Optional[int] = None) -> bool:
    """Push a task to the Redis pending queue."""
    r = get_redis()
    if not r:
        return False
    try:
        data = {
            "id": task_id,
            "type": task_type,
            "payload": payload,
            "created_at": time.time(),
        }
        r.lpush(TASK_QUEUE_KEY, json.dumps(data))
        return True
    except Exception as e:
        logger.error(f"Redis enqueue_task failed: {e}")
        return False


def dequeue_task(timeout: int = 2) -> Optional[Dict[str, Any]]:
    """Block-wait and pop the next pending task (BRPOP)."""
    r = get_redis()
    if not r:
        return None
    try:
        res = r.brpop(TASK_QUEUE_KEY, timeout=timeout)
        if not res:
            return None
        _queue_name, raw_data = res
        return json.loads(raw_data)
    except Exception as e:
        logger.error(f"Redis dequeue_task error: {e}")
        return None


def get_queue_len() -> int:
    """Return number of pending tasks in Redis queue."""
    r = get_redis()
    if not r:
        return 0
    try:
        return int(r.llen(TASK_QUEUE_KEY))
    except Exception:
        return 0


# ---------------------------------------------------------
# PubSub (SSE & Live Realtime)
# ---------------------------------------------------------

def publish_event(channel_name: str, payload: Any) -> int:
    """Publish an event payload to a Redis channel."""
    r = get_redis()
    if not r:
        return 0
    try:
        channel = f"{KEY_PREFIX}{channel_name}"
        msg_str = json.dumps(payload) if not isinstance(payload, str) else payload
        return int(r.publish(channel, msg_str))
    except Exception as e:
        logger.error(f"Redis publish_event failed: {e}")
        return 0
