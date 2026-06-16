"""
api/routes/youtube.py — FastAPI router for YouTube scraping and exporting.
"""

from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Query, HTTPException, status, Depends, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, StreamingResponse
import os
import httpx
import structlog

from api.dependencies import get_youtube_scraper_service
from api.services.youtube import YouTubeScraperService
from api.utils.exporters import export_to_csv, export_to_excel, export_to_html_dashboard, export_download_page_html

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/youtube", tags=["YouTube"])

def format_export_response(data: dict, export_type: str, requested_format: str, scraper: Optional[YouTubeScraperService] = None) -> Response:
    """Helper function to format response based on requested format (json, csv, excel, html, raw)."""
    requested_format = requested_format.lower()
    filename = f"youtube_{export_type}"
    
    if requested_format == "csv":
        return export_to_csv(data, filename)
    elif requested_format == "excel":
        return export_to_excel(data, filename)
    elif requested_format == "html":
        html_content = export_to_html_dashboard(data, "youtube")
        return HTMLResponse(content=html_content)
    elif requested_format == "raw":
        import json
        raw_payload = data.get("raw_payload", data)
        json_content = json.dumps(raw_payload, indent=2)
        video_id = data.get("video_id", "video")
        return Response(
            content=json_content,
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=youtube_payload_{video_id}.json"}
        )
    else:
        # Default: JSON
        return Response(
            content=b"",
            status_code=200
        )

@router.get(
    "/search",
    summary="Search YouTube videos",
    description="Search YouTube videos stealthily with support for sorting, timeframe filters, limits, and exporting to CSV, Excel, or HTML."
)
async def search_youtube(
    q: str = Query(..., description="The search query keyword"),
    sort: str = Query("relevance", pattern="^(relevance|date|views|rating)$", description="Criteria to sort search results"),
    time: str = Query("all", pattern="^(hour|day|week|month|year|all)$", description="Timeframe filter for search results"),
    limit: int = Query(20, ge=1, le=500, description="Max number of search results to return"),
    format: str = Query("json", pattern="^(json|csv|excel|html)$", description="Output format for search results"),
    scraper: YouTubeScraperService = Depends(get_youtube_scraper_service)
):
    try:
        results = await scraper.search(q, sort, time, limit=limit)
        if format != "json":
            return format_export_response(results, "search", format, scraper)
        return results
    except Exception as e:
        logger.error("youtube_search_failed", q=q, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search YouTube: {e}"
        )

@router.get(
    "/video",
    summary="Get video details and comments",
    description="Fetch extensive video metadata (views, likes, stream URLs, description) and top comments."
)
async def get_video_details(
    url: Optional[str] = Query(None, description="Full YouTube video URL (e.g., https://www.youtube.com/watch?v=dQw4w9WgXcQ)"),
    video_id: Optional[str] = Query(None, description="11-character YouTube video ID"),
    limit: int = Query(20, ge=0, le=500, description="Max number of comments to return"),
    include_raw: bool = Query(False, description="Whether to include raw InnerTube API payloads for debugging or offline download"),
    format: str = Query("json", pattern="^(json|csv|excel|html|raw)$", description="Output format for details"),
    scraper: YouTubeScraperService = Depends(get_youtube_scraper_service)
):
    if not url and not video_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'url' or 'video_id' parameter must be provided."
        )
        
    target = url or video_id
    try:
        fetch_raw = include_raw or (format == "raw")
        results = await scraper.get_video_details(target, comments_limit=limit, include_raw=fetch_raw) # type: ignore
        if format != "json":
            return format_export_response(results, "video_details", format, scraper)
        return results
    except Exception as e:
        logger.error("youtube_video_details_failed", target=target, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch video details: {e}"
        )

@router.get(
    "/channel/{channel_id:path}/videos",
    summary="Get channel videos or streams",
    description="Fetch list of videos or live streams uploaded by a specific YouTube channel."
)
async def get_channel_videos(
    channel_id: str,
    type: str = Query("videos", pattern="^(videos|live)$", description="Select either 'videos' or 'live' streams"),
    format: str = Query("json", pattern="^(json|csv|excel|html)$", description="Output format for video list"),
    scraper: YouTubeScraperService = Depends(get_youtube_scraper_service)
):
    try:
        results = await scraper.get_channel_videos(channel_id, type)
        if format != "json":
            return format_export_response(results, "channel", format, scraper)
        return results
    except Exception as e:
        logger.error("youtube_channel_videos_failed", channel_id=channel_id, type=type, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch channel content: {e}"
        )

