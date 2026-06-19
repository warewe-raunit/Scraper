"""
tools/unauth_x_scraper.py — Unauthenticated X (Twitter) scraper using Nitter proxy instances.

Uses public Nitter/X-proxy instances combined with the Playwright stealth stack to bypass
Cloudflare and Turnstile blocks, extracting structured profiles and tweets.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
import urllib.parse
import hashlib
from collections import OrderedDict
from typing import Optional, List, Dict, Any, Tuple

import structlog
from playwright.async_api import Page
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from tools.browser_manager import LazyBrowser, active_profile_session_id
from tools.session_store import load_session, save_session, session_age_seconds
from tools.proxy_provider import get_proxy_provider
from tools.stealth.fingerprint import BrowserProfileManager
from tools.stealth.helpers import _delay

logger = structlog.get_logger(__name__)


class _PooledBrowser:
    """A warm LazyBrowser plus the lock that serializes access to its page."""

    __slots__ = ("lazy", "lock", "last_used")

    def __init__(self, lazy: LazyBrowser):
        self.lazy = lazy
        self.lock = asyncio.Lock()
        self.last_used = time.monotonic()


class XBrowserPool:
    """Keeps Playwright browsers warm across requests instead of launching +
    closing one per scrape (the 110-script stealth launch costs 5-15s).

    A browser is pinned to its proxy at launch, so entries are keyed by
    (account_id, proxy_url, headless). With the small rotating proxy pool the
    same proxies recur often, so reuse is real. Concurrency is handled by a
    per-browser lock (a LazyBrowser has a single page; concurrent navigation
    would corrupt it), and the pool is bounded with LRU + idle-TTL eviction so
    Chromium processes don't accumulate. Evicted/idle browsers are closed (which
    also persists their session); everything is closed on shutdown via close_all.
    """

    def __init__(self) -> None:
        self.max_size = max(1, int(os.getenv("X_BROWSER_POOL_MAX", "4")))
        self.idle_ttl = float(os.getenv("X_BROWSER_IDLE_TTL", "300"))
        self._entries: "OrderedDict[Tuple[str, Optional[str], bool], _PooledBrowser]" = OrderedDict()
        self._guard = asyncio.Lock()

    async def acquire(self, account_id: str, proxy_url: Optional[str], headless: bool) -> _PooledBrowser:
        """Return a warm pooled browser for the key, creating one if needed.

        The caller MUST hold ``entry.lock`` for the duration of its use of the
        page (and release it when done) so two requests never drive the same
        page concurrently.
        """
        key = (account_id, proxy_url, headless)
        victims: List[_PooledBrowser] = []
        async with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _PooledBrowser(
                    LazyBrowser(account_id=account_id, proxy_url=proxy_url, headless=headless)
                )
                self._entries[key] = entry
            entry.last_used = time.monotonic()
            self._entries.move_to_end(key)  # mark most-recently-used
            victims = self._collect_evictions_locked(keep=key)
        # Close evicted browsers outside the guard so a slow close() doesn't
        # block other acquires.
        for v in victims:
            await self._safe_close(v)
        return entry

    def _collect_evictions_locked(self, keep) -> List[_PooledBrowser]:
        """Pop idle and over-capacity entries (never the just-used `keep`, never
        an in-use/locked one). Returns the popped browsers to close."""
        now = time.monotonic()
        victims: List[_PooledBrowser] = []

        # Idle TTL eviction.
        for k in [k for k, e in self._entries.items()
                  if k != keep and not e.lock.locked() and now - e.last_used > self.idle_ttl]:
            victims.append(self._entries.pop(k))

        # Size eviction: drop least-recently-used (front) that aren't busy.
        while len(self._entries) > self.max_size:
            victim_key = next(
                (k for k, e in self._entries.items() if k != keep and not e.lock.locked()),
                None,
            )
            if victim_key is None:
                break  # everything else is busy; don't force-close in-use browsers
            victims.append(self._entries.pop(victim_key))
        return victims

    @staticmethod
    async def _safe_close(entry: _PooledBrowser) -> None:
        try:
            await entry.lazy.close()
        except Exception as e:  # never let cleanup raise into the request path
            logger.warning("x_browser_pool.close_failed", error=str(e)[:160])

    async def close_all(self) -> None:
        """Close every pooled browser (call on app shutdown)."""
        async with self._guard:
            entries = list(self._entries.values())
            self._entries.clear()
        for e in entries:
            await self._safe_close(e)


# Process-wide pool shared by scrape_profile / scrape_search.
_x_browser_pool = XBrowserPool()


def _format_tweet_links(tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Clean relative Nitter links into direct X/Twitter URLs."""
    for t in tweets:
        link = t.get("link", "")
        if link:
            # Remove leading slash and strip Nitter tracking anchors (#m) or query parameters
            clean_link = link.lstrip("/")
            if "#" in clean_link:
                clean_link = clean_link.split("#")[0]
            if "?" in clean_link:
                clean_link = clean_link.split("?")[0]
            t["link"] = f"https://x.com/{clean_link}"
    return tweets


def _parse_html_timeline(html: str) -> dict:
    """Parse Nitter's static HTML content into structured timeline and profile data."""
    soup = BeautifulSoup(html, "html.parser")
    
    # Extract profile if present
    profile = {}
    fullname_el = soup.select_one(".profile-card-fullname")
    if fullname_el:
        username_el = soup.select_one(".profile-card-username")
        bio_el = soup.select_one(".profile-bio")
        loc_el = soup.select_one(".profile-location")
        web_el = soup.select_one(".profile-website a") or soup.select_one(".profile-website")
        join_el = soup.select_one(".profile-joindate")
        
        # Stats
        stats = {}
        for item in soup.select(".profile-statlist li"):
            header_el = item.select_one(".profile-stat-header")
            value_el = item.select_one(".profile-stat-num")
            if header_el and value_el:
                stats[header_el.text.strip().lower()] = value_el.text.strip()
                
        profile = {
            "fullname": fullname_el.text.strip(),
            "username": username_el.text.strip() if username_el else "",
            "bio": bio_el.text.strip() if bio_el else "",
            "location": loc_el.text.strip() if loc_el else "",
            "website": web_el.text.strip() if web_el else "",
            "joined": join_el.text.strip() if join_el else "",
            "stats": stats
        }
        
    # Extract tweets
    tweets = []
    for item in soup.select(".timeline-item"):
        if "show-more" in item.get("class", []):
            continue
            
        link_el = item.select_one(".tweet-link")
        if not link_el:
            continue
            
        link = link_el.get("href", "")
        # Extract tweet ID from link
        tweet_id = link.split("/")[-1].split("#")[0] if "/" in link else ""
        
        username_el = item.select_one(".username")
        fullname_el = item.select_one(".fullname")
        avatar_el = item.select_one(".avatar")
        content_el = item.select_one(".tweet-content")
        date_el = item.select_one(".tweet-date a")
        
        # Stats
        replies, retweets, likes, quotes = 0, 0, 0, 0
        stat_items = item.select(".tweet-stats .tweet-stat")
        for stat in stat_items:
            icon_el = stat.select_one(".icon-container")
            if not icon_el:
                continue
            icon_class = " ".join(icon_el.get("class", []))
            val_text = stat.text.replace(",", "").strip()
            val = int(val_text) if val_text.isdigit() else 0
            if "comment" in icon_class:
                replies = val
            elif "retweet" in icon_class:
                retweets = val
            elif "heart" in icon_class:
                likes = val
            elif "quote" in icon_class:
                quotes = val
                
        is_retweet = bool(item.select_one(".retweet-header"))
        is_pinned = "pinned" in " ".join(item.get("class", []))
        
        tweets.append({
            "link": link,
            "id": tweet_id,
            "username": username_el.text.strip() if username_el else "",
            "fullname": fullname_el.text.strip() if fullname_el else "",
            "avatar": avatar_el.get("src", "") if avatar_el else "",
            "content": content_el.text.strip() if content_el else "",
            "date": date_el.get("title", "") if date_el else "",
            "stats": {
                "replies": replies,
                "retweets": retweets,
                "likes": likes,
                "quotes": quotes
            },
            "is_retweet": is_retweet,
            "is_pinned": is_pinned
        })
        
    # Nitter renders up to two ".show-more" links once you are on a cursored page:
    # a top "Load newest" (points back to newer tweets we've already collected) and
    # a bottom "Load more" (the genuine next/older page). select_one() returned the
    # first, so from page 2 onward pagination followed "Load newest", re-fetched
    # already-seen tweets, and stopped early with 0 new. Pick the last link that is
    # not "Load newest" — that is the real next-page cursor (and on the final page,
    # where only "Load newest" remains, this correctly yields no next link).
    next_link = ""
    for anchor in soup.select(".show-more a"):
        if "newest" in anchor.get_text(" ", strip=True).lower():
            continue
        next_link = anchor.get("href", "")
    
    return {
        "profile": profile,
        "tweets": tweets,
        "next_link": next_link
    }


