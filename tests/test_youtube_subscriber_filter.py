"""
tests/test_youtube_subscriber_filter.py — YouTube subscriber-cap filter.

Covers the pure subscriber-text parser, the strict keep/drop filter behavior,
paginate-until-limit with the page ceiling, and the max_subscribers=None
no-op (regression) path.
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

import api.config as config  # noqa: E402
import api.services.youtube as yt  # noqa: E402


# --------------------------------------------------------------- text parsing

@pytest.mark.parametrize("text,expected", [
    ("1.2M subscribers", 1_200_000),
    ("12.3K subscribers", 12_300),
    ("1,234 subscribers", 1_234),
    ("1.1B subscribers", 1_100_000_000),
    ("999 subscribers", 999),
    ("1 subscriber", 1),
    ("No subscribers", None),   # no digits
    ("", None),
    (None, None),
])
def test_parse_subscriber_text(text, expected):
    assert yt.YouTubeScraperService.parse_subscriber_text(text) == expected


# ------------------------------------------------------------- filter behavior

def _svc(monkeypatch):
    """A service whose search avoids real network: stub innertube key + the
    channel sub-count resolver. Each test supplies search-page payloads."""
    svc = yt.YouTubeScraperService()

    async def _fake_key():
        return "FAKE"
    monkeypatch.setattr(svc, "_get_innertube_key", _fake_key)
    return svc


def _video(vid, channel):
    return {"id": vid, "video_id": vid, "channel_id": channel, "title": vid}


def test_filter_keeps_under_cap_drops_over_and_hidden(monkeypatch):
    svc = _svc(monkeypatch)

    # Channel sub counts: small under cap, big over cap, hidden -> None.
    counts = {
        "UCsmall": ("5K subscribers", 5_000),
        "UCbig": ("2M subscribers", 2_000_000),
        "UChidden": ("", None),
    }

    async def _resolve(cid):
        return counts[cid]
    monkeypatch.setattr(svc, "get_channel_subscriber_count", _resolve)

    page = [_video("a", "UCsmall"), _video("b", "UCbig"), _video("c", "UChidden")]
    monkeypatch.setattr(svc, "extract_videos", lambda data: page)

    async def _exec(endpoint, payload, **kw):
        return {}  # no continuation token -> single page
    monkeypatch.setattr(svc, "_execute_post", _exec)

    out = asyncio.run(svc.search("q", limit=10, max_subscribers=10_000))
    ids = [v["id"] for v in out["videos"]]
    assert ids == ["a"]                       # only the small channel passes
    assert out["videos"][0]["subscriber_count"] == 5_000
    assert out["videos"][0]["subscribers"] == "5K subscribers"


def test_none_is_noop_no_channel_lookups(monkeypatch):
    svc = _svc(monkeypatch)

    called = {"n": 0}

    async def _resolve(cid):
        called["n"] += 1
        return ("", None)
    monkeypatch.setattr(svc, "get_channel_subscriber_count", _resolve)

    page = [_video("a", "UCsmall"), _video("b", "UCbig")]
    monkeypatch.setattr(svc, "extract_videos", lambda data: page)

    async def _exec(endpoint, payload, **kw):
        return {}
    monkeypatch.setattr(svc, "_execute_post", _exec)

    out = asyncio.run(svc.search("q", limit=10, max_subscribers=None))
    assert [v["id"] for v in out["videos"]] == ["a", "b"]   # unchanged
    assert called["n"] == 0                                  # no channel lookups
    assert "subscriber_count" not in out["videos"][0]        # fields not added


def test_paginates_until_limit_met(monkeypatch):
    svc = _svc(monkeypatch)

    async def _resolve(cid):
        # "UCpass*" channels are small; "UCfail*" are huge.
        return ("1K subscribers", 1_000) if cid.startswith("UCpass") else ("9M", 9_000_000)
    monkeypatch.setattr(svc, "get_channel_subscriber_count", _resolve)

    # Each page: 1 passing + 1 failing video. Need 3 passing -> needs 3 pages.
    pages = [
        [_video("p1", "UCpass1"), _video("f1", "UCfail1")],
        [_video("p2", "UCpass2"), _video("f2", "UCfail2")],
        [_video("p3", "UCpass3"), _video("f3", "UCfail3")],
        [_video("p4", "UCpass4"), _video("f4", "UCfail4")],
    ]
    state = {"i": 0}
    monkeypatch.setattr(svc, "extract_videos", lambda data: data["__page__"])

    async def _exec(endpoint, payload, **kw):
        i = state["i"]
        state["i"] += 1
        # Always hand back a continuation token so the loop can advance.
        return {"__page__": pages[i] if i < len(pages) else [],
                "continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": "t"}}}}
    monkeypatch.setattr(svc, "_execute_post", _exec)
    # find_nested_keys on our stub dict must surface the token renderer.
    out = asyncio.run(svc.search("q", limit=3, max_subscribers=5_000))
    assert [v["id"] for v in out["videos"]] == ["p1", "p2", "p3"]


def test_page_ceiling_returns_partial(monkeypatch):
    svc = _svc(monkeypatch)
    monkeypatch.setattr(config, "YOUTUBE_SUBFILTER_MAX_PAGES", 2)

    async def _resolve(cid):
        return ("9M subscribers", 9_000_000)  # nothing ever passes
    monkeypatch.setattr(svc, "get_channel_subscriber_count", _resolve)

    monkeypatch.setattr(svc, "extract_videos", lambda data: [_video("x", "UCbig")])

    calls = {"n": 0}

    async def _exec(endpoint, payload, **kw):
        calls["n"] += 1
        return {"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": "t"}}}}
    monkeypatch.setattr(svc, "_execute_post", _exec)

    out = asyncio.run(svc.search("q", limit=10, max_subscribers=1_000))
    assert out["videos"] == []          # nothing passed
    # 1 initial search + at most MAX_PAGES continuation calls (no runaway).
    assert calls["n"] <= 1 + 2


# --------------------------------------------------------- lockup channel id

def test_lockup_extracts_channel_id():
    svc = yt.YouTubeScraperService()
    vm = {
        "contentId": "vid123",
        "metadata": {"lockupMetadataViewModel": {"title": {"content": "T"}}},
        "rendererContext": {
            "commandContext": {
                "onTap": {"innertubeCommand": {
                    "browseEndpoint": {"browseId": "UC" + "x" * 22}
                }}
            }
        },
    }
    parsed = svc.parse_lockup_view_model(vm)
    assert parsed["channel_id"] == "UC" + "x" * 22
    assert parsed["channel_url"].endswith("UC" + "x" * 22)
