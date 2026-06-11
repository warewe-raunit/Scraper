"""
tools/linkedin_login_state.py - shared LinkedIn login-state detection.
"""

from __future__ import annotations
import asyncio


LINKEDIN_LOGIN_STATE_SCRIPT = """(expectedUsername) => {
    const expected = String(expectedUsername || '')
        .trim()
        .toLowerCase();

    const isVisible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return false;
        const style = window.getComputedStyle(el);
        return style.display !== 'none'
            && style.visibility !== 'hidden'
            && parseFloat(style.opacity || '1') > 0;
    };

    const visibleLabel = (el) => {
        if (!isVisible(el)) return '';
        return (
            el.textContent ||
            el.getAttribute('aria-label') ||
            el.getAttribute('title') ||
            ''
        ).replace(/\\s+/g, ' ').trim();
    };

    const controls = [...document.querySelectorAll('a, button, [role="button"], [role="link"]')]
        .map(el => ({
            el,
            text: visibleLabel(el),
            href: el.href || el.getAttribute('href') || '',
            aria: el.getAttribute('aria-label') || '',
        }))
        .filter(item => item.text || item.href || item.aria);

    const hasVisibleLogin = controls.some(item => {
        const haystack = `${item.text} ${item.aria} ${item.href}`.toLowerCase();
        return /^sign in$/i.test(item.text) || /\\b(sign in|signin|login|log in)\\b/.test(haystack) && /\\/login\\b/i.test(item.href);
    });
    const hasVisibleSignup = controls.some(item => /^join now$/i.test(item.text) || /^sign up$/i.test(item.text));

    const hasLoggedInControls = controls.some(item => {
        const text = (item.text || '').toLowerCase();
        return (
            text.includes('start a post') ||
            text.includes('write article') ||
            text.includes('messaging') ||
            text.includes('notifications')
        );
    });

    const accountMenuSelectors = [
        '#global-nav-typeahead',
        'button.global-nav__primary-link[data-nav-item="me"]',
        'button[aria-label*="navigation menu" i]',
        'button[aria-label*="profile" i]',
        'img.global-nav__me-photo',
    ];
    const profileMenuVisible = accountMenuSelectors.some(sel => {
        try { return [...document.querySelectorAll(sel)].some(isVisible); }
        catch (_) { return false; }
    });

    const bodyText = (document.body?.innerText || '').replace(/\\s+/g, ' ');
    const logoutVisible = /\\bSign Out\\b/i.test(bodyText) || /\\bLog Out\\b/i.test(bodyText);

    return {
        loggedOut: hasVisibleLogin && !profileMenuVisible && !hasLoggedInControls,
        reason: (hasVisibleLogin && !profileMenuVisible && !hasLoggedInControls) ? 'visible_login_or_signup' : 'no_logged_out_control',
        profileMenuVisible,
        logoutVisible,
        hasLoggedInControls,
        url: window.location.href,
    };
}"""


def classify_linkedin_login_state(
    ui_state: dict | None,
    *,
    has_session_cookie: bool,
    expected_username: str = "",
) -> dict:
    """Classify LinkedIn auth state from UI clues plus cookies."""
    ui_state = ui_state or {}
    expected_username = (expected_username or "").strip()

    url = ui_state.get("url", "").lower()
    if not url or "linkedin.com" not in url:
        return {
            "logged_in": False,
            "reason": f"not_on_linkedin_domain_url_{url}",
            "ui_state": ui_state,
        }

    if "/checkpoint/" in url or "/login" in url or "/challenge/" in url or "/signup" in url:
        return {
            "logged_in": False,
            "reason": f"on_auth_checkpoint_page_{url}",
            "ui_state": ui_state,
        }

    if ui_state.get("loggedOut"):
        return {
            "logged_in": False,
            "reason": ui_state.get("reason", "logged_out_ui"),
            "ui_state": ui_state,
        }

    if has_session_cookie:
        return {
            "logged_in": True,
            "reason": "linkedin_session_cookie",
            "ui_state": ui_state,
        }

    if ui_state.get("logoutVisible") or ui_state.get("hasLoggedInControls") or ui_state.get("profileMenuVisible"):
        return {
            "logged_in": True,
            "reason": "strong_logged_in_ui",
            "ui_state": ui_state,
        }

    return {
        "logged_in": False,
        "reason": "no_session_cookie_or_account_menu",
        "ui_state": ui_state,
    }


async def linkedin_login_state(page, *, expected_username: str = "", navigate: bool = True) -> dict:
    """Return a strict login-state object for the current LinkedIn context."""
    if navigate:
        try:
            await page.goto("https://www.linkedin.com/feed", wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_load_state("load", timeout=8_000)
        except Exception:
            pass

    ui_state = None
    for attempt in range(3):
        try:
            ui_state = await page.evaluate(LINKEDIN_LOGIN_STATE_SCRIPT, expected_username)
            break
        except Exception as e:
            if "context was destroyed" in str(e).lower() and attempt < 2:
                await asyncio.sleep(1.0)
                continue
            if attempt == 2:
                # Default to loggedOut if evaluation fails repeatedly due to navigation
                ui_state = {"loggedOut": True, "reason": "repeated_navigation_error"}
            else:
                raise

    cookies = await page.context.cookies(["https://www.linkedin.com", "https://linkedin.com"])
    has_session_cookie = any(
        cookie.get("name") == "li_at" and bool(cookie.get("value"))
        for cookie in cookies
    )
    state = classify_linkedin_login_state(
        ui_state,
        has_session_cookie=has_session_cookie,
        expected_username=expected_username,
    )
    state["has_session_cookie"] = has_session_cookie
    return state
