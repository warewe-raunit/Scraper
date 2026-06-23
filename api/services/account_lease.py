"""api/services/account_lease.py — cross-worker EXCLUSIVE account leasing.

Under `uvicorn --workers N` each worker is a separate OS process, so an
in-process "in_use" flag can't stop two workers from using the same account at
the same time. That's a real ban risk: two concurrent requests on one Reddit/
LinkedIn account (even from different proxy IPs) is exactly the pattern that
gets the account rate-restricted. This is the cross-process guarantee: a worker
must win an exclusive OS file lock on an account before using it, and releases
it the moment the request finishes — so at most ONE worker holds a given
account at any instant, fleet-wide.

Why flock (not a JSON lease file with timestamps): the lock is tied to the open
file description, so if a worker crashes mid-request the OS releases the lock
automatically — a dead worker can never wedge an account, no stale-lease TTL to
tune. Within a single worker, the `_held` set excludes a second coroutine from
double-leasing the same account (flock between two fds of the SAME process is
not guaranteed to block, so we guard intra-process in memory).

On Windows (local dev = single worker) fcntl is absent, so leasing degrades to a
process-local set — correct there, because there are no sibling processes to
exclude.

# ponytail: flock + an in-memory set is the whole mechanism — no Redis, no
# heartbeat, no lease table. The OS already does crash recovery for us.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, Optional, Set

import structlog

try:
    import fcntl  # POSIX only; deploy target is Linux
except ImportError:  # pragma: no cover - Windows dev path
    fcntl = None  # type: ignore

logger = structlog.get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]


class AccountLeaseStore:
    """Exclusive, cross-process leases over a set of account ids."""

    def __init__(self, lock_dir: Path) -> None:
        self.lock_dir = Path(lock_dir)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._fds: Dict[str, object] = {}   # account_id -> open file handle (POSIX)
        self._local: Set[str] = set()       # account ids held (Windows fallback / intra-proc)
        self._guard = threading.Lock()

    def try_acquire(self, account_id: str) -> bool:
        """Take an exclusive lease on ``account_id`` across all workers.

        Returns False (without blocking) if any worker — including this one —
        already holds it.
        """
        with self._guard:
            if account_id in self._fds or account_id in self._local:
                return False  # already leased by a coroutine in THIS worker
            if fcntl is None:
                self._local.add(account_id)  # single-process dev: in-memory is enough
                return True
            fh = open(self.lock_dir / f"{account_id}.lock", "w")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False  # held by another worker process
            self._fds[account_id] = fh
            return True

    def release(self, account_id: str) -> None:
        """Release a lease this worker holds (no-op if it doesn't)."""
        with self._guard:
            self._local.discard(account_id)
            fh = self._fds.pop(account_id, None)
        if fh is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                fh.close()

    def held(self) -> list:
        """Account ids currently leased by THIS worker (for diagnostics)."""
        with self._guard:
            return list(self._fds.keys()) + list(self._local)


_stores: Dict[str, AccountLeaseStore] = {}
_stores_lock = threading.Lock()


def get_account_lease_store(platform: str) -> AccountLeaseStore:
    """Process-wide lease store for a platform (reddit/linkedin), one lock dir
    each under sessions/account_leases/<platform>/."""
    with _stores_lock:
        store = _stores.get(platform)
        if store is None:
            store = AccountLeaseStore(ROOT / "sessions" / "account_leases" / platform)
            _stores[platform] = store
        return store


if __name__ == "__main__":
    # Self-check: one worker's store leases exclusively; a second store over the
    # SAME dir (simulating a sibling worker, POSIX only) is blocked until release.
    import tempfile

    d = Path(tempfile.mkdtemp())
    a = AccountLeaseStore(d)
    assert a.try_acquire("acc_01") is True, "free account leases"
    assert a.try_acquire("acc_01") is False, "same worker can't double-lease"
    assert a.try_acquire("acc_02") is True, "a different account is independent"

    if fcntl is not None:
        b = AccountLeaseStore(d)  # another "worker"
        assert b.try_acquire("acc_01") is False, "sibling worker is excluded while held"
        a.release("acc_01")
        assert b.try_acquire("acc_01") is True, "freed account is grabbable by the sibling"
        b.release("acc_01")
        print("account_lease self-check passed (cross-process)")
    else:
        a.release("acc_01")
        assert a.try_acquire("acc_01") is True, "freed account re-leases"
        print("account_lease self-check passed (single-process fallback)")
