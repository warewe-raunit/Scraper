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
from tools.rotation import CooldownPool

logger = structlog.get_logger(__name__)
SESSION_MAX_AGE_SECONDS = int(os.getenv("REDDIT_SESSION_MAX_AGE_SECONDS", str(24 * 60 * 60)))

class AccountState:
    def __init__(
        self,
        account_id: str,
        username: str,
        proxy_url: Optional[str] = None,
        pool: Optional[CooldownPool] = None,
    ):
        self.account_id = account_id
        self.username = username
        self.proxy_url = proxy_url
        self.status = "healthy"  # "healthy", "cool_down", "needs_relogin"
        # The cool-down timer is owned by the shared CooldownPool (one cooldown
        # implementation across proxies and accounts). `pool` is injected by the
        # registry; is_healthy/time_remaining_cooldown read it for this account.
        self.pool = pool
        self.lock = asyncio.Lock()  # Ensure only one login attempt happens at a time for this account
        self.ratelimit_remaining = 100
        self.ratelimit_reset_at: Optional[float] = None
        self.ledger = None

    def _cooldown_active(self, now: float) -> bool:
        """Whether the shared pool still has this account resting."""
        if self.pool is None:
            return False
        return not self.pool.is_healthy(self.account_id, now)

    def is_healthy(self) -> bool:
        now = time.time()

        # 1. Standard cool_down check (timer lives in the shared CooldownPool)
        if self.status == "cool_down":
            if not self._cooldown_active(now):
                self.status = "healthy"
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

        # Standard cool_down (timer lives in the shared CooldownPool)
        if self.status == "cool_down" and self.pool is not None:
            remaining = self.pool.time_remaining(self.account_id, now)
            if remaining > 0:
                return remaining

        # Proactive rate limit reset cooldown
        if self.ratelimit_remaining <= 1 and self.ratelimit_reset_at:
            return max(0.0, self.ratelimit_reset_at - now)

        return 0.0

