"""
tests/test_linkedin_checkpoint_on_arrival.py — regression test for "system not
fetching OTP" bug.

LinkedIn varies the relogin flow run-to-run: sometimes it shows a password page,
sometimes it jumps straight to an email-OTP verification step. login() must
handle BOTH by page content (not URL guesses):
  - OTP field present, no password  -> route into the email-OTP handler
  - password field present           -> submit credentials (then OTP may follow)
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.linkedin_login as L  # noqa: E402


class _FakeEl:
    async def fill(self, *a, **k):
        pass

    async def press(self, *a, **k):
        pass

    async def click(self, *a, **k):
        pass


class _FakePage:
    def __init__(self):
        self.url = "https://www.linkedin.com/login"

    def on(self, *a, **k):
        pass

    def remove_listener(self, *a, **k):
        pass

    async def goto(self, url, **k):
        self.url = url


async def _noop(*a, **k):
    return None


def _run_login(monkey_attrs):
    saved = {name: getattr(L, name) for name in monkey_attrs}
    for name, val in monkey_attrs.items():
        setattr(L, name, val)
    try:
        return asyncio.run(L.login(
            page=_FakePage(), account_id="acc_li_01",
            username="avinashkum643@gmail.com", password="pw",
        ))
    finally:
        for name, val in saved.items():
            setattr(L, name, val)


def test_otp_on_arrival_routes_to_handler():
    """No password field + OTP input present -> OTP handler is invoked."""
    calls = {"otp": 0}
    logged = iter([False, True])  # not logged in -> OTP solved -> logged in

    async def _no_fields(page, kind, sels):
        return None

    async def _detect_otp(page, max_wait_seconds=2.0):
        return _FakeEl()  # an OTP field is on the page

    async def _is_logged_in(page, username=""):
        try:
            return next(logged)
        except StopIteration:
            return True

    async def _handle_otp(page, username, account_id):
        calls["otp"] += 1
        return True

    res = _run_login({
        "_login_url_candidates": lambda aid: ["https://www.linkedin.com/login",
                                              "https://www.linkedin.com/uas/login"],
        "_find_login_field": _no_fields,
        "_detect_otp_input": _detect_otp,
        "_is_logged_in": _is_logged_in,
        "_handle_email_verification_challenge": _handle_otp,
        "_delay": _noop,
        "_random_scroll": _noop,
        "_extract_login_error": _noop,
    })
    assert calls["otp"] == 1, "OTP handler must run when an OTP field is present"
    assert res.get("success") is True, res


def test_password_present_takes_credentials_path():
    """A password field present -> credentials submitted, no premature OTP/raise."""
    calls = {"otp": 0, "typed": 0}

    async def _fields(page, kind, sels):
        # username absent (Welcome-back style), password present
        return _FakeEl() if kind == "password" else None

    async def _detect_otp(page, max_wait_seconds=2.0):
        return None  # should not even be consulted on the password path

    async def _resolve(page, el):
        return el

    async def _ghost(page, el):
        pass

    async def _type(page, el, text, account_id):
        calls["typed"] += 1

    async def _is_logged_in(page, username=""):
        return True  # password submit succeeds, no OTP needed this run

    async def _handle_otp(page, username, account_id):
        calls["otp"] += 1
        return True

    res = _run_login({
        "_login_url_candidates": lambda aid: ["https://www.linkedin.com/login"],
        "_find_login_field": _fields,
        "_detect_otp_input": _detect_otp,
        "_resolve_editable_element": _resolve,
        "_ghost_move_and_click": _ghost,
        "_human_type": _type,
        "_is_logged_in": _is_logged_in,
        "_handle_email_verification_challenge": _handle_otp,
        "_delay": _noop,
        "_random_scroll": _noop,
        "_extract_login_error": _noop,
    })
    assert calls["typed"] >= 1, "password should have been typed"
    assert calls["otp"] == 0, "OTP handler must NOT run when login already succeeded"
    assert res.get("success") is True, res


if __name__ == "__main__":
    test_otp_on_arrival_routes_to_handler()
    test_password_present_takes_credentials_path()
    print("BOTH FLOW VARIANTS PASSED")
