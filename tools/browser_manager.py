"""
browser_manager.py — Launch Playwright browser with full stealth stack wired in.

Stealth layers applied (in order, all before any page.goto()):
  Layer 1: fingerprint.py        — 22 techniques (WebGL, canvas, audio, navigator)
  Layer 2: advanced_fingerprint  — 40 techniques (permissions, timing, matchMedia)
  Layer 3: bot_detection_evasion — 50+ techniques (PerimeterX, Kasada, Cloudflare, Reddit Sentinel)

Post-load:
  BotDetectionEvasionManager.post_load_check() fires on every page 'load' event
  to re-assert patches that anti-bot scripts try to restore on DOMContentLoaded.

Browser context settings (UA, viewport, locale, timezone) come from
BrowserProfileManager.generate(account_id) — deterministic per account.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright
import structlog

from tools.proxy_config import playwright_proxy_config
from tools.session_store import load_session, save_session
from tools.stealth.fingerprint import BrowserProfileManager
from tools.stealth.bot_detection_evasion import BotDetectionEvasionManager

logger = structlog.get_logger(__name__)
ROOT = Path(__file__).resolve().parent.parent
PERSISTENT_PROFILE_ROOT = ROOT / "sessions" / "browser_profiles"

# Cached IP-geolocation lookup per proxy URL. Pixelscan and similar checks
# compare browser-reported timezone against the proxy's IP-geo timezone; a
# mismatch is the single biggest "spoofed" tell. Cache so we only hit the
# geo API once per proxy per process.
_PROXY_GEO_CACHE: dict[str, dict] = {}
# Try multiple providers — each free tier has a tight day-bucket; the chain
# kicks in when the first is rate-limited.
_PROXY_GEO_PROVIDERS = (
    ("ip-api.com", "http://ip-api.com/json/?fields=status,query,timezone,country,countryCode,region,regionName,city"),
    ("ipinfo.io",  "https://ipinfo.io/json"),
    ("ipapi.co",   "https://ipapi.co/json/"),
)


_REDDIT_ORPHAN_BACKDROP_SCRIPT = """(cleanup) => {
    const host = (location.hostname || '').toLowerCase();
    if (!/(^|\\.)reddit\\.com$/.test(host)) {
        return { found: false, cleaned: false, reason: 'non_reddit', host };
    }

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

    const collect = (selector) => {
        const out = [];
        const seen = new Set();
        for (const root of roots) {
            let nodes = [];
            try { nodes = root.querySelectorAll(selector); } catch (_) { continue; }
            for (const node of nodes) {
                if (seen.has(node)) continue;
                seen.add(node);
                out.push(node);
            }
        }
        return out;
    };

    const visibleDialogs = collect([
        '[role="dialog"]',
        '[aria-modal="true"]',
        'auth-flow-modal',
        'faceplate-dialog',
        'faceplate-drawer',
        'shreddit-modal',
        'shreddit-drawer',
        'shreddit-app-promo',
        'shreddit-signup-drawer',
        'shreddit-experience-policy',
        'xpromo-nsfw-blocking-modal',
        'xpromo-app-selector',
        'cookie-consent-banner',
        '.modal',
        '[class*="modal" i]',
        '[class*="drawer" i]',
        '[class*="dialog" i]',
        '[class*="popup" i]',
        '[class*="overlay" i]',
        '[class*="promo" i]'
    ].join(',')).filter(isVisible);
    if (visibleDialogs.length) {
        return {
            found: false,
            cleaned: false,
            reason: 'visible_dialog_present',
            dialogCount: visibleDialogs.length,
        };
    }

    const viewportArea = Math.max(1, window.innerWidth * window.innerHeight);
    const scrims = collect('*').filter(el => {
        if (el === document.documentElement || el === document.body) return false;
        if (!isVisible(el)) return false;
        const rect = el.getBoundingClientRect();
        if ((rect.width * rect.height) < viewportArea * 0.65) return false;
        if (rect.right < window.innerWidth * 0.65 || rect.bottom < window.innerHeight * 0.65) return false;

        const style = window.getComputedStyle(el);
        if (style.position !== 'fixed' && style.position !== 'absolute') return false;
        const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
        if (text.length > 80) return false;

        const z = parseInt(style.zIndex || '0', 10) || 0;
        const opacity = parseFloat(style.opacity || '1');
        const bg = style.backgroundColor || '';
        const hasDarkBg = /rgba?\\(/.test(bg) && !/(255,\\s*255,\\s*255)/.test(bg);
        const name = [
            el.id || '',
            el.className || '',
            ...[...el.attributes].map(attr => `${attr.name}=${attr.value}`),
        ].join(' ');
        const namedBackdrop = /backdrop|scrim|overlay|modal|drawer|veil/i.test(name);
        const interceptsInput = style.pointerEvents !== 'none';
        return (z >= 10 || namedBackdrop || interceptsInput)
            && (namedBackdrop || opacity < 0.98 || hasDarkBg);
    });

    if (!scrims.length) {
        return { found: false, cleaned: false, reason: 'no_orphan_backdrop' };
    }
    if (!cleanup) {
        return { found: true, cleaned: false, reason: 'orphan_backdrop_detected', scrimCount: scrims.length };
    }

    for (const el of scrims) {
        try { el.setAttribute('data-redditagent-removed-backdrop', 'true'); } catch (_) {}
        try { el.remove(); } catch (_) {
            try {
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('pointer-events', 'none', 'important');
            } catch (_) {}
        }
    }
    for (const el of [document.documentElement, document.body]) {
        if (!el) continue;
        try {
            el.style.removeProperty('overflow');
            el.style.removeProperty('position');
            el.style.removeProperty('pointer-events');
        } catch (_) {}
    }
    return { found: true, cleaned: true, reason: 'orphan_backdrop_removed', scrimCount: scrims.length };
}"""


_REDDIT_ORPHAN_BACKDROP_WATCHER_SCRIPT = """
(() => {
    if (window.__redditAgentBackdropWatcherInstalled) return;
    window.__redditAgentBackdropWatcherInstalled = true;

    const isReddit = () => /(^|\\.)reddit\\.com$/.test((location.hostname || '').toLowerCase());
    const isVisible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return false;
        const style = window.getComputedStyle(el);
        return style.display !== 'none'
            && style.visibility !== 'hidden'
            && parseFloat(style.opacity || '1') > 0;
    };
    const roots = () => {
        const out = [document];
        const scan = (root) => {
            let nodes = [];
            try { nodes = root.querySelectorAll ? root.querySelectorAll('*') : []; } catch (_) { return; }
            for (const node of nodes) {
                if (node.shadowRoot && !out.includes(node.shadowRoot)) {
                    out.push(node.shadowRoot);
                    scan(node.shadowRoot);
                }
            }
        };
        scan(document);
        return out;
    };
    const collect = (selector) => {
        const out = [];
        const seen = new Set();
        for (const root of roots()) {
            let nodes = [];
            try { nodes = root.querySelectorAll(selector); } catch (_) { continue; }
            for (const node of nodes) {
                if (seen.has(node)) continue;
                seen.add(node);
                out.push(node);
            }
        }
        return out;
    };
    const hasVisibleDialog = () => collect([
        '[role="dialog"]',
        '[aria-modal="true"]',
        'auth-flow-modal',
        'faceplate-dialog',
        'faceplate-drawer',
        'shreddit-modal',
        'shreddit-drawer',
        'shreddit-app-promo',
        'shreddit-signup-drawer',
        'shreddit-experience-policy',
        'xpromo-nsfw-blocking-modal',
        'xpromo-app-selector',
        'cookie-consent-banner',
        '.modal',
        '[class*="modal" i]',
        '[class*="drawer" i]',
        '[class*="dialog" i]',
        '[class*="popup" i]',
        '[class*="overlay" i]',
        '[class*="promo" i]'
    ].join(',')).some(isVisible);

    window.__redditAgentCleanOrphanBackdrops = () => {
        if (!isReddit() || hasVisibleDialog()) return { found: false, cleaned: false };

        const viewportArea = Math.max(1, window.innerWidth * window.innerHeight);
        const scrims = collect('*').filter((el) => {
            if (el === document.documentElement || el === document.body) return false;
            if (!isVisible(el)) return false;
            const rect = el.getBoundingClientRect();
            if ((rect.width * rect.height) < viewportArea * 0.65) return false;
            if (rect.right < window.innerWidth * 0.65 || rect.bottom < window.innerHeight * 0.65) return false;

            const style = window.getComputedStyle(el);
            if (style.position !== 'fixed' && style.position !== 'absolute') return false;
            const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            if (text.length > 80) return false;

            const z = parseInt(style.zIndex || '0', 10) || 0;
            const opacity = parseFloat(style.opacity || '1');
            const bg = style.backgroundColor || '';
            const hasDarkBg = /rgba?\\(/.test(bg) && !/(255,\\s*255,\\s*255)/.test(bg);
            const name = [
                el.id || '',
                el.className || '',
                ...[...el.attributes].map((attr) => `${attr.name}=${attr.value}`),
            ].join(' ');
            const namedBackdrop = /backdrop|scrim|overlay|modal|drawer|veil/i.test(name);
            const interceptsInput = style.pointerEvents !== 'none';
            return (z >= 10 || namedBackdrop || interceptsInput)
                && (namedBackdrop || opacity < 0.98 || hasDarkBg);
        });

        for (const el of scrims) {
            try { el.setAttribute('data-redditagent-removed-backdrop', 'true'); } catch (_) {}
            try { el.remove(); } catch (_) {
                try {
                    el.style.setProperty('display', 'none', 'important');
                    el.style.setProperty('pointer-events', 'none', 'important');
                } catch (_) {}
            }
        }
        if (scrims.length) {
            for (const el of [document.documentElement, document.body]) {
                if (!el) continue;
                try {
                    el.style.removeProperty('overflow');
                    el.style.removeProperty('position');
                    el.style.removeProperty('pointer-events');
                } catch (_) {}
            }
        }
        return { found: Boolean(scrims.length), cleaned: Boolean(scrims.length), scrimCount: scrims.length };
    };

    let pending = null;
    const schedule = () => {
        if (pending) return;
        pending = setTimeout(() => {
            pending = null;
            try { window.__redditAgentCleanOrphanBackdrops(); } catch (_) {}
        }, 90);
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', schedule, { once: true });
    } else {
        schedule();
    }

    try {
        new MutationObserver(schedule).observe(document.documentElement || document, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['style', 'class', 'aria-hidden', 'inert', 'open'],
        });
    } catch (_) {}

    let eagerScans = 0;
    const eagerTimer = setInterval(() => {
        eagerScans += 1;
        try { window.__redditAgentCleanOrphanBackdrops(); } catch (_) {}
        if (eagerScans >= 24) clearInterval(eagerTimer);
    }, 750);
    setInterval(() => {
        try { window.__redditAgentCleanOrphanBackdrops(); } catch (_) {}
    }, 2500);
})();
"""


def _install_playwright_frame_detach_guard() -> None:
    """Ignore duplicate Chromium frame-detach events that Playwright may replay.

    Playwright 1.59 can raise ValueError from its internal frame list when a
    fast-moving page delivers the same detach event twice. Reddit pages churn
    iframes aggressively, so make the handler idempotent for that one case.
    """
    try:
        from playwright._impl._page import Page as ImplPage
    except Exception as exc:
        logger.debug("playwright_frame_detach_guard_unavailable", error=str(exc))
        return

    if getattr(ImplPage, "_redditagent_frame_detach_guard", False):
        return

    original = ImplPage._on_frame_detached

    def _safe_on_frame_detached(self, frame):
        frames = getattr(self, "_frames", None)
        if frames is not None and frame not in frames:
            try:
                frame._detached = True
            except Exception:
                pass
            logger.debug(
                "playwright_duplicate_frame_detach_ignored",
                page_url=getattr(self, "url", ""),
            )
            return None
        return original(self, frame)

    ImplPage._on_frame_detached = _safe_on_frame_detached
    ImplPage._redditagent_frame_detach_guard = True


_install_playwright_frame_detach_guard()


def _normalize_geo_payload(provider: str, raw: dict) -> dict:
    if provider == "ip-api.com":
        if raw.get("status") and raw["status"] != "success":
            return {}
        return {
            "ip": raw.get("query"),
            "timezone": raw.get("timezone"),
            "country_code": raw.get("countryCode"),
            "country_name": raw.get("country"),
            "region": raw.get("regionName") or raw.get("region"),
            "city": raw.get("city"),
        }
    if provider == "ipinfo.io":
        return {
            "ip": raw.get("ip"),
            "timezone": raw.get("timezone"),
            "country_code": raw.get("country"),
            "country_name": None,
            "region": raw.get("region"),
            "city": raw.get("city"),
        }
    # ipapi.co
    return {
        "ip": raw.get("ip"),
        "timezone": raw.get("timezone"),
        "country_code": raw.get("country_code") or raw.get("country"),
        "country_name": raw.get("country_name"),
        "region": raw.get("region"),
        "city": raw.get("city"),
        "languages": raw.get("languages"),
    }


async def _resolve_proxy_geo(proxy_url: Optional[str], timeout: float = 2.5) -> Optional[dict]:
    """Look up IP-geo for the proxy's exit. Returns None on any failure.

    Chains across ip-api.com → ipinfo.io → ipapi.co so a single provider's
    free-tier rate limit doesn't disable the override.
    """
    if not proxy_url:
        return None
    cached = _PROXY_GEO_CACHE.get(proxy_url)
    if cached is not None:
        return cached or None
    last_error: str = ""
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as cli:
            for provider, url in _PROXY_GEO_PROVIDERS:
                try:
                    resp = await cli.get(url)
                except Exception as exc:
                    last_error = f"{provider}: {exc!r}"
                    continue
                if resp.status_code != 200:
                    last_error = f"{provider}: HTTP {resp.status_code}"
                    continue
                try:
                    raw = resp.json() or {}
                except Exception as exc:
                    last_error = f"{provider}: bad json {exc!r}"
                    continue
                geo = _normalize_geo_payload(provider, raw)
                if geo.get("timezone"):
                    geo["_provider"] = provider
                    _PROXY_GEO_CACHE[proxy_url] = geo
                    return geo
                last_error = f"{provider}: no timezone in payload"
    except Exception as exc:
        last_error = f"client: {exc!r}"
    logger.warning("proxy_geo_lookup_failed", error=last_error or "no provider succeeded")
    # Cache failure to prevent repeated slow timeouts on subsequent launches
    _PROXY_GEO_CACHE[proxy_url] = {}
    return None


async def _align_profile_to_proxy_geo(profile: dict, proxy_url: Optional[str]) -> Optional[dict]:
    """Override profile.timezone with the proxy IP's actual timezone.

    Kills Pixelscan's "Timezone spoofed" flag (and similar checks that compare
    browser tz vs IP geo). Disable with BROWSER_PROXY_TZ_OVERRIDE=false.
    """
    if not proxy_url or not _env_bool("BROWSER_PROXY_TZ_OVERRIDE", True):
        return None
    geo = await _resolve_proxy_geo(proxy_url)
    if not geo:
        return None
    new_tz = geo.get("timezone")
    if not new_tz:
        return geo
    old_tz = profile.get("timezone")
    if old_tz != new_tz:
        logger.info(
            "proxy_tz_override",
            from_tz=old_tz,
            to_tz=new_tz,
            ip=geo.get("ip"),
            country=geo.get("country_code"),
            city=geo.get("city"),
        )
        profile["timezone"] = new_tz
    return geo


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def _install_reddit_orphan_backdrop_watcher(page: Page) -> None:
    """Install a live Reddit scrim cleaner for in-page overlays after clicks."""
    if not _env_bool("REDDIT_CLEAN_ORPHAN_BACKDROPS", True):
        return

    try:
        await page.add_init_script(_REDDIT_ORPHAN_BACKDROP_WATCHER_SCRIPT)
    except Exception:
        pass

    try:
        if not page.is_closed():
            await page.evaluate(_REDDIT_ORPHAN_BACKDROP_WATCHER_SCRIPT)
    except Exception:
        pass


async def _cleanup_reddit_orphan_backdrop(page: Page, account_id: str) -> dict:
    """Remove Reddit's occasional stuck dark scrim when no dialog is visible."""
    if not _env_bool("REDDIT_CLEAN_ORPHAN_BACKDROPS", True):
        return {"cleaned": False, "reason": "disabled"}
    if page.is_closed():
        return {"cleaned": False, "reason": "page_closed"}

    try:
        state = await page.evaluate(_REDDIT_ORPHAN_BACKDROP_SCRIPT, False)
    except Exception as exc:
        return {"cleaned": False, "reason": "detect_failed", "error": str(exc)[:180]}

    if not isinstance(state, dict) or not state.get("found"):
        return state if isinstance(state, dict) else {"cleaned": False, "reason": "unexpected_state"}

    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.25)
    except Exception:
        pass

    try:
        after_escape = await page.evaluate(_REDDIT_ORPHAN_BACKDROP_SCRIPT, False)
    except Exception:
        after_escape = None
    if isinstance(after_escape, dict) and not after_escape.get("found"):
        result = {
            "cleaned": True,
            "method": "escape",
            "reason": "orphan_backdrop_dismissed",
            "before": state,
            "after": after_escape,
        }
        logger.info("reddit.orphan_backdrop_cleaned", account_id=account_id, **result)
        return result

    try:
        cleaned = await page.evaluate(_REDDIT_ORPHAN_BACKDROP_SCRIPT, True)
    except Exception as exc:
        return {
            "cleaned": False,
            "reason": "cleanup_failed",
            "error": str(exc)[:180],
            "before": state,
        }

    result = cleaned if isinstance(cleaned, dict) else {"cleaned": False, "reason": "unexpected_cleanup_state"}
    if result.get("cleaned"):
        logger.info("reddit.orphan_backdrop_cleaned", account_id=account_id, method="remove", before=state, **result)
    return result