class AccountRegistry:
    def __init__(self):
        self.states: Dict[str, AccountState] = {}
        # Shared rotation + cooldown engine for accounts (same class the X and
        # YouTube proxy pools use). Created once and reused across re-inits so
        # cooldown timers survive dynamic re-registration.
        self._pool = CooldownPool(label="reddit_account", default_cooldown=300, redact=str)
        self._sync_lock = asyncio.Lock()

        # Background relogin orchestration (mirrors the LinkedIn account pool):
        # a bounded set of workers keeps every account's session ALIVE
        # proactively — independent of user traffic — so a request never has to
        # wait on a fresh login. Each relogin spins a Playwright browser, so the
        # concurrency is small by default.
        self._relogin_sem = asyncio.Semaphore(int(os.getenv("REDDIT_RELOGIN_CONCURRENCY", "2")))
        self._relogin_tasks: Dict[str, asyncio.Task] = {}

        # Ban state tracking configuration
        self.ban_detection_enabled = os.getenv("ACCOUNT_BAN_DETECTION", "true").lower() in ("1", "true", "yes", "on")
        self.ban_protect_enabled = os.getenv("ACCOUNT_BAN_PROTECT", "false").lower() in ("1", "true", "yes", "on")
        self.ban_probe_interval = int(os.getenv("ACCOUNT_BAN_PROBE_INTERVAL", "3"))
        self.ban_risk_warn = float(os.getenv("BAN_RISK_WARN", "6.0"))
        self.ban_risk_suspect = float(os.getenv("BAN_RISK_SUSPECT", "10.0"))
        self.ban_risk_decay = float(os.getenv("BAN_RISK_DECAY", "0.85"))
        self.ban_forbidden_streak = int(os.getenv("BAN_FORBIDDEN_STREAK", "4"))
        self._sweep_count = 0

        from tools import ban_state_store
        self.ledgers = ban_state_store.load() if self.ban_detection_enabled else {}

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
                    proxy_url=acc.get("proxy_url"),
                    pool=self._pool,
                )
                if self.ban_detection_enabled:
                    if account_id not in self.ledgers:
                        from tools.account_risk import RiskLedger
                        self.ledgers[account_id] = RiskLedger(
                            account_id=account_id,
                            platform="reddit",
                            decay=self.ban_risk_decay,
                            warn_threshold=self.ban_risk_warn,
                            suspect_threshold=self.ban_risk_suspect,
                            consecutive_forbidden_threshold=self.ban_forbidden_streak,
                        )
                    self.states[account_id].ledger = self.ledgers[account_id]

        # Register account ids with the shared pool (preserves existing
        # cooldown timers for accounts that survive a dynamic re-init).
        self._pool.set_items(list(self.states.keys()))

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
                
                # Check ban quarantine in protect mode
                if self.ban_detection_enabled and self.ban_protect_enabled:
                    ledger = self.ledgers.get(requested_account_id)
                    if ledger and ledger.is_terminal():
                        raise RuntimeError(f"Requested account '{requested_account_id}' is quarantined due to ban/restriction.")

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
            
            # Exclude terminal ban states if ban protect is active
            if self.ban_detection_enabled and self.ban_protect_enabled:
                healthy_ids = [
                    aid for aid in healthy_ids
                    if not (self.ledgers.get(aid) and self.ledgers[aid].is_terminal())
                ]

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
                    f"All Reddit accounts are rate-limited or blocked (or quarantined). "
                    f"Nearest account is '{best_account}', cooling down for {round(shortest_cooldown, 1)} seconds."
                )

            # Demote 'at_risk' accounts to fallback-only in rotation when protect is active
            if self.ban_detection_enabled and self.ban_protect_enabled:
                non_at_risk_ids = [
                    aid for aid in healthy_ids
                    if not (self.ledgers.get(aid) and self.ledgers[aid].is_at_risk())
                ]
                rotation_candidates = non_at_risk_ids if non_at_risk_ids else healthy_ids
            else:
                rotation_candidates = healthy_ids

            # Round-robin selection via the shared CooldownPool (restricted to the
            # candidates we just filtered; all are cooldown-clear by definition).
            selected = self._pool.get_next(candidates=rotation_candidates, fallback_to_shortest=False)
            if selected is None:
                # Defensive: rotation_candidates is non-empty here, so fall back to its head.
                selected = rotation_candidates[0]
            return selected

    def cool_down_account(self, account_id: str, duration_seconds: int = 300):
        """Put an account on cool-down (e.g. on 429 or 403 errors)."""
        state = self.states.get(account_id)
        if state:
            state.status = "cool_down"
            self._pool.cool_down(account_id, duration_seconds)
            logger.warn(
                "account_cooldown_activated",
                account_id=account_id,
                duration_seconds=duration_seconds,
                until=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + duration_seconds))
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

            # Run Playwright login flow asynchronously. Headful by default so
            # interactive challenges (CAPTCHA / verification) can be solved in a
            # visible window; set REDDIT_RELOGIN_HEADLESS=true for unattended
            # server runs. Falls back to BROWSER_HEADLESS when unset.
            relogin_headless = os.getenv(
                "REDDIT_RELOGIN_HEADLESS",
                os.getenv("BROWSER_HEADLESS", "false"),
            ).lower() in ("1", "true", "yes", "on")
            try:
                success = await login_account(target_acc, captcha_config, headless=relogin_headless)
                if success:
                    state.status = "healthy"
                    self._pool.clear(account_id)  # lift any active cooldown timer
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

    # --------------------------------------------------- health + auto-relogin

    def snapshot(self) -> dict:
        """Per-account health + pool counters (for the accounts/status endpoint).

        Includes accounts configured in .env that have no session yet (status
        "no_session") so ops can see which accounts still need a first login.
        """
        from api.dependencies import parse_accounts_from_env, find_session_file

        now = time.time()
        configured = {a["account_id"] for a in parse_accounts_from_env()}
        all_ids = sorted(set(self.states.keys()) | configured)

        accounts: List[Dict[str, Any]] = []
        for aid in all_ids:
            st = self.states.get(aid)
            sf = find_session_file(aid)
            session_age = round(now - sf.stat().st_mtime, 1) if sf else None
            relogging = aid in self._relogin_tasks and not self._relogin_tasks[aid].done()

            # Ban State metrics
            ban_state = "clear"
            risk_score = 0.0
            last_signal = None
            flagged_at = None
            if self.ban_detection_enabled:
                ledger = self.ledgers.get(aid)
                if ledger:
                    ban_state = ledger.ban_state
                    risk_score = round(ledger.risk_score, 1)
                    last_signal = ledger.last_signals[-1][1] if ledger.last_signals else None
                    flagged_at = ledger.flagged_at

            row = {
                "account_id": aid,
                "status": st.status if st else "no_session",
                "healthy": st.is_healthy() if st else False,
                "cooldown_remaining": round(st.time_remaining_cooldown(), 1) if st else 0.0,
                "ratelimit_remaining": st.ratelimit_remaining if st else None,
                "has_session": sf is not None if st else False,
                "session_age_seconds": session_age,
                "relogging": relogging,
                "ban_state": ban_state,
                "risk_score": risk_score,
                "last_signal": last_signal,
                "flagged_at": flagged_at,
            }
            accounts.append(row)

        counters = {
            "configured": len(configured),
            "registered": len(self.states),
            "healthy": sum(1 for a in accounts if a["healthy"]),
            "needs_relogin": sum(1 for a in accounts if a["status"] == "needs_relogin"),
            "cool_down": sum(1 for a in accounts if a["status"] == "cool_down"),
            "no_session": sum(1 for a in accounts if a["status"] == "no_session"),
            "relogging": sum(1 for a in accounts if a["relogging"]),
        }
        flagged_list = []
        if self.ban_detection_enabled:
            for aid, ledger in self.ledgers.items():
                if ledger.ban_state != "clear":
                    flagged_list.append(aid)

        return {"accounts": accounts, "counters": counters, "flagged": flagged_list}

    async def force_relogin(self, account_id: str) -> bool:
        """External trigger: flag an account for relogin and queue a background
        worker. Returns True if a relogin was queued (or already running)."""
        state = self.states.get(account_id)
        if state is not None:
            state.status = "needs_relogin"
        return self._schedule_relogin(account_id)

    def _schedule_relogin(self, account_id: str) -> bool:
        """Queue a deduplicated background relogin worker for one account.

        Gated by REDDIT_AUTO_RELOGIN (default on); when off the pool never
        launches a browser to re-login (saved-sessions-only mode). Returns
        whether a worker is queued/running for this account.
        """
        if os.getenv("REDDIT_AUTO_RELOGIN", "true").lower() not in ("1", "true", "yes", "on"):
            logger.info("reddit_relogin_skipped_disabled", account_id=account_id)
            return False
        existing = self._relogin_tasks.get(account_id)
        if existing is not None and not existing.done():
            return True  # already in flight
        self._relogin_tasks[account_id] = asyncio.create_task(self._run_relogin_once(account_id))
        return True

    async def _run_relogin_once(self, account_id: str) -> bool:
        """Background relogin worker: refresh (or create) the session for one
        account. Bounded by _relogin_sem; serialized against the request-path
        relogin via the per-account lock. Works even when the account has no
        prior state (first-time login) — on success it registers the account."""
        from api.dependencies import parse_accounts_from_env

        target_acc = next(
            (a for a in parse_accounts_from_env() if a["account_id"] == account_id), None
        )
        if not target_acc:
            logger.error("reddit_bg_relogin_credentials_missing", account_id=account_id)
            return False

        captcha_provider = os.getenv("CAPTCHA_PROVIDER")
        captcha_api_key = os.getenv("CAPTCHA_API_KEY")
        captcha_config = None
        if captcha_provider and captcha_api_key:
            captcha_config = {"provider": captcha_provider.strip(), "api_key": captcha_api_key.strip()}

        # Headful by default so interactive challenges can be solved in a visible
        # window; set REDDIT_RELOGIN_HEADLESS=true for unattended/server runs.
        headless = os.getenv(
            "REDDIT_RELOGIN_HEADLESS", os.getenv("BROWSER_HEADLESS", "false")
        ).lower() in ("1", "true", "yes", "on")

        async with self._relogin_sem:
            state = self.states.get(account_id)
            # Serialize with the request-path trigger_relogin for this account.
            lock = state.lock if state is not None else None
            if lock is not None:
                await lock.acquire()
            try:
                # Another worker/request may have healed it while we waited.
                if state is not None and state.status == "healthy":
                    self.check_proactive_expiry(account_id)
                    if state.status == "healthy":
                        logger.info("reddit_bg_relogin_skipped_already_healthy", account_id=account_id)
                        return True

                logger.info("reddit_bg_relogin_start", account_id=account_id, username=target_acc.get("username"))
                try:
                    success = await login_account(target_acc, captcha_config, headless=headless)
                except Exception as e:
                    logger.error("reddit_bg_relogin_exception", account_id=account_id, error=str(e)[:200])
                    success = False
            finally:
                if lock is not None:
                    lock.release()

        if success:
            # Register the (possibly brand-new) session so it starts serving.
            if account_id not in self.states:
                self.initialize_registry()
            st = self.states.get(account_id)
            if st is not None:
                st.status = "healthy"
                self._pool.clear(account_id)  # lift any active cooldown timer
            logger.info("reddit_bg_relogin_success", account_id=account_id)
            return True

        # Cooldown a known account so the sweeper doesn't hammer a failing login.
        if account_id in self.states:
            self.cool_down_account(account_id, duration_seconds=600)
        logger.warning("reddit_bg_relogin_failed", account_id=account_id)
        return False

    def record_signal(self, account_id: str, signal: str):
        """Record one risk signal for an account."""
        if not self.ban_detection_enabled:
            return

        ledger = self.ledgers.get(account_id)
        if not ledger:
            # Create a new ledger if missing
            from tools.account_risk import RiskLedger
            ledger = RiskLedger(
                account_id=account_id,
                platform="reddit",
                decay=self.ban_risk_decay,
                warn_threshold=self.ban_risk_warn,
                suspect_threshold=self.ban_risk_suspect,
                consecutive_forbidden_threshold=self.ban_forbidden_streak,
            )
            self.ledgers[account_id] = ledger

        old_state = ledger.ban_state
        ledger.record(signal)
        
        # Save change
        from tools import ban_state_store
        ban_state_store.save(self.ledgers)

        new_state = ledger.ban_state
        if old_state != new_state:
            if new_state == "shadow_suspected":
                logger.warning(
                    "account.ban_suspected",
                    platform="reddit",
                    account_id=account_id,
                    risk_score=round(ledger.risk_score, 1),
                    recent_signals=ledger.last_signals,
                )
            elif new_state in ("shadow_confirmed", "suspended", "restricted"):
                logger.error(
                    f"account.{new_state}",
                    platform="reddit",
                    account_id=account_id,
                    risk_score=round(ledger.risk_score, 1),
                )
            elif new_state == "clear":
                logger.info(
                    "account.ban_cleared",
                    platform="reddit",
                    account_id=account_id,
                )

    async def probe_ban(self, account_id: str) -> str:
        """
        Probe Reddit account health. Returns one of:
        "suspended" | "shadow_confirmed" | "clear" | "inconclusive"
        """
        if not self.ban_detection_enabled:
            return "inconclusive"
            
        username = None
        state = self.states.get(account_id)
        if state:
            username = state.username
        
        if not username:
            # Fallback credentials lookup
            from api.dependencies import parse_accounts_from_env
            for acc in parse_accounts_from_env():
                if acc["account_id"] == account_id:
                    username = acc["username"]
                    break
                    
        if not username:
            logger.error("reddit_probe.username_not_found", account_id=account_id)
            return "inconclusive"

        logger.info("reddit_probe.start", account_id=account_id, username=username)

        try:
            # 1. Logged-in check: user about
            from api.dependencies import create_stealth_client
            session = await create_stealth_client(account_id)
            
            loop = asyncio.get_running_loop()
            url_in = f"https://oauth.reddit.com/user/{username}/about"
            
            resp_in = await loop.run_in_executor(
                None,
                lambda: session.get(url_in, impersonate="chrome120", timeout=10)
            )
            
            if resp_in.status_code != 200:
                logger.warning("reddit_probe.logged_in_failed", account_id=account_id, status=resp_in.status_code)
                return "inconclusive"
                
            data_in = resp_in.json()
            is_suspended = data_in.get("data", {}).get("is_suspended")
            if is_suspended is True:
                logger.error("reddit_probe.suspended_detected", account_id=account_id)
                return "suspended"
                
            # 2. Logged-out shadowban check
            from tools.proxy_provider import get_proxy_provider
            from curl_cffi import requests as cffi_requests
            
            gp = get_proxy_provider()
            proxy = gp.get_next() if gp.is_enabled() else None
            
            unauth_session = cffi_requests.Session()
            if proxy:
                unauth_session.proxies = {"http": proxy, "https": proxy}
                
            url_out = f"https://www.reddit.com/user/{username}/about.json"
            resp_out = await loop.run_in_executor(
                None,
                lambda: unauth_session.get(url_out, impersonate="chrome120", timeout=10)
            )
            
            if resp_out.status_code == 404:
                logger.error("reddit_probe.shadowban_confirmed", account_id=account_id)
                return "shadow_confirmed"
            elif resp_out.status_code == 200:
                try:
                    data_out = resp_out.json()
                    if not data_out or "data" not in data_out:
                        logger.error("reddit_probe.shadowban_confirmed_empty_data", account_id=account_id)
                        return "shadow_confirmed"
                except Exception:
                    logger.error("reddit_probe.shadowban_confirmed_json_error", account_id=account_id)
                    return "shadow_confirmed"
                
                logger.info("reddit_probe.clear", account_id=account_id)
                return "clear"
            elif resp_out.status_code == 429:
                logger.warning("reddit_probe.logged_out_429", account_id=account_id)
                return "inconclusive"
            else:
                logger.warning("reddit_probe.logged_out_failed", account_id=account_id, status=resp_out.status_code)
                return "inconclusive"
                
        except Exception as e:
            logger.warning("reddit_probe.exception", account_id=account_id, error=str(e))
            return "inconclusive"

    async def sweep_and_relogin(self) -> dict:
        """One proactive health pass over ALL configured accounts: pick up new
        session files, flag expired/aged/missing sessions, and queue background
        relogins for them. Returns the post-sweep snapshot counters."""
        # Pick up any session files written since startup (e.g. by a prior sweep).
        self.initialize_registry()

        from api.dependencies import parse_accounts_from_env

        # Out-of-band active ban probe for suspects/scheduled passes
        if self.ban_detection_enabled:
            self._sweep_count += 1
            probe_tasks = []
            for acc in parse_accounts_from_env():
                aid = acc["account_id"]
                ledger = self.ledgers.get(aid)
                if not ledger:
                    continue
                should_probe = (
                    ledger.ban_state == "shadow_suspected"
                    or (self.ban_probe_interval > 0 and self._sweep_count % self.ban_probe_interval == 0)
                )
                if should_probe:
                    async def _probe_task_wrapper(account_id=aid, risk_ledger=ledger):
                        try:
                            verdict = await self.probe_ban(account_id)
                            old_state = risk_ledger.ban_state
                            applied = risk_ledger.confirm(verdict)
                            from tools import ban_state_store
                            ban_state_store.save(self.ledgers)

                            if not applied and verdict == "clear" and risk_ledger.is_terminal():
                                import time as _t
                                remaining = max(
                                    0.0,
                                    float(os.getenv("BAN_CONFIRMED_COOLDOWN_SECONDS", "86400"))
                                    - (_t.time() - (risk_ledger.flagged_at or _t.time())),
                                )
                                logger.info(
                                    "account.clear_blocked_post_ban_cooldown",
                                    platform="reddit",
                                    account_id=account_id,
                                    ban_state=risk_ledger.ban_state,
                                    cooldown_remaining_seconds=round(remaining, 1),
                                )

                            new_state = risk_ledger.ban_state
                            if old_state != new_state:
                                if new_state == "clear":
                                    logger.info("account.ban_cleared", platform="reddit", account_id=account_id)
                                elif new_state in ("shadow_confirmed", "suspended", "restricted"):
                                    logger.error(f"account.{new_state}", platform="reddit", account_id=account_id, risk_score=risk_ledger.risk_score)
                        except Exception as e:
                            logger.error("reddit_probe_failed_internal", account_id=account_id, error=str(e))
                    probe_tasks.append(_probe_task_wrapper())
            if probe_tasks:
                await asyncio.gather(*probe_tasks, return_exceptions=True)

        queued = 0
        for acc in parse_accounts_from_env():
            aid = acc["account_id"]
            state = self.states.get(aid)
            needs = False
            if state is None:
                needs = True  # configured but no usable session yet
            else:
                self.check_proactive_expiry(aid)
                needs = state.status == "needs_relogin"
            if needs and self._schedule_relogin(aid):
                queued += 1

        counters = self.snapshot()["counters"]
        logger.info("reddit_account_health_sweep", queued=queued, **counters)
        return counters

    async def run_health_loop(self) -> None:
        """Run an immediate startup sweep, then repeat on
        REDDIT_HEALTH_CHECK_INTERVAL (seconds; 0 disables the repeat). Keeps
        every account's session alive proactively, not just at query time."""
        try:
            await self.sweep_and_relogin()
        except Exception as e:
            logger.warning("reddit_account_health_sweep_failed", error=str(e))
        interval = int(os.getenv("REDDIT_HEALTH_CHECK_INTERVAL", "300"))
        while interval > 0:
            await asyncio.sleep(interval)
            try:
                await self.sweep_and_relogin()
            except Exception as e:
                logger.warning("reddit_account_health_sweep_failed", error=str(e))
