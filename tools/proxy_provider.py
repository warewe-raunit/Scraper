"""
tools/proxy_provider.py — Global rotating proxy provider (good-proxies.ru).

Single source of truth for "where do proxies come from" across every scraper
service (Reddit, X, YouTube, and any future service). It fetches a fresh batch
of elite proxies from the good-proxies.ru API on a configurable interval, holds
them in a shared CooldownPool, and hands them out round-robin with per-proxy
cooldown — exactly the same rotation engine the per-service pools already use.

Why a *batch + interval* instead of "fetch a new proxy every second":
    Empirically tested: the API sustains ~100+ req/s (Cloudflare trips at ~200
    concurrent in-flight). The batch model exists because it is simply more
    efficient — pull 200 proxies in one call, rotate locally for 60s, hit the
    API zero extra times in between. No per-second quota concern.

Enable via .env:
    GOODPROXIES_ENABLED=true
    GOODPROXIES_API_KEY=<your premium key>

When disabled or unconfigured, ``is_enabled()`` is False and every service
falls back to its original proxy source — i.e. zero behavior change.

SECURITY / BAN-RISK NOTE (read this):
    Routing *authenticated* sessions (Reddit/LinkedIn logged-in accounts) through
    rotating public proxies that change IP and country every request is a strong
    bot signal and a common cause of account bans. It also routes session cookies
    / bearer tokens through third-party machines. This provider applies globally
    by explicit request; prefer it for unauthenticated scraping.
"""

from __future__ import annotations

import os
import time
import threading
import re
import urllib.request
import json
from typing import Dict, List, Optional

import structlog
from curl_cffi import requests as cffi_requests

from tools.rotation import CooldownPool

logger = structlog.get_logger(__name__)

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in _TRUE