def _chrome_major_from_ua(user_agent: str) -> str:
    match = re.search(r"Chrome/(\d+)", str(user_agent or ""))
    return match.group(1) if match else ""


def _chrome_major_from_version(version: str) -> str:
    match = re.search(r"(\d+)(?:\.\d+)+", str(version or ""))
    return match.group(1) if match else ""


def _browser_runtime_version(browser: Browser) -> str:
    runtime_version = browser.version
    if callable(runtime_version):
        runtime_version = runtime_version()
    return str(runtime_version or "")


def _context_browser(context: BrowserContext) -> Browser | None:
    browser = getattr(context, "browser", None)
    if callable(browser):
        browser = browser()
    return browser


def _persistent_profile_dir(session_id: str) -> Path:
    safe_session_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id).strip("_") or "profile"
    return PERSISTENT_PROFILE_ROOT / safe_session_id


def _quote_ch(value: object) -> str:
    raw = str(value or "").strip().strip('"')
    return f'"{raw}"'


def _sec_ch_full_version_list(profile: dict) -> str:
    full_version = str(profile.get("chrome_full_version") or "").strip()
    major = _chrome_major_from_ua(str(profile.get("user_agent") or "")) or _chrome_major_from_version(full_version)
    if not full_version:
        full_version = f"{major or '120'}.0.0.0"

    pairs = re.findall(r'"([^"]+)";v="([^"]+)"', str(profile.get("sec_ch_ua") or ""))
    if not pairs:
        pairs = [("Not_A Brand", "8"), ("Chromium", major or "120"), ("Google Chrome", major or "120")]

    full_pairs = []
    for brand, version in pairs:
        if brand in {"Chromium", "Google Chrome"}:
            full_pairs.append((brand, full_version))
        else:
            full_pairs.append((brand, f"{version}.0.0.0" if "." not in version else version))
    return ", ".join(f'"{brand}";v="{version}"' for brand, version in full_pairs)