def _get_stealth_session_info(account_id: str) -> tuple[Optional[str], str]:
    """Load user agent and cookies from saved session state."""
    profile = BrowserProfileManager().generate(account_id)
    user_agent = profile.get("user_agent", "")
    
    session_id = active_profile_session_id(account_id)
    session_data = load_session(session_id)
    
    if not session_data or not session_data.get("cookies"):
        return None, user_agent
        
    cookies = session_data["cookies"]
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
    return cookie_str, user_agent


# --- Shared per-instance Cloudflare token cache --------------------------------
# A solved cf_clearance is bound to the User-Agent, NOT the exit IP (verified
# 2026-06-19: a token solved on proxy A passes the challenge through proxy B). So
# ONE token serves the whole rotating proxy fleet for a given instance. We cache
# it keyed by instance only (not proxy), with the UA it was solved under, so every
# proxy's cheap curl path can reuse it immediately instead of re-solving in a
# browser. Effective token life ~60 min (the 9f_nonce/9f_solution cookies); we
# treat X_SESSION_TTL_SECONDS (default 3000s = 50 min) as the cache window and keep
# the reactive re-solve as the safety net.

def _instance_token_ttl() -> int:
    try:
        return int(os.getenv("X_SESSION_TTL_SECONDS", "3000"))
    except ValueError:
        return 3000


def _instance_token_key(instance_hash: str) -> str:
    return f"x_cf_{instance_hash}"


def _load_instance_token(instance_hash: str) -> tuple[Optional[str], Optional[str]]:
    """Return (cookie_str, user_agent) for the shared instance token, or
    (None, None) when absent or past the TTL."""
    key = _instance_token_key(instance_hash)
    ttl = _instance_token_ttl()
    if ttl > 0:
        age = session_age_seconds(key)
        if age is not None and age >= ttl:
            return None, None
    data = load_session(key)
    if not data or not data.get("cookies"):
        return None, None
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in data["cookies"])
    return cookie_str, data.get("ua") or ""


def _save_instance_token(instance_hash: str, cookies: list, user_agent: str) -> None:
    """Cache a freshly-solved token so every proxy's cheap path reuses it at once."""
    if not cookies:
        return
    save_session(_instance_token_key(instance_hash), {"cookies": cookies, "ua": user_agent})


async def _persist_instance_token_from_browser(lazy_browser, page, instance_hash: str, log) -> None:
    """After the browser clears the instance's bot-check, snapshot its cookies +
    real UA into the shared instance token cache for immediate cross-proxy reuse."""
    try:
        state = await lazy_browser._context.storage_state()
        ua = await page.evaluate("() => navigator.userAgent")
        cookies = state.get("cookies", [])
        _save_instance_token(instance_hash, cookies, ua)
        log.info("unauth_x.instance_token.saved", instance_hash=instance_hash, cookies=len(cookies))
    except Exception as e:
        log.warning("unauth_x.instance_token.save_failed", error=str(e)[:120])


def _instances_warmed_first(instances: List[str]) -> List[str]:
    """Reorder so instances that already hold a fresh cached token come first.
    Without this, random ordering sends a request to an un-warmed instance (which
    forces a browser solve) before it ever reaches a warmed one whose cheap path
    would have served it. Order WITHIN each group is preserved (caller shuffles)."""
    warmed, cold = [], []
    for base in instances:
        ih = hashlib.md5(base.encode("utf-8")).hexdigest()[:10]
        cookie_str, _ = _load_instance_token(ih)
        (warmed if cookie_str else cold).append(base)
    return warmed + cold


async def _attempt_direct_scrape(
    url: str,
    proxy_url: Optional[str],
    account_id: str,
    log
) -> tuple[Optional[str], str]:
    """Attempt to fetch page content directly via curl_cffi using saved stealth session cookies.
    Returns a tuple of (HTML text or None, status/reason string).

    Prefers the shared per-instance token (proxy-portable); falls back to the
    legacy per-(proxy,instance) session when no instance token is cached.
    """
    # instance_hash is the last segment of account_id ("x_unauth_{proxy}_{instance}").
    instance_hash = account_id.rsplit("_", 1)[-1]
    cookie_str, user_agent = _load_instance_token(instance_hash)
    used_instance_token = bool(cookie_str)

    if not cookie_str:
        cookie_str, user_agent = _get_stealth_session_info(account_id)
        if not cookie_str:
            log.info("unauth_x.direct_scrape.no_saved_session")
            return None, "NO_SESSION"
        # Legacy per-account TTL check. (The instance token is freshness-checked
        # inside _load_instance_token, so it skips this.) Past the TTL, return None
        # so the caller falls back to the browser to re-solve + re-cache.
        ttl = _instance_token_ttl()
        if ttl > 0:
            age = session_age_seconds(active_profile_session_id(account_id))
            if age is not None and age >= ttl:
                log.info(
                    "unauth_x.direct_scrape.session_expired",
                    age_seconds=round(age, 1),
                    ttl_seconds=ttl,
                )
                return None, "SESSION_EXPIRED"
    else:
        log.info("unauth_x.direct_scrape.using_instance_token", instance_hash=instance_hash)

    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_str,
        "Upgrade-Insecure-Requests": "1"
    }
    
    # Split timeout: a SHORT connect timeout reflects proxy responsiveness (a dead
    # or stalled proxy never completes the CONNECT tunnel, so it fails in ~5s — in
    # the same ballpark as the ping/healthcheck range, not a flat long wait), while
    # a longer READ timeout lets a *working* proxy finish the ~77 KB page transfer
    # (which is far heavier than the lightweight ping the pool filters on).
    try:
        read_timeout = int(os.getenv("X_DIRECT_TIMEOUT_SECONDS", "8"))
    except ValueError:
        read_timeout = 8
    try:
        connect_timeout = int(os.getenv("X_DIRECT_CONNECT_TIMEOUT_SECONDS", "5"))
    except ValueError:
        connect_timeout = 5

    # Proxy candidates. A cached instance token is proxy-PORTABLE (UA-bound, not
    # IP-bound), so if the handed proxy is dead we retry the SAME token through a
    # few other live pool proxies before giving up — turning a dead-proxy stall
    # into a fast cheap-path success instead of a browser fallback. Without a
    # token there is nothing to reuse, so only the handed proxy is tried.
    proxies_to_try = [proxy_url]
    if used_instance_token:
        try:
            gp = get_proxy_provider()
            if gp.is_enabled():
                retries = int(os.getenv("X_CHEAP_PROXY_RETRIES", "2"))
                for _ in range(max(0, retries)):
                    alt = gp.get_next()
                    if alt and alt not in proxies_to_try:
                        proxies_to_try.append(alt)
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    last_reason = "CONNECTION_FAILED"
    for px in proxies_to_try:
        log.info("unauth_x.direct_scrape.attempting", url=url, proxy=(px[:24] if px else "direct"))
        try:
            response = await loop.run_in_executor(
                None,
                lambda p=px: cffi_requests.get(
                    url,
                    headers=headers,
                    impersonate="chrome120",
                    proxies=({"http": p, "https": p} if p else None),
                    timeout=(connect_timeout, read_timeout),
                ),
            )
        except Exception as e:
            # Proxy/connection failure — cool it down and try the next proxy
            # (the token works on any IP, so this is just bad-proxy luck).
            log.warning("unauth_x.direct_scrape.error", error=str(e)[:120],
                        proxy=(px[:24] if px else "direct"))
            last_reason = "CONNECTION_FAILED"
            if px and used_instance_token:
                try:
                    get_proxy_provider().cool_down(px)
                except Exception:
                    pass
            continue

        html = response.text or ""
        # Block/challenge page → token rejected; needs a browser re-solve, so
        # retrying other proxies won't help.
        is_challenge = ("Just a moment..." in html or
                        "Attention Required!" in html or
                        "Verifying your request" in html or
                        "Making sure you're not a bot!" in html or
                        "challenges.cloudflare.com" in html)
        if is_challenge:
            log.warning("unauth_x.direct_scrape.blocked_by_challenge")
            return None, "CLOUDFLARE"
        # Instance-side error (e.g. 503) — not a proxy problem; stop retrying.
        if response.status_code != 200:
            log.warning("unauth_x.direct_scrape.failed_status", status=response.status_code)
            return None, f"HTTP_{response.status_code}"
        log.info("unauth_x.direct_scrape.success", size=len(html))
        return html, "SUCCESS"

    return None, last_reason


