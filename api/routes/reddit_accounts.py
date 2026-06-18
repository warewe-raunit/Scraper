"""
api/routes/reddit_accounts.py — FastAPI router for Reddit account-pool health
and manual relogin control (mirrors the LinkedIn accounts endpoints).
"""

from __future__ import annotations

from fastapi import APIRouter
import structlog

from api.account_choices import RedditAccount
from api.dependencies import get_registry

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/reddit", tags=["Reddit"])


@router.get(
    "/accounts/status",
    summary="Reddit account pool health",
    description=(
        "Per-account session state + pool counters: status (healthy / cool_down / "
        "needs_relogin / no_session), cooldown remaining, rate-limit budget, "
        "session age, and whether a background relogin is in flight. Accounts "
        "configured in .env without a session yet appear as 'no_session'."
    ),
)
async def get_reddit_account_pool_status():
    return get_registry().snapshot()


@router.post(
    "/accounts/{account_id}/relogin",
    summary="Force a background relogin for one Reddit account",
    description=(
        "Flag an account for relogin and queue a background worker to refresh "
        "(or create) its session. Returns immediately; the relogin runs in the "
        "background. No-op queue=false if REDDIT_AUTO_RELOGIN is disabled."
    ),
)
async def reddit_force_relogin(account_id: RedditAccount):
    queued = await get_registry().force_relogin(str(account_id))
    return {"queued": queued, "account_id": str(account_id)}
