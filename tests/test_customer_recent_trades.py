"""Customer recent-trade endpoint is bounded and always owner-scoped."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app import main


def test_customer_recent_trades_is_customer_scoped_bounded_and_redacted(monkeypatch):
    profile = SimpleNamespace(id=314)
    created_at = datetime(2026, 11, 26, tzinfo=timezone.utc)
    trade = SimpleNamespace(
        id=91,
        customer_id=314,
        symbol="BTC/USDT",
        side="buy",
        mode="paper",
        status="partially_filled",
        filled_quantity="0.25",
        average_fill_price="42000.50",
        created_at=created_at,
        updated_at=created_at,
        broker_order_id="must-not-leak",
        client_order_id="must-not-leak-either",
    )
    captured = {}

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [trade]

    class Database:
        async def execute(self, statement):
            captured["statement"] = statement
            return Result()

    class Session:
        async def __aenter__(self):
            return Database()

        async def __aexit__(self, *_args):
            return False

    async def get_customer(_authorization, _db, *, require_aal2):
        assert require_aal2 is False
        return profile, {"sub": "customer-314"}

    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main, "get_customer", get_customer)

    response = asyncio.run(
        main.customer_recent_trades(limit=500, authorization="Bearer customer-token")
    )

    statement = captured["statement"]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "trades.customer_id = 314" in sql
    assert statement._limit_clause.value == 50
    assert response["scope"] == "customer_only"
    assert response["trades"] == [
        {
            "id": 91,
            "symbol": "BTC/USDT",
            "side": "BUY",
            "mode": "PAPER",
            "status": "PARTIALLY_FILLED",
            "filled_quantity": "0.25",
            "average_fill_price": "42000.50",
            "created_at": created_at.isoformat(),
            "updated_at": created_at.isoformat(),
        }
    ]
