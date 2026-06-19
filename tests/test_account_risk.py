"""
tests/test_account_risk.py — Unit tests for RiskLedger, persistence, and Reddit integration.
"""

from __future__ import annotations
import os
import json
import time
from pathlib import Path
import pytest
import asyncio
from tools.account_risk import RiskLedger, BanState, DEFAULT_WEIGHTS
from tools import ban_state_store

@pytest.fixture
def anyio_backend():
    return "asyncio"

def test_ok_decays_score():
    ledger = RiskLedger(account_id="test_acc", platform="reddit")
    
    # Increase score
    ledger.record("rate_limited")  # +1.0
    ledger.record("forbidden")     # +3.0
    assert ledger.risk_score == 4.0
    
    # Record OK and ensure it decays
    ledger.record("ok")
    assert ledger.risk_score == 4.0 * 0.85
    assert ledger.ban_state == BanState.CLEAR.value
    
    # Repeated OK returns to clear/zero
    for _ in range(40):
        ledger.record("ok")
    assert ledger.risk_score == 0.0
    assert ledger.ban_state == BanState.CLEAR.value

def test_isolated_429_does_not_warn():
    ledger = RiskLedger(account_id="test_acc", platform="reddit", warn_threshold=6.0)
    ledger.record("rate_limited")  # +1.0
    assert ledger.risk_score == 1.0
    assert ledger.ban_state == BanState.CLEAR.value

def test_repeated_forbidden_escalates():
    ledger = RiskLedger(
        account_id="test_acc", 
        platform="reddit", 
        warn_threshold=6.0, 
        suspect_threshold=10.0
    )
    
    # 1 forbidden: score 3.0 (clear)
    ledger.record("forbidden")
    assert ledger.risk_score == 3.0
    assert ledger.ban_state == BanState.CLEAR.value
    
    # 2 forbidden: score 6.0 (at_risk)
    ledger.record("forbidden")
    assert ledger.risk_score == 6.0
    assert ledger.ban_state == BanState.AT_RISK.value
    assert ledger.flagged_at is not None
    
    # 3 forbidden: score 9.0 (at_risk)
    ledger.record("forbidden")
    assert ledger.risk_score == 9.0
    assert ledger.ban_state == BanState.AT_RISK.value
    
    # 4 forbidden: score 12.0 (shadow_suspected due to score >= 10 and streak >= 4)
    ledger.record("forbidden")
    assert ledger.risk_score == 12.0
    assert ledger.ban_state == BanState.SHADOW_SUSPECTED.value
    assert ledger.consecutive_forbidden == 4

def test_consecutive_forbidden_threshold():
    ledger = RiskLedger(
        account_id="test_acc", 
        platform="reddit", 
        warn_threshold=20.0,  # set high so score alone doesn't trigger
        suspect_threshold=30.0
    )
    
    # 4 forbidden triggers suspect even if score < threshold
    for _ in range(4):
        ledger.record("forbidden")
    assert ledger.ban_state == BanState.SHADOW_SUSPECTED.value
    assert ledger.consecutive_forbidden == 4

def test_confirm_suspended_latches(monkeypatch):
    # Disable post-ban cooldown so confirm("clear") takes effect immediately
    monkeypatch.setenv("BAN_CONFIRMED_COOLDOWN_SECONDS", "0")
    ledger = RiskLedger(account_id="test_acc", platform="reddit")
    ledger.confirm("suspended")
    assert ledger.ban_state == BanState.SUSPENDED.value
    assert ledger.risk_score == 999.0

    # Ok does not clear terminal state
    ledger.record("ok")
    assert ledger.ban_state == BanState.SUSPENDED.value

    # Only confirm clear resets it
    ledger.confirm("clear")
    assert ledger.ban_state == BanState.CLEAR.value
    assert ledger.risk_score == 0.0


