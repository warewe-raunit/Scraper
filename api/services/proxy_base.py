"""
api/services/proxy_base.py — Shared proxy-rotation behavior for HTTP scraper
services (X, YouTube).

Both services previously hand-rolled an identical block: build a CooldownPool
from the per-account .env proxies, expose a ``proxies`` property, prefer the
global GoodProxies pool when enabled, and cool down a misbehaving proxy in both
pools. That duplication now lives here once.

The only real difference between the two was that YouTube prefers SOCKS proxies
from the *unverified* .env fallback pool (raw HTTP lists often can't CONNECT for
HTTPS), while X does not. That is captured by the ``prefer_socks`` flag.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

from api.config import PROXY_DEFAULT_COOLDOWN_SECONDS
from api.dependencies import parse_accounts_from_env
from tools.proxy_provider import get_proxy_provider
from tools.rotation import CooldownPool

_SOCKS_PREFIXES = ("socks5://", "socks4://", "socks5h://")


class ProxyRotatingService:
    """Mixin/base providing shared proxy selection + cooldown for HTTP scrapers."""

    #: When True, prefer SOCKS proxies from the unverified .env fallback pool.
    prefer_socks: bool = False

    def __init__(
        self,
        db: Optional[Any] = None,
        *,
        pool_label: str = "proxy",
        default_cooldown: int = PROXY_DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self.db = db
        accounts = parse_accounts_from_env()
        raw_proxies = [acc["proxy_url"] for acc in accounts if acc.get("proxy_url")]
        self.proxy_pool = CooldownPool(
            raw_proxies, label=pool_label, default_cooldown=default_cooldown
        )

    @property
    def proxies(self) -> List[str]:
        return self.proxy_pool.items

    @proxies.setter
    def proxies(self, val: List[str]) -> None:
        # Preserves existing cooldowns for surviving proxies (handled by the pool).
        self.proxy_pool.set_items(val)

    def _get_next_proxy(self, country: Optional[str] = None) -> Optional[str]:
        """Next healthy proxy (round-robin), or shortest-cooldown fallback.

        When the global GoodProxies provider is enabled, proxies come from the
        pre-verified rotating good-proxies.ru pool; otherwise we use the
        per-account pool built from .env. The .env pool is not healthchecked, so
        when ``prefer_socks`` is set we favor SOCKS entries (which tunnel HTTPS
        without relying on HTTP-proxy CONNECT support).
        """
        provider = get_proxy_provider()
        if provider.is_enabled():
            p = provider.get_next(country=country, fallback_to_any=False)
            if p:
                return p

        # Fallback to local pool
        if country:
            country = country.strip().upper()
            from tools.proxy_provider import resolve_proxy_country
            candidates = []
            for p in self.proxy_pool.items:
                p_country = resolve_proxy_country(p)
                if p_country == country:
                    candidates.append(p)
            if self.prefer_socks:
                socks_candidates = [
                    c for c in candidates if c.startswith(_SOCKS_PREFIXES)
                ]
                if socks_candidates:
                    return self.proxy_pool.get_next(candidates=socks_candidates)
            if candidates:
                return self.proxy_pool.get_next(candidates=candidates)
            return None

        if self.prefer_socks:
            socks_candidates = [
                p for p in self.proxy_pool.items if p.startswith(_SOCKS_PREFIXES)
            ]
            if socks_candidates:
                p = self.proxy_pool.get_next(candidates=socks_candidates)
                if p:
                    return p
        return self.proxy_pool.get_next()

    async def _get_next_proxy_async(self, country: Optional[str] = None) -> Optional[str]:
        """Like ``_get_next_proxy`` but, when a country is requested and the pool
        has no proxy for it, triggers an on-demand fetch for that country before
        retrying — without blocking the event loop.

        The standing pool is checked first (fast path, no network). Only on a
        miss do we fetch+healthcheck that country in a worker thread, then
        re-select. Returns None if the country still has no live proxy (caller
        fails fast rather than leaking the server IP).
        """
        proxy = self._get_next_proxy(country=country)
        if proxy or not country:
            return proxy

        provider = get_proxy_provider()
        if not provider.is_enabled():
            return proxy  # None — local-pool-only mode, nothing to fetch

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, provider.ensure_country, country)
        return self._get_next_proxy(country=country)

    def _cool_down_proxy(
        self, proxy: str, duration_seconds: int = PROXY_DEFAULT_COOLDOWN_SECONDS
    ) -> None:
        """Put a proxy on cooldown in both the global and per-account pools."""
        provider = get_proxy_provider()
        if provider.is_enabled():
            provider.cool_down(proxy, duration_seconds)
        self.proxy_pool.cool_down(proxy, duration_seconds)