# List of public Nitter / X-proxy instances.
# Twiiit.com can also be used, but direct instances are more predictable for scraping.
# Only instances whose CHEAP (curl) path actually serves are kept here — the
# request path is cheap-path-only (X_INLINE_BROWSER_SOLVE=false), so an instance
# that can't be served without a browser just wastes an attempt.
#   - tiekoetter: luna cookie gate; serves on TLS fingerprint (works cookieless).
#   - kareem:     Cloudflare; cf_clearance token is curl-replayable once warmed.
# Removed 2026-06-19 (re-add if they recover / change anti-bot):
#   - nitter.poast.org  : 503 / down.
#   - nuku.trabun.org   : Cloudflare hard-403, challenge fails to solve.
#   - nitter.catsarch.com: Anubis proof-of-work — needs JS each request, so the
#                          curl cheap path can never serve it (returns interstitial).
DEFAULT_NITTER_INSTANCES = [
    "https://nitter.tiekoetter.com",
    "https://nitter.kareem.one",
]

# JavaScript parsing code for profile timelines and search results.
PARSE_TIMELINE_SCRIPT = """() => {
    const getCleanText = (el) => el ? el.textContent.replace(/\\s+/g, ' ').trim() : '';
    
    // 1. Profile details (only present on profile pages, not search pages)
    const fullnameEl = document.querySelector('.profile-card-fullname');
    let fullname = fullnameEl ? getCleanText(fullnameEl) : null;
    
    const usernameEl = document.querySelector('.profile-card-username');
    let username = usernameEl ? getCleanText(usernameEl) : null;
    
    const bio = getCleanText(document.querySelector('.profile-bio'));
    const location = getCleanText(document.querySelector('.profile-location'));
    const website = getCleanText(document.querySelector('.profile-website a') || document.querySelector('.profile-website'));
    const joined = getCleanText(document.querySelector('.profile-joindate'));
    
    // Stats
    const stats = {};
    document.querySelectorAll('.profile-statlist li').forEach(item => {
        const label = getCleanText(item.querySelector('.profile-stat-header'));
        const val = getCleanText(item.querySelector('.profile-stat-num'));
        if (label && val) {
            stats[label.toLowerCase()] = val;
        }
    });
    
    // 2. Extract tweets
    const tweets = [];
    const items = document.querySelectorAll('.timeline-item');
    items.forEach(item => {
        // Skip 'Show more' button wrapper
        if (item.classList.contains('show-more')) return;
        
        const tweetText = getCleanText(item.querySelector('.tweet-content'));
        
        const dateEl = item.querySelector('.tweet-date a');
        const tweetDate = dateEl ? getCleanText(dateEl) : '';
        const tweetLink = dateEl ? dateEl.getAttribute('href') : '';
        
        const tFullname = getCleanText(item.querySelector('.fullname'));
        const tUsername = getCleanText(item.querySelector('.username'));
        const isRetweet = !!item.querySelector('.retweet-header');
        const isPinned = !!item.querySelector('.pinned-header');
        
        // Metrics
        const replies = getCleanText(item.querySelector('.icon-comment')?.parentElement);
        const retweets = getCleanText(item.querySelector('.icon-retweet')?.parentElement);
        const quotes = getCleanText(item.querySelector('.icon-quote')?.parentElement);
        const likes = getCleanText(item.querySelector('.icon-heart')?.parentElement);
        
        tweets.push({
            text: tweetText,
            date: tweetDate,
            link: tweetLink,
            fullname: tFullname,
            username: tUsername,
            is_retweet: isRetweet,
            is_pinned: isPinned,
            stats: {
                replies: replies || '0',
                retweets: retweets || '0',
                quotes: quotes || '0',
                likes: likes || '0'
            }
        });
    });
    
    // 3. Next page cursor/link.
    // Nitter shows a top "Load newest" and a bottom "Load more" on cursored pages;
    // querySelector picked the first ("Load newest"), which loops back to tweets we
    // already have and stops pagination early. Pick the last non-"newest" link — the
    // real next/older page cursor (and none on the final page, where only "Load
    // newest" remains).
    let nextLink = null;
    document.querySelectorAll('.show-more a').forEach(a => {
        const txt = (a.textContent || '').trim().toLowerCase();
        if (txt.indexOf('newest') !== -1) return;
        nextLink = a.getAttribute('href');
    });
    
    return {
        profile: fullname ? {
            fullname,
            username,
            bio,
            location,
            website,
            joined,
            stats
        } : null,
        tweets,
        next_link: nextLink
    };
}"""


async def _is_cloudflare_active(page: Page) -> bool:
    """Check if Cloudflare challenge page is currently active."""
    for attempt in range(3):
        try:
            title = await page.title()
            content = await page.content()
            # Log for debugging
            logger.info("unauth_x.cf_check", attempt=attempt+1, title=title, content_len=len(content), has_cf_js="challenges.cloudflare.com" in content)
            if any(term in title for term in ["Just a moment...", "Verifying your request", "Attention Required!"]):
                return True
            if "challenges.cloudflare.com" in content:
                return True
            return False
        except Exception as e:
            err_msg = str(e)
            logger.info("unauth_x.cf_check.error", attempt=attempt+1, error=err_msg)
            # If the context is destroyed or page is navigating, wait and retry
            if "destroyed" in err_msg.lower() or "navigation" in err_msg.lower():
                await asyncio.sleep(1.0)
                continue
            return False
    return False


