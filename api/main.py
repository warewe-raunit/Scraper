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

    yield

    # --- shutdown ---
    try:
        from tools.proxy_provider import get_proxy_provider
        get_proxy_provider().stop_health_loop()
    except Exception:
        pass


# 2. Instantiate FastAPI App
app = FastAPI(
    title="Reddit Stealth API Scraper",
    description=(
        "A highly scalable, performant Reddit scraper that intercepts and wraps "
        "official Reddit OAuth APIs using stealth browser session cookies and "
        "custom device client hints to avoid rate limiting and detection."
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
from api.routes import subreddits, posts, comments, users, youtube, x, linkedin
from api.dependencies import verify_api_key

app.include_router(subreddits.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
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

if __name__ == "__main__":
    import uvicorn
    
    if sys.platform == "win32":
        # Playwright subprocesses require the Proactor event loop on Windows.
        # It's the 3.8+ default, but pin it explicitly via the non-deprecated
        # API (ProactorEventLoop + set_event_loop) so we don't rely on the
        # default and don't touch the deprecated event-loop *policy* API.
        config = uvicorn.Config("api.main:app", host="127.0.0.1", port=8000, reload=True, loop="asyncio")
        server = uvicorn.Server(config)
        loop = asyncio.ProactorEventLoop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(server.serve())
        finally:
            loop.close()
    else:
        uvicorn.run("api.main:app", host="127.0.0.1", port=8000, reload=True)
