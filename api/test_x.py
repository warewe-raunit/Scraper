"""
api/test_x.py — Verification script to test XScraperService.
"""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path

# Ensure root directory is in python path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.x import XScraperService

async def test_x_service():
    print("=== Testing X Stealth API Scraper ===")
    
    # 1. Initialize Service
    try:
        print("\nInitializing XScraperService...")
        scraper = XScraperService()
        print("XScraperService successfully initialized.")
    except Exception as e:
        print(f"Failed to initialize: {e}")
        return False

    # 2. Test profile fetching (using a quick limit of 2)
    username = "jack"
    try:
        print(f"\nFetching profile details and tweets for @{username}...")
        results = await scraper.get_profile(username, limit=2)
        if results.get("success"):
            print("Successfully fetched profile!")
            prof = results.get("profile") or {}
            print(f"  Name: {prof.get('fullname')} ({prof.get('username')})")
            print(f"  Bio:  {prof.get('bio')}")
            tweets = results.get("tweets") or []
            print(f"  Fetched {len(tweets)} tweets:")
            for idx, t in enumerate(tweets):
                print(f"    [{idx + 1}] {t.get('text')[:60]}... (likes: {t.get('stats', {}).get('likes')})")
        else:
            print(f"Failed to fetch profile: {results.get('error')}")
            return False
    except Exception as e:
        print(f"Failed during profile scrape test: {e}")
        return False

    # 3. Test search query
    query = "bitcoin"
    try:
        print(f"\nSearching tweets for '{query}'...")
        results = await scraper.search(query, limit=2)
        if results.get("success"):
            tweets = results.get("tweets") or []
            print(f"Successfully fetched {len(tweets)} tweets for query '{query}':")
            for idx, t in enumerate(tweets):
                print(f"    [{idx + 1}] By @{t.get('username')}: {t.get('text')[:60]}...")
        else:
            print(f"Failed to search query: {results.get('error')}")
            return False
    except Exception as e:
        print(f"Failed during search query test: {e}")
        return False

    print("\n=== All X Service Tests Completed Successfully ===")
    return True

if __name__ == "__main__":
    asyncio.run(test_x_service())
