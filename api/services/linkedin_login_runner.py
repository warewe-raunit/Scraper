"""
api/services/linkedin_login_runner.py — Reusable LinkedIn login runner.

Pulled out of linkedin_multi_account_login.py so the AccountPool background
relogin worker can call it directly. The CLI multi-account script also imports
from here, keeping the proxy-vetting + swap-on-failure logic in ONE place.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

import structlog

# Path bootstrapping so this file works whether imported from api.* or directly.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.browser_manager import LazyBrowser, active_profile_session_id
from tools.linkedin_login import run_tool as _do_login
from tools.linkedin_login_state import linkedin_login_state

logger = structlog.get_logger(__name__)

# Browser errors that indicate the proxy itself broke — swap to a new one instead
# of giving up on the credentials.
_PROXY_ERR_MARKERS = ("ERR_EMPTY_RESPONSE", "ERR_TUNNEL", "ERR_PROXY",
                       "ERR_TIMED_OUT", "ERR_CONNECTION", "net::ERR_")


def _is_proxy_err(msg: str) -> bool:
    return any(s in msg for s in _PROXY_ERR_MARKERS)


async def vet_sticky_proxy_for_login(account_id: str, static_proxy: Optional[str]) -> Optional[str]:
    """Pick ONE proxy that can reach LinkedIn and stick to it for the entire login.

    Login is multi-step (form, captcha, redirects); LinkedIn flags IP changes
    mid-flow. So the proxy is held constant for the whole session.
    """
    if static_proxy:
        return static_proxy

    if os.getenv("LINKEDIN_LOGIN_USE_ROTATING", "true").lower() not in ("true", "1", "yes", "on"):
        return None

    from tools.goodproxies import GoodProxiesProvider
    from curl_cffi import requests as ccff_requests
    gp = GoodProxiesProvider()
    if not (gp.enabled and gp.api_key):
        return None

    max_vet = int(os.getenv("LINKEDIN_LOGIN_PROXY_VET_ATTEMPTS", "150"))
    batch_size = int(os.getenv("LINKEDIN_LOGIN_PROXY_VET_BATCH", "15"))

    async def _check(p: str) -> Optional[str]:
        loop = asyncio.get_running_loop()
        try:
            r = await loop.run_in_executor(
                None,
                lambda: ccff_requests.get(
                    "https://www.linkedin.com/robots.txt",
                    proxies={"http": p, "https": p},
                    impersonate="chrome120",
                    timeout=6,
                ),
            )
            if r.status_code == 200 and len(r.text) > 100:
                return p
        except Exception:
            pass
        gp.mark_failed(p)
        return None

    checked = 0
    while checked < max_vet:
        batch = []
        for _ in range(batch_size):
            p = await gp.get_proxy()
            if p:
                batch.append(p)
        if not batch:
            break
        results = await asyncio.gather(*[_check(p) for p in batch])
        checked += len(batch)
        for r in results:
            if r:
                logger.info("linkedin_login.sticky_proxy_vetted",
                            account_id=account_id, proxy=r[:30] + "...", checked=checked)
                return r

    logger.warning("linkedin_login.no_working_proxy_vetted",
                   account_id=account_id, attempts=checked)
    return None


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


async def _attempt_login_with_proxy(
    account_id: str,
    username: str,
    password: str,
    captcha_config: Optional[dict],
    headless: bool,
    proxy_url: Optional[str],
) -> dict:
    """One full login attempt with a specific sticky proxy.

    Returns:
        {
          "success": bool,
          "proxy_navigation_failed": bool,  # True → swap proxy and retry
          "error": str,
        }
    """
    display_proxy = "Direct"
    if proxy_url:
        display_proxy = (f"***@{proxy_url.split('@')[-1]}" if "@" in proxy_url else proxy_url)
    logger.info("starting_account_login", account_id=account_id, username=username,
                proxy=display_proxy, headless=headless)

    lazy_browser = LazyBrowser(account_id=account_id, proxy_url=proxy_url,
                                headless=headless, use_rotating_proxy=False)
    try:
        try:
            page = await lazy_browser.get_page()
            state = await linkedin_login_state(page, expected_username=username, navigate=True)
        except Exception as e:
            err = str(e)
            if _is_proxy_err(err):
                return {"success": False, "proxy_navigation_failed": True, "error": err[:200]}
            return {"success": False, "proxy_navigation_failed": False, "error": err[:200]}

        if state.get("logged_in"):
            logger.info("account_already_logged_in", account_id=account_id, reason=state.get("reason"))
            return {"success": True, "proxy_navigation_failed": False, "error": ""}

        logger.info("session_missing_or_expired_starting_login_flow", account_id=account_id)
        result = await _do_login(page=page, account_id=account_id, username=username,
                                  password=password, captcha_config=captcha_config)
        success = bool(result.get("success", False))
        err = (result.get("error") or "")
        if success:
            logger.info("login_completed_successfully", account_id=account_id)
            return {"success": True, "proxy_navigation_failed": False, "error": ""}
        if _is_proxy_err(err):
            logger.warning("linkedin_login.proxy_navigation_failed", proxy=proxy_url, error=err[:140])
            return {"success": False, "proxy_navigation_failed": True, "error": err[:200]}
        logger.error("login_failed", account_id=account_id, error=err[:200])
        return {"success": False, "proxy_navigation_failed": False, "error": err[:200]}
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
    """Run a full login, swapping proxies on Chromium-level proxy failures.

    Returns True if the session was successfully created/refreshed.
    """
    swaps = max_proxy_swaps if max_proxy_swaps is not None else int(
        os.getenv("LINKEDIN_LOGIN_PROXY_SWAP_MAX", "6")
    )

    last_proxy = None
    for swap in range(swaps):
        proxy_url = await vet_sticky_proxy_for_login(account_id, static_proxy)
        last_proxy = proxy_url

        result = await _attempt_login_with_proxy(
            account_id, username, password, captcha_config, headless, proxy_url
        )

        if result["success"]:
            if proxy_url:
                persist_login_proxy_into_session(account_id, proxy_url)
            return True

        if result["proxy_navigation_failed"] and proxy_url:
            try:
                from tools.goodproxies import GoodProxiesProvider
                GoodProxiesProvider().mark_failed(proxy_url)
            except Exception:
                pass
            logger.info("linkedin_login.swapping_proxy",
                        account_id=account_id, attempt=swap + 1, max=swaps)
            continue

        return False

    logger.error("linkedin_login.all_proxy_swaps_exhausted",
                 account_id=account_id, last_proxy=last_proxy)
    return False
