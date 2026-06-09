"""
tools/proxy_provider.py — Global rotating proxy provider (good-proxies.ru).

Single source of truth for "where do proxies come from" across every scraper
service (Reddit, X, YouTube, and any future service). It fetches a fresh batch
of elite proxies from the good-proxies.ru API on a configurable interval, holds
them in a shared CooldownPool, and hands them out round-robin with per-proxy
cooldown — exactly the same rotation engine the per-service pools already use.

Why a *batch + interval* instead of "fetch a new proxy every second":
    The good-proxies.ru API is rate-limited to **2 requests / 5 seconds**
    (34,560/day). You cannot legally/technically poll it per request. Instead we
    pull a batch (default 200 proxies) every ``GOODPROXIES_REFRESH_SECONDS``
    (default 60s) and rotate locally between refreshes. The upstream list being
    "fresh every second" does NOT require us to refetch every second — it just
    means each batch we pull is recent.

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
    # Upstream hard limit is 2 requests / 5s. Never refresh faster than this.
    MIN_REFRESH_SECONDS = 5.0
    VALID_TYPES = ("http", "https", "socks4", "socks5")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_fetch = 0.0
        self._load_config()
        self.pool = CooldownPool(
            label="global_proxy",
            default_cooldown=self.cooldown_seconds,
        )

    # ----------------------------------------------------------------- config
    def _load_config(self) -> None:
        self.enabled = _env_bool("GOODPROXIES_ENABLED", False)
        self.api_key = (os.getenv("GOODPROXIES_API_KEY") or "").strip()
        self.endpoint = (os.getenv("GOODPROXIES_API_URL") or self.DEFAULT_ENDPOINT).strip()
        self.types = (os.getenv("GOODPROXIES_TYPES") or "http,https,socks5").strip()
        self.anon = (os.getenv("GOODPROXIES_ANON") or "elite").strip()
        self.count = int(os.getenv("GOODPROXIES_COUNT") or "200")
        self.max_ping = (os.getenv("GOODPROXIES_MAX_PING") or "").strip()
        self.min_works = (os.getenv("GOODPROXIES_MIN_WORKS") or "").strip()
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
        if self.country:
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

    def refresh(self, force: bool = False) -> None:
        """Refresh the pool if the interval has elapsed (or force=True).

        Failures are non-fatal: we keep the last-good list and back off so a
        flaky API never empties the pool mid-scrape.
        """
        if not self.is_enabled():
            return
        now = time.time()
        if not force and (now - self._last_fetch) < self.refresh_seconds:
            return
        with self._lock:
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
        """Next proxy URL (``type://ip:port``) or None when disabled/empty."""
        if not self.is_enabled():
            return None
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
