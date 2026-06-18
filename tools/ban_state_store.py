"""
tools/ban_state_store.py — Tiny JSON sidecar for persisting account risk ledgers.
Python 3.10 compatible.
"""

from __future__ import annotations
import os
import json
import tempfile
from pathlib import Path
import structlog
from tools.account_risk import RiskLedger

logger = structlog.get_logger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE_PATH = str(ROOT / "sessions" / "ban_state.json")

def get_store_path() -> Path:
    """Resolve the store file path from env or use default."""
    path_str = os.getenv("BAN_STATE_FILE", DEFAULT_FILE_PATH)
    return Path(path_str)

def load() -> dict[str, RiskLedger]:
    """Load the persisted risk ledgers from the JSON file.
    Degrades gracefully to an empty dict if file is missing, corrupt, or unreadable."""
    path = get_store_path()
    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        ledgers = {}
        if isinstance(data, dict):
            for aid, d in data.items():
                try:
                    ledgers[aid] = RiskLedger.from_dict(d)
                except Exception as e:
                    logger.warning("ban_state_store.load_entry_failed", account_id=aid, error=str(e))
        return ledgers
    except Exception as e:
        logger.warning("ban_state_store.load_failed_degrading_to_empty", path=str(path), error=str(e))
        return {}

def save(ledgers: dict[str, RiskLedger]) -> None:
    """Save the risk ledgers atomically to the JSON file.
    Wrapped in try/except to avoid crashing the caller on write failures."""
    path = get_store_path()
    try:
        # Ensure parent directory exists
        path.parent.mkdir(parents=True, exist_ok=True)
        
        # Serialize ledgers
        serialized = {aid: ledger.to_dict() for aid, ledger in ledgers.items()}
        
        # Atomic write: write to a temp file in the same directory and replace
        temp_dir = path.parent
        with tempfile.NamedTemporaryFile("w", dir=temp_dir, delete=False, suffix=".tmp", encoding="utf-8") as tf:
            json.dump(serialized, tf, indent=2)
            temp_path = tf.name
            
        try:
            os.replace(temp_path, path)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise
    except Exception as e:
        logger.error("ban_state_store.save_failed", path=str(path), error=str(e))
