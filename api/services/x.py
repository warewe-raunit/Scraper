"""
api/services/x.py — Service layer for the unauthenticated X (Twitter) Scraper.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any, List
import structlog

# Ensure workspace root is in path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.unauth_x_scraper import scrape_profile, scrape_search
from tools.rotation import CooldownPool
from api.dependencies import parse_accounts_from_env
from tools.proxy_provider import get_proxy_provider

logger = structlog.get_logger(__name__)

class XScraperService:
    def __init__(self, db: Optional[Any] = None):
        self.db = db
        # Proxy rotation + cooldown is delegated to the shared CooldownPool so
        # the X and YouTube services use one identical implementation.
        accounts = parse_accounts_from_env()
        raw_proxies = [acc["proxy_url"] for acc in accounts if acc.get("proxy_url")]
        self.proxy_pool = CooldownPool(raw_proxies, label="x_proxy", default_cooldown=300)

        logger.info("x_scraper_service_initialized", proxy_count=len(self.proxy_pool))

    @property
    def proxies(self) -> List[str]:
        return self.proxy_pool.items

    @proxies.setter
    def proxies(self, val: List[str]):
        # Preserves existing cooldowns for surviving proxies (handled by the pool).
        self.proxy_pool.set_items(val)

    def _available_proxy_count(self) -> int:
        """How many proxies we can realistically try this call."""
        provider = get_proxy_provider()
        if provider.is_enabled():
            provider.refresh()
            return max(len(provider.pool), len(self.proxies), 3)
        return len(self.proxies)

    def _get_next_proxy(self) -> Optional[str]:
        """Next healthy proxy (round-robin), or shortest-cooldown fallback.

        When the global GoodProxies provider is enabled, proxies come from the
        rotating good-proxies.ru pool; otherwise we use the per-account pool
        built from .env (original behavior).
        """
        provider = get_proxy_provider()
        if provider.is_enabled():
            p = provider.get_next()
            if p:
                return p
        return self.proxy_pool.get_next()

    def _cool_down_proxy(self, proxy: str, duration_seconds: int = 300):
        """Put a proxy on cooldown (e.g. on connection errors or scrape failure)."""
        provider = get_proxy_provider()
        if provider.is_enabled():
            provider.cool_down(proxy, duration_seconds)
        self.proxy_pool.cool_down(proxy, duration_seconds)

    async def get_profile(
        self, 
        username: str, 
        limit: int = 20, 
        proxy_url: Optional[str] = None, 
        headless: bool = True
    ) -> Dict[str, Any]:
        """Scrape a user profile and their tweets without authentication, with proxy rotation and retries."""
        max_attempts = 1 if proxy_url else min(3, max(1, self._available_proxy_count()))
        last_result = {"success": False, "profile": {}, "tweets": [], "error": "No proxies available."}
        
        for attempt in range(1, max_attempts + 1):
            resolved_proxy = proxy_url or self._get_next_proxy()
            if resolved_proxy:
                logger.info(
                    "using_proxy_for_x_profile", 
                    proxy=resolved_proxy[:30] + "...", 
                    attempt=attempt, 
                    max_attempts=max_attempts
                )
            
            result = await scrape_profile(
                username=username,
                limit=limit,
                proxy_url=resolved_proxy,
                headless=headless
            )
            
            if result.get("success"):
                if self.db:
                    if result.get("profile"):
                        await self.db.save_x_profile(result["profile"])
                    if result.get("tweets"):
                        await self.db.save_x_tweets(result["tweets"])
                return result
                
            last_result = result
            if resolved_proxy:
                # Cool down proxy on failure to prevent repeated blocks
                self._cool_down_proxy(resolved_proxy)
                
            logger.warn(
                "x_profile_attempt_failed_rotating_proxy", 
                proxy=resolved_proxy[:30] + "..." if resolved_proxy else "None",
                attempt=attempt, 
                error=result.get("error")
            )
            
        return last_result

    async def search(
        self, 
        query: str, 
        limit: int = 20, 
        proxy_url: Optional[str] = None, 
        headless: bool = True
    ) -> Dict[str, Any]:
        """Scrape tweets matching a search query without authentication, with proxy rotation and retries."""
        max_attempts = 1 if proxy_url else min(3, max(1, self._available_proxy_count()))
        last_result = {"success": False, "tweets": [], "error": "No proxies available."}
        
        for attempt in range(1, max_attempts + 1):
            resolved_proxy = proxy_url or self._get_next_proxy()
            if resolved_proxy:
                logger.info(
                    "using_proxy_for_x_search", 
                    proxy=resolved_proxy[:30] + "...", 
                    attempt=attempt, 
                    max_attempts=max_attempts
                )
                
            result = await scrape_search(
                query=query,
                limit=limit,
                proxy_url=resolved_proxy,
                headless=headless
            )
            
            if result.get("success"):
                if self.db:
                    if result.get("tweets"):
                        await self.db.save_x_tweets(result["tweets"])
                return result
                
            last_result = result
            if resolved_proxy:
                # Cool down proxy on failure to prevent repeated blocks
                self._cool_down_proxy(resolved_proxy)
                
            logger.warn(
                "x_search_attempt_failed_rotating_proxy", 
                proxy=resolved_proxy[:30] + "..." if resolved_proxy else "None",
                attempt=attempt, 
                error=result.get("error")
            )
            
        return last_result
