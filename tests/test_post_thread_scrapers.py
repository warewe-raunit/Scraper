"""
tests/test_post_thread_scrapers.py — unit tests for the two new "full thread"
scrapers: X tweet threads (Nitter conversation parsing) and LinkedIn post
comments (Voyager `included` parsing). These exercise the pure parsing/URL logic
with no network, browser, or live session.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.unauth_x_scraper import (  # noqa: E402
    x_status_to_nitter_path,
    _parse_thread_html,
)
from api.services.linkedin import LinkedInScraperService  # noqa: E402


# --------------------------------------------------------------------------- #
# X — status URL → Nitter path
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url,expected", [
    ("https://x.com/jack/status/20", "/jack/status/20"),
    ("https://twitter.com/jack/status/20?s=20&t=abc", "/jack/status/20"),
    ("https://www.x.com/Some_User1/status/1789012345678901234", "/Some_User1/status/1789012345678901234"),
    ("https://nitter.net/jack/status/20#m", "/jack/status/20"),
    ("jack/status/20", "/jack/status/20"),
    ("https://x.com/i/web/status/1789012345678901234", "/i/status/1789012345678901234"),
])
def test_x_status_to_nitter_path(url, expected):
    assert x_status_to_nitter_path(url) == expected


@pytest.mark.parametrize("bad", ["", "https://x.com/jack", "not a url", "https://x.com/home"])
def test_x_status_to_nitter_path_none(bad):
    assert x_status_to_nitter_path(bad) is None


# --------------------------------------------------------------------------- #
# X — conversation HTML → main tweet + replies
# --------------------------------------------------------------------------- #

CONVERSATION_HTML = """
<div class="conversation">
  <div class="main-tweet">
    <div class="timeline-item">
      <a class="tweet-date"><a href="/op/status/100#m" title="Jan 1, 2024">Jan 1</a></a>
      <a class="tweet-date" href="/op/status/100#m" title="Jan 1, 2024">Jan 1</a>
      <a class="fullname">Original Poster</a>
      <a class="username">@op</a>
      <div class="tweet-content">This is the main tweet body.</div>
      <div class="tweet-stats">
        <span class="tweet-stat"><div class="icon-container"><span class="icon-comment"></span>2</div></span>
        <span class="tweet-stat"><div class="icon-container"><span class="icon-heart"></span>15</div></span>
      </div>
    </div>
  </div>
  <div class="replies">
    <div class="reply">
      <div class="timeline-item">
        <a class="tweet-link" href="/alice/status/101#m"></a>
        <a class="tweet-date" href="/alice/status/101#m" title="Jan 1, 2024">Jan 1</a>
        <a class="fullname">Alice</a>
        <a class="username">@alice</a>
        <div class="tweet-content">First reply from Alice.</div>
        <div class="tweet-stats">
          <span class="tweet-stat"><div class="icon-container"><span class="icon-heart"></span>3</div></span>
        </div>
      </div>
      <div class="timeline-item">
        <a class="tweet-link" href="/bob/status/102#m"></a>
        <a class="tweet-date" href="/bob/status/102#m" title="Jan 1, 2024">Jan 1</a>
        <a class="fullname">Bob</a>
        <a class="username">@bob</a>
        <div class="tweet-content">Nested reply from Bob.</div>
      </div>
    </div>
  </div>
  <div class="show-more"><a href="/op/status/100?cursor=NEXT">Load more</a></div>
