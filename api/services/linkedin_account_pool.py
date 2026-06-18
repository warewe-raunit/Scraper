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
from typing import Dict, Optional, List, Set, Any

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
    login_country: Optional[str] = None
    ledger: Optional[Any] = None

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
            "login_country": self.login_country,
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

        # Ban state tracking configuration
        self.ban_detection_enabled = os.getenv("ACCOUNT_BAN_DETECTION", "true").lower() in ("1", "true", "yes", "on")
        self.ban_protect_enabled = os.getenv("ACCOUNT_BAN_PROTECT", "false").lower() in ("1", "true", "yes", "on")
        self.ban_risk_warn = float(os.getenv("BAN_RISK_WARN", "6.0"))
        self.ban_risk_suspect = float(os.getenv("BAN_RISK_SUSPECT", "10.0"))
        self.ban_risk_decay = float(os.getenv("BAN_RISK_DECAY", "0.85"))
        self.ban_forbidden_streak = int(os.getenv("BAN_FORBIDDEN_STREAK", "4"))
        self.just_relogged_accounts = set()

        from tools import ban_state_store
        self.ledgers = ban_state_store.load() if self.ban_detection_enabled else {}

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

    async def acquire_multi(self, count: int, acquire_wait_seconds: Optional[float] = None) -> List[AccountState]:
        """Reserve up to `count` healthy accounts that are in the same country.

        If no group of accounts is free, waits up to acquire_wait_seconds.
        """
        wait_sec = acquire_wait_seconds if acquire_wait_seconds is not None else self.acquire_wait_seconds
        deadline = time.time() + wait_sec
        while True:
            async with self._lock:
                # Reactivate disabled accounts whose backoff expired
                self._reactivate_disabled_locked()

                # Helper to check if an account is terminal
                def _is_terminal(acc: AccountState) -> bool:
                    if self.ban_detection_enabled and self.ban_protect_enabled:
                        if acc.ledger and acc.ledger.ban_state in ("shadow_confirmed", "suspended", "restricted"):
                            return True
                    return False

                # Helper to check if an account is at risk
                def _is_at_risk(acc: AccountState) -> bool:
                    if self.ban_detection_enabled and self.ban_protect_enabled:
                        if acc.ledger and acc.ledger.ban_state == "at_risk":
                            return True
                    return False

                # Group free healthy accounts by country
                # We accept ALIVE or DYING, preferring ALIVE
                candidates = []
                for acc in self._accounts.values():
                    if acc.in_use:
                        continue
                    if _is_terminal(acc):
                        continue
                    if acc.status in (ALIVE, DYING):
                        candidates.append(acc)

                if candidates:
                    # Group by login_country (normalize to upper, default to "UNKNOWN")
                    groups: Dict[str, List[AccountState]] = {}
                    for acc in candidates:
                        country = (acc.login_country or "UNKNOWN").strip().upper()
                        groups.setdefault(country, []).append(acc)

                    # Sort candidates in each group so ALIVE accounts are preferred over DYING,
                    # and non-at-risk are preferred over at-risk
                    def _sort_key(a: AccountState):
                        status_val = 0 if a.status == ALIVE else 2
                        risk_val = 1 if _is_at_risk(a) else 0
                        return (status_val + risk_val, a.last_success_at)

                    for country in groups:
                        groups[country].sort(key=_sort_key)

                    # Find the group with the most candidates
                    best_country = max(groups.keys(), key=lambda c: len(groups[c]))
                    chosen_group = groups[best_country]

                    # Acquire up to `count` accounts from this group
                    acquired = chosen_group[:count]
                    for acc in acquired:
                        acc.in_use = True
                    return acquired

            if time.time() >= deadline:
                raise NoAccountAvailable("No LinkedIn accounts available in the same region.")
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
                
                # Record signal "ok"
                self.record_signal(account_id, "ok")
                self.just_relogged_accounts.discard(account_id)
                return

            acc.last_failure_at = now
            if session_redirected:
                acc.consecutive_302 += 1
                
                # Record signals: challenge_after_relogin or forbidden
                if account_id in self.just_relogged_accounts:
                    self.record_signal(account_id, "challenge_after_relogin")
                else:
                    self.record_signal(account_id, "forbidden")
                self.just_relogged_accounts.discard(account_id)

                if acc.consecutive_302 >= self.session_death_threshold:
                    if acc.status not in (DEAD, RELOGGING, DISABLED):
                        acc.status = DEAD
                        relogin_needed = True
                else:
                    if acc.status == ALIVE:
                        acc.status = DYING
            elif proxies_exhausted:
                # Record signal "inconclusive"
                self.record_signal(account_id, "inconclusive")

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
                self.confirm_verdict(account_id, "clear")
            else:
                acc.status = DEAD
                acc.last_failure_at = time.time()
                relogin_needed = True
        if relogin_needed:
            self._schedule_relogin(account_id)

    def record_signal(self, account_id: str, signal: str) -> None:
        """Record a risk signal (e.g. ok, forbidden, soft_block, rate_limited).
        Updates the account risk state and saves it to persistence.
        """
        if not self.ban_detection_enabled:
            return

        acc = self._accounts.get(account_id)
        if acc is None or acc.ledger is None:
            return

        old_state = acc.ledger.ban_state
        acc.ledger.record(signal)

        # Save change
        from tools import ban_state_store
        ban_state_store.save(self.ledgers)

        new_state = acc.ledger.ban_state
        if old_state != new_state:
            if new_state == "shadow_suspected":
                logger.warning(
                    "account.ban_suspected",
                    platform="linkedin",
                    account_id=account_id,
                    risk_score=round(acc.ledger.risk_score, 1),
                    recent_signals=acc.ledger.last_signals,
                )
            elif new_state in ("shadow_confirmed", "suspended", "restricted"):
                logger.error(
                    f"account.{new_state}",
                    platform="linkedin",
                    account_id=account_id,
                    risk_score=round(acc.ledger.risk_score, 1),
                )
            elif new_state == "clear":
                logger.info(
                    "account.ban_cleared",
                    platform="linkedin",
                    account_id=account_id,
                )

    def confirm_verdict(self, account_id: str, verdict: str) -> None:
        """Confirm a definitive verdict (clear, restricted, suspended, shadow_confirmed)
        on the account ledger, and save the state.
        """
        if not self.ban_detection_enabled:
            return

        acc = self._accounts.get(account_id)
        if acc is None or acc.ledger is None:
            return

        old_state = acc.ledger.ban_state
        acc.ledger.confirm(verdict)

        # Save change
        from tools import ban_state_store
        ban_state_store.save(self.ledgers)

        new_state = acc.ledger.ban_state
        if old_state != new_state:
            if new_state == "clear":
                logger.info(
                    "account.ban_cleared",
                    platform="linkedin",
                    account_id=account_id,
                )
            elif new_state in ("shadow_confirmed", "suspended", "restricted"):
                logger.error(
                    f"account.{new_state}",
                    platform="linkedin",
                    account_id=account_id,
                    risk_score=round(acc.ledger.risk_score, 1),
                )

    def snapshot(self) -> dict:
        accounts = []
        flagged_list = []
        for acc in self._accounts.values():
            ban_state = "clear"
            risk_score = 0.0
            last_signal = None
            flagged_at = None
            if self.ban_detection_enabled and acc.ledger:
                ban_state = acc.ledger.ban_state
                risk_score = round(acc.ledger.risk_score, 1)
                last_signal = acc.ledger.last_signals[-1][1] if acc.ledger.last_signals else None
                flagged_at = acc.ledger.flagged_at
                if ban_state != "clear":
                    flagged_list.append(acc.account_id)

            snap = acc.snapshot()
            snap.update({
                "ban_state": ban_state,
                "risk_score": risk_score,
                "last_signal": last_signal,
                "flagged_at": flagged_at,
            })
            accounts.append(snap)

        return {
            "accounts": accounts,
            "counters": {
                "total": len(self._accounts),
                "alive": sum(1 for a in self._accounts.values() if a.status == ALIVE),
                "dying": sum(1 for a in self._accounts.values() if a.status == DYING),
                "dead": sum(1 for a in self._accounts.values() if a.status == DEAD),
                "relogging": sum(1 for a in self._accounts.values() if a.status == RELOGGING),
                "disabled": sum(1 for a in self._accounts.values() if a.status == DISABLED),
                "in_use": sum(1 for a in self._accounts.values() if a.in_use),
            },
            "flagged": flagged_list,
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

        # Helper to check if an account is terminal
        def _is_terminal(acc: AccountState) -> bool:
            if self.ban_detection_enabled and self.ban_protect_enabled:
                if acc.ledger and acc.ledger.ban_state in ("shadow_confirmed", "suspended", "restricted"):
                    return True
            return False

        # Helper to check if an account is at risk
        def _is_at_risk(acc: AccountState) -> bool:
            if self.ban_detection_enabled and self.ban_protect_enabled:
                if acc.ledger and acc.ledger.ban_state == "at_risk":
                    return True
            return False

        # Pass 1: ALIVE and not at_risk.
        # Pass 2: ALIVE or DYING (excluding terminal).
        pass1_condition = lambda acc: acc.status == ALIVE and not _is_at_risk(acc)
        pass2_condition = lambda acc: acc.status in (ALIVE, DYING)

        for cond in (pass1_condition, pass2_condition):
            for i in range(n):
                idx = (self._index + i) % n
                acc = self._accounts[ids[idx]]
                if acc.in_use:
                    continue
                if _is_terminal(acc):
                    continue
                if cond(acc):
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
            reason = ""
            try:
                from api.services.linkedin_login_runner import login_account_with_retries
                ok, reason = await login_account_with_retries(
                    account_id=account_id,
                    username=username,
                    password=password,
                    static_proxy=static_proxy,
                    headless=self.relogin_headless,
                )
            except Exception as e:
                logger.error("account_pool.relogin_exception", account_id=account_id, error=str(e)[:200])
                reason = str(e)

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
                    self.just_relogged_accounts.add(account_id)
                    self.confirm_verdict(account_id, "clear")

                    for suffix in ("__mobile.json", "__desktop.json", ".json"):
                        p = SESSIONS_DIR / f"{account_id}{suffix}"
                        if p.exists():
                            try:
                                d = json.loads(p.read_text(encoding="utf-8"))
                                acc.login_country = d.get("_login_country")
                                if acc.login_country:
                                    break
                            except Exception:
                                pass

                    logger.info("account_pool.relogin_success", account_id=account_id,
                                total_relogins=acc.total_relogins)
                else:
                    acc.consecutive_relogin_failures += 1

                    # Check for restriction keywords in failure reason
                    reason_lower = (reason or "").lower()
                    if any(kw in reason_lower for kw in ("quarantine", "restricted", "suspended", "challenge")):
                        self.confirm_verdict(account_id, "restricted")

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

            country = None
            for suffix in ("__mobile.json", "__desktop.json", ".json"):
                p = SESSIONS_DIR / f"{account_id}{suffix}"
                if p.exists():
                    try:
                        d = json.loads(p.read_text(encoding="utf-8"))
                        country = d.get("_login_country")
                        if country:
                            break
                    except Exception:
                        pass

            ledger = None
            if self.ban_detection_enabled:
                if account_id not in self.ledgers:
                    from tools.account_risk import RiskLedger
                    self.ledgers[account_id] = RiskLedger(
                        account_id=account_id,
                        platform="linkedin",
                        decay=self.ban_risk_decay,
                        warn_threshold=self.ban_risk_warn,
                        suspect_threshold=self.ban_risk_suspect,
                        consecutive_forbidden_threshold=self.ban_forbidden_streak,
                    )
                ledger = self.ledgers[account_id]

            self._accounts[account_id] = AccountState(
                account_id=account_id,
                username=acc["username"],
                password=acc["password"],
                static_proxy=acc["proxy_url"],
                status=self._infer_initial_status(account_id),
                login_country=country,
                ledger=ledger,
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
