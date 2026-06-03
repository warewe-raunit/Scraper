"""
api/test_database.py — Unit tests for verifying DatabaseService and its bypass behavior.
"""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure root directory is in python path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from api.services.database import DatabaseService

@pytest.mark.anyio
async def test_database_bypass():
    print("=== Testing DatabaseService in Bypass Mode ===")
    
    # Force bypass mode by clearing env variables or mocking them
    with patch.dict("os.environ", {"SUPABASE_URL": "", "SUPABASE_KEY": ""}):
        db = DatabaseService()
        assert db.bypass_mode is True, "DatabaseService should be in bypass mode when credentials are empty"
        print("+ Verified bypass mode detection on empty strings")
        
    with patch.dict("os.environ", {"SUPABASE_URL": "https://your-project.supabase.co", "SUPABASE_KEY": "your-supabase-api-key"}):
        db = DatabaseService()
        assert db.bypass_mode is True, "DatabaseService should be in bypass mode when placeholders are detected"
        print("+ Verified bypass mode detection on placeholder strings")

    # In bypass mode, upserts should return True and execute silently
    posts_success = await db.save_reddit_posts([{"id": "test_post", "title": "Test Title"}])
    assert posts_success is True, "save_reddit_posts should return True in bypass mode"
    
    comments_success = await db.save_reddit_comments([{"id": "test_comment", "body": "Test Comment"}])
    assert comments_success is True, "save_reddit_comments should return True in bypass mode"
    
    tweets_success = await db.save_x_tweets([{"id": "test_tweet", "text": "Test Tweet"}])
    assert tweets_success is True, "save_x_tweets should return True in bypass mode"
    
    videos_success = await db.save_youtube_videos([{"video_id": "test_vid", "title": "Test Video"}])
    assert videos_success is True, "save_youtube_videos should return True in bypass mode"
    
    print("+ Verified all save methods return success/True in bypass mode without crashing")
    print("=== Database Bypass Tests Passed ===")

@pytest.mark.anyio
async def test_database_active():
    print("\n=== Testing DatabaseService with Mocked Active Client ===")
    
    # Mock supabase create_client to verify it makes actual upsert calls
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_upsert = MagicMock()
    mock_execute = MagicMock()
    
    mock_client.table.return_value = mock_table
    mock_table.upsert.return_value = mock_upsert
    mock_upsert.execute.return_value = mock_execute
    
    with patch("api.services.database.create_client", return_value=mock_client):
        with patch.dict("os.environ", {"SUPABASE_URL": "https://valid.supabase.co", "SUPABASE_KEY": "valid_key_123"}):
            db = DatabaseService()
            assert db.bypass_mode is False, "DatabaseService should NOT be in bypass mode when valid credentials are set"
            
            # Test Reddit post save
            await db.save_reddit_posts([{"id": "t_post", "title": "Mock Title"}])
            mock_client.table.assert_any_call("reddit_posts")
            
            # Test X tweet save
            await db.save_x_tweets([{"id": "t_tweet", "text": "Mock Tweet"}])
            mock_client.table.assert_any_call("x_tweets")
            
            # Test YouTube video save
            await db.save_youtube_videos([{"video_id": "t_vid", "title": "Mock Video"}])
            mock_client.table.assert_any_call("youtube_videos")
            
    print("+ Verified upsert calls are routed to Supabase tables when client is active")
    print("=== Database Active Client Tests Passed ===")

if __name__ == "__main__":
    asyncio.run(test_database_bypass())
    asyncio.run(test_database_active())