def _profile_http_headers(profile: dict) -> dict:
    """Return Client Hint headers aligned with the JS fingerprint profile."""
    screen = profile.get("screen_resolution") or {}
    width = int(screen.get("width") or 0)
    height = int(screen.get("height") or 0)
    dpr = str(profile.get("device_scale_factor") or 1)
    connection = profile.get("connection") or {}
    chrome_full_version = str(profile.get("chrome_full_version") or "").strip()
    if not chrome_full_version:
        chrome_major = _chrome_major_from_ua(str(profile.get("user_agent") or "")) or "120"
        chrome_full_version = f"{chrome_major}.0.0.0"

    headers = {
        "Accept-Language": f"{profile['locale']},en;q=0.9",
        "sec-ch-ua": profile["sec_ch_ua"],
        "sec-ch-ua-mobile": profile["sec_ch_ua_mobile"],
        "sec-ch-ua-platform": profile["sec_ch_ua_platform"],
        "sec-ch-ua-full-version": _quote_ch(chrome_full_version),
        "sec-ch-ua-full-version-list": _sec_ch_full_version_list(profile),
        "sec-ch-ua-platform-version": _quote_ch(profile.get("mobile_platform_version", "")),
        "sec-ch-ua-arch": _quote_ch(profile.get("architecture", "")),
        "sec-ch-ua-bitness": _quote_ch(profile.get("bitness", "")),
        "sec-ch-ua-wow64": "?0",
        "sec-ch-ua-model": _quote_ch(profile.get("mobile_model", "")),
        "sec-ch-ua-form-factors": _quote_ch("Mobile" if profile.get("is_mobile") else "Desktop"),
        "viewport-width": str(width),
        "sec-ch-viewport-width": str(width),
        "sec-ch-viewport-height": str(height),
        "dpr": dpr,
        "sec-ch-dpr": dpr,
        "device-memory": str(profile.get("device_memory") or 8),
        "sec-ch-device-memory": str(profile.get("device_memory") or 8),
        "ect": str(connection.get("effectiveType") or "4g"),
        "rtt": str(connection.get("rtt") or 80),
        "downlink": str(connection.get("downlink") or 12),
    }
    if connection.get("saveData"):
        headers["save-data"] = "on"
        headers["sec-ch-save-data"] = "on"
    return headers


