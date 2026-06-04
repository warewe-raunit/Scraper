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
from tools.login_tool import run_tool as login
from tools.reddit_login_state import reddit_login_state

def _configure_cli_logging() -> None:
    """Configure standalone structlog rendering for CLI use ONLY.

    Do NOT call this at import time. This module is imported as a library by the
    API (api/services/registry.py imports login_account); calling
    structlog.configure() on import clobbers the central Loguru pipeline set up by
    tools.logging_config.configure_logging(), which silently changes the console
    format and detaches the rotating logs/app.log file sink. Only configure when
    this file is run directly as a script.
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


logger = structlog.get_logger("multi_account_login")

def parse_accounts_from_env() -> list[dict]:
    """Parse all account entries starting with REDDIT_ACCOUNT_ from .env."""
    accounts = []
    # Find all keys matching REDDIT_ACCOUNT_<N>
    pattern = re.compile(r"^REDDIT_ACCOUNT_\d+$")
    
    for key, value in os.environ.items():
        if pattern.match(key):
            try:
                parts = value.split("|")
                if len(parts) != 4:
                    logger.warning(
                        "invalid_account_format",
                        key=key,
                        value=value,
                        expected="account_id|username|password|proxy_url"
                    )
                    continue
                
                account_id, username, password, proxy_url = parts
                
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
                    "proxy_url": proxy_url.strip() if proxy_url.strip() else None
                })
            except Exception as e:
                logger.error("error_parsing_account", key=key, error=str(e))
                
    # Sort accounts by account_id to run them in a predictable order
    accounts.sort(key=lambda x: x["account_id"])
    return accounts

async def login_account(account: dict, captcha_config: dict | None, headless: bool) -> bool:
    account_id = account["account_id"]
    username = account["username"]
    password = account["password"]
    proxy_url = account["proxy_url"]
    
    # Clean proxy url representation for logging
    display_proxy = "Direct"
    if proxy_url:
        if "@" in proxy_url:
            parts = proxy_url.split("@")
            display_proxy = f"***:***@{parts[-1]}"
        else:
            display_proxy = proxy_url

    logger.info(
        "starting_account_login",
        account_id=account_id,
        username=username,
        proxy=display_proxy,
        headless=headless
    )
    
    # Instantiate browser context with the sticky proxy
    lazy_browser = LazyBrowser(account_id=account_id, proxy_url=proxy_url, headless=headless)
    
    try:
        page = await lazy_browser.get_page()
        
        # Step 1: Check if already logged in via saved session cookies
        logger.info("checking_existing_session", account_id=account_id)
        state = await reddit_login_state(page, expected_username=username, navigate=True)
        
        if state.get("logged_in"):
            logger.info(
                "account_already_logged_in",
                account_id=account_id,
                username=username,
                reason=state.get("reason")
            )
            return True
        
        # Step 2: Run the automated login flow
        logger.info("session_missing_or_expired_starting_login_flow", account_id=account_id)
        result = await login(
            page=page,
            account_id=account_id,
            username=username,
            password=password,
            captcha_config=captcha_config
        )
        
        success = result.get("success", False)
        if success:
            logger.info("login_completed_successfully", account_id=account_id, username=username)
        else:
            logger.error("login_failed", account_id=account_id, error=result.get("error"))
            
        return success
        
    except Exception as e:
        logger.error("unexpected_error_during_login", account_id=account_id, error=str(e))
        return False
    finally:
        # Close browser, which automatically saves/persists session state
        await lazy_browser.close()
        logger.info("browser_closed", account_id=account_id)

async def main():
    load_dotenv(override=True)
    
    # Parse CLI arguments to allow targeting a specific account
    parser = argparse.ArgumentParser(description="Multi-account Reddit login flow.")
    parser.add_argument(
        "target_account", 
        nargs="?", 
        help="Optional specific account_id to run (e.g. acc_02). If not provided, all configured accounts will run."
    )
    args = parser.parse_args()
    
    accounts = parse_accounts_from_env()
    if not accounts:
        logger.error("no_valid_accounts_configured", msg="Please configure REDDIT_ACCOUNT_N variables in your .env file with real credentials.")
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
    
    # Captcha settings
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
            
    print("\n=== LOGIN RUN SUMMARY ===")
    for acc_id, status in summary.items():
        print(f"Account: {acc_id} -> {status}")
    print("=========================\n")

if __name__ == "__main__":
    _configure_cli_logging()
    asyncio.run(main())
