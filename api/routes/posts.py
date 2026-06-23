"""
api/routes/posts.py — FastAPI router for post scraping, searching, and URL scraping.
"""

from __future__ import annotations

from typing import Literal, Optional
from fastapi import APIRouter, Query, HTTPException, status, Depends
import structlog
from api.account_choices import RedditAccount
from api.dependencies import get_reddit_scraper_service
from api.routes import csv_response
from api.services.reddit import RedditScraperService

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/posts", tags=["Reddit"])

@router.get(
    "/search",
    summary="Search posts by keyword",
    description="Search for Reddit posts globally or restrict search within a specific subreddit."
)
async def search_posts(
    q: str = Query(..., description="The search keyword or query string"),
    subreddit: Optional[str] = Query(None, description="Optional subreddit to restrict the search within"),
    sort: Literal["relevance", "hot", "top", "new", "comments"] = Query("relevance", description="Sort criteria for search results"),
    time: Literal["hour", "day", "week", "month", "year", "all"] = Query("all", description="Timeframe filter for top/relevance results"),
    limit: int = Query(25, ge=1, le=100, description="Max number of search results to return"),
    after: Optional[str] = Query(None, description="Pagination token (after) for the next page"),
    account_id: Optional[RedditAccount] = Query(None, description="Specific account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    format: Literal["json", "csv"] = Query("json", description="Output format"),
    scraper: RedditScraperService = Depends(get_reddit_scraper_service)
):
    try:
        results = await scraper.search_posts(q, subreddit, sort, time, limit, after, account_id=account_id)
        return csv_response(results, "reddit_post_search") if format == "csv" else results
    except HTTPException:
        raise  # graceful 503 (pool exhausted) — don't mask as 500
    except Exception as e:
        logger.error("search_posts_failed", q=q, subreddit=subreddit, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to execute search: {e}"
        )

@router.get(
    "/by-url",
    summary="Scrape data using a specific URL",
    description="Auto-detects URL type (subreddit, post comments, or user profile) and extracts structured data."
)
async def scrape_by_url(
    url: str = Query(..., description="The full Reddit URL to scrape (e.g. https://www.reddit.com/r/SaaS/comments/1tnnyd4/my_post/)"),
    account_id: Optional[RedditAccount] = Query(None, description="Specific account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    format: Literal["json", "csv"] = Query("json", description="Output format"),
    scraper: RedditScraperService = Depends(get_reddit_scraper_service)
):
    try:
        results = await scraper.scrape_by_url(url, account_id=account_id)
        return csv_response(results, "reddit_by_url") if format == "csv" else results
    except HTTPException:
        raise  # graceful 503 (pool exhausted) — don't mask as 500
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error("scrape_by_url_failed", url=url, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to scrape URL: {e}"
        )

@router.get(
    "/{post_id}",
    summary="Scrape specific post details",
    description="Fetch full details for a single post by its base-36 ID (e.g. 1tnnyd4 or t3_1tnnyd4)."
)
async def get_post_details(
    post_id: str,
    account_id: Optional[RedditAccount] = Query(None, description="Specific account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    format: Literal["json", "csv"] = Query("json", description="Output format"),
    scraper: RedditScraperService = Depends(get_reddit_scraper_service)
):
    try:
        res = await scraper.scrape_post(post_id, limit=0, account_id=account_id)
        return csv_response(res["post"], "reddit_post_details") if format == "csv" else res["post"]
    except HTTPException:
        raise  # graceful 503 (pool exhausted) — don't mask as 500
    except Exception as e:
        logger.error("get_post_details_failed", post_id=post_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch post details: {e}"
        )
