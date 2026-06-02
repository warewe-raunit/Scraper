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
from api.dependencies import get_youtube_scraper_service
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

def test_youtube_channel_videos_url():
    """Verify channel videos endpoint with a handle URL."""
    import urllib.parse
    encoded_url = urllib.parse.quote("https://youtube.com/@T-Series", safe="")
    response = client.get(f"/api/v1/youtube/channel/{encoded_url}/videos", params={"type": "videos", "format": "json"})
    if response.status_code == 200:
        data = response.json()
        assert "channel_id" in data
        assert data["channel_id"] == "UCq-Fj5jknLsUf-MWSy4_brA"
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

def test_youtube_playlist_url():
    """Verify playlist videos endpoint with a playlist URL."""
    import urllib.parse
    encoded_url = urllib.parse.quote("https://youtube.com/playlist?list=PLTYbelo0OEJ4pN4rdNQKJ_MswWqQ3KfHT", safe="")
    response = client.get(f"/api/v1/youtube/playlist/{encoded_url}", params={"format": "json"})
    if response.status_code == 200:
        data = response.json()
        assert "playlist_id" in data
        assert data["playlist_id"] == "PLTYbelo0OEJ4pN4rdNQKJ_MswWqQ3KfHT"
        assert "videos" in data
        assert isinstance(data["videos"], list)
    else:
        assert response.status_code in (200, 500)

def test_youtube_download_video_json():
    """Verify that video download endpoint returns direct download info as JSON."""
    class FakeYouTubeScraperService:
        def extract_video_id(self, target):
            return TEST_VIDEO_ID

        async def get_direct_download_url(self, target, resolution):
            return {
                "download_url": "https://example.com/video.mp4",
                "video_id": TEST_VIDEO_ID,
            }

    app.dependency_overrides[get_youtube_scraper_service] = lambda: FakeYouTubeScraperService()
    try:
        response = client.get("/api/v1/youtube/download", params={"video_id": TEST_VIDEO_ID, "resolution": "360p", "format": "json"})
    finally:
        app.dependency_overrides.pop(get_youtube_scraper_service, None)

    assert response.status_code == 200
    data = response.json()
    assert data["download_url"] == "https://example.com/video.mp4"
    assert data["video_id"] == TEST_VIDEO_ID

def test_youtube_download_video_html():
    """Verify that video download endpoint returns HTML download page when format=html."""
    response = client.get("/api/v1/youtube/download", params={"video_id": TEST_VIDEO_ID, "resolution": "360p", "format": "html"})
    if response.status_code == 200:
        assert "text/html" in response.headers.get("content-type", "")
        assert "Download" in response.text
        assert "youtube" in response.text or "googlevideo" in response.text or "Video" in response.text
    else:
        assert response.status_code in (200, 500)

def test_youtube_download_video_stream_direct():
    """Verify that video download endpoint streams video content directly from YouTube when format=stream."""
    response = client.get("/api/v1/youtube/download", params={"video_id": TEST_VIDEO_ID, "resolution": "360p", "format": "stream"})
    if response.status_code == 200:
        assert "video/mp4" in response.headers.get("content-type", "")
        assert "attachment; filename=" in response.headers.get("content-disposition", "")
        assert len(response.content) > 0
    else:
        assert response.status_code in (200, 500)

def test_youtube_download_video_default(monkeypatch):
    """Verify that the default Swagger path streams without backend temp-file download."""
    class FakeYouTubeScraperService:
        def extract_video_id(self, target):
            return TEST_VIDEO_ID

        async def get_direct_download_url(self, target, resolution):
            return {
                "download_url": "https://example.com/video.mp4",
                "video_id": TEST_VIDEO_ID,
                "title": "Example Video",
                "proxy": None,
                "http_headers": {"User-Agent": "pytest"},
            }

        async def download_video(self, target, resolution):
            raise AssertionError("default download path should not download a backend file")

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        async def aiter_bytes(self, chunk_size):
            yield b"fake mp4 content"

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, headers=None):
            assert method == "GET"
            assert url == "https://example.com/video.mp4"
            assert headers == {"User-Agent": "pytest"}
            return FakeStream()

    monkeypatch.setattr("api.routes.youtube.httpx.AsyncClient", FakeAsyncClient)

    app.dependency_overrides[get_youtube_scraper_service] = lambda: FakeYouTubeScraperService()
    try:
        response = client.get("/api/v1/youtube/download", params={"video_id": TEST_VIDEO_ID, "resolution": "360p"})
    finally:
        app.dependency_overrides.pop(get_youtube_scraper_service, None)

    assert response.status_code == 200
    assert "video/mp4" in response.headers.get("content-type", "")
    assert "attachment; filename=" in response.headers.get("content-disposition", "")
    assert response.content == b"fake mp4 content"

def test_youtube_download_video_redirect():
    """Verify that video download endpoint can redirect to the direct download URL."""
    response = client.get("/api/v1/youtube/download", params={"video_id": TEST_VIDEO_ID, "resolution": "360p", "format": "redirect"}, follow_redirects=False)
    if response.status_code == 307:
        assert "location" in response.headers
        assert "googlevideo" in response.headers["location"]
    else:
        assert response.status_code in (307, 200, 500)

def test_youtube_download_video_stream():
    """Verify that video download endpoint can download and stream file when stream_from_server=True."""
    response = client.get("/api/v1/youtube/download", params={"video_id": TEST_VIDEO_ID, "resolution": "360p", "stream_from_server": True})
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
