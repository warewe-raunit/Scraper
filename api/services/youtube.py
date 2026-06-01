"""
api/services/youtube.py — YouTube Stealth API scraping service.
Utilizes YouTube's internal InnerTube API and Playwright fallback.
"""

from __future__ import annotations

import os
import re
import sys
import time
import json
import csv
import io
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List, Union
from urllib.parse import urlparse, parse_qs
import structlog
from curl_cffi import requests

# Fix path to import core/tools modules correctly
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.dependencies import parse_accounts_from_env
from browser_manager import launch_browser, close_browser

logger = structlog.get_logger(__name__)

class YouTubeScraperService:
    def __init__(self):
        self._api_key: Optional[str] = None
        self._api_key_lock = asyncio.Lock()
        
        # Initialize proxy rotator
        accounts = parse_accounts_from_env()
        self.proxies = [acc["proxy_url"] for acc in accounts if acc.get("proxy_url")]
        self._proxy_index = 0
        
        logger.info("youtube_scraper_service_initialized", proxy_count=len(self.proxies))

    def _get_next_proxy(self) -> Optional[str]:
        """Rotate through the list of proxies in a round-robin fashion."""
        if not self.proxies:
            return None
        proxy = self.proxies[self._proxy_index % len(self.proxies)]
        self._proxy_index += 1
        return proxy

    async def _get_innertube_key(self, max_retries: int = 2) -> str:
        """
        Extract the INNERTUBE_API_KEY from YouTube.
        1. Check cached key.
        2. Make GET request to YouTube homepage.
        3. Use Playwright to extract key if GET is blocked.
        4. Fall back to hardcoded public key.
        """
        if self._api_key:
            return self._api_key

        async with self._api_key_lock:
            # Recheck inside lock
            if self._api_key:
                return self._api_key

            proxy = self._get_next_proxy()
            
            # Method 1: GET request extraction
            try:
                loop = asyncio.get_running_loop()
                session = requests.Session()
                if proxy:
                    session.proxies = {"http": proxy, "https": proxy}
                
                headers = {
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "accept-language": "en-US,en;q=0.9",
                }
                
                response = await loop.run_in_executor(
                    None,
                    lambda: session.get("https://www.youtube.com/", headers=headers, impersonate="chrome120", timeout=10)
                )
                
                if response.status_code == 200:
                    match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', response.text)
                    if match:
                        self._api_key = match.group(1)
                        logger.info("extracted_innertube_key_via_http", key=self._api_key[:10] + "...")
                        return self._api_key
            except Exception as e:
                logger.warn("http_innertube_key_extraction_failed", error=str(e))

            # Method 2: Playwright fallback
            try:
                logger.info("falling_back_to_playwright_for_key")
                pw, browser, context, page = await launch_browser(
                    account_id="acc_01",
                    proxy_url=proxy,
                    headless=True
                )
                try:
                    await page.goto("https://www.youtube.com/", wait_until="domcontentloaded", timeout=20000)
                    key = await page.evaluate("() => window.ytcfg ? window.ytcfg.get('INNERTUBE_API_KEY') : null")
                    if not key:
                        html = await page.content()
                        match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
                        if match:
                            key = match.group(1)
                    
                    if key:
                        self._api_key = key
                        logger.info("extracted_innertube_key_via_playwright", key=key[:10] + "...")
                        return key
                finally:
                    await context.close()
                    await browser.close()
                    await pw.stop()
            except Exception as e:
                logger.warn("playwright_innertube_key_extraction_failed", error=str(e))

            # Method 3: Hardcoded fallback
            fallback_key = "AIzaSyAO_JVG4aDXa7KM4V0F4lQcMBa6W4Wl8wg"
            logger.warn("using_hardcoded_fallback_innertube_key", key=fallback_key[:10] + "...")
            self._api_key = fallback_key
            return fallback_key

    def _get_client_context(self, client_name: str = "WEB", user_agent: Optional[str] = None) -> dict:
        """Build the client context required by InnerTube endpoints."""
        if client_name == "ANDROID":
            return {
                "client": {
                    "clientName": "ANDROID",
                    "clientVersion": "19.01.35",
                    "hl": "en",
                    "gl": "US",
                    "platform": "MOBILE",
                    "osName": "ANDROID"
                }
            }
        else:
            ua = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            return {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20240101.01.00",
                    "hl": "en",
                    "gl": "US",
                    "userAgent": ua,
                    "clientScreen": "WATCH"
                }
            }

    def find_nested_keys(self, data: Any, key_to_find: str) -> List[Any]:
        """Recursively scan nested dictionaries and lists to extract target keys."""
        results = []
        if isinstance(data, dict):
            if key_to_find in data:
                results.append(data[key_to_find])
            for val in data.values():
                results.extend(self.find_nested_keys(val, key_to_find))
        elif isinstance(data, list):
            for item in data:
                results.extend(self.find_nested_keys(item, key_to_find))
        return results

    def parse_runs(self, runs_dict: Any) -> str:
        """Parse formatted text runs arrays returned by YouTube's API."""
        if not runs_dict:
            return ""
        if isinstance(runs_dict, str):
            return runs_dict
        if isinstance(runs_dict, dict):
            if "runs" in runs_dict:
                return "".join(run.get("text", "") for run in runs_dict["runs"] if isinstance(run, dict))
            return runs_dict.get("simpleText", "")
        return str(runs_dict)

    def parse_video_renderer(self, renderer: dict) -> dict:
        """Extract metadata from a video renderer object."""
        video_id = renderer.get("videoId", "")
        title = self.parse_runs(renderer.get("title"))
        
        desc_snippet = self.parse_runs(renderer.get("descriptionSnippet"))
        if not desc_snippet:
            snippets = renderer.get("detailedMetadataSnippets", [{}])
            if snippets:
                desc_snippet = self.parse_runs(snippets[0].get("snippetText"))
                
        views_text = self.parse_runs(renderer.get("viewCountText"))
        short_views_text = self.parse_runs(renderer.get("shortViewCountText"))
        published_time = self.parse_runs(renderer.get("publishedTimeText"))
        
        length_text = self.parse_runs(renderer.get("lengthText"))
        if not length_text:
            length_text = renderer.get("lengthText", {}).get("accessibility", {}).get("accessibilityData", {}).get("label", "")
            
        channel_name = ""
        channel_id = ""
        byline = renderer.get("longBylineText") or renderer.get("shortBylineText")
        if byline and isinstance(byline, dict):
            channel_name = self.parse_runs(byline)
            runs = byline.get("runs", [])
            if runs and isinstance(runs, list):
                channel_id = runs[0].get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId", "")
                
        thumbnails = renderer.get("thumbnail", {}).get("thumbnails", [])
        
        return {
            "id": video_id,
            "video_id": video_id,
            "title": title,
            "description": desc_snippet,
            "views": views_text or short_views_text,
            "published_time": published_time,
            "duration": length_text,
            "channel_name": channel_name,
            "channel_id": channel_id,
            "channel_url": f"https://www.youtube.com/channel/{channel_id}" if channel_id else "",
            "thumbnails": thumbnails,
            "video_url": f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
        }

    def extract_videos(self, json_data: Any) -> List[dict]:
        """Extract and clean all video items in a response."""
        videos = []
        for key in ["videoRenderer", "gridVideoRenderer", "compactVideoRenderer", "playlistVideoRenderer"]:
            renderers = self.find_nested_keys(json_data, key)
            for r in renderers:
                if isinstance(r, dict):
                    parsed = self.parse_video_renderer(r)
                    if parsed.get("id") and not any(v["id"] == parsed["id"] for v in videos):
                        videos.append(parsed)
        return videos

    def parse_comment_renderer(self, renderer: dict) -> dict:
        """Extract fields from a comment renderer object."""
        comment_id = renderer.get("commentId", "")
        author = self.parse_runs(renderer.get("authorText"))
        author_thumbnail = renderer.get("authorThumbnail", {}).get("thumbnails", [{}])[0].get("url", "")
        text = self.parse_runs(renderer.get("contentText"))
        published_time = self.parse_runs(renderer.get("publishedTimeText"))
        like_count = self.parse_runs(renderer.get("likeCount")) or "0"
        
        return {
            "id": comment_id,
            "comment_id": comment_id,
            "author": author,
            "author_thumbnail": author_thumbnail,
            "text": text,
            "published_time": published_time,
            "like_count": like_count
        }

    def extract_comments(self, json_data: Any) -> List[dict]:
        """Extract and clean all comments in a response."""
        comments = []
        
        # 1. Try parsing new Entity-based Updates Model (mutations)
        mutations = self.find_nested_keys(json_data, "mutations")
        for mut_list in mutations:
            if isinstance(mut_list, list):
                for mut in mut_list:
                    if isinstance(mut, dict):
                        payload = mut.get("payload", {})
                        if "commentEntityPayload" in payload:
                            cep = payload["commentEntityPayload"]
                            props = cep.get("properties", {})
                            author = cep.get("author", {})
                            toolbar = cep.get("toolbar", {})
                            
                            comment_id = props.get("commentId", "")
                            text = props.get("content", {}).get("content", "")
                            published_time = props.get("publishedTime", "")
                            
                            author_name = author.get("displayName", "")
                            author_thumbnail = author.get("avatarThumbnailUrl", "")
                            author_id = author.get("channelId", "")
                            
                            # Clean like count
                            like_count = (toolbar.get("likeCountNotliked") or "0").strip()
                            if not like_count:
                                like_count = (toolbar.get("likeCountLiked") or "0").strip()
                            if not like_count or like_count == " ":
                                like_count = "0"
                                
                            parsed = {
                                "id": comment_id,
                                "comment_id": comment_id,
                                "author": author_name,
                                "author_thumbnail": author_thumbnail,
                                "author_id": author_id,
                                "text": text,
                                "published_time": published_time,
                                "like_count": like_count
                            }
                            if parsed.get("id") and not any(c["id"] == parsed["id"] for c in comments):
                                comments.append(parsed)
                                
        # 2. Try parsing old Renderer-based Model (Fallback)
        renderers = self.find_nested_keys(json_data, "commentRenderer")
        for r in renderers:
            if isinstance(r, dict):
                parsed = self.parse_comment_renderer(r)
                if parsed.get("id") and not any(c["id"] == parsed["id"] for c in comments):
                    comments.append(parsed)
                    
        return comments

    def extract_like_count(self, next_data: Any) -> str:
        """Parse exact or formatted like count from watch page layout."""
        # 1. Try likeCountText
        like_texts = self.find_nested_keys(next_data, "likeCountText")
        for text in like_texts:
            val = self.parse_runs(text)
            if val:
                return val
                
        # 2. Try segmentedLikeDislikeButtonViewModel (New UI format)
        view_models = self.find_nested_keys(next_data, "segmentedLikeDislikeButtonViewModel")
        for vm in view_models:
            if isinstance(vm, dict):
                buttons = self.find_nested_keys(vm, "defaultButtonViewModel")
                for btn in buttons:
                    if isinstance(btn, dict):
                        bvm = btn.get("buttonViewModel", {})
                        if isinstance(bvm, dict):
                            title = bvm.get("title")
                            if title:
                                return str(title)
                            acc_text = bvm.get("accessibilityText")
                            if acc_text:
                                match = re.search(r"([\d,]+)", acc_text)
                                if match:
                                    return match.group(1)
                                    
        # 3. Try segmentedLikeDislikeButtonRenderer (Old UI format)
        buttons = self.find_nested_keys(next_data, "segmentedLikeDislikeButtonRenderer")
        for btn in buttons:
            if isinstance(btn, dict):
                like_btn = btn.get("likeButton", {}).get("toggleButtonRenderer", {})
                if like_btn:
                    default_text = self.parse_runs(like_btn.get("defaultText"))
                    if default_text:
                        return default_text
                    accessibility = like_btn.get("accessibilityData", {}).get("accessibilityData", {}).get("label")
                    if accessibility:
                        match = re.search(r"([\d,]+)", accessibility)
                        if match:
                            return match.group(1)
                            
        # 4. Fallback: Search all buttonViewModel objects that have a title or accessibilityText
        b_view_models = self.find_nested_keys(next_data, "buttonViewModel")
        for bvm in b_view_models:
            if isinstance(bvm, dict):
                acc_text = bvm.get("accessibilityText", "")
                if acc_text and "like this video" in acc_text.lower():
                    match = re.search(r"([\d,]+)", acc_text)
                    if match:
                        return match.group(1)
                    title = bvm.get("title")
                    if title:
                        return str(title)
        return "0"

    def extract_subscriber_count(self, next_data: Any) -> str:
        """Parse subscriber count for a channel from the watch page layout."""
        sub_texts = self.find_nested_keys(next_data, "subscriberCountText")
        for text in sub_texts:
            val = self.parse_runs(text)
            if val:
                return val
        return ""

    def extract_video_id(self, url_or_id: str) -> str:
        """Robust helper to extract 11-char YouTube video ID from URL or return raw ID."""
        url_or_id = url_or_id.strip()
        if len(url_or_id) == 11 and not ("/" in url_or_id or "?" in url_or_id):
            return url_or_id
            
        patterns = [
            r"(?:v=|list=)([^#\&\?]+)",
            r"youtu\.be/([^#\&\?]+)",
            r"embed/([^#\&\?]+)",
            r"v/([^#\&\?]+)",
            r"shorts/([^#\&\?]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, url_or_id)
            if match:
                return match.group(1)
                
        parsed = urlparse(url_or_id)
        if parsed.netloc in ("youtube.com", "www.youtube.com"):
            qs = parse_qs(parsed.query)
            if "v" in qs:
                return qs["v"][0]
                
        return url_or_id

    async def _execute_post(self, endpoint: str, payload: dict) -> dict:
        """Execute a POST request to an InnerTube endpoint with rotated proxies."""
        key = await self._get_innertube_key()
        url = f"https://www.youtube.com/youtubei/v1/{endpoint}?key={key}"
        proxy = self._get_next_proxy()
        
        loop = asyncio.get_running_loop()
        session = requests.Session()
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
            
        headers = {
            "content-type": "application/json",
            "user-agent": payload.get("context", {}).get("client", {}).get("userAgent", "Mozilla/5.0"),
            "accept": "*/*",
            "origin": "https://www.youtube.com"
        }
        
        response = await loop.run_in_executor(
            None,
            lambda: session.post(url, json=payload, headers=headers, impersonate="chrome120", timeout=15)
        )
        
        if response.status_code != 200:
            raise RuntimeError(f"InnerTube POST request failed with status {response.status_code}: {response.text[:200]}")
            
        return response.json()

    async def search(self, query: str, sort: str = "relevance", timeframe: str = "all", limit: int = 20) -> Dict[str, Any]:
        """Perform a stealth search request using InnerTube /v1/search with pagination support."""
        # Map sort and timeframe filters to sp parameter
        sp_mapping = {
            ("relevance", "all"): "EgIQAQ==",
            ("date", "all"): "CAISAhAB",
            ("views", "all"): "CAMSAhAB",
            ("rating", "all"): "CAESAhAB",
            ("date", "hour"): "CAISBAgBEAE=",
            ("date", "day"): "CAISBAgCEAE=",
            ("date", "week"): "CAISBAgDEAE=",
            ("date", "month"): "CAISBAgEEAE=",
            ("date", "year"): "CAISBAgFEAE=",
            ("views", "hour"): "CAMSBQgBEAE=",
            ("views", "day"): "CAMSBQgCEAE=",
            ("views", "week"): "CAMSBQgDEAE=",
            ("views", "month"): "CAMSBQgEEAE=",
            ("views", "year"): "CAMSBQgFEAE=",
            ("relevance", "hour"): "EgQIARAB",
            ("relevance", "day"): "EgQIAhAB",
            ("relevance", "week"): "EgQIAxAB",
            ("relevance", "month"): "EgQIBBAB",
            ("relevance", "year"): "EgQIBRAB",
        }
        sp_param = sp_mapping.get((sort, timeframe), "EgIQAQ==")
        
        payload = {
            "context": self._get_client_context("WEB"),
            "query": query,
            "params": sp_param
        }
        
        data = await self._execute_post("search", payload)
        videos = self.extract_videos(data)
        
        # Paginate if needed
        if len(videos) < limit:
            tokens = self.find_nested_keys(data, "continuationItemRenderer")
            current_token = None
            if tokens:
                current_token = tokens[0].get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
                
            try:
                while current_token and len(videos) < limit:
                    next_payload = {
                        "context": self._get_client_context("WEB"),
                        "continuation": current_token
                    }
                    next_data = await self._execute_post("search", next_payload)
                    new_videos = self.extract_videos(next_data)
                    if not new_videos:
                        break
                        
                    for v in new_videos:
                        if not any(x["id"] == v["id"] for x in videos):
                            videos.append(v)
                            if len(videos) >= limit:
                                break
                                
                    # Extract next continuation token
                    next_tokens = self.find_nested_keys(next_data, "continuationItemRenderer")
                    if next_tokens:
                        current_token = next_tokens[0].get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
                    else:
                        current_token = None
            except Exception as e:
                logger.warn("youtube_search_pagination_failed", query=query, error=str(e))
                
        # Slice results to requested limit
        videos = videos[:limit]
        
        return {
            "query": query,
            "sort": sort,
            "timeframe": timeframe,
            "results_count": len(videos),
            "videos": videos
        }

    async def get_video_details(self, url_or_id: str, comments_limit: int = 20, include_raw: bool = False) -> Dict[str, Any]:
        """Retrieve video details, stream formats, and comments using player and next APIs."""
        video_id = self.extract_video_id(url_or_id)
        
        # 1. Fetch details via /youtubei/v1/player
        # Use WEB client context to avoid Precondition Check/Attestation errors on Android client
        player_payload = {
            "context": self._get_client_context("WEB"),
            "videoId": video_id
        }
        player_data = await self._execute_post("player", player_payload)
        
        # Verify playability
        playability = player_data.get("playabilityStatus", {})
        playability_status = playability.get("status", "OK")
        playability_reason = playability.get("reason", "unknown")
        
        if playability_status != "OK":
            logger.warn("video_playability_warning", video_id=video_id, status=playability_status, reason=playability_reason)
            
        details = player_data.get("videoDetails", {}) or {}
        streaming = player_data.get("streamingData", {}) or {}
        
        # 2. Fetch full description, likes, and comments token via /youtubei/v1/next
        next_payload = {
            "context": self._get_client_context("WEB"),
            "videoId": video_id
        }
        next_data = {}
        try:
            next_data = await self._execute_post("next", next_payload)
        except Exception as e:
            logger.warn("next_endpoint_failed", video_id=video_id, error=str(e))
            
        # Parse title with fallback to next_data
        title = details.get("title", "")
        if not title and next_data:
            title_renderers = self.find_nested_keys(next_data, "videoPrimaryInfoRenderer")
            if title_renderers:
                title = self.parse_runs(title_renderers[0].get("title"))
            if not title:
                titles = self.find_nested_keys(next_data, "title")
                for t in titles:
                    val = self.parse_runs(t)
                    if val and len(val) > 5 and val != "Comments":
                        title = val
                        break
        if not title:
            title = f"YouTube Video {video_id}"
            
        # Parse description with fallback to next_data
        description = details.get("shortDescription", "")
        if not description and next_data:
            descriptions = self.find_nested_keys(next_data, "description")
            for desc in descriptions:
                val = self.parse_runs(desc)
                if val:
                    description = val
                    break
                    
        # Parse view count with fallback to next_data
        views = details.get("viewCount", "0")
        if (views == "0" or not views) and next_data:
            view_texts = self.find_nested_keys(next_data, "viewCountText")
            for vt in view_texts:
                val = self.parse_runs(vt)
                if val:
                    views = val
                    break
                    
        # Extract likes and subscribers
        likes = self.extract_like_count(next_data) if next_data else "0"
        subscribers = self.extract_subscriber_count(next_data) if next_data else ""
        
        # Parse channel name and ID with fallback to next_data
        channel_name = details.get("author", "")
        channel_id = details.get("channelId", "")
        if (not channel_name or not channel_id) and next_data:
            owner_renderers = self.find_nested_keys(next_data, "videoOwnerRenderer")
            for owner in owner_renderers:
                if isinstance(owner, dict):
                    if not channel_name:
                        channel_name = self.parse_runs(owner.get("title"))
                    if not channel_id:
                        runs = owner.get("title", {}).get("runs", [])
                        if runs and isinstance(runs, list):
                            channel_id = runs[0].get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId", "")
                        if not channel_id:
                            channel_id = owner.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId", "")
                            
        # Find comments section continuation token
        comments_token = None
        comments = []
        next_comments_token = None
        
        if next_data:
            sections = self.find_nested_keys(next_data, "itemSectionRenderer")
            for s in sections:
                if isinstance(s, dict) and s.get("targetId") == "comments-section":
                    tokens = self.find_nested_keys(s, "continuationItemRenderer")
                    if tokens:
                        comments_token = tokens[0].get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
                        break
            
            # Fetch top comments if comments token found
            if comments_token and comments_limit > 0:
                current_token = comments_token
                try:
                    while current_token and len(comments) < comments_limit:
                        comments_payload = {
                            "context": self._get_client_context("WEB"),
                            "continuation": current_token
                        }
                        comments_data = await self._execute_post("next", comments_payload)
                        new_comments = self.extract_comments(comments_data)
                        if not new_comments:
                            break
                            
                        for c in new_comments:
                            if not any(x["id"] == c["id"] for x in comments):
                                comments.append(c)
                                if len(comments) >= comments_limit:
                                    break
                                    
                        # Look for subsequent continuation token for comments
                        comment_continuations = self.find_nested_keys(comments_data, "continuationItemRenderer")
                        if comment_continuations:
                            current_token = comment_continuations[0].get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
                            next_comments_token = current_token
                        else:
                            current_token = None
                            next_comments_token = None
                except Exception as e:
                    logger.warn("failed_to_extract_comments", video_id=video_id, error=str(e))
                
        # Structure final metadata object
        res = {
            "id": video_id,
            "video_id": video_id,
            "title": title,
            "description": description,
            "view_count": views,
            "like_count": likes,
            "length_seconds": int(details.get("lengthSeconds", "0")) if details.get("lengthSeconds") else 0,
            "channel": {
                "name": channel_name,
                "id": channel_id,
                "subscribers": subscribers,
                "url": f"https://www.youtube.com/channel/{channel_id}" if channel_id else ""
            },
            "thumbnails": details.get("thumbnail", {}).get("thumbnails", []),
            "streaming_data": {
                "formats": streaming.get("formats", []),
                "adaptive_formats": streaming.get("adaptiveFormats", [])
            },
            "playability_status": playability_status,
            "playability_reason": playability_reason,
            "comments_count": len(comments),
            "comments_continuation_token": next_comments_token,
            "comments": comments
        }
        
        if include_raw:
            res["raw_payload"] = {
                "player": player_data,
                "next": next_data
            }
            
        return res

    async def get_channel_videos(self, channel_id: str, tab_type: str = "videos") -> Dict[str, Any]:
        """Fetch all videos or streams from a channel's videos tab using /v1/browse."""
        # Videos tab parameter: EgZ2aWRlb3PyBgQKAjoA
        # Live tab parameter: EgdzdHJlYW1z8gYECgJ6AA==
        params = "EgZ2aWRlb3PyBgQKAjoA" if tab_type == "videos" else "EgdzdHJlYW1z8gYECgJ6AA=="
        
        payload = {
            "context": self._get_client_context("WEB"),
            "browseId": channel_id,
            "params": params
        }
        
        data = await self._execute_post("browse", payload)
        videos = self.extract_videos(data)
        
        # Get channel header metadata
        channel_name = ""
        header = data.get("header", {})
        if header:
            # Look for c4TabbedHeaderRenderer
            tabbed = header.get("c4TabbedHeaderRenderer", {})
            if tabbed:
                channel_name = tabbed.get("title", "")
                
        if not channel_name:
            # Check other header shapes
            metadata = data.get("metadata", {})
            channel_name = metadata.get("channelMetadataRenderer", {}).get("title", "")
            
        return {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "tab_type": tab_type,
            "results_count": len(videos),
            "videos": videos
        }

    async def get_playlist(self, playlist_id: str) -> Dict[str, Any]:
        """Fetch all videos inside a playlist using /v1/browse."""
        # Playlists browseId starts with VL
        browse_id = f"VL{playlist_id}" if not playlist_id.startswith("VL") else playlist_id
        
        payload = {
            "context": self._get_client_context("WEB"),
            "browseId": browse_id
        }
        
        data = await self._execute_post("browse", payload)
        videos = self.extract_videos(data)
        
        # Extract playlist header metadata
        playlist_title = ""
        metadata = data.get("metadata", {})
        if metadata:
            playlist_title = metadata.get("playlistMetadataRenderer", {}).get("title", "")
            
        return {
            "playlist_id": playlist_id,
            "title": playlist_title,
            "results_count": len(videos),
            "videos": videos
        }

    # ================= Export Format Generators =================

    def export_to_csv(self, data: Union[Dict, List], export_type: str) -> str:
        """Export scraped content to standard CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        
        if export_type == "search" or export_type == "channel" or export_type == "playlist":
            videos = data.get("videos", []) if isinstance(data, dict) else data
            writer.writerow(["Video ID", "Title", "Channel Name", "Channel ID", "Views", "Published Time", "Duration", "Video URL"])
            for v in videos:
                writer.writerow([
                    v.get("video_id", ""),
                    v.get("title", ""),
                    v.get("channel_name", ""),
                    v.get("channel_id", ""),
                    v.get("views", ""),
                    v.get("published_time", ""),
                    v.get("duration", ""),
                    v.get("video_url", "")
                ])
        elif export_type == "comments":
            comments = data.get("comments", []) if isinstance(data, dict) else data
            writer.writerow(["Comment ID", "Author", "Comment Text", "Published Time", "Like Count"])
            for c in comments:
                writer.writerow([
                    c.get("comment_id", ""),
                    c.get("author", ""),
                    c.get("text", ""),
                    c.get("published_time", ""),
                    c.get("like_count", "")
                ])
        elif export_type == "video_details":
            # Flatten main metadata
            writer.writerow(["Field", "Value"])
            writer.writerow(["Video ID", data.get("video_id", "")])
            writer.writerow(["Title", data.get("title", "")])
            writer.writerow(["Views", data.get("view_count", "")])
            writer.writerow(["Likes", data.get("like_count", "")])
            writer.writerow(["Duration (seconds)", data.get("length_seconds", "")])
            channel = data.get("channel", {})
            writer.writerow(["Channel Name", channel.get("name", "")])
            writer.writerow(["Channel ID", channel.get("id", "")])
            writer.writerow(["Subscribers", channel.get("subscribers", "")])
            writer.writerow(["Description", data.get("description", "")])
            
        return output.getvalue()

    def export_to_excel(self, data: Union[Dict, List], export_type: str) -> str:
        """Export scraped content to an Excel-compatible HTML Spreadsheet."""
        html = '<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel" xmlns="http://www.w3.org/TR/REC-html40">\n'
        html += '<head><meta http-equiv="Content-type" content="text/html;charset=utf-8" />\n'
        html += '<style>td { border: 0.5pt solid #cccccc; } th { background-color: #f2f2f2; font-weight: bold; border: 0.5pt solid #cccccc; }</style></head>\n'
        html += '<body><table>\n'
        
        if export_type in ("search", "channel", "playlist"):
            videos = data.get("videos", []) if isinstance(data, dict) else data
            html += '<tr><th>Video ID</th><th>Title</th><th>Channel Name</th><th>Channel ID</th><th>Views</th><th>Published Time</th><th>Duration</th><th>Video URL</th></tr>\n'
            for v in videos:
                html += f'<tr><td>{v.get("video_id", "")}</td><td>{v.get("title", "")}</td><td>{v.get("channel_name", "")}</td><td>{v.get("channel_id", "")}</td><td>{v.get("views", "")}</td><td>{v.get("published_time", "")}</td><td>{v.get("duration", "")}</td><td>{v.get("video_url", "")}</td></tr>\n'
        elif export_type == "comments":
            comments = data.get("comments", []) if isinstance(data, dict) else data
            html += '<tr><th>Comment ID</th><th>Author</th><th>Comment Text</th><th>Published Time</th><th>Like Count</th></tr>\n'
            for c in comments:
                html += f'<tr><td>{c.get("comment_id", "")}</td><td>{c.get("author", "")}</td><td>{c.get("text", "")}</td><td>{c.get("published_time", "")}</td><td>{c.get("like_count", "")}</td></tr>\n'
        elif export_type == "video_details":
            html += '<tr><th colspan="2">Video Details</th></tr>\n'
            html += f'<tr><td>Video ID</td><td>{data.get("video_id", "")}</td></tr>\n'
            html += f'<tr><td>Title</td><td>{data.get("title", "")}</td></tr>\n'
            html += f'<tr><td>Views</td><td>{data.get("view_count", "")}</td></tr>\n'
            html += f'<tr><td>Likes</td><td>{data.get("like_count", "")}</td></tr>\n'
            html += f'<tr><td>Duration (seconds)</td><td>{data.get("length_seconds", "")}</td></tr>\n'
            channel = data.get("channel", {})
            html += f'<tr><td>Channel Name</td><td>{channel.get("name", "")}</td></tr>\n'
            html += f'<tr><td>Channel ID</td><td>{channel.get("id", "")}</td></tr>\n'
            html += f'<tr><td>Subscribers</td><td>{channel.get("subscribers", "")}</td></tr>\n'
            html += f'<tr><td>Description</td><td>{data.get("description", "")}</td></tr>\n'
            
        html += '</table></body></html>'
        return html

    def export_to_html(self, data: Union[Dict, List], export_type: str) -> str:
        """Export scraped content to a gorgeous, premium HTML Dashboard view."""
        # Aesthetic dashboard container
        dashboard = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YouTube Stealth Scraper Export</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #0f172a;
            --card-bg: rgba(30, 41, 59, 0.7);
            --border-glow: rgba(59, 130, 246, 0.3);
            --primary-accent: #3b82f6;
            --accent-glow: #60a5fa;
            --text-main: #f8fafc;
            --text-secondary: #94a3b8;
        }
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        body {
            font-family: 'Outfit', sans-serif;
            background: linear-gradient(135deg, #090d16 0%, var(--bg-dark) 100%);
            color: var(--text-main);
            min-height: 100vh;
            padding: 40px 20px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        header {
            text-align: center;
            margin-bottom: 50px;
            animation: fadeInDown 0.8s ease-out;
        }
        header h1 {
            font-size: 3rem;
            font-weight: 800;
            background: linear-gradient(to right, #3b82f6, #8b5cf6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 10px;
        }
        header p {
            color: var(--text-secondary);
            font-size: 1.1rem;
        }
        .dashboard-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 30px;
            animation: fadeInUp 0.8s ease-out;
        }
        .card {
            background: var(--card-bg);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 16px;
            backdrop-filter: blur(12px);
            overflow: hidden;
            transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            display: flex;
            flex-direction: column;
        }
        .card:hover {
            transform: translateY(-8px);
            border-color: var(--primary-accent);
            box-shadow: 0 10px 30px rgba(59, 130, 246, 0.15);
        }
        .thumbnail-container {
            position: relative;
            width: 100%;
            padding-top: 56.25%; /* 16:9 Aspect Ratio */
            background-color: #000;
            overflow: hidden;
        }
        .thumbnail-container img {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            object-fit: cover;
            transition: transform 0.6s;
        }
        .card:hover .thumbnail-container img {
            transform: scale(1.05);
        }
        .duration-badge {
            position: absolute;
            bottom: 10px;
            right: 10px;
            background: rgba(0, 0, 0, 0.85);
            color: #fff;
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 600;
        }
        .card-content {
            padding: 20px;
            display: flex;
            flex-direction: column;
            flex-grow: 1;
        }
        .card-title {
            font-size: 1.15rem;
            font-weight: 600;
            line-height: 1.4;
            margin-bottom: 12px;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            color: var(--text-main);
        }
        .card-channel {
            font-size: 0.9rem;
            color: var(--primary-accent);
            margin-bottom: 15px;
            text-decoration: none;
            display: inline-block;
        }
        .card-channel:hover {
            text-decoration: underline;
        }
        .card-meta {
            display: flex;
            justify-content: space-between;
            font-size: 0.85rem;
            color: var(--text-secondary);
            margin-top: auto;
        }
        .video-details-container {
            background: var(--card-bg);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 24px;
            backdrop-filter: blur(12px);
            padding: 40px;
            margin-bottom: 40px;
            animation: fadeInUp 0.8s ease-out;
        }
        .video-header {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 40px;
            margin-bottom: 40px;
        }
        @media (max-width: 768px) {
            .video-header {
                grid-template-columns: 1fr;
            }
        }
        .video-player-mockup {
            width: 100%;
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 10px 40px rgba(0,0,0,0.5);
        }
        .video-info h2 {
            font-size: 2rem;
            margin-bottom: 15px;
            line-height: 1.3;
        }
        .video-stats {
            display: flex;
            gap: 20px;
            margin-bottom: 25px;
            flex-wrap: wrap;
        }
        .stat-badge {
            background: rgba(255, 255, 255, 0.05);
            padding: 8px 16px;
            border-radius: 30px;
            font-size: 0.95rem;
            font-weight: 600;
            color: var(--accent-glow);
        }
        .video-desc {
            background: rgba(0, 0, 0, 0.2);
            padding: 20px;
            border-radius: 12px;
            color: var(--text-secondary);
            font-size: 0.95rem;
            line-height: 1.6;
            max-height: 200px;
            overflow-y: auto;
            white-space: pre-wrap;
        }
        .comments-section {
            margin-top: 40px;
        }
        .comments-section h3 {
            font-size: 1.5rem;
            margin-bottom: 25px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            padding-bottom: 10px;
        }
        .comment-item {
            display: flex;
            gap: 15px;
            padding: 20px;
            background: rgba(255, 255, 255, 0.02);
            border-radius: 12px;
            margin-bottom: 15px;
            border: 1px solid rgba(255, 255, 255, 0.02);
        }
        .comment-avatar {
            width: 44px;
            height: 44px;
            border-radius: 50%;
            overflow: hidden;
            background: #fff;
            flex-shrink: 0;
        }
        .comment-avatar img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
        .comment-body h4 {
            font-size: 0.95rem;
            font-weight: 600;
            margin-bottom: 6px;
            color: var(--text-main);
        }
        .comment-body p {
            color: var(--text-secondary);
            font-size: 0.95rem;
            line-height: 1.5;
            margin-bottom: 8px;
        }
        .comment-meta {
            font-size: 0.85rem;
            color: var(--text-secondary);
            display: flex;
            gap: 15px;
        }
        @keyframes fadeInDown {
            from { opacity: 0; transform: translateY(-20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes fadeInUp {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }
    </style>
</head>
<body>
    <div class="container">
"""
        
        # Close elements based on export type
        if export_type in ("search", "channel", "playlist"):
            title = "Search Results"
            subtitle = f"Query: {data.get('query', '')}" if export_type == "search" else f"Channel: {data.get('channel_name', '')}"
            if export_type == "playlist":
                title = "Playlist Videos"
                subtitle = f"Playlist: {data.get('title', '')}"
                
            dashboard += f"""
        <header>
            <h1>{title}</h1>
            <p>{subtitle} — {data.get('results_count', 0)} videos extracted</p>
        </header>
        <div class="dashboard-grid">
"""
            videos = data.get("videos", [])
            for v in videos:
                thumb = v.get("thumbnails", [{}])[0].get("url", "")
                dashboard += f"""
            <div class="card">
                <div class="thumbnail-container">
                    <img src="{thumb}" alt="thumbnail">
                    <span class="duration-badge">{v.get("duration", "")}</span>
                </div>
                <div class="card-content">
                    <h3 class="card-title">{v.get("title", "")}</h3>
                    <a href="{v.get("channel_url", "#")}" target="_blank" class="card-channel">{v.get("channel_name", "")}</a>
                    <div class="card-meta">
                        <span>{v.get("views", "")}</span>
                        <span>{v.get("published_time", "")}</span>
                    </div>
                </div>
            </div>
"""
            dashboard += "</div>"
            
        elif export_type == "video_details":
            thumb = data.get("thumbnails", [{}])[-1].get("url", "")
            channel = data.get("channel", {})
            dashboard += f"""
        <header>
            <h1>Video Insights</h1>
            <p>Stealth Data Extraction Report</p>
        </header>
        <div class="video-details-container">
            <div class="video-header">
                <div>
                    <img src="{thumb}" class="video-player-mockup" alt="thumbnail">
                </div>
                <div class="video-info">
                    <h2>{data.get("title", "")}</h2>
                    <div class="video-stats">
                        <span class="stat-badge">{data.get("view_count", "0")} views</span>
                        <span class="stat-badge">{data.get("like_count", "0")} likes</span>
                        <span class="stat-badge">{data.get("length_seconds", "0")}s duration</span>
                    </div>
                    <div style="margin-bottom: 20px;">
                        <a href="{channel.get("url", "#")}" target="_blank" style="color: var(--primary-accent); text-decoration: none; font-weight: 600; font-size: 1.1rem;">
                            {channel.get("name", "")}
                        </a>
                        <span style="color: var(--text-secondary); margin-left: 10px; font-size: 0.95rem;">{channel.get("subscribers", "")}</span>
                    </div>
                    <div class="video-desc">{data.get("description", "")}</div>
                </div>
            </div>
            
            <div class="comments-section">
                <h3>Top Comments ({len(data.get("comments", []))})</h3>
"""
            for c in data.get("comments", []):
                dashboard += f"""
                <div class="comment-item">
                    <div class="comment-avatar">
                        <img src="{c.get("author_thumbnail", "")}" alt="avatar">
                    </div>
                    <div class="comment-body">
                        <h4>{c.get("author", "")}</h4>
                        <p>{c.get("text", "")}</p>
                        <div class="comment-meta">
                            <span>Likes: {c.get("like_count", "0")}</span>
                            <span>{c.get("published_time", "")}</span>
                        </div>
                    </div>
                </div>
"""
            dashboard += """
            </div>
        </div>
"""
        elif export_type == "comments":
            dashboard += f"""
        <header>
            <h1>Comment Thread Export</h1>
            <p>{len(data.get("comments", []))} comments extracted</p>
        </header>
        <div class="comments-section" style="max-width: 800px; margin: 0 auto;">
"""
            for c in data.get("comments", []):
                dashboard += f"""
            <div class="comment-item" style="background: var(--card-bg);">
                <div class="comment-avatar">
                    <img src="{c.get("author_thumbnail", "")}" alt="avatar">
                </div>
                <div class="comment-body">
                    <h4>{c.get("author", "")}</h4>
                    <p>{c.get("text", "")}</p>
                    <div class="comment-meta">
                        <span>Likes: {c.get("like_count", "0")}</span>
                        <span>{c.get("published_time", "")}</span>
                    </div>
                </div>
            </div>
"""
            dashboard += "</div>"
            
        dashboard += """
    </div>
</body>
</html>
"""
        return dashboard

    async def download_video(self, url_or_id: str, resolution: str = "360p") -> str:
        """
        Download a YouTube video at a specific resolution (muxed format without ffmpeg)
        using yt-dlp and rotated proxies.
        Returns the path to the downloaded temp file.
        """
        import yt_dlp
        import uuid
        
        video_id = self.extract_video_id(url_or_id)
        
        # Determine target resolution height
        try:
            height = int(resolution.replace("p", ""))
        except ValueError:
            height = 360
            
        # Rotate proxy
        proxy = self._get_next_proxy()
        
        # Prepare unique download location
        downloads_dir = ROOT / "downloads"
        downloads_dir.mkdir(exist_ok=True)
        
        # To avoid name conflicts, append a short UUID
        unique_suffix = str(uuid.uuid4())[:8]
        out_tmpl = str(downloads_dir / f"{video_id}_{resolution}_{unique_suffix}.%(ext)s")
        
        # Use single-format selection to ensure we don't trigger merge errors without ffmpeg
        ydl_opts = {
            "format": f"best[height<={height}][ext=mp4]/best[height<={height}]/best",
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
        }
        if proxy:
            ydl_opts["proxy"] = proxy
            
        logger.info("youtube_video_download_started", video_id=video_id, resolution=resolution, proxy=proxy[:15] + "..." if proxy else None)
        
        # Run yt-dlp in an executor to avoid blocking the asyncio event loop
        loop = asyncio.get_running_loop()
        
        def run_ytdlp():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_id, download=True)
                filename = ydl.prepare_filename(info)
                return filename, info
                
        try:
            filename, info = await loop.run_in_executor(None, run_ytdlp)
            
            # Find the actual downloaded file (handling possible ext change by yt-dlp)
            actual_file = None
            if os.path.exists(filename):
                actual_file = filename
            else:
                # Fallback search in downloads dir
                expected_prefix = f"{video_id}_{resolution}_{unique_suffix}"
                for entry in downloads_dir.iterdir():
                    if entry.stem == expected_prefix:
                        actual_file = str(entry)
                        break
                        
            if not actual_file or not os.path.exists(actual_file):
                raise FileNotFoundError(f"yt-dlp completed download but the expected file was not found on disk.")
                
            logger.info("youtube_video_download_completed", video_id=video_id, file_path=actual_file, file_size=os.path.getsize(actual_file))
            return actual_file
            
        except Exception as e:
            logger.error("youtube_video_download_failed", video_id=video_id, resolution=resolution, error=str(e))
            raise RuntimeError(f"Failed to download video: {e}")