def test_post_ban_cooldown_blocks_clear():
    """A confirmed terminal state cannot be cleared while the post-ban
    cooldown window is active. Once the window elapses, clear takes effect."""
    os.environ["BAN_CONFIRMED_COOLDOWN_SECONDS"] = "86400"  # 1 day
    try:
        ledger = RiskLedger(account_id="test_acc", platform="reddit")
        ledger.confirm("suspended")
        assert ledger.is_terminal()
        assert ledger.is_in_post_ban_cooldown()

        applied = ledger.confirm("clear")
        assert applied is False
        assert ledger.ban_state == BanState.SUSPENDED.value
        assert ledger.flagged_at is not None

        # Simulate cooldown elapsed by rewinding flagged_at past the window
        ledger.flagged_at = time.time() - 86401
        assert not ledger.is_in_post_ban_cooldown()

        applied = ledger.confirm("clear")
        assert applied is True
        assert ledger.ban_state == BanState.CLEAR.value
        assert ledger.flagged_at is None
    finally:
        os.environ.pop("BAN_CONFIRMED_COOLDOWN_SECONDS", None)


def test_post_ban_cooldown_persists_across_restart(tmp_path):
    """The cooldown anchor (flagged_at) survives save→load, so a server
    restart honors the remaining cooldown rather than resetting it."""
    os.environ["BAN_CONFIRMED_COOLDOWN_SECONDS"] = "86400"
    os.environ["BAN_STATE_FILE"] = str(tmp_path / "ban_state.json")
    try:
        ledger = RiskLedger(account_id="test_acc", platform="reddit")
        ledger.confirm("suspended")
        ban_state_store.save({"test_acc": ledger})

        reloaded = ban_state_store.load()["test_acc"]
        assert reloaded.ban_state == BanState.SUSPENDED.value
        assert reloaded.flagged_at is not None
        assert reloaded.is_in_post_ban_cooldown()
        assert reloaded.confirm("clear") is False
        assert reloaded.ban_state == BanState.SUSPENDED.value
    finally:
        os.environ.pop("BAN_CONFIRMED_COOLDOWN_SECONDS", None)
        os.environ.pop("BAN_STATE_FILE", None)

def test_inconclusive_never_condemns():
    ledger = RiskLedger(account_id="test_acc", platform="reddit")
    
    # Record inconclusive
    ledger.record("inconclusive")
    assert ledger.risk_score == 0.0
    assert len(ledger.last_signals) == 0
    assert ledger.ban_state == BanState.CLEAR.value
    
    # Confirm inconclusive is a no-op
    ledger.record("forbidden")
    ledger.record("forbidden") # score 6.0 -> at_risk
    assert ledger.ban_state == BanState.AT_RISK.value
    
    ledger.confirm("inconclusive")
    assert ledger.ban_state == BanState.AT_RISK.value

def test_to_from_dict():
    ledger = RiskLedger(account_id="test_acc", platform="reddit")
    ledger.record("rate_limited")
    ledger.record("forbidden")
    
    d = ledger.to_dict()
    ledger2 = RiskLedger.from_dict(d)
    
    assert ledger2.account_id == "test_acc"
    assert ledger2.platform == "reddit"
    assert ledger2.risk_score == 4.0
    assert ledger2.ban_state == BanState.CLEAR.value
    assert len(ledger2.last_signals) == 2

def test_persistence_roundtrip(tmp_path):
    # Setup temp file path
    temp_file = tmp_path / "ban_state.json"
    os.environ["BAN_STATE_FILE"] = str(temp_file)
    
    ledger_reddit = RiskLedger(account_id="reddit_acc", platform="reddit")
    ledger_reddit.record("forbidden")
    
    ledger_linkedin = RiskLedger(account_id="linkedin_acc", platform="linkedin")
    ledger_linkedin.record("rate_limited")
    
    ledgers = {
        "reddit_acc": ledger_reddit,
        "linkedin_acc": ledger_linkedin,
    }
    
    # Save
    ban_state_store.save(ledgers)
    assert temp_file.exists()
    
    # Load
    loaded = ban_state_store.load()
    assert len(loaded) == 2
    assert loaded["reddit_acc"].account_id == "reddit_acc"
    assert loaded["reddit_acc"].platform == "reddit"
    assert loaded["reddit_acc"].risk_score == 3.0
    assert loaded["linkedin_acc"].account_id == "linkedin_acc"
    assert loaded["linkedin_acc"].platform == "linkedin"
    assert loaded["linkedin_acc"].risk_score == 1.0

