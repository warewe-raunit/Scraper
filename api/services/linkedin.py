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

# Per-account "recently worked" proxy cache: account_id -> {proxy_url: last_success_ts}.
# LinkedIn binds li_at to an IP class; once a proxy returns 200 for an account it
# is the strongest candidate for the next request — stronger than anything the
# rotating pool can offer. In-memory (fast path); also persisted to the session
# JSON as _known_good_proxies so restarts keep the knowledge.
_RECENT_GOOD_PROXIES: Dict[str, Dict[str, float]] = {}
_STICKY_PROXY_TTL = int(os.getenv("LINKEDIN_STICKY_PROXY_TTL", "300"))


def _proxy_host(proxy_url: Optional[str]) -> Optional[str]:
    """Extract the host/IP from a proxy URL like http://1.2.3.4:8080."""
    if not proxy_url:
        return None
    m = re.search(r"://([^:/@]+)", proxy_url)
    return m.group(1) if m else None


def _subnet_distance(proxy_url: str, anchor_ip: Optional[str]) -> int:
    """0 = same /24 as anchor, 1 = same /16, 2 = unrelated. Lower sorts first.

    LinkedIn's IP binding is class-based — a proxy in the login proxy's subnet
    is far more likely to be accepted than a random residential IP.
    """
    if not anchor_ip:
        return 2
    ip = _proxy_host(proxy_url)
    if not ip:
        return 2
    a, b = ip.split("."), anchor_ip.split(".")
    if len(a) != 4 or len(b) != 4:
        return 2
    if a[:3] == b[:3]:
        return 0
    if a[:2] == b[:2]:
        return 1
    return 2


def _is_bare_400(status: Optional[int], resp: Any) -> bool:
    """LinkedIn's IP-binding rejection: HTTP 400 with a bare {"status":400}
    body (or empty body). A genuine bad-request 400 carries an error payload
    with details/message. Bare 400 means "this IP isn't yours" — the request
    itself is fine and will succeed from an accepted IP.
    """
    if status != 400 or resp is None:
        return False
    try:
        body = (resp.text or "").strip()
    except Exception:
        return False
    if not body:
        return True
    if len(body) > 200:
        return False
    try:
        j = json.loads(body)
    except Exception:
        return False
    return isinstance(j, dict) and set(j) <= {"status", "code", "message"} and not j.get("message")

