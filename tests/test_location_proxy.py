"""
tests/test_location_proxy.py — Location-based proxy filtering tests.
"""

import asyncio
import os
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_KEY", "x")

import api.config as config  # noqa: E402
import api.services.youtube as yt  # noqa: E402
import api.services.x as x_service  # noqa: E402
import api.services.proxy_base as proxy_base  # noqa: E402
from tools.proxy_provider import GoodProxiesProvider  # noqa: E402


class _FakeResp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = "stub"

    def json(self):
        return self._data


class _FakeSession:
    def __init__(self):
        self.proxies = None

    def post(self, url, json=None, headers=None, impersonate=None, timeout=None):
        return _FakeResp(200, {"ok": True})


def test_youtube_search_location_passes_to_get_next_proxy(monkeypatch):
    monkeypatch.setattr(yt.requests, "Session", _FakeSession)
    svc = yt.YouTubeScraperService()

    async def _fake_key():
        return "FAKE_KEY"

    monkeypatch.setattr(svc, "_get_innertube_key", _fake_key)

    seen_countries = []

    def _fake_get_next_proxy(country=None):
        seen_countries.append(country)
        return "http://some-proxy:8080"

    monkeypatch.setattr(svc, "_get_next_proxy", _fake_get_next_proxy)

    async def run():
        await svc._execute_post("search", {"context": {}}, max_retries=1, location="DE")

    asyncio.run(run())
    assert "DE" in seen_countries


def test_youtube_search_location_no_direct_fallback(monkeypatch):
    monkeypatch.setattr(yt.requests, "Session", _FakeSession)
    svc = yt.YouTubeScraperService()

    async def _fake_key():
        return "FAKE_KEY"

    monkeypatch.setattr(svc, "_get_innertube_key", _fake_key)

    # Return None for proxy, simulating no matching proxies for location
    monkeypatch.setattr(svc, "_get_next_proxy", lambda country=None: None)

    async def run():
        # With location, it should raise RuntimeError directly without falling back to direct
        with pytest.raises(RuntimeError, match="No proxy available for country: FR"):
            await svc._execute_post("search", {"context": {}}, max_retries=2, location="FR")

    asyncio.run(run())


def test_x_search_location_passes_to_get_next_proxy(monkeypatch):
    svc = x_service.XScraperService()

    seen_countries = []

    def _fake_get_next_proxy(country=None):
        seen_countries.append(country)
        return "http://some-proxy:8080"

    monkeypatch.setattr(svc, "_get_next_proxy", _fake_get_next_proxy)

    async def _fake_scrape_search(query, limit, proxy_url, headless):
        return {"success": True, "tweets": []}

    monkeypatch.setattr(x_service, "scrape_search", _fake_scrape_search)

    async def run():
        await svc.search("hello", limit=5, location="US")

    asyncio.run(run())
    assert "US" in seen_countries


def test_x_search_location_fails_immediately_when_no_proxy(monkeypatch):
    svc = x_service.XScraperService()

    # Return None for proxy, simulating no matching proxies for location
    monkeypatch.setattr(svc, "_get_next_proxy", lambda country=None: None)

    async def run():
        # Should return success=False and the error message immediately without scraping
        result = await svc.search("hello", limit=5, location="US")
        assert result["success"] is False
        assert "No proxies available for the selected location: US" in result["error"]

    asyncio.run(run())


# --------------------------------------------------------------------------
# On-demand per-country proxy fetch
# --------------------------------------------------------------------------

def _make_enabled_provider():
    """A provider with the network disabled, configured as if enabled, for unit
    tests of the on-demand path."""
    prov = GoodProxiesProvider()
    prov.enabled = True
    prov.api_key = "x"
    prov.on_demand_country = True
    # No daemon — keep the pool static under the test's control.
    prov.continuous_health = False
    return prov


def test_ensure_country_fetches_and_admits_proxy(monkeypatch):
    prov = _make_enabled_provider()

    def fake_fetch_raw(country_override=None, count_override=None, bypass_geo_guard=False):
        url = "http://9.9.9.9:80"
        prov.proxy_countries[url] = country_override
        return [url]

    monkeypatch.setattr(prov, "_fetch_raw", fake_fetch_raw)
    monkeypatch.setattr(prov, "_filter_live", lambda urls: list(urls))

    assert prov.ensure_country("JP") == 1
    assert "http://9.9.9.9:80" in prov.pool.items
    assert prov.proxy_countries["http://9.9.9.9:80"] == "JP"
    # The newly admitted proxy is selectable for that country (replicate the
    # country filter get_next applies, without its network refresh side effect).
    jp = [p for p in prov.pool.items if prov.proxy_countries.get(p) == "JP"]
    assert prov.pool.get_next(candidates=jp) == "http://9.9.9.9:80"


def test_ensure_country_negative_cache_blocks_refetch(monkeypatch):
    prov = _make_enabled_provider()
    calls = {"n": 0}

    def fake_fetch_raw(country_override=None, count_override=None, bypass_geo_guard=False):
        calls["n"] += 1
        return []  # country has zero upstream proxies

    monkeypatch.setattr(prov, "_fetch_raw", fake_fetch_raw)
    monkeypatch.setattr(prov, "_filter_live", lambda urls: list(urls))

    assert prov.ensure_country("JP") == 0
    assert prov.ensure_country("JP") == 0  # within cooldown — must NOT refetch
    assert calls["n"] == 1


def test_get_next_proxy_async_triggers_on_demand(monkeypatch):
    svc = yt.YouTubeScraperService()

    class _FakeProvider:
        def __init__(self):
            self.ensured = []

        def is_enabled(self):
            return True

        def ensure_country(self, country, want=1):
            self.ensured.append(country)
            return 1

    fake = _FakeProvider()
    monkeypatch.setattr(proxy_base, "get_proxy_provider", lambda: fake)

    # First selection misses; after the on-demand fetch the country resolves.
    state = {"n": 0}

    def fake_next(country=None):
        state["n"] += 1
        return None if state["n"] == 1 else "http://fetched:80"

    monkeypatch.setattr(svc, "_get_next_proxy", fake_next)

    result = asyncio.run(svc._get_next_proxy_async(country="JP"))
    assert result == "http://fetched:80"
    assert fake.ensured == ["JP"]
