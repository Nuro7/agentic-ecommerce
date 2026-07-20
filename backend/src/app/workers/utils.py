"""Shared async helper for Celery tasks.

Every Celery task that needs to run async code calls ``run_async(coro)``
instead of ``asyncio.run(coro)``.

Problem with ``asyncio.run()``:
  It creates a NEW event loop, runs the coroutine, then CLOSES the loop.
  httpx.AsyncClient registers cleanup callbacks on the loop. When the loop
  closes, those fire — but the loop is already gone ->
  ``RuntimeError: Event loop is closed`` in the worker logs.

This helper reuses the current thread''s event loop when available, or
creates a new one and tears it down cleanly (draining async generators
before close so httpx can shut down without error).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run *coro* in an event loop, reusing the current thread''s loop if available.

    Replaces bare ``asyncio.run()`` calls in Celery tasks to avoid the
    ``RuntimeError: Event loop is closed`` noise caused by httpx''s async
    cleanup callbacks firing after ``asyncio.run()`` shuts the loop down.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Nested call (should not happen in a Celery worker thread, but guard it).
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_run_in_new_loop, coro)
                return future.result()
        if not loop.is_closed():
            return loop.run_until_complete(coro)
        raise RuntimeError("loop is closed")  # fall through to create a new one
    except RuntimeError:
        pass  # No current event loop or it is closed - create a fresh one below.

    return _run_in_new_loop(coro)


def _run_in_new_loop(coro: Coroutine[Any, Any, T]) -> T:
    """Create a fresh event loop, run *coro*, and close it cleanly."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            # Drain all async generators so httpx / aioredis can close their
            # connections without triggering RuntimeError on loop.close().
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:
            pass
        loop.close()
