"""
api/services/database.py — Database service for saving scraped Reddit, X, and YouTube data to Supabase.
Supports silent bypass mode when Supabase is not configured.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
import structlog
from supabase import create_client, Client

logger = structlog.get_logger(__name__)

def _parse_int(val: Any) -> int:
    """Helper to convert string numbers with commas/letters into plain integers."""
    if not val:
        return 0
    if isinstance(val, int):
        return val
    try:
        cleaned = str(val).replace(",", "").strip()
        # Parse suffixes like K, M
        if cleaned.lower().endswith("k"):
            return int(float(cleaned[:-1]) * 1000)
        if cleaned.lower().endswith("m"):
            return int(float(cleaned[:-1]) * 1000000)
        return int(float(cleaned))
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
                    "num_comments": post.get("num_comments"),
                    "upvotes": post.get("upvotes") or post.get("score"),
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
                    "score": post.get("score") or post.get("upvotes")
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
                    "points": comment.get("points") or comment.get("score"),
                    "score": comment.get("score") or comment.get("points"),
                    "created_utc": comment.get("created_utc"),
                    "published_at": comment.get("published_at"),
                    "subreddit": comment.get("subreddit"),
                    "url": comment.get("url"),
                    "replies_count": comment.get("replies_count", 0)
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
                    "views": str(video.get("views") or video.get("view_count") or ""),
                    "published_time": video.get("published_time"),
                    "duration": video.get("duration"),
                    "channel_name": video.get("channel_name"),
                    "channel_id": video.get("channel_id"),
                    "channel_url": video.get("channel_url"),
                    "thumbnails": video.get("thumbnails"),
                    "video_url": video.get("video_url") or f"https://www.youtube.com/watch?v={video_id}",
                    "view_count": str(video.get("view_count") or video.get("views") or ""),
                    "like_count": str(video.get("like_count") or ""),
                    "length_seconds": video.get("length_seconds")
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
                    "like_count": str(c.get("like_count", "0")),
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
                "subscribers": str(channel.get("subscribers" or "")),
                "url": channel.get("url") or f"https://www.youtube.com/channel/{channel_id}"
            }
            self.client.table("youtube_channels").upsert(record).execute()
            logger.info("saved_youtube_channel", channel_id=channel_id)
            return True
        except Exception as e:
            logger.error("failed_to_save_youtube_channel", error=str(e))
            return False
