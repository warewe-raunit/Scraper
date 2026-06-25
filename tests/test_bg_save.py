"""Contract test for the shared fire-and-forget DB-persistence helper."""

import asyncio

from api.services import bg_save
from api.services.bg_save import save_bg


def test_save_bg_runs_coro_off_caller_and_does_not_block():
    """The caller returns immediately; the coroutine runs on the loop and the
    task is tracked so it isn't GC'd before completing."""
    ran = asyncio.Event()

    async def _persist():
        ran.set()

    async def main():
        save_bg(_persist())
        assert not ran.is_set()          # not awaited inline — caller didn't block
        await asyncio.sleep(0)           # yield so the background task can run
        assert ran.is_set()
        await asyncio.sleep(0)           # one more turn for the done callback
        assert not bg_save._bg_tasks      # done callback cleaned up the set

    asyncio.run(main())


def test_save_bg_logs_failure_instead_of_raising(monkeypatch):
    """A dropped write is logged via `log_event`, never surfaced to the caller."""
    logged = {}
    monkeypatch.setattr(bg_save.logger, "warning",
                        lambda event, **kw: logged.update(event=event, **kw))

    async def _boom():
        raise ValueError("supabase down")

    async def main():
        save_bg(_boom(), log_event="unit_save_failed")
        await asyncio.sleep(0)           # let the task run + done callback fire
        await asyncio.sleep(0)

    asyncio.run(main())
    assert logged["event"] == "unit_save_failed"
    assert "supabase down" in logged["error"]


def test_save_bg_closes_coro_when_no_running_loop():
    """Called from sync context: no loop -> coroutine is closed, not leaked."""
    async def _persist():
        pass

    coro = _persist()
    save_bg(coro)                        # no running loop
    # A closed coroutine cannot be awaited again.
    try:
        asyncio.run(coro)
    except RuntimeError as e:
        assert "cannot reuse" in str(e) or "already" in str(e)
    else:
        raise AssertionError("coroutine was not closed")
