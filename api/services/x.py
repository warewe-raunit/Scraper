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
from tools.retry_policy import AttemptOutcome, RetryPolicy, robust_retry
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

    def _retry_policy(self, label: str, *, pinned: bool) -> RetryPolicy:
        """Build the bounded-retry policy for one scrape call.

        - pinned (caller supplied an explicit proxy_url): single attempt, no
          rotation, no waiting.
        - otherwise: cap attempts at the larger of 8 or the available proxy
          count (capped at 15) and keep the ~90s wall-clock budget so the loop
          spans at least one 60s pool refill.
        """
        if pinned:
            return RetryPolicy(max_attempts=1, time_budget_seconds=30.0, label=label)
        attempts = max(8, min(15, self._available_proxy_count()))
        return RetryPolicy(max_attempts=attempts, label=label)

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
        """Scrape a user profile and their tweets without authentication.

        Uses the shared bounded-retry policy: rotates through proxies long
        enough to span at least one pool refill, then ALWAYS returns a
        structured result (data on success, or a best-effort failure dict) -
        never raises and never terminates with no output. A pinned proxy_url
        is honored with a single attempt (no rotation).
        """
        async def _attempt(n: int) -> AttemptOutcome:
            resolved_proxy = proxy_url or self._get_next_proxy()
            if resolved_proxy:
                logger.info(
                    "using_proxy_for_x_profile",
                    proxy=resolved_proxy[:30] + "...",
                    attempt=n,
                )
            result = await scrape_profile(
                username=username,
                limit=limit,
                proxy_url=resolved_proxy,
                headless=headless,
            )
            if result.get("success"):
                if self.db:
                    if result.get("profile"):
                        await self.db.save_x_profile(result["profile"])
                    if result.get("tweets"):
                        await self.db.save_x_tweets(result["tweets"])
                return AttemptOutcome(success=True, result=result)
            logger.warning(
                "x_profile_attempt_failed_rotating_proxy",
                proxy=resolved_proxy[:30] + "..." if resolved_proxy else "None",
                attempt=n,
                error=result.get("error"),
            )
            return AttemptOutcome(
                success=False,
                result=result,
                retryable=not proxy_url,
                cooldown_target=resolved_proxy if not proxy_url else None,
                error=result.get("error"),
            )

        policy = self._retry_policy("x_profile", pinned=bool(proxy_url))
        return await robust_retry(
            _attempt,
            policy=policy,
            on_cooldown=self._cool_down_proxy,
            exhausted_result=lambda last: (
                last.result if last and last.result is not None
                else {"success": False, "profile": {}, "tweets": [],
                      "error": "No proxies available."}
            ),
        )

    async def search(
        self, 
        query: str, 
        limit: int = 20, 
        proxy_url: Optional[str] = None, 
        headless: bool = True
    ) -> Dict[str, Any]:
        """Scrape tweets matching a search query without authentication.

        Same robust bounded-retry behavior as get_profile: rotates proxies
        across at least one pool refill and always returns a structured result
        instead of terminating with no output.
        """
        async def _attempt(n: int) -> AttemptOutcome:
            resolved_proxy = proxy_url or self._get_next_proxy()
            if resolved_proxy:
                logger.info(
                    "using_proxy_for_x_search",
                    proxy=resolved_proxy[:30] + "...",
                    attempt=n,
                )
            result = await scrape_search(
                query=query,
                limit=limit,
                proxy_url=resolved_proxy,
                headless=headless,
            )
            if result.get("success"):
                if self.db and result.get("tweets"):
                    await self.db.save_x_tweets(result["tweets"])
                return AttemptOutcome(success=True, result=result)
            logger.warning(
                "x_search_attempt_failed_rotating_proxy",
                proxy=resolved_proxy[:30] + "..." if resolved_proxy else "None",
                attempt=n,
                error=result.get("error"),
            )
            return AttemptOutcome(
                success=False,
                result=result,
                retryable=not proxy_url,
                cooldown_target=resolved_proxy if not proxy_url else None,
                error=result.get("error"),
            )

        policy = self._retry_policy("x_search", pinned=bool(proxy_url))
        return await robust_retry(
            _attempt,
            policy=policy,
            on_cooldown=self._cool_down_proxy,
            exhausted_result=lambda last: (
                last.result if last and last.result is not None
                else {"success": False, "tweets": [],
                      "error": "No proxies available."}
            ),
        )
