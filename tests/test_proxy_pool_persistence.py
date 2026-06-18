"""
tests/test_proxy_pool_persistence.py — regression tests for the proxy pool
"empties and refills every tick" bug.

Two guarantees:
  1. Tolerant re-verification: a proxy that fails ONE probe is kept (within the
     consecutive-failure tolerance) instead of being evicted, so a small pool
     survives a transient bad probe wave.
  2. Floor-gated top-up: while the live count is at/above min_live the daemon
     does NOT run the expensive fetch — the pool persists.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Enable the provider with a dummy key so is_enabled() is True but __init__
# doesn't start the real daemon thread (we drive _enrich_once manually).
os.environ["GOODPROXIES_ENABLED"] = "true"
os.environ["GOODPROXIES_API_KEY"] = "test-key"
os.environ["GOODPROXIES_CONTINUOUS_HEALTH"] = "false"  # no daemon thread in tests
os.environ["GOODPROXIES_REVERIFY"] = "true"
os.environ["GOODPROXIES_REVERIFY_FAILS"] = "2"
os.environ["GOODPROXIES_MIN_LIVE"] = "3"
os.environ["GOODPROXIES_TARGET_LIVE"] = "5"
os.environ["GOODPROXIES_HEALTHCHECK"] = "true"

from tools.proxy_provider import GoodProxiesProvider  # noqa: E402


def _make_provider():
    # continuous_health=false -> __init__ calls refresh(force=True); stub fetch
    # so it doesn't hit the network during construction.
    GoodProxiesProvider._fetch = lambda self: []  # type: ignore
    p = GoodProxiesProvider()
    # Pin the tuning knobs on the instance so the test is independent of whatever
    # .env another test module may have loaded via load_dotenv(override=True).
    p.target_live = 5
    p.min_live = 3
    p.reverify_fail_threshold = 2
    p.reverify_enabled = True
    return p


def test_transient_probe_failure_does_not_evict():
    """A proxy that fails a single re-verify probe must persist (tolerance=2)."""
    p = _make_provider()
    p.pool.set_items(["http://1.1.1.1:80", "http://2.2.2.2:80"])

    # All probes fail this tick, and no top-up available.
    p._probe_all = lambda urls: {u: False for u in urls}
    p._fetch_raw = lambda: []

    p._enrich_once()
    # First failure is within tolerance (threshold=2) -> both kept.
    assert set(p.pool.items) == {"http://1.1.1.1:80", "http://2.2.2.2:80"}, p.pool.items

    # Second consecutive failure -> now evicted.
    p._enrich_once()
    assert p.pool.items == [], p.pool.items


def test_recovered_proxy_resets_strikes():
    """A proxy that fails then passes should have its strike count reset."""
    p = _make_provider()
    p.pool.set_items(["http://1.1.1.1:80"])
    p._fetch_raw = lambda: []

    # Fail once (within tolerance, kept), then pass (reset), then fail once more.
    p._probe_all = lambda urls: {u: False for u in urls}
    p._enrich_once()
    assert p.pool.items == ["http://1.1.1.1:80"]

    p._probe_all = lambda urls: {u: True for u in urls}
    p._enrich_once()
    assert p.pool.items == ["http://1.1.1.1:80"]
    assert p._reverify_strikes == {}  # strikes cleared after a pass

    # One more failure should NOT evict (counter was reset, so this is strike 1).
    p._probe_all = lambda urls: {u: False for u in urls}
    p._enrich_once()
    assert p.pool.items == ["http://1.1.1.1:80"]


def test_no_topup_fetch_when_at_or_above_floor():
    """At/above min_live the daemon must NOT refetch — the pool just persists."""
    p = _make_provider()
    # min_live=3; hold exactly 3 healthy proxies.
    held = ["http://1.1.1.1:80", "http://2.2.2.2:80", "http://3.3.3.3:80"]
    p.pool.set_items(held)
    p._probe_all = lambda urls: {u: True for u in urls}

    fetched = {"count": 0}

    def _spy_fetch():
        fetched["count"] += 1
        return ["http://9.9.9.9:80"]

    p._fetch_raw = _spy_fetch
    p._enrich_once()

    assert fetched["count"] == 0, "must not fetch while at/above min_live"
    assert set(p.pool.items) == set(held)


def test_topup_fetch_only_when_below_floor():
    """Below min_live the daemon fetches and tops up toward target_live."""
    p = _make_provider()
    p.pool.set_items(["http://1.1.1.1:80"])  # 1 < min_live(3)
    p._probe_all = lambda urls: {u: True for u in urls}

    candidates = ["http://5.5.5.5:80", "http://6.6.6.6:80", "http://7.7.7.7:80",
                  "http://8.8.8.8:80", "http://9.9.9.9:80", "http://10.10.10.10:80"]
    p._fetch_raw = lambda: candidates
    p._filter_live = lambda urls: list(urls)  # pretend all candidates are live

    p._enrich_once()
    # target_live=5, had 1 -> need 4 fresh -> total 5 (capped at target).
    assert len(p.pool.items) == 5, p.pool.items
    assert "http://1.1.1.1:80" in p.pool.items  # original survivor kept


def test_grace_window_keeps_freshly_admitted_proxy():
    """A proxy admitted via top-up gets a grace cycle: even if every re-probe
    fails afterward, it persists one extra tick before the strike counter can
    start, so noisy probes right after admission don't churn it back out."""
    p = _make_provider()
    p.reverify_grace_cycles = 1
    p.reverify_fail_threshold = 2

    # Admit one fresh proxy via the top-up path (pool starts empty < min_live).
    p._probe_all = lambda urls: {u: True for u in urls}
    p._fetch_raw = lambda: ["http://1.1.1.1:80"]
    p._filter_live = lambda urls: list(urls)
    p._enrich_once()
    assert p.pool.items == ["http://1.1.1.1:80"]
    assert p._grace.get("http://1.1.1.1:80") == 1  # grace granted on admission

    # From here every probe fails and there's no fresh supply.
    p._probe_all = lambda urls: {u: False for u in urls}
    p._fetch_raw = lambda: []

    p._enrich_once()  # grace cycle: kept WITHOUT probing
    assert p.pool.items == ["http://1.1.1.1:80"], "grace must keep a just-added proxy"

    p._enrich_once()  # grace spent -> probe fails -> strike 1 (< threshold) -> kept
    assert p.pool.items == ["http://1.1.1.1:80"]

    p._enrich_once()  # strike 2 -> evicted
    assert p.pool.items == []


