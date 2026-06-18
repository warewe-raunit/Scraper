"""
tests/test_reddit_auto_relogin.py — unit tests for the Reddit account registry's
proactive health snapshot + background auto-relogin (the LinkedIn-style pool
behavior added for Reddit). All login + filesystem access is stubbed so no
browser launches and no real sessions are touched.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("GOODPROXIES_ENABLED", "false")

import api.dependencies as deps  # noqa: E402
import api.services.registry as registry_mod  # noqa: E402
from api.services.registry import AccountRegistry  # noqa: E402


def _make_registry(monkeypatch, *, configured, with_session, login_result=True, login_calls=None):
    """Build an AccountRegistry with env/session/login fully stubbed.

    configured:   list of account_ids present in .env
    with_session: set of account_ids that have a saved session file
    login_result: what the stubbed login_account returns (or a callable(acc)->bool)
    """
    accounts = [
        {"account_id": aid, "username": f"u_{aid}", "password": "pw", "proxy_url": None}
        for aid in configured
    ]
    monkeypatch.setattr(deps, "parse_accounts_from_env", lambda: list(accounts))
    monkeypatch.setattr(deps, "get_available_session_accounts", lambda: list(with_session))

    def _find_session_file(aid):
        return Path(f"{aid}.json") if aid in with_session else None

    monkeypatch.setattr(deps, "find_session_file", _find_session_file)
    # Fake a FRESH session mtime (now) so the age-based proactive-expiry check in
    # check_proactive_expiry()/snapshot() treats stubbed sessions as recent and
    # doesn't flag them just because the file isn't real.
    now = time.time()
    fresh_stat = os.stat_result((0, 0, 0, 0, 0, 0, 0, now, now, now))
    monkeypatch.setattr(Path, "stat", lambda self: fresh_stat, raising=False)

    async def _login_account(acc, captcha_config, headless=True):
        if login_calls is not None:
            login_calls.append(acc["account_id"])
        ok = login_result(acc) if callable(login_result) else login_result
        # Simulate the session file appearing after a successful login.
        if ok:
            with_session.add(acc["account_id"])
        return ok

    monkeypatch.setattr(registry_mod, "login_account", _login_account)
    return AccountRegistry()


def test_snapshot_reports_no_session_accounts(monkeypatch):
    reg = _make_registry(monkeypatch, configured=["acc_01", "acc_02"], with_session={"acc_01"})
    snap = reg.snapshot()
    by_id = {a["account_id"]: a for a in snap["accounts"]}

    assert by_id["acc_01"]["status"] == "healthy"
    assert by_id["acc_01"]["has_session"] is True
    assert by_id["acc_02"]["status"] == "no_session"
    assert by_id["acc_02"]["has_session"] is False

    c = snap["counters"]
    assert c["configured"] == 2
    assert c["registered"] == 1
    assert c["no_session"] == 1
    assert c["healthy"] == 1


def test_auto_relogin_disabled_does_not_queue(monkeypatch):
    monkeypatch.setenv("REDDIT_AUTO_RELOGIN", "false")
    reg = _make_registry(monkeypatch, configured=["acc_01"], with_session=set())
    queued = asyncio.run(reg.force_relogin("acc_01"))
    assert queued is False
    assert "acc_01" not in reg._relogin_tasks


def test_schedule_relogin_dedups(monkeypatch):
    monkeypatch.setenv("REDDIT_AUTO_RELOGIN", "true")

    async def _scenario():
        # Slow login so the first task is still in flight when we re-schedule.
        async def _slow_login(acc, captcha_config, headless=True):
            await asyncio.sleep(0.2)
            return True
        monkeypatch.setattr(registry_mod, "login_account", _slow_login)

        reg = _make_registry(monkeypatch, configured=["acc_01"], with_session=set())
        # rebind login_account after _make_registry overwrote it
        monkeypatch.setattr(registry_mod, "login_account", _slow_login)

        first = reg._schedule_relogin("acc_01")
        task1 = reg._relogin_tasks["acc_01"]
        second = reg._schedule_relogin("acc_01")
        task2 = reg._relogin_tasks["acc_01"]
        assert first is True and second is True
        assert task1 is task2  # deduped: same in-flight task
        await task1

    asyncio.run(_scenario())


def test_sweep_queues_and_relogins_missing_sessions(monkeypatch):
    monkeypatch.setenv("REDDIT_AUTO_RELOGIN", "true")
    login_calls = []

    async def _scenario():
        reg = _make_registry(
            monkeypatch,
            configured=["acc_01", "acc_02"],
            with_session={"acc_01"},
            login_result=True,
            login_calls=login_calls,
        )
        # acc_02 has no session → sweep should queue a relogin for it.
        await reg.sweep_and_relogin()
        # Wait for the background relogin worker(s) to finish.
        tasks = [t for t in reg._relogin_tasks.values() if not t.done()]
        if tasks:
            await asyncio.gather(*tasks)
        return reg

    reg = asyncio.run(_scenario())
    assert "acc_02" in login_calls  # the dead/missing account was relogged
    assert "acc_01" not in login_calls  # the healthy one was left alone
    # After a successful relogin acc_02 is registered and healthy.
    assert "acc_02" in reg.states
    assert reg.states["acc_02"].status == "healthy"


def test_failed_relogin_cools_down_known_account(monkeypatch):
    monkeypatch.setenv("REDDIT_AUTO_RELOGIN", "true")

    async def _scenario():
        reg = _make_registry(
            monkeypatch,
            configured=["acc_01"],
            with_session={"acc_01"},
            login_result=False,
        )
        reg.flag_relogin_needed("acc_01")
        ok = await reg._run_relogin_once("acc_01")
        return reg, ok

    reg, ok = asyncio.run(_scenario())
    assert ok is False
    # A failed relogin puts the known account on cooldown rather than spinning.
    assert reg.states["acc_01"].status == "cool_down"
