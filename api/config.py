"""
api/config.py — Centralized, env-overridable tuning constants.

Single source of truth for the magic numbers that were previously scattered
as literals across the scraper services (retry caps, cooldown durations,
backoff curves, request timeouts, impersonation target, and the YouTube
InnerTube client identity). Every value falls back to the exact literal that
used to be hardcoded, so importing this module is a behavior-preserving change
— it only adds the ability to override each value from the environment.

Read at import time. Service modules are imported lazily (after the app's
load_dotenv runs), so .env values are already present.
"""

from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str) -> str:
    val = os.getenv(name)
    return val if val is not None and val.strip() else default


_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in _TRUE


# --------------------------------------------------------------- HTTP client
# curl_cffi browser-impersonation target used by every HTTP-based scraper.
HTTP_IMPERSONATE = _env_str("HTTP_IMPERSONATE", "chrome120")

# --------------------------------------------------------------------- Reddit
REDDIT_REQUEST_TIMEOUT = _env_int("REDDIT_REQUEST_TIMEOUT", 15)
REDDIT_MAX_RETRIES = _env_int("REDDIT_MAX_RETRIES", 8)
REDDIT_BACKOFF_BASE = _env_float("REDDIT_BACKOFF_BASE", 2.0)
REDDIT_BACKOFF_CAP = _env_float("REDDIT_BACKOFF_CAP", 12.0)
# Cooldown applied to an account on a hard block (403/429) when the upstream
# x-ratelimit-reset header is missing.
REDDIT_BLOCK_COOLDOWN_SECONDS = _env_int("REDDIT_BLOCK_COOLDOWN_SECONDS", 600)
# Cooldown on a network/proxy connection failure.
REDDIT_NETWORK_COOLDOWN_SECONDS = _env_int("REDDIT_NETWORK_COOLDOWN_SECONDS", 180)
# Short cooldown for transient/unexpected errors and client-init failures.
REDDIT_TRANSIENT_COOLDOWN_SECONDS = _env_int("REDDIT_TRANSIENT_COOLDOWN_SECONDS", 60)

# -------------------------------------------------------------------- YouTube
YOUTUBE_REQUEST_TIMEOUT = _env_int("YOUTUBE_REQUEST_TIMEOUT", 15)
# Shorter per-proxy timeout: a dead/slow proxy should fail fast instead of
# burning the full request timeout before we move on or fall back to direct.
YOUTUBE_PROXY_REQUEST_TIMEOUT = _env_int("YOUTUBE_PROXY_REQUEST_TIMEOUT", 8)
YOUTUBE_KEY_REQUEST_TIMEOUT = _env_int("YOUTUBE_KEY_REQUEST_TIMEOUT", 10)
YOUTUBE_MAX_RETRIES = _env_int("YOUTUBE_MAX_RETRIES", 8)
YOUTUBE_PROXY_COOLDOWN_SECONDS = _env_int("YOUTUBE_PROXY_COOLDOWN_SECONDS", 300)
# InnerTube is a public API that works without a proxy. After this many proxy
# attempts fail (or on the final attempt) fall back to a DIRECT connection so the
# request still returns data when the rotating pool is unusable for YouTube's
# HTTPS endpoint. Set YOUTUBE_DIRECT_FALLBACK=false to force proxy-only.
YOUTUBE_MAX_PROXY_ATTEMPTS = _env_int("YOUTUBE_MAX_PROXY_ATTEMPTS", 3)
YOUTUBE_DIRECT_FALLBACK = _env_bool("YOUTUBE_DIRECT_FALLBACK", True)
# Subscriber-cap filter (search?max_subscribers=N) safety ceilings: bound how
# far the paginate-until-limit walk goes so a tiny cap can't run unbounded.
YOUTUBE_SUBFILTER_MAX_PAGES = _env_int("YOUTUBE_SUBFILTER_MAX_PAGES", 6)
YOUTUBE_SUBFILTER_MAX_CHANNEL_LOOKUPS = _env_int("YOUTUBE_SUBFILTER_MAX_CHANNEL_LOOKUPS", 80)
# Channel-subscriber lookup: 1 attempt = ZERO retries. A channel lookup that
# fails just drops that video; it must never stall the request retrying dead
# proxies. _execute_post rotates to a fresh proxy per attempt, so raising this
# rotates (it never retries the same proxy), but the default is a single shot.
YOUTUBE_SUBFILTER_LOOKUP_RETRIES = _env_int("YOUTUBE_SUBFILTER_LOOKUP_RETRIES", 1)
# Resolve channel subs over a DIRECT connection only (public metadata, fast and
# reliable) — no slow dead-proxy browse calls. Set false to allow a proxy
# fallback when the direct call returns nothing.
YOUTUBE_SUBFILTER_DIRECT_ONLY = _env_bool("YOUTUBE_SUBFILTER_DIRECT_ONLY", True)
# Max concurrent channel lookups per page (so they can't stampede the pool).
YOUTUBE_SUBFILTER_CONCURRENCY = _env_int("YOUTUBE_SUBFILTER_CONCURRENCY", 8)
YOUTUBE_WEB_CLIENT_VERSION = _env_str("YOUTUBE_WEB_CLIENT_VERSION", "2.20240101.01.00")
YOUTUBE_ANDROID_CLIENT_VERSION = _env_str("YOUTUBE_ANDROID_CLIENT_VERSION", "19.01.35")
YOUTUBE_DEFAULT_USER_AGENT = _env_str(
    "YOUTUBE_DEFAULT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
# Last-resort public InnerTube key used only when both HTTP and Playwright key
# extraction fail. Overridable so a rotated key doesn't require a code change.
YOUTUBE_FALLBACK_INNERTUBE_KEY = _env_str(
    "YOUTUBE_FALLBACK_INNERTUBE_KEY", "AIzaSyAO_JVG4aDXa7KM4V0F4lQcMBa6W4Wl8wg"
)

# ---------------------------------------------------------------------- Proxy
# Default cooldown for the per-service .env proxy pools (X / YouTube).
PROXY_DEFAULT_COOLDOWN_SECONDS = _env_int("PROXY_DEFAULT_COOLDOWN_SECONDS", 300)
