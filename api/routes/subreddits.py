"""
api/routes/subreddits.py — FastAPI router for subreddit scraping endpoints.
"""

from __future__ import annotations

from typing import Literal, Optional
from fastapi import APIRouter, Query, HTTPException, status, Depends
import structlog
from api.dependencies import get_reddit_scraper_service
from api.routes import csv_response
from api.services.reddit import RedditScraperService

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/subreddit", tags=["Subreddits"])

@router.get(
    "/{subreddit}/posts",
    summary="Scrape subreddit posts",
    description="Retrieve a listing of posts from a specific subreddit community, with support for sorting, limits, and pagination."
)
async def get_subreddit_posts(
    subreddit: str,
    sort: str = Query("hot", regex="^(hot|new|top|rising)$", description="Sort criteria for listings"),
    time: str = Query("all", regex="^(hour|day|week|month|year|all)$", description="Timeframe filter (only active when sort='top')"),
    limit: int = Query(25, ge=1, le=100, description="Max number of posts to return"),
    after: Optional[str] = Query(None, description="Pagination token (after) for the next page"),
    account_id: Optional[str] = Query(None, description="Specify a specific account ID to use. If omitted, rotates available sessions."),
    format: Literal["json", "csv"] = Query("json", description="Output format"),
    scraper: RedditScraperService = Depends(get_reddit_scraper_service)
):
    try:
        results = await scraper.scrape_subreddit(subreddit, sort, time, limit, after, account_id=account_id)
        return csv_response(results, "reddit_subreddit_posts") if format == "csv" else results
    except Exception as e:
        logger.error("subreddit_posts_scrape_failed", subreddit=subreddit, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to scrape subreddit: {e}"
        )
