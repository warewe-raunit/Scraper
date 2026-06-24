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
# Connect-phase timeout, split from the (read) total above. A dead/slow rotating
# proxy should fail CONNECT fast (~5s) and let the loop rotate to a fresh proxy,
# instead of burning the full 15s before failing over. Live Reddit still gets the
# full read window. Passed as curl_cffi timeout=(connect, read).
REDDIT_CONNECT_TIMEOUT = _env_float("REDDIT_CONNECT_TIMEOUT", 5.0)
REDDIT_MAX_RETRIES = _env_int("REDDIT_MAX_RETRIES", 8)
REDDIT_BACKOFF_BASE = _env_float("REDDIT_BACKOFF_BASE", 2.0)
REDDIT_BACKOFF_CAP = _env_float("REDDIT_BACKOFF_CAP", 12.0)
# Cooldown applied to an account on a hard block (403/429) when the upstream
# x-ratelimit-reset header is missing.
REDDIT_BLOCK_COOLDOWN_SECONDS = _env_int("REDDIT_BLOCK_COOLDOWN_SECONDS", 600)
# Cooldown on a network/proxy connection failure.
REDDIT_NETWORK_COOLDOWN_SECONDS = _env_int("REDDIT_NETWORK_COOLDOWN_SECONDS", 180)
# Global Reddit concurrency cap — the load control that actually works. Reddit
# 429s come from too many SIMULTANEOUS requests per account (instantaneous
# concurrency), not the per-minute average, so we cap in-flight Reddit requests
# and shed the excess with 503 IMMEDIATELY (no queue/wait → no pile-up). Per
# worker; total ≈ REDDIT_MAX_INFLIGHT × API_WORKERS. At ~3.9s/request latency,
# ~10/worker (≈40 total) holds the measured safe ceiling of ~10 req/s. Raise to
# allow more, lower if you see 429s. 0 disables the cap.
REDDIT_MAX_INFLIGHT = _env_int("REDDIT_MAX_INFLIGHT", 10)

# Per-account proactive rate limiting (token bucket). Reddit allows ~100 req/min
# per account; capping BELOW that prevents most 429s instead of absorbing them.
# Each worker enforces REDDIT_ACCOUNT_RPM / API_WORKERS per account (the budget is
# partitioned across workers). Lower it if you still see 429s; raise toward 100 to
# use the full budget. Set REDDIT_RATE_LIMIT_ENABLED=false to disable.
# Default OFF: the concurrency cap (REDDIT_MAX_INFLIGHT) is the load control that
# actually works. The per-account token bucket over-throttled (10->3.5 req/s) in
# load tests, so prod runs with it disabled; the default matches prod so a deploy
# missing this .env line doesn't silently re-enable the over-throttler.
REDDIT_RATE_LIMIT_ENABLED = _env_bool("REDDIT_RATE_LIMIT_ENABLED", False)
REDDIT_ACCOUNT_RPM = _env_float("REDDIT_ACCOUNT_RPM", 90.0)

# Max wall-clock seconds a single request will spend WAITING on an exhausted pool
# (all accounts cooling) before giving up with 503. Bounds tail latency: short
# Reddit rate-limit windows are caught in ~1-2s; a genuine hard block 503s here
# instead of grinding the full retry budget (~60-96s). The client retries via the
# Retry-After header.
REDDIT_EXHAUSTION_WAIT_BUDGET = _env_float("REDDIT_EXHAUSTION_WAIT_BUDGET", 12.0)
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
# The video-details `next` call (description/likes/comments token) is public
# metadata, same as `player`. By default it goes proxy-first (stealth, original
# behavior). Set true to send it DIRECT-only — skips the up-to-3 proxy attempts
# (8s timeout each) and cuts get_video_details tail latency, at the cost of
# using the server IP for that call. Default false = unchanged behavior.
YOUTUBE_NEXT_DIRECT_FIRST = _env_bool("YOUTUBE_NEXT_DIRECT_FIRST", False)
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
# ANDROID_VR is the InnerTube client that still returns videoDetails (views,
# length) without a poToken/attestation, so it's the fallback when the WEB
# client is bot-gated. Plain ANDROID/IOS now 400 with FAILED_PRECONDITION.
YOUTUBE_ANDROID_VR_CLIENT_VERSION = _env_str("YOUTUBE_ANDROID_VR_CLIENT_VERSION", "1.57.29")
YOUTUBE_ANDROID_VR_USER_AGENT = _env_str(
    "YOUTUBE_ANDROID_VR_USER_AGENT",
    "com.google.android.apps.youtube.vr.oculus/1.57.29 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip",
)
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

# ----------------------------------------------------------------- Concurrency
# Size of the shared thread pool that runs the blocking curl_cffi / yt-dlp calls
# (Reddit, YouTube, warm-path X) off the event loop. asyncio's default executor
# is min(32, cpu+4) ≈ 8-16 on a small VPS, which silently caps HTTP-scraper
# concurrency far below 100: the 17th simultaneous request waits behind a
# 15s-timeout call. These threads are I/O-bound (network waits, not CPU), so
# over-provisioning relative to core count is correct. Set in api/main.py before
# the first request is served.
API_THREAD_POOL_SIZE = _env_int("API_THREAD_POOL_SIZE", 128)
