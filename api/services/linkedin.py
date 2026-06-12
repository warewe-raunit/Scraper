"""
api/services/linkedin.py — Core LinkedIn API scraping service utilizing
the authenticated active session and Voyager REST/Dash APIs.
"""

from __future__ import annotations

import re
import os
import sys
import json
import time
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import quote_plus, quote

import structlog

# Fix path to import core/tools modules correctly
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# No browser manager import needed since we use direct HTTP requests via curl_cffi.

logger = structlog.get_logger(__name__)
SESSIONS_DIR = ROOT / "sessions"

class LinkedInScraperService:
    def __init__(self, db: Optional[Any] = None):
        self.db = db

    def _parse_linkedin_accounts(self) -> List[Dict[str, Any]]:
        """Parse LINKEDIN_ACCOUNT_<N> entries from the environment.

        Format: account_id|username|password[|proxy_url]
        Proxy via GOODPROXIES_* if 4th part omitted.
        """
        accounts = []
        pattern = re.compile(r"^LINKEDIN_ACCOUNT_\d+$")

        for key, value in os.environ.items():
            if pattern.match(key):
                try:
                    parts = value.split("|")
                    if len(parts) < 3 or len(parts) > 4:
                        continue
                    account_id = parts[0]
                    username = parts[1]
                    password = parts[2]
                    proxy_url = parts[3] if len(parts) == 4 else None

                    if "your_username" in username or "your_password" in password:
                        continue

                    accounts.append({
                        "account_id": account_id.strip(),
                        "username": username.strip(),
                        "password": password.strip(),
                        "proxy_url": proxy_url.strip() if proxy_url and proxy_url.strip() else None,
                    })
                except Exception as e:
                    logger.error("error_parsing_linkedin_account_env", key=key, error=str(e))

        accounts.sort(key=lambda x: x["account_id"])
        return accounts

    def _get_available_sessions(self) -> List[str]:
        """Get all LinkedIn account IDs with a saved session file."""
        if not SESSIONS_DIR.exists():
            return []
        
        account_ids = []
        for path in SESSIONS_DIR.glob("*.json"):
            name = path.stem
            if "__" in name:
                parts = name.split("__")
                account_id = parts[0]
            else:
                account_id = name
                
            if account_id.startswith("acc_li_") and account_id not in account_ids:
                account_ids.append(account_id)
                
        account_ids.sort()
        return account_ids

    def _resolve_session_file(self, target_account_id: str) -> Optional[Path]:
        """Locate the session JSON for an account (prefer mobile profile)."""
        session_file = SESSIONS_DIR / f"{target_account_id}__mobile.json"
        if not session_file.exists():
            session_file = SESSIONS_DIR / f"{target_account_id}.json"
        if not session_file.exists():
            for path in SESSIONS_DIR.glob(f"{target_account_id}*.json"):
                return path
            return None
        return session_file

    def _load_session_cookies(self, session_file: Path) -> tuple[Optional[str], Optional[str], dict]:
        """Return (cookie_header, csrf_token, raw_state).

        CSRF derives from JSESSIONID — correct LinkedIn Voyager pattern.
        Edge clearance cookies (__cf_bm, bcookie, lidc, li_at) ride along in
        the Cookie header; staleness here is what triggers 302→/login.
        """
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            logger.error("voyager_session_read_failed", path=str(session_file), error=str(e))
            return None, None, {}

        cookie_parts = []
        csrf = None
        for cookie in state.get("cookies", []):
            cookie_parts.append(f"{cookie['name']}={cookie['value']}")
            if cookie.get("name") == "JSESSIONID":
                csrf = cookie.get("value", "").replace('"', '')

        if not cookie_parts:
            return None, None, state
        return "; ".join(cookie_parts), csrf, state

    def _merge_set_cookies_into_session(self, target_account_id: str, resp: Any) -> int:
        """Parse Set-Cookie headers from a 302 response and merge them into the
        session JSON. Returns count of cookies updated/added.

        This is the lightweight self-heal — works because LinkedIn's 302→same-URL
        ships fresh edge cookies in Set-Cookie. No browser launch required.
        """
        if resp is None:
            return 0

        session_file = self._resolve_session_file(target_account_id)
        if not session_file:
            return 0

        # curl_cffi headers: get_list returns list of values for the same header
        set_cookie_values: List[str] = []
        headers_obj = resp.headers
        try:
            getter = getattr(headers_obj, "get_list", None)
            if callable(getter):
                set_cookie_values = list(getter("set-cookie") or [])
            else:
                # Fallback: scan items()
                for k, v in headers_obj.items():
                    if k.lower() == "set-cookie":
                        set_cookie_values.append(v)
        except Exception:
            pass

        if not set_cookie_values:
            return 0

        from http.cookies import SimpleCookie
        parsed = {}
        for raw in set_cookie_values:
            try:
                c = SimpleCookie()
                c.load(raw)
                for name, morsel in c.items():
                    parsed[name] = {
                        "name": name,
                        "value": morsel.value,
                        "domain": morsel["domain"] or ".linkedin.com",
                        "path": morsel["path"] or "/",
                    }
            except Exception:
                continue

        if not parsed:
            return 0

        with open(session_file, "r", encoding="utf-8") as f:
            state = json.load(f)

        existing = {c["name"]: c for c in state.get("cookies", [])}
        updated = 0
        for name, new_c in parsed.items():
            if name in existing:
                if existing[name].get("value") != new_c["value"]:
                    existing[name]["value"] = new_c["value"]
                    updated += 1
            else:
                existing[name] = {
                    "name": name,
                    "value": new_c["value"],
                    "domain": new_c["domain"],
                    "path": new_c["path"],
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "None",
                }
                updated += 1

        state["cookies"] = list(existing.values())
        session_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return updated

    async def _refresh_edge_cookies(self, target_account_id: str, proxy_url: Optional[str]) -> bool:
        """Self-heal: launch a Playwright context using THE SAME storage-state
        file that HTTP reads, navigate to /feed so Cloudflare refreshes the
        edge cookies, then write storage_state back to the same path.

        Preserves the original file if li_at gets killed during the visit.
        """
        from playwright.async_api import async_playwright

        session_file = self._resolve_session_file(target_account_id)
        if not session_file:
            logger.error("voyager_refresh.no_session_file", account_id=target_account_id)
            return False

        logger.info("voyager_refresh.start", account_id=target_account_id, session_file=str(session_file))

        # Snapshot original for rollback if li_at dies during the visit.
        try:
            original_state = json.loads(session_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("voyager_refresh.read_failed", error=str(e))
            return False

        # iPhone-class UA so LinkedIn serves the same mobile flavor the HTTP request impersonates.
        ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"

        pw = None
        browser = None
        try:
            pw = await async_playwright().start()
            launch_kwargs: dict = {"headless": True, "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"]}
            if proxy_url:
                from tools.proxy_config import playwright_proxy_config
                pc = playwright_proxy_config(proxy_url)
                if pc:
                    launch_kwargs["proxy"] = pc

            browser = await pw.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                user_agent=ua,
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
                locale="en-US",
                storage_state=original_state,
            )
            page = await context.new_page()

            # Use a lighter authenticated endpoint than /feed — /feed runs heavy
            # JS that LinkedIn watches for headless tells. /mynetwork still requires
            # auth (proves li_at works) and triggers a Cloudflare edge-cookie issue,
            # but loads less anti-bot JS.
            refresh_url = os.getenv("LINKEDIN_REFRESH_URL", "https://www.linkedin.com/mynetwork/")
            try:
                await page.goto(refresh_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                logger.warning("voyager_refresh.goto_warn", error=str(e))
            await asyncio.sleep(2.5)

            new_state = await context.storage_state()
            cookies = new_state.get("cookies", [])
            has_li_at = any(c.get("name") == "li_at" and c.get("value") for c in cookies)
            cf_bm_count = sum(1 for c in cookies if c.get("name") == "__cf_bm")
            final_url = page.url
            logger.info("voyager_refresh.post_nav", final_url=final_url, has_li_at=has_li_at, cookie_count=len(cookies))

            if not has_li_at:
                logger.error("voyager_refresh.li_at_lost_keeping_original",
                             account_id=target_account_id, final_url=final_url)
                return False

            session_file.write_text(json.dumps(new_state, indent=2), encoding="utf-8")
            logger.info(
                "voyager_refresh.success",
                account_id=target_account_id,
                cookies_count=len(cookies),
                cf_bm_count=cf_bm_count,
                path=str(session_file),
            )
            return True
        except Exception as e:
            logger.error("voyager_refresh.exception", error=str(e))
            return False
        finally:
            try:
                if browser is not None:
                    await browser.close()
            except Exception:
                pass
            try:
                if pw is not None:
                    await pw.stop()
            except Exception:
                pass

    async def _voyager_get(self, api_path: str, account_id: Optional[str] = None) -> Optional[dict]:
        """Top-level fetch: acquires an account from the pool, runs the request,
        releases with health signal. Retries across accounts on session death.

        For pool-less explicit account targeting (legacy callers, debug scripts),
        passing `account_id` bypasses the pool.
        """
        if account_id is not None:
            # Legacy single-account path — used by debug/inspection scripts.
            return await self._voyager_get_for_account(api_path, account_id)

        from api.services.linkedin_account_pool import LinkedInAccountPool, NoAccountAvailable
        pool = await LinkedInAccountPool.instance()

        max_account_swaps = int(os.getenv("LINKEDIN_REQUEST_ACCOUNT_SWAPS", "5"))
        last_result: Optional[dict] = None

        for swap in range(max_account_swaps):
            try:
                acc = await pool.acquire()
            except NoAccountAvailable:
                logger.error("voyager_get.no_account_available")
                return None

            session_redirected = False
            success = False
            try:
                logger.info("voyager_get.account_acquired",
                            account_id=acc.account_id, swap=swap + 1, status=acc.status)
                result, session_redirected = await self._voyager_get_for_account_with_signal(
                    api_path, acc.account_id
                )
                if result is not None:
                    success = True
                    last_result = result
            finally:
                await pool.release(acc.account_id,
                                    success=success,
                                    session_redirected=session_redirected)

            if success:
                return last_result

            # Swap to next account if session redirect — otherwise the failure is
            # something we won't fix by changing accounts (network/proxy issues).
            if not session_redirected:
                return None

        logger.warning("voyager_get.exhausted_account_swaps",
                       attempts=max_account_swaps)
        return last_result

    async def _voyager_get_for_account_with_signal(
        self, api_path: str, target_account_id: str
    ) -> tuple[Optional[dict], bool]:
        """Run the request, return (json_or_none, session_redirected_flag)."""
        try:
            return await self._voyager_get_for_account(api_path, target_account_id, return_signal=True)
        except Exception as e:
            logger.error("voyager_get_for_account.exception", error=str(e)[:200])
            return None, False

    async def _voyager_get_for_account(
        self,
        api_path: str,
        target_account_id: str,
        return_signal: bool = False,
    ):
        """Direct HTTP fetch via curl_cffi for ONE specific account.

        Self-healing: on 302 → merge Set-Cookies and retry. On second 302 the
        session is genuinely dead — caller (pool wrapper) will mark account
        dead and retry on a different account.

        If return_signal=True, returns (json_or_none, session_redirected).
        Else returns json_or_none (legacy interface).
        """
        # Verify session file exists for this account
        if not self._resolve_session_file(target_account_id):
            if return_signal:
                return None, False
            return None

        # Resolve proxy (rotating proxy pool or static proxy from .env)
        from tools.goodproxies import GoodProxiesProvider
        gp = GoodProxiesProvider()

        static_proxy = None
        accounts_info = self._parse_linkedin_accounts()
        for acc in accounts_info:
            if acc["account_id"] == target_account_id:
                static_proxy = acc.get("proxy_url")
                break

        # If the login flow pinned a sticky proxy into the session file, prefer it.
        # LinkedIn binds li_at to the login IP; using the same proxy here keeps the
        # IP class stable so the session validates.
        login_proxy = None
        try:
            sf = self._resolve_session_file(target_account_id)
            if sf:
                with open(sf, "r", encoding="utf-8") as f:
                    st = json.load(f)
                login_proxy = st.get("_login_proxy")
        except Exception:
            pass

        # Clear any stale cooldown on the login proxy — earlier versions of this
        # code cooled it down on 4xx LinkedIn responses (a working proxy), and
        # that cooldown may still be ticking. The login proxy is our best chance
        # at avoiding LinkedIn's IP-binding rejection.
        if login_proxy and gp.enabled and gp.pool is not None:
            try:
                gp.pool.clear(login_proxy)
                logger.info("voyager_get.cleared_login_proxy_cooldown",
                            account_id=target_account_id, proxy=login_proxy[:30] + "...")
            except Exception:
                pass

        max_proxy_attempts = int(os.getenv("LINKEDIN_PROXY_MAX_ATTEMPTS", "40"))
        proxy_timeout = int(os.getenv("LINKEDIN_PROXY_TIMEOUT", "8"))
        # Give the pinned login proxy a longer timeout — if it's slow but alive,
        # we want to use it instead of rotating to random pool proxies.
        login_proxy_timeout = int(os.getenv("LINKEDIN_LOGIN_PROXY_TIMEOUT", "15"))
        url = f"https://www.linkedin.com{api_path}"

        async def _attempt(proxy_url: Optional[str]) -> tuple[Optional[int], Optional[Any]]:
            session_file = self._resolve_session_file(target_account_id)
            if not session_file:
                raise FileNotFoundError(f"Session file not found for account {target_account_id}")

            cookie_header, csrf, _ = self._load_session_cookies(session_file)
            if not cookie_header:
                logger.warning("voyager_no_cookies", path=str(session_file))
                return None, None
            if not csrf:
                logger.warning("voyager_no_csrf_token", path=str(session_file))
                return None, None

            headers = {
                "accept": "application/vnd.linkedin.normalized+json+2.1",
                "accept-language": "en-US,en;q=0.9",
                "csrf-token": csrf,
                "x-restli-protocol-version": "2.0.0",
                "x-li-lang": "en_US",
                "x-li-track": '{"clientVersion":"1.13.0","mpVersion":"1.13.0","osName":"web","timezoneOffset":5.5,"deviceFormFactor":"DESKTOP","mpName":"voyager-web"}',
                "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
                "cookie": cookie_header,
                "referer": "https://www.linkedin.com/feed/",
            }

            from curl_cffi import requests
            session = requests.Session()
            if proxy_url:
                session.proxies = {"http": proxy_url, "https": proxy_url}

            # Login proxy gets a more patient timeout — it's the one most likely
            # to be IP-class compatible with the session.
            effective_timeout = login_proxy_timeout if proxy_url == login_proxy else proxy_timeout

            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: session.get(url, headers=headers, impersonate="chrome120", timeout=effective_timeout, allow_redirects=False),
            )
            return resp.status_code, resp

        async def _pick_proxy() -> Optional[str]:
            if gp.enabled and gp.api_key:
                p = await gp.get_proxy()
                if p:
                    return p
            return static_proxy

        async def _attempt_with_proxy_rotation(
            preferred_proxy: Optional[str] = None,
        ) -> tuple[Optional[int], Optional[Any], Optional[str]]:
            """Try fetching across rotating proxies. Cools-down dead proxies.

            If preferred_proxy is given, try it FIRST — used to pin a known-good
            proxy from a prior attempt (so we don't re-roll dice on retry).

            Returns (status, response, last_proxy_used).
            """
            last_status: Optional[int] = None
            last_resp: Optional[Any] = None
            last_proxy: Optional[str] = None
            for i in range(max_proxy_attempts):
                if i == 0 and preferred_proxy:
                    proxy_url = preferred_proxy
                else:
                    proxy_url = await _pick_proxy()
                last_proxy = proxy_url
                logger.info("voyager_fetch.attempt", attempt=i + 1, account_id=target_account_id,
                            proxy=(proxy_url[:30] + "...") if proxy_url else None,
                            pinned=(i == 0 and preferred_proxy is not None))
                try:
                    status, resp = await _attempt(proxy_url)
                except FileNotFoundError:
                    raise
                except Exception as e:
                    err = str(e)[:120]
                    logger.warning("voyager_fetch.proxy_connection_failed", attempt=i + 1, proxy=proxy_url, error=err)
                    # Only cool down on connection failures — NOT on server responses.
                    # A proxy that returned 302 worked perfectly; cooling it down
                    # drains the pool of working proxies.
                    if proxy_url and gp.enabled and gp.api_key:
                        gp.mark_failed(proxy_url)
                    if not (gp.enabled and gp.api_key):
                        return None, None, proxy_url
                    continue
                last_status, last_resp = status, resp
                # Network round-trip succeeded → proxy is healthy.
                # Explicitly clear any leftover cooldown so it stays in rotation.
                if proxy_url and gp.enabled and gp.pool is not None:
                    try:
                        gp.pool.clear(proxy_url)
                    except Exception:
                        pass
                return status, resp, proxy_url
            return last_status, last_resp, last_proxy

        status, resp, proxy_url = await _attempt_with_proxy_rotation(preferred_proxy=login_proxy)
        if status is None and resp is None:
            logger.error("voyager_fetch.all_proxies_exhausted", account_id=target_account_id, attempts=max_proxy_attempts)
            if return_signal:
                return None, False
            return None

        # Self-healing trigger: ANY 3xx from Voyager means edge cookies stale.
        # LinkedIn's Cloudflare layer responds 302 → same URL with fresh Set-Cookie
        # (bcookie, lidc, __cf_bm) asking the client to retry with refreshed edge state.
        def _is_auth_redirect(s: Optional[int], r: Any) -> bool:
            return s in (301, 302, 303, 307, 308)

        if _is_auth_redirect(status, resp):
            logger.warning("voyager_fetch.auth_redirect_detected", status=status,
                           location=(resp.headers.get("location") if resp is not None else None))
            # FAST PATH: harvest Set-Cookie from the 302 response and merge.
            # LinkedIn/Cloudflare's 302 → same-URL ships fresh bcookie/lidc/__cf_bm
            # in Set-Cookie. Browser refresh is destructive (kills li_at on headless
            # via datacenter IP) — merging response cookies is non-destructive.
            try:
                merged = self._merge_set_cookies_into_session(target_account_id, resp)
            except Exception as e:
                logger.warning("voyager_fetch.merge_failed", error=str(e))
                merged = 0

            # Pin the proxy that produced the 302 — it reached LinkedIn so it works.
            working_proxy = proxy_url

            if merged > 0:
                logger.info("voyager_fetch.cookies_merged", count=merged, retry_proxy=working_proxy)
                status, resp, proxy_url = await _attempt_with_proxy_rotation(preferred_proxy=working_proxy)
                if status is None and resp is None:
                    logger.error("voyager_fetch.retry_all_proxies_exhausted")
                    if return_signal:
                        return None, True
                    return None
            else:
                # No new cookies in 302 → session already dead, do not waste a
                # browser launch. Signal the pool to relogin this account.
                logger.info("voyager_fetch.no_setcookies_session_dead",
                            account_id=target_account_id)
                if return_signal:
                    return None, True
                return None

            # Second 302 after cookie merge → session is dead.
            if _is_auth_redirect(status, resp):
                logger.error("voyager_fetch.still_redirecting_session_dead",
                             location=(resp.headers.get("location") if resp is not None else None))
                if return_signal:
                    return None, True
                return None

        if status != 200 or resp is None:
            text_head = ""
            try:
                text_head = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            logger.warning("voyager_fetch.failed", status=status, text_head=text_head,
                           url_path=api_path[:300])
            # IMPORTANT: do NOT mark the proxy failed here. Any HTTP response
            # (including 400/401/403/404/500) means the proxy tunnel reached
            # LinkedIn perfectly — the rejection is server-side (bad query,
            # dead session, stale API path). Cooling down a working proxy
            # drains the pool of the few proxies that actually reach LinkedIn.
            session_dead = status in (401, 403)
            if return_signal:
                return None, session_dead
            return None

        logger.info("voyager_fetch.success", status=status, account_id=target_account_id)
        try:
            data = resp.json()
        except Exception as e:
            logger.error("voyager_fetch.json_parse_failed", error=str(e))
            if return_signal:
                return None, False
            return None

        if return_signal:
            return data, False
        return data

    def extract_profile_id(self, profile_or_url: str) -> str:
        """Extract profile ID/username from a LinkedIn URL or return the raw profile ID."""
        profile_or_url = profile_or_url.strip()
        if not profile_or_url:
            return ""
        if "linkedin.com" in profile_or_url or "/" in profile_or_url:
            # Matches /in/username/ or /in/username
            match = re.search(r"/in/([^/?#\s]+)", profile_or_url)
            if match:
                return match.group(1)
        return profile_or_url

    def extract_company_name(self, company_or_url: str) -> str:
        """Extract company name from a LinkedIn URL or return the raw company name."""
        company_or_url = company_or_url.strip()
        if not company_or_url:
            return ""
        if "linkedin.com" in company_or_url or "/" in company_or_url:
            # Matches /company/name/ or /company/name
            match = re.search(r"/company/([^/?#\s]+)", company_or_url)
            if match:
                return match.group(1)
        return company_or_url

    async def scrape_profile(self, public_id: str, account_id: Optional[str] = None) -> Dict[str, Any]:
        """Scrape professional profile details using Voyager API."""
        public_id = self.extract_profile_id(public_id)
        api_path = f"/voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity={public_id}&decorationId=com.linkedin.voyager.dash.deco.identity.profile.FullProfile-36"
        
        data = await self._voyager_get(api_path, account_id=account_id)
        if not data:
            raise RuntimeError(f"Failed to scrape LinkedIn profile '{public_id}' using Voyager API.")
            
        raw_data = self._parse_voyager_profile(data)
        
        # Save to database if db service configured
        if self.db and hasattr(self.db, "save_linkedin_profile"):
            try:
                await self.db.save_linkedin_profile(public_id, raw_data)
            except Exception as e:
                logger.error("failed_to_save_profile_to_db", public_id=public_id, error=str(e))
                
        return {
            "success": True,
            "public_id": public_id,
            "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "data": raw_data
        }

    def _parse_voyager_profile(self, data: dict) -> dict:
        """Parse a Voyager profile response into the standard profile structure."""
        included = data.get("included", [])
        
        # 1. Find profile object
        profile = {}
        for item in included:
            if item.get("$type") == "com.linkedin.voyager.dash.identity.profile.Profile":
                profile = item
                break
                
        if not profile:
            return {}
            
        first_name = profile.get("firstName", "")
        last_name = profile.get("lastName", "")
        name = f"{first_name} {last_name}".strip()
        headline = profile.get("headline", "")
        about = profile.get("summary", "")
        
        # Email / contact info
        contact = []
        email_obj = profile.get("emailAddress")
        if email_obj and isinstance(email_obj, dict):
            email = email_obj.get("emailAddress")
            if email:
                contact.append(f"Email: {email}")
                
        # 2. Extract experience
        experience = []
        positions = [item for item in included if item.get("$type") == "com.linkedin.voyager.dash.identity.profile.Position"]
        for pos in positions:
            title = pos.get("title", "")
            company = pos.get("companyName", "")
            desc = pos.get("description", "")
            loc = pos.get("locationName", "")
            
            time_period = pos.get("timePeriod")
            time_str = ""
            if time_period and isinstance(time_period, dict):
                start = time_period.get("startDate")
                end = time_period.get("endDate")
                
                start_str = ""
                if start and isinstance(start, dict):
                    s_month = start.get("month", "")
                    s_year = start.get("year", "")
                    start_str = f"{s_month}/{s_year}" if s_month else str(s_year)
                    
                end_str = "Present"
                if end and isinstance(end, dict):
                    e_month = end.get("month", "")
                    e_year = end.get("year", "")
                    end_str = f"{e_month}/{e_year}" if e_month else str(e_year)
                    
                if start_str:
                    time_str = f"{start_str} - {end_str}"
                    
            parts = []
            if title and company:
                parts.append(f"{title} at {company}")
            elif title:
                parts.append(title)
            elif company:
                parts.append(company)
                
            if time_str:
                parts.append(f"({time_str})")
            if loc:
                parts.append(f"in {loc}")
            if desc:
                parts.append(f"| {desc}")
                
            experience.append(" ".join(parts))
            
        # 3. Extract education
        education = []
        educations = [item for item in included if item.get("$type") == "com.linkedin.voyager.dash.identity.profile.Education"]
        for edu in educations:
            school = edu.get("schoolName", "")
            degree = edu.get("degreeName", "")
            field = edu.get("fieldOfStudy", "")
            desc = edu.get("description", "")
            
            time_period = edu.get("timePeriod")
            time_str = ""
            if time_period and isinstance(time_period, dict):
                start = time_period.get("startDate")
                end = time_period.get("endDate")
                start_year = start.get("year") if start else None
                end_year = end.get("year") if end else None
                if start_year and end_year:
                    time_str = f"({start_year} - {end_year})"
                elif start_year:
                    time_str = f"({start_year} - Present)"
                    
            edu_parts = []
            if degree and field:
                edu_parts.append(f"{degree} in {field}")
            elif degree:
                edu_parts.append(degree)
            elif field:
                edu_parts.append(field)
                
            if school:
                edu_parts.append(f"from {school}")
            if time_str:
                edu_parts.append(time_str)
            if desc:
                edu_parts.append(f"| {desc}")
                
            education.append(" ".join(edu_parts))
            
        # 4. Extract skills
        skills = []
        skills_list = [item for item in included if item.get("$type") == "com.linkedin.voyager.dash.identity.profile.Skill"]
        for sk in skills_list:
            name_sk = sk.get("name", "")
            if name_sk:
                skills.append(name_sk)
                
        return {
            "name": name,
            "headline": headline,
            "about": about,
            "experience": experience,
            "education": education,
            "skills": skills,
            "contact": contact
        }

    async def scrape_company(self, company_name: str, account_id: Optional[str] = None) -> Dict[str, Any]:
        """Scrape company organization details using Voyager API."""
        company_name = self.extract_company_name(company_name)
        api_path = f"/voyager/api/organization/dash/companies?q=universalName&universalName={company_name}"
        
        data = await self._voyager_get(api_path, account_id=account_id)
        if not data:
            raise RuntimeError(f"Failed to scrape LinkedIn company '{company_name}' using Voyager API.")
            
        raw_data = self._parse_voyager_company(data)
        return {
            "success": True,
            "company_name": company_name,
            "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "data": raw_data
        }

    def _parse_voyager_company(self, data: dict) -> dict:
        """Parse a Voyager company response into the standard organization structure."""
        included = data.get("included", [])
        
        comp = {}
        for item in included:
            if item.get("$type") == "com.linkedin.voyager.dash.organization.Company":
                comp = item
                break
                
        if not comp:
            return {}
            
        name = comp.get("name", "")
        tagline = comp.get("tagline", "")
        description = comp.get("description", "")
        website = comp.get("websiteUrl", "")
        
        # Handle locations
        locations = []
        grouped_locs = comp.get("groupedLocationsByCountry", [])
        for group in grouped_locs:
            country_name = group.get("localizedName", "")
            loc_list = group.get("locations", [])
            for loc in loc_list:
                addr = loc.get("address", {})
                city = addr.get("city", "")
                state = addr.get("geographicArea", "")
                line1 = addr.get("line1", "")
                zip_code = addr.get("postalCode", "")
                
                parts = [line1, city, state, zip_code, country_name]
                clean_parts = [p.strip() for p in parts if p and str(p).strip()]
                locations.append(", ".join(clean_parts))
                
        if not locations:
            loc_list = comp.get("locations", [])
            for loc in loc_list:
                addr = loc.get("address", {})
                city = addr.get("city", "")
                state = addr.get("geographicArea", "")
                line1 = addr.get("line1", "")
                zip_code = addr.get("postalCode", "")
                country = addr.get("country", "")
                
                parts = [line1, city, state, zip_code, country]
                clean_parts = [p.strip() for p in parts if p and str(p).strip()]
                locations.append(", ".join(clean_parts))
                
        detail_parts = []
        emp_count = comp.get("employeeCount")
        if emp_count:
            detail_parts.append(f"{emp_count} employees")
            
        founded = comp.get("foundedOn")
        if founded and isinstance(founded, dict):
            year = founded.get("year")
            if year:
                detail_parts.append(f"Founded in {year}")
                
        org_type = comp.get("organizationType")
        if org_type and isinstance(org_type, dict):
            t_name = org_type.get("localizedName")
            if t_name:
                detail_parts.append(t_name)
                
        details = " | ".join(detail_parts)
        
        return {
            "name": name,
            "tagline": tagline,
            "about": description,
            "details": details,
            "website": website,
            "locations": locations
        }

    async def _search_jobs_voyager(
        self,
        keywords: str,
        location: Optional[str],
        wt: str = "",
        jt: str = "",
        exp: str = "",
        tpr: str = "",
        limit: int = 25,
        account_id: Optional[str] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """Search jobs via the Voyager JSON API."""
        count = min(max(int(limit), 1), 100)

        # Captured-query override if set in env
        override = os.getenv("LINKEDIN_VOYAGER_JOBS_PATH", "").strip()
        if override:
            api_path = (
                override
                .replace("{keywords}", quote(keywords, safe=""))
                .replace("{location}", quote(location or "", safe=""))
                .replace("{count}", str(count))
                .replace("{start}", "0")
            )
            logger.info("voyager_using_override_path")
        else:
            filters = []
            if wt:
                filters.append(("workplaceType", wt))
            if jt:
                filters.append(("jobType", jt))
            if exp:
                filters.append(("experience", exp))
            if tpr:
                filters.append(("timePostedRange", tpr))
            filter_str = ",".join(
                "(key:%s,value:List(%s))" % (k, v) for k, v in filters
            )

            # Strip whitespace — LinkedIn rejects trailing spaces in keywords
            # and locationFallback with a 400.
            keywords_clean = (keywords or "").strip()
            location_clean = (location or "").strip()

            parts = ["origin:JOB_SEARCH_PAGE_QUERY_EXPANSION", "keywords:" + keywords_clean]
            if location_clean:
                parts.append("locationFallback:" + location_clean)
            if filter_str:
                parts.append("selectedFilters:(" + filter_str + ")")
            parts.append("spellCorrectionEnabled:true")
            query_value = "(" + ",".join(parts) + ")"

            # decorationId version is configurable via env so we can bump it
            # without code changes when LinkedIn rotates it.
            deco_version = os.getenv("LINKEDIN_VOYAGER_JOBS_DECO_VERSION", "190")
            api_path = (
                "/voyager/api/voyagerJobsDashJobCards"
                f"?decorationId=com.linkedin.voyager.dash.deco.jobs.search.JobSearchCardsCollection-{deco_version}"
                f"&count={count}"
                "&q=jobSearch"
                "&start=0"
                f"&query={quote(query_value, safe='():,->')}"
            )

        data = await self._voyager_get(api_path, account_id=account_id)
        if data is None:
            return None

        jobs = self._parse_voyager_jobs(data)
        if jobs is None:
            # Shape mismatch: dump raw JSON
            try:
                dump = ROOT / "linkedin_voyager_jobs_sample.json"
                dump.write_text(json.dumps(data, indent=2)[:200000], encoding="utf-8")
                logger.warning("voyager_jobs_shape_unrecognized_dumped", path=str(dump))
            except Exception as e:
                logger.warning("voyager_jobs_dump_failed", error=str(e))
            return None
        return jobs

    def _parse_voyager_jobs(self, data: dict) -> Optional[List[Dict[str, Any]]]:
        """Parse a Voyager jobs response into the standard job dict shape."""
        included = data.get("included")
        if not isinstance(included, list):
            return None

        jobs = []
        seen = set()
        for rec in included:
            if not isinstance(rec, dict):
                continue
            urn = rec.get("entityUrn", "") or ""
            title = rec.get("title")
            is_job = ("fsd_jobPosting" in urn or "jobPosting" in urn.lower()) and bool(title)
            if not is_job:
                continue

            job_id = None
            if ":" in urn:
                job_id = urn.rsplit(":", 1)[-1]
            if job_id and job_id in seen:
                continue

            company = None
            for k in ("companyName", "primaryDescription", "subtitle"):
                v = rec.get(k)
                if isinstance(v, dict):
                    v = v.get("text")
                if isinstance(v, str) and v.strip():
                    company = v.strip()
                    break

            loc = None
            for k in ("secondaryDescription", "formattedLocation", "tertiaryDescription"):
                v = rec.get(k)
                if isinstance(v, dict):
                    v = v.get("text")
                if isinstance(v, str) and v.strip():
                    loc = v.strip()
                    break

            url = None
            if job_id:
                url = f"https://www.linkedin.com/jobs/view/{job_id}/"

            if job_id:
                seen.add(job_id)
            jobs.append({
                "title": title.get("text") if isinstance(title, dict) else title,
                "url": url,
                "company": company,
                "location": loc,
                "details": "",
            })

        if not jobs:
            return None
        return jobs

    async def search_jobs(
        self,
        keywords: str,
        location: Optional[str] = None,
        workplace_types: Optional[List[str]] = None,
        job_types: Optional[List[str]] = None,
        experience_levels: Optional[List[str]] = None,
        date_posted: Optional[str] = None,
        limit: int = 25,
        account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Search job postings using Voyager API search."""
        available_ids = self._get_available_sessions()
        if not available_ids:
            raise RuntimeError("No active LinkedIn sessions found. Please run the login runner first.")

        target_account_id = account_id
        if not target_account_id or target_account_id not in available_ids:
            target_account_id = available_ids[0]

        accounts_info = self._parse_linkedin_accounts()
        proxy_url = None
        for acc in accounts_info:
            if acc["account_id"] == target_account_id:
                proxy_url = acc.get("proxy_url")
                break

        keywords = keywords.strip()
        
        # Workplace Types mapping: 1: On-site, 2: Remote, 3: Hybrid
        wt_map = {"on_site": "1", "remote": "2", "hybrid": "3"}
        wt_vals = []
        if workplace_types:
            for t in workplace_types:
                val = t.value if hasattr(t, "value") else str(t)
                val = val.strip().lower()
                if val in wt_map:
                    wt_vals.append(wt_map[val])
        _wt = ",".join(wt_vals) if wt_vals else ""
                
        # Job Types mapping: F: Full-time, P: Part-time, C: Contract, T: Temporary, I: Internship, V: Volunteer
        jt_map = {
            "full_time": "F",
            "part_time": "P",
            "contract": "C",
            "temporary": "T",
            "internship": "I",
            "volunteer": "V"
        }
        jt_vals = []
        if job_types:
            for t in job_types:
                val = t.value if hasattr(t, "value") else str(t)
                val = val.strip().lower()
                if val in jt_map:
                    jt_vals.append(jt_map[val])
        _jt = ",".join(jt_vals) if jt_vals else ""
                
        # Experience Levels mapping: 1: Internship, 2: Entry level, 3: Associate, 4: Mid-Senior level, 5: Director, 6: Executive
        e_map = {
            "internship": "1",
            "entry_level": "2",
            "associate": "3",
            "mid_senior": "4",
            "director": "5",
            "executive": "6"
        }
        e_vals = []
        if experience_levels:
            for t in experience_levels:
                val = t.value if hasattr(t, "value") else str(t)
                val = val.strip().lower()
                if val in e_map:
                    e_vals.append(e_map[val])
        _exp = ",".join(e_vals) if e_vals else ""
                
        # Date Posted mapping: r86400: Past 24h, r604800: Past week, r2592000: Past month
        tpr_map = {
            "past_24h": "r86400",
            "past_week": "r604800",
            "past_month": "r2592000"
        }
        _tpr = ""
        if date_posted:
            val = date_posted.value if hasattr(date_posted, "value") else str(date_posted)
            val = val.strip().lower()
            if val in tpr_map:
                _tpr = tpr_map[val]

        voyager_jobs = await self._search_jobs_voyager(
            keywords=keywords,
            location=location,
            wt=_wt, jt=_jt, exp=_exp, tpr=_tpr,
            limit=limit,
            account_id=target_account_id
        )
        
        if voyager_jobs is None:
            raise RuntimeError("Failed to fetch jobs using Voyager API search.")
            
        return {
            "success": True,
            "query": keywords,
            "location": location,
            "source": "voyager",
            "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "data": voyager_jobs[:limit]
        }

    async def search_blended(
        self,
        query: str,
        category: str = "all",
        account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Perform a blended or categorized universal search using Voyager API."""
        query = query.strip()
        
        category_map = {
            "all": None,
            "people": "PEOPLE",
            "companies": "COMPANIES",
            "jobs": "JOBS",
            "posts": "CONTENT",
            "groups": "GROUPS"
        }
        cat_str = category.value if hasattr(category, "value") else str(category)
        target_cat = category_map.get(cat_str.strip().lower())
        
        encoded_query = quote_plus(query)
        
        if target_cat:
            api_path = (
                "/voyager/api/search/dash/clusters"
                "?decorationId=com.linkedin.voyager.dash.deco.search.SearchClusterCollection-165"
                "&origin=GLOBAL_SEARCH_HEADER"
                "&q=all"
                f"&query=(flagshipSearchIntent:SEARCH_SRP,queryParameters:(keywords:List({encoded_query}),resultType:List({target_cat})),includeFiltersInResponse:false)"
                "&count=15"
                "&start=0"
            )
        else:
            api_path = (
                "/voyager/api/search/dash/clusters"
                "?decorationId=com.linkedin.voyager.dash.deco.search.SearchClusterCollection-165"
                "&origin=GLOBAL_SEARCH_HEADER"
                "&q=all"
                f"&query=(flagshipSearchIntent:SEARCH_SRP,queryParameters:(keywords:List({encoded_query})),includeFiltersInResponse:false)"
                "&count=15"
                "&start=0"
            )
            
        data = await self._voyager_get(api_path, account_id=account_id)
        if not data:
            raise RuntimeError("Failed to fetch search results from Voyager API.")
            
        return {
            "success": True,
            "query": query,
            "category": category,
            "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "data": self._parse_search_results(data, target_cat=target_cat)
        }

    def _parse_search_results(self, data: dict, target_cat: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
        """Parse blended search results into grouped categories."""
        included = data.get("included", [])
        urn_map = {item["entityUrn"]: item for item in included if "entityUrn" in item}
        
        results = {
            "people": [],
            "companies": [],
            "jobs": [],
            "groups": [],
            "flat_results": []
        }
        
        elements = data.get("data", {}).get("elements", [])
        for cluster in elements:
            cluster_title = cluster.get("title", {}).get("text", "").lower() if isinstance(cluster.get("title"), dict) else str(cluster.get("title") or "").lower()
            items = cluster.get("items", [])
            
            for item in items:
                union = item.get("itemUnion", {})
                ref = union.get("*entityResult")
                if not ref or ref not in urn_map:
                    continue
                    
                ent = urn_map[ref]
                
                title_obj = ent.get("title")
                title_text = ""
                if isinstance(title_obj, dict):
                    title_text = title_obj.get("text", "")
                elif isinstance(title_obj, str):
                    title_text = title_obj
                if not title_text:
                    title_text = "(untitled)"
                    
                url = ent.get("navigationUrl", "")
                
                subtitle_obj = ent.get("primarySubtitle")
                subtitle_text = ""
                if isinstance(subtitle_obj, dict):
                    subtitle_text = subtitle_obj.get("text", "")
                elif isinstance(subtitle_obj, str):
                    subtitle_text = subtitle_obj
                    
                if not subtitle_text:
                    summary_obj = ent.get("summary")
                    if isinstance(summary_obj, dict):
                        subtitle_text = summary_obj.get("text", "")
                    elif isinstance(summary_obj, str):
                        subtitle_text = summary_obj
                        
                result_item = {
                    "title": title_text,
                    "url": url,
                    "snippet": subtitle_text
                }
                
                assigned_cat = "flat_results"
                if url:
                    if "/in/" in url:
                        assigned_cat = "people"
                    elif "/company/" in url:
                        assigned_cat = "companies"
                    elif "/jobs/view/" in url:
                        assigned_cat = "jobs"
                    elif "/groups/" in url:
                        assigned_cat = "groups"
                        
                if assigned_cat == "flat_results" and cluster_title:
                    if "people" in cluster_title:
                        assigned_cat = "people"
                    elif "compan" in cluster_title:
                        assigned_cat = "companies"
                    elif "job" in cluster_title:
                        assigned_cat = "jobs"
                    elif "group" in cluster_title:
                        assigned_cat = "groups"
                        
                if target_cat:
                    cat_key = target_cat.lower()
                    if cat_key == "content":
                        cat_key = "posts"
                    if cat_key in results:
                        if not any(x["url"] == url for x in results[cat_key]):
                            results[cat_key].append(result_item)
                    else:
                        if not any(x["url"] == url for x in results["flat_results"]):
                            results["flat_results"].append(result_item)
                else:
                    if not any(x["url"] == url for x in results[assigned_cat]):
                        results[assigned_cat].append(result_item)
                        
        return {k: v for k, v in results.items() if v}
