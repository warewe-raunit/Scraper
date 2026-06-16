"""
api/services/linkedin_env.py — single source of truth for parsing
LINKEDIN_ACCOUNT_<N> entries from the environment.

Deliberately lightweight (no Playwright / browser_manager imports) so the
account pool, the scraper service, and the CLI login runner can all share one
parser without pulling heavy dependencies into modules that don't need them.

Format: account_id|username|password[|proxy_url]
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

import structlog

logger = structlog.get_logger(__name__)

_ACCOUNT_KEY = re.compile(r"^LINKEDIN_ACCOUNT_\d+$")


def parse_linkedin_accounts_env() -> List[Dict[str, Any]]:
    """Return every configured LinkedIn account as a dict, sorted by account_id.

    Skips malformed rows and placeholder credentials. Each dict has keys:
    account_id, username, password, proxy_url (proxy_url is None when omitted).
    """
    accounts: List[Dict[str, Any]] = []
    for key, value in os.environ.items():
        if not _ACCOUNT_KEY.match(key):
            continue
        try:
            parts = value.split("|")
            if len(parts) < 3 or len(parts) > 4:
                logger.warning(
                    "linkedin_account.invalid_format",
                    key=key,
                    expected="account_id|username|password[|proxy_url]",
                )
                continue

            account_id = parts[0].strip()
            username = parts[1].strip()
            password = parts[2].strip()
            proxy_url = parts[3].strip() if len(parts) == 4 and parts[3].strip() else None

            if "your_username" in username or "your_password" in password:
                logger.warning("linkedin_account.placeholder_skipped", account_id=account_id)
                continue

            accounts.append({
                "account_id": account_id,
                "username": username,
                "password": password,
                "proxy_url": proxy_url,
            })
        except Exception as e:
            logger.error("linkedin_account.parse_failed", key=key, error=str(e))

    accounts.sort(key=lambda a: a["account_id"])
    return accounts
