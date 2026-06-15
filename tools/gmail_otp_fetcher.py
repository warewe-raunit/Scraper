"""
tools/gmail_otp_fetcher.py — Fetch LinkedIn email verification codes from
Gmail via OAuth-authenticated IMAP (XOAUTH2).

Per-account refresh tokens are stored in .env as:
    LINKEDIN_GMAIL_REFRESH_TOKEN_<account_id>=<token>

Issue the token once with:
    python tools/gmail_oauth_setup.py <account_id>

The fetcher:
  1. Refreshes the per-account access token from Google
  2. Connects to imap.gmail.com:993
  3. Authenticates via XOAUTH2 against that account's Gmail
  4. Polls UNSEEN messages from LinkedIn senders
  5. Extracts and returns the 6-digit OTP
"""

from __future__ import annotations

import asyncio
import base64
import email
import imaplib
import os
import re
import time
from email.header import decode_header
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


_LINKEDIN_FROM_PATTERNS = (
    "security-noreply@linkedin.com",
    "noreply@linkedin.com",
    "no-reply@linkedin.com",
    "@linkedin.com",
)

# LinkedIn's verification mails contain a six-digit PIN, typically on its own
# line near the top of the body and in the subject. Match the most specific
# patterns first so we don't accidentally pick up a 6-digit substring of a URL.
_OTP_PATTERNS = (
    re.compile(r"verification code is\s*[:\-]?\s*(\d{6})", re.IGNORECASE),
    re.compile(r"is your LinkedIn\s+verification code\s*[:\-]?\s*(\d{6})", re.IGNORECASE),
    re.compile(r"(?<!\d)(\d{6})(?!\d)\s+is your", re.IGNORECASE),
    re.compile(r"PIN[^\d]{0,10}(\d{6})", re.IGNORECASE),
    re.compile(r"(?<!\d)(\d{6})(?!\d)"),
)


def _decode_header(raw: Optional[str]) -> str:
    if not raw:
        return ""
    parts = []
    for chunk, enc in decode_header(raw):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(enc or "utf-8", errors="replace"))
            except Exception:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _extract_body(msg: email.message.Message) -> str:
    """Return the text content of an email, preferring plain text over HTML."""
    if not msg.is_multipart():
        try:
            payload = msg.get_payload(decode=True) or b""
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            return ""

    plain_parts = []
    html_parts = []
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype not in ("text/plain", "text/html"):
            continue
        if "attachment" in (part.get("Content-Disposition") or "").lower():
            continue
        try:
            data = part.get_payload(decode=True) or b""
            text = data.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain":
            plain_parts.append(text)
        else:
            html_parts.append(text)
    if plain_parts:
        return "\n".join(plain_parts)
    if html_parts:
        joined = "\n".join(html_parts)
        return re.sub(r"<[^>]+>", " ", joined)
    return ""


def _parse_otp(subject: str, body: str) -> Optional[str]:
    for source in (subject, body):
        if not source:
            continue
        for pat in _OTP_PATTERNS:
            m = pat.search(source)
            if m:
                code = m.group(1)
                if len(code) == 6 and code.isdigit():
                    return code
    return None


def _is_linkedin_sender(from_header: str) -> bool:
    low = (from_header or "").lower()
    return any(p in low for p in _LINKEDIN_FROM_PATTERNS)


def _refresh_access_token(refresh_token: str, client_id: str, client_secret: str) -> Optional[str]:
    """Trade a refresh token for a short-lived (1h) access token."""
    import requests
    try:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            logger.error("gmail_otp.token_refresh_failed", response=str(data)[:200])
        return token
    except Exception as e:
        logger.error("gmail_otp.token_refresh_exception", error=str(e)[:200])
        return None


def _xoauth2_string(user: str, access_token: str) -> bytes:
    """Build the raw XOAUTH2 SASL string Gmail expects.

    NOTE: imaplib.authenticate base64-encodes the bytes returned by the
    callback, so we return the *unencoded* SASL frame here. Double-encoding
    triggers 'BAD Invalid SASL argument'.
    """
    raw = f"user={user}\x01auth=Bearer {access_token}\x01\x01"
    return raw.encode("utf-8")


