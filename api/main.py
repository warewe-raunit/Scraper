"""
api/main.py — FastAPI application entry point.
Configures structured logging, routes, error handling, and documentation.
"""

from __future__ import annotations

import sys
import time
import logging
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

# 1. Setup Structured Logging with structlog
logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=logging.INFO,
)

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger("api_gateway")

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
)

# 3. Add CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
from api.routes import subreddits, posts, comments, users, youtube
from api.dependencies import verify_api_key

app.include_router(subreddits.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(posts.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(comments.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(users.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])
app.include_router(youtube.router, prefix="/api/v1", dependencies=[Depends(verify_api_key)])

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
    uvicorn.run("api.main:app", host="127.0.0.1", port=8000, reload=True)
