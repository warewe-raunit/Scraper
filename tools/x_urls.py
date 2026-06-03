"""Shared X/Twitter URL helpers for browser and JSON endpoints."""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


X_WEB_HOSTS = {
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
    "mobile.x.com",
    "m.x.com",
}


def x_web_base_url(account_id: str | None = None) -> str:
    """Return the profile-consistent X web origin."""
    explicit = (os.getenv("X_WEB_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    category = (
        os.getenv("BROWSER_DEVICE_CATEGORY")
        or os.getenv("BROWSER_PROFILE_DEVICE_CATEGORY")
        or ""
    ).strip().lower()
    if category == "mobile":
        return "https://mobile.x.com"
    return "https://x.com"


def _x_api_base_url() -> str:
    explicit = (os.getenv("X_API_BASE_URL") or "").strip().rstrip("/")
    return explicit or "https://x.com"


def _x_auth_base_url() -> str:
    explicit = (os.getenv("X_AUTH_BASE_URL") or "").strip().rstrip("/")
    return explicit or "https://x.com"


def _is_auth_path(path: str) -> bool:
    clean = "/" + str(path or "").lstrip("/").lower()
    return (
        clean.startswith("/i/flow/login")
        or clean.startswith("/login")
        or clean.startswith("/i/flow/signup")
    )


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


def x_url(
    path: str = "/",
    account_id: str | None = None,
    *,
    params: dict | None = None,
    endpoint: str = "web",
) -> str:
    """Build an X/Twitter URL for the configured web origin."""
    raw = str(path or "/").strip()
    if raw.startswith(("http://", "https://")):
        return canonical_x_url(raw, account_id=account_id, params=params, endpoint=endpoint)
    if endpoint == "api":
        base = _x_api_base_url()
    elif _is_auth_path(raw):
        base = _x_auth_base_url()
    else:
        base = x_web_base_url(account_id)
    return _with_params(_join_path(base, raw), params)


def canonical_x_url(
    value: str,
    account_id: str | None = None,
    *,
    params: dict | None = None,
    endpoint: str = "web",
) -> str:
    """Normalize X/Twitter links to the configured origin."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        return x_url(raw, account_id=account_id, params=params, endpoint=endpoint)
    elif not raw.startswith(("http://", "https://")):
        raw = "https://" + raw

    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host in X_WEB_HOSTS:
        if endpoint == "api":
            base = _x_api_base_url()
        elif _is_auth_path(parsed.path):
            base = _x_auth_base_url()
        else:
            base = x_web_base_url(account_id)
        base_parts = urlparse(base)
        parsed = parsed._replace(
            scheme=base_parts.scheme or "https",
            netloc=base_parts.netloc,
        )

    normalized = urlunparse(parsed)
    return _with_params(normalized, params)
