"""
api/services/database.py — Database service for saving scraped Reddit, X, and YouTube data to Supabase.
Supports silent bypass mode when Supabase is not configured.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional
import structlog
from supabase import create_client, Client

logger = structlog.get_logger(__name__)

_SUFFIX_MULT = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _parse_int(val: Any) -> int:
    """Convert messy count values into plain integers for BIGINT columns.

    Handles ints/floats, comma grouping, K/M/B suffixes, and display strings
    with trailing words, e.g. "1.2M views" -> 1200000, "1,234 subscribers" ->
    1234, "12K" -> 12000. Returns 0 when nothing numeric can be extracted.
    """
    if val is None or val == "":
        return 0
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    # Pull the first number-like token plus an optional K/M/B magnitude suffix.
    match = re.search(r"([\d,]+(?:\.\d+)?)\s*([KkMmBb])?", str(val))
    if not match:
        return 0
    try:
        number = float(match.group(1).replace(",", ""))
        mult = _SUFFIX_MULT.get((match.group(2) or "").lower(), 1)
        return int(number * mult)
    except Exception:
        return 0

class DatabaseService:
    def __init__(self):
        self.url = os.getenv("SUPABASE_URL", "").strip()
        self.key = os.getenv("SUPABASE_KEY", "").strip()
        
        # Check if URL/Key are placeholders or empty
        is_configured = (
            self.url and 
            self.key and 
            "your-project" not in self.url and 
            "your-supabase" not in self.key
        )
        
        self.client: Optional[Client] = None
        self.bypass_mode = not is_configured
        
        if self.bypass_mode:
            logger.warning(
                "database_service_bypass_mode_enabled",
                reason="SUPABASE_URL and/or SUPABASE_KEY is missing or configured with placeholders."
            )
        else:
            try:
                self.client = create_client(self.url, self.key)
                logger.info("database_service_initialized", url=self.url)
            except Exception as e:
                logger.error("database_service_initialization_failed", error=str(e))
                self.bypass_mode = True

    async def save_reddit_posts(self, posts: List[Dict[str, Any]]) -> bool:
        """Upsert a list of Reddit posts to public.reddit_posts."""
        if self.bypass_mode or not self.client:
            return True
        if not posts:
            return True
            
        try:
            # Structuring records to match our schema
            records = []
            for post in posts:
                # Ensure list is cloned and fields are properly mapped
                record = {
                    "id": post.get("id"),
                    "fullname": post.get("fullname"),
                    "title": post.get("title"),
                    "text": post.get("text"),
                    "username": post.get("username"),
                    "author_fullname": post.get("author_fullname"),
                    "subreddit": post.get("subreddit"),
                    "subreddit_id": post.get("subreddit_id"),
                    "num_comments": _parse_int(post.get("num_comments")),
                    "upvotes": _parse_int(post.get("upvotes") or post.get("score")),
                    "upvote_ratio": post.get("upvote_ratio"),
                    "created_utc": post.get("created_utc"),
                    "published_at": post.get("published_at"),
                    "url": post.get("url"),
                    "original_url": post.get("original_url"),
                    "is_video": post.get("is_video", False),
                    "video_url": post.get("video_url"),
                    "images": post.get("images"),
                    "nsfw": post.get("nsfw", False),
                    "spoiler": post.get("spoiler", False),
                    "pinned": post.get("pinned", False),
                    "category": post.get("category"),
                    "score": _parse_int(post.get("score") or post.get("upvotes"))
                }
                records.append(record)
                
            self.client.table("reddit_posts").upsert(records).execute()
            logger.info("saved_reddit_posts", count=len(records))
            return True
        except Exception as e:
            logger.error("failed_to_save_reddit_posts", error=str(e))
            return False

    async def save_reddit_comments(self, comments: List[Dict[str, Any]]) -> bool:
        """Upsert a list of Reddit comments to public.reddit_comments."""
        if self.bypass_mode or not self.client:
            return True
        if not comments:
            return True
            
        try:
            records = []
            for comment in comments:
                record = {
                    "id": comment.get("id"),
                    "fullname": comment.get("fullname"),
                    "parent_id": comment.get("parent_id"),
                    "post_id": comment.get("post_id"),
                    "username": comment.get("username"),
                    "body": comment.get("body"),
                    "body_html": comment.get("body_html"),
                    "points": _parse_int(comment.get("points") or comment.get("score")),
                    "score": _parse_int(comment.get("score") or comment.get("points")),
                    "created_utc": comment.get("created_utc"),
                    "published_at": comment.get("published_at"),
                    "subreddit": comment.get("subreddit"),
                    "url": comment.get("url"),
                    "replies_count": _parse_int(comment.get("replies_count"))
                }
                records.append(record)
                
            self.client.table("reddit_comments").upsert(records).execute()
            logger.info("saved_reddit_comments", count=len(records))
            return True
        except Exception as e:
            logger.error("failed_to_save_reddit_comments", error=str(e))
            return False

    async def save_reddit_user(self, user: Dict[str, Any]) -> bool:
        """Upsert Reddit user details to public.reddit_users."""
        if self.bypass_mode or not self.client:
            return True
        if not user:
            return True
            
        try:
            record = {
                "id": user.get("id"),
                "username": user.get("username"),
                "fullname": user.get("fullname"),
                "created_utc": user.get("created_utc"),
                "link_karma": user.get("link_karma"),
                "comment_karma": user.get("comment_karma"),
                "total_karma": user.get("total_karma"),
                "is_employee": user.get("is_employee"),
                "is_gold": user.get("is_gold"),
                "is_mod": user.get("is_mod"),
                "verified": user.get("verified"),
                "icon_img": user.get("icon_img"),
                "banner_img": user.get("banner_img"),
                "profile_title": user.get("profile_title"),
                "profile_description": user.get("profile_description"),
                "url": user.get("url")
            }
            self.client.table("reddit_users").upsert(record).execute()
            logger.info("saved_reddit_user", username=user.get("username"))
            return True
        except Exception as e:
            logger.error("failed_to_save_reddit_user", error=str(e))
            return False

    async def save_x_tweets(self, tweets: List[Dict[str, Any]]) -> bool:
        """Upsert a list of X tweets to public.x_tweets."""
        if self.bypass_mode or not self.client:
            return True
        if not tweets:
            return True
            
        try:
            records = []
            for tweet in tweets:
                # Resolve primary key (id) fallback
                tweet_id = tweet.get("id")
                if not tweet_id:
                    link = tweet.get("link", "")
                    tweet_id = link.split("/")[-1].split("#")[0] if "/" in link else ""
                
                if not tweet_id:
                    continue
                    
                stats = tweet.get("stats", {})
                record = {
                    "id": tweet_id,
                    "username": tweet.get("username", "").strip().replace("@", ""),
                    "fullname": tweet.get("fullname"),
                    "avatar": tweet.get("avatar"),
                    "content": tweet.get("content") or tweet.get("text"),
                    "date": tweet.get("date"),
                    "likes": _parse_int(stats.get("likes") or tweet.get("likes")),
                    "retweets": _parse_int(stats.get("retweets") or tweet.get("retweets")),
                    "replies": _parse_int(stats.get("replies") or tweet.get("replies")),
                    "quotes": _parse_int(stats.get("quotes") or tweet.get("quotes")),
                    "is_reply": tweet.get("is_reply", False),
                    "is_retweet": tweet.get("is_retweet", False),
                    "is_pinned": tweet.get("is_pinned", False),
                    "link": tweet.get("link")
                }
                records.append(record)
                
            if records:
                self.client.table("x_tweets").upsert(records).execute()
                logger.info("saved_x_tweets", count=len(records))
            return True
        except Exception as e:
            logger.error("failed_to_save_x_tweets", error=str(e))
            return False

    async def save_x_profile(self, profile: Dict[str, Any]) -> bool:
        """Upsert X user profile details to public.x_profiles."""
        if self.bypass_mode or not self.client:
            return True
        if not profile:
            return True
            
        try:
            username = profile.get("username", "").strip().replace("@", "")
            if not username:
                return True
                
            stats = profile.get("stats", {})
            record = {
                "username": username,
                "fullname": profile.get("fullname"),
                "bio": profile.get("bio"),
                "location": profile.get("location"),
                "website": profile.get("website"),
                "joined": profile.get("joined"),
                "tweets_count": _parse_int(stats.get("tweets")),
                "followers_count": _parse_int(stats.get("followers")),
                "following_count": _parse_int(stats.get("following")),
                "likes_count": _parse_int(stats.get("likes"))
            }
            self.client.table("x_profiles").upsert(record).execute()
            logger.info("saved_x_profile", username=username)
            return True
        except Exception as e:
            logger.error("failed_to_save_x_profile", error=str(e))
            return False

    async def save_youtube_videos(self, videos: List[Dict[str, Any]]) -> bool:
        """Upsert a list of YouTube video objects to public.youtube_videos."""
        if self.bypass_mode or not self.client:
            return True
        if not videos:
            return True
            
        try:
            records = []
            for video in videos:
                video_id = video.get("video_id") or video.get("id")
                if not video_id:
                    continue
                    
                record = {
                    "id": video_id,
                    "title": video.get("title"),
                    "description": video.get("description"),
                    # Keep the raw display string for fidelity ("1.2M views")
                    "views": (str(video.get("views")) if video.get("views") is not None else None),
                    "published_time": video.get("published_time"),
                    "duration": video.get("duration"),
                    "channel_name": video.get("channel_name"),
                    "channel_id": video.get("channel_id"),
                    "channel_url": video.get("channel_url"),
                    "thumbnails": video.get("thumbnails"),
                    "video_url": video.get("video_url") or f"https://www.youtube.com/watch?v={video_id}",
                    "view_count": str(_parse_int(video.get("view_count") or video.get("views"))),
                    "like_count": str(_parse_int(video.get("like_count"))),
                    "length_seconds": _parse_int(video.get("length_seconds")),
                }
                records.append(record)
                
            if records:
                self.client.table("youtube_videos").upsert(records).execute()
                logger.info("saved_youtube_videos", count=len(records))
            return True
        except Exception as e:
            logger.error("failed_to_save_youtube_videos", error=str(e))
            return False

    async def save_youtube_comments(self, comments: List[Dict[str, Any]], video_id: str) -> bool:
        """Upsert a list of YouTube comments to public.youtube_comments."""
        if self.bypass_mode or not self.client:
            return True
        if not comments:
            return True
            
        try:
            records = []
            for c in comments:
                comment_id = c.get("comment_id") or c.get("id")
                if not comment_id:
                    continue
                    
                record = {
                    "id": comment_id,
                    "author": c.get("author"),
                    "author_thumbnail": c.get("author_thumbnail"),
                    "author_id": c.get("author_id"),
                    "text": c.get("text"),
                    "published_time": c.get("published_time"),
                    "like_count": str(_parse_int(c.get("like_count"))),
                    "video_id": video_id
                }
                records.append(record)
                
            if records:
                self.client.table("youtube_comments").upsert(records).execute()
                logger.info("saved_youtube_comments", count=len(records))
            return True
        except Exception as e:
            logger.error("failed_to_save_youtube_comments", error=str(e))
            return False

    async def save_youtube_channel(self, channel: Dict[str, Any]) -> bool:
        """Upsert YouTube channel header details to public.youtube_channels."""
        if self.bypass_mode or not self.client:
            return True
        if not channel:
            return True
            
        try:
            channel_id = channel.get("id")
            if not channel_id:
                return True
                
            record = {
                "id": channel_id,
                "name": channel.get("name"),
                # BUG FIX: was `channel.get("subscribers" or "")` which evaluates
                # to channel.get("subscribers") with no default -> stored "None".
                "subscribers": (str(channel.get("subscribers")) if channel.get("subscribers") is not None else None),
                "url": channel.get("url") or f"https://www.youtube.com/channel/{channel_id}"
            }
            self.client.table("youtube_channels").upsert(record).execute()
            logger.info("saved_youtube_channel", channel_id=channel_id)
            return True
        except Exception as e:
            logger.error("failed_to_save_youtube_channel", error=str(e))
            return False

    # ===================== Read / Query methods =====================
    # All reads are bypass-safe: they return [] / None when Supabase is not
    # configured, so callers never crash in local/no-DB mode. Use these instead
    # of re-scraping when the data already exists in the database.

    def _query(
        self,
        table: str,
        *,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 50,
        offset: int = 0,
        order_by: Optional[str] = None,
        descending: bool = True,
    ) -> List[Dict[str, Any]]:
        """Generic select with equality filters, ordering and pagination."""
        if self.bypass_mode or not self.client:
            return []
        try:
            q = self.client.table(table).select("*")
            for col, val in (filters or {}).items():
                if val is not None:
                    q = q.eq(col, val)
            if order_by:
                q = q.order(order_by, desc=descending)
            start = max(0, int(offset))
            end = start + max(1, int(limit)) - 1
            resp = q.range(start, end).execute()
            return resp.data or []
        except Exception as e:
            logger.error("db_query_failed", table=table, error=str(e))
            return []

    def _query_one(self, table: str, filters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        rows = self._query(table, filters=filters, limit=1)
        return rows[0] if rows else None

    # ----- Reddit -----
    async def get_reddit_posts(
        self,
        subreddit: Optional[str] = None,
        username: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return self._query(
            "reddit_posts",
            filters={"subreddit": subreddit, "username": username},
            limit=limit,
            offset=offset,
            order_by="created_utc",
        )

    async def get_reddit_post(self, post_id: str) -> Optional[Dict[str, Any]]:
        return self._query_one("reddit_posts", {"id": post_id})

    async def get_reddit_comments(
        self,
        post_id: Optional[str] = None,
        username: Optional[str] = None,
        subreddit: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return self._query(
            "reddit_comments",
            filters={"post_id": post_id, "username": username, "subreddit": subreddit},
            limit=limit,
            offset=offset,
            order_by="created_utc",
        )

    async def get_reddit_user(self, username: str) -> Optional[Dict[str, Any]]:
        return self._query_one("reddit_users", {"username": username})

    # ----- X (Twitter) -----
    async def get_x_tweets(
        self,
        username: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return self._query(
            "x_tweets",
            filters={"username": username},
            limit=limit,
            offset=offset,
            order_by="date",
        )

    async def get_x_profile(self, username: str) -> Optional[Dict[str, Any]]:
        return self._query_one("x_profiles", {"username": username})

    # ----- YouTube -----
    async def get_youtube_videos(
        self,
        channel_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return self._query(
            "youtube_videos",
            filters={"channel_id": channel_id},
            limit=limit,
            offset=offset,
            order_by="view_count",
        )

    async def get_youtube_video(self, video_id: str) -> Optional[Dict[str, Any]]:
        return self._query_one("youtube_videos", {"id": video_id})

    async def get_youtube_comments(
        self,
        video_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return self._query(
            "youtube_comments",
            filters={"video_id": video_id},
            limit=limit,
            offset=offset,
            order_by="like_count",
        )

    async def get_youtube_channel(self, channel_id: str) -> Optional[Dict[str, Any]]:
        return self._query_one("youtube_channels", {"id": channel_id})
