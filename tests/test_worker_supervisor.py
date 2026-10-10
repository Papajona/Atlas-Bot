import asyncio

import pytest

import app.main as main


def test_supervisor_restarts_worker_when_its_audit_call_raises(monkeypatch):
    calls = []

    async def broken_audit(*args, **kwargs):
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(main, "_audit", broken_audit)

    async def worker():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            await main._audit("WORKER_TEST_FAILURE", {"cycle": 1})
        raise asyncio.CancelledError()

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await main._supervise_worker(
                "audit-failure-regression",
                worker,
                initial_backoff=0,
                max_backoff=0,
            )

    asyncio.run(run())
    assert calls == [1, 2]