def profile_session_id(account_id: str, profile: dict) -> str:
    """Return the storage-state key for this account/profile pairing."""
    category = str(profile.get("device_category") or "desktop").strip().lower()
    is_mobile = bool(profile.get("is_mobile", False))
    if category == "desktop" and not is_mobile:
        return account_id
    safe_category = re.sub(r"[^A-Za-z0-9_.-]+", "_", category).strip("_") or "profile"
    return f"{account_id}__{safe_category}"


async def _validate_runtime_profile(browser: Browser, profile: dict) -> None:
    """Prevent silent drift between generated profile and actual browser."""
    if not _env_bool("BROWSER_VALIDATE_RUNTIME_PROFILE", True):
        return
    ua_major = _chrome_major_from_ua(str(profile.get("user_agent") or ""))
    runtime_version = _browser_runtime_version(browser)
    runtime_major = _chrome_major_from_version(str(runtime_version))
    if not ua_major or not runtime_major or ua_major == runtime_major:
        return

    message = (
        "Browser profile Chrome major does not match Playwright Chromium major "
        f"(profile={ua_major}, runtime={runtime_major}). Update BROWSER_USER_AGENT "
        "and BROWSER_SEC_CH_UA to match the installed browser, install a matching "
        "browser runtime, or set BROWSER_VALIDATE_RUNTIME_PROFILE=false to allow "
        "the inconsistent profile explicitly."
    )
    if _env_bool("BROWSER_STRICT_PROFILE_CONSISTENCY", True):
        raise RuntimeError(message)
    logger.warning(
        "browser_profile_runtime_mismatch",
        profile_chrome_major=ua_major,
        runtime_chrome_major=runtime_major,
    )


