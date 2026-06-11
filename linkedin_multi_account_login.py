import argparse
import asyncio
import os
import random
import re
import sys
from dotenv import load_dotenv
import structlog

# Add current directory to path if not present
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools.browser_manager import LazyBrowser
from tools.linkedin_login import run_tool as login
from tools.linkedin_login_state import linkedin_login_state

def _configure_cli_logging() -> None:
    """Configure standalone structlog rendering for CLI use ONLY.

    Only configure when this file is run directly as a script.
    """
    log_format = os.getenv("LOG_FORMAT", "console").lower()
    processors = [
        structlog.processors.TimeStamper(fmt="iso" if log_format == "json" else "%Y-%m-%d %H:%M:%S"),
    ]
    if log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    structlog.configure(processors=processors)


logger = structlog.get_logger("linkedin_multi_account_login")

def parse_accounts_from_env() -> list[dict]:
    """Parse all account entries starting with LINKEDIN_ACCOUNT_ from .env."""
    accounts = []
    # Find all keys matching LINKEDIN_ACCOUNT_<N>
    pattern = re.compile(r"^LINKEDIN_ACCOUNT_\d+$")
    
    for key, value in os.environ.items():
        if pattern.match(key):
            try:
                parts = value.split("|")
                if len(parts) < 3 or len(parts) > 4:
                    logger.warning(
                        "invalid_account_format",
                        key=key,
                        value=value,
                        expected="account_id|username|password[|proxy_url]"
                    )
                    continue
                
                account_id = parts[0]
                username = parts[1]
                password = parts[2]
                proxy_url = parts[3] if len(parts) == 4 else None
                
                # Check for placeholders
                if "your_username" in username or "your_password" in password:
                    logger.warning(
                        "placeholder_credentials_detected",
                        account_id=account_id,
                        username=username,
                        msg="Skipping account due to placeholder values."
                    )
                    continue
                
                accounts.append({
                    "account_id": account_id.strip(),
                    "username": username.strip(),
                    "password": password.strip(),
                    "proxy_url": proxy_url.strip() if proxy_url and proxy_url.strip() else None
                })
            except Exception as e:
                logger.error("error_parsing_account", key=key, error=str(e))
                
    # Sort accounts by account_id to run them in a predictable order
    accounts.sort(key=lambda x: x["account_id"])
    return accounts

async def _resolve_sticky_proxy_for_login(account_id: str, static_proxy: str | None) -> str | None:
    """Resolve a sticky proxy for the entire login flow.

    Priority:
      1. Static proxy from env (LINKEDIN_ACCOUNT_N has 4th part) — used as-is.
      2. If LINKEDIN_LOGIN_USE_ROTATING=true and GoodProxies enabled — pick ONE
         healthy proxy that can reach LinkedIn and stick to it for entire login.
         (Login is multi-step; LinkedIn flags IP changes mid-flow.)
      3. None → direct connection.
    """
    if static_proxy:
        return static_proxy

    if os.getenv("LINKEDIN_LOGIN_USE_ROTATING", "true").lower() not in ("true", "1", "yes", "on"):
        return None

    from tools.goodproxies import GoodProxiesProvider
    from curl_cffi import requests as ccff_requests
    gp = GoodProxiesProvider()
    if not (gp.enabled and gp.api_key):
        return None

    # Vet proxies in parallel batches; pick the first that reaches LinkedIn.
    # Concurrent check is much faster than sequential and burns less wall-clock
    # before finding the rare ~3% healthy proxies.
    import asyncio as _aio
    max_vet = int(os.getenv("LINKEDIN_LOGIN_PROXY_VET_ATTEMPTS", "150"))
    batch_size = int(os.getenv("LINKEDIN_LOGIN_PROXY_VET_BATCH", "15"))

    async def _check(p: str) -> str | None:
        loop = _aio.get_running_loop()
        try:
            r = await loop.run_in_executor(
                None,
                lambda: ccff_requests.get(
                    "https://www.linkedin.com/robots.txt",
                    proxies={"http": p, "https": p},
                    impersonate="chrome120",
                    timeout=6,
                ),
            )
            if r.status_code == 200 and len(r.text) > 100:
                return p
        except Exception:
            pass
        gp.mark_failed(p)
        return None

    checked = 0
    while checked < max_vet:
        batch = []
        for _ in range(batch_size):
            p = await gp.get_proxy()
            if p:
                batch.append(p)
        if not batch:
            break
        results = await _aio.gather(*[_check(p) for p in batch])
        checked += len(batch)
        for r in results:
            if r:
                logger.info("linkedin_login.sticky_proxy_vetted", account_id=account_id, proxy=r[:30] + "...", checked=checked)
                return r

    logger.warning("linkedin_login.no_working_proxy_vetted", account_id=account_id, attempts=checked)
    return None


