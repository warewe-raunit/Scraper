"""INNERTUBE_API_KEY caching: TTL-based reuse + refresh-on-rejection.

The key is a static public value, so the win isn't a per-session TTL — it's that
a stale key now self-heals (TTL re-fetch) and a key-rejected 400 forces exactly
one refresh instead of burning the whole proxy-rotation retry budget on a dead
key.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_KEY", "x")
os.environ.setdefault("GOODPROXIES_ENABLED", "false")

import api.config as config  # noqa: E402
import api.services.youtube as yt  # noqa: E402


def _svc_with_fetch(monkeypatch, key="K1", is_fallback=False):
    """Service whose network key-fetch is stubbed and call-counted."""
    svc = yt.YouTubeScraperService()
    calls = {"n": 0}

    async def _fake_fetch(max_retries=2):
        calls["n"] += 1
        return key, is_fallback

    monkeypatch.setattr(svc, "_fetch_innertube_key", _fake_fetch)
    return svc, calls


def test_key_cached_within_ttl_only_fetches_once(monkeypatch):
    svc, calls = _svc_with_fetch(monkeypatch)

    async def main():
        assert await svc._get_innertube_key() == "K1"
        assert await svc._get_innertube_key() == "K1"  # served from cache
        return calls["n"]

    assert asyncio.run(main()) == 1


def test_expired_key_is_refetched(monkeypatch):
    svc, calls = _svc_with_fetch(monkeypatch)

    async def main():
        await svc._get_innertube_key()
        svc._api_key_expires_at = time.monotonic() - 1  # force expiry
        await svc._get_innertube_key()
        return calls["n"]

    assert asyncio.run(main()) == 2


def test_fallback_key_gets_short_ttl(monkeypatch):
    """The last-resort constant must not be pinned for the full TTL, or we'd
    never retry live extraction."""
    svc, _ = _svc_with_fetch(monkeypatch, key="FB", is_fallback=True)

    async def main():
        await svc._get_innertube_key()
        return svc._api_key_expires_at - time.monotonic()

    remaining = asyncio.run(main())
    assert remaining <= 300 + 1
    # And shorter than a full real-key TTL whenever the configured TTL exceeds the cap.
    if config.YOUTUBE_INNERTUBE_KEY_TTL > 300:
        assert remaining < config.YOUTUBE_INNERTUBE_KEY_TTL


def test_invalidate_forces_refetch(monkeypatch):
    svc, calls = _svc_with_fetch(monkeypatch)

    async def main():
        await svc._get_innertube_key()
        svc._invalidate_innertube_key()
        assert svc._api_key is None
        await svc._get_innertube_key()
        return calls["n"]

    assert asyncio.run(main()) == 2


@pytest.mark.parametrize("status,body,expected", [
    (400, '{"error":{"message":"API key not valid. Please pass a valid API key."}}', True),
    (400, '{"error":{"status":"INVALID_ARGUMENT","message":"API_KEY_INVALID"}}', True),
    (400, "FAILED_PRECONDITION: bot check", False),   # bot-gating, not key
    (403, "API key not valid", False),                # only 400 counts
    (200, "ok", False),
])
def test_key_rejection_heuristic(status, body, expected):
    assert yt.YouTubeScraperService._innertube_key_rejected(status, body) is expected
