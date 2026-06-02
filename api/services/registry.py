"""
api/services/registry.py — Account registry for tracking account health,
cooldown periods, and triggering automated Playwright re-logins.
"""

from __future__ import annotations

import os
import sys
import time
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List
import json
import structlog

# Fix path to import core/tools modules correctly
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multi_account_login import login_account

logger = structlog.get_logger(__name__)
SESSION_MAX_AGE_SECONDS = int(os.getenv("REDDIT_SESSION_MAX_AGE_SECONDS", str(24 * 60 * 60)))

class AccountState:
    def __init__(self, account_id: str, username: str, proxy_url: Optional[str] = None):
        self.account_id = account_id
        self.username = username
        self.proxy_url = proxy_url
        self.status = "healthy"  # "healthy", "cool_down", "needs_relogin"
        self.cool_down_until: Optional[float] = None
        self.lock = asyncio.Lock()  # Ensure only one login attempt happens at a time for this account
        self.ratelimit_remaining = 100
        self.ratelimit_reset_at: Optional[float] = None

    def is_healthy(self) -> bool:
        now = time.time()
        
        # 1. Standard cool_down check
        if self.status == "cool_down":
            if self.cool_down_until and now >= self.cool_down_until:
                self.status = "healthy"
                self.cool_down_until = None
                return True
            return False
            
        # 2. Proactive rate limit exhaustion check
        if self.ratelimit_remaining <= 1:
            if self.ratelimit_reset_at:
                if now < self.ratelimit_reset_at:
                    return False
                else:
                    # Cooldown window has naturally expired
                    self.ratelimit_remaining = 100
                    self.ratelimit_reset_at = None
                    return True
            else:
                return True
                
        return self.status == "healthy"

    def time_remaining_cooldown(self) -> float:
        now = time.time()
        
        # Standard cool_down
        if self.status == "cool_down" and self.cool_down_until:
            return max(0.0, self.cool_down_until - now)
            
        # Proactive rate limit reset cooldown
        if self.ratelimit_remaining <= 1 and self.ratelimit_reset_at:
            return max(0.0, self.ratelimit_reset_at - now)
            
        return 0.0

