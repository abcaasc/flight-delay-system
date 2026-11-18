"""缓存层：Redis 优先，不可用时降级为进程内 TTL 缓存。

设计说明书 NFR-1 要求看板首屏 P95 ≤ 3s，缓存是达成手段之一；
降级时必须继续服务（页面顶部显示 degraded 标记）。
"""
from __future__ import annotations

import functools
import hashlib
import json
import threading
import time
from typing import Any, Callable

from app.config import Config
from app.utils.logger import get_logger

log = get_logger(__name__)


class _MemoryCache:
    """进程内 TTL 缓存（无 Redis 时的降级实现）。"""

    def __init__(self, max_items: int = 512):
        self._data: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._max = max_items
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._data.get(key)
            if not item:
                self.misses += 1
                return None
            expire_at, value = item
            if expire_at and expire_at < time.time():
                self._data.pop(key, None)
                self.misses += 1
                return None
            self.hits += 1
            return value

    def setex(self, key: str, ttl: int, value: str) -> None:
        with self._lock:
            if len(self._data) >= self._max:
                # 简单淘汰：清掉最早过期的一批
                for k in list(self._data)[: self._max // 4]:
                    self._data.pop(k, None)
            self._data[key] = (time.time() + ttl if ttl else 0, value)

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                self._data.pop(k, None)
            return len(keys)

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "backend": "memory",
                "items": len(self._data),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
            }


class _RedisCache:
    def __init__(self, client):
        self.client = client

    def get(self, key: str) -> str | None:
        try:
            value = self.client.get(key)
            return value.decode("utf-8") if isinstance(value, bytes) else value
        except Exception:
            return None

    def setex(self, key: str, ttl: int, value: str) -> None:
        try:
            self.client.setex(key, ttl or 300, value)
        except Exception:
            log.warning("Redis 写入失败，本次结果不入缓存", exc_info=False)

    def delete_prefix(self, prefix: str) -> int:
        try:
            keys = list(self.client.scan_iter(match=f"{prefix}*", count=500))
            return int(self.client.delete(*keys)) if keys else 0
        except Exception:
            return 0

    def stats(self) -> dict:
        try:
            info = self.client.info()
            return {
                "backend": "redis",
                "items": int(info.get("db0", {}).get("keys", 0)) if isinstance(info.get("db0"), dict) else None,
                "hits": int(info.get("keyspace_hits", 0)),
                "misses": int(info.get("keyspace_misses", 0)),
                "hit_rate": round(
                    info.get("keyspace_hits", 0)
                    / max(1, info.get("keyspace_hits", 0) + info.get("keyspace_misses", 0)),
                    4,
                ),
            }
        except Exception:
            return {"backend": "redis", "items": None, "hits": None, "misses": None, "hit_rate": None}


def _build_cache():
    if Config.REDIS_URL:
        try:
            import redis as redis_lib

            client = redis_lib.Redis.from_url(Config.REDIS_URL, socket_connect_timeout=1.5,
                                              socket_timeout=1.5, decode_responses=False)
            client.ping()
            log.info("缓存后端：Redis %s", Config.REDIS_URL)
            return _RedisCache(client)
        except Exception as exc:
            log.warning("Redis 不可用（%s），降级为进程内缓存", exc)
    log.info("缓存后端：进程内 TTL 缓存")
    return _MemoryCache()


cache = _build_cache()


def make_key(namespace: str, **params: Any) -> str:
    """稳定缓存键：参数排序后哈希，避免 dict 顺序不同产生两份缓存。"""
    normalized = {k: v for k, v in sorted(params.items()) if v not in (None, "", [], {})}
    digest = hashlib.md5(json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                                    default=str).encode("utf-8")).hexdigest()[:16]
    return f"fds:{namespace}:{digest}"


def json_cached(namespace: str, ttl: int | None = None) -> Callable:
    """缓存 JSON 可序列化结果；命中直接返回，不重算。"""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = make_key(namespace, function=f"{fn.__module__}.{fn.__qualname__}",
                           args=args, kwargs=kwargs)
            hit = cache.get(key)
            if hit is not None:
                try:
                    return json.loads(hit)
                except Exception:
                    pass
            result = fn(*args, **kwargs)
            try:
                cache.setex(key, ttl or Config.CACHE_TTL_QUERY, json.dumps(result, ensure_ascii=False, default=str))
            except Exception:
                pass
            return result

        return wrapper

    return decorator


def invalidate(namespace: str) -> int:
    """数据更新后清缓存（ETL/入湖完成时调用）。"""
    n = cache.delete_prefix(f"fds:{namespace}:")
    if n:
        log.info("清理缓存 %s：%d 条", namespace, n)
    return n


def cache_stats() -> dict:
    return cache.stats()
