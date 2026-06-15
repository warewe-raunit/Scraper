"""
tools/goodproxies.py — Rotating proxy pool client for good-proxies.ru API.
"""

from __future__ import annotations

import os
import time
import httpx
import asyncio
from pathlib import Path
from typing import List, Optional
import structlog

from tools.rotation import CooldownPool

logger = structlog.get_logger(__name__)

class GoodProxiesProvider:
    _instance: Optional[GoodProxiesProvider] = None
    _lock = asyncio.Lock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(GoodProxiesProvider, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.enabled = os.getenv("GOODPROXIES_ENABLED", "").lower() in ("true", "1", "yes", "on")
        self.api_key = os.getenv("GOODPROXIES_API_KEY", "").strip()
        self.api_url = os.getenv("GOODPROXIES_API_URL", "https://api.good-proxies.ru/api").strip()
        
        # Parse additional filters from env
        self.types = os.getenv("GOODPROXIES_TYPES", "http,https,socks5").strip()
        self.anon = os.getenv("GOODPROXIES_ANON", "elite").strip()
        self.count = os.getenv("GOODPROXIES_COUNT", "200").strip()
        self.max_ping = os.getenv("GOODPROXIES_MAX_PING", "3000").strip()
        self.country = os.getenv("GOODPROXIES_COUNTRY", "any").strip()
        self.refresh_seconds = int(os.getenv("GOODPROXIES_REFRESH_SECONDS", "60"))
        self.cooldown_seconds = int(os.getenv("GOODPROXIES_COOLDOWN_SECONDS", "120"))
        
        self.pool: Optional[CooldownPool] = None
        self.last_fetched = 0.0
        self.fetch_lock = asyncio.Lock()
        self._initialized = True

    async def _fetch_proxies(self) -> List[str]:
        """Query the good-proxies.ru API to export the proxy list."""
        if not self.api_key:
            logger.warning("goodproxies.no_api_key_configured")
            return []
            
        params = {
            "key": self.api_key,
            "type": self.types,
            "count": self.count,
            "ping": self.max_ping,
        }
        if self.country and self.country.lower() != "any":
            params["country"] = self.country
        if self.anon:
            params["anon"] = self.anon

        logger.info("goodproxies.fetching_api_start", api_url=self.api_url)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(self.api_url, params=params)
                if resp.status_code != 200:
                    logger.error("goodproxies.api_error", status_code=resp.status_code, text=resp.text[:200])
                    return []
                
                # Good-proxies.ru returns a plain-text list of ip:port, one per line
                proxies = []
                for line in resp.text.splitlines():
                    line = line.strip()
                    if line and ":" in line:
                        # Prepend http:// protocol for Playwright and httpx/curl compatibility
                        proxies.append(f"http://{line}")
                        
                logger.info("goodproxies.fetched_api_success", count=len(proxies))
                return proxies
        except Exception as e:
            logger.error("goodproxies.fetch_failed", error=str(e))
            return []

    async def get_proxy(self) -> Optional[str]:
        """Resolve next rotated proxy from the pool, refreshing if stale or empty."""
        if not self.enabled or not self.api_key:
            return None

        async with self.fetch_lock:
            now = time.time()
            if not self.pool or len(self.pool) == 0 or (now - self.last_fetched > self.refresh_seconds):
                proxies = await self._fetch_proxies()
                if proxies:
                    if not self.pool:
                        self.pool = CooldownPool(proxies, label="goodproxies", default_cooldown=self.cooldown_seconds)
                    else:
                        self.pool.set_items(proxies)
                    self.last_fetched = now
                elif not self.pool:
                    return None

        proxy = self.pool.get_next()
        if proxy:
            logger.info("goodproxies.resolved_rotated_proxy", proxy=proxy[:30] + "...")
        return proxy

    def mark_failed(self, proxy: str):
        """Put proxy on cooldown after request failure."""
        if self.pool and proxy in self.pool:
            self.pool.cool_down(proxy, self.cooldown_seconds)
