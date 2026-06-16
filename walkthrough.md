# Walkthrough — Latency, Cleanup & Production-Readiness Pass

This document records the analysis of the Reddit Stealth Scraper codebase and the
changes applied in this pass. Scope for this pass was **Phase 0 (production +
cleanup) and Phase 1 (safe performance wins)** — only changes provable by static
review and the existing `tests/` suite (no live proxies/credentials were
available to verify network-behavior changes).

---

## 1. What the system is

```
FastAPI (api/main.py)
 ├─ Reddit   → api/services/reddit.py      (curl_cffi sync + executor, OAuth cookie sessions)
 ├─ LinkedIn → api/services/linkedin.py    (curl_cffi sync + executor, account pool, Playwright heal)
 ├─ X/Nitter → api/services/x.py → tools/unauth_x_scraper.py (HTTP + Playwright fallback)
 ├─ YouTube  → api/services/youtube.py     (InnerTube POST sync + executor, Playwright key fallback)
 └─ shared:  tools/proxy_provider.py (good-proxies health daemon) · tools/rotation.py (CooldownPool)
             tools/browser_manager.py (stealth Playwright) · api/dependencies.py (HTTP client builder)
```

All HTTP scrapers share the same pattern: synchronous `curl_cffi` run inside
`loop.run_in_executor`, with proxy rotation + per-item cooldown delegated to the
shared `CooldownPool`.

---

## 2. Findings (full analysis)

### Latency bottlenecks (confirmed in code)

| # | Location | Issue | Status this pass |
|---|----------|-------|------------------|
| L1 | all 4 services | sync `curl_cffi` in `run_in_executor` saturates the thread pool under slow proxies | **Deferred** (Phase 3 — needs live verification of impersonate/proxy parity) |
| L2 | `reddit.py` `_execute_with_failover` | backoff `sleep` fires even when switching to a fresh account+IP | **Deferred** (Phase 2) |
| L3 | `youtube.py` `get_video_details` | independent `player` + `next` POSTs run serially (~3s → 1.5s) | **Fixed** |
| L4 | `unauth_x_scraper.py` | `LazyBrowser` built + closed per request, defeating its own persistence | **Deferred** (Phase 2) |
| L5 | `youtube.py` `_get_innertube_key` | Playwright key extraction can sit in the request path | **Fixed** (startup warmup) |
| L6 | `linkedin.py` | on-demand proxy probe + mid-request Playwright cookie refresh block the client | **Deferred** (Phase 3) |
| L7 | `dependencies.py` `create_stealth_client` | fingerprint profile regenerated on every request | **Fixed** (per-account cache) |

### Sloppy code / hardcoded values / production blockers

- **Hardcoded default API key** `"stealth_secret_key_123"` in `verify_api_key` — a
  source-visible secret used whenever `API_KEY` was unset. **Fixed.**
- **Invalid CORS** — `allow_origins=["*"]` combined with `allow_credentials=True`
  (rejected by browsers, insecure). **Fixed.**
- Hardcoded magic numbers scattered across services (cooldowns `600/180/60`,
  backoff `2.0/12.0`, timeouts `15/10`, `impersonate="chrome120"`), plus YouTube's
  `clientVersion`, default UA, and fallback InnerTube key. **Centralized to config.**
- Duplicated proxy-rotation boilerplate across `x.py` and `youtube.py`. **Deduped.**
- Dead/unresolved return-type annotations in `dependencies.py`. **Fixed.**
- `import os as _os` inside `lifespan`; dead imports (`time`, `json`, `csv`, `io`,
  `Union`, `close_browser`) in `youtube.py`; f-string with no placeholder. **Cleaned.**
- Deprecated FastAPI `Query(regex=...)` (Pydantic v2 wants `pattern=`) across all
  Reddit + YouTube routes. **Fixed.**

---

## 3. Changes applied

### New files

- **`api/config.py`** — single source of truth for env-overridable tuning
  constants. Every value defaults to the exact literal it replaced, so importing
  it is behavior-preserving. New optional env vars (all have safe defaults):
  `HTTP_IMPERSONATE`, `REDDIT_REQUEST_TIMEOUT`, `REDDIT_MAX_RETRIES`,
  `REDDIT_BACKOFF_BASE`, `REDDIT_BACKOFF_CAP`, `REDDIT_BLOCK_COOLDOWN_SECONDS`,
  `REDDIT_NETWORK_COOLDOWN_SECONDS`, `REDDIT_TRANSIENT_COOLDOWN_SECONDS`,
  `YOUTUBE_REQUEST_TIMEOUT`, `YOUTUBE_KEY_REQUEST_TIMEOUT`, `YOUTUBE_MAX_RETRIES`,
  `YOUTUBE_PROXY_COOLDOWN_SECONDS`, `YOUTUBE_WEB_CLIENT_VERSION`,
  `YOUTUBE_ANDROID_CLIENT_VERSION`, `YOUTUBE_DEFAULT_USER_AGENT`,
  `YOUTUBE_FALLBACK_INNERTUBE_KEY`, `PROXY_DEFAULT_COOLDOWN_SECONDS`.

