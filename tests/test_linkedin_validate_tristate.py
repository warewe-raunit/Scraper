"""
tests/test_linkedin_validate_tristate.py — regression test for "all accounts get
relogined" bug.

A health probe that fails because no proxy reached LinkedIn (exhausted) is
INCONCLUSIVE and must NOT mark the account dead / trigger a relogin. Only a real
302->login redirect (dead li_at) or a missing session file counts as "dead".
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("GOODPROXIES_ENABLED", "false")

from api.services.linkedin import LinkedInScraperService  # noqa: E402


def _classify(signal, *, has_session=True):
    """Run _classify_account_health with a stubbed voyager probe returning
    `signal` = (result, redirected, exhausted)."""
    svc = LinkedInScraperService()
    svc._resolve_session_file = lambda aid: ("session.json" if has_session else None)

    async def _stub(path, aid):
        return signal

    svc._voyager_get_for_account_with_signal = _stub
    return asyncio.run(svc._classify_account_health("acc_li_01"))


def test_healthy_when_data_returned():
    assert _classify(({"data": 1}, False, False)) == "healthy"


def test_dead_on_redirect():
    # 302 -> login: the li_at is genuinely dead.
    assert _classify((None, True, False)) == "dead"


def test_dead_when_no_session_file():
    assert _classify(({"data": 1}, False, False), has_session=False) == "dead"


def test_inconclusive_on_proxy_exhaustion():
    # No proxy reached LinkedIn -> proves nothing about the session.
    assert _classify((None, False, True)) == "inconclusive"


def test_inconclusive_on_probe_exception():
    svc = LinkedInScraperService()
    svc._resolve_session_file = lambda aid: "session.json"

    async def _boom(path, aid):
        raise RuntimeError("network down")

    svc._voyager_get_for_account_with_signal = _boom
    assert asyncio.run(svc._classify_account_health("acc_li_01")) == "inconclusive"


def test_inconclusive_verdict_does_not_report_health():
    """validate_all_accounts must NOT report (relogin) an inconclusive account."""
    from api.services import linkedin_account_pool as pool_mod

    reported = []

    class _FakePool:
        def account_ids(self):
            return ["acc_li_01", "acc_li_02"]

        async def report_account_health(self, aid, healthy):
            reported.append((aid, healthy))

    fake = _FakePool()

    async def _instance():
        return fake

    pool_mod.LinkedInAccountPool.instance = classmethod(lambda cls: _instance())  # type: ignore

    svc = LinkedInScraperService()

    async def _classify(aid):
        # acc_li_01 healthy, acc_li_02 inconclusive
        return "healthy" if aid == "acc_li_01" else "inconclusive"

    svc._classify_account_health = _classify

    verdict = asyncio.run(svc.validate_all_accounts())
    assert verdict == {"acc_li_01": "healthy", "acc_li_02": "inconclusive"}, verdict
    # Only the healthy account was reported; the inconclusive one was left alone.
    assert reported == [("acc_li_01", True)], reported


def test_pool_acquire_multi_same_region(monkeypatch):
    from api.services.linkedin_account_pool import LinkedInAccountPool, AccountState, ALIVE, DYING, DEAD

    # This test manually releases via in_use=False (not pool.release()), so isolate
    # it from exclusive leasing and the rate cap — it checks region grouping only.
    monkeypatch.setenv("LINKEDIN_EXCLUSIVE_ACCOUNTS", "false")
    monkeypatch.setenv("LINKEDIN_RATE_LIMIT_ENABLED", "false")

    # Create a clean pool instance and manually populate _accounts to avoid loading from env/sessions
    pool = LinkedInAccountPool()
    pool._accounts = {
        "acc1": AccountState(account_id="acc1", username="u1", password="p1", static_proxy=None, status=ALIVE, login_country="US"),
        "acc2": AccountState(account_id="acc2", username="u2", password="p2", static_proxy=None, status=DYING, login_country="US"),
        "acc3": AccountState(account_id="acc3", username="u3", password="p3", static_proxy=None, status=ALIVE, login_country="RU"),
        "acc4": AccountState(account_id="acc4", username="u4", password="p4", static_proxy=None, status=ALIVE, login_country="US"),
        "acc5": AccountState(account_id="acc5", username="u5", password="p5", static_proxy=None, status=DEAD, login_country="RU"),
    }

    # US group has: acc1 (ALIVE), acc2 (DYING), acc4 (ALIVE)
    # RU group has: acc3 (ALIVE)
    # DEAD accounts like acc5 are excluded
    # Max free healthy group is US (size 3)

    # Acquire 2 accounts: should pick from the US group (the largest)
    # and should prioritize ALIVE (acc1, acc4) over DYING (acc2)
    acquired = asyncio.run(pool.acquire_multi(count=2, acquire_wait_seconds=0.1))

    assert len(acquired) == 2
    acquired_ids = {a.account_id for a in acquired}
    assert acquired_ids == {"acc1", "acc4"}
    assert all(a.in_use for a in acquired)

    # Release them back
    for a in acquired:
        a.in_use = False

    # If we acquire 3 accounts, we should get acc1, acc4, and acc2 (in that order of priority)
    acquired3 = asyncio.run(pool.acquire_multi(count=3, acquire_wait_seconds=0.1))
    assert len(acquired3) == 3
    assert [a.account_id for a in acquired3] == ["acc1", "acc4", "acc2"]


if __name__ == "__main__":
    test_healthy_when_data_returned()
    test_dead_on_redirect()
    test_dead_when_no_session_file()
    test_inconclusive_on_proxy_exhaustion()
    test_inconclusive_on_probe_exception()
    test_inconclusive_verdict_does_not_report_health()
    test_pool_acquire_multi_same_region()
    print("ALL LINKEDIN VALIDATE TRI-STATE TESTS PASSED")