def test_persistence_corrupt_file_graceful(tmp_path):
    # Setup temp file path
    temp_file = tmp_path / "ban_state.json"
    os.environ["BAN_STATE_FILE"] = str(temp_file)
    
    # Write corrupt JSON
    with open(temp_file, "w") as f:
        f.write("{invalid json")
        
    loaded = ban_state_store.load()
    assert loaded == {}

# Mock classes for integration tests
class MockResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._json_data

class MockSession:
    def __init__(self, mock_get_callable):
        self.mock_get = mock_get_callable
        self.headers = {}
        self.proxy_url = None
        self.account_id = "test_acc"
        self.proxy_display = "mock_proxy"

    def get(self, url, *args, **kwargs):
        return self.mock_get(url, *args, **kwargs)

@pytest.mark.anyio
async def test_reddit_probe_suspended(monkeypatch, tmp_path):
    # Stub store file path to avoid polluting sessions
    temp_file = tmp_path / "ban_state.json"
    monkeypatch.setenv("BAN_STATE_FILE", str(temp_file))
    monkeypatch.setenv("ACCOUNT_BAN_DETECTION", "true")

    from api.services.registry import AccountRegistry
    
    # Create real session file on temp path to avoid FileNotFoundError on stat
    sess_file = tmp_path / "acc_01.json"
    sess_file.write_text("{}")

    # Mock parse env & session files
    import api.dependencies as deps
    monkeypatch.setattr(deps, "parse_accounts_from_env", lambda: [{"account_id": "acc_01", "username": "user01", "password": "pw", "proxy_url": None}])
    monkeypatch.setattr(deps, "get_available_session_accounts", lambda: ["acc_01"])
    monkeypatch.setattr(deps, "find_session_file", lambda aid: sess_file)
    
    # Mock create_stealth_client to return 200 with suspended flag
    def mock_logged_in_get(url, *args, **kwargs):
        return MockResponse(200, {"data": {"is_suspended": True}})
    
    async def mock_create_client(aid):
        return MockSession(mock_logged_in_get)
    monkeypatch.setattr(deps, "create_stealth_client", mock_create_client)

    registry = AccountRegistry()
    verdict = await registry.probe_ban("acc_01")
    assert verdict == "suspended"

@pytest.mark.anyio
async def test_reddit_probe_shadow_confirmed(monkeypatch, tmp_path):
    temp_file = tmp_path / "ban_state.json"
    monkeypatch.setenv("BAN_STATE_FILE", str(temp_file))
    monkeypatch.setenv("ACCOUNT_BAN_DETECTION", "true")

    from api.services.registry import AccountRegistry
    import api.dependencies as deps
    
    sess_file = tmp_path / "acc_01.json"
    sess_file.write_text("{}")
    
    monkeypatch.setattr(deps, "parse_accounts_from_env", lambda: [{"account_id": "acc_01", "username": "user01", "password": "pw", "proxy_url": None}])
    monkeypatch.setattr(deps, "get_available_session_accounts", lambda: ["acc_01"])
    monkeypatch.setattr(deps, "find_session_file", lambda aid: sess_file)
    
    # Mock logged-in client (200 OK, not suspended)
    def mock_logged_in_get(url, *args, **kwargs):
        return MockResponse(200, {"data": {"is_suspended": False}})
        
    async def mock_create_client(aid):
        return MockSession(mock_logged_in_get)
    monkeypatch.setattr(deps, "create_stealth_client", mock_create_client)

    # Mock unauth session client (404 Not Found)
    from curl_cffi import requests as cffi_requests
    def mock_unauth_session(*args, **kwargs):
        return MockSession(lambda url, *args, **kwargs: MockResponse(404))
    monkeypatch.setattr(cffi_requests, "Session", mock_unauth_session)

    registry = AccountRegistry()
    verdict = await registry.probe_ban("acc_01")
    assert verdict == "shadow_confirmed"