def active_profile_session_id(account_id: str) -> str:
    """Return the storage-state key for the currently configured profile."""
    profile = BrowserProfileManager().generate(account_id)
    return profile_session_id(account_id, profile)


_PLAYWRIGHT_CHROMIUM_VERSION: Optional[str] = None


async def _vet_rotating_proxy(gp: Any) -> Optional[str]:
    """Pick the first goodproxy that can complete an HTTPS request to a
    LinkedIn-class target. Tests proxies in parallel batches. Bad proxies get
    pushed back into the cooldown pool so the next call doesn't redraw them.

    Tunables (env):
        BROWSER_PROXY_VET_TARGET   — URL to test against (default linkedin robots)
        BROWSER_PROXY_VET_MAX      — how many proxies to try total (default 60)
        BROWSER_PROXY_VET_BATCH    — parallel checks per round (default 12)
        BROWSER_PROXY_VET_TIMEOUT  — per-proxy timeout seconds (default 5)
    """
    try:
        from curl_cffi import requests as ccff_requests
    except Exception:
        return None

    target = os.getenv("BROWSER_PROXY_VET_TARGET", "https://www.linkedin.com/robots.txt").strip()
    max_total = int(os.getenv("BROWSER_PROXY_VET_MAX", "60"))
    batch_size = int(os.getenv("BROWSER_PROXY_VET_BATCH", "12"))
    timeout = int(os.getenv("BROWSER_PROXY_VET_TIMEOUT", "5"))

    async def _check(p: str) -> Optional[str]:
        loop = asyncio.get_running_loop()
        try:
            r = await loop.run_in_executor(
                None,
                lambda: ccff_requests.get(
                    target,
                    proxies={"http": p, "https": p},
                    impersonate="chrome120",
                    timeout=timeout,
                ),
            )
            if 200 <= r.status_code < 400 and len(r.text or "") > 50:
                return p
        except Exception:
            pass
        try:
            gp.mark_failed(p)
        except Exception:
            pass
        return None

    checked = 0
    while checked < max_total:
        batch = []
        for _ in range(batch_size):
            p = await gp.get_proxy()
            if p:
                batch.append(p)
        if not batch:
            return None
        results = await asyncio.gather(*[_check(p) for p in batch])
        checked += len(batch)
        for r in results:
            if r:
                return r
    return None