- **`api/services/proxy_base.py`** — `ProxyRotatingService` base class holding the
  proxy-pool init, `proxies` property, `_get_next_proxy`, and `_cool_down_proxy`
  that `x.py` and `youtube.py` previously each hand-rolled. The only real
  difference (YouTube preferring SOCKS in the unverified .env fallback) is now the
  `prefer_socks` class flag.

### Security / production (Phase 0)

- **`api/dependencies.py` — fail-closed auth.** Removed the hardcoded fallback key.
  If `API_KEY` is unset, every request is rejected with `503` unless auth is
  *explicitly* disabled with `API_AUTH_DISABLED=true` (development only). A
  misconfigured production deploy can no longer ship with a known key.
- **`api/main.py` — CORS.** Origins now come from `CORS_ALLOW_ORIGINS`
  (comma-separated, default `*`). Credentials are enabled **only** when an explicit
  origin list is set — the wildcard case now correctly disables credentials,
  fixing the spec-invalid combination.

### Performance (Phase 1)

- **`youtube.py` `get_video_details`** — `player` and `next` now run concurrently
  via `asyncio.gather(..., return_exceptions=True)`. `player` failures propagate
  exactly as before; `next` failures still degrade gracefully to empty. Roughly
  halves base latency for the video-details path.
- **`api/main.py`** — InnerTube key is warmed in a background task on startup
  (`YOUTUBE_WARMUP_ON_STARTUP=true` by default), so the first video request doesn't
  pay key-extraction cost inline. Lazy extraction remains as fallback.
- **`api/dependencies.py`** — the deterministic stealth fingerprint profile is now
  cached per account, instead of regenerated on every request.

### Cleanup (Phase 0)

- `dependencies.py`: return annotations resolved via a `TYPE_CHECKING` import block.
- `main.py`: hoisted `import os` to module scope (removed inline `import os as _os`).
- `youtube.py`: removed dead imports and a placeholder-less f-string.
- `x.py`: removed now-unused imports after adopting the base class.
- `reddit.py`: removed unused `Union` import.
- All Reddit + YouTube routes: `Query(regex=...)` → `Query(pattern=...)`.
- Magic numbers in `reddit.py` and `youtube.py` wired to `api/config.py`.

---

## 4. Deliberately NOT changed (and why)

- **Async `curl_cffi` migration (L1)** — the single biggest throughput win, but it
  changes the network behavior of a working stealth system. Without live proxies +
  account sessions to confirm that `AsyncSession` preserves impersonation and proxy
  behavior, the regression risk outweighs the benefit for this pass.
- **Reddit no-backoff-on-failover (L2)**, **persistent X browser pool (L4)**,
  **LinkedIn background probe/cookie healing (L6)** — behavior-changing and best
  validated against live targets. Documented here as the recommended next phase.
- **`/health` endpoint** — it reports saved-session *file* counts rather than live
  session health. Left as-is to avoid changing an output shape that monitoring may
  depend on; flagged as a known limitation.

---

## 5. Verification

- `python -m pytest tests/` → **13 passed** (was 13; no regressions). The previous
  Pydantic `regex` deprecation warnings are gone.
- Import smoke test of every touched module (`api.config`, `api.services.proxy_base`,
  `api.dependencies`, `api.services.reddit`, `api.services.x`, `api.services.youtube`,
  `api.main`, and all Reddit/YouTube routes) → all import cleanly.
- `pyflakes` on every touched file → no warnings.
- `tests/test_subreddit_route.py` updated to set an explicit `API_KEY` (it
  previously relied on the removed hardcoded default).

> Note: these checks confirm the changes are import-clean, behavior-preserving for
> the covered paths, and pass the suite. They do **not** exercise live scraping —
> validate the YouTube parallel-fetch and startup key-warmup against real traffic
> before relying on them in production.

---

## 6. Recommended next steps (future phases)

1. **Phase 2** — Reddit: skip backoff when failing over to a different account/IP
   (only sleep when reusing the same one). X: hold a persistent browser/context
   pool instead of launching+closing per request.
2. **Phase 3** — migrate all HTTP scrapers to `curl_cffi` `AsyncSession`; move
   LinkedIn proxy vetting and cookie healing fully into background tasks.
3. Make `/health` reflect real session liveness.
4. Consider a `.env.example` documenting every config key in `api/config.py`.
