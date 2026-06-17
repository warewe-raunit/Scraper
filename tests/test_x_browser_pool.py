"""
tests/test_x_browser_pool.py — unit tests for the persistent X browser pool.

LazyBrowser is lazy (its constructor launches nothing and close() is a no-op
until a browser is actually started), so these exercise the pool's keying,
reuse, and eviction logic without spawning Chromium.
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Disable idle eviction by default; tests that need it set it explicitly.
os.environ.setdefault("X_BROWSER_POOL_MAX", "2")
os.environ.setdefault("X_BROWSER_IDLE_TTL", "99999")

from tools.unauth_x_scraper import XBrowserPool  # noqa: E402


def _pool(max_size=2):
    p = XBrowserPool()
    p.max_size = max_size
    p.idle_ttl = 99999
    return p


def test_same_key_is_reused():
    async def run():
        p = _pool()
        e1 = await p.acquire("acc", "http://p1", True)
        e2 = await p.acquire("acc", "http://p1", True)
        assert e1 is e2  # warm browser reused, not relaunched
    asyncio.run(run())


def test_distinct_keys_are_separate():
    async def run():
        p = _pool()
        e1 = await p.acquire("acc", "http://p1", True)
        e2 = await p.acquire("acc", "http://p2", True)
        assert e1 is not e2
    asyncio.run(run())


def test_lru_eviction_over_capacity():
    async def run():
        p = _pool(max_size=2)
        await p.acquire("acc", "p1", True)
        await p.acquire("acc", "p2", True)
        await p.acquire("acc", "p3", True)  # exceeds max -> evict LRU (p1)
        keys = list(p._entries.keys())
        assert ("acc", "p1", True) not in keys, keys
        assert ("acc", "p2", True) in keys
        assert ("acc", "p3", True) in keys
        assert len(p._entries) == 2
    asyncio.run(run())


def test_in_use_browser_is_never_evicted():
    async def run():
        p = _pool(max_size=1)
        busy = await p.acquire("acc", "p1", True)
        await busy.lock.acquire()  # simulate an in-flight request on p1
        try:
            await p.acquire("acc", "p2", True)  # over capacity, but p1 is busy
            # p1 must survive because force-closing an in-use browser would
            # corrupt the active scrape.
            assert ("acc", "p1", True) in p._entries
            assert len(p._entries) == 2
        finally:
            busy.lock.release()
    asyncio.run(run())


def test_close_all_clears_pool():
    async def run():
        p = _pool()
        await p.acquire("acc", "p1", True)
        await p.acquire("acc", "p2", True)
        await p.close_all()
        assert len(p._entries) == 0
    asyncio.run(run())


if __name__ == "__main__":
    test_same_key_is_reused()
    test_distinct_keys_are_separate()
    test_lru_eviction_over_capacity()
    test_in_use_browser_is_never_evicted()
    test_close_all_clears_pool()
    print("ALL X BROWSER POOL TESTS PASSED")
