"""
tools/unauth_x_scraper.py — Unauthenticated X (Twitter) scraper using Nitter proxy instances.

Uses public Nitter/X-proxy instances combined with the Playwright stealth stack to bypass
Cloudflare and Turnstile blocks, extracting structured profiles and tweets.
"""

from __future__ import annotations

import asyncio
import os
import random
import urllib.parse
import hashlib
from typing import Optional, List, Dict, Any

import structlog
from playwright.async_api import Page
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from tools.browser_manager import LazyBrowser, active_profile_session_id
from tools.session_store import load_session, session_age_seconds
from tools.stealth.fingerprint import BrowserProfileManager
from tools.stealth.helpers import _delay, _random_scroll

logger = structlog.get_logger(__name__)


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


async def _attempt_direct_scrape(
    url: str,
    proxy_url: Optional[str],
    account_id: str,
    log
) -> Optional[str]:
    """Attempt to fetch page content directly via curl_cffi using saved stealth session cookies.
    Returns the HTML text if successful and not blocked, else None.
    """
    cookie_str, user_agent = _get_stealth_session_info(account_id)
    if not cookie_str:
        log.info("unauth_x.direct_scrape.no_saved_session")
        return None

    # Time-limit the cached session. The cookies/clearance token the browser
    # obtained for this proxy+instance are short-lived; past the TTL, skip the
    # (likely-stale) direct replay and return None so the caller falls back to the
    # browser, which re-solves and re-persists a fresh session. Tune via
    # X_SESSION_TTL_SECONDS (default 1500s = 25 min); set to 0 to disable.
    try:
        ttl = int(os.getenv("X_SESSION_TTL_SECONDS", "1500"))
    except ValueError:
        ttl = 1500
    if ttl > 0:
        age = session_age_seconds(active_profile_session_id(account_id))
        if age is not None and age >= ttl:
            log.info(
                "unauth_x.direct_scrape.session_expired",
                age_seconds=round(age, 1),
                ttl_seconds=ttl,
            )
            return None
        
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_str,
        "Upgrade-Insecure-Requests": "1"
    }
    
    curl_proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    
    log.info("unauth_x.direct_scrape.attempting", url=url)
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: cffi_requests.get(
                url,
                headers=headers,
                impersonate="chrome120",
                proxies=curl_proxies,
                timeout=12
            )
        )
        
        if response.status_code != 200:
            log.warning("unauth_x.direct_scrape.failed_status", status=response.status_code)
            return None
            
        html = response.text
        # Check if the page is a block/challenge page
        is_blocked = ("Just a moment..." in html or 
                      "Attention Required!" in html or 
                      "Verifying your request" in html or
                      "Making sure you're not a bot!" in html or
                      "Rate limit exceeded" in html or
                      "429 Too Many Requests" in html)
                      
        if is_blocked:
            log.warning("unauth_x.direct_scrape.blocked_by_challenge")
            return None
            
        log.info("unauth_x.direct_scrape.success", size=len(html))
        return html
        
    except Exception as e:
        log.warning("unauth_x.direct_scrape.error", error=str(e))
        return None

# List of public Nitter / X-proxy instances.
# Twiiit.com can also be used, but direct instances are more predictable for scraping.
DEFAULT_NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nuku.trabun.org",
    "https://nitter.tiekoetter.com",
    "https://nitter.kareem.one",
    "https://nitter.catsarch.com",
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
        direct_html = await _attempt_direct_scrape(target_url, proxy_url, account_id, log)
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
                        
                        page_html = await _attempt_direct_scrape(paginated_url, proxy_url, account_id, log)
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
        
        # 3. Fallback to browser (Playwright)
        log.info("unauth_x.direct_scrape.fallback_to_browser", account_id=account_id)
        lazy_browser = LazyBrowser(account_id=account_id, proxy_url=proxy_url, headless=headless)
        try:
            page = await lazy_browser.get_page()
            log.info("unauth_x.navigating", url=target_url)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            await _delay(f"unauth_x_{clean_username}", 2.0, 4.0)
            
            is_blocked, is_proxy_issue = await _check_page_for_blocks(page, log)
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
                    
                    is_blocked, is_proxy_issue = await _check_page_for_blocks(page, log)
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
            await lazy_browser.close()


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
        direct_html = await _attempt_direct_scrape(target_url, proxy_url, account_id, log)
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
                        
                        page_html = await _attempt_direct_scrape(paginated_url, proxy_url, account_id, log)
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
                
        # 3. Fallback to browser (Playwright)
        log.info("unauth_x.search.direct_scrape.fallback_to_browser", account_id=account_id)
        lazy_browser = LazyBrowser(account_id=account_id, proxy_url=proxy_url, headless=headless)
        try:
            page = await lazy_browser.get_page()
            log.info("unauth_x.search.navigating", url=target_url)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            await _delay("unauth_x_search", 2.0, 4.0)
            
            is_blocked, is_proxy_issue = await _check_page_for_blocks(page, log)
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
                    
                    is_blocked, is_proxy_issue = await _check_page_for_blocks(page, log)
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
            await lazy_browser.close()
            
    return {
        "success": False,
        "tweets": [],
        "source_instance": None,
        "error": "All public Nitter instances were blocked, rate-limited, or failed to respond."
    }
