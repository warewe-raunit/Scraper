"""api/worker_leader.py — elect exactly one uvicorn worker to run the
background warmup / relogin / token-warm loops.

Under ``uvicorn --workers N`` every worker process runs the lifespan startup, so
each would independently launch the proxy-health, LinkedIn/Reddit relogin, and
X-token-warm loops. That is N× the login attempts against the same accounts —
real ban risk (see BAN_* tuning) plus wasted CPU and duplicate headful Chromium.

Election uses an exclusive, non-blocking file lock: the first worker to grab it
becomes the leader and runs the loops; the rest skip them and just serve
requests. The lock is held for the process lifetime, so if the leader crashes
and uvicorn respawns it, the OS frees the lock and the new process re-wins it.

# ponytail: flock is the right primitive for "one of N forked processes" — no
# Redis, no leases, no heartbeat. On Windows (local dev = single worker) fcntl is
# absent, so we are always the leader, which is the correct answer there.
"""

from __future__ import annotations

from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_is_leader: bool | None = None
_lock_handle = None  # held open for the process lifetime to retain the lock


def is_warmup_leader() -> bool:
    """True if THIS worker should run the background loops. Decided once, cached."""
    global _is_leader, _lock_handle
    if _is_leader is not None:
        return _is_leader

    try:
        import fcntl  # POSIX only; the deploy target is Linux (systemd + xvfb)
    except ImportError:
        _is_leader = True  # Windows dev: single process, always leader
        return _is_leader

    lock_path = Path(__file__).resolve().parents[1] / "logs" / ".warmup_leader.lock"
    lock_path.parent.mkdir(exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_handle = fh  # keep open → keep the lock
        _is_leader = True
    except OSError:
        fh.close()
        _is_leader = False

    logger.info("warmup_leader_elected", leader=_is_leader)
    return _is_leader


if __name__ == "__main__":
    # Self-check: within ONE process the first call wins and the result is stable.
    assert is_warmup_leader() is True, "single process must be its own leader"
    assert is_warmup_leader() is True, "result must be cached/stable"
    print("worker_leader self-check passed")
