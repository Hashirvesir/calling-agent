"""Redis client singleton — shared state across multiple uvicorn workers.

Only needed when running with more than one worker process (each worker is a
separate OS process with its own memory, so the in-process dicts/sets that
used to live in app/api/webhooks.py — active-call dedup, outbound-call
registry, extraction dedup — must move to something all workers can see).

If REDIS_URL is unset, get_redis() returns None and callers fall back to
single-worker-safe behavior. This keeps local/dev usage (python main.py,
single worker) working with zero setup.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger

from app.core.config import settings

_client = None
_client_lock = asyncio.Lock()
_last_attempt = 0.0
_retry_task: asyncio.Task | None = None
_RETRY_INTERVAL_SECS = 30.0


async def init_redis():
    """Initialize the shared Redis async client. Call once at startup."""
    global _client, _last_attempt
    if not settings.redis_url:
        logger.info("REDIS_URL not set — running in single-worker mode (no shared state).")
        return None
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is not None:
            return _client
        _last_attempt = time.monotonic()
        try:
            import redis.asyncio as redis
            client = redis.from_url(settings.redis_url, decode_responses=True)
            await client.ping()
            _client = client
            logger.info("Redis client initialized.")
        except Exception as exc:
            logger.error(f"Redis init failed — falling back to single-worker mode: {exc}")
            _client = None
    return _client


async def get_redis():
    # Never block a call on a down Redis (each failed connect cost ~4s inline); retry in the background.
    global _retry_task
    if _client is None and settings.redis_url:
        retry_idle = _retry_task is None or _retry_task.done()
        if retry_idle and time.monotonic() - _last_attempt >= _RETRY_INTERVAL_SECS:
            _retry_task = asyncio.create_task(init_redis())
        return None
    return _client


async def close_redis():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