@pytest.mark.anyio
async def test_reddit_probe_clear(monkeypatch, tmp_path):
    temp_file = tmp_path / "ban_state.json"
    monkeypatch.setenv("BAN_STATE_FILE", str(temp_file))
    monkeypatch.setenv("ACCOUNT_BAN_DETECTION", "true")

    from api.services.registry import AccountRegistry
    import api.dependencies as deps
    
    sess_file = tmp_path / "acc_01.json"
    sess_file.write_text("{}")
    
    monkeypatch.setattr(deps, "parse_accounts_from_env", lambda: [{"account_id": "acc_01", "username": "user01", "password": "pw", "proxy_url": None}])
    monkeypatch.setattr(deps, "get_available_session_accounts", lambda: ["acc_01"])
    monkeypatch.setattr(deps, "find_session_file", lambda aid: sess_file)
    
    # Mock logged-in client (200 OK, not suspended)
    def mock_logged_in_get(url, *args, **kwargs):
        return MockResponse(200, {"data": {"is_suspended": False}})
        
    async def mock_create_client(aid):
        return MockSession(mock_logged_in_get)
    monkeypatch.setattr(deps, "create_stealth_client", mock_create_client)

    # Mock unauth session client (200 OK, has data)
    from curl_cffi import requests as cffi_requests
    def mock_unauth_session(*args, **kwargs):
        return MockSession(lambda url, *args, **kwargs: MockResponse(200, {"data": {"subreddit": {}}}))
    monkeypatch.setattr(cffi_requests, "Session", mock_unauth_session)

    registry = AccountRegistry()
    verdict = await registry.probe_ban("acc_01")
    assert verdict == "clear"

@pytest.mark.anyio
async def test_reddit_probe_inconclusive(monkeypatch, tmp_path):
    temp_file = tmp_path / "ban_state.json"
    monkeypatch.setenv("BAN_STATE_FILE", str(temp_file))
    monkeypatch.setenv("ACCOUNT_BAN_DETECTION", "true")

    from api.services.registry import AccountRegistry
    import api.dependencies as deps
    
    sess_file = tmp_path / "acc_01.json"
    sess_file.write_text("{}")
    
    monkeypatch.setattr(deps, "parse_accounts_from_env", lambda: [{"account_id": "acc_01", "username": "user01", "password": "pw", "proxy_url": None}])
    monkeypatch.setattr(deps, "get_available_session_accounts", lambda: ["acc_01"])
    monkeypatch.setattr(deps, "find_session_file", lambda aid: sess_file)
    
    # Mock logged-in client (200 OK, not suspended)
    def mock_logged_in_get(url, *args, **kwargs):
        return MockResponse(200, {"data": {"is_suspended": False}})
        
    async def mock_create_client(aid):
        return MockSession(mock_logged_in_get)
    monkeypatch.setattr(deps, "create_stealth_client", mock_create_client)

    # Mock unauth session client (429 Rate Limited)
    from curl_cffi import requests as cffi_requests
    def mock_unauth_session(*args, **kwargs):
        return MockSession(lambda url, *args, **kwargs: MockResponse(429))
    monkeypatch.setattr(cffi_requests, "Session", mock_unauth_session)

    registry = AccountRegistry()
    verdict = await registry.probe_ban("acc_01")
    assert verdict == "inconclusive"

def test_reddit_snapshot_fields(monkeypatch, tmp_path):
    temp_file = tmp_path / "ban_state.json"
    monkeypatch.setenv("BAN_STATE_FILE", str(temp_file))
    monkeypatch.setenv("ACCOUNT_BAN_DETECTION", "true")

    from api.services.registry import AccountRegistry
    import api.dependencies as deps
    
    sess_file = tmp_path / "acc_01.json"
    sess_file.write_text("{}")
    
    monkeypatch.setattr(deps, "parse_accounts_from_env", lambda: [{"account_id": "acc_01", "username": "user01", "password": "pw", "proxy_url": None}])
    monkeypatch.setattr(deps, "get_available_session_accounts", lambda: ["acc_01"])
    monkeypatch.setattr(deps, "find_session_file", lambda aid: sess_file)
    
    registry = AccountRegistry()
    registry.record_signal("acc_01", "forbidden") # +3.0
    registry.record_signal("acc_01", "forbidden") # +3.0 -> risk_score = 6.0 (at_risk)

    snap = registry.snapshot()
    acc_snap = next(a for a in snap["accounts"] if a["account_id"] == "acc_01")
    
    assert acc_snap["ban_state"] == "at_risk"
    assert acc_snap["risk_score"] == 6.0
    assert acc_snap["last_signal"] == "forbidden"
    assert acc_snap["flagged_at"] is not None
    assert "acc_01" in snap["flagged"]

