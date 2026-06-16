"""
api/services/linkedin_account_pool.py — Robust multi-account session pool for
LinkedIn scraping.

Designed to scale: with N accounts the pool serves requests round-robin, marks
accounts dying/dead on session-death signals, and kicks off background
relogins so other accounts keep serving traffic in the meantime.

Lifecycle states per account:
    ALIVE       — session valid, available to serve a request
    DYING       — got 1 inconclusive 302 (cookie merge); next 302 → DEAD
    DEAD        — session invalidated, queued for relogin
    RELOGGING   — relogin worker is currently running for this account
    DISABLED    — relogin failed too many times; backoff until reset_after

Concurrency:
    - acquire() / release() are protected by an asyncio.Lock
    - the relogin worker pool is bounded by a Semaphore
    - multiple Voyager calls may be in flight on DIFFERENT accounts at once
    - the same account is never handed to two concurrent callers
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import structlog

logger = structlog.get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]
SESSIONS_DIR = ROOT / "sessions"

ALIVE = "ALIVE"
DYING = "DYING"
DEAD = "DEAD"
RELOGGING = "RELOGGING"
DISABLED = "DISABLED"


@dataclass
class AccountState:
    account_id: str
    username: str
    password: str
    static_proxy: Optional[str]

    status: str = ALIVE
    in_use: bool = False
    consecutive_302: int = 0
    consecutive_proxy_exhaust: int = 0
    consecutive_relogin_failures: int = 0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    disabled_until: float = 0.0
    total_requests: int = 0
    total_successes: int = 0
    total_relogins: int = 0

    def snapshot(self) -> dict:
        return {
            "account_id": self.account_id,
            "status": self.status,
            "in_use": self.in_use,
            "consecutive_302": self.consecutive_302,
            "consecutive_proxy_exhaust": self.consecutive_proxy_exhaust,
            "consecutive_relogin_failures": self.consecutive_relogin_failures,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "disabled_until": self.disabled_until,
            "total_requests": self.total_requests,
            "total_successes": self.total_successes,
            "total_relogins": self.total_relogins,
        }


class NoAccountAvailable(Exception):
    """Raised when no account is currently available to serve a request."""


class LinkedInAccountPool:
    """Process-wide singleton orchestrating multi-account LinkedIn scraping."""

    _instance: Optional["LinkedInAccountPool"] = None
    _instance_lock = asyncio.Lock()

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self._accounts: Dict[str, AccountState] = {}
        self._index = 0
        self._lock = asyncio.Lock()

        # Bounded background relogin workers — each relogin spins a Playwright
        # browser, which is expensive. Default: 3 concurrent relogins.
        self._relogin_sem = asyncio.Semaphore(
            int(os.getenv("LINKEDIN_RELOGIN_CONCURRENCY", "3"))
        )
        self._relogin_tasks: Dict[str, asyncio.Task] = {}

        # Tunables
        self.session_death_threshold = int(os.getenv("LINKEDIN_SESSION_DEATH_AFTER_302", "2"))
        self.proxy_exhaust_relogin_threshold = int(
            os.getenv("LINKEDIN_RELOGIN_AFTER_PROXY_EXHAUST", "2"))
        self.disable_after_failures = int(os.getenv("LINKEDIN_DISABLE_AFTER_RELOGIN_FAILURES", "3"))
        self.disable_backoff_seconds = int(os.getenv("LINKEDIN_DISABLE_BACKOFF_SECONDS", "900"))
        self.acquire_wait_seconds = float(os.getenv("LINKEDIN_ACQUIRE_WAIT_SECONDS", "20"))
        # Background relogin runs HEADFUL by default so interactive challenges
        # (CAPTCHA / "security verification") can be solved in a visible window.
        # Set LINKEDIN_RELOGIN_HEADLESS=true for unattended/server runs (email-OTP
        # accounts still self-solve via the Gmail token). Falls back to
        # BROWSER_HEADLESS when LINKEDIN_RELOGIN_HEADLESS is unset.
        _relogin_headless = os.getenv(
            "LINKEDIN_RELOGIN_HEADLESS",
            os.getenv("BROWSER_HEADLESS", "false"),
        )
        self.relogin_headless = _relogin_headless.lower() in ("1", "true", "yes", "on")

        self._load_accounts_from_env()

    # ----------------------------------------------------------- public API

    @classmethod
    async def instance(cls) -> "LinkedInAccountPool":
        async with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    async def acquire(self) -> AccountState:
        """Reserve the next healthy account.

        Waits up to acquire_wait_seconds for one to become free. Raises
        NoAccountAvailable if every account is in use, dead, or disabled.
        """
        deadline = time.time() + self.acquire_wait_seconds
        while True:
            async with self._lock:
                acc = self._pick_next_alive_locked()
                if acc is not None:
                    acc.in_use = True
                    return acc
                # Reactivate disabled accounts whose backoff expired
                self._reactivate_disabled_locked()
            if time.time() >= deadline:
                raise NoAccountAvailable("No LinkedIn accounts available within wait window.")
            await asyncio.sleep(0.25)

    async def release(self, account_id: str, *, success: bool, session_redirected: bool,
                      proxies_exhausted: bool = False) -> None:
        """Return an account after a request.

        success: did the Voyager call return 200?
        session_redirected: did we get a 302 even after cookie merge?
            (signals that this account's session is dying/dead)
        proxies_exhausted: did every proxy this account could reach fail/reject?
            (signals its pinned proxy is dead — a relogin repins a fresh one)
        """
        relogin_needed = False
        async with self._lock:
            acc = self._accounts.get(account_id)
            if acc is None:
                return
            acc.in_use = False
            acc.total_requests += 1
            now = time.time()

            if success:
                acc.total_successes += 1
                acc.last_success_at = now
                acc.consecutive_302 = 0
                acc.consecutive_proxy_exhaust = 0
                # Successful call → restore to ALIVE no matter what state
                if acc.status not in (DISABLED, RELOGGING):
                    acc.status = ALIVE
                return

            acc.last_failure_at = now
            if session_redirected:
                acc.consecutive_302 += 1
                if acc.consecutive_302 >= self.session_death_threshold:
                    if acc.status not in (DEAD, RELOGGING, DISABLED):
                        acc.status = DEAD
                        relogin_needed = True
                else:
                    if acc.status == ALIVE:
                        acc.status = DYING
            elif proxies_exhausted:
                # Session is probably fine — the account's pinned proxy just died.
                # A relogin repins a fresh working _login_proxy. Mark DYING (still
                # serves as a fallback) and schedule a repin after a couple of
                # consecutive exhaustions so a one-off network blip doesn't relogin.
                acc.consecutive_proxy_exhaust += 1
                if acc.status == ALIVE:
                    acc.status = DYING
                if (acc.consecutive_proxy_exhaust >= self.proxy_exhaust_relogin_threshold
                        and acc.status not in (RELOGGING, DISABLED)):
                    relogin_needed = True
        if relogin_needed:
            self._schedule_relogin(account_id)

    async def force_relogin(self, account_id: str) -> None:
        """External trigger: mark an account dead and queue a relogin."""
        async with self._lock:
            acc = self._accounts.get(account_id)
            if acc is None or acc.status in (RELOGGING, DISABLED):
                return
            acc.status = DEAD
        self._schedule_relogin(account_id)

    def account_ids(self) -> list:
        """All known account ids (for health-validation sweeps)."""
        return list(self._accounts.keys())

    async def report_account_health(self, account_id: str, healthy: bool) -> None:
        """Record the verdict of an out-of-band health probe.

        healthy=True  → the session returned live data → mark ALIVE (usable).
        healthy=False → the session is dead/redirecting → mark DEAD and queue a
                        relogin (only fires if LINKEDIN_AUTO_RELOGIN is on).

        Never stomps an account that's mid-request (in_use), currently
        relogging, or disabled on backoff.
        """
        relogin_needed = False
        async with self._lock:
            acc = self._accounts.get(account_id)
            if acc is None or acc.in_use or acc.status in (RELOGGING, DISABLED):
                return
            if healthy:
                acc.status = ALIVE
                acc.consecutive_302 = 0
                acc.consecutive_proxy_exhaust = 0
                acc.last_success_at = time.time()
            else:
                acc.status = DEAD
                acc.last_failure_at = time.time()
                relogin_needed = True
        if relogin_needed:
            self._schedule_relogin(account_id)

    def snapshot(self) -> dict:
        return {
            "accounts": [acc.snapshot() for acc in self._accounts.values()],
            "counters": {
                "total": len(self._accounts),
                "alive": sum(1 for a in self._accounts.values() if a.status == ALIVE),
                "dying": sum(1 for a in self._accounts.values() if a.status == DYING),
                "dead": sum(1 for a in self._accounts.values() if a.status == DEAD),
                "relogging": sum(1 for a in self._accounts.values() if a.status == RELOGGING),
                "disabled": sum(1 for a in self._accounts.values() if a.status == DISABLED),
                "in_use": sum(1 for a in self._accounts.values() if a.in_use),
            },
        }

    # --------------------------------------------------------- selection logic

    def _pick_next_alive_locked(self) -> Optional[AccountState]:
        """Round-robin pick, preferring ALIVE over DYING accounts.

        DYING still serves (one more chance after cookie merge / dead proxy) but
        only as a fallback — a healthy ALIVE account is always tried first so a
        request isn't slowed by an account we already know is degraded. Only
        DEAD/RELOGGING/DISABLED are fully excluded.
        """
        ids = list(self._accounts.keys())
        if not ids:
            return None
        n = len(ids)
        # Pass 1: ALIVE only. Pass 2: accept DYING as a fallback.
        for statuses in ((ALIVE,), (ALIVE, DYING)):
            for i in range(n):
                idx = (self._index + i) % n
                acc = self._accounts[ids[idx]]
                if acc.in_use:
                    continue
                if acc.status in statuses:
                    self._index = (idx + 1) % n
                    return acc
        return None

    def _reactivate_disabled_locked(self) -> None:
        now = time.time()
        for acc in self._accounts.values():
            if acc.status == DISABLED and acc.disabled_until <= now:
                acc.status = DEAD
                acc.disabled_until = 0.0
                acc.consecutive_relogin_failures = 0
                logger.info("account_pool.disabled_window_expired", account_id=acc.account_id)
                # Re-queue via _schedule_relogin so the LINKEDIN_AUTO_RELOGIN
                # gate + dedup apply here too (was bypassing both).
                self._schedule_relogin(acc.account_id)

    # ------------------------------------------------- background relogin

    def _schedule_relogin(self, account_id: str) -> None:
        # Master switch for "saved sessions only" mode: when off, the pool never
        # launches a browser to re-login — dead accounts just stay DEAD until a
        # manual login. Default on (self-healing).
        if os.getenv("LINKEDIN_AUTO_RELOGIN", "true").lower() not in ("1", "true", "yes", "on"):
            logger.info("account_pool.relogin_skipped_disabled", account_id=account_id)
            return
        if account_id in self._relogin_tasks and not self._relogin_tasks[account_id].done():
            return
        task = asyncio.create_task(self._run_relogin_once(account_id))
        self._relogin_tasks[account_id] = task

    async def _run_relogin_once(self, account_id: str) -> None:
        async with self._relogin_sem:
            async with self._lock:
                acc = self._accounts.get(account_id)
                if acc is None or acc.status == RELOGGING:
                    return
                acc.status = RELOGGING
                username = acc.username
                password = acc.password
                static_proxy = acc.static_proxy

            logger.info("account_pool.relogin_start", account_id=account_id)
            ok = False
            try:
                from api.services.linkedin_login_runner import login_account_with_retries
                ok = await login_account_with_retries(
                    account_id=account_id,
                    username=username,
                    password=password,
                    static_proxy=static_proxy,
                    headless=self.relogin_headless,
                )
            except Exception as e:
                logger.error("account_pool.relogin_exception", account_id=account_id, error=str(e)[:200])

            async with self._lock:
                acc = self._accounts.get(account_id)
                if acc is None:
                    return
                if ok:
                    acc.status = ALIVE
                    acc.consecutive_302 = 0
                    acc.consecutive_relogin_failures = 0
                    acc.total_relogins += 1
                    acc.last_success_at = time.time()
                    logger.info("account_pool.relogin_success", account_id=account_id,
                                total_relogins=acc.total_relogins)
                else:
                    acc.consecutive_relogin_failures += 1
                    if acc.consecutive_relogin_failures >= self.disable_after_failures:
                        acc.status = DISABLED
                        acc.disabled_until = time.time() + self.disable_backoff_seconds
                        logger.error("account_pool.account_disabled",
                                     account_id=account_id,
                                     until=acc.disabled_until,
                                     backoff_seconds=self.disable_backoff_seconds)
                    else:
                        acc.status = DEAD
                        logger.warning("account_pool.relogin_failed",
                                       account_id=account_id,
                                       failures=acc.consecutive_relogin_failures)

    # ----------------------------------------------------- env account loader

    def _load_accounts_from_env(self) -> None:
        from api.services.linkedin_env import parse_linkedin_accounts_env
        loaded = 0
        for acc in parse_linkedin_accounts_env():
            account_id = acc["account_id"]
            if account_id in self._accounts:
                continue
            self._accounts[account_id] = AccountState(
                account_id=account_id,
                username=acc["username"],
                password=acc["password"],
                static_proxy=acc["proxy_url"],
                status=self._infer_initial_status(account_id),
            )
            loaded += 1

        # Sort accounts for deterministic ordering
        self._accounts = dict(sorted(self._accounts.items()))
        logger.info("account_pool.loaded", count=loaded,
                    initial_alive=sum(1 for a in self._accounts.values() if a.status == ALIVE),
                    initial_dead=sum(1 for a in self._accounts.values() if a.status == DEAD))

    def _infer_initial_status(self, account_id: str) -> str:
        """Account is ALIVE if a session file exists, else DEAD (needs login)."""
        for suffix in ("__mobile.json", "__desktop.json", ".json"):
            p = SESSIONS_DIR / f"{account_id}{suffix}"
            if p.exists():
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    cookies = d.get("cookies", [])
                    if any(c.get("name") == "li_at" and c.get("value") for c in cookies):
                        return ALIVE
                except Exception:
                    pass
        return DEAD

    async def warmup(self) -> None:
        """Trigger relogins for all accounts that start DEAD.

        Called at process startup; doesn't block — workers run in background.
        No-op when auto-relogin is disabled (LINKEDIN_AUTO_RELOGIN=false).
        """
        for acc_id, acc in self._accounts.items():
            if acc.status == DEAD:
                self._schedule_relogin(acc_id)
