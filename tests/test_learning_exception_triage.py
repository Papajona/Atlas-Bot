"""Behavioral checks for persisted research and episode-learning failures."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import db, daily_research, trade_learning


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


def test_prior_trial_label_database_errors_propagate(monkeypatch):
    class BrokenSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _query):
            raise SQLAlchemyError("research history database unavailable")

    monkeypatch.setattr(db, "SessionLocal", lambda: BrokenSession())
    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        asyncio.run(daily_research._prior_trial_labels("BTC/USDT"))


def test_malformed_prior_trial_history_fails_closed(monkeypatch):
    class MalformedSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _query):
            return _ScalarRows(["[]"])

    monkeypatch.setattr(db, "SessionLocal", lambda: MalformedSession())
    with pytest.raises(RuntimeError, match="history is malformed"):
        asyncio.run(daily_research._prior_trial_labels("BTC/USDT"))


def _episode_session_factory(episode, persisted):
    sessions = []

    class Session:
        def __init__(self, initial):
            self.initial = initial
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _query):
            return _ScalarRows([episode] if self.initial else [])

        async def get(self, _model, _id, **_kwargs):
            return persisted

        async def commit(self):
            self.commits += 1

    def factory():
        session = Session(initial=not sessions)
        sessions.append(session)
        return session

    return factory, sessions


def test_per_episode_runtime_failures_are_persisted_and_counted(monkeypatch):
    episode = SimpleNamespace(id=71)
    persisted = SimpleNamespace(id=71, replay_status="PENDING", replay_error="", updated_at=None)
    factory, sessions = _episode_session_factory(episode, persisted)

    def raise_runtime_error(*_args, **_kwargs):
        raise RuntimeError("provider replay timed out")

    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(trade_learning, "_fetch_learning_data", raise_runtime_error)

    result = asyncio.run(trade_learning.process_trade_learning_episodes(limit=1))

    assert result["selected"] == 1
    assert result["failed"] == 1
    assert result["skipped"] == 0
    assert persisted.replay_status == "ERROR"
    assert persisted.replay_error == "provider replay timed out"
    assert sessions[-1].commits == 1


def test_unsupported_episode_errors_are_persisted_as_unsupported(monkeypatch):
    episode = SimpleNamespace(id=72)
    persisted = SimpleNamespace(id=72, replay_status="PENDING", replay_error="", replay_version="", updated_at=None)
    factory, _sessions = _episode_session_factory(episode, persisted)

    def raise_unsupported(*_args, **_kwargs):
        raise ValueError("unsupported market")

    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(trade_learning, "_fetch_learning_data", raise_unsupported)
    result = asyncio.run(trade_learning.process_trade_learning_episodes(limit=1))

    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert persisted.replay_status == "UNSUPPORTED"
    assert persisted.replay_error == "unsupported market"
