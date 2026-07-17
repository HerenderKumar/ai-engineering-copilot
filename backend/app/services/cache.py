"""
Cache layer (Phase 1) — Redis-backed, degrades gracefully to in-process memory.

Two hot paths benefit:
  * content-hash → vector      (never embed identical text twice across repos)
  * normalized query → results (repeat questions answer in ~0 ms)

Resilience contract: a cache must NEVER take the service down. Every method
swallows Redis errors, logs the fallback once, and transparently switches to
a bounded in-process dict (fine for a single replica; Redis makes it shared
across replicas). This is the graceful-degradation pattern used throughout
the platform (handoff §6.6).
"""

import json
import logging
import time
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.logging import log_event

logger = logging.getLogger(__name__)

_MAX_LOCAL_ENTRIES = 10_000  # bound memory when running without Redis


class CacheClient:
    def __init__(self, url: str = settings.REDIS_URL):
        self._local: Dict[str, tuple] = {}  # key -> (expires_at, json_str)
        self._redis = None
        self._warned = False
        if not settings.CACHE_ENABLED:
            return
        try:
            import redis
            client = redis.from_url(url, decode_responses=True,
                                    socket_connect_timeout=1, socket_timeout=1)
            client.ping()
            self._redis = client
            logger.info("Cache backend: redis")
        except Exception as e:
            self._warn_fallback(e)

    def _warn_fallback(self, err: Exception) -> None:
        if not self._warned:
            log_event(logger, "cache.fallback", level=logging.WARNING,
                      backend="local-memory", reason=str(err))
            self._warned = True

    # -- generic JSON get/set --------------------------------------------------

    def get_json(self, key: str) -> Optional[Any]:
        if not settings.CACHE_ENABLED:
            return None
        if self._redis is not None:
            try:
                raw = self._redis.get(key)
                return json.loads(raw) if raw else None
            except Exception as e:
                self._warn_fallback(e)
                self._redis = None
        entry = self._local.get(key)
        if entry is None:
            return None
        expires_at, raw = entry
        if expires_at < time.time():
            self._local.pop(key, None)
            return None
        return json.loads(raw)

    def set_json(self, key: str, value: Any, ttl: int) -> None:
        if not settings.CACHE_ENABLED:
            return
        raw = json.dumps(value)
        if self._redis is not None:
            try:
                self._redis.setex(key, ttl, raw)
                return
            except Exception as e:
                self._warn_fallback(e)
                self._redis = None
        if len(self._local) >= _MAX_LOCAL_ENTRIES:  # crude eviction: drop oldest half
            for k in list(self._local.keys())[: _MAX_LOCAL_ENTRIES // 2]:
                self._local.pop(k, None)
        self._local[key] = (time.time() + ttl, raw)


cache = CacheClient()