class LinkedInScraperService:
    def __init__(self, db: Optional[Any] = None):
        self.db = db

    def _parse_linkedin_accounts(self) -> List[Dict[str, Any]]:
        """Parse LINKEDIN_ACCOUNT_<N> entries from the environment.

        Format: account_id|username|password[|proxy_url]. Proxy via GOODPROXIES_*
        when the 4th part is omitted. Delegates to the shared parser so the
        service, the pool, and the CLI runner can't drift.
        """
        from api.services.linkedin_env import parse_linkedin_accounts_env
        return parse_linkedin_accounts_env()

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

    def _record_good_proxy(self, target_account_id: str, proxy_url: Optional[str],
                           login_proxy: Optional[str]) -> None:
        """Remember a proxy that just returned 200 for this account.

        In-memory sticky cache (next request tries it right after the login
        proxy) + persisted _known_good_proxies in the session JSON (survives
        restarts, capped at 5, most recent first).
        """
        if not proxy_url:
            return
        _RECENT_GOOD_PROXIES.setdefault(target_account_id, {})[proxy_url] = time.time()
        if proxy_url == login_proxy:
            return
        session_file = self._resolve_session_file(target_account_id)
        if not session_file:
            return
        try:
            state = json.loads(session_file.read_text(encoding="utf-8"))
            goods = [p for p in state.get("_known_good_proxies", []) if p != proxy_url]
            goods.insert(0, proxy_url)
            state["_known_good_proxies"] = goods[:5]
            session_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
            logger.info("voyager_fetch.known_good_proxy_saved",
                        account_id=target_account_id, proxy=proxy_url[:30] + "...",
                        total=len(state["_known_good_proxies"]))
        except Exception as e:
            logger.warning("voyager_fetch.known_good_save_failed", error=str(e)[:120])

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

            # Preserve underscore-prefixed metadata keys (like _user_agent, _extra_http_headers, _login_proxy)
            for k, v in original_state.items():
                if isinstance(k, str) and k.startswith("_") and k not in new_state:
                    new_state[k] = v

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
        tried_accounts: set = set()

        for swap in range(max_account_swaps):
            try:
                acc = await pool.acquire()
            except NoAccountAvailable:
                logger.error("voyager_get.no_account_available")
                return last_result

            # Pool round-robin can hand back an account we already failed on this
            # request (e.g. only 1 ALIVE left). That means we've cycled through
            # every available account — stop rather than spin on a repeat.
            if acc.account_id in tried_accounts:
                await pool.release(acc.account_id, success=False,
                                   session_redirected=False, proxies_exhausted=False)
                break
            tried_accounts.add(acc.account_id)

            session_redirected = False
            proxies_exhausted = False
            success = False
            try:
                logger.info("voyager_get.account_acquired",
                            account_id=acc.account_id, swap=swap + 1, status=acc.status)
                result, session_redirected, proxies_exhausted = \
                    await self._voyager_get_for_account_with_signal(api_path, acc.account_id)
                if result is not None:
                    success = True
                    last_result = result
            finally:
                await pool.release(acc.account_id,
                                    success=success,
                                    session_redirected=session_redirected,
                                    proxies_exhausted=proxies_exhausted)

            if success:
                return last_result

            # Swap to another account when THIS account can't serve right now —
            # either its session is dying (302) or every proxy it could reach is
            # dead/IP-rejected (exhausted). A different account has a different
            # pinned proxy + fresh cookies, so it may well succeed. Only a hard
            # non-swappable failure (missing cookies, genuine bad query) stops us.
            if not (session_redirected or proxies_exhausted):
                return last_result

            logger.info("voyager_get.swapping_account",
                        from_account=acc.account_id, swap=swap + 1,
                        session_redirected=session_redirected,
                        proxies_exhausted=proxies_exhausted)

        logger.warning("voyager_get.exhausted_account_swaps",
                       attempts=len(tried_accounts))
        return last_result

    async def _classify_account_health(self, account_id: str) -> str:
        """Tri-state health verdict for an account's saved session:

            "healthy"      — a real Voyager call returned data.
            "dead"         — no session file, or the session 302→login (the
                             li_at is genuinely expired/invalid). Needs relogin.
            "inconclusive" — the probe failed for proxy/network reasons (no proxy
                             reached LinkedIn, or an exception). This proves
                             NOTHING about the session, so we must NOT relogin —
                             otherwise a flaky/empty proxy pool would falsely
                             condemn every account at once.
        """
        if not self._resolve_session_file(account_id):
            return "dead"  # nothing to validate — genuinely needs a login
        probe_path = os.getenv("LINKEDIN_VALIDATE_PROBE_PATH", "/voyager/api/me")
        try:
            result, redirected, _exhausted = \
                await self._voyager_get_for_account_with_signal(probe_path, account_id)
        except Exception as e:
            logger.warning("linkedin.validate_probe_error",
                           account_id=account_id, error=str(e)[:160])
            return "inconclusive"
        if result is not None:
            return "healthy"
        if redirected:
            return "dead"  # 302→login = the session itself is dead
        # result is None but NOT a redirect → couldn't reach LinkedIn at all
        # (proxy exhausted/unreachable). Session is presumed fine; don't relogin.
        return "inconclusive"

    async def validate_account(self, account_id: str) -> bool:
        """Back-compat boolean probe (True only when the session is confirmed
        alive). Inconclusive proxy/network failures count as not-healthy here but
        do NOT imply the session is dead — see _classify_account_health.
        """
        return (await self._classify_account_health(account_id)) == "healthy"

    async def validate_all_accounts(self) -> Dict[str, str]:
        """Probe every pool account in parallel and report each verdict to the
        pool. Only DEFINITIVE verdicts touch account state:

            healthy      → marked ALIVE (usable)
            dead         → marked DEAD + queued for relogin
            inconclusive → left untouched (no relogin) — the probe couldn't reach
                           LinkedIn, so we can't conclude the session is bad.

        Returns {account_id: verdict}.
        """
        from api.services.linkedin_account_pool import LinkedInAccountPool
        pool = await LinkedInAccountPool.instance()
        ids = pool.account_ids()
        if not ids:
            return {}

        async def _check(aid: str):
            verdict = await self._classify_account_health(aid)
            if verdict == "healthy":
                await pool.report_account_health(aid, True)
                if hasattr(pool, "confirm_verdict"):
                    pool.confirm_verdict(aid, "clear")
            elif verdict == "dead":
                await pool.report_account_health(aid, False)
            # inconclusive → deliberately no report (leave status as-is)
            return aid, verdict

        results = await asyncio.gather(*[_check(a) for a in ids], return_exceptions=True)
        verdict: Dict[str, str] = {}
        for r in results:
            if isinstance(r, Exception):
                continue
            aid, v = r
            verdict[aid] = v
        logger.info("linkedin.account_validation_complete",
                    healthy=sum(1 for v in verdict.values() if v == "healthy"),
                    dead=sum(1 for v in verdict.values() if v == "dead"),
                    inconclusive=sum(1 for v in verdict.values() if v == "inconclusive"),
                    total=len(ids))
        return verdict

    async def _voyager_get_for_account_with_signal(
        self, api_path: str, target_account_id: str
    ) -> tuple[Optional[dict], bool, bool]:
        """Run the request, return (json_or_none, session_redirected, proxies_exhausted)."""
        try:
            return await self._voyager_get_for_account(api_path, target_account_id, return_signal=True)
        except Exception as e:
            logger.error("voyager_get_for_account.exception", error=str(e)[:200])
            return None, False, False

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
                return None, False, False
            return None

        # Resolve proxy via the global provider — pre-filtered by geo and
        # healthchecked at fetch time, so every proxy in the pool is already
        # confirmed live and in the right country before we try it here.
        from tools.proxy_provider import get_proxy_provider
        gp = get_proxy_provider()

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
        login_country = None
        known_good_persisted: List[str] = []
        try:
            sf = self._resolve_session_file(target_account_id)
            if sf:
                with open(sf, "r", encoding="utf-8") as f:
                    st = json.load(f)
                login_proxy = st.get("_login_proxy")
                login_country = st.get("_login_country")
                kg = st.get("_known_good_proxies")
                if isinstance(kg, list):
                    known_good_persisted = [p for p in kg if isinstance(p, str)]
        except Exception:
            pass

        # If login_proxy is present but login_country is missing (legacy sessions), resolve and save it
        if login_proxy and not login_country:
            try:
                from tools.proxy_provider import resolve_proxy_country
                login_country = resolve_proxy_country(login_proxy)
                if login_country:
                    sf = self._resolve_session_file(target_account_id)
                    if sf:
                        with open(sf, "r", encoding="utf-8") as f:
                            st = json.load(f)
                        st["_login_country"] = login_country
                        with open(sf, "w", encoding="utf-8") as f:
                            json.dump(st, f, indent=2)
                        logger.info("voyager_get.resolved_and_saved_login_country",
                                    account_id=target_account_id, country=login_country)
            except Exception as e:
                logger.warning("voyager_get.save_login_country_failed", error=str(e))

        # Clear any stale cooldown on the login proxy — earlier versions of this
        # code cooled it down on 4xx LinkedIn responses (a working proxy), and
        # that cooldown may still be ticking. The login proxy is our best chance
        # at avoiding LinkedIn's IP-binding rejection.
        if login_proxy and gp.is_enabled() and gp.pool is not None:
            try:
                gp.pool.clear(login_proxy)
                logger.info("voyager_get.cleared_login_proxy_cooldown",
                            account_id=target_account_id, proxy=login_proxy[:30] + "...")
            except Exception:
                pass

        proxy_timeout = int(os.getenv("LINKEDIN_PROXY_TIMEOUT", "4"))
        # Give the pinned login proxy a longer timeout — if it's slow but alive,
        # we want to use it instead of rotating to random pool proxies.
        login_proxy_timeout = int(os.getenv("LINKEDIN_LOGIN_PROXY_TIMEOUT", "5"))
        url = f"https://www.linkedin.com{api_path}"

        async def _attempt(proxy_url: Optional[str]) -> tuple[Optional[int], Optional[Any]]:
            session_file = self._resolve_session_file(target_account_id)
            if not session_file:
                raise FileNotFoundError(f"Session file not found for account {target_account_id}")

            cookie_header, csrf, state = self._load_session_cookies(session_file)
            if not cookie_header:
                logger.warning("voyager_no_cookies", path=str(session_file))
                return None, None
            if not csrf:
                logger.warning("voyager_no_csrf_token", path=str(session_file))
                return None, None

            # Dynamically resolve user-agent and Client Hints headers from the saved state,
            # falling back to the deterministic profile if missing (e.g. legacy sessions).
            from tools.stealth.fingerprint import BrowserProfileManager
            from tools.browser_manager import _profile_http_headers

            user_agent = state.get("_user_agent")
            dynamic_headers = state.get("_extra_http_headers")
            is_mobile = False

            if not user_agent or not dynamic_headers:
                profile = BrowserProfileManager().generate(target_account_id)
                user_agent = profile["user_agent"]
                dynamic_headers = _profile_http_headers(profile)
                is_mobile = profile.get("is_mobile", False)
            else:
                is_mobile = "android" in user_agent.lower() or "iphone" in user_agent.lower()

            device_ff = "MOBILE" if is_mobile else "DESKTOP"
            os_name = "android" if is_mobile else "web"

            x_li_track = json.dumps({
                "clientVersion": "1.13.0",
                "mpVersion": "1.13.0",
                "osName": os_name,
                "timezoneOffset": 5.5,
                "deviceFormFactor": device_ff,
                "mpName": "voyager-web"
            }, separators=(',', ':'))

            headers = {
                "accept": "application/vnd.linkedin.normalized+json+2.1",
                "accept-language": "en-US,en;q=0.9",
                "csrf-token": csrf,
                "x-restli-protocol-version": "2.0.0",
                "x-li-lang": "en_US",
                "x-li-track": x_li_track,
                "user-agent": user_agent,
                "cookie": cookie_header,
                "referer": "https://www.linkedin.com/feed/",
            }

            # Align dynamic headers to avoid case duplicate conflicts
            for k, v in list(dynamic_headers.items()):
                if k.lower() in (hk.lower() for hk in headers):
                    base_key = next(hk for hk in headers if hk.lower() == k.lower())
                    del headers[base_key]
            headers.update(dynamic_headers)

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

        def _pick_proxy() -> Optional[str]:
            if gp.is_enabled():
                # Force same-country proxy rotation to avoid impossible travel bans
                p = gp.get_next(country=login_country, fallback_to_any=False)
                if p:
                    return p
            return static_proxy

        # --- Smart rotation -------------------------------------------------
        # Order of trust: pinned/login proxy → recently-good (in-memory) →
        # persisted known-good → freshly vetted pool proxies, closest subnet
        # first. Pool proxies are probed in PARALLEL batches against robots.txt
        # before the real request — only ~5% of any goodproxies batch is alive,
        # and probing 15 at once for 4s beats trying them serially for 8s each.
        probe_timeout = int(os.getenv("LINKEDIN_PROXY_PROBE_TIMEOUT", "4"))
        vet_batch_size = int(os.getenv("LINKEDIN_PROXY_VET_BATCH", "15"))
        vet_max = int(os.getenv("LINKEDIN_PROXY_VET_MAX", "45"))
        # Per-account budget. Kept short on purpose: for an IP-bound session,
        # random pool proxies mostly bare-400 anyway, so grinding the pool is
        # low-yield. Failing fast lets _voyager_get swap to another account whose
        # pinned proxy is alive — a far better bet than the pool lottery.
        fetch_budget = float(os.getenv("LINKEDIN_FETCH_BUDGET_SECONDS", "12"))
        storm_threshold = int(os.getenv("LINKEDIN_BARE400_STORM_THRESHOLD", "3"))

        async def _probe_ok(p: str) -> bool:
            from curl_cffi import requests as cr
            loop = asyncio.get_running_loop()
            try:
                r = await loop.run_in_executor(
                    None,
                    lambda: cr.get(
                        "https://www.linkedin.com/robots.txt",
                        proxies={"http": p, "https": p},
                        impersonate="chrome120",
                        timeout=probe_timeout,
                    ),
                )
                return 200 <= r.status_code < 400
            except Exception:
                return False

        async def _smart_rotation(
            preferred_proxy: Optional[str] = None,
        ) -> tuple[Optional[int], Optional[Any], Optional[str], bool]:
            """Returns (status, response, proxy_used, bare400_storm).

            A bare {"status":400} response from a healthy proxy is LinkedIn's
            IP-binding rejection — we rotate to the next candidate instead of
            aborting. storm_threshold distinct rejections means the session no
            longer accepts ANY reachable IP → caller should trigger relogin
            (which repins a fresh _login_proxy).
            """
            started = time.monotonic()
            tried: set = set()
            bare_400_seen = 0
            last: tuple = (None, None, None)
            login_ip = _proxy_host(login_proxy)
            attempt_no = 0

            async def _try(p: Optional[str]):
                nonlocal bare_400_seen, last, attempt_no
                attempt_no += 1
                logger.info("voyager_fetch.attempt", attempt=attempt_no,
                            account_id=target_account_id,
                            proxy=(p[:30] + "...") if p else None,
                            pinned=(p == preferred_proxy or p == login_proxy))
                try:
                    status, resp = await _attempt(p)
                except FileNotFoundError:
                    raise
                except Exception as e:
                    logger.warning("voyager_fetch.proxy_connection_failed",
                                   attempt=attempt_no, proxy=p, error=str(e)[:120])
                    # Only cool down on connection failures — NOT on server
                    # responses. Never cool down the login proxy: it may be
                    # briefly flaky and is irreplaceable without a relogin.
                    if p and p != login_proxy and gp.is_enabled():
                        gp.cool_down(p)
                    _RECENT_GOOD_PROXIES.get(target_account_id, {}).pop(p, None)
                    return None
                if status is None:
                    # Missing cookies/csrf — no proxy will fix this.
                    last = (None, None, p)
                    return (None, None, p, False)
                last = (status, resp, p)
                # Network round-trip succeeded → proxy is healthy; keep it in rotation.
                if p and gp.is_enabled() and gp.pool is not None:
                    try:
                        gp.pool.clear(p)
                    except Exception:
                        pass
                if _is_bare_400(status, resp):
                    bare_400_seen += 1
                    # Per-proxy rejection — recoverable (we rotate to the next
                    # proxy). NOT an account-health signal: the session stays
                    # healthy. Only the storm (every reachable proxy rejected) is
                    # request-level, logged at ERROR below.
                    logger.warning("voyager_fetch.bare_400_ip_reject",
                                   account_id=target_account_id, proxy=p,
                                   rejections=bare_400_seen)
                    if bare_400_seen >= storm_threshold:
                        return (status, resp, p, True)
                    return None  # rotate to next candidate
                return (status, resp, p, False)

            # Phase 1: trusted candidates — no probe needed, try directly.
            now = time.time()
            recent = _RECENT_GOOD_PROXIES.get(target_account_id, {})
            direct: List[str] = []
            for c in (preferred_proxy, login_proxy):
                if c and c not in direct:
                    direct.append(c)
            for p, ts in sorted(recent.items(), key=lambda kv: -kv[1]):
                if now - ts <= _STICKY_PROXY_TTL and p not in direct:
                    if login_country:
                        p_country = gp.proxy_countries.get(p)
                        if p_country and p_country != login_country:
                            continue
                    direct.append(p)
            for p in known_good_persisted:
                if p not in direct:
                    if login_country:
                        p_country = gp.proxy_countries.get(p)
                        if p_country and p_country != login_country:
                            continue
                    direct.append(p)

            for p in direct:
                if time.monotonic() - started > fetch_budget:
                    break
                if p in tried:
                    continue
                tried.add(p)
                outcome = await _try(p)
                if outcome is not None:
                    return outcome

            # No rotating pool configured → static proxy or direct connection.
            if not gp.is_enabled():
                fallback = static_proxy  # None → direct connection
                if fallback not in tried:
                    tried.add(fallback)
                    outcome = await _try(fallback)
                    if outcome is not None:
                        return outcome
                return last[0], last[1], last[2], bare_400_seen >= storm_threshold

            # Phase 2: vet pool proxies in parallel batches, send the real
            # request only through ones that just proved they reach LinkedIn.
            probed = 0
            while probed < vet_max and time.monotonic() - started < fetch_budget:
                batch: List[str] = []
                draws = 0
                while len(batch) < vet_batch_size and draws < vet_batch_size * 3:
                    draws += 1
                    p = _pick_proxy()
                    if not p:
                        break
                    if p in tried or p in batch:
                        continue
                    batch.append(p)
                if not batch:
                    break
                probed += len(batch)
                results = await asyncio.gather(*[_probe_ok(p) for p in batch])
                healthy = [p for p, ok in zip(batch, results) if ok]
                for p, ok in zip(batch, results):
                    if not ok:
                        tried.add(p)
                        gp.cool_down(p)  # global provider uses cool_down (no mark_failed)
                # LinkedIn's IP binding is class-based — same-subnet proxies first.
                healthy.sort(key=lambda p: _subnet_distance(p, login_ip))
                logger.info("voyager_fetch.vet_batch", account_id=target_account_id,
                            probed=len(batch), healthy=len(healthy), total_probed=probed)
                for p in healthy:
                    if p in tried:
                        continue
                    if time.monotonic() - started > fetch_budget:
                        break
                    tried.add(p)
                    outcome = await _try(p)
                    if outcome is not None:
                        return outcome
            # Exhausted. Any non-bare-400 server response would have returned
            # above, so reaching here with bare-400s seen means EVERY proxy that
            # reached LinkedIn was rejected — the session accepts none of our
            # reachable IPs. Storm regardless of count: relogin is the only fix.
            return last[0], last[1], last[2], bare_400_seen > 0

        # Signal tuple helper: (result, session_redirected, proxies_exhausted).
        # session_redirected → pool may mark account DEAD + relogin.
        # proxies_exhausted  → pool swaps account now + repins proxy (session ok).
        def _ret(data, *, redirected=False, exhausted=False):
            if return_signal:
                return data, redirected, exhausted
            return data

        def _is_auth_redirect(s: Optional[int]) -> bool:
            return s in (301, 302, 303, 307, 308)

        status, resp, proxy_url, bare400_storm = await _smart_rotation(preferred_proxy=login_proxy)

        if bare400_storm:
            # Healthy proxies all rejected with bare 400 — the session is bound to
            # an IP class we can no longer reach. Signal session death so the pool
            # swaps accounts NOW and background relogin repins a reachable proxy.
            logger.error("voyager_fetch.ip_binding_storm_triggering_relogin",
                         account_id=target_account_id)
            return _ret(None, redirected=True)
        if status is None and resp is None:
            # No proxy reached LinkedIn at all (pinned dead + pool unreachable).
            # Session is probably fine — swap to an account with a live proxy.
            logger.error("voyager_fetch.all_proxies_exhausted", account_id=target_account_id)
            return _ret(None, exhausted=True)

        # Self-heal on 302: LinkedIn/Cloudflare 302→same-URL ships fresh edge
        # cookies (bcookie/lidc/__cf_bm) in Set-Cookie. Rapid-fire requests churn
        # these constantly. Merge response cookies and retry on the SAME working
        # proxy across several rounds; if that won't converge, fall back to a
        # browser navigation (Playwright) which reliably re-mints edge cookies
        # without losing li_at. Only after both fail do we declare the session dead.
        heal_rounds = int(os.getenv("LINKEDIN_302_HEAL_ROUNDS", "3"))
        # Browser edge-refresh is OFF by default: navigating to LinkedIn through
        # the (datacenter-class) proxy headless gets bounced to remember-me-auto-
        # login and the resulting storage_state loses li_at, so it never heals —
        # it just burns ~8s. When HTTP cookie-merge can't converge, swapping to a
        # healthy account (different pinned proxy + fresh cookies) is faster and
        # more reliable. Background relogin re-mints this account's cookies anyway.
        browser_refresh_enabled = os.getenv("LINKEDIN_302_BROWSER_REFRESH", "false").lower() in ("1", "true", "yes", "on")

        if _is_auth_redirect(status):
            working_proxy = proxy_url  # produced the 302 → it reaches LinkedIn
            logger.warning("voyager_fetch.auth_redirect_detected", status=status,
                           account_id=target_account_id, working_proxy=working_proxy)

            healed = False
            for round_i in range(heal_rounds):
                try:
                    merged = self._merge_set_cookies_into_session(target_account_id, resp)
                except Exception as e:
                    logger.warning("voyager_fetch.merge_failed", error=str(e))
                    merged = 0
                if merged <= 0:
                    break  # nothing new to merge → HTTP heal can't progress
                logger.info("voyager_fetch.cookies_merged", round=round_i + 1,
                            count=merged, retry_proxy=working_proxy)
                status, resp, proxy_url, bare400_storm = await _smart_rotation(preferred_proxy=working_proxy)
                if bare400_storm:
                    logger.error("voyager_fetch.ip_binding_storm_triggering_relogin",
                                 account_id=target_account_id)
                    return _ret(None, redirected=True)
                if status is None and resp is None:
                    return _ret(None, exhausted=True)
                if not _is_auth_redirect(status):
                    healed = True
                    break

            # HTTP cookie-merge didn't converge → try a real browser navigation
            # through the same working proxy to re-mint edge cookies.
            if not healed and _is_auth_redirect(status) and browser_refresh_enabled:
                logger.info("voyager_fetch.browser_edge_refresh_attempt",
                            account_id=target_account_id, proxy=working_proxy)
                try:
                    refreshed = await self._refresh_edge_cookies(target_account_id, working_proxy)
                except Exception as e:
                    logger.warning("voyager_fetch.browser_refresh_exception", error=str(e)[:160])
                    refreshed = False
                if refreshed:
                    status, resp, proxy_url, bare400_storm = await _smart_rotation(preferred_proxy=working_proxy)
                    if bare400_storm:
                        return _ret(None, redirected=True)
                    if status is None and resp is None:
                        return _ret(None, exhausted=True)
                    if not _is_auth_redirect(status):
                        healed = True

            if not healed and _is_auth_redirect(status):
                logger.error("voyager_fetch.still_redirecting_session_dead",
                             account_id=target_account_id)
                return _ret(None, redirected=True)

        if status != 200 or resp is None:
            if status == 999:
                try:
                    from api.services.linkedin_account_pool import LinkedInAccountPool
                    pool = await LinkedInAccountPool.instance()
                    pool.record_signal(target_account_id, "soft_block")
                except Exception as e:
                    logger.warning("voyager_fetch.soft_block_record_failed", error=str(e))

            text_head = ""
            try:
                text_head = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            logger.warning("voyager_fetch.failed", status=status, text_head=text_head,
                           url_path=api_path[:300])
            # 401/403 = auth rejected → session dead (swap + relogin). A bare 400
            # storm was already handled above. Any other 4xx/5xx is a server-side
            # rejection of THIS query (bad params/path) — not fixable by swapping,
            # so report failure without thrashing accounts.
            session_dead = status in (401, 403)
            return _ret(None, redirected=session_dead)

        logger.info("voyager_fetch.success", status=status, account_id=target_account_id,
                    proxy=(proxy_url[:30] + "...") if proxy_url else None)
        # This proxy just proved the session accepts its IP — remember it so the
        # next request skips the lottery entirely.
        self._record_good_proxy(target_account_id, proxy_url, login_proxy)
        try:
            data = resp.json()
        except Exception as e:
            logger.error("voyager_fetch.json_parse_failed", error=str(e))
            return _ret(None)

        return _ret(data)

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

    def extract_activity_urn(self, post_or_url: str) -> Optional[str]:
        """Extract the canonical 'urn:li:activity:N' (or ugcPost/share) URN from a
        LinkedIn post URL, raw URN, or bare numeric id.

        Handles:
          - https://www.linkedin.com/posts/john-doe_slug-activity-7298...-AbCd
          - https://www.linkedin.com/feed/update/urn:li:activity:7298.../
          - urn:li:activity:7298...
          - 7298... (bare numeric id → assumed activity)
        Returns None if no id can be located.
        """
        if not post_or_url:
            return None
        s = post_or_url.strip()
        # Full urn already present (activity / ugcPost / share).
        m = re.search(r"urn:li:(activity|ugcPost|share):(\d+)", s)
        if m:
            return f"urn:li:{m.group(1)}:{m.group(2)}"
        # /posts/...-activity-<id>-<tracking> permalink form.
        m = re.search(r"(activity|ugcPost|share)[-:](\d{6,})", s)
        if m:
            return f"urn:li:{m.group(1)}:{m.group(2)}"
        # Bare numeric id → treat as an activity urn.
        m = re.search(r"\b(\d{12,})\b", s)
        if m:
            return f"urn:li:activity:{m.group(1)}"
        return None

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

    @staticmethod
    def _apply_path_override(env_var: str, default_path: str, subs: Dict[str, str]) -> str:
        """Return a captured GraphQL path from `env_var` (with {placeholders}
        filled) if set, else the hardcoded REST `default_path`.

        Every LinkedIn service uses this so any endpoint can be pointed at a
        captured GraphQL queryId path (see capture_linkedin_queries.py)
        when LinkedIn deprecates/rotates the REST decoration — without code
        changes. Placeholder values are URL-encoded by the caller as needed.
        """
        override = os.getenv(env_var, "").strip()
        if not override:
            return default_path
        out = override
        for key, val in subs.items():
            out = out.replace("{" + key + "}", val)
        logger.info("voyager.using_path_override", env_var=env_var)
        return out

    async def _paginate_voyager(
        self,
        build_path,            # (start: int, count: int) -> api_path str
        parse_page,            # (data: dict) -> Optional[List[dict]]
        *,
        limit: int,
        account_id: Optional[str],
        page_size: int,
        dedup_key,             # (item) -> hashable | None
        single_account: bool = False,  # pin ONE pooled account for the whole walk
    ) -> Optional[List[Dict[str, Any]]]:
        """Walk Voyager result pages by advancing the `start` offset until `limit`
        is reached, an empty page ends the set, or a safety ceiling trips.

        Design: we distribute parallel page requests across different accounts residing
        in the same country, rather than firing multiple concurrent requests on a single account.
        This prevents concurrent request rate-limit blocks and impossible travel bans.

        "Next page" is purely the API `start` offset — no browser. LinkedIn refuses
        very deep offsets (~1000), bounded by LINKEDIN_PAGINATION_MAX/_MAX_PAGES.
        Returns None only if NO page could be fetched at all.
        """
        hard_max = int(os.getenv("LINKEDIN_PAGINATION_MAX", "500"))
        max_pages = int(os.getenv("LINKEDIN_PAGINATION_MAX_PAGES", "30"))
        parallel = max(1, int(os.getenv("LINKEDIN_PARALLEL_PAGES", "3")))
        account_tries = int(os.getenv("LINKEDIN_PAGINATION_ACCOUNT_TRIES", "3"))
        target = max(1, min(int(limit), hard_max))
        page_size = max(1, page_size)
        # "Healthy enough" floor: a walk yielding fewer than this when more was
        # asked for signals a degraded session → try another account.
        healthy_floor = min(target, page_size)

        async def _walk_on(acct: str) -> tuple[List[Dict[str, Any]], bool, bool, bool]:
            """Returns (items, any_page_ok, session_redirected, proxies_exhausted).

            The last two carry the per-page health signals up to the pool so a
            session that died (or whose proxies are exhausted) mid-walk gets
            demoted/relogged — same self-healing the single-shot path gets.
            """
            out: List[Dict[str, Any]] = []
            seen: set = set()
            any_ok = False
            redirected = False
            exhausted = False
            next_idx = 0
            stop = False
            while not stop and next_idx < max_pages and len(out) < target:
                # Only fan out as many pages as we still need — never over-fetch.
                # ceil((target-have)/page_size), bounded by `parallel` and the
                # page ceiling. Underfilled pages are corrected on the next loop.
                remaining_pages = -(-(target - len(out)) // page_size)
                wave_size = max(1, min(parallel, remaining_pages, max_pages - next_idx))
                wave = []
                for _ in range(wave_size):
                    wave.append(next_idx * page_size)
                    next_idx += 1
                results = await asyncio.gather(*[
                    self._voyager_get_for_account_with_signal(build_path(s, page_size), acct)
                    for s in wave
                ], return_exceptions=True)
                for start, r in zip(wave, results):
                    if isinstance(r, Exception) or r is None:
                        logger.info("voyager_paginate.page", start=start, page_items="fail",
                                    added=0, total=len(out), target=target, account=acct)
                        continue
                    data, page_redirected, page_exhausted = r
                    redirected = redirected or page_redirected
                    exhausted = exhausted or page_exhausted
                    if data is None:
                        logger.info("voyager_paginate.page", start=start, page_items="fail",
                                    added=0, total=len(out), target=target, account=acct)
                        continue
                    any_ok = True
                    page = parse_page(data) or []
                    added = 0
                    for item in page:
                        k = dedup_key(item)
                        if k is not None and k in seen:
                            continue
                        if k is not None:
                            seen.add(k)
                        out.append(item)
                        added += 1
                        if len(out) >= target:
                            break
                    logger.info("voyager_paginate.page", start=start, page_items=len(page),
                                added=added, total=len(out), target=target, account=acct)
                    # Stop on a genuinely empty page, on no NEW items (a degraded
                    # session looping on the same duplicate — avoids walking to
                    # max_pages), or once the target is met.
                    if len(page) == 0 or added == 0 or len(out) >= target:
                        stop = True
                        break
            return out, any_ok, redirected, exhausted

        # Caller pinned a specific account → walk it directly, no pool.
        if account_id is not None:
            out, ok, _redirected, _exhausted = await _walk_on(account_id)
            return out[:target] if ok else None

        # Consistency mode: pin ONE pooled account for the whole walk so every
        # page describes a single coherent (per-account ranked) view. The
        # multi-account path below mixes accounts across pages, which is fine for
        # stable collections but produces an incoherent union for personalized
        # RELEVANCE-sorted ones like comments — different result every call.
        if single_account:
            from api.services.linkedin_account_pool import LinkedInAccountPool
            pool = await LinkedInAccountPool.instance()
            acc = await pool.acquire()
            out, ok, redirected, exhausted = [], False, False, False
            try:
                out, ok, redirected, exhausted = await _walk_on(acc.account_id)
            finally:
                await pool.release(acc.account_id, success=ok,
                                   session_redirected=redirected, proxies_exhausted=exhausted)
            return out[:target] if ok else None

        # Pool: distribute requests in parallel waves across different accounts from the same region
        from api.services.linkedin_account_pool import LinkedInAccountPool, NoAccountAvailable
        pool = await LinkedInAccountPool.instance()

        out: List[Dict[str, Any]] = []
        seen: set = set()
        any_ok_overall = False

        # Prepare pagination offsets
        offsets_to_fetch = [i * page_size for i in range(max_pages)]
        offset_ptr = 0

        # Bound the walk: a degraded proxy pool makes every page fail, and the
        # unbounded requeue below otherwise grinds until target is met (~400s seen
        # in prod). A wall-clock budget + per-offset retry cap returns partial
        # results fast instead. Tune via LINKEDIN_PAGINATION_BUDGET_SECONDS.
        deadline = time.monotonic() + float(os.getenv("LINKEDIN_PAGINATION_BUDGET_SECONDS", "60"))
        requeues: Dict[int, int] = {}

        while offset_ptr < len(offsets_to_fetch) and len(out) < target:
            if time.monotonic() > deadline:
                logger.warning("voyager_paginate.budget_exhausted",
                               collected=len(out), target=target)
                break
            remaining_needed = -(-(target - len(out)) // page_size)
            wave_size = min(parallel, remaining_needed, len(offsets_to_fetch) - offset_ptr)
            if wave_size <= 0:
                break

            wave_offsets = offsets_to_fetch[offset_ptr : offset_ptr + wave_size]

            try:
                # Acquire different accounts from the same country/region
                accounts = await pool.acquire_multi(count=wave_size)
            except NoAccountAvailable:
                logger.error("voyager_paginate.no_accounts_available")
                break

            actual_wave_size = len(accounts)
            if actual_wave_size <= 0:
                break

            wave_offsets = wave_offsets[:actual_wave_size]
            offset_ptr += actual_wave_size

            # Concurrently request pagination offsets using distinct accounts from the same country
            tasks = [
                self._voyager_get_for_account_with_signal(build_path(start, page_size), acc.account_id)
                for start, acc in zip(wave_offsets, accounts)
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            wave_stop = False
            for start, acc, r in zip(wave_offsets, accounts, results):
                success = False
                redirected = False
                exhausted = False
                data = None

                if isinstance(r, tuple) and len(r) == 3:
                    data, redirected, exhausted = r
                    if data is not None:
                        success = True

                if success:
                    any_ok_overall = True
                    page = parse_page(data) or []
                    added = 0
                    for item in page:
                        k = dedup_key(item)
                        if k is not None and k in seen:
                            continue
                        if k is not None:
                            seen.add(k)
                        out.append(item)
                        added += 1
                        if len(out) >= target:
                            break
                    logger.info("voyager_paginate.page", start=start, page_items=len(page),
                                added=added, total=len(out), target=target, account=acc.account_id)

                    if len(page) == 0 or added == 0:
                        wave_stop = True
                else:
                    logger.warning("voyager_paginate.page_failed", start=start, account=acc.account_id)
                    # Requeue this offset for a different account, but cap retries
                    # per offset (account_tries) so a dead pool can't requeue the
                    # same page forever.
                    requeues[start] = requeues.get(start, 0) + 1
                    if requeues[start] < account_tries:
                        offsets_to_fetch.append(start)

                # Release each account independently with its health status
                await pool.release(
                    acc.account_id,
                    success=success,
                    session_redirected=redirected,
                    proxies_exhausted=exhausted
                )

            if wave_stop or len(out) >= target:
                break

        if not any_ok_overall and not out:
            return None
        return out[:target]

    async def scrape_profile(self, public_id: str, account_id: Optional[str] = None) -> Dict[str, Any]:
        """Scrape professional profile details using Voyager API."""
        public_id = self.extract_profile_id(public_id)
        api_path = self._apply_path_override(
            "LINKEDIN_VOYAGER_PROFILE_PATH",
            f"/voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity={public_id}&decorationId=com.linkedin.voyager.dash.deco.identity.profile.FullProfile-36",
            {"public_id": quote(public_id, safe="")},
        )

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

    @staticmethod
    def _is_entity(item: dict, type_suffix: str, urn_kind: str) -> bool:
        """Match a Voyager included object by readable $type suffix (REST) OR by
        entityUrn kind (GraphQL/hashed-type fallback). entityUrns like
        urn:li:fsd_profilePosition:... are stable across both transports, so this
        keeps parsing working even if LinkedIn swaps decorations for queryIds.
        """
        t = str(item.get("$type", ""))
        if t.endswith(type_suffix):
            return True
        urn = str(item.get("entityUrn", ""))
        # Anchor the kind so fsd_profile doesn't match fsd_profilePosition etc.
        return bool(re.search(r"urn:li:%s:" % re.escape(urn_kind), urn))

    def _parse_voyager_profile(self, data: dict) -> dict:
        """Parse a Voyager profile response into the standard profile structure."""
        included = data.get("included", [])

        # 1. Find profile object — exact dash type first, then field/urn fallback
        #    so a GraphQL-shaped response (hashed type) still parses.
        profile = {}
        for item in included:
            if item.get("$type") == "com.linkedin.voyager.dash.identity.profile.Profile":
                profile = item
                break
        if not profile:
            for item in included:
                if (item.get("firstName") is not None and "lastName" in item
                        and re.search(r"urn:li:fsd_profile:", str(item.get("entityUrn", "")))):
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
        positions = [item for item in included if self._is_entity(item, ".profile.Position", "fsd_profilePosition")]
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
        educations = [item for item in included if self._is_entity(item, ".profile.Education", "fsd_profileEducation")]
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
        skills_list = [item for item in included if self._is_entity(item, ".profile.Skill", "fsd_profileSkill")]
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
        api_path = self._apply_path_override(
            "LINKEDIN_VOYAGER_COMPANY_PATH",
            f"/voyager/api/organization/dash/companies?q=universalName&universalName={company_name}",
            {"company": quote(company_name, safe="")},
        )

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
            # Fallback: a company object by entityUrn + a name field (GraphQL shape).
            for item in included:
                if (item.get("name") and re.search(r"urn:li:fsd_company:", str(item.get("entityUrn", "")))):
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

    async def scrape_post_comments(
        self,
        post_url: str,
        limit: int = 25,
        sort: str = "relevant",
        account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Scrape the comment thread on a LinkedIn post using the Voyager API.

        Accepts a post URL / activity URN / bare id, resolves the activity URN,
        then paginates the comments collection until `limit` comments are
        gathered. Follows the same REST-default + captured-GraphQL env override
        pattern as the other endpoints: set LINKEDIN_VOYAGER_COMMENTS_PATH to a
        captured queryId path (with {urn}/{count}/{start}/{sort} placeholders)
        when LinkedIn rotates the REST decoration.
        """
        urn = self.extract_activity_urn(post_url)
        if not urn:
            raise ValueError(f"Could not extract a post/activity id from: {post_url}")

        available_ids = self._get_available_sessions()
        if not available_ids:
            raise RuntimeError("No active LinkedIn sessions found. Please run the login runner first.")
        account_id = account_id if (account_id and account_id in available_ids) else None

        page_size = int(os.getenv("LINKEDIN_COMMENTS_PAGE_SIZE", "50"))
        # LinkedIn sortOrder enum: RELEVANCE (top) / REVERSE_CHRONOLOGICAL (recent).
        sort_key = (sort or "relevant").strip().lower()
        sort_order = "REVERSE_CHRONOLOGICAL" if sort_key in ("recent", "newest", "chronological") else "RELEVANCE"

        urn_enc = quote(urn, safe="")
        override = os.getenv("LINKEDIN_VOYAGER_COMMENTS_PATH", "").strip()
        if override:
            def build_path(start: int, count: int) -> str:
                return (override
                        .replace("{urn}", urn_enc)
                        .replace("{count}", str(count))
                        .replace("{start}", str(start))
                        .replace("{sort}", sort_order))
            logger.info("voyager_comments.using_override_path")
        else:
            def build_path(start: int, count: int) -> str:
                return (
                    "/voyager/api/feed/comments"
                    f"?count={count}"
                    "&q=comments"
                    f"&sortOrder={sort_order}"
                    f"&start={start}"
                    f"&updateId={urn_enc}"
                )

        # Fetch the post's own content alongside its comments — concurrently, so
        # it adds no latency. Comments pin a single account (single_account=True)
        # so paged offsets stay coherent; see _paginate_voyager.
        comments, post = await asyncio.gather(
            self._paginate_voyager(
                build_path, self._parse_voyager_comments,
                limit=limit, account_id=account_id, page_size=page_size,
                dedup_key=lambda c: c.get("comment_urn") or (c.get("author") or "") + (c.get("text") or "")[:60],
                single_account=(account_id is None),
            ),
            self._scrape_post_content(urn, account_id),
        )
        if comments is None:
            raise RuntimeError(f"Failed to fetch comments for post '{urn}' using Voyager API.")

        return {
            "success": True,
            "post_urn": urn,
            "post_url": f"https://www.linkedin.com/feed/update/{urn}/",
            "sort": sort_order,
            "post": post,
            "count": len(comments[:limit]),
            "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "data": comments[:limit],
        }

    async def _scrape_post_content(self, urn: str, account_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Fetch the post's own content (author/body/url) for the comments
        endpoint. REST default + LINKEDIN_VOYAGER_POST_PATH override ({urn}
        placeholder) — same rotation escape-hatch as the other endpoints.
        Non-fatal: returns None if the post can't be hydrated so comments still
        return.
        """
        urn_enc = quote(urn, safe="")
        override = os.getenv("LINKEDIN_VOYAGER_POST_PATH", "").strip()
        path = (override.replace("{urn}", urn_enc) if override
                else f"/voyager/api/feed/updatesV2/{urn_enc}?commentsCount=0&likesCount=0")
        try:
            data = await self._voyager_get(path, account_id=account_id)
        except Exception as e:
            logger.warning("voyager_post_content.failed", error=str(e)[:120])
            return None
        if not data:
            return None
        # The update object lands in `included` (REST) or at `data` (single-update
        # envelope); _extract_posts_from_included handles UpdateV2/FeedUpdate shapes.
        candidates = list(data.get("included") or [])
        main = data.get("data")
        if isinstance(main, dict):
            candidates.append(main)
        posts = self._extract_posts_from_included(candidates, snippet_len=0)
        if not posts:
            return None
        p = posts[0]
        return {"author": p.get("title"), "text": p.get("snippet"), "url": p.get("url") or f"https://www.linkedin.com/feed/update/{urn}/", "post_urn": urn}

    @staticmethod
    def _comment_text(node: Any, _depth: int = 0) -> str:
        """Recursively pull the human-readable body out of a comment's text
        container. LinkedIn nests the string as commentary/comment/value →
        {"text": "..."} (sometimes doubly), across both REST and GraphQL shapes.
        """
        if _depth > 5 or node is None:
            return ""
        if isinstance(node, str):
            return node
        if isinstance(node, dict):
            for key in ("text", "values", "value", "attributedText"):
                if key in node:
                    found = LinkedInScraperService._comment_text(node[key], _depth + 1)
                    if found:
                        return found
        if isinstance(node, list):
            for v in node:
                found = LinkedInScraperService._comment_text(v, _depth + 1)
                if found:
                    return found
        return ""

    def _parse_voyager_comments(self, data: dict) -> Optional[List[Dict[str, Any]]]:
        """Parse a Voyager comments response into a flat list of comment dicts.

        Tolerant of REST (com.linkedin.voyager.feed.Comment) and Dash/GraphQL
        (social.Comment / fsd_comment) shapes: a comment is any `included` object
        whose $type contains 'Comment' or whose entityUrn is a comment urn. Author
        name is resolved from an inline commenter object or a *commenter urn ref.
        """
        included = data.get("included")
        if not isinstance(included, list):
            return None

        urn_map = {item["entityUrn"]: item for item in included
                   if isinstance(item, dict) and "entityUrn" in item}

        def _resolve_author(comment: dict) -> str:
            commenter = comment.get("commenter")
            # Inline object: look for a name/title text field.
            if isinstance(commenter, dict):
                for key in ("title", "name", "displayName"):
                    txt = self._comment_text(commenter.get(key))
                    if txt:
                        return txt
                # Mini-profile style first/last name.
                first = commenter.get("firstName")
                last = commenter.get("lastName")
                if first or last:
                    return f"{first or ''} {last or ''}".strip()
                ref = commenter.get("*miniProfile") or commenter.get("*member")
                if isinstance(ref, str) and ref in urn_map:
                    prof = urn_map[ref]
                    return f"{prof.get('firstName', '')} {prof.get('lastName', '')}".strip()
            # Urn ref form: "*commenter": "urn:li:fsd_profile:..."
            ref = comment.get("*commenter")
            if isinstance(ref, str) and ref in urn_map:
                prof = urn_map[ref]
                for key in ("title", "name"):
                    txt = self._comment_text(prof.get(key))
                    if txt:
                        return txt
                return f"{prof.get('firstName', '')} {prof.get('lastName', '')}".strip()
            return ""

        comments: List[Dict[str, Any]] = []
        seen = set()
        for item in included:
            if not isinstance(item, dict):
                continue
            t = str(item.get("$type", ""))
            urn = str(item.get("entityUrn", ""))
            is_comment = ("Comment" in t) or bool(re.search(r"urn:li:(?:fsd_)?comment:", urn))
            if not is_comment:
                continue

            text = (self._comment_text(item.get("commentary"))
                    or self._comment_text(item.get("comment"))
                    or self._comment_text(item.get("commentV2")))
            if not text:
                continue
            if urn and urn in seen:
                continue
            if urn:
                seen.add(urn)

            created = item.get("createdAt") or item.get("createdTime")
            created_str = ""
            if isinstance(created, (int, float)) and created > 0:
                try:
                    created_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created / 1000))
                except Exception:
                    created_str = ""

            likes = 0
            social = item.get("socialDetail") or {}
            if isinstance(social, dict):
                lk = social.get("totalSocialActivityCounts") or social.get("likes") or {}
                if isinstance(lk, dict):
                    likes = lk.get("numLikes") or lk.get("total") or (lk.get("paging") or {}).get("total") or 0

            comments.append({
                "author": _resolve_author(item),
                "text": text.strip(),
                "created_at": created_str,
                "likes": likes,
                "comment_urn": urn or None,
            })

        if not comments:
            return None
        return comments

    # Verified LinkedIn geo URN ids for common locations. Anything not here is
    # resolved once via the typeahead API and cached; unresolvable locations are
    # omitted from the query (jobs return unfiltered rather than 400ing).
    _GEO_ID_CACHE: Dict[str, Optional[str]] = {}
    _KNOWN_GEO_IDS = {
        "india": "102713980",
        "united states": "103644278",
        "usa": "103644278",
        "us": "103644278",
        "united kingdom": "101165590",
        "uk": "101165590",
        "canada": "101174742",
        "australia": "101452733",
        "germany": "101282230",
        "singapore": "102454443",
        "worldwide": "92000000",
        "remote": "92000000",
    }

    async def _resolve_geo_id(self, location: str, account_id: Optional[str]) -> Optional[str]:
        """Map a human location string to LinkedIn's geo URN id.

        The jobs endpoint only accepts locationUnion:(geoId:N) — free-text
        location fields were removed from this API generation and 400 the
        whole request.
        """
        key = location.strip().lower()
        if not key:
            return None
        if key in self._GEO_ID_CACHE:
            return self._GEO_ID_CACHE[key]
        if key in self._KNOWN_GEO_IDS:
            self._GEO_ID_CACHE[key] = self._KNOWN_GEO_IDS[key]
            return self._GEO_ID_CACHE[key]

        geo_id: Optional[str] = None
        try:
            path = (
                "/voyager/api/graphql?variables=(keywords:" + quote(location, safe="")
                + ",query:(typeaheadFilterQuery:(geoSearchTypes:List(MARKET_AREA,COUNTRY_REGION,ADMIN_DIVISION_1,CITY))),type:GEO)"
                + "&queryId=voyagerSearchDashReusableTypeahead.57a4fa1dd92d3266ed968fdbab2d7bf5"
            )
            data = await self._voyager_get(path, account_id=account_id)
            if data:
                blob = json.dumps(data)
                m = re.search(r"urn:li:(?:fsd_geo|geo):(\d+)", blob)
                if m:
                    geo_id = m.group(1)
        except Exception as e:
            logger.warning("voyager_geo_typeahead_failed", location=location, error=str(e)[:120])

        if not geo_id:
            logger.warning("voyager_geo_unresolved_omitting_location", location=location)
        self._GEO_ID_CACHE[key] = geo_id
        return geo_id

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
        """Search jobs via the Voyager JSON API, paginating to satisfy `limit`."""
        page_size = int(os.getenv("LINKEDIN_JOBS_PAGE_SIZE", "100"))

        # Captured-query override if set in env. build_path inserts the page's
        # start/count so the paginator can advance through result pages.
        override = os.getenv("LINKEDIN_VOYAGER_JOBS_PATH", "").strip()
        if override:
            kw_enc = quote(keywords, safe="")
            loc_enc = quote(location or "", safe="")

            def build_path(start: int, count: int) -> str:
                return (override
                        .replace("{keywords}", kw_enc)
                        .replace("{location}", loc_enc)
                        .replace("{count}", str(count))
                        .replace("{start}", str(start)))
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
            # Map-style selectedFilters — the (key:X,value:List(Y)) pair format
            # belongs to the retired search API generation and bare-400s the
            # current voyagerJobsDashJobCards endpoint.
            filter_str = ",".join("%s:List(%s)" % (k, v) for k, v in filters)

            # Strip whitespace — LinkedIn rejects trailing spaces in keywords with a 400.
            keywords_clean = (keywords or "").strip()
            location_clean = (location or "").strip()

            parts = ["origin:JOB_SEARCH_PAGE_QUERY_EXPANSION", "keywords:" + keywords_clean]
            if location_clean:
                # Free-text location fields 400 this endpoint — must be a geo URN id.
                geo_id = await self._resolve_geo_id(location_clean, account_id)
                if geo_id:
                    parts.append(f"locationUnion:(geoId:{geo_id})")
            if filter_str:
                parts.append("selectedFilters:(" + filter_str + ")")
            parts.append("spellCorrectionEnabled:true")
            query_value = "(" + ",".join(parts) + ")"
            encoded_q = quote(query_value, safe='():,->')

            # decorationId version is configurable via env so we can bump it
            # without code changes when LinkedIn rotates it.
            deco_version = os.getenv("LINKEDIN_VOYAGER_JOBS_DECO_VERSION", "190")

            def build_path(start: int, count: int) -> str:
                return (
                    "/voyager/api/voyagerJobsDashJobCards"
                    f"?decorationId=com.linkedin.voyager.dash.deco.jobs.search.JobSearchCardsCollection-{deco_version}"
                    f"&count={count}"
                    "&q=jobSearch"
                    f"&start={start}"
                    f"&query={encoded_q}"
                )

        return await self._paginate_voyager(
            build_path, self._parse_voyager_jobs,
            limit=limit, account_id=account_id, page_size=page_size,
            dedup_key=lambda j: j.get("url") or j.get("title"),
        )

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

        # Only pin a concrete account if the caller explicitly named a valid one.
        # Otherwise pass None so _voyager_get uses the multi-account POOL path,
        # which round-robins ALIVE accounts and swaps on session-death / proxy
        # exhaustion. Forcing available_ids[0] here was defeating that — a
        # transient 302 on the first account 502'd the whole request.
        target_account_id = account_id if (account_id and account_id in available_ids) else None

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
        limit: int = 25,
        account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Perform a blended or categorized universal search using Voyager API.

        Category-specific searches (people/companies/groups/posts/jobs) paginate
        across `start` offsets to satisfy `limit`. Blended "all" stays a single
        page — it's an intentionally shallow cross-type teaser.
        """
        query = query.strip()
        search_page_size = int(os.getenv("LINKEDIN_SEARCH_PAGE_SIZE", "49"))

        # Validate the caller-supplied account exactly like search_jobs: an
        # unknown id must fall back to the multi-account POOL path, not pin the
        # whole walk to a nonexistent account (which yields an empty result).
        available_ids = self._get_available_sessions()
        if not available_ids:
            raise RuntimeError("No active LinkedIn sessions found. Please run the login runner first.")
        account_id = account_id if (account_id and account_id in available_ids) else None

        category_map = {
            "all": None,
            "people": "PEOPLE",
            "companies": "COMPANIES",
            "jobs": "JOBS",
            "posts": "CONTENT",
            "groups": "GROUPS"
        }
        cat_key_map = {"PEOPLE": "people", "COMPANIES": "companies",
                       "GROUPS": "groups", "CONTENT": "posts"}
        cat_str = category.value if hasattr(category, "value") else str(category)
        target_cat = category_map.get(cat_str.strip().lower())

        def _result(parsed: dict, note: Optional[str] = None) -> Dict[str, Any]:
            out = {
                "success": True,
                "query": query,
                "category": category,
                "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "data": parsed,
            }
            if note:
                out["note"] = note
            return out

        # JOBS via the blended clusters endpoint returns nothing useful — delegate
        # to the dedicated jobs search (paginates), reshaped into the blended contract.
        if target_cat == "JOBS":
            jobs = await self._search_jobs_voyager(keywords=query, location=None,
                                                   limit=limit, account_id=account_id)
            job_items = [
                {"title": j.get("title") or "", "url": j.get("url") or "",
                 "snippet": " · ".join(p for p in (j.get("company"), j.get("location")) if p)}
                for j in (jobs or [])
            ]
            return _result({"jobs": job_items})

        encoded_query = quote_plus(query)
        kw_enc = quote(query, safe="")
        content_override = os.getenv("LINKEDIN_VOYAGER_CONTENT_PATH", "").strip()
        search_override = os.getenv("LINKEDIN_VOYAGER_SEARCH_PATH", "").strip()

        _CLUSTERS_BASE = (
            "/voyager/api/search/dash/clusters"
            "?decorationId=com.linkedin.voyager.dash.deco.search.SearchClusterCollection-165"
            "&origin=GLOBAL_SEARCH_HEADER&q=all"
        )

        # ---- Posts / CONTENT (paginated when a captured GraphQL path exists) ----
        if target_cat == "CONTENT":
            if content_override:
                def build_posts(start: int, count: int) -> str:
                    return (content_override
                            .replace("{keywords}", kw_enc)
                            .replace("{count}", str(count))
                            .replace("{start}", str(start)))
                posts = await self._paginate_voyager(
                    build_posts,
                    lambda d: self._extract_posts_from_included(d.get("included", [])),
                    limit=limit, account_id=account_id, page_size=search_page_size,
                    dedup_key=lambda p: p.get("url") or (p.get("snippet") or "")[:80],
                )
                if posts is None:
                    raise RuntimeError("Failed to fetch search results from Voyager API.")
                return _result({"posts": posts})
            # No captured path → clusters returns content-less stubs. Honest empty + note.
            api_path = (_CLUSTERS_BASE +
                        f"&query=(flagshipSearchIntent:SEARCH_SRP,queryParameters:(keywords:List({encoded_query}),resultType:List(CONTENT)),includeFiltersInResponse:false)"
                        f"&count={search_page_size}&start=0")
            data = await self._voyager_get(api_path, account_id=account_id)
            if not data:
                raise RuntimeError("Failed to fetch search results from Voyager API.")
            posts = self._parse_search_results(data, target_cat="CONTENT").get("posts", [])
            return _result({"posts": posts}, note=(
                "LinkedIn no longer returns post bodies via the search clusters "
                "endpoint; set LINKEDIN_VOYAGER_CONTENT_PATH to a captured GraphQL "
                "content-search path to enable hydrated posts."))

        # ---- People / Companies / Groups (paginated) ----
        if target_cat:
            cat_key = cat_key_map.get(target_cat, "flat_results")
            if search_override:
                def build_search(start: int, count: int) -> str:
                    return (search_override
                            .replace("{keywords}", kw_enc)
                            .replace("{resultType}", target_cat)
                            .replace("{count}", str(count))
                            .replace("{start}", str(start)))
            else:
                def build_search(start: int, count: int) -> str:
                    return (_CLUSTERS_BASE +
                            f"&query=(flagshipSearchIntent:SEARCH_SRP,queryParameters:(keywords:List({encoded_query}),resultType:List({target_cat})),includeFiltersInResponse:false)"
                            f"&count={count}&start={start}")
            items = await self._paginate_voyager(
                build_search,
                lambda d: self._parse_search_results(d, target_cat=target_cat).get(cat_key, []),
                limit=limit, account_id=account_id, page_size=search_page_size,
                dedup_key=lambda x: x.get("url"),
            )
            if items is None:
                raise RuntimeError("Failed to fetch search results from Voyager API.")
            return _result({cat_key: items})

        # ---- Blended "all": single-page teaser + single-page hydrated-post merge ----
        api_path = self._apply_path_override(
            "LINKEDIN_VOYAGER_SEARCH_ALL_PATH",
            (_CLUSTERS_BASE +
             f"&query=(flagshipSearchIntent:SEARCH_SRP,queryParameters:(keywords:List({encoded_query})),includeFiltersInResponse:false)"
             f"&count={search_page_size}&start=0"),
            {"keywords": encoded_query, "count": str(search_page_size), "start": "0"},
        )
        data = await self._voyager_get(api_path, account_id=account_id)
        if not data:
            raise RuntimeError("Failed to fetch search results from Voyager API.")
        parsed = self._parse_search_results(data)
        parsed = {k: v[:limit] for k, v in parsed.items()}
        if content_override:
            try:
                cpath = (content_override.replace("{keywords}", kw_enc)
                         .replace("{count}", str(search_page_size)).replace("{start}", "0"))
                cdata = await self._voyager_get(cpath, account_id=account_id)
                if cdata:
                    hydrated = self._extract_posts_from_included(cdata.get("included", []))
                    if hydrated:
                        parsed["posts"] = hydrated[:limit]
            except Exception as e:
                logger.warning("voyager_blended_posts_merge_failed", error=str(e)[:120])
        return _result(parsed)

    def _extract_posts_from_included(self, included: List[dict], snippet_len: int = 500) -> List[Dict[str, Any]]:
        """Pull post/update results out of `included` feed-update objects.

        Content search hydrates posts as FeedUpdate/UpdateV2 objects (when a
        working content queryId is used). Each carries commentary text + an
        actor; the entityUrn maps to a /feed/update/ permalink. The clusters
        endpoint returns only stubs, so this yields [] there — by design.
        """
        def _text(obj):
            # LinkedIn text comes as {"text": "..."} sometimes nested twice.
            seen = 0
            while isinstance(obj, dict) and "text" in obj and seen < 4:
                obj = obj["text"]
                seen += 1
            return obj if isinstance(obj, str) else ""

        posts: List[Dict[str, Any]] = []
        seen_urns = set()
        for item in included:
            t = str(item.get("$type", ""))
            if not (("UpdateV2" in t or t.endswith(".Update") or "FeedUpdate" in t)):
                continue
            commentary = item.get("commentary")
            body = ""
            if isinstance(commentary, dict):
                body = _text(commentary.get("text")) or _text(commentary)
            if not body:
                continue
            urn = item.get("entityUrn") or item.get("updateMetadata", {}).get("urn") or ""
            if urn and urn in seen_urns:
                continue
            actor = item.get("actor") or {}
            author = _text(actor.get("name")) if isinstance(actor, dict) else ""
            url = ""
            # Prefer the clean inner activity/ugcPost/share urn for the permalink;
            # the entityUrn often wraps it as fsd_update:(urn:li:activity:N,...).
            m = (re.search(r"urn:li:activity:\d+", urn)
                 or re.search(r"urn:li:(?:ugcPost|share):\d+", urn))
            if m:
                url = f"https://www.linkedin.com/feed/update/{m.group(0)}/"
            if urn:
                seen_urns.add(urn)
            posts.append({
                "title": author or "(post)",
                "url": url,
                "snippet": body.strip()[:snippet_len] if snippet_len else body.strip(),
            })
        return posts

    @staticmethod
    def _find_cluster_elements(node: Any, _depth: int = 0) -> List[dict]:
        """Locate the search-cluster `elements` array regardless of envelope
        depth. REST puts it at data.elements; GraphQL nests it under the query
        name (data.<queryName>.elements). Returns the first `elements` list whose
        entries look like clusters (have an `items` array)."""
        if _depth > 6 or node is None:
            return []
        if isinstance(node, dict):
            els = node.get("elements")
            if isinstance(els, list) and any(isinstance(e, dict) and "items" in e for e in els):
                return els
            for v in node.values():
                found = LinkedInScraperService._find_cluster_elements(v, _depth + 1)
                if found:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = LinkedInScraperService._find_cluster_elements(v, _depth + 1)
                if found:
                    return found
        return []

    def _parse_search_results(self, data: dict, target_cat: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
        """Parse blended search results into grouped categories."""
        included = data.get("included", [])
        urn_map = {item["entityUrn"]: item for item in included if "entityUrn" in item}

        results = {
            "people": [],
            "companies": [],
            "jobs": [],
            "posts": [],
            "groups": [],
            "flat_results": []
        }

        # Posts come from hydrated feed-update objects in `included`, not from the
        # entityResult cluster items the loop below handles.
        results["posts"] = self._extract_posts_from_included(included)

        # The cluster `elements` array sits at data.data.elements in REST but is
        # nested deeper under the GraphQL queryId (e.g. data.data.<queryName>.
        # elements). Find it wherever it is so a captured SEARCH_PATH parses too.
        elements = self._find_cluster_elements(data.get("data", {}))
        for cluster in elements:
            cluster_title = cluster.get("title", {}).get("text", "").lower() if isinstance(cluster.get("title"), dict) else str(cluster.get("title") or "").lower()
            items = cluster.get("items", [])
            
            for item in items:
                # REST nests the ref under itemUnion; GraphQL under item.
                union = item.get("itemUnion") or item.get("item") or {}
                ref = union.get("*entityResult") if isinstance(union, dict) else None
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
