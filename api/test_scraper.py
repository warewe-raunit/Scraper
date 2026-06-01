"""
api/test_scraper.py — Verification script to test failover and re-login logic.
"""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path

# Ensure root directory is in python path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import structlog
from api.dependencies import get_registry
from api.services.scraper import RedditScraperService

logger = structlog.get_logger(__name__)

async def test_reddit_scraping():
    print("=== Testing Reddit Stealth API Scraper with Failover ===")
    
    # 1. Initialize Registry and Service
    try:
        print("\nInitializing AccountRegistry and Scraper Service...")
        registry = get_registry()
        scraper = RedditScraperService(registry)
        print("Account Registry successfully initialized.")
        print(f"Registered accounts: {list(registry.states.keys())}")
    except Exception as e:
        print(f"Failed to initialize: {e}")
        return False

    # 2. Test Subreddit Fetching (Should choose first healthy account: acc_01)
    subreddit = "SaaS"
    try:
        print(f"\nFetching top posts from r/{subreddit}...")
        results = await scraper.scrape_subreddit(subreddit, sort="hot", limit=2)
        print(f"Successfully fetched {results['results_count']} posts!")
        for idx, post in enumerate(results["posts"]):
            print(f"  [{idx + 1}] Title: {post['title']} (by {post['username']})")
    except Exception as e:
        print(f"Failed to fetch subreddit posts: {e}")
        return False

    # 3. Simulate Token Expiration on acc_01
    print("\nSimulating token expiry on acc_01...")
    state = registry.get_account_state("acc_01")
    if state:
        # Flag as needing re-login
        registry.flag_relogin_needed("acc_01")
        print("acc_01 status set to: needs_relogin")
        
        # Fetching posts again should trigger a proactive re-login using Playwright!
        try:
            print("\nRequesting again (should trigger automated proactive re-login)...")
            results = await scraper.scrape_subreddit(subreddit, sort="new", limit=2, account_id="acc_01")
            print("Successfully recovered post-relogin!")
            print(f"Successfully fetched {results['results_count']} posts after auto-login!")
        except Exception as e:
            print(f"Failed during auto-relogin test: {e}")
            return False
    else:
        print("acc_01 state not found. Skipping auto-relogin test.")

    print("\n=== All Tests Completed Successfully ===")
    return True

if __name__ == "__main__":
    asyncio.run(test_reddit_scraping())
