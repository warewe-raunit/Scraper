"""
tools/account_risk.py — Pure, unit-testable module for account ban risk calculations.
Python 3.10 compatible.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import time

BanState = Enum("BanState", {
    "CLEAR": "clear",
    "AT_RISK": "at_risk",
    "SHADOW_SUSPECTED": "shadow_suspected",
    "SHADOW_CONFIRMED": "shadow_confirmed",
    "SUSPENDED": "suspended",
    "RESTRICTED": "restricted",
}, type=str)

# Platform-agnostic names mapped to risk weights.
DEFAULT_WEIGHTS = {
    "ok": 0.0,                  # 200 / healthy -> decays score
    "rate_limited": 1.0,        # 429 at low request rate
    "forbidden": 3.0,           # 403
    "challenge_after_relogin": 5.0,   # session refreshed but still blocked
    "soft_block": 2.0,          # LinkedIn 999
    "suspend_confirmed": 999.0, # is_suspended == true -> instant max
    "shadow_confirmed": 999.0,  # logged-out 404 while logged-in 200
    "inconclusive": 0.0,        # proxy/network — never condemns
}

TERMINAL_STATES = {
    BanState.SHADOW_CONFIRMED.value,
    BanState.SUSPENDED.value,
    BanState.RESTRICTED.value,
}

@dataclass
class RiskLedger:
    account_id: str
    platform: str               # "reddit" | "linkedin"
    risk_score: float = 0.0
    ban_state: str = BanState.CLEAR.value
    consecutive_forbidden: int = 0
    last_signals: list = field(default_factory=list)   # list of (timestamp, signal)
    flagged_at: float | None = None
    decay: float = 0.85
    warn_threshold: float = 6.0
    suspect_threshold: float = 10.0
    consecutive_forbidden_threshold: int = 4

    def record(self, signal: str, weight: float | None = None) -> None:
        """Apply one signal. 'ok' decays; danger signals add weight & update
        ban_state per thresholds. Pure arithmetic — call this from hot paths."""
        if signal == "inconclusive":
            # Strict no-op for inconclusive signals
            return

        now = time.time()
        
        # Determine weight
        if weight is None:
            weight = DEFAULT_WEIGHTS.get(signal, 0.0)

        # Apply signal effect
        if signal == "ok":
            self.risk_score *= self.decay
            if self.risk_score < 0.01:  # Prevent tiny float underflow issues
                self.risk_score = 0.0
            self.consecutive_forbidden = 0
        else:
            # Danger signal
            self.risk_score += weight
            if signal == "forbidden":
                self.consecutive_forbidden += 1

        # Keep ring buffer of last ~20 signals
        self.last_signals.append((now, signal))
        if len(self.last_signals) > 20:
            self.last_signals = self.last_signals[-20:]

        # Update ban state based on thresholds, unless in terminal state
        if self.ban_state not in TERMINAL_STATES:
            # Check for suspects first, then warn
            is_suspect = (
                self.risk_score >= self.suspect_threshold
                or self.consecutive_forbidden >= self.consecutive_forbidden_threshold
                or signal == "challenge_after_relogin"
            )
            
            if is_suspect:
                self.ban_state = BanState.SHADOW_SUSPECTED.value
                if self.flagged_at is None:
                    self.flagged_at = now
            elif self.risk_score >= self.warn_threshold:
                self.ban_state = BanState.AT_RISK.value
                if self.flagged_at is None:
                    self.flagged_at = now
            elif signal == "ok" and self.risk_score < self.warn_threshold:
                # Returned to clear
                self.ban_state = BanState.CLEAR.value
                self.flagged_at = None

    def confirm(self, verdict: str) -> None:
        """Apply a definitive probe verdict: 'suspended' | 'shadow_confirmed'
        | 'restricted' | 'clear'. Sets the terminal ban_state."""
        now = time.time()
        if verdict == "clear":
            self.ban_state = BanState.CLEAR.value
            self.risk_score = 0.0
            self.consecutive_forbidden = 0
            self.flagged_at = None
        elif verdict in (BanState.SUSPENDED.value, BanState.SHADOW_CONFIRMED.value, BanState.RESTRICTED.value):
            self.ban_state = verdict
            self.risk_score = 999.0
            if self.flagged_at is None:
                self.flagged_at = now
        # Verdict 'inconclusive' is a strict no-op

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "platform": self.platform,
            "risk_score": self.risk_score,
            "ban_state": self.ban_state,
            "consecutive_forbidden": self.consecutive_forbidden,
            "last_signals": self.last_signals,
            "flagged_at": self.flagged_at,
            "decay": self.decay,
            "warn_threshold": self.warn_threshold,
            "suspect_threshold": self.suspect_threshold,
            "consecutive_forbidden_threshold": self.consecutive_forbidden_threshold,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RiskLedger:
        return cls(
            account_id=d["account_id"],
            platform=d["platform"],
            risk_score=d.get("risk_score", 0.0),
            ban_state=d.get("ban_state", BanState.CLEAR.value),
            consecutive_forbidden=d.get("consecutive_forbidden", 0),
            last_signals=d.get("last_signals") or [],
            flagged_at=d.get("flagged_at"),
            decay=d.get("decay", 0.85),
            warn_threshold=d.get("warn_threshold", 6.0),
            suspect_threshold=d.get("suspect_threshold", 10.0),
            consecutive_forbidden_threshold=d.get("consecutive_forbidden_threshold", 4),
        )