@router.get(
    "/playlist/{playlist_id:path}",
    summary="Get playlist videos",
    description="Fetch list of videos inside a specific YouTube playlist."
)
async def get_playlist(
    playlist_id: str,
    format: str = Query("json", pattern="^(json|csv|excel|html)$", description="Output format for video list"),
    scraper: YouTubeScraperService = Depends(get_youtube_scraper_service)
):
    try:
        results = await scraper.get_playlist(playlist_id)
        if format != "json":
            return format_export_response(results, "playlist", format, scraper)
        return results
    except Exception as e:
        logger.error("youtube_playlist_failed", playlist_id=playlist_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch playlist content: {e}"
        )

@router.get(
    "/download",
    summary="Download YouTube video",
    response_class=StreamingResponse,
    description=(
        "Download a YouTube video from Swagger UI without saving the MP4 on the backend by leaving `format='stream'`.\n\n"
        "Use `format='json'` for fast direct-link metadata, `format='html'` for a download page, "
        "`format='redirect'` to redirect to YouTube's media URL, or `format='stream'` to proxy the "
        "remote stream without first storing it on the server. Use `format='file'` only when you "
        "explicitly want the backend to download a temporary MP4 before returning it."
    ),
    responses={
        200: {
            "description": "Video file download by default, or the selected alternate response format.",
            "content": {
                "video/mp4": {
                    "schema": {
                        "type": "string",
                        "format": "binary",
                    }
                },
                "application/json": {
                    "schema": {
                        "type": "object",
                    }
                },
                "text/html": {
                    "schema": {
                        "type": "string",
                    }
                },
            },
        }
    }
)
async def download_youtube_video(
    background_tasks: BackgroundTasks,
    url: Optional[str] = Query(None, description="Full YouTube video URL"),
    video_id: Optional[str] = Query(None, description="11-character YouTube video ID"),
    resolution: str = Query("360p", pattern="^(144p|240p|360p|480p|720p|1080p|1440p|2160p)$", description="Target video resolution"),
    stream_from_server: bool = Query(False, description="Legacy file mode. If True, the backend downloads a temporary MP4 before returning it."),
    format: str = Query("stream", pattern="^(stream|json|html|redirect|file)$", description="Response format: 'stream' (default) downloads in Swagger without saving a backend file, 'json' returns URL metadata, 'html' shows a page, 'redirect' redirects the browser, 'file' downloads a temporary backend file first."),
    scraper: YouTubeScraperService = Depends(get_youtube_scraper_service)
):
    if not url and not video_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'url' or 'video_id' parameter must be provided."
        )
        
    target = url or video_id
    clean_video_id = scraper.extract_video_id(target)
    
    try:
        if not stream_from_server and format != "file":
            # Return direct download info based on format
            direct_url_info = await scraper.get_direct_download_url(target, resolution)
            if format == "redirect":
                return RedirectResponse(url=direct_url_info["download_url"])
            elif format == "html":
                html_content = export_download_page_html(direct_url_info)
                return HTMLResponse(content=html_content)
            elif format == "stream":
                download_url = direct_url_info["download_url"]
                proxy_url = direct_url_info.get("proxy")
                request_headers = direct_url_info.get("http_headers") or {}
                title = direct_url_info.get("title", f"youtube_{clean_video_id}")
                # Sanitize filename
                safe_title = "".join([c for c in title if c.isalnum() or c in (" ", "_", "-")]).strip()
                safe_title = safe_title.replace(" ", "_") or f"youtube_{clean_video_id}"
                
                async def video_streamer():
                    timeout = httpx.Timeout(connect=15.0, read=None, write=15.0, pool=15.0)
                    async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout, follow_redirects=True) as client:
                        async with client.stream("GET", download_url, headers=request_headers) as r:
                            r.raise_for_status()
                            async for chunk in r.aiter_bytes(chunk_size=1024*64):
                                yield chunk
                
                return StreamingResponse(
                    video_streamer(),
                    media_type="video/mp4",
                    headers={
                        "Content-Disposition": f'attachment; filename="{safe_title}_{resolution}.mp4"'
                    }
                )
            else:
                return JSONResponse(content=direct_url_info)
            
        # Fallback to downloading on backend and streaming file
        file_path = await scraper.download_video(target, resolution)
        
        # Cleanup file after sending
        def remove_file(path: str):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as ex:
                    logger.error("failed_to_cleanup_downloaded_file", path=path, error=str(ex))
                    
        background_tasks.add_task(remove_file, file_path)
        
        return FileResponse(
            path=file_path,
            media_type="video/mp4",
            filename=f"youtube_{clean_video_id}_{resolution}.mp4"
        )
    except Exception as e:
        logger.error("youtube_download_failed", target=target, resolution=resolution, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process download request: {e}"
        )
