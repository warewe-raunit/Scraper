"""
tools/setup_database.py — Python script to automate table creation in Supabase/PostgreSQL.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Ensure root directory is in python path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(override=True)

SQL_SCHEMA = """
-- 1. Reddit Tables
CREATE TABLE IF NOT EXISTS public.reddit_posts (
    id TEXT PRIMARY KEY,
    fullname TEXT,
    title TEXT,
    text TEXT,
    username TEXT,
    author_fullname TEXT,
    subreddit TEXT,
    subreddit_id TEXT,
    num_comments BIGINT DEFAULT 0,
    upvotes BIGINT DEFAULT 0,
    upvote_ratio DOUBLE PRECISION,
    created_utc DOUBLE PRECISION,
    published_at TEXT,
    url TEXT,
    original_url TEXT,
    is_video BOOLEAN DEFAULT FALSE,
    video_url TEXT,
    images JSONB,
    nsfw BOOLEAN DEFAULT FALSE,
    spoiler BOOLEAN DEFAULT FALSE,
    pinned BOOLEAN DEFAULT FALSE,
    category TEXT,
    score BIGINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS public.reddit_comments (
    id TEXT PRIMARY KEY,
    fullname TEXT,
    parent_id TEXT,
    post_id TEXT,
    username TEXT,
    body TEXT,
    body_html TEXT,
    points BIGINT DEFAULT 0,
    score BIGINT DEFAULT 0,
    created_utc DOUBLE PRECISION,
    published_at TEXT,
    subreddit TEXT,
    url TEXT,
    replies_count BIGINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS public.reddit_users (
    id TEXT PRIMARY KEY,
    username TEXT,
    fullname TEXT,
    created_utc DOUBLE PRECISION,
    link_karma BIGINT DEFAULT 0,
    comment_karma BIGINT DEFAULT 0,
    total_karma BIGINT DEFAULT 0,
    is_employee BOOLEAN DEFAULT FALSE,
    is_gold BOOLEAN DEFAULT FALSE,
    is_mod BOOLEAN DEFAULT FALSE,
    verified BOOLEAN DEFAULT FALSE,
    icon_img TEXT,
    banner_img TEXT,
    profile_title TEXT,
    profile_description TEXT,
    url TEXT
);

-- 2. X (Twitter) Tables
CREATE TABLE IF NOT EXISTS public.x_tweets (
    id TEXT PRIMARY KEY,
    username TEXT,
    fullname TEXT,
    avatar TEXT,
    content TEXT,
    date TEXT,
    likes BIGINT DEFAULT 0,
    retweets BIGINT DEFAULT 0,
    replies BIGINT DEFAULT 0,
    quotes BIGINT DEFAULT 0,
    is_reply BOOLEAN DEFAULT FALSE,
    is_retweet BOOLEAN DEFAULT FALSE,
    is_pinned BOOLEAN DEFAULT FALSE,
    link TEXT
);

CREATE TABLE IF NOT EXISTS public.x_profiles (
    username TEXT PRIMARY KEY,
    fullname TEXT,
    bio TEXT,
    location TEXT,
    website TEXT,
    joined TEXT,
    tweets_count BIGINT DEFAULT 0,
    followers_count BIGINT DEFAULT 0,
    following_count BIGINT DEFAULT 0,
    likes_count BIGINT DEFAULT 0
);

-- 3. YouTube Tables
CREATE TABLE IF NOT EXISTS public.youtube_channels (
    id TEXT PRIMARY KEY,
    name TEXT,
    subscribers TEXT,
    url TEXT
);

CREATE TABLE IF NOT EXISTS public.youtube_videos (
    id TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,
    views TEXT,
    published_time TEXT,
    duration TEXT,
    channel_name TEXT,
    channel_id TEXT,
    channel_url TEXT,
    thumbnails JSONB,
    video_url TEXT,
    -- Numeric counts so ORDER BY / range queries sort correctly (were TEXT, which
    -- sorted lexicographically: '9' > '1000000'). `views` keeps the display string.
    view_count BIGINT DEFAULT 0,
    like_count BIGINT DEFAULT 0,
    length_seconds BIGINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS public.youtube_comments (
    id TEXT PRIMARY KEY,
    author TEXT,
    author_thumbnail TEXT,
    author_id TEXT,
    text TEXT,
    published_time TEXT,
    like_count BIGINT DEFAULT 0,
    video_id TEXT
);

-- 4. Cooldown Registry Table (Deduplication, proxy/account status tracking)
CREATE TABLE IF NOT EXISTS public.cooldown_registry (
    item_key TEXT PRIMARY KEY, -- e.g. "proxy:192.168.1.1" or "account:acc_01"
    pool_label TEXT NOT NULL,  -- e.g. "x_proxy", "reddit_account"
    cooldown_until TIMESTAMP WITH TIME ZONE NOT NULL,
    fail_count INTEGER DEFAULT 0
);

-- 5. Migrations for existing databases (CREATE TABLE IF NOT EXISTS won't alter
--    columns on tables that already exist). Safe to re-run; no-ops once applied.
--    The stored values are already digit-only, so the USING casts cannot fail.
ALTER TABLE public.youtube_videos
    ALTER COLUMN view_count TYPE BIGINT USING NULLIF(view_count, '')::BIGINT,
    ALTER COLUMN like_count TYPE BIGINT USING NULLIF(like_count, '')::BIGINT;

ALTER TABLE public.youtube_comments
    ALTER COLUMN like_count TYPE BIGINT USING NULLIF(like_count, '')::BIGINT;
"""

def main():
    print("=== Supabase Database Schema Setup ===")
    
    # Try to read connection string from environment
    connection_string = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    
    if not connection_string:
        print("\n[INFO] No DATABASE_URL or SUPABASE_DB_URL found in environment variables.")
        print("Please configure your database connection string in your .env file:")
        print("DATABASE_URL=postgresql://postgres:[PASSWORD]@db.[PROJECT_ID].supabase.co:5432/postgres")
        print("\nAlternatively, copy the SQL schema below and paste it into the Supabase SQL Editor:")
        print("-" * 60)
        print(SQL_SCHEMA)
        print("-" * 60)
        return

    print(f"Connecting to database...")
    try:
        # Check for psycopg2
        try:
            import psycopg2
        except ImportError:
            print("[ERROR] psycopg2 is not installed. Please run: pip install psycopg2-binary")
            print("Or copy/paste the SQL schema displayed below directly into your Supabase SQL Editor.")
            print("-" * 60)
            print(SQL_SCHEMA)
            print("-" * 60)
            return

        conn = psycopg2.connect(connection_string)
        conn.autocommit = True
        with conn.cursor() as cursor:
            print("Executing SQL schema...")
            cursor.execute(SQL_SCHEMA)
            print("[SUCCESS] All tables created/verified successfully in your Supabase project!")
        conn.close()
    except Exception as e:
        print(f"[ERROR] Failed to run database setup: {e}")
        print("\nHere is the raw SQL schema so you can run it manually in the Supabase SQL Editor:")
        print("-" * 60)
        print(SQL_SCHEMA)
        print("-" * 60)

if __name__ == "__main__":
    main()
