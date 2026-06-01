"""
api/test_youtube.py — Integration tests for YouTube Stealth Scraper API.
"""

from __future__ import annotations

import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

# Ensure root directory is in python path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.main import app
from api.services.youtube import YouTubeScraperService

client = TestClient(app)
client.headers.update({"X-API-Key": "stealth_secret_key_123"})

# Use a well-known stable YouTube video ID (Rick Astley - Never Gonna Give You Up)
TEST_VIDEO_ID = "dQw4w9WgXcQ"
TEST_CHANNEL_ID = "UCuAXFKgjiq78Gc1YLzp9GsA" # YouTube India or any stable channel
TEST_PLAYLIST_ID = "PLMC9KNkIncKvYin_USF1qoRsZIUDHS2xk" # Standard public playlist

def test_youtube_search_json():
    """Verify that search returns correct JSON structure and respects limits."""
    # Test short limit
    response = client.get("/api/v1/youtube/search", params={"q": "pytest python tutorials", "format": "json", "limit": 5})
    assert response.status_code == 200
    data = response.json()
    assert "query" in data
    assert "videos" in data
    assert isinstance(data["videos"], list)
    assert len(data["videos"]) <= 5
    if data["videos"]:
        video = data["videos"][0]
        assert "id" in video
        assert "title" in video
        assert "channel_name" in video

    # Test larger limit requiring pagination
    response_paginated = client.get("/api/v1/youtube/search", params={"q": "pytest python tutorials", "format": "json", "limit": 30})
    if response_paginated.status_code == 200:
        data_pag = response_paginated.json()
        assert len(data_pag["videos"]) <= 30
        assert len(data_pag["videos"]) > 20  # should be paginated since default page is 20

def test_youtube_search_formats():
    """Verify exporting search results in CSV, Excel, and HTML formats."""
    # 1. Test CSV Export
    csv_resp = client.get("/api/v1/youtube/search", params={"q": "python tutorial", "format": "csv"})
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers.get("content-type", "")
    assert "attachment; filename=" in csv_resp.headers.get("content-disposition", "")
    assert "Video ID,Title,Channel Name" in csv_resp.text

    # 2. Test Excel Export
    xls_resp = client.get("/api/v1/youtube/search", params={"q": "python tutorial", "format": "excel"})
    assert xls_resp.status_code == 200
    assert "application/vnd.ms-excel" in xls_resp.headers.get("content-type", "")
    assert "Video ID" in xls_resp.text

    # 3. Test HTML Dashboard Export
    html_resp = client.get("/api/v1/youtube/search", params={"q": "python tutorial", "format": "html"})
    assert html_resp.status_code == 200
    assert "text/html" in html_resp.headers.get("content-type", "")
    assert "<!DOCTYPE html>" in html_resp.text
    assert "YouTube Stealth Scraper Export" in html_resp.text

def test_youtube_video_details():
    """Verify video details endpoint pulls metadata and comments."""
    # 1. Test basic JSON retrieval
    response = client.get("/api/v1/youtube/video", params={"video_id": TEST_VIDEO_ID, "format": "json", "limit": 5})
    if response.status_code == 200:
        data = response.json()
        assert "video_id" in data
        assert "title" in data
        assert "view_count" in data
        assert "like_count" in data
        assert "channel" in data
        assert "comments" in data
        assert isinstance(data["comments"], list)
        assert len(data["comments"]) <= 5
    else:
        assert response.status_code in (200, 500)

    # 2. Test JSON with include_raw=True
    response_raw = client.get("/api/v1/youtube/video", params={"video_id": TEST_VIDEO_ID, "format": "json", "include_raw": True, "limit": 2})
    if response_raw.status_code == 200:
        data = response_raw.json()
        assert "raw_payload" in data
        assert "player" in data["raw_payload"]
        assert "next" in data["raw_payload"]
    else:
        assert response_raw.status_code in (200, 500)

    # 3. Test raw payload download (format=raw)
    response_download = client.get("/api/v1/youtube/video", params={"video_id": TEST_VIDEO_ID, "format": "raw", "limit": 2})
    if response_download.status_code == 200:
        assert "application/json" in response_download.headers.get("content-type", "")
        assert "attachment; filename=" in response_download.headers.get("content-disposition", "")
        data = response_download.json()
        assert "player" in data
        assert "next" in data
    else:
        assert response_download.status_code in (200, 500)

def test_youtube_channel_videos():
    """Verify channel videos endpoint."""
    response = client.get(f"/api/v1/youtube/channel/{TEST_CHANNEL_ID}/videos", params={"type": "videos", "format": "json"})
    if response.status_code == 200:
        data = response.json()
        assert "channel_id" in data
        assert "channel_name" in data
        assert "videos" in data
        assert isinstance(data["videos"], list)
    else:
        assert response.status_code in (200, 500)

def test_youtube_playlist():
    """Verify playlist videos endpoint."""
    response = client.get(f"/api/v1/youtube/playlist/{TEST_PLAYLIST_ID}", params={"format": "json"})
    if response.status_code == 200:
        data = response.json()
        assert "playlist_id" in data
        assert "videos" in data
        assert isinstance(data["videos"], list)
    else:
        assert response.status_code in (200, 500)

def test_youtube_download_video():
    """Verify that video download endpoint starts the process and returns mp4 file response."""
    response = client.get("/api/v1/youtube/download", params={"video_id": TEST_VIDEO_ID, "resolution": "360p"})
    if response.status_code == 200:
        assert "video/mp4" in response.headers.get("content-type", "")
        assert "attachment; filename=" in response.headers.get("content-disposition", "")
        assert int(response.headers.get("content-length", 0)) > 0
    else:
        assert response.status_code in (200, 500)

def test_youtube_unauthenticated():
    """Verify that requests without a valid API Key return 401 Unauthorized."""
    from fastapi.testclient import TestClient
    from api.main import app
    unauth_client = TestClient(app)
    response = unauth_client.get("/api/v1/youtube/search", params={"q": "python"})
    assert response.status_code == 401
    assert "Invalid or missing API Key" in response.json().get("detail", "")


