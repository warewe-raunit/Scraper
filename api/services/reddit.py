"""
api/services/scraper.py — Core Reddit API scraping service with failover,
cooldown periods, and automated re-login retry loop.
"""

from __future__ import annotations

import re
import sys
import time
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse
import structlog
from curl_cffi import requests

# Fix path to import core/tools modules correctly
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.dependencies import create_stealth_client
from api.services.registry import AccountRegistry
from api import config

logger = structlog.get_logger(__name__)

class RedditScraperService:
    def __init__(self, registry: AccountRegistry, db: Optional[Any] = None):
        self.registry = registry
        self.db = db

    async def _execute_with_failover(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        requested_account_id: Optional[str] = None,
        max_retries: int = config.REDDIT_MAX_RETRIES
    ) -> Dict[str, Any]:
        """
        Execute a GET request with structured failover, cooldowns, and re-login retries.

        Robust bounded retry with *fast failover*: when a request fails, we switch
        to a different healthy account immediately — no backoff — because a fresh
        account (and, with the rotating proxy pool, a fresh IP) doesn't benefit
        from waiting for the previous one to recover. A backoff sleep is applied
        ONLY when the entire account pool is exhausted (every account cooling
        down): there, waiting is the one thing that helps, so we sleep and retry
        instead of failing instantly. The cap (``max_retries``) keeps a
        genuinely-down target from hanging forever. On exhaustion this raises
        RuntimeError; the public scrape_* methods translate that into a structured
        empty result so the caller always receives output.
        """
        current_account_id = requested_account_id
        backoff_base = config.REDDIT_BACKOFF_BASE
        backoff_cap = config.REDDIT_BACKOFF_CAP
        just_relogged_accounts = set()

        for attempt in range(1, max_retries + 1):
            # 1. Resolve next healthy account ID
            try:
                account_id = await self.registry.get_next_healthy_account(current_account_id)
            except Exception as e:
                # Pool exhausted / all accounts cooling down. This is the only
                # case where waiting helps — back off so cooldowns and the
                # rotating proxy pool can recover, then retry until the budget
                # runs out (instead of failing on the first exhausted check).
                if attempt < max_retries:
                    delay = min(backoff_cap, backoff_base * (2 ** (attempt - 1)))
                    logger.warning(
                        "reddit_account_pool_exhausted_backing_off",
                        attempt=attempt, delay_seconds=round(delay, 1), error=str(e)
                    )
                    await asyncio.sleep(delay)
                    current_account_id = None
                    continue
                logger.error("failover_failed_no_healthy_accounts", error=str(e), url=url)
                raise RuntimeError(f"Scrape request failed: {e}")

            # Get state object to inspect cookie expiry
            state = self.registry.get_account_state(account_id)
            
            # Proactive token check (before sending request)
            if state and state.status == "needs_relogin":
                logger.info("proactive_relogin_triggered", account_id=account_id)
                relogin_success = await self.registry.trigger_relogin(account_id)
                if not relogin_success:
                    # If login failed, skip and select another account
                    current_account_id = None
                    continue
                just_relogged_accounts.add(account_id)

            # 2. Build stealth client for the selected account
            try:
                session = await create_stealth_client(account_id)
            except Exception as e:
                logger.error("failed_to_initialize_client", account_id=account_id, error=str(e))
                # Put account in temporary cooldown and try another
                self.registry.cool_down_account(account_id, duration_seconds=config.REDDIT_TRANSIENT_COOLDOWN_SECONDS)
                self.registry.record_signal(account_id, "inconclusive")
                current_account_id = None
                continue

            log = logger.bind(
                url=url,
                params=params,
                account_id=account_id,
                proxy=session.proxy_display,
                attempt=attempt
            )
            log.debug("executing_reddit_api_request")

            # 3. Perform GET request
            try:
                # Run curl_cffi in a separate thread to keep event loop non-blocking
                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: session.get(url, params=params, impersonate=config.HTTP_IMPERSONATE, timeout=config.REDDIT_REQUEST_TIMEOUT)
                )
                
                log.info("reddit_api_response_received", status_code=response.status_code)

                # Helper to update limits from headers
                def try_update_limits(resp):
                    rl_rem = resp.headers.get("x-ratelimit-remaining")
                    rl_res = resp.headers.get("x-ratelimit-reset")
                    if rl_rem is not None and rl_res is not None:
                        try:
                            self.registry.update_account_limits(
                                account_id,
                                remaining=int(float(rl_rem)),
                                reset_seconds=int(float(rl_res))
                            )
                        except Exception:
                            pass

                # Case A: Success (200 OK)
                if response.status_code == 200:
                    try_update_limits(response)
                    self.registry.record_signal(account_id, "ok")
                    just_relogged_accounts.discard(account_id)
                    return response.json()

                # Case B: Expired Token / Session (401 Unauthorized)
                if response.status_code == 401:
                    log.warn("session_unauthorized_triggering_relogin", response_text=response.text[:200])
                    if account_id in just_relogged_accounts:
                        self.registry.record_signal(account_id, "challenge_after_relogin")
                    else:
                        self.registry.record_signal(account_id, "inconclusive")
                    self.registry.flag_relogin_needed(account_id)
                    # Trigger re-login immediately
                    relogin_success = await self.registry.trigger_relogin(account_id)
                    if relogin_success:
                        # Retry immediately with the same account
                        just_relogged_accounts.add(account_id)
                        current_account_id = account_id
                        continue
                    else:
                        # Relogin failed, pick another account on next retry
                        current_account_id = None
                        continue

                # Case C: Rate Limited (429 Too Many Requests)
                if response.status_code == 429:
                    log.warn("rate_limit_encountered", response_text=response.text[:200])
                    self.registry.record_signal(account_id, "rate_limited")
                    rl_reset = response.headers.get("x-ratelimit-reset")
                    cooldown_seconds = config.REDDIT_BLOCK_COOLDOWN_SECONDS # if reset header missing
                    if rl_reset:
                        try:
                            cooldown_seconds = int(float(rl_reset))
                        except Exception:
                            pass
                    self.registry.cool_down_account(account_id, duration_seconds=cooldown_seconds)
                    current_account_id = None # select a different account on retry
                    continue

                # Case D: Access Forbidden / Blocked (403 Forbidden)
                if response.status_code == 403:
                    log.warn("access_forbidden_ip_flagged", response_text=response.text[:200])
                    if account_id in just_relogged_accounts:
                        self.registry.record_signal(account_id, "challenge_after_relogin")
                    else:
                        self.registry.record_signal(account_id, "forbidden")
                    rl_reset = response.headers.get("x-ratelimit-reset")
                    cooldown_seconds = config.REDDIT_BLOCK_COOLDOWN_SECONDS # if reset header missing
                    if rl_reset:
                        try:
                            cooldown_seconds = int(float(rl_reset))
                        except Exception:
                            pass
                    self.registry.cool_down_account(account_id, duration_seconds=cooldown_seconds)
                    current_account_id = None # select a different account on retry
                    continue

                # Case E: Other Status Errors
                log.error("unhandled_error_status", status_code=response.status_code, text=response.text[:200])
                try_update_limits(response)
                self.registry.record_signal(account_id, "inconclusive")
                self.registry.cool_down_account(account_id, duration_seconds=config.REDDIT_TRANSIENT_COOLDOWN_SECONDS) # Cooldown briefly
                current_account_id = None
                continue

            except requests.errors.RequestsError as e:
                log.error("network_or_proxy_connection_failed", error=str(e))
                self.registry.record_signal(account_id, "inconclusive")
                try:
                    from tools.proxy_provider import get_proxy_provider
                    _prov = get_proxy_provider()
                    if _prov.is_enabled() and getattr(session, "proxy_url", None):
                        _prov.cool_down(session.proxy_url)
                except Exception:
                    pass
                self.registry.cool_down_account(account_id, duration_seconds=config.REDDIT_NETWORK_COOLDOWN_SECONDS)
                current_account_id = None
                continue
            except Exception as e:
                log.error("unexpected_request_exception", error=str(e))
                self.registry.record_signal(account_id, "inconclusive")
                self.registry.cool_down_account(account_id, duration_seconds=config.REDDIT_TRANSIENT_COOLDOWN_SECONDS)
                current_account_id = None
                continue
        
        raise RuntimeError(
            f"Failed to fetch data from Reddit API after {max_retries} attempts "
            f"spanning proxy-pool refreshes (url={url}). All attempts were rate-"
            f"limited, blocked, or failed to connect."
        )

    def _published_at(self, created_utc: Any) -> Optional[str]:
        if created_utc is None:
            return None
        try:
            return datetime.fromtimestamp(float(created_utc), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OSError):
            return None

    def _published_ago(self, created_utc: Any) -> Optional[str]:
        if created_utc is None:
            return None
        try:
            delta_seconds = int(time.time() - float(created_utc))
        except (TypeError, ValueError):
            return None

        if delta_seconds < 0:
            delta_seconds = abs(delta_seconds)
            suffix = "from now"
        else:
            suffix = "ago"

        intervals = [
            ("year", 365 * 24 * 60 * 60),
            ("month", 30 * 24 * 60 * 60),
            ("day", 24 * 60 * 60),
            ("hour", 60 * 60),
            ("minute", 60),
        ]
        for label, seconds in intervals:
            value = delta_seconds // seconds
            if value:
                plural = "s" if value != 1 else ""
                return f"{value} {label}{plural} {suffix}"
        return f"{delta_seconds} seconds {suffix}"

    def clean_post_data(self, post: Dict[str, Any]) -> Dict[str, Any]:
        """Extract and clean standard fields from a raw Reddit post (t3) object."""
        d = post.get("data", {})
        post_id = d.get("id")
        subreddit = d.get("subreddit")
        permalink = d.get("permalink", "")
        post_url = f"https://www.reddit.com{permalink}" if permalink else d.get("url")
        
        # Extract media components
        media = d.get("media")
        preview = d.get("preview")
        video_url = None
        images = []
        
        if media and "reddit_video" in media:
            video_url = media["reddit_video"].get("fallback_url")
        elif preview and "images" in preview:
            for img in preview["images"]:
                source = img.get("source", {})
                if source.get("url"):
                    img_url = source["url"].replace("&amp;", "&")
                    images.append(img_url)
                    
        created_utc = d.get("created_utc")
        return {
            "id": post_id,
            "fullname": d.get("name"),
            "title": d.get("title"),
            "text": d.get("selftext"),
            "username": d.get("author"),
            "author_fullname": d.get("author_fullname"),
            "subreddit": subreddit,
            "subreddit_id": d.get("subreddit_id"),
            "num_comments": d.get("num_comments"),
            "upvotes": d.get("score"),
            "upvote_ratio": d.get("upvote_ratio"),
            "created_utc": created_utc,
            "published_at": self._published_at(created_utc),
            "published_ago": self._published_ago(created_utc),
            "url": post_url,
            "original_url": d.get("url"),
            "is_video": d.get("is_video", False),
            "video_url": video_url,
            "images": images,
            "nsfw": d.get("over_18", False),
            "spoiler": d.get("spoiler", False),
            "pinned": d.get("pinned", False),
            "category": d.get("category"),
            "score": d.get("score")
        }

    def clean_comment_data(self, comment: Dict[str, Any], post_id: Optional[str] = None) -> Dict[str, Any]:
        """Extract and clean standard fields from a raw Reddit comment (t1) object."""
        d = comment.get("data", {})
        comment_id = d.get("id")
        subreddit = d.get("subreddit")
        actual_post_id = post_id or d.get("link_id", "").replace("t3_", "")
        permalink = d.get("permalink", "")
        
        comment_url = f"https://www.reddit.com{permalink}" if permalink else None
        if not comment_url and subreddit and actual_post_id and comment_id:
            comment_url = f"https://www.reddit.com/r/{subreddit}/comments/{actual_post_id}/_/{comment_id}/"
            
        created_utc = d.get("created_utc")
        return {
            "id": comment_id,
            "fullname": d.get("name"),
            "parent_id": d.get("parent_id"),
            "post_id": actual_post_id,
            "username": d.get("author"),
            "body": d.get("body"),
            "body_html": d.get("body_html"),
            "points": d.get("score"),
            "score": d.get("score"),
            "created_utc": created_utc,
            "published_at": self._published_at(created_utc),
            "published_ago": self._published_ago(created_utc),
            "subreddit": subreddit,
            "url": comment_url,
            "replies_count": len(d.get("replies", {}).get("data", {}).get("children", [])) if isinstance(d.get("replies"), dict) else 0
        }

    async def scrape_subreddit(
        self,
        subreddit: str,
        sort: str = "hot",
        timeframe: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
        account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Scrape subreddit posts list."""
        if subreddit:
            subreddit = subreddit.strip("/")
            if subreddit.lower().startswith("r/"):
                subreddit = subreddit[2:]

        valid_sorts = {"hot", "new", "top", "rising"}
        if sort not in valid_sorts:
            sort = "hot"
            
        url = f"https://oauth.reddit.com/r/{subreddit}/{sort}"
        params = {"limit": min(100, limit)}
        if sort == "top":
            params["t"] = timeframe
        if after:
            params["after"] = after
            
        raw_data = await self._execute_with_failover(url, params=params, requested_account_id=account_id)
        posts_list = []
        
        children = raw_data.get("data", {}).get("children", [])
        for child in children:
            if child.get("kind") == "t3":
                posts_list.append(self.clean_post_data(child))
                
        if self.db:
            await self.db.save_reddit_posts(posts_list)
            
        return {
            "subreddit": subreddit,
            "sort": sort,
            "timeframe": timeframe if sort == "top" else None,
            "limit": limit,
            "after": raw_data.get("data", {}).get("after"),
            "before": raw_data.get("data", {}).get("before"),
            "results_count": len(posts_list),
            "posts": posts_list
        }

    async def scrape_post(
        self,
        post_id: str,
        sort: str = "confidence",
        depth: Optional[int] = None,
        limit: int = 100,
        account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Scrape a post's full details and its comment listing."""
        clean_post_id = post_id.replace("t3_", "")
        url = f"https://oauth.reddit.com/comments/{clean_post_id}"
        
        params = {
            "sort": sort,
            "limit": limit
        }
        if depth is not None:
            params["depth"] = depth
            
        raw_data = await self._execute_with_failover(url, params=params, requested_account_id=account_id)
        
        if not isinstance(raw_data, list) or len(raw_data) < 2:
            raise RuntimeError("Unexpected response structure from comments endpoint")
            
        post_listing = raw_data[0]
        comments_listing = raw_data[1]
        
        post_details = {}
        post_children = post_listing.get("data", {}).get("children", [])
        if post_children:
            post_details = self.clean_post_data(post_children[0])
            
        comments = []
        raw_comments = comments_listing.get("data", {}).get("children", [])
        
        def recurse_comments(comment_nodes: List[Dict[str, Any]]):
            for node in comment_nodes:
                if node.get("kind") == "t1":
                    comments.append(self.clean_comment_data(node, post_id=clean_post_id))
                    
                    replies_data = node.get("data", {}).get("replies")
                    if isinstance(replies_data, dict):
                        children = replies_data.get("data", {}).get("children", [])
                        recurse_comments(children)
                        
        recurse_comments(raw_comments)
        
        if self.db:
            if post_details:
                await self.db.save_reddit_posts([post_details])
            if comments:
                await self.db.save_reddit_comments(comments)
        
        return {
            "post": post_details,
            "comments_count": len(comments),
            "comments": comments
        }

    async def search_posts(
        self,
        query: str,
        subreddit: Optional[str] = None,
        sort: str = "relevance",
        timeframe: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
        account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Search posts by keyword (globally or within a specific subreddit)."""
        if subreddit:
            subreddit = subreddit.strip("/")
            if subreddit.lower().startswith("r/"):
                subreddit = subreddit[2:]

        valid_sorts = {"relevance", "hot", "top", "new", "comments"}
        if sort not in valid_sorts:
            sort = "relevance"
            
        if subreddit:
            url = f"https://oauth.reddit.com/r/{subreddit}/search"
            params = {"q": query, "restrict_sr": "on", "limit": min(100, limit), "sort": sort}
        else:
            url = "https://oauth.reddit.com/search"
            params = {"q": query, "limit": min(100, limit), "sort": sort}
            
        if sort == "top" or sort == "relevance":
            params["t"] = timeframe
        if after:
            params["after"] = after
            
        raw_data = await self._execute_with_failover(url, params=params, requested_account_id=account_id)
        posts_list = []
        
        children = raw_data.get("data", {}).get("children", [])
        for child in children:
            if child.get("kind") == "t3":
                posts_list.append(self.clean_post_data(child))
                
        if self.db:
            await self.db.save_reddit_posts(posts_list)
            
        return {
            "query": query,
            "subreddit": subreddit,
            "sort": sort,
            "timeframe": timeframe if sort in {"top", "relevance"} else None,
            "limit": limit,
            "after": raw_data.get("data", {}).get("after"),
            "before": raw_data.get("data", {}).get("before"),
            "results_count": len(posts_list),
            "posts": posts_list
        }

    async def scrape_user_about(self, username: str, account_id: Optional[str] = None) -> Dict[str, Any]:
        """Get user details/about profile data."""
        url = f"https://oauth.reddit.com/user/{username}/about"
        raw_data = await self._execute_with_failover(url, requested_account_id=account_id)
        
        d = raw_data.get("data", {})
        sub = d.get("subreddit", {})
        
        user_profile = {
            "username": d.get("name"),
            "fullname": d.get("name"),
            "id": d.get("id"),
            "created_utc": d.get("created_utc"),
            "link_karma": d.get("link_karma"),
            "comment_karma": d.get("comment_karma"),
            # `.get(key, default)` returns None when the key exists with a null value
            # (common for suspended/restricted users), so guard each operand with `or 0`
            # to avoid `None + None` TypeErrors crashing the whole request.
            "total_karma": (
                d.get("total_karma")
                if d.get("total_karma") is not None
                else (d.get("link_karma") or 0) + (d.get("comment_karma") or 0)
            ),
            "is_employee": d.get("is_employee"),
            "is_gold": d.get("is_gold"),
            "is_mod": d.get("is_mod"),
            "verified": d.get("verified"),
            "icon_img": sub.get("icon_img"),
            "banner_img": sub.get("banner_size") or sub.get("banner_img"),
            "profile_title": sub.get("title"),
            "profile_description": sub.get("public_description"),
            "url": f"https://www.reddit.com/user/{username}/"
        }
        if self.db:
            await self.db.save_reddit_user(user_profile)
        return user_profile

    async def scrape_user_posts(
        self,
        username: str,
        sort: str = "new",
        timeframe: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
        account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get a user's submitted posts."""
        url = f"https://oauth.reddit.com/user/{username}/submitted"
        params = {"limit": min(100, limit), "sort": sort}
        if sort == "top":
            params["t"] = timeframe
        if after:
            params["after"] = after
            
        raw_data = await self._execute_with_failover(url, params=params, requested_account_id=account_id)
        posts_list = []
        
        children = raw_data.get("data", {}).get("children", [])
        for child in children:
            if child.get("kind") == "t3":
                posts_list.append(self.clean_post_data(child))
                
        if self.db:
            await self.db.save_reddit_posts(posts_list)
            
        return {
            "username": username,
            "sort": sort,
            "timeframe": timeframe if sort == "top" else None,
            "limit": limit,
            "after": raw_data.get("data", {}).get("after"),
            "before": raw_data.get("data", {}).get("before"),
            "results_count": len(posts_list),
            "posts": posts_list
        }

    async def scrape_user_comments(
        self,
        username: str,
        sort: str = "new",
        timeframe: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
        account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get a user's submitted comments."""
        url = f"https://oauth.reddit.com/user/{username}/comments"
        params = {"limit": min(100, limit), "sort": sort}
        if sort == "top":
            params["t"] = timeframe
        if after:
            params["after"] = after
            
        raw_data = await self._execute_with_failover(url, params=params, requested_account_id=account_id)
        comments_list = []
        
        children = raw_data.get("data", {}).get("children", [])
        for child in children:
            if child.get("kind") == "t1":
                comments_list.append(self.clean_comment_data(child))
                
        if self.db:
            await self.db.save_reddit_comments(comments_list)
            
        return {
            "username": username,
            "sort": sort,
            "timeframe": timeframe if sort == "top" else None,
            "limit": limit,
            "after": raw_data.get("data", {}).get("after"),
            "before": raw_data.get("data", {}).get("before"),
            "results_count": len(comments_list),
            "comments": comments_list
        }

    async def scrape_by_url(self, url: str, account_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Automatically parse the type of Reddit URL (subreddit, post, user)
        and scrape the corresponding resource.
        """
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        
        user_match = re.match(r"^(?:user|u)/([^/]+)/?$", path, re.IGNORECASE)
        if user_match:
            username = user_match.group(1)
            logger.info("url_parsed_as_user", url=url, username=username)
            return {
                "type": "user",
                "details": await self.scrape_user_about(username, account_id=account_id)
            }
            
        subreddit_match = re.match(r"^r/([^/]+)/?$", path, re.IGNORECASE)
        if subreddit_match:
            subreddit = subreddit_match.group(1)
            logger.info("url_parsed_as_subreddit", url=url, subreddit=subreddit)
            return {
                "type": "subreddit",
                "details": await self.scrape_subreddit(subreddit, sort="hot", limit=25, account_id=account_id)
            }
            
        post_match = re.search(r"comments/([a-z0-9]+)", path, re.IGNORECASE)
        if post_match:
            post_id = post_match.group(1)
            logger.info("url_parsed_as_post", url=url, post_id=post_id)
            return {
                "type": "post",
                "details": await self.scrape_post(post_id, limit=50, account_id=account_id)
            }
            
        raise ValueError(
            "Invalid Reddit URL. Must be a subreddit (e.g. /r/SaaS), "
            "a post thread (e.g. /r/SaaS/comments/1tnnyd4/), "
            "or a user profile (e.g. /u/PaceNormal6940)."
        )
