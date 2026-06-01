"""
session_store.py — Save and restore Playwright browser sessions per account.
Sessions are stored as JSON files (cookies + localStorage) in ./sessions/
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SESSIONS_DIR = Path(__file__).parent / "sessions"


def _session_path(account_id: str) -> Path:
    SESSIONS_DIR.mkdir(exist_ok=True)
    return SESSIONS_DIR / f"{account_id}.json"


def session_exists(account_id: str) -> bool:
    return _session_path(account_id).exists()


def save_session(account_id: str, storage_state: dict) -> None:
    path = _session_path(account_id)
    with open(path, "w") as f:
        json.dump(storage_state, f)


def load_session(account_id: str) -> dict | None:
    path = _session_path(account_id)
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def delete_session(account_id: str) -> None:
    path = _session_path(account_id)
    if path.exists():
        path.unlink()
