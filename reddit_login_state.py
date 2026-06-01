"""
reddit_login_state.py - shared Reddit login-state detection.

The important rule: links to post authors like /user/someone are not proof
that the active browser session is logged in. Anonymous Reddit pages contain
plenty of user/profile links.
"""

from __future__ import annotations


REDDIT_LOGIN_STATE_SCRIPT = """(expectedUsername) => {
    const expected = String(expectedUsername || '')
        .trim()
        .replace(/^u\\//i, '')
        .replace(/^@/, '')
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
        return /^log in$/i.test(item.text) || /\\b(log in|login)\\b/.test(haystack) && /\\/login\\b/i.test(item.href);
    });
    const hasVisibleSignup = controls.some(item => /^sign up$/i.test(item.text));

    const hasLoggedInControls = controls.some(item => {
        const text = (item.text || '').toLowerCase();
        return (
            text.includes('create post') ||
            text.includes('create a post') ||
            text.includes('open inbox')
        );
    });

    const accountMenuSelectors = [
        '#expand-user-drawer-button',
        'button[id*="USER_DROPDOWN"]',
        'button[aria-label*="profile" i]',
        'button[aria-label*="avatar" i]',
        'button[aria-label*="account" i]',
        'a[href="/settings/account"]',
        'a[href^="/settings"]',
    ];
    const profileMenuVisible = accountMenuSelectors.some(sel => {
        try { return [...document.querySelectorAll(sel)].some(isVisible); }
        catch (_) { return false; }
    });
    const settingsVisible = [...document.querySelectorAll('a[href^="/settings"]')].some(isVisible);

    const userLinks = controls.filter(item => /\\/(user|u)\\//i.test(item.href));
    const expectedUserVisible = expected && userLinks.some(item => {
        try {
            const url = new URL(item.href, window.location.origin);
            const parts = url.pathname.split('/').filter(Boolean);
            const idx = parts.findIndex(part => /^(user|u)$/i.test(part));
            return idx >= 0 && parts[idx + 1] && decodeURIComponent(parts[idx + 1]).toLowerCase() === expected;
        } catch (_) {
            return false;
        }
    });

    const bodyText = (document.body?.innerText || '').replace(/\\s+/g, ' ');
    const logoutVisible = /\\bLog Out\\b/i.test(bodyText);

    return {
        loggedOut: hasVisibleLogin || hasVisibleSignup,
        reason: hasVisibleLogin || hasVisibleSignup ? 'visible_login_or_signup' : 'no_logged_out_control',
        profileMenuVisible,
        settingsVisible,
        expectedUserVisible: Boolean(expectedUserVisible),
        logoutVisible,
        hasLoggedInControls,
        userLinkCount: userLinks.length,
        url: window.location.href,
    };
}"""


def classify_reddit_login_state(
    ui_state: dict | None,
    *,
    has_session_cookie: bool,
    expected_username: str = "",
) -> dict:
    """Classify Reddit auth state from UI clues plus HTTP-only cookies."""
    ui_state = ui_state or {}
    expected_username = (expected_username or "").strip()

    if ui_state.get("loggedOut"):
        return {
            "logged_in": False,
            "reason": ui_state.get("reason", "logged_out_ui"),
            "ui_state": ui_state,
        }

    if has_session_cookie:
        return {
            "logged_in": True,
            "reason": "reddit_session_cookie",
            "ui_state": ui_state,
        }

    if ui_state.get("logoutVisible") or ui_state.get("settingsVisible") or ui_state.get("hasLoggedInControls"):
        return {
            "logged_in": True,
            "reason": "strong_logged_in_ui",
            "ui_state": ui_state,
        }

    if expected_username and ui_state.get("expectedUserVisible") and ui_state.get("profileMenuVisible"):
        return {
            "logged_in": True,
            "reason": "expected_user_in_profile_menu",
            "ui_state": ui_state,
        }

    return {
        "logged_in": False,
        "reason": "no_session_cookie_or_account_menu",
        "ui_state": ui_state,
    }


async def reddit_login_state(page, *, expected_username: str = "", navigate: bool = True) -> dict:
    """Return a strict login-state object for the current Reddit context."""
    if navigate:
        await page.goto("https://www.reddit.com", wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass

    ui_state = await page.evaluate(REDDIT_LOGIN_STATE_SCRIPT, expected_username)
    cookies = await page.context.cookies(["https://www.reddit.com", "https://reddit.com"])
    has_session_cookie = any(
        cookie.get("name") == "reddit_session" and bool(cookie.get("value"))
        for cookie in cookies
    )
    state = classify_reddit_login_state(
        ui_state,
        has_session_cookie=has_session_cookie,
        expected_username=expected_username,
    )
    state["has_session_cookie"] = has_session_cookie
    return state
