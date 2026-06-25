"""Fire-and-forget background DB persistence.

Scraped data is already built before we persist it, so awaiting the Supabase
upsert inline just adds its latency to the response. Schedule it as a tracked
background task instead: the caller returns immediately and a dropped write is
logged, never silent.

Single shared implementation so reddit/youtube/x stay consistent.
"""

from __future__ import annotations

import asyncio
import structlog

logger = structlog.get_logger(__name__)

# Tasks are kept in a set so they aren't garbage-collected mid-flight
# (asyncio only holds a weak reference to running tasks).
_bg_tasks: set = set()


def save_bg(coro, *, log_event: str = "bg_db_save_failed") -> None:
    """Run a persistence coroutine in the background, off the response path.

    A failed write logs `log_event` rather than surfacing to the caller. If
    there is no running loop (e.g. called from sync test code), the coroutine
    is closed cleanly instead of leaking.
    """
    def _done(task) -> None:
        _bg_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(log_event, error=str(task.exception()))

    try:
        t = asyncio.get_running_loop().create_task(coro)
        _bg_tasks.add(t)
        t.add_done_callback(_done)
    except RuntimeError:
        coro.close()  # no running loop — drop the coroutine cleanly