async def launch_browser(
    account_id: str,
    proxy_url: Optional[str] = None,
    headless: bool = False,
    use_rotating_proxy: bool = True,
) -> tuple[Playwright, Any, BrowserContext, Page]:
    logger.info("browser_manager.launch_browser.start", account_id=account_id, headless=headless, proxy_url=proxy_url, use_rotating_proxy=use_rotating_proxy)
    pw = await async_playwright().start()

    global _PLAYWRIGHT_CHROMIUM_VERSION
    if _PLAYWRIGHT_CHROMIUM_VERSION is None:
        try:
            temp_browser = await pw.chromium.launch(headless=True)
            _PLAYWRIGHT_CHROMIUM_VERSION = _browser_runtime_version(temp_browser)
            await temp_browser.close()
        except Exception:
            _PLAYWRIGHT_CHROMIUM_VERSION = "148.0.7778.96"

    profile_mgr = BrowserProfileManager()
    profile = profile_mgr.generate(account_id)

    # Resolve Proxy (support global rotating proxy override).
    #
    # For rotating proxies, vet candidates concurrently with curl_cffi against
    # a cheap HTTPS endpoint before handing off to Chromium. Public goodproxies
    # have ~3% hit rate and Chromium's proxy stack is stricter than curl_cffi,
    # so picking one blind triggers ERR_CONNECTION_RESET / ERR_TUNNEL on most
    # launches. Pre-vetting keeps the same Reddit-style "rotating pool" flow
    # but avoids the lottery.
    from tools.goodproxies import GoodProxiesProvider
    gp = GoodProxiesProvider()
    resolved_proxy = proxy_url
    if resolved_proxy == "direct":
        resolved_proxy = None
    elif use_rotating_proxy and gp.enabled and gp.api_key:
        resolved_proxy = await _vet_rotating_proxy(gp)
        if resolved_proxy:
            logger.info("browser_manager.launch_browser.using_rotating_proxy", proxy=resolved_proxy[:30] + "...")
        else:
            logger.warning("browser_manager.launch_browser.no_working_proxy")
            resolved_proxy = None

    await _align_profile_to_proxy_geo(profile, resolved_proxy)

    if _PLAYWRIGHT_CHROMIUM_VERSION:
        profile["chrome_full_version"] = _PLAYWRIGHT_CHROMIUM_VERSION
        runtime_major = _PLAYWRIGHT_CHROMIUM_VERSION.split(".")[0]
        ua = str(profile.get("user_agent") or "")
        if "Chrome/" in ua:
            profile["user_agent"] = re.sub(r"Chrome/\d+", f"Chrome/{runtime_major}", ua)
        from tools.stealth.fingerprint import _SEC_CH_UA_MAP, _DEFAULT_MOBILE_SEC_CH_UA
        if runtime_major in _SEC_CH_UA_MAP:
            profile["sec_ch_ua"] = _SEC_CH_UA_MAP[runtime_major]

    screen = profile["screen_resolution"]
    session_id = profile_session_id(account_id, profile)

    launch_args: dict = {
        "headless": headless,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            f"--window-size={screen['width']},{screen['height']}",
        ],
    }
    proxy_config = playwright_proxy_config(resolved_proxy)
    if proxy_config:
        launch_args["proxy"] = proxy_config

    exec_path = os.getenv("BROWSER_EXECUTABLE_PATH", "").strip() or None
    if exec_path:
        launch_args["executable_path"] = exec_path

    saved_session = load_session(session_id)
    logger.info(
        "browser_manager.session_loaded",
        session_id=session_id,
        exists=bool(saved_session),
        cookies_count=len(saved_session.get("cookies", [])) if saved_session else 0,
    )
    viewport_height = screen["height"]
    if bool(profile.get("is_mobile", False)):
        viewport_height = max(360, screen["height"] - 80)

    context_args: dict = {
        "viewport": {"width": screen["width"], "height": viewport_height},
        "screen": {"width": screen["width"], "height": screen["height"]},
        "user_agent": profile["user_agent"],
        "locale": profile["locale"],
        "timezone_id": profile["timezone"],
        "device_scale_factor": profile["device_scale_factor"],
        "is_mobile": bool(profile.get("is_mobile", False)),
        "has_touch": bool(profile.get("has_touch", False)),
        "extra_http_headers": _profile_http_headers(profile),
        "service_workers": "block" if _env_bool("BROWSER_BLOCK_SERVICE_WORKERS", True) else "allow",
    }

    cdp_ws_url = os.getenv("BROWSER_CDP_WS_URL", "").strip() or None
    if cdp_ws_url:
        logger.info("connecting_to_cdp_browser", cdp_url=cdp_ws_url)
        browser = await pw.chromium.connect_over_cdp(cdp_ws_url)
        contexts = browser.contexts
        if contexts:
            context = contexts[0]
            try:
                await context.set_extra_http_headers(_profile_http_headers(profile))
            except Exception:
                pass
            if saved_session and saved_session.get("cookies"):
                try:
                    await context.add_cookies(saved_session["cookies"])
                except Exception:
                    pass
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            context = await browser.new_context(**context_args)
            page = await context.new_page()
    else:
        use_persistent_context = _env_bool("BROWSER_PERSISTENT_CONTEXT", True)

        if use_persistent_context:
            profile_dir = _persistent_profile_dir(session_id)
            profile_dir.mkdir(parents=True, exist_ok=True)
            persistent_args = {**launch_args, **context_args}
            persistent_args.pop("extra_http_headers", None)
            context = await pw.chromium.launch_persistent_context(str(profile_dir), **persistent_args)
            runtime_browser = _context_browser(context)
            browser = runtime_browser or context
            try:
                if runtime_browser:
                    await _validate_runtime_profile(runtime_browser, profile)
                    runtime_version = _browser_runtime_version(runtime_browser)
                    if runtime_version:
                        profile["chrome_full_version"] = runtime_version
                await context.set_extra_http_headers(_profile_http_headers(profile))
                if saved_session and saved_session.get("cookies"):
                    await context.add_cookies(saved_session["cookies"])
            except Exception:
                await context.close()
                await pw.stop()
                raise

            page = context.pages[0] if context.pages else await context.new_page()
        else:
            browser = await pw.chromium.launch(**launch_args)
            try:
                await _validate_runtime_profile(browser, profile)
            except Exception:
                await browser.close()
                await pw.stop()
                raise
            runtime_version = _browser_runtime_version(browser)
            if runtime_version:
                profile["chrome_full_version"] = runtime_version

            context_args["extra_http_headers"] = _profile_http_headers(profile)
            if saved_session:
                context_args["storage_state"] = saved_session

            context = await browser.new_context(**context_args)
            page = await context.new_page()

    # Reddit-specific orphan backdrop watcher — only install for Reddit pages
    await _install_reddit_orphan_backdrop_watcher(page)

    evasion_mgr = BotDetectionEvasionManager()
    await evasion_mgr.inject_all(page, profile)

    loop = asyncio.get_running_loop()

    def _is_reddit_page(p: Page) -> bool:
        """Check if the page is currently on a Reddit domain."""
        try:
            url = p.url or ""
            return "reddit.com" in url.lower()
        except Exception:
            return False

    async def _post_load_check(p: Page) -> None:
        try:
            await evasion_mgr.post_load_check(p)
        except Exception:
            pass
        # Only run Reddit backdrop cleanup on Reddit pages
        if _is_reddit_page(p):
            await _post_navigation_cleanup(p)

    async def _post_navigation_cleanup(p: Page) -> None:
        for delay_s in (0.0, 0.8, 2.0):
            try:
                if delay_s:
                    await asyncio.sleep(delay_s)
                if p.is_closed():
                    return
                await _cleanup_reddit_orphan_backdrop(p, account_id)
            except Exception:
                pass

    page.on("load", lambda p: loop.create_task(_post_load_check(p)))
    page.on("domcontentloaded", lambda p: loop.create_task(
        _post_navigation_cleanup(p) if _is_reddit_page(p) else asyncio.sleep(0)
    ))
    page.on(
        "framenavigated",
        lambda frame: (
            loop.create_task(_post_navigation_cleanup(page))
            if frame == page.main_frame and _is_reddit_page(page) else None
        ),
    )

    logger.info("browser_manager.launch_browser.complete", account_id=account_id, session_id=session_id)
    # Stash resolved proxy on the context so callers can detect which goodproxy
    # was actually used (needed to mark_failed on navigation errors).
    try:
        setattr(context, "_resolved_proxy", resolved_proxy)
    except Exception:
        pass
    return pw, browser, context, page


