"""
tools/linkedin_login.py — LinkedIn login tool for AI agents.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Optional

import structlog
from playwright.async_api import Page

from tools.stealth.helpers import (
    _delay, _random_scroll, _human_type, _ghost_move_and_click,
    _resolve_editable_element, _ok, _fail, _ms,
)
from tools.linkedin_login_state import linkedin_login_state

logger = structlog.get_logger(__name__)


async def _login_form_still_visible(page: Page) -> bool:
    try:
        result = await page.evaluate("""() => {
            const selectors = [
                '#username', '#password',
                'input[name="session_key"]', 'input[name="session_password"]',
                '.error-message', '#error-for-username'
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) return true;
            }
            return false;
        }""")
        return bool(result)
    except Exception:
        return False


async def _resolve_login_submit_element(page: Page, password_el):
    # Try standard selectors
    selectors = ['button[type="submit"]', 'button[aria-label="Sign in"]', '.btn__primary--large']
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return el
        except Exception:
            continue
            
    # Robust fallback: find any visible button or element containing 'Sign in' or 'Log in'
    try:
        element_handle = await page.evaluate_handle(
            """() => {
                const isVisible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return false;
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && parseFloat(style.opacity || '1') > 0;
                };
                const elements = [...document.querySelectorAll('button, input[type="submit"], [role="button"]')].filter(isVisible);
                
                // Look for direct text match
                const match = elements.find(el => {
                    const txt = (el.textContent || el.value || '').trim().toLowerCase();
                    return txt.includes('sign in') || txt.includes('log in');
                });
                return match || null;
            }"""
        )
        element = element_handle.as_element()
        if element:
            return element
    except Exception as e:
        logger.warning("fallback_submit_lookup_failed", error=str(e))
        
    return None


async def _extract_login_error(page: Page) -> str:
    """Return the most useful visible login error LinkedIn is showing."""
    try:
        return await page.evaluate("""() => {
            const candidates = [];
            const selectors = [
                '#error-for-username',
                '#error-for-password',
                '[role="alert"]',
                '.error-message',
                '.artdeco-inline-feedback--error',
            ];
            for (const sel of selectors) {
                for (const n of document.querySelectorAll(sel)) {
                    const rect = n.getBoundingClientRect();
                    const txt = (n.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (txt && rect.width > 0 && rect.height > 0) candidates.push(txt);
                }
            }
            return [...new Set(candidates)].join(' | ');
        }""")
    except Exception:
        return ""


def _format_network_errors(events: list[dict]) -> str:
    if not events:
        return ""
    unique: list[str] = []
    seen = set()
    for event in events[-8:]:
        body = event.get("body") or ""
        item = f"{event.get('status', 'ERR')} {event.get('url', '')}"
        if body:
            item = f"{item} body={body}"
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return "; ".join(unique)


def _login_url_candidates(account_id: str) -> list[str]:
    candidates = [
        (os.getenv("LINKEDIN_LOGIN_URL") or "").strip(),
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/uas/login",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


async def _find_first_visible(page: Page, selectors: list[str], timeout: int = 5_000):
    for sel in selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=timeout, state="visible")
            if el:
                return el
        except Exception:
            continue
    return None


async def _find_login_field(page: Page, kind: str, selectors: list[str]):
    direct = await _find_first_visible(page, selectors, timeout=2_500)
    if direct:
        return direct

    # Robust fallback for dynamic/obfuscated pages (e.g. mobile/responsive web)
    try:
        element_handle = await page.evaluate_handle(
            """({kind}) => {
                const isVisible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return false;
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && parseFloat(style.opacity || '1') > 0;
                };
                
                const inputs = [...document.querySelectorAll('input')].filter(isVisible);
                
                if (kind === 'password') {
                    // Try autocomplete match first, then fallback to type
                    const autocomp = inputs.find(el => (el.getAttribute('autocomplete') || '').includes('password'));
                    if (autocomp) return autocomp;
                    return inputs.find(el => el.type === 'password') || null;
                } else {
                    // Try autocomplete match first, then fallback to type
                    const autocomp = inputs.find(el => (el.getAttribute('autocomplete') || '').includes('username'));
                    if (autocomp) return autocomp;
                    return inputs.find(el => 
                        (el.type === 'email' || el.type === 'text') && 
                        !((el.id || '').includes('search')) && 
                        !((el.name || '').includes('search'))
                    ) || null;
                }
            }""",
            {"kind": kind}
        )
        element = element_handle.as_element()
        if element:
            return element
    except Exception as e:
        logger.warning("fallback_field_lookup_failed", kind=kind, error=str(e))
        
    return None


async def _is_logged_in(page: Page, expected_username: str = "") -> bool:
    try:
        state = await linkedin_login_state(
            page,
            expected_username=expected_username,
            navigate=False,
        )
        return bool(state.get("logged_in"))
    except Exception:
        return False


async def login(
    page: Page,
    account_id: str,
    username: str,
    password: str,
    db=None,
    proxy_id: Optional[str] = None,
    captcha_config: Optional[dict] = None,
    proxy_config: Optional[dict] = None,
) -> dict:
    """Execute an explicit LinkedIn login sequence with human-like behavior."""
    log = logger.bind(account_id=account_id, action="LINKEDIN_LOGIN")
    start_ms = _ms()
    network_events: list[dict] = []

    async def _capture_response(response) -> None:
        try:
            url = response.url
            if "linkedin.com" not in url:
                return
            interesting = any(part in url.lower() for part in ("login", "auth", "session", "signin"))
            if response.status < 400 and not interesting:
                return
            event = {"status": response.status, "url": url[:180]}
            network_events.append(event)
        except Exception:
            pass

    page.on("response", _capture_response)

    try:
        user_sels = [
            'input[autocomplete*="username"]',
            'input[type="email"]',
            '#username',
            'input[name="session_key"]',
        ]
        pass_sels = [
            'input[autocomplete*="password"]',
            'input[type="password"]',
            '#password',
            'input[name="session_password"]',
        ]

        user_el = None
        pass_el = None
        for attempt_index, login_url in enumerate(_login_url_candidates(account_id), start=1):
            log.info("linkedin.login.navigating", url=login_url, attempt=attempt_index)
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)

            await _delay(account_id, 2.0, 4.0)
            await _random_scroll(page, account_id, read_min=0.5, read_max=2.0)
            await _delay(account_id, 0.8, 1.8)

            user_el = await _find_login_field(page, "username", user_sels)
            pass_el = await _find_login_field(page, "password", pass_sels) if user_el else None
            if user_el and pass_el:
                break

        if not user_el or not pass_el:
            raise RuntimeError("Username or password fields not found on LinkedIn login page.")

        user_el = await _resolve_editable_element(page, user_el)
        pass_el = await _resolve_editable_element(page, pass_el)

        await _ghost_move_and_click(page, user_el)
        await _delay(account_id, 0.2, 0.6)
        await user_el.fill("")
        await _human_type(page, user_el, username, account_id)

        await _delay(account_id, 0.2, 0.7)
        await _ghost_move_and_click(page, pass_el)
        await _delay(account_id, 0.2, 0.6)
        await pass_el.fill("")
        await _human_type(page, pass_el, password, account_id)

        await _delay(account_id, 0.5, 1.2)
        await _ghost_move_and_click(page, pass_el)
        await pass_el.press("Enter")
        await _delay(account_id, 2.5, 5.0)

        # Handle post-submit check
        is_logged_in = await _is_logged_in(page, username)
        failure_reason = "Login not confirmed after submit"

        if not is_logged_in:
            error_text = await _extract_login_error(page)
            failure_reason = error_text or "Login not confirmed after submit"
            if network_events:
                failure_reason = f"{failure_reason}. Network: {_format_network_errors(network_events)}"
            log.warning("linkedin.login.rejected", reason=failure_reason)

        elapsed = _ms() - start_ms

        if is_logged_in:
            log.info("linkedin.login.success", elapsed_ms=elapsed)
            return _ok({"logged_in": True, "url": page.url})

        return _fail(failure_reason, {"url": page.url, "network": network_events[-8:]})

    except Exception as exc:
        elapsed = _ms() - start_ms
        error_msg = str(exc)
        log.error("linkedin.login.failed", error=error_msg)
        return _fail(error_msg)
    finally:
        try:
            page.remove_listener("response", _capture_response)
        except Exception:
            pass


async def run_tool(
    page: Page,
    account_id: str,
    username: str,
    password: str,
    db=None,
    proxy_id: Optional[str] = None,
    captcha_config: Optional[dict] = None,
    proxy_config: Optional[dict] = None,
) -> dict:
    """AI agent entry point for LinkedIn login."""
    return await login(
        page=page, account_id=account_id, username=username, password=password,
        db=db, proxy_id=proxy_id, captcha_config=captcha_config, proxy_config=proxy_config,
    )