@pytest.mark.anyio
async def test_reddit_protect_rotation_guarantees(monkeypatch, tmp_path):
    temp_file = tmp_path / "ban_state.json"
    monkeypatch.setenv("BAN_STATE_FILE", str(temp_file))
    monkeypatch.setenv("ACCOUNT_BAN_DETECTION", "true")

    from api.services.registry import AccountRegistry
    import api.dependencies as deps
    
    sess_file_1 = tmp_path / "acc_01.json"
    sess_file_1.write_text("{}")
    sess_file_2 = tmp_path / "acc_02.json"
    sess_file_2.write_text("{}")

    # 2 accounts configured and available
    monkeypatch.setattr(deps, "parse_accounts_from_env", lambda: [
        {"account_id": "acc_01", "username": "user01", "password": "pw", "proxy_url": None},
        {"account_id": "acc_02", "username": "user02", "password": "pw", "proxy_url": None}
    ])
    monkeypatch.setattr(deps, "get_available_session_accounts", lambda: ["acc_01", "acc_02"])
    monkeypatch.setattr(deps, "find_session_file", lambda aid: sess_file_1 if aid == "acc_01" else sess_file_2)

    # Phase 1 guarantee: with protect=false, terminal ban status is ignored
    monkeypatch.setenv("ACCOUNT_BAN_PROTECT", "false")
    registry = AccountRegistry()
    
    # Set acc_01 to suspended via confirm
    registry.ledgers["acc_01"].confirm("suspended")
    
    # Should still be able to select acc_01
    selected = await registry.get_next_healthy_account("acc_01")
    assert selected == "acc_01"

    # Phase 2: with protect=true, terminal status raises error / excludes in rotation
    monkeypatch.setenv("ACCOUNT_BAN_PROTECT", "true")
    registry_protect = AccountRegistry()
    registry_protect.ledgers["acc_01"].confirm("suspended")

    # Explicit request raises error
    with pytest.raises(RuntimeError):
        await registry_protect.get_next_healthy_account("acc_01")

    # Rotation excludes acc_01 and only returns acc_02
    for _ in range(5):
        selected_rot = await registry_protect.get_next_healthy_account()
        assert selected_rot == "acc_02"


@pytest.mark.anyio
async def test_linkedin_relogin_restriction_verdict(monkeypatch, tmp_path):
    temp_file = tmp_path / "ban_state.json"
    monkeypatch.setenv("BAN_STATE_FILE", str(temp_file))
    monkeypatch.setenv("ACCOUNT_BAN_DETECTION", "true")
    monkeypatch.setenv("LINKEDIN_AUTO_RELOGIN", "true")

    import api.services.linkedin_account_pool as pool_mod
    import api.services.linkedin_env as env_mod

    monkeypatch.setattr(env_mod, "parse_linkedin_accounts_env", lambda: [
        {"account_id": "acc_li_01", "username": "user01", "password": "pw", "proxy_url": None}
    ])
    
    # Mock login runner to return False and "restricted" reason
    import api.services.linkedin_login_runner as runner_mod
    async def mock_login(*args, **kwargs):
        return False, "This account is temporarily restricted due to security verification."
    monkeypatch.setattr(runner_mod, "login_account_with_retries", mock_login)

    # Force clean pool instantiation
    monkeypatch.setattr(pool_mod.LinkedInAccountPool, "_instance", None)
    pool = await pool_mod.LinkedInAccountPool.instance()
    
    # Trigger relogin
    await pool._run_relogin_once("acc_li_01")
    
    # Check that ledger is restricted
    acc = pool._accounts["acc_li_01"]
    assert acc.ledger is not None
    assert acc.ledger.ban_state == "restricted"