def _fetch_otp_blocking(
    user: str,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    *,
    poll_timeout: float,
    poll_interval: float,
    started_at: float,
) -> Optional[str]:
    """Blocking IMAP+OAuth poll. Returns the OTP string or None on timeout."""
    deadline = started_at + poll_timeout

    access_token = _refresh_access_token(refresh_token, client_id, client_secret)
    if not access_token:
        return None
    access_token_issued_at = time.time()

    while time.time() < deadline:
        # Refresh access token if older than 50 minutes (Google tokens last 1h).
        if time.time() - access_token_issued_at > 50 * 60:
            new_token = _refresh_access_token(refresh_token, client_id, client_secret)
            if new_token:
                access_token = new_token
                access_token_issued_at = time.time()

        try:
            with imaplib.IMAP4_SSL("imap.gmail.com", 993) as conn:
                xoauth2 = _xoauth2_string(user, access_token)
                try:
                    conn.authenticate("XOAUTH2", lambda _: xoauth2)
                except imaplib.IMAP4.error as auth_exc:
                    logger.error("gmail_otp.imap_auth_failed",
                                 user=user, error=str(auth_exc)[:200])
                    # Token might be revoked — bail; outer caller will warn.
                    return None

                conn.select("INBOX")
                since_epoch_minus_1 = int(started_at) - 60
                date_str = time.strftime("%d-%b-%Y", time.gmtime(since_epoch_minus_1))
                typ, data = conn.search(None, f'(UNSEEN SINCE {date_str})')
                if typ != "OK":
                    logger.warning("gmail_otp.search_failed", typ=typ)
                else:
                    ids = (data[0] or b"").split()
                    for msg_id in reversed(ids):
                        typ, msg_data = conn.fetch(msg_id, "(RFC822)")
                        if typ != "OK" or not msg_data:
                            continue
                        raw_msg = next(
                            (x[1] for x in msg_data if isinstance(x, tuple) and len(x) > 1),
                            None,
                        )
                        if not raw_msg:
                            continue
                        msg = email.message_from_bytes(raw_msg)
                        from_h = _decode_header(msg.get("From"))
                        if not _is_linkedin_sender(from_h):
                            continue
                        date_h = msg.get("Date")
                        msg_ts = email.utils.parsedate_to_datetime(date_h).timestamp() if date_h else 0
                        if msg_ts and msg_ts < started_at - 60:
                            continue
                        subject = _decode_header(msg.get("Subject"))
                        body = _extract_body(msg)
                        code = _parse_otp(subject, body)
                        if not code:
                            continue
                        logger.info("gmail_otp.found",
                                    from_h=from_h, subject=subject[:80], code=code)
                        try:
                            conn.store(msg_id, "+FLAGS", "\\Seen")
                        except Exception:
                            pass
                        return code
        except Exception as e:
            logger.warning("gmail_otp.poll_iteration_failed", error=str(e)[:200])
        time.sleep(poll_interval)
    return None


async def fetch_linkedin_otp(
    *,
    account_id: str,
    gmail_address: str,
    poll_timeout: float = 180.0,
    poll_interval: float = 4.0,
) -> Optional[str]:
    """Poll the account's Gmail (via OAuth-XOAUTH2 IMAP) for a fresh LinkedIn
    verification code received after this function was called.

    Returns the 6-digit code or None on timeout / misconfig.
    """
    client_id = os.getenv("GMAIL_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_OAUTH_CLIENT_SECRET", "").strip()
    refresh_token = os.getenv(f"LINKEDIN_GMAIL_REFRESH_TOKEN_{account_id}", "").strip()

    if not client_id or not client_secret:
        logger.warning("gmail_otp.no_oauth_client",
                       msg="Set GMAIL_OAUTH_CLIENT_ID / GMAIL_OAUTH_CLIENT_SECRET in .env")
        return None
    if not refresh_token:
        logger.warning(
            "gmail_otp.no_refresh_token",
            account_id=account_id,
            msg=f"Run: python tools/gmail_oauth_setup.py {account_id}",
        )
        return None

    started_at = time.time()
    logger.info("gmail_otp.poll_start",
                account_id=account_id, gmail=gmail_address, timeout_s=poll_timeout)

    loop = asyncio.get_running_loop()
    code = await loop.run_in_executor(
        None,
        lambda: _fetch_otp_blocking(
            gmail_address, refresh_token, client_id, client_secret,
            poll_timeout=poll_timeout, poll_interval=poll_interval, started_at=started_at,
        ),
    )
    if code is None:
        logger.warning("gmail_otp.timeout",
                       account_id=account_id, elapsed_s=round(time.time() - started_at, 1))
    return code
