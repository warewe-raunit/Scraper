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
from tools.proxy_provider import get_proxy_provider
from tools.browser_manager import launch_browser, close_browser
from tools.rotation import CooldownPool

logger = structlog.get_logger(__name__)

class YouTubeScraperService:
    def __init__(self, db: Optional[Any] = None):
        self.db = db
        self._api_key: Optional[str] = None
        self._api_key_lock = asyncio.Lock()

        # Proxy rotation + cooldown is delegated to the shared CooldownPool so
        # the X and YouTube services use one identical implementation.
        accounts = parse_accounts_from_env()
        raw_proxies = [acc["proxy_url"] for acc in accounts if acc.get("proxy_url")]
        self.proxy_pool = CooldownPool(raw_proxies, label="youtube_proxy", default_cooldown=300)

        logger.info("youtube_scraper_service_initialized", proxy_count=len(self.proxy_pool))

    @property
    def proxies(self) -> List[str]:
        return self.proxy_pool.items

    @proxies.setter
    def proxies(self, val: List[str]):
        # Preserves existing cooldowns for surviving proxies (handled by the pool).
        self.proxy_pool.set_items(val)

    def _get_next_proxy(self) -> Optional[str]:
        """Next healthy proxy (round-robin), or shortest-cooldown fallback.

        When the global GoodProxies provider is enabled, proxies come from the
        rotating good-proxies.ru pool; otherwise we use the per-account pool
        built from .env (original behavior).

        Since YouTube InnerTube requests are HTTPS, public HTTP/HTTPS proxies from
        free/public lists almost always fail to establish CONNECT tunnels or are blocked.
        We prefer SOCKS proxies from the pool if any are available.
        """
        provider = get_proxy_provider()
        if provider.is_enabled():
            # Filter the global provider's pool for SOCKS proxies
            socks_candidates = [p for p in provider.pool.items if p.startswith(("socks5://", "socks4://", "socks5h://"))]
            if socks_candidates:
                p = provider.pool.get_next(candidates=socks_candidates)
                if p:
                    return p
            p = provider.get_next()
            if p:
                return p

        # If fallback to per-account pool, also prefer SOCKS if present
        socks_candidates = [p for p in self.proxy_pool.items if p.startswith(("socks5://", "socks4://", "socks5h://"))]
        if socks_candidates:
            p = self.proxy_pool.get_next(candidates=socks_candidates)
            if p:
                return p
        return self.proxy_pool.get_next()

    def _cool_down_proxy(self, proxy: str, duration_seconds: int = 300):
        """Put a proxy on cooldown (e.g. on connection errors or 503 response code)."""
        provider = get_proxy_provider()
        if provider.is_enabled():
            provider.cool_down(proxy, duration_seconds)
        self.proxy_pool.cool_down(proxy, duration_seconds)

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

            # Method 1: GET request extraction with retries
            max_key_retries = 3
            for attempt in range(1, max_key_retries + 1):
                proxy = self._get_next_proxy()
                # On the final retry attempt, fall back to direct connection (no proxy)
                # to prevent complete key extraction failure.
                if attempt == max_key_retries and attempt > 1:
                    proxy = None
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
                            logger.info("extracted_innertube_key_via_http", key=self._api_key[:10] + "...", attempt=attempt)
                            return self._api_key
                    else:
                        if proxy:
                            self._cool_down_proxy(proxy, duration_seconds=300)
                        logger.warn("http_innertube_key_extraction_non_200", status=response.status_code, attempt=attempt)
                except Exception as e:
                    if proxy:
                        self._cool_down_proxy(proxy, duration_seconds=300)
                    logger.warn("http_innertube_key_extraction_failed", attempt=attempt, error=str(e))
                    if attempt < max_key_retries:
                        await asyncio.sleep(0.5)

            # Method 2: Playwright fallback
            try:
                logger.info("falling_back_to_playwright_for_key")
                pw_proxy = self._get_next_proxy()
                pw, browser, context, page = await launch_browser(
                    account_id="acc_01",
                    proxy_url=pw_proxy,
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

    def parse_lockup_view_model(self, vm: dict) -> dict:
        """Extract metadata from a lockupViewModel object (new YouTube UI format)."""
        video_id = vm.get("contentId", "")
        metadata = vm.get("metadata", {}).get("lockupMetadataViewModel", {})
        title = metadata.get("title", {}).get("content", "")
        
        # Extract views and published time
        views_text = ""
        published_time = ""
        meta_parts = self.find_nested_keys(metadata, "text")
        contents = []
        for part in meta_parts:
            if isinstance(part, dict) and "content" in part:
                contents.append(part["content"])
            elif isinstance(part, str):
                contents.append(part)
                
        for c in contents:
            if "views" in c.lower() or "watching" in c.lower():
                views_text = c
            elif "ago" in c.lower() or "streamed" in c.lower() or "new" in c.lower():
                published_time = c
                
        # Duration
        length_text = ""
        badges = self.find_nested_keys(vm, "thumbnailBadgeViewModel")
        for badge in badges:
            if isinstance(badge, dict) and "text" in badge:
                length_text = badge["text"]
                break
                
        # Thumbnails
        thumbnails = []
        thumb_vm = vm.get("contentImage", {}).get("thumbnailViewModel", {})
        sources = self.find_nested_keys(thumb_vm, "sources")
        for src_list in sources:
            if isinstance(src_list, list):
                thumbnails.extend(src_list)
                
        return {
            "id": video_id,
            "video_id": video_id,
            "title": title,
            "description": "",
            "views": views_text,
            "published_time": published_time,
            "duration": length_text,
            "channel_name": "",
            "channel_id": "",
            "channel_url": "",
            "thumbnails": thumbnails,
            "video_url": f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
        }

    def extract_videos(self, json_data: Any) -> List[dict]:
        """Extract and clean all video items in a response."""
        videos = []
        
        # 1. Parse older renderers
        for key in ["videoRenderer", "gridVideoRenderer", "compactVideoRenderer", "playlistVideoRenderer"]:
            renderers = self.find_nested_keys(json_data, key)
            for r in renderers:
                if isinstance(r, dict):
                    parsed = self.parse_video_renderer(r)
                    if parsed.get("id") and not any(v["id"] == parsed["id"] for v in videos):
                        videos.append(parsed)
                        
        # 2. Parse new lockupViewModel
        lockups = self.find_nested_keys(json_data, "lockupViewModel")
        for l in lockups:
            if isinstance(l, dict) and l.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
                parsed = self.parse_lockup_view_model(l)
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

    async def _execute_post(self, endpoint: str, payload: dict, max_retries: int = 8) -> dict:
        """Execute a POST request to an InnerTube endpoint with rotated proxies and retries.

        Robust bounded retry: enough attempts (with growing backoff) to span at
        least one rotating-proxy pool refill (~60s) before giving up, then a
        final direct (no-proxy) attempt so a fully proxy-blocked run still has a
        chance to return data instead of terminating empty.
        """
        last_exception = None
        for attempt in range(1, max_retries + 1):
            key = await self._get_innertube_key()
            url = f"https://www.youtube.com/youtubei/v1/{endpoint}?key={key}"
            proxy = self._get_next_proxy()

            # Last resort fallback: if previous attempts failed and we are on the final retry,
            # try running direct (no proxy) to ensure the service stays up.
            if attempt == max_retries and last_exception is not None:
                logger.warn("youtube_innertube_direct_fallback", endpoint=endpoint, attempt=attempt)
                proxy = None
            
            logger.info("executing_innertube_post", endpoint=endpoint, attempt=attempt, proxy=proxy[:30] + "..." if proxy else None)
            
            try:
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
                
                # Check status and response
                response = await loop.run_in_executor(
                    None,
                    lambda: session.post(url, json=payload, headers=headers, impersonate="chrome120", timeout=15)
                )
                
                if response.status_code == 200:
                    return response.json()
                
                raise RuntimeError(f"InnerTube POST request failed with status {response.status_code}: {response.text[:200]}")
            except Exception as e:
                last_exception = e
                # Flag the proxy as bad and put it on cooldown for 5 minutes
                if proxy:
                    self._cool_down_proxy(proxy, duration_seconds=300)
                    
                logger.warn(
                    "innertube_post_attempt_failed",
                    endpoint=endpoint,
                    attempt=attempt,
                    proxy=proxy[:30] + "..." if proxy else None,
                    error=str(e)
                )
                if attempt < max_retries:
                    # Capped exponential backoff so the loop spans a ~60s pool
                    # refill across the attempt budget (instead of 0.5*attempt,
                    # which barely waited a couple of seconds total).
                    await asyncio.sleep(min(12.0, 1.5 * (2 ** (attempt - 1))))

        raise RuntimeError(
            f"All {max_retries} attempts (spanning proxy-pool refreshes + a "
            f"direct fallback) failed for InnerTube POST to {endpoint}. "
            f"Last error: {last_exception}"
        )

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
        
        if self.db:
            await self.db.save_youtube_videos(videos)
            
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
            
        if self.db:
            if channel_id:
                await self.db.save_youtube_channel({
                    "id": channel_id,
                    "name": channel_name,
                    "subscribers": subscribers,
                    "url": f"https://www.youtube.com/channel/{channel_id}"
                })
            await self.db.save_youtube_videos([res])
            if comments:
                await self.db.save_youtube_comments(comments, video_id)
                
        return res

    def extract_playlist_id(self, url_or_id: str) -> str:
        """Extract playlist ID from URL or return raw ID."""
        url_or_id = url_or_id.strip()
        parsed = urlparse(url_or_id)
        if parsed.netloc:
            qs = parse_qs(parsed.query)
            if "list" in qs:
                return qs["list"][0]
        # Regex check for list= parameter
        match = re.search(r"[?&]list=([^#\&\?]+)", url_or_id)
        if match:
            return match.group(1)
        return url_or_id

    async def resolve_channel_id(self, url_or_handle: str) -> str:
        """Resolve a channel URL, handle, or username to a canonical channel ID (UC...)."""
        url_or_handle = url_or_handle.strip()
        
        # 1. If it's already a UC ID, return it
        if len(url_or_handle) == 24 and url_or_handle.startswith("UC"):
            return url_or_handle
            
        # 2. Extract handle or name from URL if it's a URL
        parsed = urlparse(url_or_handle)
        handle_or_name = url_or_handle
        if parsed.netloc:
            path_parts = [p for p in parsed.path.split("/") if p]
            if path_parts:
                if path_parts[0] == "channel" and len(path_parts) > 1:
                    if path_parts[1].startswith("UC") and len(path_parts[1]) == 24:
                        return path_parts[1]
                elif path_parts[0].startswith("@"):
                    handle_or_name = path_parts[0]
                elif path_parts[0] in ("c", "user") and len(path_parts) > 1:
                    handle_or_name = path_parts[1]
                else:
                    handle_or_name = path_parts[0]
                    
        # Ensure we search with the @ prefix if it looks like a handle name
        query = handle_or_name
        if not query.startswith("@") and not query.startswith("UC"):
            query = f"@{query}"
            
        logger.info("resolving_channel_id_via_search", query=query)
        
        # 3. Call InnerTube search to find the channel
        try:
            payload = {
                "context": self._get_client_context("WEB"),
                "query": query,
            }
            data = await self._execute_post("search", payload)
            channel_renderers = self.find_nested_keys(data, "channelRenderer")
            if channel_renderers and isinstance(channel_renderers[0], dict):
                channel_id = channel_renderers[0].get("channelId")
                if channel_id and channel_id.startswith("UC"):
                    logger.info("resolved_channel_id_via_search", query=query, channel_id=channel_id)
                    return channel_id
        except Exception as e:
            logger.warn("failed_to_resolve_channel_id_via_search", query=query, error=str(e))
            
        return handle_or_name

    async def get_channel_videos(self, channel_id: str, tab_type: str = "videos") -> Dict[str, Any]:
        """Fetch all videos or streams from a channel's videos tab using /v1/browse."""
        # Resolve handle or URL to UC channel ID
        resolved_channel_id = await self.resolve_channel_id(channel_id)
        
        # Videos tab parameter: EgZ2aWRlb3PyBgQKAjoA
        # Live tab parameter: EgdzdHJlYW1z8gYECgJ6AA==
        params = "EgZ2aWRlb3PyBgQKAjoA" if tab_type == "videos" else "EgdzdHJlYW1z8gYECgJ6AA=="
        
        payload = {
            "context": self._get_client_context("WEB"),
            "browseId": resolved_channel_id,
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
            
        # Backfill channel info into the videos list
        for v in videos:
            if not v.get("channel_id"):
                v["channel_id"] = resolved_channel_id
            if not v.get("channel_name"):
                v["channel_name"] = channel_name
            if not v.get("channel_url"):
                v["channel_url"] = f"https://www.youtube.com/channel/{resolved_channel_id}"
                
        if self.db:
            await self.db.save_youtube_channel({
                "id": resolved_channel_id,
                "name": channel_name,
                "subscribers": "",
                "url": f"https://www.youtube.com/channel/{resolved_channel_id}"
            })
            await self.db.save_youtube_videos(videos)
            
        return {
            "channel_id": resolved_channel_id,
            "channel_name": channel_name,
            "tab_type": tab_type,
            "results_count": len(videos),
            "videos": videos
        }

    async def get_playlist(self, playlist_id: str) -> Dict[str, Any]:
        """Fetch all videos inside a playlist using /v1/browse."""
        # Extract playlist ID if full URL is passed
        clean_playlist_id = self.extract_playlist_id(playlist_id)
        
        # Playlists browseId starts with VL
        browse_id = f"VL{clean_playlist_id}" if not clean_playlist_id.startswith("VL") else clean_playlist_id
        
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
            
        if self.db:
            await self.db.save_youtube_videos(videos)
            
        return {
            "playlist_id": clean_playlist_id,
            "title": playlist_title,
            "results_count": len(videos),
            "videos": videos
        }

    async def get_direct_download_url(self, url_or_id: str, resolution: str = "360p") -> Dict[str, Any]:
        """
        Extract format information and return the direct stream URL (googlevideo)
        for local download.
        """
        import yt_dlp
        
        video_id = self.extract_video_id(url_or_id)
        
        try:
            height = int(resolution.replace("p", ""))
        except ValueError:
            height = 360
            
        proxy = self._get_next_proxy()
        
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 10,
            "retries": 2,
        }
        if proxy:
            ydl_opts["proxy"] = proxy
            
        loop = asyncio.get_running_loop()
        def run_ytdlp():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_id, download=False)
                return info
                
        try:
            info = await loop.run_in_executor(None, run_ytdlp)
            
            # Find best muxed mp4 format matching target height
            best_format = None
            for f in info.get("formats", []):
                if f.get("acodec") != "none" and f.get("vcodec") != "none" and f.get("ext") == "mp4":
                    h = f.get("height") or 0
                    if h <= height:
                        if not best_format or h > (best_format.get("height") or 0):
                            best_format = f
                            
            if not best_format:
                # Fallback to any muxed format
                for f in info.get("formats", []):
                    if f.get("acodec") != "none" and f.get("vcodec") != "none":
                        best_format = f
                        break
                        
            if not best_format:
                raise ValueError("No suitable video stream format found.")
                
            return {
                "title": info.get("title", f"YouTube Video {video_id}"),
                "video_id": video_id,
                "resolution": f"{best_format.get('height', height)}p",
                "download_url": best_format.get("url"),
                "http_headers": best_format.get("http_headers") or info.get("http_headers") or {},
                "proxy": proxy,
                "instructions": "Open the download_url in a new tab, right-click on the video player, and select 'Save Video As...' to download it directly to your device."
            }
        except Exception as e:
            if proxy:
                self._cool_down_proxy(proxy, duration_seconds=300)
            logger.error("youtube_direct_url_extraction_failed", video_id=video_id, error=str(e))
            raise RuntimeError(f"Failed to extract direct download URL: {e}")

    async def download_video(self, url_or_id: str, resolution: str = "360p", max_retries: int = 3) -> str:
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
            
        # Prepare unique download location
        downloads_dir = ROOT / "downloads"
        downloads_dir.mkdir(exist_ok=True)
        
        last_exception = None
        for attempt in range(1, max_retries + 1):
            # Rotate proxy
            proxy = self._get_next_proxy()
            
            # To avoid name conflicts, append a short UUID
            unique_suffix = str(uuid.uuid4())[:8]
            out_tmpl = str(downloads_dir / f"{video_id}_{resolution}_{unique_suffix}.%(ext)s")
            
            # Use single-format selection to ensure we don't trigger merge errors without ffmpeg
            ydl_opts = {
                "format": f"best[height<={height}][ext=mp4]/best[height<={height}]/best",
                "outtmpl": out_tmpl,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 10,
                "retries": 2,
            }
            if proxy:
                ydl_opts["proxy"] = proxy
                
            logger.info("youtube_video_download_attempt", video_id=video_id, resolution=resolution, attempt=attempt, proxy=proxy[:15] + "..." if proxy else None)
            
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
                last_exception = e
                # Flag the proxy as bad and put it on cooldown
                if proxy:
                    self._cool_down_proxy(proxy, duration_seconds=300)
                logger.warn("youtube_video_download_attempt_failed", video_id=video_id, resolution=resolution, attempt=attempt, error=str(e))
                if attempt < max_retries:
                    await asyncio.sleep(1)
                    
        logger.error("youtube_video_download_failed_all_attempts", video_id=video_id, resolution=resolution, error=str(last_exception))
        raise RuntimeError(f"Failed to download video after {max_retries} attempts: {last_exception}")
