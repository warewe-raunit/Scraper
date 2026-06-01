"""
api/dependencies.py — FastAPI dependencies for the Reddit Stealth API Scraper.
Extracts token_v2 and cookies from saved sessions, resolves sticky proxies,
and configures curl_cffi sessions with stealth headers.
"""

from __future__ import annotations

import os
import re
import sys
import json
from pathlib import Path
from typing import Optional, Dict, Any, List
import structlog
from curl_cffi import requests

# Fix path to import core/tools modules correctly
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.stealth.fingerprint import BrowserProfileManager
from browser_manager import _profile_http_headers

logger = structlog.get_logger(__name__)

# Load environment variables from workspace root .env
from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

# Directory where session JSON files are stored
SESSIONS_DIR = ROOT / "sessions"

# Round-robin counter for rotating account sessions
_rotation_index = 0

def parse_accounts_from_env() -> List[Dict[str, Any]]:
    """Parse all account entries starting with REDDIT_ACCOUNT_ from .env."""
    accounts = []
    pattern = re.compile(r"^REDDIT_ACCOUNT_\d+$")
    
    for key, value in os.environ.items():
        if pattern.match(key):
            try:
                parts = value.split("|")
                if len(parts) != 4:
                    continue
                account_id, username, password, proxy_url = parts
                
                # Skip placeholders
                if "your_username" in username or "your_password" in password:
                    continue
                    
                accounts.append({
                    "account_id": account_id.strip(),
                    "username": username.strip(),
                    "password": password.strip(),
                    "proxy_url": proxy_url.strip() if proxy_url.strip() else None
                })
            except Exception as e:
                logger.error("error_parsing_account_env", key=key, error=str(e))
                
    accounts.sort(key=lambda x: x["account_id"])
    return accounts

def get_available_session_accounts() -> List[str]:
    """Find all account IDs that have an existing saved session file."""
    if not SESSIONS_DIR.exists():
        return []
    
    account_ids = []
    # Match any JSON file in the sessions directory, e.g. acc_01__mobile.json or acc_01.json
    for path in SESSIONS_DIR.glob("*.json"):
        # Extract account_id part (everything before the optional __mobile/device suffix)
        name = path.stem
        if "__" in name:
            account_id = name.split("__")[0]
        else:
            account_id = name
        if account_id not in account_ids:
            account_ids.append(account_id)
            
    account_ids.sort()
    return account_ids

def find_session_file(account_id: str) -> Optional[Path]:
    """Find the specific session JSON file for a given account ID."""
    if not SESSIONS_DIR.exists():
        return None
        
    # Check exact match first
    exact_path = SESSIONS_DIR / f"{account_id}.json"
    if exact_path.exists():
        return exact_path
        
    # Search for pattern match (e.g., acc_01__mobile.json)
    for path in SESSIONS_DIR.glob(f"{account_id}*.json"):
        return path
        
    return None

def get_session_cookies_and_token(session_file: Path) -> tuple[str, str]:
    """Extract cookie string and token_v2 from a saved Playwright session JSON file."""
    cookie_string = ""
    token_v2 = ""
    
    try:
        with open(session_file, "r", encoding="utf-8") as f:
            state = json.load(f)
            
        essential_cookie_names = [
            "loid", "session_tracker", "reddit_session", 
            "token_v2", "csrf_token", "g_state", 
            "edgebucket", "eu_cookie"
        ]
        cookie_parts = []
        for cookie in state.get("cookies", []):
            if cookie.get("name") == "token_v2":
                token_v2 = cookie.get("value", "")
            if cookie.get("name") in essential_cookie_names:
                cookie_parts.append(f"{cookie['name']}={cookie['value']}")
                
        cookie_string = "; ".join(cookie_parts)
    except Exception as e:
        logger.error("session_extraction_failed", path=str(session_file), error=str(e))
        
    return cookie_string, token_v2

