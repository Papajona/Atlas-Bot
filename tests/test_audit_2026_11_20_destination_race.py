"""Withdrawal destination first-use race (2026-11-20 audit). Fake session; no database required."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError

from app.transaction_policy import register_or_check_destination


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _RacingDB:
    """First lookup finds nothing; the insert loses the race; the retry lookup finds the winner."""

    def __init__(self, winner):
        self.winner = winner
        self.selects = 0
        self.savepoints = 0

    async def execute(self, _statement):
        self.selects += 1
        return _Result(None if self.selects == 1 else self.winner)

    def add(self, _row):
        pass

    async def flush(self):
        raise IntegrityError("INSERT", {}, Exception("uq_withdrawal_destination_customer_fingerprint"))

    @asynccontextmanager
    async def begin_nested(self):
        self.savepoints += 1
        yield


def test_losing_the_first_use_race_reuses_the_committed_row_instead_of_failing():
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    winner = SimpleNamespace(status="PENDING", first_seen_at=now, last_used_at=None, verified_at=None)
    db = _RacingDB(winner)
    out = asyncio.run(register_or_check_destination(
        db, customer_id=1, destination="T" + "A" * 33, currency="USDT", network="TRON", now=now))
    assert db.savepoints == 1 and db.selects == 2
    assert out["allowed"] is False
    assert winner.last_used_at == now
