"""
api/main.py — FastAPI application entry point.
Configures structured logging, routes, error handling, and documentation.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, Response, status, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import structlog
from dotenv import load_dotenv

# Ensure root directory is in python path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load environment variables
load_dotenv(override=True)

import asyncio
# On Windows the Proactor event loop (required for Playwright subprocesses) has
# been the default since Python 3.8, so we no longer set a policy explicitly —
# asyncio.set_event_loop_policy / WindowsProactorEventLoopPolicy are deprecated
# in Python 3.14+. The __main__ block below pins the Proactor loop directly.


# 1. Configure logging — structlog stays the API, Loguru is the sink/renderer.
#    (Pretty colored stdout + rotating compressed JSON file; LOG_LEVEL/LOG_FORMAT
#    env vars still apply.) See tools/logging_config.py.
from tools.logging_config import configure_logging
configure_logging()

logger = structlog.get_logger("api_gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks (modern replacement for @app.on_event)."""
    # --- startup ---
    # Start the continuous proxy-health daemon so the pool is already stocked
    # with verified-live proxies before the first request. Non-blocking; the
    # daemon runs in its own thread and self-heals.
    try:
        from tools.proxy_provider import get_proxy_provider
        provider = get_proxy_provider()
        if provider.is_enabled():
            provider.start_health_loop()  # idempotent
            logger.info("proxy_pool_warmup_dispatched", target_live=provider.target_live)
    except Exception as e:
        logger.warning("proxy_pool_warmup_failed", error=str(e))

    # Trigger background relogin for any LinkedIn accounts that start DEAD.
    # Doesn't block — the AccountPool dispatches asyncio tasks.
    try:
        from api.services.linkedin_account_pool import LinkedInAccountPool
        pool = await LinkedInAccountPool.instance()
        await pool.warmup()
        logger.info("linkedin_pool_warmup_dispatched", **pool.snapshot()["counters"])
    except Exception as e:
        logger.warning("linkedin_pool_warmup_failed", error=str(e))

    # Health-validate every LinkedIn account so only sessions that ACTUALLY work
    # serve traffic — a saved session file (li_at present) can still be dead
    # (302→login). Unhealthy ones are marked DEAD + relogged in the background.
    # Dispatched as a task so server startup isn't blocked by the probes; the
    # pool also self-heals on real-request signals while this runs. If
    # LINKEDIN_HEALTH_CHECK_INTERVAL > 0, the sweep repeats on that interval.
    if os.getenv("LINKEDIN_VALIDATE_ON_STARTUP", "true").lower() in ("1", "true", "yes", "on"):
        async def _account_health_sweep():
            try:
                from api.services.linkedin import LinkedInScraperService
                svc = LinkedInScraperService()
                await svc.validate_all_accounts()
                interval = int(os.getenv("LINKEDIN_HEALTH_CHECK_INTERVAL", "0"))
                while interval > 0:
                    await asyncio.sleep(interval)
                    await svc.validate_all_accounts()
            except Exception as e:
                logger.warning("linkedin_account_health_sweep_failed", error=str(e))
        asyncio.create_task(_account_health_sweep())
        logger.info("linkedin_account_health_sweep_dispatched")

    # Keep every Reddit account's session ALIVE proactively (not just when a
    # request hits a 401): sweep all configured accounts at startup and on an
    # interval, relogging any whose session is missing, aged out, or whose token
    # is expiring — in the background, bounded by REDDIT_RELOGIN_CONCURRENCY.
    # Gated by REDDIT_AUTO_RELOGIN (the actual relogin) + REDDIT_VALIDATE_ON_STARTUP.
    if os.getenv("REDDIT_VALIDATE_ON_STARTUP", "true").lower() in ("1", "true", "yes", "on"):
        async def _reddit_account_health_loop():
            try:
                from api.dependencies import get_registry
                await get_registry().run_health_loop()
            except Exception as e:
                logger.warning("reddit_account_health_loop_failed", error=str(e))
        asyncio.create_task(_reddit_account_health_loop())
        logger.info("reddit_account_health_loop_dispatched")

    # Warm the YouTube InnerTube API key in the background so the FIRST video
    # request doesn't pay the (potentially Playwright-backed) key-extraction
    # cost inline. Non-blocking; falls back to lazy extraction if it fails.
    if os.getenv("YOUTUBE_WARMUP_ON_STARTUP", "true").lower() in ("1", "true", "yes", "on"):
        async def _warm_youtube_key():
            try:
                from api.dependencies import get_youtube_scraper_service, get_database_service
                svc = get_youtube_scraper_service(get_database_service())
                await svc._get_innertube_key()
                logger.info("youtube_innertube_key_warmed")
            except Exception as e:
                logger.warning("youtube_key_warmup_failed", error=str(e))
        asyncio.create_task(_warm_youtube_key())
        logger.info("youtube_key_warmup_dispatched")

    # Mint/refresh X (Nitter) instance tokens in the BACKGROUND — the same model
    # as the LinkedIn login runner: solve each instance's bot-check off the request
    # path, cache the token, and refresh before its ~50min TTL. Once warmed, X
    # requests use the cheap curl path and (with X_INLINE_BROWSER_SOLVE=false)
    # never launch a browser inline. Waits briefly first so the proxy pool is
    # stocked before the solves run.
    if os.getenv("X_WARMUP_ON_STARTUP", "true").lower() in ("1", "true", "yes", "on"):
        async def _x_token_warmer():
            try:
                from tools.unauth_x_scraper import warm_all_instances
                await asyncio.sleep(int(os.getenv("X_WARM_INITIAL_DELAY", "15")))
                interval = int(os.getenv("X_TOKEN_WARM_INTERVAL", "2400"))  # 40 min
                while True:
                    res = await warm_all_instances()
                    logger.info("x_token_warm_complete",
                                warmed=sum(1 for v in res.values() if v), total=len(res))
                    if interval <= 0:
                        break
                    await asyncio.sleep(interval)
            except Exception as e:
                logger.warning("x_token_warm_failed", error=str(e))
        asyncio.create_task(_x_token_warmer())
        logger.info("x_token_warmer_dispatched")

    yield

    # --- shutdown ---
    try:
        from tools.proxy_provider import get_proxy_provider
        get_proxy_provider().stop_health_loop()
    except Exception:
        pass

    # Close any warm X scraper browsers so Chromium processes don't leak.
    try:
        from tools.unauth_x_scraper import _x_browser_pool
        await _x_browser_pool.close_all()
    except Exception:
        pass


