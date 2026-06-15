"""
tools/gmail_oauth_setup.py — one-time per-account Gmail OAuth consent helper.

Run:
    python tools/gmail_oauth_setup.py acc_li_02

The script:
  1. Reads GMAIL_OAUTH_CLIENT_ID / GMAIL_OAUTH_CLIENT_SECRET from .env
     (one-time global Google Cloud setup).
  2. Looks up the LinkedIn account's Gmail address from LINKEDIN_ACCOUNT_<N>.
  3. Opens the Google consent screen in your default browser.
  4. Spawns a local HTTP server on http://localhost:8765/ that captures the
     redirected `code` parameter.
  5. Exchanges the code for a long-lived refresh token.
  6. Appends `LINKEDIN_GMAIL_REFRESH_TOKEN_<account_id>=<token>` to .env.

Refresh tokens never expire unless you revoke them or the user changes their
Google password. So this is genuinely a one-time-per-account ~30-second flow.

Google Cloud Project setup (one time total, before running this script):

  1. https://console.cloud.google.com/ — create a project.
  2. APIs & Services → Library → search "Gmail API" → Enable.
  3. APIs & Services → OAuth consent screen
       - User Type: External
       - App name + your email
       - Scopes: add `https://mail.google.com/`
       - Test users: add every Gmail address you plan to use
         (you can stay in Testing mode forever for personal use — no
         verification needed)
  4. APIs & Services → Credentials → Create Credentials → OAuth client ID
       - Application type: Desktop app
       - Download the JSON (or copy client_id + client_secret).
  5. Paste into .env:
       GMAIL_OAUTH_CLIENT_ID=...
       GMAIL_OAUTH_CLIENT_SECRET=...
"""

from __future__ import annotations

import argparse
import http.server
import os
import re
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

SCOPE = "https://mail.google.com/"
REDIRECT_PORT = int(os.getenv("GMAIL_OAUTH_REDIRECT_PORT", "8765"))
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/"
ENV_PATH = ROOT / ".env"


def _read_account_email(account_id: str) -> Optional[str]:
    """Find LINKEDIN_ACCOUNT_N for the given account_id and return its username (email)."""
    pattern = re.compile(r"^LINKEDIN_ACCOUNT_\d+$")
    for key, value in os.environ.items():
        if not pattern.match(key):
            continue
        parts = value.split("|")
        if len(parts) < 3:
            continue
        if parts[0].strip() == account_id:
            return parts[1].strip()
    return None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the `code` query parameter from Google's redirect."""

    captured_code: Optional[str] = None
    captured_error: Optional[str] = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            _CallbackHandler.captured_code = params["code"][0]
            body = b"<h1>Authorized.</h1><p>You can close this tab.</p>"
        elif "error" in params:
            _CallbackHandler.captured_error = params["error"][0]
            body = f"<h1>OAuth error: {params['error'][0]}</h1>".encode()
        else:
            body = b"<h1>No code in callback. Close and retry.</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Silence the default stderr access log.
        return


def _run_callback_server() -> str:
    """Run a local HTTP server until Google's redirect lands. Returns the code."""
    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while _CallbackHandler.captured_code is None and _CallbackHandler.captured_error is None:
            thread.join(timeout=0.5)
    finally:
        server.shutdown()
        server.server_close()
    if _CallbackHandler.captured_error:
        raise RuntimeError(f"OAuth error: {_CallbackHandler.captured_error}")
    if not _CallbackHandler.captured_code:
        raise RuntimeError("No authorization code received.")
    return _CallbackHandler.captured_code


def _exchange_code_for_refresh_token(code: str, client_id: str, client_secret: str) -> str:
    """Trade the one-shot code for a long-lived refresh token."""
    import requests
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    data = resp.json()
    if "refresh_token" not in data:
        raise RuntimeError(
            f"Token exchange failed: {data}. "
            "If you previously authorized this app for this Gmail, revoke access at "
            "https://myaccount.google.com/permissions and re-run."
        )
    return data["refresh_token"]


def _append_refresh_token_to_env(account_id: str, refresh_token: str) -> None:
    """Write or replace LINKEDIN_GMAIL_REFRESH_TOKEN_<account_id> in .env."""
    key = f"LINKEDIN_GMAIL_REFRESH_TOKEN_{account_id}"
    line = f"{key}={refresh_token}\n"

    existing = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    lines = existing.splitlines(keepends=True)

    replaced = False
    for i, raw in enumerate(lines):
        if raw.startswith(key + "="):
            lines[i] = line
            replaced = True
            break

    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(line)

    ENV_PATH.write_text("".join(lines), encoding="utf-8")


def main():
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(
        description="One-time per-account Gmail OAuth consent helper for LinkedIn OTP fetching."
    )
    parser.add_argument("account_id", help="LinkedIn account id, e.g. acc_li_02")
    args = parser.parse_args()

    client_id = os.getenv("GMAIL_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("ERROR: GMAIL_OAUTH_CLIENT_ID / GMAIL_OAUTH_CLIENT_SECRET not set in .env.")
        print("See the docstring at the top of tools/gmail_oauth_setup.py for Cloud setup steps.")
        sys.exit(2)

    email = _read_account_email(args.account_id)
    if not email:
        print(f"ERROR: no LINKEDIN_ACCOUNT_<N> entry found with account_id={args.account_id!r}")
        sys.exit(2)

    print(f"Account:  {args.account_id}")
    print(f"Gmail:    {email}")
    print(f"Scope:    {SCOPE}")
    print()
    print("Opening Google consent screen in your default browser...")
    print("If a different Google account is signed in, switch to:", email)
    print()

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",       # forces refresh_token issuance
            "login_hint": email,
        })
    )

    try:
        webbrowser.open(auth_url, new=1, autoraise=True)
    except Exception:
        pass

    print(f"Listening on {REDIRECT_URI} for the redirect...")
    code = _run_callback_server()
    print("Received authorization code. Exchanging for refresh token...")

    refresh_token = _exchange_code_for_refresh_token(code, client_id, client_secret)
    _append_refresh_token_to_env(args.account_id, refresh_token)

    print()
    print(f"OK — refresh token saved to .env as LINKEDIN_GMAIL_REFRESH_TOKEN_{args.account_id}")
    print("This token is long-lived. Run the LinkedIn login as usual.")


if __name__ == "__main__":
    main()