class GoodProxiesProvider:
    """Fetches + rotates a global pool of proxies from good-proxies.ru."""

    DEFAULT_ENDPOINT = "https://api.good-proxies.ru/api"
    # Conservative floor — avoid hammering the API on misconfigured env.
    MIN_REFRESH_SECONDS = 2.0
    VALID_TYPES = ("http", "https", "socks4", "socks5")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_fetch = 0.0
        self._load_config()
        self.pool = CooldownPool(
            label="global_proxy",
            default_cooldown=self.cooldown_seconds,
        )
        self._refresh_thread = None
        # Continuous-health daemon state
        self._stop_health = threading.Event()
        self._health_thread: Optional[threading.Thread] = None
        # Per-proxy consecutive re-verify failure counter. A proxy is only
        # evicted after `reverify_fail_threshold` CONSECUTIVE misses, so a single
        # transient blip doesn't drain a small pool (this is what makes the pool
        # persist instead of oscillating near-empty).
        self._reverify_strikes: Dict[str, int] = {}
        # Per-proxy grace counter: a freshly-admitted (already-healthchecked)
        # proxy is immune from re-verify eviction for this many enrich cycles, so
        # one flaky re-probe right after admission can't churn it straight back
        # Out (immune this cycle).
        self._grace: Dict[str, int] = {}
        # Map of proxy URL -> country code for geo-consistent rotation
        self.proxy_countries: Dict[str, str] = {}
        # Per-country negative cache for on-demand fetches: country -> unix ts
        # until which we won't re-attempt an on-demand fetch (so a country with
        # zero available proxies upstream can't trigger a fetch storm).
        self._country_fetch_cooldown: Dict[str, float] = {}
        # ---- Permanent dead-proxy blocklist (shared across workers) ----------
        # A proxy that fails a real request (cool_down) or fails re-verification
        # is recorded here and NEVER re-admitted to any pool — no cooldown-and-
        # retry of a dead IP. Persisted to a file so every worker process honors
        # one blocklist ("doesn't come back to any pool"). Resets on restart.
        self.blocklist_enabled = _env_bool("GOODPROXIES_BLOCKLIST_DEAD", True)
        # Whether a re-verify MISS (failed the cheap Google liveness probe N times)
        # also permanently blocklists. Default False: a probe miss is not proof the
        # proxy is dead for Reddit, and on a thin pool (MAX_PING-limited supply)
        # permanently banning every flaky probe burns the candidate well until the
        # pool starves. A real REQUEST failure still blocklists permanently
        # (cool_down/mark_dead). Reverify misses just get evicted + re-fetchable.
        self.blocklist_on_reverify = _env_bool("GOODPROXIES_BLOCKLIST_ON_REVERIFY", False)
        self._dead_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "sessions", "proxy_dead.json",
        )
        self._dead: set = self._load_dead()
        self._dead_loaded_at = 0.0
        # Proxies PROVEN good on a real request (200). The homepage healthcheck
        # admits proxies that then fail the authenticated API call; this set lets
        # get_next PREFER proxies that actually completed a real Reddit request, so
        # the hot path stops retrying through probe-passing-but-API-failing proxies.
        self._known_good: set = set()
        if self.is_enabled():
            if self.continuous_health:
                self.start_health_loop()
            else:
                # Legacy lazy mode: one-shot fetch, refresh on demand.
                self.refresh(force=True)

    # ----------------------------------------------------------------- config
    def _load_config(self) -> None:
        self.enabled = _env_bool("GOODPROXIES_ENABLED", False)
        self.api_key = (os.getenv("GOODPROXIES_API_KEY") or "").strip()
        self.endpoint = (os.getenv("GOODPROXIES_API_URL") or self.DEFAULT_ENDPOINT).strip()
        # socks5 measured ~0% live on this provider — default to http/https so a
        # deploy without a tuned .env doesn't waste the healthcheck on dead socks.
        self.types = (os.getenv("GOODPROXIES_TYPES") or "http,https").strip()
        self.anon = (os.getenv("GOODPROXIES_ANON") or "elite").strip()
        self.count = int(os.getenv("GOODPROXIES_COUNT") or "300")
        self.max_ping = (os.getenv("GOODPROXIES_MAX_PING") or "").strip()
        self.min_works = (os.getenv("GOODPROXIES_MIN_WORKS") or "").strip()
        self.max_time = (os.getenv("GOODPROXIES_MAX_TIME") or "").strip()
        self.speed = (os.getenv("GOODPROXIES_SPEED") or "").strip()
        self.lang = (os.getenv("GOODPROXIES_LANG") or "").strip()
        # US-only by default. Empty env no longer means "worldwide"; it means US.
        # Set GOODPROXIES_COUNTRY explicitly (e.g. "us,ca") to override, or
        # GOODPROXIES_COUNTRY=any to deliberately allow all countries.
        _country = (os.getenv("GOODPROXIES_COUNTRY") or "us").strip()
        self.country = "" if _country.lower() == "any" else _country
        # Client-side geo guard: drop any proxy whose returned country is not in
        # this allow-set, as a safety net in case the upstream geo filter leaks.
        self.allowed_countries = (
            {c.strip().upper() for c in self.country.split(",") if c.strip()}
            if self.country
            else set()
        )
        self.cooldown_seconds = float(os.getenv("GOODPROXIES_COOLDOWN_SECONDS") or "120")
        self.sort_by_latency = _env_bool("GOODPROXIES_SORT_BY_LATENCY", True)
        self.refresh_seconds = max(
            self.MIN_REFRESH_SECONDS,
            float(os.getenv("GOODPROXIES_REFRESH_SECONDS") or "60"),
        )
        # --- Liveness pre-check -------------------------------------------
        # Free/cheap proxy lists are mostly dead-on-arrival. Before admitting a
        # batch to the pool we concurrently probe each proxy against a fast,
        # tiny endpoint and keep only the ones that actually return 200. This
        # turns "200 proxies, ~90% dead" into a small pool of confirmed-live
        # proxies and eliminates the retry storm seen in the logs.
        self.healthcheck_enabled = _env_bool("GOODPROXIES_HEALTHCHECK", True)
        self.healthcheck_url = (
            os.getenv("GOODPROXIES_HEALTHCHECK_URL")
            or "https://www.google.com/generate_204"
        ).strip()
        # Multi-target liveness: validate each proxy against EVERY URL here — a
        # proxy is "live" only if it reaches all of them. Set this to e.g.
        # "https://www.reddit.com/,https://www.linkedin.com/robots.txt" so every
        # admitted proxy works for BOTH Reddit and LinkedIn, not just the first
        # service — a LinkedIn fetch then never burns time on a Reddit-only proxy.
        # Comma-separated; falls back to the single healthcheck_url above.
        _urls_raw = (os.getenv("GOODPROXIES_HEALTHCHECK_URLS") or "").strip()
        self.healthcheck_urls = (
            [u.strip() for u in _urls_raw.split(",") if u.strip()]
            if _urls_raw else [self.healthcheck_url]
        )
        self.healthcheck_timeout = float(
            os.getenv("GOODPROXIES_HEALTHCHECK_TIMEOUT") or "6"
        )
        self.healthcheck_workers = int(
            os.getenv("GOODPROXIES_HEALTHCHECK_WORKERS") or "50"
        )
        # Strict mode: validate against the REAL target (e.g. www.reddit.com) and
        # admit only a clean 2xx/3xx — so "live" means "actually reaches Reddit",
        # not "tunnels to Google". A Reddit-blocked proxy (403/429) is rejected.
        # Uses impersonation so the probe looks like the real request path.
        self.healthcheck_strict = _env_bool("GOODPROXIES_HEALTHCHECK_STRICT", False)
        # Number of consecutive probes a NEW proxy must pass to be admitted. The
        # double-probe (2) squares the death rate on flaky free proxies and was
        # admitting ~0 from each fetch (added=0 in the logs). 1 = single probe,
        # far higher yield; a one-off responder that sneaks in is evicted by the
        # next re-verify wave anyway, so the gate isn't load-bearing on its own.
        self.admit_probes = max(1, int(os.getenv("GOODPROXIES_ADMIT_PROBES") or "1"))
        # --- Continuous health daemon -------------------------------------
        # A background thread keeps the pool stocked with PRE-VERIFIED live
        # proxies so the request path never probes or blocks. Each tick it
        # re-verifies the current pool (evicting any that just died) and, if
        # below target, fetches + healthchecks fresh candidates to top up.
        self.continuous_health = _env_bool("GOODPROXIES_CONTINUOUS_HEALTH", True)
        self.target_live = int(os.getenv("GOODPROXIES_TARGET_LIVE") or "40")
        self.enrich_interval = max(
            3.0, float(os.getenv("GOODPROXIES_ENRICH_INTERVAL") or "15")
        )
        # Re-verify existing pool members each tick so a proxy that died since
        # admission is evicted before a request ever picks it.
        self.reverify_enabled = _env_bool("GOODPROXIES_REVERIFY", True)
        # Tolerance: how many CONSECUTIVE failed re-probes before a proxy is
        # evicted. >1 keeps flaky-but-usable proxies through transient blips so
        # the pool persists instead of draining to empty on one bad probe wave.
        self.reverify_fail_threshold = max(
            1, int(os.getenv("GOODPROXIES_REVERIFY_FAILS") or "2")
        )
        # Grace cycles for a freshly-admitted proxy before it is eligible for
        # re-verify eviction. >0 lets newly added proxies persist through a noisy
        # probe wave right after admission (set to 0 to disable).
        self.reverify_grace_cycles = max(
            0, int(os.getenv("GOODPROXIES_REVERIFY_GRACE_CYCLES") or "1")
        )
        # Floor: only run the (expensive) top-up fetch+healthcheck when the live
        # pool drops BELOW this. While at/above the floor the daemon just gently
        # re-verifies what it holds — so a healthy pool PERSISTS instead of
        # refetching every tick chasing an unreachable target_live. Capped to
        # target_live (a floor above the ceiling makes no sense).
        self.min_live = max(
            1, min(int(os.getenv("GOODPROXIES_MIN_LIVE") or "12"), self.target_live)
        )
        # --- On-demand per-country fetch ----------------------------------
        # When a caller requests a country (e.g. JP) that has no proxy in the
        # standing pool, fetch + healthcheck that country specifically at
        # request time and merge the live ones in, instead of failing fast.
        # This covers the "rare country intermittently has zero" gap without
        # bloating the standing pool with every country.
        self.on_demand_country = _env_bool("GOODPROXIES_ON_DEMAND_COUNTRY", True)
        # How many candidates to request + probe for an on-demand country fetch.
        self.on_demand_count = max(
            1, int(os.getenv("GOODPROXIES_ON_DEMAND_COUNT") or "60")
        )
        # Negative-cache TTL: after an on-demand fetch (success or not), don't
        # re-fetch the same country for this many seconds.
        self.on_demand_cooldown = max(
            5.0, float(os.getenv("GOODPROXIES_ON_DEMAND_COOLDOWN") or "45")
        )

    def is_enabled(self) -> bool:
        """True only when explicitly enabled AND a key is configured."""
        return bool(self.enabled and self.api_key)

    # --------------------------------------------------------------- fetching
    def _build_params(self, country_override: Optional[str] = None, count_override: Optional[int] = None) -> dict:
        params = {
            "key": self.api_key,
            "anon": self.anon,
            "type": self.types,
            "count": str(count_override or self.count),
            "get": "json",
            # `country` is now requested so we can verify geo client-side.
            "fields": "ip,type,ping,works,country",
        }
        if self.max_ping:
            params["ping"] = self.max_ping
        if self.min_works:
            params["works"] = self.min_works
        if self.max_time:
            params["time"] = self.max_time
        if self.speed:
            params["speed"] = self.speed
        if self.lang:
            params["lang"] = self.lang
        # An explicit override (on-demand single-country fetch) wins over the
        # standing GOODPROXIES_COUNTRY filter.
        country = country_override if country_override is not None else self.country
        if country and country.lower() != "any":
            params["country"] = country
            params["cm"] = "include"  # return ONLY these countries (explicit)
        return params

    @staticmethod
    def _as_float(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _to_proxy_url(cls, entry: dict) -> Optional[str]:
        # API JSON gives {"ip": "1.2.3.4:8080", "type": "socks5", ...}
        ip = str(entry.get("ip") or "").strip()
        ptype = str(entry.get("type") or "http").strip().lower()
        if not ip or ":" not in ip:
            return None
        if ptype == "https":
            ptype = "http"
        elif ptype not in cls.VALID_TYPES:
            ptype = "http"
        # Emit REMOTE-DNS schemes for SOCKS so curl_cffi/libcurl resolves the
        # target hostname through the proxy. Plain socks5:// (local DNS) fails on
        # most socks5 proxies with curl err 97; socks5h:// works. Measured
        # 2026-06-17: socks5:// -> 0/15 live, socks5h:// -> 8/15 live.
        scheme = {"socks5": "socks5h", "socks4": "socks4a"}.get(ptype, ptype)
        return f"{scheme}://{ip}"

    def _fetch(self) -> List[str]:
        """Fetch + filter + healthcheck (the one-shot path used by refresh())."""
        raw = self._fetch_raw()
        if self.healthcheck_enabled and raw:
            live = self._filter_live(raw)
            logger.info("goodproxies_healthcheck",
                        received=len(raw), live=len(live), dropped=len(raw) - len(live))
            # If every proxy fails (e.g. the probe endpoint itself is down),
            # keep the unfiltered list rather than going dark.
            return live or raw
        return raw

    def _fetch_raw(
        self,
        country_override: Optional[str] = None,
        count_override: Optional[int] = None,
        bypass_geo_guard: bool = False,
    ) -> List[str]:
        """Fetch + apply geo/quality filters + latency sort. No healthcheck.

        ``country_override`` requests a single specific country (on-demand path),
        and ``bypass_geo_guard`` skips the standing ``allowed_countries`` drop so
        that explicitly-requested country isn't filtered out by a US-only env.
        """
        resp = cffi_requests.get(
            self.endpoint,
            params=self._build_params(country_override, count_override),
            timeout=20,
            impersonate="chrome120",
        )
        text = resp.text or ""
        if resp.status_code != 200 or text.lstrip().startswith("Error"):
            raise RuntimeError(
                f"good-proxies API error: status={resp.status_code} body={text[:160]!r}"
            )
        data = resp.json()
        if isinstance(data, dict):  # wrap=1 envelope, defensive
            data = data.get("data", [])
        min_works = self._as_float(self.min_works, 0.0) if self.min_works else 0.0
        rows = []  # (url, ping, works)
        seen = set()
        dead = self._dead_set() if self.blocklist_enabled else set()
        dropped_geo = 0
        dropped_dead = 0
        for entry in data or []:
            url = self._to_proxy_url(entry)
            if not url or url in seen:
                continue
            if url in dead:  # never re-admit a blocklisted (dead) proxy
                dropped_dead += 1
                continue
            country = str(entry.get("country") or "").strip().upper()
            if country:
                self.proxy_countries[url] = country
            # Client-side geo safety net: drop any proxy whose returned country
            # isn't in the allow-set. Guards against upstream geo leaks. Skipped
            # for an explicit on-demand single-country fetch (bypass_geo_guard).
            if self.allowed_countries and not bypass_geo_guard:
                if country and country not in self.allowed_countries:
                    dropped_geo += 1
                    continue
            works = self._as_float(entry.get("works"), 0.0)
            if min_works and works < min_works:
                continue  # client-side reliability ("speed") guard
            # NOTE: the request `ping` filter is in ms, but the JSON `ping`
            # field is response time in seconds. We only use it to SORT
            # (unit-agnostic, fastest first); the ms ceiling stays server-side.
            ping = self._as_float(entry.get("ping"), float("inf"))
            seen.add(url)
            rows.append((url, ping, works))
        if self.sort_by_latency:
            rows.sort(key=lambda r: r[1])  # lowest latency first
        if dropped_geo:
            logger.info(
                "goodproxies_geo_filtered",
                dropped=dropped_geo,
                kept=len(rows),
                allowed=sorted(self.allowed_countries),
            )
        if dropped_dead:
            logger.info("goodproxies_blocklist_filtered",
                        dropped=dropped_dead, blocklist_size=len(dead), kept=len(rows))
        return [u for (u, _ping, _works) in rows]

    def _probe_one(self, proxy_url: str) -> bool:
        """Alive only if the proxy reaches EVERY configured healthcheck target.

        Probing multiple targets (e.g. Reddit + LinkedIn) keeps the live pool
        usable for every service instead of just the first — so a LinkedIn fetch
        never lands on a proxy that only ever reached Reddit. Short-circuits on the
        first miss to keep the common (dead-proxy) case cheap.
        """
        for url in self.healthcheck_urls:
            if not self._probe_url(proxy_url, url):
                return False
        return True

    def _probe_url(self, proxy_url: str, target_url: str) -> bool:
        """Return True if the proxy returns a usable HTTP response for one target.

        This is a *liveness* probe, not a content check: getting any response back
        proves the proxy connected and tunneled (HTTPS CONNECT succeeded). The
        target's status code does NOT indicate proxy health — many datacenter/
        proxy IPs get a 403/429 from the healthcheck host while tunneling
        perfectly, so the old ``2xx/3xx only`` rule falsely evicted good, live
        proxies. Real failures (timeout, connection refused, CONNECT tunnel
        failure, TLS error, the curl_cffi unpack bug) raise and count as dead.
        A 5xx is the one status we still reject, since it usually means the proxy
        gateway itself errored rather than the target responding.
        """
        try:
            if self.healthcheck_strict:
                # Representative probe: impersonate (matches the real request path)
                # and require a clean 2xx/3xx — a blocked proxy returns 403/429 and
                # is correctly rejected.
                resp = cffi_requests.get(
                    target_url,
                    proxies={"http": proxy_url, "https": proxy_url},
                    timeout=self.healthcheck_timeout,
                    impersonate="chrome120",
                )
                return 200 <= resp.status_code < 400
            # Cheap liveness probe: any response < 500 proves the tunnel works. No
            # impersonate (can trigger the curl_cffi unpack bug on some proxies).
            resp = cffi_requests.get(
                target_url,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=self.healthcheck_timeout,
            )
            return resp.status_code < 500
        except Exception:
            return False

    def _probe_all(self, urls: List[str]) -> Dict[str, bool]:
        """Concurrently probe every proxy; return {url: alive?} preserving order."""
        from concurrent.futures import ThreadPoolExecutor
        if not urls:
            return {}
        workers = max(1, min(self.healthcheck_workers, len(urls)))
        results: Dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for url, ok in ex.map(lambda u: (u, self._probe_one(u)), urls):
                results[url] = ok
        return results

    def _filter_live(self, urls: List[str]) -> List[str]:
        """Admit proxies that pass ``admit_probes`` consecutive probes (default 1).
        Re-verification of *existing* members stays a single tolerant probe (see
        _enrich_once); this is the ADMISSION gate. More passes = stricter but lower
        yield — 2 was admitting ~0 from each fetch on this provider's flaky pool."""
        live = urls
        for _ in range(self.admit_probes):
            live = [u for u, ok in self._probe_all(live).items() if ok]
            if not live:
                return []
        return live

    # ------------------------------------------------- continuous health daemon
    def start_health_loop(self) -> None:
        """Start the background health daemon (idempotent).

        The loop keeps the pool stocked with pre-verified live proxies. Safe to
        call multiple times — only one thread ever runs. Runs an immediate
        enrich pass synchronously-ish via the thread so the pool warms fast.
        """
        if not self.is_enabled():
            return
        if self._health_thread is not None and self._health_thread.is_alive():
            return
        self._stop_health.clear()
        self._health_thread = threading.Thread(
            target=self._health_loop, name="proxy-health", daemon=True
        )
        self._health_thread.start()
        logger.info("proxy_health_loop_started",
                    target_live=self.target_live, interval=self.enrich_interval,
                    reverify=self.reverify_enabled)

    def stop_health_loop(self) -> None:
        """Signal the daemon to stop (used in tests/shutdown)."""
        self._stop_health.set()

    def _health_loop(self) -> None:
        while not self._stop_health.is_set():
            try:
                self._enrich_once()
            except Exception as e:  # never let the daemon die
                logger.warning("proxy_health_loop_error", error=str(e)[:200])
            # Interruptible sleep so stop_health_loop() returns promptly.
            self._stop_health.wait(self.enrich_interval)

    def _enrich_once(self) -> None:
        """One health pass that makes the pool PERSIST:

        1. Re-verify current members *tolerantly* — a proxy is only evicted after
           `reverify_fail_threshold` consecutive failed probes, so a single flaky
           wave doesn't drain a small pool.
        2. Only when the live count drops BELOW `min_live` do we run the
           expensive top-up fetch+healthcheck (filling toward `target_live`).
           While at/above the floor we keep what we hold and refetch nothing.
        """
        # 1. Tolerant re-verification (with grace for freshly-admitted proxies).
        current = self.pool.items
        graced_count = 0
        if current and self.reverify_enabled:
            # Proxies still inside their grace window are kept WITHOUT probing,
            # so a flaky probe immediately after admission can't evict them.
            to_probe = [p for p in current if self._grace.get(p, 0) <= 0]
            probed = self._probe_all(to_probe) if to_probe else {}
            live_now: List[str] = []
            for p in current:
                grace_left = self._grace.get(p, 0)
                if grace_left > 0:
                    self._grace[p] = grace_left - 1
                    live_now.append(p)  # immune this cycle
                    graced_count += 1
                elif probed.get(p):
                    self._reverify_strikes.pop(p, None)
                    live_now.append(p)
                else:
                    strikes = self._reverify_strikes.get(p, 0) + 1
                    if strikes < self.reverify_fail_threshold:
                        self._reverify_strikes[p] = strikes
                        live_now.append(p)  # within tolerance — keep it
                    else:
                        self._reverify_strikes.pop(p, None)  # confirmed dead (this pass)
                        # Evict from the pool (not appended to live_now). Only
                        # blocklist permanently if explicitly opted in — otherwise
                        # it stays re-fetchable so the well doesn't dry up.
                        if self.blocklist_enabled and self.blocklist_on_reverify:
                            self._record_dead(p)
        else:
            live_now = list(current)
        live_set = set(live_now)

        # 2. Floor-gated top-up: refetch only when genuinely short of proxies.
        do_topup = len(live_set) < self.min_live
        now = time.time()
        can_fetch = (now - self._last_fetch) >= self.refresh_seconds
        
        fresh_live: List[str] = []
        topped_up = False
        if do_topup and can_fetch:
            topped_up = True
            need = self.target_live - len(live_set)
            try:
                raw = self._fetch_raw()
            except Exception as e:
                logger.warning("proxy_enrich_fetch_failed", error=str(e)[:160])
                raw = []
            candidates = [p for p in raw if p not in live_set]
            if candidates:
                fresh_live = self._filter_live(candidates)[: max(0, need)]
            self._last_fetch = now

        newly_added = [p for p in fresh_live if p not in live_set]
        merged = live_now + newly_added
        # set_items preserves cooldowns of survivors and drops evicted ones.
        self.pool.set_items(merged)
        # Grant freshly-admitted proxies their grace window so they persist
        # through the next probe wave instead of churning straight back out.
        for p in newly_added:
            self._grace[p] = self.reverify_grace_cycles
        # Forget strike/grace records for proxies no longer tracked.
        merged_set = set(merged)
        self._reverify_strikes = {
            p: s for p, s in self._reverify_strikes.items() if p in merged_set
        }
        self._grace = {p: g for p, g in self._grace.items() if p in merged_set}
        logger.info("proxy_pool_enriched",
                    live=len(merged), target=self.target_live, min_live=self.min_live,
                    reverified=len(live_now), added=len(newly_added),
                    graced=graced_count, topped_up=topped_up)

    def refresh(self, force: bool = False) -> None:
        """Refresh the pool if the interval has elapsed (or force=True) in a background thread.

        Failures are non-fatal: we keep the last-good list and back off so a
        flaky API never empties the pool mid-scrape.
        """
        if not self.is_enabled():
            return
        now = time.time()
        if not force and (now - self._last_fetch) < self.refresh_seconds:
            return

        # Check if another refresh is already running in background
        if getattr(self, "_refresh_thread", None) and self._refresh_thread.is_alive():
            return

        # Spawn daemon thread to refresh
        self._refresh_thread = threading.Thread(
            target=self._run_refresh_sync,
            args=(force,),
            daemon=True
        )
        self._refresh_thread.start()

    def _run_refresh_sync(self, force: bool = False) -> None:
        """Perform the actual synchronous fetch and update within lock."""
        with self._lock:
            # Recheck condition inside lock
            now = time.time()
            if not force and (now - self._last_fetch) < self.refresh_seconds:
                return
            try:
                proxies = self._fetch()
            except Exception as e:
                logger.warning("goodproxies_refresh_failed", error=str(e), kept=len(self.pool))
                self._last_fetch = now  # back off; don't hammer on errors
                return
            if proxies:
                # set_items preserves cooldowns for proxies surviving the swap.
                self.pool.set_items(proxies)
                logger.info("goodproxies_refreshed", count=len(proxies))
            else:
                logger.warning("goodproxies_empty_response_keeping_old", kept=len(self.pool))
            self._last_fetch = now

    # -------------------------------------------------------------- selection
    def get_next(self, country: Optional[str] = None, fallback_to_any: bool = True) -> Optional[str]:
        """Next pre-verified live proxy (``type://ip:port``), or None.

        If a country code (e.g. 'US') is provided, it tries to select a proxy from
        that country.

        Pure pool read — no inline fetch/probe — when the continuous health
        daemon is running (it owns enrichment). Falls back to the lazy
        interval-refresh only in legacy mode. Returning None never blocks the
        caller: the service falls back to a pinned/static/direct connection.
        """
        if not self.is_enabled():
            return None
        if self.continuous_health:
            # Daemon should be running; start it if somehow it isn't (idempotent).
            if self._health_thread is None or not self._health_thread.is_alive():
                self.start_health_loop()
        else:
            self.refresh()

        if country:
            country = country.strip().upper()
            candidates = [p for p in self.pool.items if self.proxy_countries.get(p) == country]
            if candidates:
                return self.pool.get_next(candidates=candidates)
            if not fallback_to_any:
                return None

        # Prefer proxies PROVEN good on a real request (and still live) — this is
        # what cuts the retry-through-dead-proxies latency. Fall back to any live
        # proxy when none are proven yet (cold start) or all are cooling.
        if self._known_good:
            good_live = [p for p in self.pool.items if p in self._known_good]
            if good_live:
                sel = self.pool.get_next(candidates=good_live)
                if sel:
                    return sel

        return self.pool.get_next()

    def ensure_country(self, country: str, want: int = 1) -> int:
        """On-demand: make sure the pool holds at least one live proxy for
        ``country``, fetching + healthchecking that country specifically if not.

        Returns the number of newly-admitted live proxies. Blocking (network
        fetch + healthcheck) — callers on an event loop should run it in an
        executor. Bounded by a per-country negative cache so a country with no
        upstream proxies can't trigger repeated slow fetches.
        """
        if not self.is_enabled() or not self.on_demand_country:
            return 0
        country = country.strip().upper()
        if not country:
            return 0
        # Already covered? Nothing to do.
        if any(self.proxy_countries.get(p) == country for p in self.pool.items):
            return 0
        now = time.time()
        if now < self._country_fetch_cooldown.get(country, 0.0):
            return 0
        # Reserve the cooldown up front so concurrent callers don't all fetch.
        self._country_fetch_cooldown[country] = now + self.on_demand_cooldown
        try:
            raw = self._fetch_raw(
                country_override=country,
                count_override=self.on_demand_count,
                bypass_geo_guard=True,
            )
        except Exception as e:
            logger.warning("ondemand_country_fetch_failed", country=country, error=str(e)[:160])
            return 0
        live = self._filter_live(raw[: self.on_demand_count]) if raw else []
        added = 0
        for p in live:
            if p not in self.pool:
                self.pool.add(p)
                # Grant grace so the health daemon's next re-verify wave doesn't
                # immediately churn the freshly-admitted proxy back out.
                self._grace[p] = self.reverify_grace_cycles
                added += 1
        logger.info("ondemand_country_fetched",
                    country=country, received=len(raw), live=len(live), added=added)
        return added

    # ----------------------------------------------------- dead-proxy blocklist
    def _load_dead(self) -> set:
        try:
            with open(self._dead_path, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            return set()

    def _dead_set(self) -> set:
        """The blocklist, refreshed from disk every ~5s so a proxy another worker
        just killed is honored here too."""
        now = time.time()
        if now - self._dead_loaded_at > 5.0:
            self._dead |= self._load_dead()
            self._dead_loaded_at = now
        return self._dead

    def _record_dead(self, proxy: str) -> None:
        """Add to the persistent blocklist (no pool mutation). Best-effort write
        that merges concurrent worker additions; a dropped write is re-applied on
        the next death, so the blocklist only ever grows."""
        if not proxy or proxy in self._dead:
            return
        self._dead.add(proxy)
        try:
            os.makedirs(os.path.dirname(self._dead_path), exist_ok=True)
            merged = self._dead | self._load_dead()
            tmp = f"{self._dead_path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sorted(merged), f)
            os.replace(tmp, self._dead_path)
            self._dead = merged
        except OSError:
            pass

    def mark_good(self, proxy: str) -> None:
        """Record that a proxy completed a REAL request (200). get_next prefers
        these so the hot path stops retrying probe-only proxies."""
        if proxy:
            self._known_good.add(proxy)

    def mark_dead(self, proxy: str) -> None:
        """Blocklist a proxy permanently AND drop it from the live pool now."""
        if not proxy:
            return
        self._known_good.discard(proxy)
        self._record_dead(proxy)
        items = self.pool.items
        if proxy in items:
            self.pool.set_items([p for p in items if p != proxy])

    def cool_down(self, proxy: str, duration_seconds: Optional[float] = None) -> None:
        """A proxy failed a real request. Blocklist it permanently — a failed
        public proxy is treated as dead and never re-admitted to any pool —
        instead of the old cool-down-and-reuse. ``duration_seconds`` is accepted
        for API compatibility and ignored. (Set GOODPROXIES_BLOCKLIST_DEAD=false
        to restore the old temporary-cooldown behavior.)"""
        if not proxy:
            return
        if self.blocklist_enabled:
            self.mark_dead(proxy)
        else:
            self.pool.cool_down(proxy, duration_seconds)


_provider: Optional[GoodProxiesProvider] = None
_provider_lock = threading.Lock()


def get_proxy_provider() -> GoodProxiesProvider:
    """Process-wide singleton accessor for the global proxy provider."""
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = GoodProxiesProvider()
    return _provider


def _proxy_host(proxy_url: Optional[str]) -> Optional[str]:
    """Extract the host/IP from a proxy URL like http://1.2.3.4:8080."""
    if not proxy_url:
        return None
    m = re.search(r"://([^:/@]+)", proxy_url)
    return m.group(1) if m else None


def resolve_proxy_country(proxy_url: Optional[str]) -> Optional[str]:
    """Resolve the country code (e.g. 'US') for a proxy URL.

    First attempts local cache from GoodProxiesProvider, falling back to a quick
    external IP geo-lookup.
    """
    if not proxy_url:
        return None
    try:
        gp = get_proxy_provider()
        if gp.is_enabled() and proxy_url in gp.proxy_countries:
            return gp.proxy_countries[proxy_url].strip().upper()
    except Exception:
        pass

    host = _proxy_host(proxy_url)
    if not host:
        return None

    if host.lower() in ("localhost", "127.0.0.1", "::1"):
        return None

    try:
        url = f"http://ip-api.com/json/{host}?fields=countryCode"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            data = json.loads(response.read().decode())
            cc = data.get("countryCode")
            if cc:
                return cc.strip().upper()
    except Exception as e:
        logger.warning("proxy_geo_resolution_failed", host=host, error=str(e))
    return None
