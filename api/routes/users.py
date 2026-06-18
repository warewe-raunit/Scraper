"""
api/routes/users.py — FastAPI router for user details, posts, and comments scraping.
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
router = APIRouter(prefix="/user", tags=["Reddit"])

@router.get(
    "/{username}/about",
    summary="Scrape user profile details",
    description="Retrieve user profile details including karma scores, creation timestamps, and profile descriptions."
)
async def get_user_about(
    username: str,
    account_id: Optional[RedditAccount] = Query(None, description="Specific account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    format: Literal["json", "csv"] = Query("json", description="Output format"),
    scraper: RedditScraperService = Depends(get_reddit_scraper_service)
):
    try:
        results = await scraper.scrape_user_about(username, account_id=account_id)
        return csv_response(results, "reddit_user_about") if format == "csv" else results
    except Exception as e:
        logger.error("get_user_about_failed", username=username, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user profile: {e}"
        )

@router.get(
    "/{username}/posts",
    summary="Scrape user submitted posts",
    description="Retrieve a listing of posts submitted by a specific Reddit user."
)
async def get_user_posts(
    username: str,
    sort: Literal["new", "hot", "top"] = Query("new", description="Sort criteria for submitted posts"),
    time: Literal["hour", "day", "week", "month", "year", "all"] = Query("all", description="Timeframe filter (only active when sort='top')"),
    limit: int = Query(25, ge=1, le=100, description="Max number of posts to return"),
    after: Optional[str] = Query(None, description="Pagination token (after) for the next page"),
    account_id: Optional[RedditAccount] = Query(None, description="Specific account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    format: Literal["json", "csv"] = Query("json", description="Output format"),
    scraper: RedditScraperService = Depends(get_reddit_scraper_service)
):
    try:
        results = await scraper.scrape_user_posts(username, sort, time, limit, after, account_id=account_id)
        return csv_response(results, "reddit_user_posts") if format == "csv" else results
    except Exception as e:
        logger.error("get_user_posts_failed", username=username, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user posts: {e}"
        )

@router.get(
    "/{username}/comments",
    summary="Scrape user submitted comments",
    description="Retrieve a listing of comments submitted by a specific Reddit user."
)
async def get_user_comments(
    username: str,
    sort: Literal["new", "hot", "top"] = Query("new", description="Sort criteria for comments"),
    time: Literal["hour", "day", "week", "month", "year", "all"] = Query("all", description="Timeframe filter (only active when sort='top')"),
    limit: int = Query(25, ge=1, le=100, description="Max number of comments to return"),
    after: Optional[str] = Query(None, description="Pagination token (after) for the next page"),
    account_id: Optional[RedditAccount] = Query(None, description="Specific account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    format: Literal["json", "csv"] = Query("json", description="Output format"),
    scraper: RedditScraperService = Depends(get_reddit_scraper_service)
):
    try:
        results = await scraper.scrape_user_comments(username, sort, time, limit, after, account_id=account_id)
        return csv_response(results, "reddit_user_comments") if format == "csv" else results
    except Exception as e:
        logger.error("get_user_comments_failed", username=username, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user comments: {e}"
        )
