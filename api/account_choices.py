"""
api/account_choices.py — dynamic `account_id` dropdown enums.

Every Reddit/LinkedIn endpoint takes an optional `account_id`. The valid values
are a finite, known set (the accounts configured in .env), so we render them as a
Swagger dropdown instead of a free-text box — same UX as the sort/time/format
dropdowns. The enums are built once at import (env config is stable for the
process lifetime; new accounts need an .env change + restart anyway). When no
accounts are configured we fall back to plain `str` so the param still works.

StrEnum members ARE their string value, so they pass through the service layer
(dict lookups, URL building, logging) transparently — no normalization needed.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import List, Optional, Type

import structlog

logger = structlog.get_logger(__name__)


def _safe_members(ids: List[str]) -> dict:
    """Map account ids → enum member names (valid identifiers), values = real id."""
    out: dict = {}
    used: set = set()
    for raw in ids:
        if not raw:
            continue
        name = re.sub(r"\W", "_", raw)
        if not name or name[0].isdigit():
            name = "_" + name
        base, n = name, 1
        while name in used:
            n += 1
            name = f"{base}_{n}"
        used.add(name)
        out[name] = raw  # value shown in the dropdown is the real account id
    return out


def _build(enum_name: str, ids: List[str]) -> Optional[Type[StrEnum]]:
    members = _safe_members(ids)
    if not members:
        return None
    return StrEnum(enum_name, members)


def _reddit_ids() -> List[str]:
    try:
        from api.dependencies import parse_accounts_from_env
        return [a["account_id"] for a in parse_accounts_from_env()]
    except Exception as e:
        logger.warning("account_choices.reddit_ids_failed", error=str(e)[:120])
        return []


def _linkedin_ids() -> List[str]:
    try:
        from api.services.linkedin_env import parse_linkedin_accounts_env
        return [a["account_id"] for a in parse_linkedin_accounts_env()]
    except Exception as e:
        logger.warning("account_choices.linkedin_ids_failed", error=str(e)[:120])
        return []


_RedditAccountEnum = _build("RedditAccountId", _reddit_ids())
_LinkedInAccountEnum = _build("LinkedInAccountId", _linkedin_ids())

# Public annotation aliases: a dropdown enum when accounts are configured, else a
# plain `str` text box (so the endpoints keep working with zero accounts).
RedditAccount = _RedditAccountEnum if _RedditAccountEnum is not None else str
LinkedInAccount = _LinkedInAccountEnum if _LinkedInAccountEnum is not None else str