def test_resolve_proxy_country():
    from tools.proxy_provider import resolve_proxy_country, get_proxy_provider
    import urllib.request
    
    gp = get_proxy_provider()
    gp.enabled = True
    gp.api_key = "test-key"
    gp.proxy_countries["http://12.34.56.78:8080"] = "US"
    
    # Test case 1: Cached country lookup
    assert resolve_proxy_country("http://12.34.56.78:8080") == "US"
    
    # Test case 2: Local address lookup should return None
    assert resolve_proxy_country("http://127.0.0.1:8080") is None
    
    # Test case 3: Mocked external lookup
    original_urlopen = urllib.request.urlopen
    try:
        class DummyResponse:
            def read(self):
                return b'{"countryCode": "DE"}'
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
                
        urllib.request.urlopen = lambda req, timeout=None: DummyResponse()
        assert resolve_proxy_country("http://99.99.99.99:8080") == "DE"
    finally:
        urllib.request.urlopen = original_urlopen


def test_get_next_geo_rotation():
    p = _make_provider()
    p.enabled = True
    p.api_key = "test-key"
    
    # Set items and populate their countries
    proxies = ["http://1.1.1.1:80", "http://2.2.2.2:80", "http://3.3.3.3:80"]
    p.pool.set_items(proxies)
    p.proxy_countries = {
        "http://1.1.1.1:80": "US",
        "http://2.2.2.2:80": "US",
        "http://3.3.3.3:80": "RU",
    }
    
    # Request US proxy specifically with fallback_to_any=False
    p1 = p.get_next(country="US", fallback_to_any=False)
    p2 = p.get_next(country="US", fallback_to_any=False)
    assert p1 in ("http://1.1.1.1:80", "http://2.2.2.2:80")
    assert p2 in ("http://1.1.1.1:80", "http://2.2.2.2:80")
    
    # Request RU proxy specifically with fallback_to_any=False
    p3 = p.get_next(country="RU", fallback_to_any=False)
    assert p3 == "http://3.3.3.3:80"
    
    # Request DE proxy (absent) with fallback_to_any=False -> should return None
    p_none = p.get_next(country="DE", fallback_to_any=False)
    assert p_none is None
    
    # Request DE proxy (absent) with fallback_to_any=True -> should return any proxy from pool
    p_any = p.get_next(country="DE", fallback_to_any=True)
    assert p_any in proxies


if __name__ == "__main__":
    test_transient_probe_failure_does_not_evict()
    test_recovered_proxy_resets_strikes()
    test_no_topup_fetch_when_at_or_above_floor()
    test_topup_fetch_only_when_below_floor()
    test_grace_window_keeps_freshly_admitted_proxy()
    test_resolve_proxy_country()
    test_get_next_geo_rotation()
    print("ALL PROXY PERSISTENCE TESTS PASSED")