</div>
"""


def test_parse_thread_html_main_and_replies():
    parsed = _parse_thread_html(CONVERSATION_HTML)

    main = parsed["main_tweet"]
    assert main["content"] == "This is the main tweet body."
    assert main["link"] == "/op/status/100"
    assert main["stats"]["likes"] == 15
    assert main["stats"]["replies"] == 2

    replies = parsed["replies"]
    assert len(replies) == 2
    assert replies[0]["username"] == "@alice"
    assert replies[0]["content"] == "First reply from Alice."
    assert replies[0]["stats"]["likes"] == 3
    assert replies[1]["fullname"] == "Bob"

    # The main tweet must not leak into the replies list.
    assert all(r["link"] != "/op/status/100" for r in replies)

    # Real "Load more" cursor is picked up.
    assert parsed["next_link"] == "/op/status/100?cursor=NEXT"


def test_parse_thread_html_skips_load_newest_cursor():
    html = CONVERSATION_HTML.replace(
        '<div class="show-more"><a href="/op/status/100?cursor=NEXT">Load more</a></div>',
        '<div class="show-more"><a href="/op/status/100?cursor=NEWER">Load newest</a></div>',
    )
    parsed = _parse_thread_html(html)
    assert parsed["next_link"] == ""


def test_parse_thread_html_empty():
    parsed = _parse_thread_html("<html><body>nothing here</body></html>")
    assert parsed["main_tweet"] == {}
    assert parsed["replies"] == []
    assert parsed["next_link"] == ""


# --------------------------------------------------------------------------- #
# LinkedIn — post URL/URN extraction
# --------------------------------------------------------------------------- #

@pytest.fixture
def svc():
    return LinkedInScraperService(db=None)


@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/posts/john-doe_some-slug-activity-7298012345678901234-AbCd",
     "urn:li:activity:7298012345678901234"),
    ("https://www.linkedin.com/feed/update/urn:li:activity:7298012345678901234/",
     "urn:li:activity:7298012345678901234"),
    ("urn:li:activity:7298012345678901234", "urn:li:activity:7298012345678901234"),
    ("urn:li:ugcPost:7298012345678901234", "urn:li:ugcPost:7298012345678901234"),
    ("7298012345678901234", "urn:li:activity:7298012345678901234"),
])
def test_extract_activity_urn(svc, url, expected):
    assert svc.extract_activity_urn(url) == expected


@pytest.mark.parametrize("bad", ["", "https://www.linkedin.com/in/john-doe", "no id here"])
def test_extract_activity_urn_none(svc, bad):
    assert svc.extract_activity_urn(bad) is None


# --------------------------------------------------------------------------- #
# LinkedIn — comments `included` parsing (REST + Dash/GraphQL shapes)
# --------------------------------------------------------------------------- #

def test_parse_voyager_comments_rest_shape(svc):
    data = {
        "included": [
            {
                "entityUrn": "urn:li:fsd_profile:ABC",
                "firstName": "Jane",
                "lastName": "Smith",
            },
            {
                "$type": "com.linkedin.voyager.feed.Comment",
                "entityUrn": "urn:li:comment:(activity:700,1)",
                "*commenter": "urn:li:fsd_profile:ABC",
                "comment": {"text": "Great post, thanks for sharing!"},
                "createdAt": 1700000000000,
                "socialDetail": {"totalSocialActivityCounts": {"numLikes": 4}},
            },
        ]
    }
    out = svc._parse_voyager_comments(data)
    assert out is not None
    assert len(out) == 1
    c = out[0]
    assert c["author"] == "Jane Smith"
    assert c["text"] == "Great post, thanks for sharing!"
    assert c["likes"] == 4
    assert c["created_at"].startswith("20")
    assert c["comment_urn"] == "urn:li:comment:(activity:700,1)"


def test_parse_voyager_comments_dash_shape(svc):
    data = {
        "included": [
            {
                "$type": "com.linkedin.voyager.dash.social.Comment",
                "entityUrn": "urn:li:fsd_comment:(700,1)",
                "commenter": {"title": {"text": "Acme Corp"}},
                "commentary": {"text": "Congrats team!"},
            },
        ]
    }
    out = svc._parse_voyager_comments(data)
    assert out is not None
    assert len(out) == 1
    assert out[0]["author"] == "Acme Corp"
    assert out[0]["text"] == "Congrats team!"


def test_parse_voyager_comments_dedup_and_empty(svc):
    # Comment objects with no resolvable text are dropped.
    assert svc._parse_voyager_comments({"included": [
        {"$type": "com.linkedin.voyager.feed.Comment",
         "entityUrn": "urn:li:comment:(a,1)", "comment": {}},
    ]}) is None
    # No comment objects at all → None.
    assert svc._parse_voyager_comments({"included": [{"$type": "x.Other"}]}) is None
    # Malformed payload → None.
    assert svc._parse_voyager_comments({}) is None
