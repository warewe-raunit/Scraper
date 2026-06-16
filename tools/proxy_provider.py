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
from typing import List, Optional

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
        self.count = int(os.getenv("GOODPROXIES_COUNT") or "200")
        self.max_ping = (os.getenv("GOODPROXIES_MAX_PING") or "").strip()
        self.min_works = (os.getenv("GOODPROXIES_MIN_WORKS") or "").strip()
        self.max_time = (os.getenv("GOODPROXIES_MAX_TIME") or "").strip()
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
        self.healthcheck_timeout = float(
            os.getenv("GOODPROXIES_HEALTHCHECK_TIMEOUT") or "6"
        )
        self.healthcheck_workers = int(
            os.getenv("GOODPROXIES_HEALTHCHECK_WORKERS") or "50"
        )
        # --- Continuous health daemon -------------------------------------
        # A background thread keeps the pool stocked with PRE-VERIFIED live
        # proxies so the request path never probes or blocks. Each tick it
        # re-verifies the current pool (evicting any that just died) and, if
        # below target, fetches + healthchecks fresh candidates to top up.
        self.continuous_health = _env_bool("GOODPROXIES_CONTINUOUS_HEALTH", True)
        self.target_live = int(os.getenv("GOODPROXIES_TARGET_LIVE") or "25")
        self.enrich_interval = max(
            3.0, float(os.getenv("GOODPROXIES_ENRICH_INTERVAL") or "15")
        )
        # Re-verify existing pool members each tick so a proxy that died since
        # admission is evicted before a request ever picks it.
        self.reverify_enabled = _env_bool("GOODPROXIES_REVERIFY", True)

    def is_enabled(self) -> bool:
        """True only when explicitly enabled AND a key is configured."""
        return bool(self.enabled and self.api_key)

    # --------------------------------------------------------------- fetching
    def _build_params(self) -> dict:
        params = {
            "key": self.api_key,
            "anon": self.anon,
            "type": self.types,
            "count": str(self.count),
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
        if self.country and self.country.lower() != "any":
            params["country"] = self.country
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
        if ptype not in cls.VALID_TYPES:
            ptype = "http"
        return f"{ptype}://{ip}"

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

    def _fetch_raw(self) -> List[str]:
        """Fetch + apply geo/quality filters + latency sort. No healthcheck."""
        resp = cffi_requests.get(
            self.endpoint,
            params=self._build_params(),
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
        dropped_geo = 0
        for entry in data or []:
            url = self._to_proxy_url(entry)
            if not url or url in seen:
                continue
            # Client-side geo safety net: drop any proxy whose returned country
            # isn't in the allow-set. Guards against upstream geo leaks.
            if self.allowed_countries:
                country = str(entry.get("country") or "").strip().upper()
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
        return [u for (u, _ping, _works) in rows]

    def _probe_one(self, proxy_url: str) -> bool:
        """Return True if the proxy can fetch the healthcheck URL with 2xx/3xx.

        Uses a short timeout so a dead proxy is rejected fast. Any exception
        (timeout, refused, tunnel failure, TLS error, the curl_cffi fingerprint
        unpack bug) counts as 'dead' for admission purposes.
        """
        try:
            resp = cffi_requests.get(
                self.healthcheck_url,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=self.healthcheck_timeout,
                # No impersonate here: we want a cheap liveness probe, and
                # impersonate can trigger the curl_cffi unpack bug on some proxies.
            )
            return 200 <= resp.status_code < 400
        except Exception:
            return False

    def _filter_live(self, urls: List[str]) -> List[str]:
        """Concurrently probe proxies; return only those that respond."""
        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, min(self.healthcheck_workers, len(urls)))
        live: List[str] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = ex.map(lambda u: (u, self._probe_one(u)), urls)
            for url, ok in results:
                if ok:
                    live.append(url)
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
        """One health pass: re-verify current pool, top up to target with fresh
        live proxies. All proxies in the pool after this are confirmed live.
        """
        # 1. Re-verify what we already hold; evict any that died since admission.
        current = self.pool.items
        if current and self.reverify_enabled:
            live_now = self._filter_live(current)
        else:
            live_now = list(current)
        live_set = set(live_now)

        # 2. Below target → fetch fresh candidates and healthcheck them.
        fresh_live: List[str] = []
        need = self.target_live - len(live_set)
        if need > 0:
            try:
                raw = self._fetch_raw()
            except Exception as e:
                logger.warning("proxy_enrich_fetch_failed", error=str(e)[:160])
                raw = []
            candidates = [p for p in raw if p not in live_set]
            if candidates:
                fresh_live = self._filter_live(candidates)

        merged = live_now + [p for p in fresh_live if p not in live_set]
        # set_items preserves cooldowns of survivors and drops evicted ones.
        self.pool.set_items(merged)
        self._last_fetch = time.time()
        logger.info("proxy_pool_enriched",
                    live=len(merged), target=self.target_live,
                    reverified=len(live_now), added=len(merged) - len(live_now))

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
    def get_next(self) -> Optional[str]:
        """Next pre-verified live proxy (``type://ip:port``), or None.

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
        return self.pool.get_next()

    def cool_down(self, proxy: str, duration_seconds: Optional[float] = None) -> None:
        """Rest a misbehaving proxy so it is skipped for a while."""
        if proxy:
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