class AccountRegistry:
    def __init__(self):
        self.states: Dict[str, AccountState] = {}
        self._rotation_index = 0
        self._sync_lock = asyncio.Lock()
        self.initialize_registry()

    def initialize_registry(self):
        """Initialise account states from .env and sessions directory."""
        from api.dependencies import parse_accounts_from_env, get_available_session_accounts

        accounts_info = parse_accounts_from_env()
        available_sessions = get_available_session_accounts()

        for acc in accounts_info:
            account_id = acc["account_id"]
            if account_id in available_sessions:
                self.states[account_id] = AccountState(
                    account_id=account_id,
                    username=acc["username"],
                    proxy_url=acc.get("proxy_url")
                )
        
        logger.info(
            "account_registry_initialized",
            configured_accounts=len(accounts_info),
            active_sessions=len(available_sessions),
            registered_states=list(self.states.keys())
        )

    def get_account_state(self, account_id: str) -> Optional[AccountState]:
        return self.states.get(account_id)

    def check_proactive_expiry(self, account_id: str):
        """Mark sessions for relogin when token expiry or saved-session age requires it."""
        state = self.states.get(account_id)
        if not state or state.status == "needs_relogin":
            return
            
        from api.dependencies import find_session_file
        session_file = find_session_file(account_id)
        if not session_file:
            state.status = "needs_relogin"
            logger.warning("session_file_missing_relogin_required", account_id=account_id)
            return
            
        try:
            now = time.time()
            session_age_seconds = max(0.0, now - session_file.stat().st_mtime)
            if session_age_seconds >= SESSION_MAX_AGE_SECONDS:
                state.status = "needs_relogin"
                logger.info(
                    "proactive_session_age_relogin_required",
                    account_id=account_id,
                    session_file=str(session_file),
                    age_seconds=round(session_age_seconds, 1),
                    max_age_seconds=SESSION_MAX_AGE_SECONDS,
                    saved_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session_file.stat().st_mtime))
                )
                return

            with open(session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for cookie in data.get("cookies", []):
                if cookie.get("name") == "token_v2":
                    expires = cookie.get("expires", 0)
                    # If expired or expiring within 5 minutes (300 seconds)
                    if expires and now >= expires - 300:
                        logger.info(
                            "proactive_token_expiry_detected",
                            account_id=account_id,
                            expires=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expires))
                        )
                        state.status = "needs_relogin"
        except Exception as e:
            logger.error("error_checking_proactive_expiry", account_id=account_id, error=str(e))

    async def get_next_healthy_account(self, requested_account_id: Optional[str] = None) -> str:
        """
        Get the next healthy account ID.
        If a specific account_id is requested, returns it (or triggers login if needed).
        If no account is specified, rotates through healthy ones.
        """
        async with self._sync_lock:
            # 1. Handle explicit requested account
            if requested_account_id:
                state = self.states.get(requested_account_id)
                if not state:
                    # Attempt to register dynamically if it exists in available sessions
                    from api.dependencies import get_available_session_accounts

                    available_sessions = get_available_session_accounts()
                    if requested_account_id in available_sessions:
                        self.initialize_registry()
                        state = self.states.get(requested_account_id)
                    
                    if not state:
                        raise ValueError(f"Requested account '{requested_account_id}' is not configured or has no active session.")
                
                # Check proactive expiry first
                self.check_proactive_expiry(requested_account_id)

                # Check status
                if state.status == "cool_down":
                    remaining = state.time_remaining_cooldown()
                    if remaining > 0:
                        logger.warning("requested_account_in_cooldown", account_id=requested_account_id, remaining=remaining)
                        raise RuntimeError(f"Account '{requested_account_id}' is cooling down for another {round(remaining, 1)} seconds.")
                    else:
                        state.status = "healthy"
                
                return requested_account_id

            # 2. Dynamic Selection (Rotation over healthy accounts)
            available_ids = list(self.states.keys())
            if not available_ids:
                raise RuntimeError("No active Reddit accounts registered in the service.")

            # Run proactive expiry check on all accounts
            for aid in available_ids:
                self.check_proactive_expiry(aid)

            # Filter healthy ones (needs_relogin accounts are considered healthy here
            # because we trigger re-login right before executing the request in the scraper)
            healthy_ids = [aid for aid in available_ids if self.states[aid].is_healthy() or self.states[aid].status == "needs_relogin"]
            
            if not healthy_ids:
                # Fallback: find the account with the shortest cool down time
                cooldowns = {aid: self.states[aid].time_remaining_cooldown() for aid in available_ids}
                best_account = min(cooldowns, key=cooldowns.get) # type: ignore
                shortest_cooldown = cooldowns[best_account]
                
                logger.error(
                    "all_accounts_exhausted",
                    cooldowns=cooldowns,
                    best_fallback=best_account,
                    wait_seconds=round(shortest_cooldown, 1)
                )
                
                raise RuntimeError(
                    f"All Reddit accounts are rate-limited or blocked. "
                    f"Nearest account is '{best_account}', cooling down for {round(shortest_cooldown, 1)} seconds."
                )

            # Round-robin selection
            selected = healthy_ids[self._rotation_index % len(healthy_ids)]
            self._rotation_index += 1
            return selected

    def cool_down_account(self, account_id: str, duration_seconds: int = 300):
        """Put an account on cool-down (e.g. on 429 or 403 errors)."""
        state = self.states.get(account_id)
        if state:
            state.status = "cool_down"
            state.cool_down_until = time.time() + duration_seconds
            logger.warn(
                "account_cooldown_activated",
                account_id=account_id,
                duration_seconds=duration_seconds,
                until=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state.cool_down_until))
            )

    def update_account_limits(self, account_id: str, remaining: int, reset_seconds: int):
        """Update ratelimit remaining and reset timestamp dynamically from headers."""
        state = self.states.get(account_id)
        if state:
            state.ratelimit_remaining = remaining
            state.ratelimit_reset_at = time.time() + reset_seconds
            logger.info(
                "account_ratelimit_updated",
                account_id=account_id,
                remaining=remaining,
                reset_seconds=reset_seconds,
                reset_at=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state.ratelimit_reset_at))
            )

    def flag_relogin_needed(self, account_id: str):
        """Mark an account as needing re-login (e.g. on 401 Unauthorized errors)."""
        state = self.states.get(account_id)
        if state:
            state.status = "needs_relogin"
            logger.warn("account_flagged_for_relogin", account_id=account_id)

    async def trigger_relogin(self, account_id: str) -> bool:
        """Trigger Playwright automated re-login flow to refresh token_v2."""
        state = self.states.get(account_id)
        if not state:
            return False

        async with state.lock:
            # Check if another thread/request already resolved it
            if state.status == "healthy":
                self.check_proactive_expiry(account_id)
                if state.status == "healthy":
                    logger.info("relogin_skipped_already_healthy", account_id=account_id)
                    return True

            logger.info("relogin_triggered_for_account", account_id=account_id, username=state.username)

            # Resolve credentials
            from api.dependencies import parse_accounts_from_env

            accounts_info = parse_accounts_from_env()
            target_acc = None
            for acc in accounts_info:
                if acc["account_id"] == account_id:
                    target_acc = acc
                    break

            if not target_acc:
                logger.error("relogin_failed_credentials_missing", account_id=account_id)
                return False

            # Captcha resolver configuration
            captcha_provider = os.getenv("CAPTCHA_PROVIDER")
            captcha_api_key = os.getenv("CAPTCHA_API_KEY")
            captcha_config = None
            if captcha_provider and captcha_api_key:
                captcha_config = {
                    "provider": captcha_provider.strip(),
                    "api_key": captcha_api_key.strip()
                }

            # Run Playwright login flow asynchronously
            try:
                success = await login_account(target_acc, captcha_config, headless=True)
                if success:
                    state.status = "healthy"
                    state.cool_down_until = None
                    logger.info("relogin_completed_successfully", account_id=account_id)
                    return True
                else:
                    # Login failed, cooldown account to avoid lockout
                    self.cool_down_account(account_id, duration_seconds=600)  # cooldown for 10 minutes
                    logger.error("relogin_flow_failed", account_id=account_id)
                    return False
            except Exception as e:
                self.cool_down_account(account_id, duration_seconds=600)
                logger.error("relogin_exception_occurred", account_id=account_id, error=str(e))
                return False
