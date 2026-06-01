"""Shared Reddit URL helpers for browser and JSON endpoints."""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


REDDIT_WEB_HOSTS = {
    "reddit.com",
    "www.reddit.com",
    "old.reddit.com",
    "new.reddit.com",
    "sh.reddit.com",
    "m.reddit.com",
}


def reddit_web_base_url(account_id: str | None = None) -> str:
    """Return the profile-consistent Reddit web origin."""
    explicit = (os.getenv("REDDIT_WEB_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    category = (
        os.getenv("BROWSER_DEVICE_CATEGORY")
        or os.getenv("BROWSER_PROFILE_DEVICE_CATEGORY")
        or ""
    ).strip().lower()
    if category == "mobile":
        return "https://m.reddit.com"
    return "https://www.reddit.com"


def _reddit_json_base_url() -> str:
    explicit = (os.getenv("REDDIT_JSON_BASE_URL") or "").strip().rstrip("/")
    return explicit or "https://www.reddit.com"


def _reddit_auth_base_url() -> str:
    explicit = (os.getenv("REDDIT_AUTH_BASE_URL") or "").strip().rstrip("/")
    return explicit or "https://www.reddit.com"


def _is_auth_path(path: str) -> bool:
    clean = "/" + str(path or "").lstrip("/").lower()
    return clean.startswith("/login") or clean.startswith("/account/login")


def _join_path(base: str, path: str) -> str:
    clean_path = str(path or "/").strip()
    if not clean_path.startswith("/"):
        clean_path = "/" + clean_path
    return base.rstrip("/") + clean_path


def _with_params(url: str, params: dict | None = None) -> str:
    if not params:
        return url
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None:
            continue
        pairs.append((str(key), str(value)))
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def reddit_url(
    path: str = "/",
    account_id: str | None = None,
    *,
    params: dict | None = None,
    endpoint: str = "web",
) -> str:
    """Build a Reddit URL for the configured web origin or JSON origin."""
    raw = str(path or "/").strip()
    if raw.startswith(("http://", "https://")):
        return canonical_reddit_url(raw, account_id=account_id, params=params, endpoint=endpoint)
    if endpoint == "json":
        base = _reddit_json_base_url()
    elif _is_auth_path(raw):
        base = _reddit_auth_base_url()
    else:
        base = reddit_web_base_url(account_id)
    return _with_params(_join_path(base, raw), params)


def canonical_reddit_url(
    value: str,
    account_id: str | None = None,
    *,
    params: dict | None = None,
    endpoint: str = "web",
) -> str:
    """Normalize Reddit links to the configured Reddit origin."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        return reddit_url(raw, account_id=account_id, params=params, endpoint=endpoint)
    elif not raw.startswith(("http://", "https://")):
        raw = "https://" + raw

    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host in REDDIT_WEB_HOSTS:
        if endpoint == "json":
            base = _reddit_json_base_url()
        elif _is_auth_path(parsed.path):
            base = _reddit_auth_base_url()
        else:
            base = reddit_web_base_url(account_id)
        base_parts = urlparse(base)
        parsed = parsed._replace(
            scheme=base_parts.scheme or "https",
            netloc=base_parts.netloc,
        )

    normalized = urlunparse(parsed)
    return _with_params(normalized, params)
