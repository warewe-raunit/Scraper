#!/usr/bin/env python3
"""
run_unauth_x_scraper.py — CLI runner for the unauthenticated X/Twitter scraper.

Allows scraping public profile timelines and search queries without an account.
"""

import argparse
import asyncio
import json
import os
import sys
from dotenv import load_dotenv

import structlog

# Set up current directory to PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools.unauth_x_scraper import scrape_profile, scrape_search

def _configure_cli_logging() -> None:
    """Configure standalone structlog rendering for CLI use ONLY.

    Not called at import time so that importing this module never clobbers the
    central Loguru logging pipeline (see tools/logging_config.py).
    """
    log_format = os.getenv("LOG_FORMAT", "console").lower()
    processors = [
        structlog.processors.TimeStamper(fmt="iso" if log_format == "json" else "%Y-%m-%d %H:%M:%S"),
    ]
    if log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    structlog.configure(processors=processors)


logger = structlog.get_logger("run_unauth_x_scraper")


async def main():
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(
        description="Scrape Twitter/X content without authentication using public Nitter proxies + Playwright stealth."
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True, help="Command to execute")
    
    # Profile command parser
    profile_parser = subparsers.add_parser("profile", help="Scrape a user profile timeline")
    profile_parser.add_argument("username", help="X username (e.g. jack)")
    profile_parser.add_argument("--limit", type=int, default=20, help="Max tweets to scrape (default: 20)")
    profile_parser.add_argument("--output", help="Optional output JSON filepath")
    profile_parser.add_argument(
        "--headless",
        type=str,
        default="true",
        choices=["true", "false"],
        help="Run browser headlessly (default: true)"
    )
    profile_parser.add_argument("--proxy", help="Optional custom proxy URL")

    # Search command parser
    search_parser = subparsers.add_parser("search", help="Scrape tweets matching a search query")
    search_parser.add_argument("query", help="Search query string")
    search_parser.add_argument("--limit", type=int, default=20, help="Max tweets to scrape (default: 20)")
    search_parser.add_argument("--output", help="Optional output JSON filepath")
    search_parser.add_argument(
        "--headless",
        type=str,
        default="true",
        choices=["true", "false"],
        help="Run browser headlessly (default: true)"
    )
    search_parser.add_argument("--proxy", help="Optional custom proxy URL")

    args = parser.parse_args()

    # Parse parameters
    headless_bool = args.headless.lower() == "true"
    proxy_url = args.proxy or os.getenv("PROXY_URL")
    
    print("\n=== UNAUTHENTICATED X SCRAPER ===")
    
    if args.command == "profile":
        print(f"Target profile: @{args.username}")
        print(f"Limit:          {args.limit} tweets")
        print(f"Headless:       {headless_bool}")
        print(f"Proxy:          {proxy_url or 'None'}")
        print("Starting scrape...\n")
        
        result = await scrape_profile(
            username=args.username,
            limit=args.limit,
            proxy_url=proxy_url,
            headless=headless_bool
        )
        
    elif args.command == "search":
        print(f"Search query:   '{args.query}'")
        print(f"Limit:          {args.limit} tweets")
        print(f"Headless:       {headless_bool}")
        print(f"Proxy:          {proxy_url or 'None'}")
        print("Starting scrape...\n")
        
        result = await scrape_search(
            query=args.query,
            limit=args.limit,
            proxy_url=proxy_url,
            headless=headless_bool
        )

    # Output result
    if result.get("success"):
        print("\nScrape Completed Successfully!")
        print(f"Used proxy instance: {result.get('source_instance')}")
        
        # Display summary stats
        if args.command == "profile":
            prof = result.get("profile") or {}
            print(f"\nUser: {prof.get('fullname')} ({prof.get('username')})")
            print(f"Bio:  {prof.get('bio')}")
            stats = prof.get("stats") or {}
            print(f"Stats: {stats.get('tweets', '0')} tweets | {stats.get('followers', '0')} followers")
            
        tweets = result.get("tweets") or []
        print(f"Tweets collected:  {len(tweets)}")
        
        # Save output
        output_file = args.output
        if not output_file:
            # Generate a default file name
            safe_name = "".join([c if c.isalnum() else "_" for c in (args.username if args.command == "profile" else args.query)])
            output_file = f"x_{args.command}_{safe_name}.json"
            
        try:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            print(f"\nSaved raw JSON data to: {os.path.abspath(output_file)}")
        except Exception as e:
            print(f"\nError saving to file: {e}")
    else:
        print("\nScrape Failed!")
        print(f"Reason: {result.get('error')}")


if __name__ == "__main__":
    _configure_cli_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(0)