async def _solve_cloudflare_challenge(page: Page, log, timeout_seconds: int = 20) -> bool:
    """Detect and attempt to solve Cloudflare Turnstile challenges."""
    # Poll briefly to see if Cloudflare activates (handles slow loading/redirects).
    # Default 2 checks (~0.8s) instead of the old 5 (~3.2s): by this point the page
    # already finished domcontentloaded + a 2-4s human delay, so a managed challenge
    # is almost always already present or absent. A challenge that appears later is
    # still caught downstream by _check_page_for_blocks, which fails the instance.
    # Real logs showed has_cf_js=False on every poll for these instances, i.e. ~3s
    # wasted per page. Tune via X_CF_DETECT_POLLS.
    try:
        detect_polls = max(1, int(os.getenv("X_CF_DETECT_POLLS", "2")))
    except ValueError:
        detect_polls = 2
    cloudflare_detected = False
    for i in range(detect_polls):
        if page.is_closed():
            return False
        if await _is_cloudflare_active(page):
            cloudflare_detected = True
            break
        if i < detect_polls - 1:
            await asyncio.sleep(0.8)

    if not cloudflare_detected:
        return True

    log.info("unauth_x.solve_cf.detected_challenge")
    
    start_time = asyncio.get_running_loop().time()
    while (asyncio.get_running_loop().time() - start_time) < timeout_seconds:
        if page.is_closed():
            return False

        if not await _is_cloudflare_active(page):
            log.info("unauth_x.solve_cf.solved_or_bypassed")
            return True

        try:
            iframe = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
            for selector in ['#challenge-stage', 'input[type="checkbox"]', '.ctp-checkbox-label', 'span.mark']:
                loc = iframe.locator(selector)
                if await loc.count() > 0:
                    log.info("unauth_x.solve_cf.clicking_checkbox", selector=selector)
                    await loc.click(timeout=3000, force=True)
                    await asyncio.sleep(2.0)
                    break
        except Exception as e:
            log.debug("unauth_x.solve_cf.click_attempt_error", error=str(e))

        await asyncio.sleep(1.5)

    if not await _is_cloudflare_active(page):
        return True

    log.warning("unauth_x.solve_cf.failed_to_solve")
    return False


async def _handle_page_navigation_and_blocks(
    page: Page,
    log,
    label: str,
    proxy_url: Optional[str],
) -> tuple[bool, bool]:
    """Handle Cloudflare challenge solving and general block checking on page navigation."""
    cf_solved = await _solve_cloudflare_challenge(page, log)
    if not cf_solved:
        log.warning(f"unauth_x.{label}.cloudflare_block_failed_to_solve")
        return True, True
    
    return await _check_page_for_blocks(page, log)


async def _check_page_for_blocks(page: Page, log) -> tuple[bool, bool]:
    """Check if the page returned a rate limit, Cloudflare challenge, or empty content.
    Returns:
        (is_blocked, is_proxy_issue) as a tuple of booleans.
    """
    max_check_attempts = 4
    title = ""
    content = ""
    for attempt in range(1, max_check_attempts + 1):
        try:
            if page.is_closed():
                return True, True
            title = await page.title()
            content = await page.content()
            break
        except Exception as e:
            err_msg = str(e)
            log.warning("unauth_x.check_blocks.page_access_failed", attempt=attempt, error=err_msg)
            if attempt == max_check_attempts:
                return True, True
            if "destroyed" in err_msg.lower() or "navigation" in err_msg.lower():
                await asyncio.sleep(0.5 * attempt)
                continue
            return True, True

    # Cloudflare/challenge checks (Proxy reputation issue)
    if "Just a moment..." in title or "Attention Required!" in title or "Verifying your request" in title:
        log.info("unauth_x.check_blocks.challenge_detected", title=title)
        return True, True

    # Nitter-specific blocks/errors (IP rate limits)
    if "Rate limit exceeded" in content or "429 Too Many Requests" in content:
        log.info("unauth_x.check_blocks.rate_limited")
        return True, True

    # Nitter instance backend itself is blocked by Twitter (Not a proxy issue)
    if "Instance has been rate-limited" in content:
        log.info("unauth_x.check_blocks.instance_rate_limited")
        return True, False

    if len(content) < 1500:
        log.warning("unauth_x.check_blocks.page_too_small", size=len(content))
        return True, False

    return False, False


def x_status_to_nitter_path(url_or_id: str) -> Optional[str]:
    """Convert an X/Twitter status URL (or a bare 'user/status/id') into the
    Nitter conversation path '/{user}/status/{id}'.

    Accepts:
      - https://x.com/jack/status/20
      - https://twitter.com/jack/status/20?s=20
      - https://nitter.net/jack/status/20
      - jack/status/20
    Returns None if no status id can be located.
    """
    if not url_or_id:
        return None
    s = url_or_id.strip()
    # Username-less permalinks: /i/web/status/{id} or /i/status/{id}.
    m = re.search(r"/i/(?:web/)?status/(\d+)", s)
    if m:
        return f"/i/status/{m.group(1)}"
    # Full /{user}/status/{id} (handles x.com, twitter.com, nitter.*, bare path).
    m = re.search(r"(?:^|/)([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)", s)
    if m:
        return f"/{m.group(1)}/status/{m.group(2)}"
    # Fallback: a bare numeric status id with no username.
    m = re.search(r"^(\d{6,})$", s)
    if m:
        return f"/i/status/{m.group(1)}"
    return None


def _extract_thread_item(item) -> Optional[Dict[str, Any]]:
    """Extract one tweet dict from a Nitter '.timeline-item'/'.main-tweet' node.

    Unlike _parse_html_timeline this keys off '.tweet-date a' for the permalink
    (always present on conversation pages — the main tweet has no '.tweet-link'
    overlay anchor), so it works for both the focused tweet and its replies.
    """
    content_el = item.select_one(".tweet-content")
    date_el = item.select_one(".tweet-date a")
    if not date_el and not content_el:
        return None

    # Strip Nitter's #m anchor and any query string so links compare cleanly.
    raw_link = date_el.get("href", "") if date_el else ""
    link = raw_link.split("#")[0].split("?")[0]
    tweet_id = link.split("/")[-1] if "/" in link else ""

    username_el = item.select_one(".username")
    fullname_el = item.select_one(".fullname")
    avatar_el = item.select_one(".avatar")

    replies, retweets, likes, quotes = 0, 0, 0, 0
    for stat in item.select(".tweet-stats .tweet-stat"):
        # The icon type class (icon-comment/-retweet/-heart/-quote) may sit on the
        # .icon-container or on a child span depending on the Nitter version, so
        # scan the whole stat subtree's class names rather than one node.
        classes = " ".join(
            " ".join(el.get("class", [])) for el in stat.find_all(True)
        )
        val_text = stat.text.replace(",", "").strip()
        val = int(val_text) if val_text.isdigit() else 0
        if "comment" in classes:
            replies = val
        elif "retweet" in classes:
            retweets = val
        elif "heart" in classes:
            likes = val
        elif "quote" in classes:
            quotes = val

    return {
        "link": link,
        "id": tweet_id,
        "username": username_el.text.strip() if username_el else "",
        "fullname": fullname_el.text.strip() if fullname_el else "",
        "avatar": avatar_el.get("src", "") if avatar_el else "",
        "content": content_el.text.strip() if content_el else "",
        "date": date_el.get("title", "") if date_el else "",
        "stats": {
            "replies": replies,
            "retweets": retweets,
            "likes": likes,
            "quotes": quotes,
        },
        "is_retweet": bool(item.select_one(".retweet-header")),
    }


