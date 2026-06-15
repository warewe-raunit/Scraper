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
                    "proxy_url": proxy_url.strip() if proxy_url and proxy_url.strip() else None,
                })
            except Exception as e:
                logger.error("error_parsing_account", key=key, error=str(e))
                
    # Sort accounts by account_id to run them in a predictable order
    accounts.sort(key=lambda x: x["account_id"])
    return accounts

_PROXY_ERR_MARKERS = (
    "ERR_EMPTY_RESPONSE", "ERR_TUNNEL", "ERR_PROXY", "ERR_TIMED_OUT",
    "ERR_CONNECTION", "net::ERR_", "Page.goto: Timeout",
)

# Login-side markers that also mean "swap proxy and retry": LinkedIn redirected
# to a region-locked variant (zh-cn, 451), served a checkpoint challenge to a
# suspect IP class, or silently dropped the auth call. None of these mean the
# credentials are wrong — they mean the proxy needs swapping.
_RETRYABLE_LOGIN_MARKERS = (
    "Login not confirmed",
    "/zh-cn/", "/checkpoint/pk/", "451 ",
    "Username or password fields not found",
    "Password field not found",
)


def _is_proxy_err(msg: str) -> bool:
    return any(s in msg for s in _PROXY_ERR_MARKERS)


def _is_retryable_login_err(msg: str) -> bool:
    return any(s in msg for s in _RETRYABLE_LOGIN_MARKERS)


def _mark_proxy_failed(proxy_url):
    if not proxy_url:
        return
    try:
        from tools.goodproxies import GoodProxiesProvider
        GoodProxiesProvider().mark_failed(proxy_url)
    except Exception:
        pass


async def _login_once(account_id, username, password, proxy_url, captcha_config, headless, use_rotating):
    """Single LazyBrowser launch + login attempt. Mirror of Reddit's login_account body.

    On Playwright proxy errors, captures the actually-used goodproxy from the
    browser context and feeds it back into the cooldown pool so the next
    swap doesn't pick the same dead proxy.
    """
    display_proxy = "Direct/Rotating" if use_rotating else (
        f"***:***@{proxy_url.split('@')[-1]}" if proxy_url and "@" in proxy_url else (proxy_url or "Direct")
    )
    logger.info("starting_account_login", account_id=account_id, username=username,
                proxy=display_proxy, headless=headless, use_rotating_proxy=use_rotating)

    lazy_browser = LazyBrowser(account_id=account_id, proxy_url=proxy_url,
                                headless=headless, use_rotating_proxy=use_rotating)

    def _used_proxy():
        # The proxy the session was actually established through — the IP class
        # LinkedIn binds li_at to. Must be pinned into the session on success.
        return lazy_browser.last_used_proxy if use_rotating else proxy_url

    try:
        try:
            page = await lazy_browser.get_page()
            state = await linkedin_login_state(page, expected_username=username, navigate=True)
        except Exception as e:
            err = str(e)
            if _is_proxy_err(err):
                _mark_proxy_failed(lazy_browser.last_used_proxy)
                return "proxy_err", err[:200], None
            return "fatal", err[:200], None

        if state.get("logged_in"):
            logger.info("account_already_logged_in", account_id=account_id, reason=state.get("reason"))
            return "success", "", _used_proxy()

        # FAST PATH: saved-session identity cookies still recognize the user,
        # so LinkedIn jumped straight to /checkpoint/ asking only for the email
        # PIN — skip the full credentials submit, just solve the OTP.
        try:
            current_url = (page.url or "").lower()
            on_checkpoint = ("/checkpoint/" in current_url
                              or "verification" in current_url
                              or "challenge" in current_url)
        except Exception:
            on_checkpoint = False

        if on_checkpoint:
            from tools.linkedin_login import _handle_email_verification_challenge
            logger.info("session_validation_landed_on_checkpoint_fast_path",
                        account_id=account_id, url=page.url)
            try:
                solved = await _handle_email_verification_challenge(page, username, account_id)
            except Exception as exc:
                logger.warning("login.fast_path_otp_failed", error=str(exc)[:200])
                solved = False
            if solved:
                state_after = await linkedin_login_state(page, expected_username=username, navigate=False)
                if state_after.get("logged_in"):
                    logger.info("login_completed_via_otp_fast_path", account_id=account_id)
                    return "success", "", _used_proxy()
                logger.warning("login.fast_path_otp_did_not_unblock", account_id=account_id)
            # Fall through to the full credentials flow if fast path failed.

        logger.info("session_missing_or_expired_starting_login_flow", account_id=account_id)
        result = await login(page=page, account_id=account_id, username=username,
                              password=password, captcha_config=captcha_config)
        if result.get("success"):
            logger.info("login_completed_successfully", account_id=account_id)
            return "success", "", _used_proxy()

        err = result.get("error") or ""
        # OTP challenge was already handled on this proxy. Swapping wastes the
        # verification window and burns another OTP, so treat as fatal.
        if "[OTP_CHALLENGE_HANDLED]" in err:
            logger.error("login_failed.otp_challenge", account_id=account_id, error=err[:300])
            return "fatal", err[:300], None
        if _is_proxy_err(err):
            _mark_proxy_failed(lazy_browser.last_used_proxy)
            logger.warning("login.proxy_navigation_failed", error=err[:140])
            return "proxy_err", err[:200], None
        if _is_retryable_login_err(err):
            _mark_proxy_failed(lazy_browser.last_used_proxy)
            logger.warning("login.region_or_challenge_block_swapping_proxy", error=err[:140])
            return "proxy_err", err[:200], None
        logger.error("login_failed", account_id=account_id, error=err[:200])
        return "fatal", err[:200], None
    finally:
        try:
            await lazy_browser.close()
        except Exception:
            pass


async def login_account(account: dict, captcha_config: dict | None, headless: bool) -> bool:
    """Reddit-style login flow with proxy retry.

    Same single LazyBrowser pattern as Reddit (browser_manager handles
    goodproxy selection via use_rotating_proxy=True). Mobile fingerprint/
    headers via BROWSER_DEVICE_CATEGORY=mobile in .env.

    Because goodproxies rotate randomly and ~95% are dead, this wraps the
    Reddit-equivalent login in a retry loop: on net::ERR_* / timeout, close
    the browser, reopen with a fresh goodproxy, try again. Up to
    LINKEDIN_LOGIN_PROXY_SWAP_MAX attempts (default 8).
    """
    account_id = account["account_id"]
    username = account["username"]
    password = account["password"]
    static_proxy = account["proxy_url"]

    use_rotating = static_proxy is None
    max_swaps = int(os.getenv("LINKEDIN_LOGIN_PROXY_SWAP_MAX", "3"))

    for swap in range(max_swaps):
        outcome, err, used_proxy = await _login_once(
            account_id, username, password, static_proxy, captcha_config, headless, use_rotating
        )
        if outcome == "success":
            pin = used_proxy or static_proxy
            if pin:
                from api.services.linkedin_login_runner import persist_login_proxy_into_session
                persist_login_proxy_into_session(account_id, pin)
            return True
        if outcome == "fatal":
            return False
        # proxy_err → next iteration relaunches with a fresh goodproxy
        logger.info("login.swapping_proxy", account_id=account_id, attempt=swap + 1, max=max_swaps)

    logger.error("login.all_proxy_swaps_exhausted", account_id=account_id)
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
