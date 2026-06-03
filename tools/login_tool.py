"""
tools/login_tool.py — Reddit login tool for AI agents.

Stealth features:
- Circuit breaker prevents account lockout from repeated failures
- Human-like typing with per-character delays and typo simulation
- Organic settle time and random scrolls before touching the form
- Ghost cursor mouse movement to form fields
- reCAPTCHA v3 Enterprise solving (2captcha / anticaptcha / capsolver)
- Multi-attempt login with token refresh between attempts
- Validates logged-in state via multiple selector strategies

Usage:
    result = await run_tool(page, account_id, username="u", password="p")
    # result: {success: bool, data: {logged_in, url}, error: str|None}
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
from tools.stealth.captcha import solve_login_recaptcha, inject_grecaptcha_override
from tools.reddit_urls import reddit_url
from tools.reddit_login_state import reddit_login_state

logger = structlog.get_logger(__name__)


async def _login_form_still_visible(page: Page) -> bool:
    try:
        result = await page.evaluate("""() => {
            const selectors = [
                '#login-username', '#login-password',
                'input[name="username"]', 'input[type="password"]',
                'auth-flow-modal', '.AnimatedForm__errorMessage',
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
    try:
        submit = await page.evaluate("""() => {
            const selectors = ['button[type="submit"]', 'button[data-step="username-and-password"]'];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) return true;
            }
            return false;
        }""")
        if submit:
            return await page.query_selector('button[type="submit"]')
    except Exception:
        pass
    return None


async def _extract_login_error(page: Page) -> str:
    """Return the most useful visible login error Reddit is showing."""
    try:
        return await page.evaluate("""() => {
            const candidates = [];
            const selectors = [
                '[role="alert"]',
                'faceplate-alert',
                'auth-flow-modal [slot="error"]',
                '.AnimatedForm__errorMessage',
                '[class*="ErrorMessage"]',
                '[class*="error-message"]',
                '[class*="error" i]',
            ];
            for (const sel of selectors) {
                for (const n of document.querySelectorAll(sel)) {
                    const rect = n.getBoundingClientRect();
                    const txt = (n.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (txt && rect.width > 0 && rect.height > 0) candidates.push(txt);
                }
            }
            const body = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
            const knownMessages = [
                'Server error. Try again later.',
                'Incorrect username or password',
                'Something went wrong',
                'Try again later',
                'We had a server error'
            ];
            for (const msg of knownMessages) {
                if (body.includes(msg)) candidates.push(msg);
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
    """Try stable Reddit auth surfaces before giving up on missing fields."""
    candidates = [
        (os.getenv("REDDIT_LOGIN_URL") or "").strip(),
        reddit_url("/login/", account_id),
        "https://www.reddit.com/login/",
        "https://www.reddit.com/account/login/",
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
    """Find Reddit login fields in normal DOM or open shadow DOM."""
    direct = await _find_first_visible(page, selectors, timeout=2_500)
    if direct:
        return direct

    handle = await page.evaluate_handle(
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
        const roots = [document];
        const addShadowRoots = (root) => {
            let nodes = [];
            try { nodes = root.querySelectorAll ? root.querySelectorAll('*') : []; } catch (_) { return; }
            for (const node of nodes) {
                if (node.shadowRoot && !roots.includes(node.shadowRoot)) {
                    roots.push(node.shadowRoot);
                    addShadowRoots(node.shadowRoot);
                }
            }
        };
        addShadowRoots(document);

        const candidates = [];
        const seen = new Set();
        for (const root of roots) {
            let nodes = [];
            try {
                nodes = root.querySelectorAll('input, textarea, [contenteditable="true"], [role="textbox"]');
            } catch (_) {
                continue;
            }
            for (const el of nodes) {
                if (seen.has(el) || !isVisible(el)) continue;
                seen.add(el);
                candidates.push(el);
            }
        }

        const textAround = (el) => {
            const bits = [
                el.getAttribute?.('type'),
                el.getAttribute?.('name'),
                el.getAttribute?.('id'),
                el.getAttribute?.('autocomplete'),
                el.getAttribute?.('placeholder'),
                el.getAttribute?.('aria-label'),
                el.closest?.('label')?.innerText,
            ];
            let root = el.getRootNode?.();
            if (root?.host) {
                bits.push(root.host.getAttribute?.('aria-label'));
                bits.push(root.host.getAttribute?.('name'));
                bits.push(root.host.getAttribute?.('id'));
                bits.push(root.host.innerText);
            }
            let parent = el.parentElement;
            for (let i = 0; parent && i < 3; i++, parent = parent.parentElement) {
                bits.push(parent.getAttribute?.('aria-label'));
                bits.push(parent.innerText);
            }
            return bits.filter(Boolean).join(' ').replace(/\\s+/g, ' ').toLowerCase();
        };

        const score = (el) => {
            const type = String(el.getAttribute?.('type') || '').toLowerCase();
            const haystack = textAround(el);
            let value = 0;
            if (kind === 'password') {
                if (type === 'password') value += 100;
                if (/\\bpassword\\b/.test(haystack)) value += 40;
                if (/current-password/.test(haystack)) value += 30;
                return value;
            }
            if (type === 'password') return -100;
            if (/\\b(email|username|user name)\\b/.test(haystack)) value += 80;
            if (/autocomplete.*(username|email)|\\b(username|email)\\b/.test(haystack)) value += 30;
            if (/phone/.test(haystack)) value -= 25;
            if (/password/.test(haystack)) value -= 80;
            return value;
        };

        let best = null;
        let bestScore = 0;
        for (const el of candidates) {
            const current = score(el);
            if (current > bestScore) {
                best = el;
                bestScore = current;
            }
        }
        return best;
    }""",
        {"kind": kind},
    )
    element = handle.as_element()
    if not element:
        await handle.dispose()
        return None
    return element


async def _login_page_diagnostics(page: Page) -> dict:
    """Small page snapshot for debugging auth shells/interstitials."""
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        dom = await page.evaluate("""() => {
            const visible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden';
            };
            const inputs = [...document.querySelectorAll('input, textarea')]
                .filter(visible)
                .map(el => ({
                    type: el.getAttribute('type') || '',
                    name: el.getAttribute('name') || '',
                    id: el.getAttribute('id') || '',
                    placeholder: el.getAttribute('placeholder') || '',
                    autocomplete: el.getAttribute('autocomplete') || '',
                }));
            const buttons = [...document.querySelectorAll('button, a, [role="button"]')]
                .filter(visible)
                .map(el => (el.textContent || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim())
                .filter(Boolean)
                .slice(0, 10);
            const body = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 240);
            return { inputs, buttons, body };
        }""")
    except Exception as exc:
        dom = {"error": str(exc)}
    return {"url": page.url, "title": title, **(dom or {})}


async def _dismiss_post_login_backdrop(page: Page, account_id: str, log=None) -> dict:
    """Dismiss a stuck Reddit modal scrim after login has already succeeded."""
    try:
        state = await page.evaluate("""() => {
            const isVisible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && parseFloat(style.opacity || '1') > 0;
            };
            const dialogs = [...document.querySelectorAll(
                '[role="dialog"], [aria-modal="true"], auth-flow-modal, shreddit-async-loader[bundlename*="auth" i]'
            )].filter(isVisible);
            const viewportArea = Math.max(1, window.innerWidth * window.innerHeight);
            const scrims = [...document.querySelectorAll('body *')].filter(el => {
                if (!isVisible(el)) return false;
                const rect = el.getBoundingClientRect();
                if ((rect.width * rect.height) < viewportArea * 0.65) return false;
                const style = window.getComputedStyle(el);
                const pos = style.position;
                if (pos !== 'fixed' && pos !== 'absolute') return false;
                const bg = style.backgroundColor || '';
                const opacity = parseFloat(style.opacity || '1');
                const z = parseInt(style.zIndex || '0', 10) || 0;
                const hasDarkBg = /rgba?\\(/.test(bg) && !/(255,\\s*255,\\s*255)/.test(bg);
                return z >= 10 && (opacity < 0.98 || hasDarkBg);
            });
            return {
                hasDialog: dialogs.length > 0,
                scrimCount: scrims.length,
                url: location.href,
                title: document.title,
            };
        }""")
    except Exception:
        return {"dismissed": False, "reason": "state_failed"}

    if not state.get("scrimCount"):
        return {"dismissed": False, "reason": "no_backdrop", **state}

    close_clicked = False
    try:
        close = await page.evaluate("""() => {
            const isVisible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden';
            };
            const candidates = [...document.querySelectorAll('button, [role="button"], a')];
            for (const el of candidates) {
                if (!isVisible(el)) continue;
                const label = (
                    el.getAttribute('aria-label') ||
                    el.getAttribute('title') ||
                    el.textContent ||
                    ''
                ).replace(/\\s+/g, ' ').trim();
                if (/^(close|dismiss|not now|maybe later|continue)$/i.test(label) || /close/i.test(label)) {
                    const rect = el.getBoundingClientRect();
                    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, label };
                }
            }
            return null;
        }""")
        if close:
            close_handle = await page.evaluate_handle(
                """({x, y}) => document.elementFromPoint(x, y)""",
                {"x": close["x"], "y": close["y"]},
            )
            close_el = close_handle.as_element()
            if close_el:
                await _ghost_move_and_click(page, close_el)
                close_clicked = True
                await _delay(account_id, 0.5, 1.0)
            await close_handle.dispose()
    except Exception:
        close_clicked = False

    if not close_clicked:
        try:
            await page.keyboard.press("Escape")
            await _delay(account_id, 0.5, 1.0)
        except Exception:
            pass

    try:
        after = await page.evaluate("""() => {
            const bodyText = (document.body?.innerText || '').replace(/\\s+/g, ' ').slice(0, 160);
            return { bodyText, url: location.href };
        }""")
    except Exception:
        after = {}

    result = {"dismissed": True, "close_clicked": close_clicked, **state, "after": after}
    if log is not None:
        log.info("reddit.login.post_login_backdrop_dismissed", **result)
    return result


async def _read_field_value(element) -> str:
    try:
        return await element.input_value()
    except Exception:
        try:
            return await element.evaluate("el => el.value || el.textContent || ''")
        except Exception:
            return ""


async def _force_exact_value(element, value: str) -> None:
    await element.fill("")
    await element.fill(value)
    try:
        await element.evaluate("""el => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""")
    except Exception:
        pass


async def _ensure_exact_value(element, value: str) -> bool:
    current = await _read_field_value(element)
    if current == value:
        return True
    await _force_exact_value(element, value)
    return await _read_field_value(element) == value


async def _is_logged_in(page: Page, expected_username: str = "") -> bool:
    try:
        state = await reddit_login_state(
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
    """Execute an explicit Reddit login sequence with human-like behavior.

    Sequence:
        1. Navigate to login page
        2. Organic delay + field resolution
        3. Human-like typing with jitter
        4. Solve CAPTCHA (min_score=0.5)
        5. On rejection, retry once with min_score=0.3
    """
    log = logger.bind(account_id=account_id, action="LOGIN")
    start_ms = _ms()
    network_events: list[dict] = []

    async def _capture_response(response) -> None:
        try:
            url = response.url
            if "reddit.com" not in url:
                return
            interesting = any(part in url.lower() for part in ("login", "auth", "account", "api"))
            if response.status < 400 and not interesting:
                return
            event = {"status": response.status, "url": url[:180]}
            if response.status >= 400 and "account/login" in url.lower():
                try:
                    body = (await response.text()).replace("\n", " ").strip()
                    event["body"] = body[:300]
                except Exception:
                    pass
            network_events.append(event)
        except Exception:
            pass

    page.on("response", _capture_response)

    try:
        if db is not None:
            try:
                await db.ensure_account_exists(account_id)
            except Exception:
                pass

        user_sels = [
            '#login-username input', "#login-username",
            'input[name="username"]', 'input[id="username"]',
            'input[autocomplete="username"]', 'input[placeholder*="sername"]',
        ]
        pass_sels = [
            '#login-password input', "#login-password",
            'input[name="password"]', 'input[type="password"]',
            'input[autocomplete="current-password"]',
        ]

        user_el = None
        pass_el = None
        diagnostics: list[dict] = []
        for attempt_index, login_url in enumerate(_login_url_candidates(account_id), start=1):
            log.info("reddit.login.navigating", url=login_url, attempt=attempt_index)
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)

            await _delay(account_id, 2.0, 4.0)
            await _random_scroll(page, account_id, read_min=0.5, read_max=2.0)
            await _delay(account_id, 0.8, 1.8)

            user_el = await _find_login_field(page, "username", user_sels)
            pass_el = await _find_login_field(page, "password", pass_sels) if user_el else None
            if user_el and pass_el:
                break

            diag = await _login_page_diagnostics(page)
            diagnostics.append(diag)
            log.warning(
                "reddit.login.form_not_found",
                url=diag.get("url"),
                title=diag.get("title"),
                input_count=len(diag.get("inputs") or []),
                body=diag.get("body"),
            )

        if not user_el:
            raise RuntimeError(f"Username field not found. Diagnostics: {diagnostics[-2:]}")
        if not pass_el:
            raise RuntimeError(f"Password field not found. Diagnostics: {diagnostics[-2:]}")

        user_el = await _resolve_editable_element(page, user_el)
        pass_el = await _resolve_editable_element(page, pass_el)

        await _ghost_move_and_click(page, user_el)
        await _delay(account_id, 0.2, 0.6)
        await user_el.fill("")
        await _human_type(page, user_el, username, account_id)
        username_ok = await _ensure_exact_value(user_el, username)
        if not username_ok:
            raise RuntimeError("Username field did not retain the exact configured value")

        await _delay(account_id, 0.2, 0.7)
        await _ghost_move_and_click(page, pass_el)
        await _delay(account_id, 0.2, 0.6)
        await pass_el.fill("")
        await _human_type(page, pass_el, password, account_id)
        password_ok = await _ensure_exact_value(pass_el, password)
        if not password_ok:
            raise RuntimeError("Password field did not retain the exact configured value")

        attempt_scores = [0.5, 0.3] if captcha_config and captcha_config.get("api_key") else [0.0]
        is_logged_in = False
        failure_reason = "Login not confirmed after submit"

        for attempt_no, min_score in enumerate(attempt_scores, start=1):
            captcha_token = None
            if captcha_config and captcha_config.get("api_key"):
                try:
                    captcha_token = await solve_login_recaptcha(
                        page=page, account_id=account_id,
                        captcha_config=captcha_config, proxy_config=proxy_config,
                        log=log, min_score=min_score,
                    )
                except Exception as cap_exc:
                    log.warning("reddit.login.captcha_failed", error=str(cap_exc))

            if captcha_token:
                await inject_grecaptcha_override(page, captcha_token)

            await _delay(account_id, 0.5, 1.2)
            await _ghost_move_and_click(page, pass_el)
            await pass_el.press("Enter")
            await _delay(account_id, 1.2, 2.0)

            if await _login_form_still_visible(page):
                submit_el = await _resolve_login_submit_element(page, pass_el)
                if submit_el:
                    await _ghost_move_and_click(page, submit_el)
                    await _delay(account_id, 2.5, 4.5)

            is_logged_in = await _is_logged_in(page, username)

            error_text = await _extract_login_error(page)

            if is_logged_in:
                break

            failure_reason = error_text or "Login not confirmed after submit"
            if network_events:
                failure_reason = f"{failure_reason}. Network: {_format_network_errors(network_events)}"
            log.warning("reddit.login.rejected", min_score=min_score, reason=failure_reason, attempt=attempt_no)

            if "server error" in failure_reason.lower() or "try again later" in failure_reason.lower():
                break

        elapsed = _ms() - start_ms

        if not is_logged_in:
            try:
                await page.wait_for_timeout(3000)
                is_logged_in = await _is_logged_in(page, username)
                if not is_logged_in and "login" in page.url:
                    await page.goto(reddit_url("/", account_id), wait_until="domcontentloaded", timeout=30_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8_000)
                    except Exception:
                        pass
                    is_logged_in = await _is_logged_in(page, username)
                if is_logged_in:
                    failure_reason = ""
            except Exception:
                pass

        if is_logged_in:
            await _dismiss_post_login_backdrop(page, account_id, log)
            if db is not None:
                try:
                    safe_pid = None
                    if proxy_id:
                        row = await db.get_proxy(proxy_id)
                        safe_pid = proxy_id if row else None
                    await db.log_action(
                        account_id=account_id, action_type="LOGIN", result="SUCCESS",
                        target_url=page.url, proxy_id=safe_pid, response_time_ms=elapsed,
                    )
                except Exception:
                    pass
            log.info("reddit.login.success", elapsed_ms=elapsed)
            return _ok({"logged_in": True, "url": page.url})

        if db is not None:
            try:
                safe_pid = None
                if proxy_id:
                    row = await db.get_proxy(proxy_id)
                    safe_pid = proxy_id if row else None
                await db.log_action(
                    account_id=account_id, action_type="LOGIN", result="FAILURE",
                    target_url=page.url, error_message=failure_reason,
                    proxy_id=safe_pid, response_time_ms=elapsed,
                )
            except Exception:
                pass
        log.warning("reddit.login.unconfirmed", reason=failure_reason)
        return _fail(failure_reason, {"url": page.url, "network": network_events[-8:]})

    except Exception as exc:
        elapsed = _ms() - start_ms
        error_msg = str(exc)
        log.error("reddit.login.failed", error=error_msg)
        if db is not None:
            try:
                await db.log_action(
                    account_id=account_id, action_type="LOGIN", result="FAILURE",
                    target_url=getattr(page, "url", ""), error_message=error_msg,
                    response_time_ms=elapsed,
                )
            except Exception:
                pass
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
    """AI agent entry point for Reddit login.

    Args:
        page: Active Playwright page (must have stealth fingerprint injected beforehand).
        account_id: Unique account identifier (used for personality-based timing).
        username: Reddit username.
        password: Reddit password.
        db: Optional database instance for action logging.
        proxy_id: Optional proxy ID for logging correlation.
        captcha_config: Optional dict with keys: provider, api_key.
                        Providers: "2captcha", "anticaptcha", "capsolver".
        proxy_config: Optional dict with proxy details for capsolver.

    Returns:
        {success: bool, data: {logged_in: bool, url: str}, error: str|None}
    """
    return await login(
        page=page, account_id=account_id, username=username, password=password,
        db=db, proxy_id=proxy_id, captcha_config=captcha_config, proxy_config=proxy_config,
    )