def _parse_thread_html(html: str) -> dict:
    """Parse a Nitter conversation page into the focused tweet + its replies.

    Returns {"main_tweet": {...}|{}, "replies": [...], "next_link": str}. The
    main tweet lives in '.main-tweet'; replies are the '.timeline-item' nodes in
    the '.replies'/'.conversation' container (each '.reply' may nest sub-replies,
    all of which are '.timeline-item's — we flatten them in document order).
    """
    soup = BeautifulSoup(html, "html.parser")

    main_tweet = {}
    main_el = soup.select_one(".main-tweet .timeline-item") or soup.select_one(".main-tweet")
    main_link = ""
    if main_el:
        parsed_main = _extract_thread_item(main_el)
        if parsed_main:
            main_tweet = parsed_main
            main_link = parsed_main.get("link", "")

    replies = []
    seen = set()
    replies_root = soup.select_one(".replies") or soup.select_one(".conversation")
    reply_items = replies_root.select(".timeline-item") if replies_root else []
    # On instances that don't wrap replies in '.replies', fall back to every
    # '.timeline-item' that isn't the main tweet.
    if not reply_items:
        reply_items = soup.select(".timeline-item")
    for item in reply_items:
        # Skip the main tweet if it surfaced inside the reply scan.
        if main_el is not None and item is main_el:
            continue
        if "show-more" in item.get("class", []):
            continue
        parsed = _extract_thread_item(item)
        if not parsed:
            continue
        link = parsed.get("link") or ""
        if link and link == main_link:
            continue
        key = link or parsed.get("content", "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        replies.append(parsed)

    # "Load more" replies cursor — same logic as the timeline parser: pick the
    # last '.show-more' anchor that isn't a "Load newest" back-reference.
    next_link = ""
    for anchor in soup.select(".show-more a"):
        if "newest" in anchor.get_text(" ", strip=True).lower():
            continue
        next_link = anchor.get("href", "")

    return {"main_tweet": main_tweet, "replies": replies, "next_link": next_link}


async def scrape_thread(
    status_url: str,
    limit: int = 20,
    instances: Optional[List[str]] = None,
    proxy_url: Optional[str] = None,
    headless: bool = True,
) -> Dict[str, Any]:
    """Scrape a tweet's full conversation thread (focused tweet + replies)
    without authentication.

    Mirrors scrape_profile/scrape_search: try a fast direct curl_cffi GET on the
    saved stealth session first, fall back to the warm Playwright browser pool to
    solve Cloudflare, paginate the "Load more" reply cursor until `limit` replies
    are gathered, and ALWAYS return a structured dict (never raise).

    Args:
        status_url: Full X/Twitter status URL (e.g. https://x.com/jack/status/20).
        limit: Target number of replies to collect.
        instances: List of Nitter proxy instance URLs.
        proxy_url: Proxy URL for browser context / direct fetch.
        headless: Whether to run browser headlessly.

    Returns:
        Dict with main_tweet, replies, source_instance, success, error.
    """
    nitter_path = x_status_to_nitter_path(status_url)
    log = logger.bind(status_url=status_url, action="UNAUTH_SCRAPE_THREAD")
    if not nitter_path:
        return {
            "success": False,
            "main_tweet": {},
            "replies": [],
            "source_instance": None,
            "error": f"Could not extract a tweet/status id from: {status_url}",
        }

    if not instances:
        instances = list(DEFAULT_NITTER_INSTANCES)
        random.shuffle(instances)
        instances = _instances_warmed_first(instances)

    def _collect(parsed: dict, main_holder: dict, replies: list, seen: set) -> int:
        """Merge a parsed page into the running thread; return # new replies."""
        if parsed.get("main_tweet") and not main_holder.get("main"):
            main_holder["main"] = parsed["main_tweet"]
        added = 0
        for r in parsed.get("replies", []):
            link = r.get("link") or ""
            key = link or r.get("content", "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            replies.append(r)
            added += 1
        return added

    for attempt, base_url in enumerate(instances, start=1):
        log.info("unauth_x.thread.attempt", attempt=attempt, instance=base_url)

        proxy_hash = hashlib.md5(proxy_url.encode("utf-8")).hexdigest()[:10] if proxy_url else "direct"
        instance_hash = hashlib.md5(base_url.encode("utf-8")).hexdigest()[:10]
        account_id = f"x_unauth_{proxy_hash}_{instance_hash}"

        target_url = urllib.parse.urljoin(base_url, nitter_path)
        main_holder: dict = {}
        replies: List[Dict[str, Any]] = []
        seen: set = set()

        # 1. Direct curl_cffi attempt.
        direct_html, direct_reason = await _attempt_direct_scrape(target_url, proxy_url, account_id, log)
        if direct_html:
            try:
                parsed = _parse_thread_html(direct_html)
                if parsed.get("main_tweet") or parsed.get("replies"):
                    _collect(parsed, main_holder, replies, seen)
                    log.info("unauth_x.thread.direct.parse_success",
                             replies=len(replies), has_main=bool(main_holder.get("main")))

                    next_link = parsed.get("next_link")
                    current_url = target_url
                    while next_link and len(replies) < limit:
                        paginated_url = urllib.parse.urljoin(current_url, next_link)
                        page_html, _ = await _attempt_direct_scrape(paginated_url, proxy_url, account_id, log)
                        if not page_html:
                            break
                        page_parsed = _parse_thread_html(page_html)
                        new_added = _collect(page_parsed, main_holder, replies, seen)
                        log.info("unauth_x.thread.direct.paginate.added", count=new_added, total=len(replies))
                        if new_added == 0:
                            break
                        next_link = page_parsed.get("next_link")
                        current_url = paginated_url

                    return {
                        "success": True,
                        "main_tweet": main_holder.get("main", {}),
                        "replies": _format_tweet_links(replies[:limit]),
                        "reply_count": len(replies[:limit]),
                        "source_instance": base_url,
                        "error": None,
                    }
            except Exception as e:
                log.warning("unauth_x.thread.direct.parse_failed", error=str(e))
        else:
            if direct_reason not in ("CLOUDFLARE", "SESSION_EXPIRED", "NO_SESSION"):
                log.info("unauth_x.thread.direct.skip_browser_fallback", reason=direct_reason, instance=base_url)
                continue

        # 2. Browser fallback.
        if not _inline_browser_solve_enabled():
            log.info("unauth_x.thread.inline_solve_disabled", instance=base_url)
            continue
        log.info("unauth_x.thread.fallback_to_browser", account_id=account_id)
        entry = await _x_browser_pool.acquire(account_id, proxy_url, headless)
        lazy_browser = entry.lazy
        await entry.lock.acquire()
        try:
            page = await lazy_browser.get_page()
            log.info("unauth_x.thread.navigating", url=target_url)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            await _delay("unauth_x_thread", 2.0, 4.0)

            is_blocked, is_proxy_issue = await _handle_page_navigation_and_blocks(
                page, log, "main_thread", proxy_url
            )
            if is_blocked:
                if is_proxy_issue and proxy_url is not None:
                    log.warning("unauth_x.thread.proxy_blocked_early_abort", instance=base_url)
                    return {
                        "success": False,
                        "main_tweet": {},
                        "replies": [],
                        "source_instance": None,
                        "error": "PROXY_BLOCKED",
                    }
                log.warning("unauth_x.thread.instance_blocked", instance=base_url)
                continue

            # Bot-check cleared in the browser — cache this instance's token
            # (cookies + UA) so every proxy's cheap path reuses it immediately.
            await _persist_instance_token_from_browser(lazy_browser, page, instance_hash, log)
            html = await page.content()
            parsed = _parse_thread_html(html)
            if parsed.get("main_tweet") or parsed.get("replies"):
                _collect(parsed, main_holder, replies, seen)
                log.info("unauth_x.thread.parse_success",
                         replies=len(replies), has_main=bool(main_holder.get("main")))

                next_link = parsed.get("next_link")
                current_url = target_url
                while next_link and len(replies) < limit:
                    paginated_url = urllib.parse.urljoin(current_url, next_link)
                    await page.goto(paginated_url, wait_until="domcontentloaded", timeout=45000)
                    await _delay("unauth_x_thread", 1.5, 3.0)

                    is_blocked, _ = await _handle_page_navigation_and_blocks(
                        page, log, "paginate_thread", proxy_url
                    )
                    if is_blocked:
                        log.warning("unauth_x.thread.paginate.blocked")
                        break

                    page_parsed = _parse_thread_html(await page.content())
                    new_added = _collect(page_parsed, main_holder, replies, seen)
                    log.info("unauth_x.thread.paginate.added", count=new_added, total=len(replies))
                    if new_added == 0:
                        break
                    next_link = page_parsed.get("next_link")
                    current_url = paginated_url

                return {
                    "success": True,
                    "main_tweet": main_holder.get("main", {}),
                    "replies": _format_tweet_links(replies[:limit]),
                    "reply_count": len(replies[:limit]),
                    "source_instance": base_url,
                    "error": None,
                }
        except Exception as e:
            err_msg = str(e)
            log.error("unauth_x.thread.attempt_failed", instance=base_url, error=err_msg)
            try:
                await page.evaluate("() => { try { window.stop(); } catch(err) {} }")
                await asyncio.sleep(0.5)
            except Exception:
                pass
            is_proxy_error = any(indicator in err_msg for indicator in [
                "ERR_TUNNEL_CONNECTION_FAILED",
                "ERR_PROXY_CONNECTION_FAILED",
                "ERR_CONNECTION_RESET",
                "ERR_TIMED_OUT",
                "TimeoutError",
                "net::ERR_",
            ])
            if is_proxy_error and proxy_url is not None:
                log.warning("unauth_x.thread.proxy_connection_failed_early_abort", instance=base_url)
                return {
                    "success": False,
                    "main_tweet": {},
                    "replies": [],
                    "source_instance": None,
                    "error": "PROXY_BLOCKED",
                }
        finally:
            entry.lock.release()

    return {
        "success": False,
        "main_tweet": {},
        "replies": [],
        "source_instance": None,
        "error": "All public Nitter instances were blocked, rate-limited, or failed to respond.",
    }


async def scrape_profile(
    username: str,
    limit: int = 40,
    instances: Optional[List[str]] = None,
    proxy_url: Optional[str] = None,
    headless: bool = True,
) -> Dict[str, Any]:
    """Scrape a user profile and their tweets without authentication.

    Args:
        username: Target Twitter username (e.g. 'jack').
        limit: Target number of tweets to collect.
        instances: List of Nitter proxy instance URLs.
        proxy_url: Proxy URL for browser context.
        headless: Whether to run browser headlessly.

    Returns:
        Dict containing user profile data and list of tweets.
    """
    clean_username = username.strip().replace("@", "")
    log = logger.bind(username=clean_username, action="UNAUTH_SCRAPE_PROFILE")

    if not instances:
        instances = list(DEFAULT_NITTER_INSTANCES)
        random.shuffle(instances)
        instances = _instances_warmed_first(instances)

    profile_data = {}
    all_tweets = []
    seen_links = set()
    next_relative_path = f"/{clean_username}"

    for attempt, base_url in enumerate(instances, start=1):
        log.info("unauth_x.scrape.attempt", attempt=attempt, instance=base_url)
        
        # 1. Deterministic account ID for this proxy + instance combination
        proxy_hash = hashlib.md5(proxy_url.encode('utf-8')).hexdigest()[:10] if proxy_url else "direct"
        instance_hash = hashlib.md5(base_url.encode('utf-8')).hexdigest()[:10]
        account_id = f"x_unauth_{proxy_hash}_{instance_hash}"
        
        target_url = urllib.parse.urljoin(base_url, next_relative_path)
        
        # 2. Try direct HTTP GET scrape first
        direct_html, direct_reason = await _attempt_direct_scrape(target_url, proxy_url, account_id, log)
        if direct_html:
            try:
                parsed = _parse_html_timeline(direct_html)
                if parsed and parsed.get("tweets"):
                    if parsed.get("profile"):
                        profile_data = parsed["profile"]
                    
                    for t in parsed["tweets"]:
                        link = t.get("link")
                        if link and link not in seen_links:
                            seen_links.add(link)
                            all_tweets.append(t)
                            
                    log.info("unauth_x.direct_scrape.parse_success", tweets_found=len(parsed["tweets"]), unique_tweets=len(all_tweets))
                    
                    # Direct pagination
                    next_link = parsed.get("next_link")
                    current_url = target_url
                    while next_link and len(all_tweets) < limit:
                        paginated_url = urllib.parse.urljoin(current_url, next_link)
                        log.info("unauth_x.direct_scrape.paginate", url=paginated_url, current_count=len(all_tweets))
                        
                        page_html, page_reason = await _attempt_direct_scrape(paginated_url, proxy_url, account_id, log)
                        if not page_html:
                            break
                            
                        paginated_parsed = _parse_html_timeline(page_html)
                        if not paginated_parsed or not paginated_parsed.get("tweets"):
                            break
                            
                        new_added = 0
                        for t in paginated_parsed["tweets"]:
                            link = t.get("link")
                            if link and link not in seen_links:
                                seen_links.add(link)
                                all_tweets.append(t)
                                new_added += 1
                                
                        log.info("unauth_x.direct_scrape.paginate.added", count=new_added, total=len(all_tweets))
                        if new_added == 0:
                            break
                        next_link = paginated_parsed.get("next_link")
                        current_url = paginated_url
                        
                    return {
                        "success": True,
                        "profile": profile_data,
                        "tweets": _format_tweet_links(all_tweets[:limit]),
                        "source_instance": base_url,
                        "error": None
                    }
            except Exception as e:
                log.warning("unauth_x.direct_scrape.parse_failed", error=str(e))
        else:
            if direct_reason not in ("CLOUDFLARE", "SESSION_EXPIRED", "NO_SESSION"):
                log.info("unauth_x.direct_scrape.skip_browser_fallback", reason=direct_reason, instance=base_url)
                continue
        
        # 3. Fallback to browser (Playwright)
        if not _inline_browser_solve_enabled():
            log.info("unauth_x.inline_solve_disabled", instance=base_url)
            continue
        log.info("unauth_x.direct_scrape.fallback_to_browser", account_id=account_id)
        entry = await _x_browser_pool.acquire(account_id, proxy_url, headless)
        lazy_browser = entry.lazy
        await entry.lock.acquire()
        try:
            page = await lazy_browser.get_page()
            log.info("unauth_x.navigating", url=target_url)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            await _delay(f"unauth_x_{clean_username}", 2.0, 4.0)
            
            is_blocked, is_proxy_issue = await _handle_page_navigation_and_blocks(
                page, log, "main_profile", proxy_url
            )
            if is_blocked:
                if is_proxy_issue and proxy_url is not None:
                    log.warning("unauth_x.proxy_blocked_early_abort", instance=base_url)
                    return {
                        "success": False,
                        "profile": {},
                        "tweets": [],
                        "source_instance": None,
                        "error": "PROXY_BLOCKED"
                    }
                log.warning("unauth_x.instance_blocked_or_empty", instance=base_url)
                continue

            # Bot-check cleared in the browser — cache this instance's token
            # (cookies + UA) so every proxy's cheap path reuses it immediately.
            await _persist_instance_token_from_browser(lazy_browser, page, instance_hash, log)
            parsed = await page.evaluate(PARSE_TIMELINE_SCRIPT)
            if parsed and parsed.get("tweets"):
                if parsed.get("profile"):
                    profile_data = parsed["profile"]
                
                for t in parsed["tweets"]:
                    link = t.get("link")
                    if link and link not in seen_links:
                        seen_links.add(link)
                        all_tweets.append(t)
                        
                log.info("unauth_x.parse_success", tweets_found=len(parsed["tweets"]), unique_tweets=len(all_tweets))
                
                # Browser pagination
                next_link = parsed.get("next_link")
                current_url = target_url
                while next_link and len(all_tweets) < limit:
                    paginated_url = urllib.parse.urljoin(current_url, next_link)
                    log.info("unauth_x.paginate", url=paginated_url, current_count=len(all_tweets))
                    
                    await page.goto(paginated_url, wait_until="domcontentloaded", timeout=45000)
                    await _delay(f"unauth_x_{clean_username}", 1.5, 3.0)
                    
                    is_blocked, is_proxy_issue = await _handle_page_navigation_and_blocks(
                        page, log, "paginate_profile", proxy_url
                    )
                    if is_blocked:
                        log.warning("unauth_x.paginate.blocked", is_proxy_issue=is_proxy_issue)
                        break
                        
                    paginated_parsed = await page.evaluate(PARSE_TIMELINE_SCRIPT)
                    if not paginated_parsed or not paginated_parsed.get("tweets"):
                        log.info("unauth_x.paginate.no_more_tweets")
                        break
                        
                    new_added = 0
                    for t in paginated_parsed["tweets"]:
                        link = t.get("link")
                        if link and link not in seen_links:
                            seen_links.add(link)
                            all_tweets.append(t)
                            new_added += 1
                            
                    log.info("unauth_x.paginate.added", count=new_added, total=len(all_tweets))
                    if new_added == 0:
                        break
                    next_link = paginated_parsed.get("next_link")
                    current_url = paginated_url
                    
                return {
                    "success": True,
                    "profile": profile_data,
                    "tweets": _format_tweet_links(all_tweets[:limit]),
                    "source_instance": base_url,
                    "error": None
                }
        except Exception as e:
            err_msg = str(e)
            log.error("unauth_x.attempt_failed", instance=base_url, error=err_msg)
            try:
                await page.evaluate("() => { try { window.stop(); } catch(err) {} }")
                await asyncio.sleep(0.5)
            except Exception:
                pass
                
            is_proxy_error = any(indicator in err_msg for indicator in [
                "ERR_TUNNEL_CONNECTION_FAILED",
                "ERR_PROXY_CONNECTION_FAILED",
                "ERR_CONNECTION_RESET",
                "ERR_TIMED_OUT",
                "TimeoutError",
                "net::ERR_"
            ])
            if is_proxy_error and proxy_url is not None:
                log.warning("unauth_x.proxy_connection_failed_early_abort", instance=base_url)
                return {
                    "success": False,
                    "profile": {},
                    "tweets": [],
                    "source_instance": None,
                    "error": "PROXY_BLOCKED"
                }
        finally:
            # Keep the browser warm in the pool; just release our turn on it.
            entry.lock.release()


async def scrape_search(
    query: str,
    limit: int = 20,
    instances: Optional[List[str]] = None,
    proxy_url: Optional[str] = None,
    headless: bool = True,
) -> Dict[str, Any]:
    """Scrape search results for a query without authentication.

    Args:
        query: Search query (e.g. 'bitcoin' or '#stablecoin').
        limit: Target number of tweets to collect.
        instances: List of Nitter proxy instance URLs.
        proxy_url: Proxy URL for browser context.
        headless: Whether to run browser headlessly.

    Returns:
        Dict containing list of parsed search tweets.
    """
    log = logger.bind(query=query, action="UNAUTH_SCRAPE_SEARCH")

    if not instances:
        instances = list(DEFAULT_NITTER_INSTANCES)
        random.shuffle(instances)
        instances = _instances_warmed_first(instances)

    all_tweets = []
    seen_links = set()
    encoded_query = urllib.parse.quote(query)
    next_relative_path = f"/search?q={encoded_query}"

    for attempt, base_url in enumerate(instances, start=1):
        log.info("unauth_x.search.attempt", attempt=attempt, instance=base_url)
        
        # 1. Deterministic account ID for this proxy + instance combination
        proxy_hash = hashlib.md5(proxy_url.encode('utf-8')).hexdigest()[:10] if proxy_url else "direct"
        instance_hash = hashlib.md5(base_url.encode('utf-8')).hexdigest()[:10]
        account_id = f"x_unauth_{proxy_hash}_{instance_hash}"
        
        target_url = urllib.parse.urljoin(base_url, next_relative_path)
        
        # 2. Try direct HTTP GET scrape first
        direct_html, direct_reason = await _attempt_direct_scrape(target_url, proxy_url, account_id, log)
        if direct_html:
            try:
                parsed = _parse_html_timeline(direct_html)
                if parsed and parsed.get("tweets"):
                    for t in parsed["tweets"]:
                        link = t.get("link")
                        if link and link not in seen_links:
                            seen_links.add(link)
                            all_tweets.append(t)
                            
                    log.info("unauth_x.search.direct_scrape.parse_success", tweets_found=len(parsed["tweets"]), unique_tweets=len(all_tweets))
                    
                    # Direct pagination
                    next_link = parsed.get("next_link")
                    current_url = target_url
                    while next_link and len(all_tweets) < limit:
                        paginated_url = urllib.parse.urljoin(current_url, next_link)
                        log.info("unauth_x.search.direct_scrape.paginate", url=paginated_url, current_count=len(all_tweets))
                        
                        page_html, page_reason = await _attempt_direct_scrape(paginated_url, proxy_url, account_id, log)
                        if not page_html:
                            break
                            
                        parsed_page = _parse_html_timeline(page_html)
                        if not parsed_page or not parsed_page.get("tweets"):
                            break
                            
                        new_added = 0
                        for t in parsed_page["tweets"]:
                            link = t.get("link")
                            if link and link not in seen_links:
                                seen_links.add(link)
                                all_tweets.append(t)
                                new_added += 1
                                
                        log.info("unauth_x.search.direct_scrape.paginate.added", count=new_added, total=len(all_tweets))
                        if new_added == 0:
                            break
                        next_link = parsed_page.get("next_link")
                        current_url = paginated_url
                        
                    return {
                        "success": True,
                        "tweets": _format_tweet_links(all_tweets[:limit]),
                        "source_instance": base_url,
                        "error": None
                    }
            except Exception as e:
                log.warning("unauth_x.search.direct_scrape.parse_failed", error=str(e))
        else:
            if direct_reason not in ("CLOUDFLARE", "SESSION_EXPIRED", "NO_SESSION"):
                log.info("unauth_x.search.direct_scrape.skip_browser_fallback", reason=direct_reason, instance=base_url)
                continue
                
        # 3. Fallback to browser (Playwright)
        if not _inline_browser_solve_enabled():
            log.info("unauth_x.search.inline_solve_disabled", instance=base_url)
            continue
        log.info("unauth_x.search.direct_scrape.fallback_to_browser", account_id=account_id)
        entry = await _x_browser_pool.acquire(account_id, proxy_url, headless)
        lazy_browser = entry.lazy
        await entry.lock.acquire()
        try:
            page = await lazy_browser.get_page()
            log.info("unauth_x.search.navigating", url=target_url)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            await _delay("unauth_x_search", 2.0, 4.0)
            
            is_blocked, is_proxy_issue = await _handle_page_navigation_and_blocks(
                page, log, "main_search", proxy_url
            )
            if is_blocked:
                if is_proxy_issue and proxy_url is not None:
                    log.warning("unauth_x.search.proxy_blocked_early_abort", instance=base_url)
                    return {
                        "success": False,
                        "tweets": [],
                        "source_instance": None,
                        "error": "PROXY_BLOCKED"
                    }
                log.warning("unauth_x.search.instance_blocked", instance=base_url)
                continue

            # Bot-check cleared in the browser — cache this instance's token
            # (cookies + UA) so every proxy's cheap path reuses it immediately.
            await _persist_instance_token_from_browser(lazy_browser, page, instance_hash, log)
            parsed = await page.evaluate(PARSE_TIMELINE_SCRIPT)
            if parsed and parsed.get("tweets"):
                for t in parsed["tweets"]:
                    link = t.get("link")
                    if link and link not in seen_links:
                        seen_links.add(link)
                        all_tweets.append(t)
                        
                log.info("unauth_x.search.parse_success", tweets_found=len(parsed["tweets"]), unique_tweets=len(all_tweets))
                
                # Browser pagination
                next_link = parsed.get("next_link")
                current_url = target_url
                while next_link and len(all_tweets) < limit:
                    paginated_url = urllib.parse.urljoin(current_url, next_link)
                    log.info("unauth_x.search.paginate", url=paginated_url, current_count=len(all_tweets))
                    
                    await page.goto(paginated_url, wait_until="domcontentloaded", timeout=45000)
                    await _delay("unauth_x_search", 1.5, 3.0)
                    
                    is_blocked, is_proxy_issue = await _handle_page_navigation_and_blocks(
                        page, log, "paginate_search", proxy_url
                    )
                    if is_blocked:
                        log.warning("unauth_x.search.paginate.blocked", is_proxy_issue=is_proxy_issue)
                        break
                        
                    paginated_parsed = await page.evaluate(PARSE_TIMELINE_SCRIPT)
                    if not paginated_parsed or not paginated_parsed.get("tweets"):
                        log.info("unauth_x.search.paginate.no_more_tweets")
                        break
                        
                    new_added = 0
                    for t in paginated_parsed["tweets"]:
                        link = t.get("link")
                        if link and link not in seen_links:
                            seen_links.add(link)
                            all_tweets.append(t)
                            new_added += 1
                            
                    log.info("unauth_x.search.paginate.added", count=new_added, total=len(all_tweets))
                    if new_added == 0:
                        break
                    next_link = paginated_parsed.get("next_link")
                    current_url = paginated_url
                    
                return {
                    "success": True,
                    "tweets": _format_tweet_links(all_tweets[:limit]),
                    "source_instance": base_url,
                    "error": None
                }
        except Exception as e:
            err_msg = str(e)
            log.error("unauth_x.search.attempt_failed", instance=base_url, error=err_msg)
            try:
                await page.evaluate("() => { try { window.stop(); } catch(err) {} }")
                await asyncio.sleep(0.5)
            except Exception:
                pass
                
            is_proxy_error = any(indicator in err_msg for indicator in [
                "ERR_TUNNEL_CONNECTION_FAILED",
                "ERR_PROXY_CONNECTION_FAILED",
                "ERR_CONNECTION_RESET",
                "ERR_TIMED_OUT",
                "TimeoutError",
                "net::ERR_"
            ])
            if is_proxy_error and proxy_url is not None:
                log.warning("unauth_x.search.proxy_connection_failed_early_abort", instance=base_url)
                return {
                    "success": False,
                    "tweets": [],
                    "source_instance": None,
                    "error": "PROXY_BLOCKED"
                }
        finally:
            # Keep the browser warm in the pool; just release our turn on it.
            entry.lock.release()
            
    return {
        "success": False,
        "tweets": [],
        "source_instance": None,
        "error": "All public Nitter instances were blocked, rate-limited, or failed to respond."
    }


# --- Out-of-band token warming -------------------------------------------------
# Mint instance tokens the same way LinkedIn mints li_at: solve the bot-check once
# in a browser OFF the request path, cache the token, and refresh before its TTL.
# Then the request path uses the cheap curl path and (with X_INLINE_BROWSER_SOLVE
# disabled) never launches a browser inline.

def _inline_browser_solve_enabled() -> bool:
    """Whether the REQUEST path may launch a browser to solve a challenge inline.
    Set X_INLINE_BROWSER_SOLVE=false to rely solely on the background warmer +
    cached tokens — requests then skip un-warmed instances instead of blocking on
    an inline browser solve."""
    return (os.getenv("X_INLINE_BROWSER_SOLVE", "true").strip().lower()
            in ("1", "true", "yes", "on"))


async def warm_instance(base_url: str, proxy_url: Optional[str] = None, headless: bool = True) -> bool:
    """Solve one instance's bot-check in a browser OUT OF BAND and cache its token
    (cookies + UA) so every proxy's cheap path can reuse it. Returns True when a
    token was cached. Safe to call repeatedly (it just refreshes)."""
    instance_hash = hashlib.md5(base_url.encode("utf-8")).hexdigest()[:10]
    log = logger.bind(instance=base_url, action="WARM_X_INSTANCE")
    if proxy_url is None:
        try:
            gp = get_proxy_provider()
            if gp.is_enabled():
                proxy_url = gp.get_next()
        except Exception:
            pass
    proxy_hash = hashlib.md5(proxy_url.encode("utf-8")).hexdigest()[:10] if proxy_url else "warm"
    account_id = f"x_unauth_{proxy_hash}_{instance_hash}"
    entry = await _x_browser_pool.acquire(account_id, proxy_url, headless)
    lazy_browser = entry.lazy
    await entry.lock.acquire()
    try:
        page = await lazy_browser.get_page()
        target = base_url.rstrip("/") + "/jack"
        await page.goto(target, wait_until="domcontentloaded", timeout=45000)
        await _delay("warm_x", 1.0, 2.0)
        is_blocked, _ = await _handle_page_navigation_and_blocks(page, log, "warm", proxy_url)
        if is_blocked:
            log.warning("unauth_x.warm.blocked", instance=base_url)
            return False
        await _persist_instance_token_from_browser(lazy_browser, page, instance_hash, log)
        log.info("unauth_x.warm.ok", instance=base_url)
        return True
    except Exception as e:
        log.warning("unauth_x.warm.failed", instance=base_url, error=str(e)[:120])
        return False
    finally:
        entry.lock.release()


async def warm_all_instances(instances: Optional[List[str]] = None, headless: bool = True) -> dict:
    """Warm every instance sequentially (background use). Returns {instance: ok}."""
    insts = instances or list(DEFAULT_NITTER_INSTANCES)
    results: dict = {}
    for base in insts:
        try:
            results[base] = await warm_instance(base, headless=headless)
        except Exception as e:
            logger.warning("unauth_x.warm.exception", instance=base, error=str(e)[:120])
            results[base] = False
    return results
