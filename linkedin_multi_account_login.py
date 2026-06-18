import argparse
import asyncio
import os
import random
import sys
from dotenv import load_dotenv
import structlog

# Add current directory to path if not present
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.services.linkedin_env import parse_linkedin_accounts_env
from api.services.linkedin_login_runner import login_account_with_retries

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
    """Parse all LINKEDIN_ACCOUNT_<N> entries from .env (shared parser)."""
    return parse_linkedin_accounts_env()


async def login_account(account: dict, captcha_config: dict | None, headless: bool) -> bool:
    """Run one account's login via the shared runner.

    The full LazyBrowser launch, proxy-swap retry, OTP fast path, and
    login-proxy pinning all live in linkedin_login_runner.login_account_with_retries
    — this CLI just unpacks the account dict and delegates so the two paths
    can't drift.
    """
    success, reason = await login_account_with_retries(
        account_id=account["account_id"],
        username=account["username"],
        password=account["password"],
        static_proxy=account["proxy_url"],
        captcha_config=captcha_config,
        headless=headless,
    )
    return success

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
