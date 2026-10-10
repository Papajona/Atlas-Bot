import asyncio

import pytest

import app.main as main


def test_worker_supervisor_restarts_when_worker_and_audit_both_raise(monkeypatch):
    async def run():
        calls = {"worker": 0, "audit": 0}
        restarted = asyncio.Event()
        blocker = asyncio.Event()

        async def failing_audit(*_args, **_kwargs):
            calls["audit"] += 1
            raise RuntimeError("audit database unavailable")

        async def worker_factory():
            calls["worker"] += 1
            if calls["worker"] == 1:
                raise RuntimeError("worker database operation failed")
            restarted.set()
            await blocker.wait()

        monkeypatch.setattr(main, "_audit", failing_audit)
        task = asyncio.create_task(
            main._supervise_worker(
                "test-worker",
                worker_factory,
                initial_backoff=0.001,
                max_backoff=0.002,
            )
        )
        try:
            await asyncio.wait_for(restarted.wait(), timeout=1.0)
            assert calls["worker"] >= 2
            assert calls["audit"] >= 1
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