# 2. Instantiate FastAPI App
app = FastAPI(
    title="Scraper API",
    description=(
        "A highly scalable, performant multi-platform scraper (Reddit, LinkedIn, "
        "YouTube, and X) that wraps official/public APIs using stealth browser "
        "session cookies and custom device client hints to avoid rate limiting "
        "and detection."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# 3. Add CORS Middleware
# Origins are configurable via CORS_ALLOW_ORIGINS (comma-separated). The default
# "*" stays permissive for ease of integration, but credentials are only allowed
# when an explicit origin list is configured — the CORS spec forbids combining
# allow_origins="*" with allow_credentials=True (browsers reject it outright).
def _parse_cors_origins() -> list[str]:
    raw = (os.getenv("CORS_ALLOW_ORIGINS") or "*").strip()
    if not raw:
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]

_cors_origins = _parse_cors_origins()
_cors_allow_all = "*" in _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _cors_allow_all else _cors_origins,
    allow_credentials=not _cors_allow_all,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 4. Request Logging and Timing Middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", "")
    
    # Bind request context parameters to logs
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        ip=request.client.host if request.client else "unknown",
    )
    
    logger.info("request_started")
    
    try:
        response: Response = await call_next(request)
        process_time = time.perf_counter() - start_time
        logger.info(
            "request_completed",
            status_code=response.status_code,
            duration_seconds=round(process_time, 4),
        )
        response.headers["x-process-time"] = str(process_time)
        return response
    except Exception as e:
        process_time = time.perf_counter() - start_time
        logger.exception(
            "request_failed",
            error=str(e),
            duration_seconds=round(process_time, 4),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An unexpected error occurred during request processing."}
        )

# 5. Register Routes and Routers
from api.routes import subreddits, posts, comments, users, youtube, x, linkedin, reddit_accounts
from api.dependencies import verify_api_key

app.include_router(subreddits.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(reddit_accounts.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(posts.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(comments.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(users.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(youtube.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(x.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(linkedin.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])

@app.get("/", include_in_schema=False)
def index_redirect():
    """Redirect root access to API docs."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")

@app.get("/health", tags=["System"], summary="API health status check")
def health_check():
    """Check API server health status and account session status."""
    from api.dependencies import get_available_session_accounts
    
    try:
        sessions = get_available_session_accounts()
        return {
            "status": "healthy",
            "active_session_count": len(sessions),
            "available_accounts": sessions,
        }
    except Exception as e:
        logger.error("health_check_failed", error=str(e))
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "error": str(e)}
        )

def proactor_loop_factory(use_subprocess: bool = False) -> "asyncio.AbstractEventLoop":
    """uvicorn event-loop factory that pins the Proactor loop on Windows.

    uvicorn 0.40 hands subprocess/reload workers a ``SelectorEventLoop``
    (use_subprocess=True), but Playwright — and any asyncio subprocess — needs
    the Proactor loop on Windows; a SelectorEventLoop makes browser/relogin
    launches fail with NotImplementedError. uvicorn resolves this factory per
    worker (before the loop is created), so forcing Proactor here keeps
    ``--reload`` working without breaking Chromium. (This factory is only wired
    in on win32; other platforms keep uvicorn's default 'auto'.)
    """
    return asyncio.ProactorEventLoop()


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8000"))
    reload = os.getenv("API_RELOAD", "true").strip().lower() in ("1", "true", "yes", "on")

    # Go through uvicorn.run so the auto-reload supervisor is active. (The old
    # Windows branch built a Server and called server.serve() directly, which
    # silently disabled --reload — the reloader lives in the supervisor layer.)
    # On Windows we MUST force the Proactor loop via a custom loop factory,
    # because under --reload uvicorn would otherwise pick SelectorEventLoop in
    # the worker and break Playwright. Watch only source dirs so reloads aren't
    # triggered by sessions/, downloads/, or logs.
    loop = "api.main:proactor_loop_factory" if sys.platform == "win32" else "auto"
    uvicorn.run(
        "api.main:app",
        host=host,
        port=port,
        reload=reload,
        reload_dirs=[str(ROOT / "api"), str(ROOT / "tools")] if reload else None,
        loop=loop,
    )
