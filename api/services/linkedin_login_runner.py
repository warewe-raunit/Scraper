"""
api/services/linkedin_login_runner.py — Reusable LinkedIn login runner.

Mirrors multi_account_login.py (Reddit) — single LazyBrowser launch, the
browser manager handles goodproxy selection via use_rotating_proxy=True.
Mobile fingerprint comes from BROWSER_DEVICE_CATEGORY=mobile in .env.

Wraps the Reddit-equivalent login in a proxy-swap loop because goodproxies
rotate randomly and most are dead — on net::ERR_* / timeout, close the
browser and relaunch with a fresh goodproxy. Up to
LINKEDIN_LOGIN_PROXY_SWAP_MAX attempts (default 8).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import structlog

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.browser_manager import LazyBrowser, active_profile_session_id
from tools.linkedin_login import run_tool as _do_login
from tools.linkedin_login_state import linkedin_login_state

logger = structlog.get_logger(__name__)

_PROXY_ERR_MARKERS = (
    "ERR_EMPTY_RESPONSE", "ERR_TUNNEL", "ERR_PROXY", "ERR_TIMED_OUT",
    "ERR_CONNECTION", "net::ERR_", "Page.goto: Timeout",
)

# Login-side markers that also mean "swap proxy and retry": LinkedIn redirected
# to a region-locked variant (zh-cn, 451), served a checkpoint challenge to a
# suspect IP class, or silently dropped the auth call. None of these mean the
# credentials are wrong — they mean the proxy needs swapping.
_RETRYABLE_LOGIN_MARKERS = (
    "Login not confirmed",
    "/zh-cn/", "/checkpoint/pk/", "451 ",
    "Username or password fields not found",
)


def _is_proxy_err(msg: str) -> bool:
    return any(s in msg for s in _PROXY_ERR_MARKERS)


def _is_retryable_login_err(msg: str) -> bool:
    return any(s in msg for s in _RETRYABLE_LOGIN_MARKERS)


def persist_login_proxy_into_session(account_id: str, proxy_url: str) -> None:
    """Pin the login proxy into the saved session JSON so scraping can prefer it."""
    try:
        sess_id = active_profile_session_id(account_id)
        sess_path = ROOT / "sessions" / f"{sess_id}.json"
        if sess_path.exists():
            d = json.loads(sess_path.read_text(encoding="utf-8"))
            d["_login_proxy"] = proxy_url
            sess_path.write_text(json.dumps(d, indent=2), encoding="utf-8")
            logger.info("login.proxy_pinned_to_session", path=str(sess_path), proxy=proxy_url[:30] + "...")
    except Exception as e:
        logger.warning("login.proxy_pin_failed", error=str(e))


def _mark_proxy_failed(proxy_url: Optional[str]) -> None:
    if not proxy_url:
        return
    try:
        from tools.goodproxies import GoodProxiesProvider
        GoodProxiesProvider().mark_failed(proxy_url)
    except Exception:
        pass


async def _login_once(account_id, username, password, proxy_url, captcha_config, headless, use_rotating):
    """Single LazyBrowser launch + login attempt — exact Reddit pattern.

    On proxy navigation error, captures the actually-used goodproxy from the
    browser context and feeds it back into the cooldown pool so the next
    swap doesn't pick the same dead proxy.
    """
    display_proxy = "Direct/Rotating" if use_rotating else (
        f"***:***@{proxy_url.split('@')[-1]}" if proxy_url and "@" in proxy_url else (proxy_url or "Direct")
    )
    logger.info("starting_account_login", account_id=account_id, username=username,
                proxy=display_proxy, headless=headless, use_rotating_proxy=use_rotating)

    lazy_browser = LazyBrowser(account_id=account_id, proxy_url=proxy_url,
                                headless=headless, use_rotating_proxy=use_rotating)
    try:
        try:
            page = await lazy_browser.get_page()
            state = await linkedin_login_state(page, expected_username=username, navigate=True)
        except Exception as e:
            err = str(e)
            if _is_proxy_err(err):
                _mark_proxy_failed(lazy_browser.last_used_proxy)
                return "proxy_err", err[:200]
            return "fatal", err[:200]

        if state.get("logged_in"):
            logger.info("account_already_logged_in", account_id=account_id, reason=state.get("reason"))
            return "success", ""

        # FAST PATH: saved-session identity cookies still recognize the user,
        # so LinkedIn jumped straight to /checkpoint/ asking only for the email
        # PIN — skip the full credentials submit, just solve the OTP.
        try:
            current_url = (page.url or "").lower()
            on_checkpoint = ("/checkpoint/" in current_url
                              or "verification" in current_url
                              or "challenge" in current_url)
        except Exception:
            on_checkpoint = False

        if on_checkpoint:
            from tools.linkedin_login import _handle_email_verification_challenge
            logger.info("session_validation_landed_on_checkpoint_fast_path",
                        account_id=account_id, url=page.url)
            try:
                solved = await _handle_email_verification_challenge(page, username, account_id)
            except Exception as exc:
                logger.warning("login.fast_path_otp_failed", error=str(exc)[:200])
                solved = False
            if solved:
                # Verify we actually got in.
                state_after = await linkedin_login_state(page, expected_username=username, navigate=False)
                if state_after.get("logged_in"):
                    logger.info("login_completed_via_otp_fast_path", account_id=account_id)
                    return "success", ""
                logger.warning("login.fast_path_otp_did_not_unblock", account_id=account_id)
            # Fast path didn't work — fall through to the full credentials flow.

        logger.info("session_missing_or_expired_starting_login_flow", account_id=account_id)
        result = await _do_login(page=page, account_id=account_id, username=username,
                                  password=password, captcha_config=captcha_config)
        if result.get("success"):
            logger.info("login_completed_successfully", account_id=account_id)
            return "success", ""

        err = result.get("error") or ""
        if "[OTP_CHALLENGE_HANDLED]" in err:
            logger.error("login_failed.otp_challenge", account_id=account_id, error=err[:300])
            return "fatal", err[:300]
        if _is_proxy_err(err):
            _mark_proxy_failed(lazy_browser.last_used_proxy)
            logger.warning("login.proxy_navigation_failed", error=err[:140])
            return "proxy_err", err[:200]
        if _is_retryable_login_err(err):
            _mark_proxy_failed(lazy_browser.last_used_proxy)
            logger.warning("login.region_or_challenge_block_swapping_proxy", error=err[:140])
            return "proxy_err", err[:200]
        logger.error("login_failed", account_id=account_id, error=err[:200])
        return "fatal", err[:200]
    finally:
        try:
            await lazy_browser.close()
        except Exception:
            pass


async def login_account_with_retries(
    account_id: str,
    username: str,
    password: str,
    static_proxy: Optional[str],
    *,
    captcha_config: Optional[dict] = None,
    headless: bool = True,
    max_proxy_swaps: Optional[int] = None,
) -> bool:
    """Run a LinkedIn login. Reddit-style LazyBrowser pattern with proxy-swap
    retry on net::ERR_* / timeout (because goodproxies rotate and most are dead).
    """
    use_rotating = static_proxy is None
    swaps = max_proxy_swaps if max_proxy_swaps is not None else int(
        os.getenv("LINKEDIN_LOGIN_PROXY_SWAP_MAX", "3")
    )

    for swap in range(swaps):
        outcome, err = await _login_once(
            account_id, username, password, static_proxy, captcha_config, headless, use_rotating
        )
        if outcome == "success":
            if static_proxy:
                persist_login_proxy_into_session(account_id, static_proxy)
            return True
        if outcome == "fatal":
            return False
        logger.info("login.swapping_proxy", account_id=account_id, attempt=swap + 1, max=swaps)

    logger.error("login.all_proxy_swaps_exhausted", account_id=account_id)
    return False
