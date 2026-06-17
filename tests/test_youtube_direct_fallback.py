"""
tests/test_youtube_direct_fallback.py — _execute_post must fall back to a direct
(no-proxy) connection when the rotating proxy pool is unusable, instead of
looping on dead proxies forever.
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_KEY", "x")

import api.config as config  # noqa: E402
import api.services.youtube as yt  # noqa: E402


class _FakeResp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = "stub"

    def json(self):
        return self._data


class _FakeSession:
    """Records the proxies seen per call; fails on proxy, succeeds direct."""
    seen_proxies = []

    def __init__(self):
        self.proxies = None

    def post(self, url, json=None, headers=None, impersonate=None, timeout=None):
        _FakeSession.seen_proxies.append(self.proxies)
        if self.proxies:  # any proxy attempt -> simulate a dead proxy
            raise RuntimeError("CONNECT tunnel failed, response 502")
        return _FakeResp(200, {"ok": True})  # direct attempt -> success


def test_execute_post_falls_back_to_direct(monkeypatch):
    _FakeSession.seen_proxies = []
    # Deterministic + fast: 1 proxy attempt, then direct.
    monkeypatch.setattr(config, "YOUTUBE_MAX_PROXY_ATTEMPTS", 1)
    monkeypatch.setattr(config, "YOUTUBE_DIRECT_FALLBACK", True)
    monkeypatch.setattr(yt.requests, "Session", _FakeSession)

    svc = yt.YouTubeScraperService()

    async def _fake_key():
        return "FAKE_KEY"

    monkeypatch.setattr(svc, "_get_innertube_key", _fake_key)
    monkeypatch.setattr(svc, "_get_next_proxy", lambda: "http://dead-proxy:8080")

    async def run():
        async def _no_sleep(_):
            return None
        monkeypatch.setattr(yt.asyncio, "sleep", _no_sleep)
        return await svc._execute_post("search", {"context": {}}, max_retries=4)

    result = asyncio.run(run())
    assert result == {"ok": True}
    # First call went through the proxy (failed), a later call was direct (None).
    assert _FakeSession.seen_proxies[0] is not None
    assert any(p is None for p in _FakeSession.seen_proxies), _FakeSession.seen_proxies


def test_direct_fallback_disabled_stays_on_proxy(monkeypatch):
    _FakeSession.seen_proxies = []
    monkeypatch.setattr(config, "YOUTUBE_MAX_PROXY_ATTEMPTS", 1)
    monkeypatch.setattr(config, "YOUTUBE_DIRECT_FALLBACK", False)  # proxy-only
    monkeypatch.setattr(yt.requests, "Session", _FakeSession)

    svc = yt.YouTubeScraperService()

    async def _fake_key():
        return "FAKE_KEY"

    monkeypatch.setattr(svc, "_get_innertube_key", _fake_key)
    monkeypatch.setattr(svc, "_get_next_proxy", lambda: "http://dead-proxy:8080")

    async def run():
        async def _no_sleep(_):
            return None
        monkeypatch.setattr(yt.asyncio, "sleep", _no_sleep)
        return await svc._execute_post("search", {"context": {}}, max_retries=3)

    # Proxy-only with every proxy dead -> exhausts and raises (never goes direct).
    try:
        asyncio.run(run())
        assert False, "expected RuntimeError when direct fallback disabled"
    except RuntimeError:
        pass
    assert all(p is not None for p in _FakeSession.seen_proxies), _FakeSession.seen_proxies
