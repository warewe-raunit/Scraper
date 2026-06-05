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
from tools.proxy_provider import get_proxy_provider

logger = structlog.get_logger(__name__)

class XScraperService:
    def __init__(self, db: Optional[Any] = None):
        self.db = db
        logger.info("x_scraper_service_initialized")

    def _available_proxy_count(self) -> int:
        """How many proxies we can realistically try this call."""
        provider = get_proxy_provider()
        if provider.is_enabled():
            provider.refresh()
            return max(len(provider.pool), 3)
        return 1  # no proxy pool -> single DIRECT attempt

    def _get_next_proxy(self) -> Optional[str]:
        """Next proxy from the global rotating pool, or None (DIRECT) when the
        provider is disabled. The old per-account .env proxy pool was removed."""
        provider = get_proxy_provider()
        if provider.is_enabled():
            return provider.get_next()
        return None

    def _cool_down_proxy(self, proxy: str, duration_seconds: int = 300):
        """Rest a misbehaving proxy in the global pool."""
        provider = get_proxy_provider()
        if provider.is_enabled():
            provider.cool_down(proxy, duration_seconds)

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