@pytest.mark.anyio
async def test_linkedin_snapshot_fields(monkeypatch, tmp_path):
    temp_file = tmp_path / "ban_state.json"
    monkeypatch.setenv("BAN_STATE_FILE", str(temp_file))
    monkeypatch.setenv("ACCOUNT_BAN_DETECTION", "true")

    import api.services.linkedin_account_pool as pool_mod
    import api.services.linkedin_env as env_mod

    monkeypatch.setattr(env_mod, "parse_linkedin_accounts_env", lambda: [
        {"account_id": "acc_li_01", "username": "user01", "password": "pw", "proxy_url": None}
    ])
    
    monkeypatch.setattr(pool_mod.LinkedInAccountPool, "_instance", None)
    pool = await pool_mod.LinkedInAccountPool.instance()
    
    pool.record_signal("acc_li_01", "forbidden") # +3.0
    pool.record_signal("acc_li_01", "forbidden") # +3.0 -> risk_score = 6.0 (at_risk)

    snap = pool.snapshot()
    acc_snap = next(a for a in snap["accounts"] if a["account_id"] == "acc_li_01")
    
    assert acc_snap["ban_state"] == "at_risk"
    assert acc_snap["risk_score"] == 6.0
    assert acc_snap["last_signal"] == "forbidden"
    assert acc_snap["flagged_at"] is not None
    assert "acc_li_01" in snap["flagged"]


@pytest.mark.anyio
async def test_linkedin_protect_rotation_guarantees(monkeypatch, tmp_path):
    temp_file = tmp_path / "ban_state.json"
    monkeypatch.setenv("BAN_STATE_FILE", str(temp_file))
    monkeypatch.setenv("ACCOUNT_BAN_DETECTION", "true")
    # Test exercises the suspended→clear→at_risk recovery path; cooldown bypass
    monkeypatch.setenv("BAN_CONFIRMED_COOLDOWN_SECONDS", "0")

    import api.services.linkedin_account_pool as pool_mod
    import api.services.linkedin_env as env_mod

    monkeypatch.setattr(env_mod, "parse_linkedin_accounts_env", lambda: [
        {"account_id": "acc_li_01", "username": "user01", "password": "pw", "proxy_url": None},
        {"account_id": "acc_li_02", "username": "user02", "password": "pw", "proxy_url": None}
    ])

    # Phase 1: protect=false -> terminal ban status is ignored
    monkeypatch.setenv("ACCOUNT_BAN_PROTECT", "false")
    monkeypatch.setattr(pool_mod.LinkedInAccountPool, "_instance", None)
    pool = await pool_mod.LinkedInAccountPool.instance()
    
    pool.confirm_verdict("acc_li_01", "suspended")
    
    async with pool._lock:
        selected = pool._pick_next_alive_locked()
    assert selected is not None
    # With protect=false, acc_li_01 is still ALIVE in status and chosen
    assert selected.account_id in ("acc_li_01", "acc_li_02")

    # Phase 2: protect=true -> terminal status excludes, at_risk demoted
    monkeypatch.setenv("ACCOUNT_BAN_PROTECT", "true")
    monkeypatch.setattr(pool_mod.LinkedInAccountPool, "_instance", None)
    pool_protect = await pool_mod.LinkedInAccountPool.instance()
    
    pool_protect.confirm_verdict("acc_li_01", "suspended")
    
    # acc_li_01 is suspended, so pick_next should ONLY return acc_li_02
    for _ in range(5):
        async with pool_protect._lock:
            selected_rot = pool_protect._pick_next_alive_locked()
        assert selected_rot is not None
        assert selected_rot.account_id == "acc_li_02"

    # Demote at_risk: let's clear terminal status of acc_li_01 and make it at_risk
    pool_protect.confirm_verdict("acc_li_01", "clear")
    pool_protect.record_signal("acc_li_01", "forbidden")
    pool_protect.record_signal("acc_li_01", "forbidden") # risk_score = 6.0 (at_risk)
    
    # Both are ALIVE, but acc_li_01 is at_risk, acc_li_02 is clear.
    # Pass 1 should pick acc_li_02 first, then fallback to acc_li_01 only in Pass 2 if acc_li_02 is in_use.
    # Let's verify pick_next:
    pool_protect._index = 0  # reset round-robin index
    
    # Pick next (without in_use) -> picks acc_li_02 because it's not at_risk
    async with pool_protect._lock:
        selected_rot = pool_protect._pick_next_alive_locked()
    assert selected_rot.account_id == "acc_li_02"
