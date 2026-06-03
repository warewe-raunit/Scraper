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
from api.dependencies import parse_accounts_from_env

logger = structlog.get_logger(__name__)

class XScraperService:
    def __init__(self):
        # Initialize proxy states and cooldown tracker
        accounts = parse_accounts_from_env()
        raw_proxies = [acc["proxy_url"] for acc in accounts if acc.get("proxy_url")]
        
        # Deduplicate proxies and store as dictionary of {proxy_url: cooldown_until}
        self.proxy_cooldowns: Dict[str, float] = {p: 0.0 for p in raw_proxies}
        self._proxy_index = 0
        
        logger.info("x_scraper_service_initialized", proxy_count=len(self.proxy_cooldowns))

    @property
    def proxies(self) -> List[str]:
        return list(self.proxy_cooldowns.keys())

    @proxies.setter
    def proxies(self, val: List[str]):
        self.proxy_cooldowns = {p: self.proxy_cooldowns.get(p, 0.0) for p in val}

    def _get_next_proxy(self) -> Optional[str]:
        """Get the next healthy proxy that is not currently on cooldown."""
        if not self.proxy_cooldowns:
            return None
            
        now = time.time()
        proxies_list = list(self.proxy_cooldowns.keys())
        
        # Find all proxies whose cooldown has expired
        healthy_proxies = [p for p in proxies_list if self.proxy_cooldowns[p] <= now]
        
        if not healthy_proxies:
            # Fallback: All proxies are on cooldown. Pick the one with the shortest remaining cooldown
            best_proxy = min(proxies_list, key=lambda p: self.proxy_cooldowns[p])
            logger.warn("all_x_proxies_on_cooldown_falling_back", fallback_proxy=best_proxy[:30] + "...")
            return best_proxy
            
        # Round-robin select from healthy proxies
        proxy = healthy_proxies[self._proxy_index % len(healthy_proxies)]
        self._proxy_index += 1
        return proxy

    def _cool_down_proxy(self, proxy: str, duration_seconds: int = 300):
        """Put a proxy on cooldown (e.g. on connection errors or scrape failure)."""
        if proxy in self.proxy_cooldowns:
            self.proxy_cooldowns[proxy] = time.time() + duration_seconds
            logger.warn(
                "x_proxy_cooldown_activated",
                proxy=proxy[:30] + "...",
                duration_seconds=duration_seconds,
                until=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.proxy_cooldowns[proxy]))
            )

    async def get_profile(
        self, 
        username: str, 
        limit: int = 20, 
        proxy_url: Optional[str] = None, 
        headless: bool = True
    ) -> Dict[str, Any]:
        """Scrape a user profile and their tweets without authentication, with proxy rotation and retries."""
        max_attempts = 1 if proxy_url else min(3, max(1, len(self.proxies)))
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
        max_attempts = 1 if proxy_url else min(3, max(1, len(self.proxies)))
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