def create_stealth_client(account_id: Optional[str] = None) -> requests.Session:
    """
    Build a requests.Session configured with:
      1. Extraction of token_v2 authentication.
      2. Generated hardware client-hints aligned with account profile.
      3. Sticky proxy configuration.
    """
    global _rotation_index
    
    # 1. Resolve Account Selection
    available_accounts = get_available_session_accounts()
    if not available_accounts:
        raise RuntimeError("No saved Reddit sessions found in the sessions directory.")
        
    if account_id:
        if account_id not in available_accounts:
            # Try to match fuzzy
            matched = [a for a in available_accounts if account_id.lower() in a.lower()]
            if matched:
                selected_account = matched[0]
            else:
                raise ValueError(f"Account '{account_id}' has no active saved session.")
        else:
            selected_account = account_id
    else:
        # Rotate through available sessions
        selected_account = available_accounts[_rotation_index % len(available_accounts)]
        _rotation_index += 1
        
    # 2. Find session file & extract cookies
    session_file = find_session_file(selected_account)
    if not session_file:
        raise FileNotFoundError(f"Session file not found for account {selected_account}")
        
    cookie_string, token_v2 = get_session_cookies_and_token(session_file)
    if not token_v2:
        raise ValueError(f"Could not find token_v2 in cookies for account {selected_account}")
        
    # 3. Resolve Proxy (Sticky Proxy)
    # Parse accounts from .env to find proxy settings for this account
    accounts_info = parse_accounts_from_env()
    proxy_url = None
    for acc in accounts_info:
        if acc["account_id"] == selected_account:
            proxy_url = acc.get("proxy_url")
            break
            
    # Clean proxy output for log security
    display_proxy = "Direct"
    if proxy_url:
        if "@" in proxy_url:
            display_proxy = f"***:***@{proxy_url.split('@')[-1]}"
        else:
            display_proxy = proxy_url

    # 4. Generate Stealth Profile & Headers
    profile_mgr = BrowserProfileManager()
    profile = profile_mgr.generate(selected_account)
    dynamic_headers = _profile_http_headers(profile)
    
    headers = {
        "authorization": f"Bearer {token_v2}",
        "accept": "*/*",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.reddit.com",
        "referer": "https://www.reddit.com/",
        "user-agent": profile["user_agent"],
        "cookie": cookie_string
    }
    headers.update(dynamic_headers)
    
    # 5. Build curl_cffi session
    session = requests.Session()
    session.headers.update(headers)
    
    if proxy_url:
        session.proxies = {
            "http": proxy_url,
            "https": proxy_url
        }
        
    # Attach helper attributes to session for diagnostics/logging
    session.account_id = selected_account
    session.proxy_display = display_proxy
    
    logger.info(
        "created_stealth_client",
        account_id=selected_account,
        proxy=display_proxy,
        has_token=bool(token_v2)
    )
    
    return session

# Initialize Global AccountRegistry Singleton
from api.services.registry import AccountRegistry
_global_registry = None

def get_registry() -> AccountRegistry:
    """Dependency provider for global AccountRegistry singleton."""
    global _global_registry
    if _global_registry is None:
        _global_registry = AccountRegistry()
    return _global_registry

def get_scraper_service() -> RedditScraperService:
    """Dependency provider for RedditScraperService with registered accounts failover."""
    from api.services.scraper import RedditScraperService
    registry = get_registry()
    return RedditScraperService(registry)


_global_youtube_service = None

def get_youtube_scraper_service() -> YouTubeScraperService:
    """Dependency provider for YouTubeScraperService singleton."""
    global _global_youtube_service
    if _global_youtube_service is None:
        from api.services.youtube import YouTubeScraperService
        _global_youtube_service = YouTubeScraperService()
    return _global_youtube_service


from fastapi import Security, HTTPException, status
from fastapi.security import APIKeyHeader, APIKeyQuery

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
api_key_query = APIKeyQuery(name="api_key", auto_error=False)

async def verify_api_key(
    header_key: Optional[str] = Security(api_key_header),
    query_key: Optional[str] = Security(api_key_query)
) -> str:
    """
    Validate the incoming request against the configured API_KEY in environment variables.
    Supports authorization via X-API-Key header or api_key query parameter.
    """
    configured_key = os.getenv("API_KEY", "stealth_secret_key_123")
    
    # If the key is empty, authentication is disabled/bypassed
    if not configured_key:
        return ""
        
    if header_key == configured_key:
        return header_key
    if query_key == configured_key:
        return query_key
        
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API Key. Please provide it in the 'X-API-Key' header or 'api_key' query parameter."
    )


