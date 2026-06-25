"""Dynamic account_id enums must be picklable.

Regression for the server-only crash: under multi-worker uvicorn, loguru runs
with enqueue=True and pickles every log record. A record carrying an account_id
enum member (e.g. account_id=<LinkedInAccountId.acc_li_02>) raised
PicklingError because the functional-API enum class wasn't importable under its
own __qualname__ — so the log line was silently dropped.
"""

import os
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_KEY", "x")
os.environ.setdefault("GOODPROXIES_ENABLED", "false")

import api.account_choices as ac  # noqa: E402


def test_dynamic_account_enum_member_round_trips_through_pickle():
    cls = ac._build("LinkedInAccountIdTest", ["acc_li_01", "acc_li_02"])
    member = cls["acc_li_02"]

    restored = pickle.loads(pickle.dumps(member))  # raised PicklingError before fix

    assert restored is member          # same singleton member
    assert restored == "acc_li_02"     # str-mixin: equals the raw id
    # And the class is reachable under its own name (what pickle looks up).
    assert getattr(ac, "LinkedInAccountIdTest") is cls
