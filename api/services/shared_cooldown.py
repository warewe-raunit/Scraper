"""api/services/shared_cooldown.py — cross-worker account cooldown sharing.

Under `uvicorn --workers N` each worker has its OWN in-memory AccountRegistry, so
a 429/403 cooldown that one worker sets is invisible to the others — they keep
sending traffic to an account this worker already knows is rate-limited, burning
attempts and deepening the rate-limit hole. This is the shared truth: workers
write cooldowns here and consult it during account selection, so N workers
coordinate instead of each independently re-discovering that acc_01 is throttled.

ponytail: a JSON file with atomic-replace writes + max-merge, not Redis. Single
box, low write rate (only on a block). Reads are cached ~0.5s so the hot
selection path isn't I/O-bound. No cross-process file lock: a concurrent
read-modify-write can rarely drop one update, but that just means an account is
re-cooled by the next worker that hits it — self-correcting, never corrupting
(os.replace is atomic). Add flock only if lost updates ever measurably matter.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict


class SharedCooldownStore:
    def __init__(self, path: Path, read_cache_ttl: float = 0.5) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(exist_ok=True)
        self._ttl = read_cache_ttl
        self._cache: Dict[str, float] = {}
        self._cache_at = 0.0
        self._guard = threading.Lock()  # in-process guard for cache + file RMW

    def _load(self) -> Dict[str, float]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return {k: float(v) for k, v in json.load(f).items()}
        except (FileNotFoundError, json.JSONDecodeError, ValueError, AttributeError):
            return {}

    def cool_down(self, account_id: str, until_ts: float) -> None:
        """Record that ``account_id`` is unavailable until ``until_ts`` (unix)."""
        now = time.time()
        with self._guard:
            data = self._load()
            # Keep the later of any existing/new cooldown; prune expired entries.
            data = {k: v for k, v in data.items() if v > now}
            if until_ts > data.get(account_id, 0.0):
                data[account_id] = until_ts
            # Best-effort write: concurrent os.replace from sibling workers can
            # transiently collide on Windows (PermissionError). A dropped write is
            # self-correcting (re-written on the next block), so never raise into
            # the request path — just keep the local cache fresh.
            try:
                tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self.path)  # atomic swap — readers never see a partial file
            except OSError:
                pass
            self._cache = data
            self._cache_at = time.monotonic()

    def remaining(self, account_id: str) -> float:
        """Seconds until ``account_id`` frees per the shared store (0 if free)."""
        mono = time.monotonic()
        with self._guard:
            if mono - self._cache_at > self._ttl:
                self._cache = self._load()
                self._cache_at = mono
            until = self._cache.get(account_id, 0.0)
        return max(0.0, until - time.time())


_store: SharedCooldownStore | None = None
_store_lock = threading.Lock()


def get_shared_cooldown_store() -> SharedCooldownStore:
    """Process-wide singleton over sessions/reddit_cooldowns.json (overridable)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                root = Path(__file__).resolve().parents[2]
                path = Path(os.getenv("REDDIT_COOLDOWN_FILE", str(root / "sessions" / "reddit_cooldowns.json")))
                _store = SharedCooldownStore(path)
    return _store


if __name__ == "__main__":
    # Self-check: a cooldown written is visible after the cache window, and a
    # second store (simulating another worker) reads it from the same file.
    import tempfile
    p = Path(tempfile.mkdtemp()) / "cd.json"
    a = SharedCooldownStore(p, read_cache_ttl=0.0)
    a.cool_down("acc_01", time.time() + 60)
    assert 58 < a.remaining("acc_01") <= 60, a.remaining("acc_01")
    assert a.remaining("acc_99") == 0.0
    b = SharedCooldownStore(p, read_cache_ttl=0.0)  # another "worker"
    assert 58 < b.remaining("acc_01") <= 60, "second worker must see shared cooldown"
    a.cool_down("acc_01", time.time() + 5)  # shorter — must NOT shrink the later one
    assert b.remaining("acc_01") > 50, "max-merge must keep the later cooldown"
    print("shared_cooldown self-check passed")
