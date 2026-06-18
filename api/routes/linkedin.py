"""
api/routes/linkedin.py — FastAPI router for LinkedIn Voyager API scraping.
"""

from __future__ import annotations

import csv
import io
from enum import Enum
from typing import Optional, Any, List
from fastapi import APIRouter, Query, HTTPException, status, Depends, Response
import structlog

from api.account_choices import LinkedInAccount
from api.dependencies import get_linkedin_scraper_service
from api.services.linkedin import LinkedInScraperService

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/linkedin", tags=["LinkedIn"])

class ResponseFormat(str, Enum):
    JSON = "json"
    CSV = "csv"

class WorkplaceType(str, Enum):
    ON_SITE = "on_site"
    REMOTE = "remote"
    HYBRID = "hybrid"

class JobType(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    INTERNSHIP = "internship"
    VOLUNTEER = "volunteer"

class ExperienceLevel(str, Enum):
    INTERNSHIP = "internship"
    ENTRY_LEVEL = "entry_level"
    ASSOCIATE = "associate"
    MID_SENIOR = "mid_senior"
    DIRECTOR = "director"
    EXECUTIVE = "executive"

class DatePostedRange(str, Enum):
    PAST_24H = "past_24h"
    PAST_WEEK = "past_week"
    PAST_MONTH = "past_month"

class SearchCategory(str, Enum):
    ALL = "all"
    PEOPLE = "people"
    COMPANIES = "companies"
    JOBS = "jobs"
    POSTS = "posts"
    GROUPS = "groups"

def convert_to_csv(endpoint_type: str, result: dict) -> str:
    """Helper to convert structured scraping output to CSV format."""
    output = io.StringIO()
    writer = csv.writer(output)
    
    scraped_at = result.get("scraped_at", "")
    data = result.get("data", {})
    
    if endpoint_type == "profile":
        headers = ["public_id", "name", "headline", "about", "experience", "education", "contact", "scraped_at"]
        writer.writerow(headers)
        if data:
            experience = " | ".join(data.get("experience") or []) if isinstance(data.get("experience"), list) else str(data.get("experience") or "")
            education = " | ".join(data.get("education") or []) if isinstance(data.get("education"), list) else str(data.get("education") or "")
            contact = " | ".join(data.get("contact") or []) if isinstance(data.get("contact"), list) else str(data.get("contact") or "")
            
            row = [
                result.get("public_id", ""),
                data.get("name", ""),
                data.get("headline", ""),
                data.get("about", ""),
                experience,
                education,
                contact,
                scraped_at
            ]
            writer.writerow(row)
            
    elif endpoint_type == "company":
        headers = ["company_name", "name", "tagline", "details", "about", "scraped_at"]
        writer.writerow(headers)
        if data:
            row = [
                result.get("company_name", ""),
                data.get("name", ""),
                data.get("tagline", ""),
                data.get("details", ""),
                data.get("about", ""),
                scraped_at
            ]
            writer.writerow(row)
            
    elif endpoint_type == "jobs":
        headers = ["query", "location", "title", "url", "company", "job_location", "details", "scraped_at"]
        writer.writerow(headers)
        job_list = data if isinstance(data, list) else []
        for job in job_list:
            row = [
                result.get("query", ""),
                result.get("location", "") or "",
                job.get("title", ""),
                job.get("url", ""),
                job.get("company", "") or "",
                job.get("location", "") or "",
                job.get("details", "") or "",
                scraped_at
            ]
            writer.writerow(row)
            
    elif endpoint_type == "comments":
        headers = ["post_urn", "author", "text", "created_at", "likes", "comment_urn", "scraped_at"]
        writer.writerow(headers)
        comment_list = data if isinstance(data, list) else []
        for c in comment_list:
            row = [
                result.get("post_urn", ""),
                c.get("author", ""),
                c.get("text", ""),
                c.get("created_at", "") or "",
                c.get("likes", 0),
                c.get("comment_urn", "") or "",
                scraped_at
            ]
            writer.writerow(row)

    elif endpoint_type == "search":
        headers = ["query", "category", "title", "url", "snippet", "scraped_at"]
        writer.writerow(headers)
        if isinstance(data, dict):
            for category, items in data.items():
                if isinstance(items, list):
                    for item in items:
                        row = [
                            result.get("query", ""),
                            category,
                            item.get("title", ""),
                            item.get("url", ""),
                            item.get("snippet", "") or "",
                            scraped_at
                        ]
                        writer.writerow(row)
                        
    return output.getvalue()

def format_response(endpoint_type: str, name_or_query: str, result: dict, response_format: ResponseFormat) -> Any:
    """Format and return response in JSON or CSV format."""
    if response_format == ResponseFormat.CSV:
        csv_content = convert_to_csv(endpoint_type, result)
        filename = f"linkedin_{endpoint_type}_{name_or_query}.csv".replace(" ", "_")
        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            }
        )
    return result

