"""
api/routes/comments.py — FastAPI router for comment scraping.
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
router = APIRouter(prefix="/post", tags=["Reddit"])

import re
from urllib.parse import urlparse

def extract_post_id(url_or_id: str) -> str:
    """
    Extract the base-36 Reddit post ID from a full URL or direct ID.
    Handles:
      - https://www.reddit.com/r/SaaS/comments/1tnnyd4/my_post/
      - https://reddit.com/comments/1tnnyd4/
      - https://redd.it/1tnnyd4
      - t3_1tnnyd4
      - 1tnnyd4
    """
    url_or_id = url_or_id.strip()
    
    # 1. Standard comments URL format: .../comments/{post_id}/...
    match = re.search(r"comments/([a-z0-9]+)", url_or_id, re.IGNORECASE)
    if match:
        return match.group(1)
        
    # 2. Short URL or path containing only the ID, e.g. redd.it/1tnnyd4 or reddit.com/1tnnyd4
    if "://" in url_or_id or url_or_id.startswith("www.") or "." in url_or_id.split("/")[0]:
        parse_target = url_or_id if "://" in url_or_id else "https://" + url_or_id
        try:
            parsed = urlparse(parse_target)
            path = parsed.path.strip("/")
            if path and "/" not in path and re.match(r"^[a-z0-9]+$", path, re.IGNORECASE):
                if path.startswith("t3_"):
                    return path[3:]
                return path
        except Exception:
            pass

    # 3. Direct ID or fullname fallback
    if url_or_id.startswith("t3_"):
        return url_or_id[3:]
    return url_or_id

@router.get(
    "/comments",
    summary="Get Reddit comments",
    description="Fetch comments, timestamps, scores, usernames, and URLs for a specific post using its URL or ID."
)
async def get_post_comments(
    url: str = Query(..., description="The full Reddit post URL or direct post ID"),
    sort: Literal["confidence", "top", "new", "controversial", "old", "random", "qa"] = Query("confidence", description="Comment sorting criteria"),
    depth: Optional[int] = Query(None, ge=1, le=10, description="Max depth of comment replies tree to fetch"),
    limit: int = Query(100, ge=1, le=500, description="Max number of comments to return"),
    account_id: Optional[RedditAccount] = Query(None, description="Specific account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    format: Literal["json", "csv"] = Query("json", description="Output format"),
    scraper: RedditScraperService = Depends(get_reddit_scraper_service)
):
    post_id = extract_post_id(url)
    try:
        results = await scraper.scrape_post(post_id, sort=sort, depth=depth, limit=limit, account_id=account_id)
        return csv_response(results, "reddit_post_comments") if format == "csv" else results
    except Exception as e:
        logger.error("get_post_comments_failed", url=url, post_id=post_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch post comments: {e}"
        )
