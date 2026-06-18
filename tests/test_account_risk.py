"""
tests/test_account_risk.py — Unit tests for RiskLedger and persistence.
"""

from __future__ import annotations
import os
import json
import time
import pytest
from tools.account_risk import RiskLedger, BanState, DEFAULT_WEIGHTS
from tools import ban_state_store

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

def test_confirm_suspended_latches():
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
