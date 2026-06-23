"""
api/routes/subreddits.py — FastAPI router for subreddit scraping endpoints.
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
router = APIRouter(prefix="/subreddit", tags=["Reddit"])

@router.get(
    "/{subreddit}/posts",
    summary="Scrape subreddit posts",
    description="Retrieve a listing of posts from a specific subreddit community, with support for sorting, limits, and pagination."
)
@router.get(
    "/r/{subreddit}/posts",
    include_in_schema=False
)
async def get_subreddit_posts(
    subreddit: str,
    sort: Literal["hot", "new", "top", "rising"] = Query("hot", description="Sort criteria for listings"),
    time: Literal["hour", "day", "week", "month", "year", "all"] = Query("all", description="Timeframe filter (only active when sort='top')"),
    limit: int = Query(25, ge=1, le=100, description="Max number of posts to return"),
    after: Optional[str] = Query(None, description="Pagination token (after) for the next page"),
    account_id: Optional[RedditAccount] = Query(None, description="Specific account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    format: Literal["json", "csv"] = Query("json", description="Output format"),
    scraper: RedditScraperService = Depends(get_reddit_scraper_service)
):
    subreddit = subreddit.strip("/")
    if subreddit.lower().startswith("r/"):
        subreddit = subreddit[2:]
    try:
        results = await scraper.scrape_subreddit(subreddit, sort, time, limit, after, account_id=account_id)
        return csv_response(results, "reddit_subreddit_posts") if format == "csv" else results
    except HTTPException:
        raise  # graceful 503 (pool exhausted) — don't mask as 500
    except Exception as e:
        logger.error("subreddit_posts_scrape_failed", subreddit=subreddit, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to scrape subreddit: {e}"
        )