def _persist_login_proxy(account_id: str, proxy_url: str) -> None:
    """Write the working login proxy into the session file so scraping can prefer it."""
    try:
        import json as _json
        from pathlib import Path as _Path
        from tools.browser_manager import active_profile_session_id as _aps
        sess_id = _aps(account_id)
        sess_path = _Path(__file__).resolve().parent / "sessions" / f"{sess_id}.json"
        if sess_path.exists():
            d = _json.loads(sess_path.read_text(encoding="utf-8"))
            d["_login_proxy"] = proxy_url
            sess_path.write_text(_json.dumps(d, indent=2), encoding="utf-8")
            logger.info("login.proxy_pinned_to_session", path=str(sess_path), proxy=proxy_url[:30] + "...")
    except Exception as e:
        logger.warning("login.proxy_pin_failed", error=str(e))


async def _attempt_login(account_id, username, password, captcha_config, headless, proxy_url):
    """Run one full login attempt with a specific sticky proxy.

    Returns dict: {success: bool, proxy_navigation_failed: bool, error: str}.
    proxy_navigation_failed=True means the proxy itself broke (ERR_EMPTY_RESPONSE etc.),
    signalling the caller to swap proxies and retry.
    """
    display_proxy = "Direct"
    if proxy_url:
        display_proxy = (f"***@{proxy_url.split('@')[-1]}" if "@" in proxy_url else proxy_url)
    logger.info("starting_account_login", account_id=account_id, username=username,
                proxy=display_proxy, headless=headless)

    lazy_browser = LazyBrowser(account_id=account_id, proxy_url=proxy_url, headless=headless, use_rotating_proxy=False)
    PROXY_ERRS = ("ERR_EMPTY_RESPONSE", "ERR_TUNNEL", "ERR_PROXY", "ERR_TIMED_OUT", "ERR_CONNECTION", "net::ERR_")

    def _is_proxy_err(e: str) -> bool:
        return any(s in e for s in PROXY_ERRS)

    try:
        try:
            page = await lazy_browser.get_page()
            state = await linkedin_login_state(page, expected_username=username, navigate=True)
        except Exception as e:
            err = str(e)
            if _is_proxy_err(err):
                return {"success": False, "proxy_navigation_failed": True, "error": err[:200]}
            return {"success": False, "proxy_navigation_failed": False, "error": err[:200]}

        if state.get("logged_in"):
            logger.info("account_already_logged_in", account_id=account_id, reason=state.get("reason"))
            return {"success": True, "proxy_navigation_failed": False, "error": ""}

        logger.info("session_missing_or_expired_starting_login_flow", account_id=account_id)
        result = await login(page=page, account_id=account_id, username=username,
                             password=password, captcha_config=captcha_config)
        success = bool(result.get("success", False))
        err = (result.get("error") or "")
        if success:
            logger.info("login_completed_successfully", account_id=account_id, username=username)
            return {"success": True, "proxy_navigation_failed": False, "error": ""}
        if _is_proxy_err(err):
            logger.warning("linkedin_login.proxy_navigation_failed", proxy=proxy_url, error=err[:140])
            return {"success": False, "proxy_navigation_failed": True, "error": err[:200]}
        logger.error("login_failed", account_id=account_id, error=err[:200])
        return {"success": False, "proxy_navigation_failed": False, "error": err[:200]}
    finally:
        try:
            await lazy_browser.close()
        except Exception:
            pass


