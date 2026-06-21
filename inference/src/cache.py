"""
Redis caching layer for prediction results.

WHY cache predictions?
  Customer churn probability for a given customer doesn't change second-to-second.
  API callers (CRM, marketing automation) often query the same customer_id multiple
  times within a short window (e.g., page load → enrichment → export all within 60s).
  Re-running the ML model for every identical request wastes CPU and adds latency.

Cache strategy:
  - Key: SHA-256 hash of (customer_id + feature_values) — same customer with different
    features (e.g., after a new event is processed) gets a fresh prediction.
  - TTL: 300 seconds (5 min) — short enough to reflect feature updates, long enough
    to absorb repeated calls in typical CRM workflows.
  - Connection pool: 10 connections shared across the FastAPI worker process.
    Each Gunicorn worker has its own pool (no cross-process sharing needed).
  - Graceful degradation: if Redis is unavailable, predictions still work (no cache).
    We log a warning but never block inference on cache health.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio import ConnectionPool

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))
REDIS_URL         = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_KEY_PREFIX  = "churn:pred:"

_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = aioredis.ConnectionPool.from_url(
            REDIS_URL,
            max_connections=10,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
    return _pool


def _make_cache_key(customer_id: str, features: dict[str, Any]) -> str:
    """
    SHA-256 of customer_id + sorted feature values.
    Sorting ensures key is order-independent (same features, different dict order → same key).
    """
    payload = json.dumps({"customer_id": customer_id, "features": features}, sort_keys=True)
    digest  = hashlib.sha256(payload.encode()).hexdigest()[:32]
    return f"{CACHE_KEY_PREFIX}{digest}"


async def get_cached_prediction(customer_id: str, features: dict[str, Any]) -> dict | None:
    try:
        client = aioredis.Redis(connection_pool=_get_pool())
        key    = _make_cache_key(customer_id, features)
        cached = await client.get(key)
        if cached:
            log.debug(f"Cache hit for customer {customer_id}")
            return json.loads(cached)
        return None
    except Exception as e:
        log.warning(f"Redis get failed (degraded gracefully): {e}")
        return None


async def set_cached_prediction(
    customer_id: str,
    features: dict[str, Any],
    prediction: dict,
) -> None:
    try:
        client = aioredis.Redis(connection_pool=_get_pool())
        key    = _make_cache_key(customer_id, features)
        await client.setex(key, CACHE_TTL_SECONDS, json.dumps(prediction))
        log.debug(f"Cached prediction for customer {customer_id} (TTL={CACHE_TTL_SECONDS}s)")
    except Exception as e:
        log.warning(f"Redis set failed (degraded gracefully): {e}")


async def invalidate_customer(customer_id: str) -> int:
    """
    Invalidate all cached predictions for a customer (called after feature refresh).
    Scans for keys matching the prefix — acceptable for small caches in a POC.
    For large deployments, maintain a customer_id → key mapping in a Redis Set.
    """
    try:
        client  = aioredis.Redis(connection_pool=_get_pool())
        pattern = f"{CACHE_KEY_PREFIX}*"
        deleted = 0
        async for key in client.scan_iter(pattern, count=100):
            await client.delete(key)
            deleted += 1
        log.info(f"Invalidated {deleted} cache entries for pattern {pattern}")
        return deleted
    except Exception as e:
        log.warning(f"Redis invalidate failed (degraded gracefully): {e}")
        return 0


async def cache_health() -> dict:
    """Returns Redis health status for the /health endpoint."""
    try:
        client = aioredis.Redis(connection_pool=_get_pool())
        await client.ping()
        info   = await client.info("memory")
        return {
            "status":        "healthy",
            "used_memory_mb": round(info["used_memory"] / 1024 / 1024, 2),
        }
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}