async def persist_session(account_id: str, context: BrowserContext) -> None:
    profile = BrowserProfileManager().generate(account_id)
    session_id = profile_session_id(account_id, profile)
    logger.info("browser_manager.persist_session.start", account_id=account_id, session_id=session_id)
    state = await context.storage_state()
    # Save user agent and Client Hints headers to align HTTP clients with browser fingerprints
    state["_user_agent"] = profile.get("user_agent")
    state["_extra_http_headers"] = _profile_http_headers(profile)
    save_session(session_id, state)
    logger.info(
        "browser_manager.persist_session.complete",
        account_id=account_id,
        session_id=session_id,
        cookies_count=len(state.get("cookies", [])),
    )


async def close_browser(pw: Playwright, browser: Any) -> None:
    await browser.close()
    await pw.stop()


class LazyBrowser:
    """Browser that launches only on first tool call, stays open for the session."""

    def __init__(self, account_id: str, proxy_url: Optional[str] = None, headless: bool = False, use_rotating_proxy: bool = True):
        self.account_id = account_id
        self.proxy_url = proxy_url
        self.headless = headless
        self.use_rotating_proxy = use_rotating_proxy
        self.last_used_proxy: Optional[str] = None
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Any] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._launch_lock = asyncio.Lock()

    @property
    def launched(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    async def _reset_closed(self) -> None:
        browser = self._browser
        pw = self._pw
        self._pw = self._browser = self._context = self._page = None
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass

    async def get_page(self) -> Page:
        async with self._launch_lock:
            if self._page is not None:
                browser_disconnected = (
                    self._browser is not None
                    and hasattr(self._browser, "is_connected")
                    and not self._browser.is_connected()
                )
                if self._page.is_closed() or browser_disconnected:
                    await self._reset_closed()

            if self._page is None:
                self._pw, self._browser, self._context, self._page = await launch_browser(
                    self.account_id, self.proxy_url, self.headless, self.use_rotating_proxy
                )
                try:
                    self.last_used_proxy = getattr(self._context, "_resolved_proxy", None)
                except Exception:
                    self.last_used_proxy = None
            return self._page

    async def get_context(self) -> BrowserContext:
        await self.get_page()
        return self._context  # type: ignore[return-value]

    async def persist_session(self) -> None:
        """Persist the current browser context, if it has been launched."""
        if self._context is not None:
            await persist_session(self.account_id, self._context)

    async def close(self) -> None:
        if self._browser is not None:
            try:
                await self.persist_session()
            except Exception:
                pass
            await close_browser(self._pw, self._browser)  # type: ignore[arg-type]
            self._pw = self._browser = self._context = self._page = None