async def login_account(account: dict, captcha_config: dict | None, headless: bool) -> bool:
    account_id = account["account_id"]
    username = account["username"]
    password = account["password"]
    static_proxy = account["proxy_url"]

    max_proxy_swaps = int(os.getenv("LINKEDIN_LOGIN_PROXY_SWAP_MAX", "6"))

    last_proxy = None
    for swap in range(max_proxy_swaps):
        proxy_url = await _resolve_sticky_proxy_for_login(account_id, static_proxy)
        last_proxy = proxy_url
        if proxy_url is None and static_proxy is None and swap == max_proxy_swaps - 1:
            logger.warning("linkedin_login.falling_back_to_direct", account_id=account_id)

        result = await _attempt_login(account_id, username, password, captcha_config, headless, proxy_url)
        if result["success"]:
            if proxy_url:
                _persist_login_proxy(account_id, proxy_url)
            return True

        if result["proxy_navigation_failed"] and proxy_url:
            # The vetted proxy did not work inside Chromium. Mark dead, swap.
            try:
                from tools.goodproxies import GoodProxiesProvider
                GoodProxiesProvider().mark_failed(proxy_url)
            except Exception:
                pass
            logger.info("linkedin_login.swapping_proxy", account_id=account_id, attempt=swap + 1, max=max_proxy_swaps)
            continue

        # Non-proxy failure — login truly failed (bad creds, captcha unsolvable, etc.)
        return False

    logger.error("linkedin_login.all_proxy_swaps_exhausted", account_id=account_id, last_proxy=last_proxy)
    return False

async def main():
    load_dotenv(override=True)
    
    # Parse CLI arguments to allow targeting a specific account
    parser = argparse.ArgumentParser(description="Multi-account LinkedIn login flow.")
    parser.add_argument(
        "target_account", 
        nargs="?", 
        help="Optional specific account_id to run (e.g. acc_01). If not provided, all configured accounts will run."
    )
    args = parser.parse_args()
    
    accounts = parse_accounts_from_env()
    if not accounts:
        logger.error("no_valid_accounts_configured", msg="Please configure LINKEDIN_ACCOUNT_N variables in your .env file with real credentials.")
        return
        
    if args.target_account:
        accounts = [acc for acc in accounts if acc["account_id"] == args.target_account]
        if not accounts:
            logger.error("target_account_not_found", target=args.target_account)
            print(f"Error: Account with ID '{args.target_account}' not found in .env configurations.")
            return
        logger.info("filtered_single_account", target=args.target_account)
    else:
        logger.info("parsed_accounts", count=len(accounts))
    
    # Captcha settings (included to match Reddit tool logic)
    captcha_provider = os.getenv("CAPTCHA_PROVIDER")
    captcha_api_key = os.getenv("CAPTCHA_API_KEY")
    captcha_config = None
    if captcha_provider and captcha_api_key:
        captcha_config = {
            "provider": captcha_provider.strip(),
            "api_key": captcha_api_key.strip()
        }
        logger.info("captcha_solver_configured", provider=captcha_provider)
        
    # Lag settings
    lag_min = int(os.getenv("LOGIN_FLOW_LAG_MIN", "15"))
    lag_max = int(os.getenv("LOGIN_FLOW_LAG_MAX", "30"))
    
    headless = os.getenv("BROWSER_HEADLESS", "true").lower() in ("true", "1", "yes")
    
    summary = {}
    
    for idx, account in enumerate(accounts):
        account_id = account["account_id"]
        
        # Run login
        success = await login_account(account, captcha_config, headless)
        summary[account_id] = "SUCCESS" if success else "FAILED"
        
        # If not the last account, introduce a lag
        if idx < len(accounts) - 1:
            delay = random.uniform(lag_min, lag_max)
            logger.info("introducing_lag", next_delay_seconds=round(delay, 2))
            
            # Countdown print
            for remaining in range(int(delay), 0, -1):
                if remaining % 5 == 0 or remaining <= 5:
                    print(f"Waiting {remaining} seconds before starting the next account...")
                await asyncio.sleep(1)
            # Sleep fractional part
            await asyncio.sleep(delay - int(delay))
            
    print("\n=== LINKEDIN LOGIN RUN SUMMARY ===")
    for acc_id, status in summary.items():
        print(f"Account: {acc_id} -> {status}")
    print("==================================\n")

if __name__ == "__main__":
    _configure_cli_logging()
    asyncio.run(main())
