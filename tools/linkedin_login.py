"""
tools/linkedin_login.py — LinkedIn login tool for AI agents.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
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


async def _detect_otp_input(page: Page, max_wait_seconds: float = 25.0) -> Any:
    """Wait up to max_wait_seconds for any plausible OTP input to become visible.

    Tiered detection:
      1. Targeted selectors (LinkedIn's known OTP field names/ids).
      2. Generic numeric inputs on /checkpoint/ or verification pages.
      3. ANY visible text/tel input on a checkpoint page (last resort).
    """
    otp_selectors = (
        'input[name="pin"]',
        'input[name="challengeId"]',
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"][maxlength="6"]',
        'input[maxlength="6"]',
        'input[id*="pin" i]',
        'input[id*="verification" i]',
        'input[id*="challenge" i]',
        'input[aria-label*="code" i]',
        'input[aria-label*="pin" i]',
        'input[inputmode="numeric"]',
        'input[type="tel"]',
        'input[type="text"][maxlength="6"]',
        'input#input__email_verification_pin',
        'input#input__phone_verification_pin',
    )

    fallback_selectors = (
        'input[type="text"]:visible',
        'input[type="tel"]:visible',
        'input:not([type="hidden"]):not([type="submit"]):not([type="button"])',
    )

    deadline = asyncio.get_event_loop().time() + max_wait_seconds
    while asyncio.get_event_loop().time() < deadline:
        for sel in otp_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                continue
        try:
            await asyncio.sleep(1.0)
        except Exception:
            break

    # Last-resort fallback: any visible input on a checkpoint URL.
    url_now = (page.url or "").lower()
    if "/checkpoint/" in url_now or "verification" in url_now or "challenge" in url_now:
        for sel in fallback_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    logger.info("linkedin.login.otp_input_fallback_selector_used", selector=sel)
                    return el
            except Exception:
                continue
    return None


async def _handle_email_verification_challenge(
    page: Page, username: str, account_id: str
) -> bool:
    """If LinkedIn shows the post-login email-verification page, fetch the
    6-digit code from Gmail via OAuth IMAP and submit it.

    Returns True if a code was filled and submitted, False otherwise.
    """
    # First give LinkedIn time to redirect to /checkpoint after the login submit.
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    await asyncio.sleep(1.5)

    url_now = (page.url or "").lower()
    page_title = ""
    try:
        page_title = (await page.title() or "").lower()
    except Exception:
        pass

    looks_like_checkpoint = (
        "/checkpoint/" in url_now
        or "/uas/login-submit" in url_now
        or "verification" in url_now
        or "challenge" in url_now
        or "verify" in page_title
        or "security" in page_title
    )

    logger.info(
        "linkedin.login.post_submit_state",
        url=page.url, title=page_title[:80], looks_like_checkpoint=looks_like_checkpoint,
    )

    # Try to detect input even if URL/title don't look like a checkpoint —
    # LinkedIn sometimes overlays the OTP modal on the login page itself.
    otp_el = await _detect_otp_input(page, max_wait_seconds=30.0 if looks_like_checkpoint else 6.0)

    if not otp_el:
        # Always capture a debug screenshot when we suspect checkpoint but missed
        # the input — invaluable for tuning selectors next time.
        if looks_like_checkpoint:
            try:
                from pathlib import Path as _P
                shot = _P(__file__).resolve().parents[1] / f"sessions/_otp_debug_{account_id}.png"
                await page.screenshot(path=str(shot), full_page=True)
                logger.warning("linkedin.login.email_otp_input_not_found_on_checkpoint",
                               url=page.url, screenshot=str(shot))
            except Exception:
                logger.warning("linkedin.login.email_otp_input_not_found_on_checkpoint", url=page.url)
        return False

    logger.info("linkedin.login.email_otp_challenge_detected", url=page.url)

    from tools.gmail_otp_fetcher import fetch_linkedin_otp

    timeout_s = float(os.getenv("LINKEDIN_EMAIL_OTP_TIMEOUT_SECONDS", "180"))
    code = await fetch_linkedin_otp(
        account_id=account_id,
        gmail_address=username,
        poll_timeout=timeout_s,
    )
    if not code:
        logger.error("linkedin.login.email_otp_not_received", username=username)
        return False

    logger.info("linkedin.login.email_otp_fetched", code_preview=code[:2] + "****")

    try:
        await otp_el.click()
        await otp_el.fill("")
        await _human_type(page, otp_el, code, account_id)
        await _delay(account_id, 0.5, 1.2)

        submit_selectors = (
            'button[type="submit"]:not([disabled])',
            'button:has-text("Submit")',
            'button:has-text("Verify")',
            'button:has-text("Continue")',
            'button:has-text("Next")',
            '[role="button"]:has-text("Submit")',
            '[role="button"]:has-text("Verify")',
            'button[data-litms-control-urn*="verify"]',
        )
        clicked = False
        for sel in submit_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            await otp_el.press("Enter")

        # Wait for the verification page to navigate away.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        await _delay(account_id, 3.0, 5.0)
        return True
    except Exception as e:
        logger.error("linkedin.login.email_otp_submit_failed", error=str(e)[:200])
        return False


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


async def _find_google_signin_button(page: Page) -> tuple[Any, bool]:
    # Define the iframe-based locator
    iframe_locator = page.frame_locator('iframe[title*="Google"], iframe[title*="google"], iframe[src*="accounts.google.com/gsi"]')
    google_iframe_btn = iframe_locator.get_by_role("button", name=re.compile("Google|Sign in|Continue", re.IGNORECASE))

    # Wait up to 8 seconds for the iframe button to be visible
    try:
        await google_iframe_btn.wait_for(state="visible", timeout=8000)
        return google_iframe_btn, True
    except Exception:
        pass

    # Fallback to main page buttons
    selectors = [
        'button:has-text("Google")',
        'a:has-text("Google")',
        '#google-sign-in-btn',
        '.google-sign-in-btn',
        '[data-testid="google-sign-in-btn"]',
    ]
    for sel in selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                return btn, False
        except Exception:
            continue

    # Fallback to general role / text locator on main page
    try:
        btn = page.get_by_role("button", name=re.compile("Google", re.IGNORECASE))
        if await btn.is_visible():
            return btn, True
        btn = page.get_by_text(re.compile("Continue with Google|Sign in with Google", re.IGNORECASE))
        if await btn.is_visible():
            return btn, True
    except Exception:
        pass

    return None, False


async def login(
    page: Page,
    account_id: str,
    username: str,
    password: str,
    db=None,
    proxy_id: Optional[str] = None,
    captcha_config: Optional[dict] = None,
    proxy_config: Optional[dict] = None,
    login_method: str = "linkedin",
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
        google_btn = None
        is_locator = False
        for attempt_index, login_url in enumerate(_login_url_candidates(account_id), start=1):
            log.info("linkedin.login.navigating", url=login_url, attempt=attempt_index)
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)

            await _delay(account_id, 2.0, 4.0)
            await _random_scroll(page, account_id, read_min=0.5, read_max=2.0)
            await _delay(account_id, 0.8, 1.8)

            if login_method == "google":
                google_btn, is_locator = await _find_google_signin_button(page)
                if google_btn:
                    break
            else:
                user_el = await _find_login_field(page, "username", user_sels)
                pass_el = await _find_login_field(page, "password", pass_sels) if user_el else None
                if user_el and pass_el:
                    break

        if login_method == "google":
            if not google_btn:
                raise RuntimeError("Could not find 'Continue with Google' button on the LinkedIn login page.")

            log.info("linkedin.login.google.clicking_button")

            async with page.expect_popup() as popup_info:
                await google_btn.click()

            popup = await popup_info.value
            await popup.wait_for_load_state("domcontentloaded")
            log.info("linkedin.login.google.popup_opened", url=popup.url)

            # Fill email
            email_el = None
            email_selectors = ['#identifierId', 'input[type="email"]', 'input[name="identifier"]']
            for sel in email_selectors:
                try:
                    email_el = await popup.wait_for_selector(sel, timeout=10000, state="visible")
                    if email_el:
                        break
                except Exception:
                    continue

            if not email_el:
                raise RuntimeError("Could not find Google email input field in popup.")

            await _delay(account_id, 0.6, 1.4)
            await email_el.fill("")
            await _human_type(popup, email_el, username, account_id)
            await _delay(account_id, 0.4, 0.9)

            # Click Next
            next_btn = None
            next_selectors = ['#identifierNext', 'button:has-text("Next")', '[role="button"]:has-text("Next")']
            for sel in next_selectors:
                try:
                    next_btn = await popup.query_selector(sel)
                    if next_btn and await next_btn.is_visible():
                        break
                except Exception:
                    continue

            if not next_btn:
                raise RuntimeError("Could not find Google email 'Next' button.")

            await next_btn.click()
            await _delay(account_id, 2.0, 4.0)

            # Fill password
            pass_el = None
            pass_selectors = ['input[type="password"]', 'input[name="password"]', '#password input']
            for sel in pass_selectors:
                try:
                    pass_el = await popup.wait_for_selector(sel, timeout=10000, state="visible")
                    if pass_el:
                        break
                except Exception:
                    continue

            if not pass_el:
                try:
                    error_el = await popup.query_selector('[role="alert"]')
                    if error_el and await error_el.is_visible():
                        err_txt = await error_el.text_content()
                        raise RuntimeError(f"Google email submission failed: {err_txt.strip()}")
                except Exception as e:
                    if "Google email submission failed" in str(e):
                        raise e
                raise RuntimeError("Could not find Google password input field in popup.")

            await _delay(account_id, 0.6, 1.4)
            await pass_el.fill("")
            await _human_type(popup, pass_el, password, account_id)
            await _delay(account_id, 0.4, 0.9)

            # Click Password Next
            pass_next_btn = None
            pass_next_selectors = ['#passwordNext', 'button:has-text("Next")', '[role="button"]:has-text("Next")']
            for sel in pass_next_selectors:
                try:
                    pass_next_btn = await popup.query_selector(sel)
                    if pass_next_btn and await pass_next_btn.is_visible():
                        break
                except Exception:
                    continue

            if not pass_next_btn:
                raise RuntimeError("Could not find Google password 'Next' button.")

            await pass_next_btn.click()
            log.info("linkedin.login.google.credentials_submitted")

            # Wait for popup to close / login to complete
            for _ in range(60):
                if popup.is_closed():
                    log.info("linkedin.login.google.popup_closed")
                    break
                is_logged = await _is_logged_in(page, username)
                if is_logged:
                    log.info("linkedin.login.google.logged_in_before_popup_close")
                    break
                try:
                    error_el = await popup.query_selector('[role="alert"]')
                    if error_el and await error_el.is_visible():
                        err_txt = await error_el.text_content()
                        raise RuntimeError(f"Google login failed: {err_txt.strip()}")
                except Exception as e:
                    if "Google login failed" in str(e):
                        raise e
                await asyncio.sleep(1.0)

            await _delay(account_id, 3.0, 5.0)

        else:
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

        # Email-verification challenge: LinkedIn redirects to a "Quick verification"
        # checkpoint page asking for a 6-digit code mailed to the account. Detect
        # that, fetch the code via Gmail OAuth IMAP, fill it, and re-check login.
        otp_was_attempted = False
        if not is_logged_in:
            try:
                solved = await _handle_email_verification_challenge(page, username, account_id)
                otp_was_attempted = solved or "/checkpoint/" in (page.url or "").lower()
                if solved:
                    await _delay(account_id, 2.0, 4.0)
                    is_logged_in = await _is_logged_in(page, username)
            except Exception as exc:
                log.warning("linkedin.login.otp_challenge_handler_failed", error=str(exc)[:200])

        if not is_logged_in:
            error_text = await _extract_login_error(page)
            failure_reason = error_text or "Login not confirmed after submit"
            if otp_was_attempted:
                # Tag the failure so the runner can decide NOT to swap proxy —
                # we already burned the verification page on this proxy; swapping
                # gets a fresh OTP we won't see, and replays the captcha.
                failure_reason = f"[OTP_CHALLENGE_HANDLED] {failure_reason}"
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
    login_method: str = "linkedin",
) -> dict:
    """AI agent entry point for LinkedIn login."""
    return await login(
        page=page, account_id=account_id, username=username, password=password,
        db=db, proxy_id=proxy_id, captcha_config=captcha_config, proxy_config=proxy_config,
        login_method=login_method,
    )
