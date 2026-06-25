"""YouTube count data is whole and normalized across endpoints.

Guards the fixes for missing/misleading numbers: every video carries a numeric
view_count; get_video_details normalizes view/like counts to ints (keeping raw
text); lockup search results recover the channel name; the channel endpoint
resolves the channel's subscriber count.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_KEY", "x")
os.environ.setdefault("GOODPROXIES_ENABLED", "false")

import api.services.youtube as yt  # noqa: E402

Svc = yt.YouTubeScraperService


# ----------------------------------------------------------- count text parser

@pytest.mark.parametrize("text,expected", [
    ("1.2M views", 1_200_000),
    ("12,345 views", 12_345),
    ("12345", 12_345),
    ("1.1B", 1_100_000_000),
    ("12.3K subscribers", 12_300),
    ("1,234 likes", 1_234),
    ("No views", None),
    ("", None),
    (None, None),
])
def test_parse_count_text(text, expected):
    assert Svc.parse_count_text(text) == expected


def test_parse_subscriber_text_still_delegates():
    assert Svc.parse_subscriber_text("29.9M subscribers") == 29_900_000
    assert Svc.parse_subscriber_text("") is None


# -------------------------------------------------------- video renderer shape

def test_video_renderer_has_numeric_view_count():
    svc = Svc()
    r = {
        "videoId": "abc12345678",
        "title": {"simpleText": "Hello"},
        "viewCountText": {"simpleText": "1,234,567 views"},
    }
    out = svc.parse_video_renderer(r)
    assert out["views"] == "1,234,567 views"   # raw text preserved
    assert out["view_count"] == 1_234_567      # normalized integer


def test_video_renderer_unparseable_views_is_none_not_wrong_number():
    svc = Svc()
    out = svc.parse_video_renderer({"videoId": "x", "viewCountText": {"simpleText": ""}})
    assert out["view_count"] is None


# ------------------------------------------------------- lockup (new UI) shape

def _lockup(rows):
    return {
        "contentId": "vid1",
        "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": "My Title"},
            "metadata": {"contentMetadataViewModel": {"metadataRows": rows}},
        }},
    }


def test_lockup_recovers_channel_name_and_view_count():
    svc = Svc()
    vm = _lockup([
        {"metadataParts": [{"text": {"content": "Cool Channel"}}]},
        {"metadataParts": [{"text": {"content": "12K views"}},
                           {"text": {"content": "2 years ago"}}]},
    ])
    out = svc.parse_lockup_view_model(vm)
    assert out["channel_name"] == "Cool Channel"   # was always "" before
    assert out["views"] == "12K views"
    assert out["view_count"] == 12_000
    assert out["published_time"] == "2 years ago"


def test_lockup_new_in_channel_name_not_misread_as_time():
    """The old `"new" in text` heuristic would mislabel this channel as a
    published_time. A real channel literally named with 'New' must survive."""
    svc = Svc()
    vm = _lockup([
        {"metadataParts": [{"text": {"content": "New York Times"}}]},
        {"metadataParts": [{"text": {"content": "500K views"}},
                           {"text": {"content": "1 day ago"}}]},
    ])
    out = svc.parse_lockup_view_model(vm)
    assert out["channel_name"] == "New York Times"
    assert out["published_time"] == "1 day ago"


# ----------------------------------------------- get_channel_videos sub count

def test_get_channel_videos_resolves_subscriber_count(monkeypatch):
    svc = Svc()

    data = {
        "header": {"c4TabbedHeaderRenderer": {
            "title": "Chan",
            "subscriberCountText": {"simpleText": "1.5M subscribers"},
        }},
        "contents": {"videoRenderer": {
            "videoId": "vid00000001",
            "title": {"simpleText": "A video"},
            "viewCountText": {"simpleText": "9,000 views"},
        }},
    }

    async def _resolve(cid):
        return "UC123456789012345678901"
    async def _exec(endpoint, payload, **kw):
        return data
    monkeypatch.setattr(svc, "resolve_channel_id", _resolve)
    monkeypatch.setattr(svc, "_execute_post", _exec)

    out = asyncio.run(svc.get_channel_videos("UCchan"))
    assert out["subscribers"] == "1.5M subscribers"
    assert out["subscriber_count"] == 1_500_000
    assert out["videos"][0]["view_count"] == 9_000   # per-video numeric too
