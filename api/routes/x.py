"""
api/routes/x.py — FastAPI router for unauthenticated X (Twitter) scraping.
"""

from __future__ import annotations

from typing import Optional, Literal
from fastapi import APIRouter, Query, HTTPException, status, Depends
import structlog

from api.dependencies import get_x_scraper_service
from api.search_locations import SearchLocation
from api.services.x import XScraperService
from api.routes import csv_response

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/x", tags=["X (Twitter)"])

@router.get(
    "/profile/{username}",
    summary="Scrape X user profile",
    description="Retrieve public profile information and recent tweets for a specific user username without authentication."
)
async def get_x_profile(
    username: str,
    limit: int = Query(20, ge=1, le=100, description="Max number of tweets to return"),
    format: Literal["json", "csv"] = Query("json", description="Output format for results"),
    proxy: Optional[str] = Query(None, description="Optional custom proxy URL to use for this request"),
    headless: bool = Query(True, description="Run browser context in headless mode"),
    scraper: XScraperService = Depends(get_x_scraper_service)
):
    try:
        result = await scraper.get_profile(
            username=username,
            limit=limit,
            proxy_url=proxy,
            headless=headless
        )
        if not result.get("success"):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=result.get("error") or "Failed to scrape profile timeline from public instances."
            )
        
        if format == "csv":
            return csv_response(result, f"x_profile_{username}")
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("x_profile_scrape_failed", username=username, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to scrape user profile: {e}"
        )

@router.get(
    "/thread",
    summary="Scrape an X tweet thread (replies)",
    description="Given a tweet/post URL, return the focused tweet plus its reply thread. Set 'limit' to cap the number of replies returned."
)
async def get_x_thread(
    url: str = Query(..., description="Full X/Twitter status URL (e.g. https://x.com/jack/status/20)"),
    limit: int = Query(20, ge=1, le=200, description="Max number of replies to return"),
    location: Optional[SearchLocation] = Query(None, description="Country for proxy exit selection (renders as a dropdown of full country names). Only selects the proxy's exit country — it does not change which replies are returned."),
    format: Literal["json", "csv"] = Query("json", description="Output format for results"),
    proxy: Optional[str] = Query(None, description="Optional custom proxy URL to use for this request"),
    headless: bool = Query(True, description="Run browser context in headless mode"),
    scraper: XScraperService = Depends(get_x_scraper_service)
):
    try:
        result = await scraper.get_thread(
            status_url=url,
            limit=limit,
            proxy_url=proxy,
            headless=headless,
            location=location.code if location else None
        )
        if not result.get("success"):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=result.get("error") or "Failed to scrape thread from public instances."
            )

        if format == "csv":
            return csv_response(result, "x_thread")
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("x_thread_scrape_failed", url=url, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to scrape X thread: {e}"
        )

@router.get(
    "/search",
    summary="Search X tweets",
    description="Scrape tweets matching a search query keyword with advanced filters without authentication."
)
async def search_x_tweets(
    q: str = Query(..., description="The search query keyword or hashtag"),
    limit: int = Query(20, ge=1, le=100, description="Max number of tweets to return"),
    location: Optional[SearchLocation] = Query(None, description="Country for proxy exit selection (renders as a dropdown of full country names). Note: only selects the proxy's exit country — it does NOT localize which tweets are returned (X keyword search is global)."),
    format: Literal["json", "csv"] = Query("json", description="Output format for results"),
    since: Optional[str] = Query(None, description="Filter tweets since date (YYYY-MM-DD)"),
    until: Optional[str] = Query(None, description="Filter tweets until date (YYYY-MM-DD)"),
    from_user: Optional[str] = Query(None, description="Filter tweets from a specific user handle"),
    to_user: Optional[str] = Query(None, description="Filter tweets replying to a specific user handle"),
    min_likes: Optional[int] = Query(None, ge=0, description="Minimum favorites/likes count threshold"),
    min_retweets: Optional[int] = Query(None, ge=0, description="Minimum retweets count threshold"),
    min_replies: Optional[int] = Query(None, ge=0, description="Minimum replies count threshold"),
    filter_links: Optional[bool] = Query(None, description="If true, filter tweets containing links"),
    exclude_replies: Optional[bool] = Query(None, description="If true, exclude replies"),
    exclude_retweets: Optional[bool] = Query(None, description="If true, exclude native retweets"),
    latest: bool = Query(False, description="If true, searches for latest tweets (default sorting is chronological)"),
    popular: bool = Query(False, description="If true, filters for popular tweets (mimics Top/Popular tab using min_faves:100)"),
    trending: bool = Query(False, description="If true, filters for trending/viral tweets (using min_faves:500)"),
    proxy: Optional[str] = Query(None, description="Optional custom proxy URL to use for this request"),
    headless: bool = Query(True, description="Run browser context in headless mode"),
    scraper: XScraperService = Depends(get_x_scraper_service)
):
    # Build advanced query string matching Twitter advanced operators
    parts = [q.strip()]
    if since:
        parts.append(f"since:{since.strip()}")
    if until:
        parts.append(f"until:{until.strip()}")
    if from_user:
        parts.append(f"from:{from_user.strip()}")
    if to_user:
        parts.append(f"to:{to_user.strip()}")
    
    # Resolve engagement filters (explicit params override presets)
    likes_threshold = min_likes
    if likes_threshold is None:
        if trending:
            likes_threshold = 500
        elif popular:
            likes_threshold = 100

    if likes_threshold is not None and likes_threshold >= 0:
        parts.append(f"min_faves:{likes_threshold}")
        
    if min_retweets is not None and min_retweets >= 0:
        parts.append(f"min_retweets:{min_retweets}")
    if min_replies is not None and min_replies >= 0:
        parts.append(f"min_replies:{min_replies}")
    if filter_links is True:
        parts.append("filter:links")
    if exclude_replies is True:
        parts.append("-filter:replies")
    if exclude_retweets is True:
        parts.append("-filter:nativeretweets")

    compiled_query = " ".join(parts)
    
    try:
        result = await scraper.search(
            query=compiled_query,
            limit=limit,
            proxy_url=proxy,
            headless=headless,
            location=location.code if location else None
        )
        if not result.get("success"):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=result.get("error") or "Failed to scrape search results from public instances."
            )
        
        if format == "csv":
            # Sanitize filename
            safe_q = "".join(c for c in q if c.isalnum() or c in (" ", "_", "-")).strip()
            return csv_response(result, f"x_search_{safe_q}")
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("x_search_scrape_failed", query=compiled_query, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search X tweets: {e}"
        )
