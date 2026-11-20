"""Trade-learning replay must recheck fee eligibility after expensive data fetches."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace


def test_episode_quarantined_during_fetch_is_not_persisted_as_replayed(monkeypatch):
    from app import db as db_module
    from app import trade_learning

    selected_snapshot = SimpleNamespace(
        id=7,
        status="COMPLETED",
        replay_status="PENDING",
        replay_version="",
    )
    locked_row = SimpleNamespace(
        id=7,
        status="FEE_PENDING",
        replay_status="PENDING",
        replay_version="",
    )

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [selected_snapshot]

    class Session:
        def __init__(self, *, initial: bool):
            self.initial = initial
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, _type, _value, _traceback):
            return False

        async def execute(self, _statement):
            assert self.initial
            return Result()

        async def get(self, _model, episode_id, *, with_for_update=False):
            assert not self.initial
            assert episode_id == 7
            assert with_for_update is True
            return locked_row

        async def commit(self):
            self.commits += 1

    sessions = [Session(initial=True), Session(initial=False)]
    created = 0

    def session_factory():
        nonlocal created
        session = sessions[min(created, 1)]
        created += 1
        return session

    monkeypatch.setattr(db_module, "SessionLocal", session_factory)
    monkeypatch.setattr(
        trade_learning,
        "_fetch_learning_data",
        lambda _episode, *, days: object(),
    )
    monkeypatch.setattr(
        trade_learning,
        "replay_trade_episode",
        lambda _frame, _episode: {},
    )

    result = asyncio.run(
        trade_learning.process_trade_learning_episodes(limit=1)
    )

    assert result["selected"] == 1
    assert result["processed"] == 0
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert sessions[1].commits == 0
    assert locked_row.status == "FEE_PENDING"