@router.get(
    "/profile/{profile_id}",
    summary="Scrape LinkedIn profile details",
    description="Retrieve deep profile details (experience, education, skills, about). Accepts raw profile ID or a full LinkedIn Profile URL (via query parameter or URL-encoded in path)."
)
async def get_linkedin_profile(
    profile_id: str,
    profile_url: Optional[str] = Query(None, description="Optional full LinkedIn Profile URL (if provided, overrides profile_id)"),
    format: ResponseFormat = Query(ResponseFormat.JSON, description="Output format"),
    account_id: Optional[LinkedInAccount] = Query(None, description="Specific LinkedIn account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    scraper: LinkedInScraperService = Depends(get_linkedin_scraper_service)
):
    target = profile_url if profile_url else profile_id
    try:
        result = await scraper.scrape_profile(public_id=target, account_id=account_id)
        scraped_id = result.get("public_id", target)
        return format_response("profile", scraped_id, result, format)
    except Exception as e:
        logger.error("linkedin_profile_scrape_failed", public_id=target, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to scrape LinkedIn profile: {e}"
        )

@router.get(
    "/company/{company_name}",
    summary="Scrape LinkedIn company details",
    description="Retrieve organization/company page information. Accepts raw company name or a full LinkedIn Company URL (via query parameter or URL-encoded in path)."
)
async def get_linkedin_company(
    company_name: str,
    company_url: Optional[str] = Query(None, description="Optional full LinkedIn Company URL (if provided, overrides company_name)"),
    format: ResponseFormat = Query(ResponseFormat.JSON, description="Output format"),
    account_id: Optional[LinkedInAccount] = Query(None, description="Specific LinkedIn account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    scraper: LinkedInScraperService = Depends(get_linkedin_scraper_service)
):
    target = company_url if company_url else company_name
    try:
        result = await scraper.scrape_company(company_name=target, account_id=account_id)
        scraped_name = result.get("company_name", target)
        return format_response("company", scraped_name, result, format)
    except Exception as e:
        logger.error("linkedin_company_scrape_failed", company_name=target, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to scrape LinkedIn company: {e}"
        )

@router.get(
    "/jobs",
    summary="Search LinkedIn job postings",
    description="Query job postings matching keywords, location, and key filters (workplace type, job type, experience, time posted)."
)
async def search_linkedin_jobs(
    keywords: str = Query(..., description="Job search keywords (e.g. 'Software Engineer')"),
    location: Optional[str] = Query(None, description="Job search location (e.g. 'New York')"),
    workplace_type: Optional[List[WorkplaceType]] = Query(None, description="Filter by workplace types (on-site, remote, hybrid)"),
    job_type: Optional[List[JobType]] = Query(None, description="Filter by job types (full-time, contract, internship, etc.)"),
    experience_level: Optional[List[ExperienceLevel]] = Query(None, description="Filter by experience levels"),
    date_posted: Optional[DatePostedRange] = Query(None, description="Filter by when the job was posted"),
    limit: int = Query(25, ge=1, le=1000, description="Max job postings to retrieve (paginated)"),
    format: ResponseFormat = Query(ResponseFormat.JSON, description="Output format"),
    account_id: Optional[LinkedInAccount] = Query(None, description="Specific LinkedIn account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    scraper: LinkedInScraperService = Depends(get_linkedin_scraper_service)
):
    try:
        result = await scraper.search_jobs(
            keywords=keywords,
            location=location,
            workplace_types=workplace_type,
            job_types=job_type,
            experience_levels=experience_level,
            date_posted=date_posted,
            limit=limit,
            account_id=account_id
        )
        name_or_query = f"{keywords}_{location or 'any'}"
        return format_response("jobs", name_or_query, result, format)
    except Exception as e:
        logger.error("linkedin_jobs_search_failed", keywords=keywords, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to search LinkedIn jobs: {e}"
        )

class CommentSortOrder(str, Enum):
    RELEVANT = "relevant"
    RECENT = "recent"

@router.get(
    "/post/comments",
    summary="Scrape comments on a LinkedIn post",
    description="Given a LinkedIn post URL (or activity URN), return the comment thread. Use 'limit' to cap the number of comments returned."
)
async def get_linkedin_post_comments(
    url: str = Query(..., description="Full LinkedIn post URL or activity URN (e.g. https://www.linkedin.com/posts/..-activity-7298..-AbCd or urn:li:activity:7298..)"),
    sort: CommentSortOrder = Query(CommentSortOrder.RELEVANT, description="Comment sort order (relevant = top, recent = chronological)"),
    limit: int = Query(25, ge=1, le=1000, description="Max number of comments to retrieve (paginated)"),
    format: ResponseFormat = Query(ResponseFormat.JSON, description="Output format"),
    account_id: Optional[LinkedInAccount] = Query(None, description="Specific LinkedIn account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    scraper: LinkedInScraperService = Depends(get_linkedin_scraper_service)
):
    try:
        result = await scraper.scrape_post_comments(
            post_url=url,
            limit=limit,
            sort=sort.value,
            account_id=account_id,
        )
        return format_response("comments", result.get("post_urn", "post"), result, format)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error("linkedin_post_comments_failed", url=url, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to scrape LinkedIn post comments: {e}"
        )

@router.get(
    "/accounts/status",
    summary="LinkedIn account pool health",
    description="Per-account state + pool counters. Useful for ops dashboards.",
)
async def get_account_pool_status():
    from api.services.linkedin_account_pool import LinkedInAccountPool
    pool = await LinkedInAccountPool.instance()
    return pool.snapshot()


@router.post(
    "/accounts/{account_id}/relogin",
    summary="Force a background relogin for one account",
)
async def force_relogin(account_id: LinkedInAccount):
    from api.services.linkedin_account_pool import LinkedInAccountPool
    pool = await LinkedInAccountPool.instance()
    await pool.force_relogin(str(account_id))
    return {"queued": True, "account_id": str(account_id)}


@router.get(
    "/search",
    summary="Universal blended search",
    description="Perform a blended query search matching keywords, with optional target category filters."
)
async def search_linkedin_blended(
    q: str = Query(..., description="Search query string"),
    category: SearchCategory = Query(SearchCategory.ALL, description="Search category filter (people, jobs, companies, posts, groups)"),
    limit: int = Query(25, ge=1, le=1000, description="Max results to retrieve (paginated; not used for category=all)"),
    format: ResponseFormat = Query(ResponseFormat.JSON, description="Output format"),
    account_id: Optional[LinkedInAccount] = Query(None, description="Specific LinkedIn account to use (dropdown of configured accounts). If omitted, rotates available sessions."),
    scraper: LinkedInScraperService = Depends(get_linkedin_scraper_service)
):
    try:
        result = await scraper.search_blended(query=q, category=category, limit=limit, account_id=account_id)
        return format_response("search", q, result, format)
    except Exception as e:
        logger.error("linkedin_blended_search_failed", q=q, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to execute blended search: {e}"
        )
