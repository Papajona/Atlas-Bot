"""Customer equity observations and workspace preferences stay tenant-scoped."""
from __future__ import annotations

import asyncio
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

from app import main
from app.db import CustomerEquitySnapshot, CustomerWorkspacePreference


def test_equity_snapshot_writer_uses_idempotent_customer_15_minute_buckets():
    captured = {}

    class Database:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement):
            captured["statement"] = statement

        async def commit(self):
            captured["committed"] = True

    observed = datetime(2026, 11, 26, 12, 28, 45, tzinfo=timezone.utc)
    asyncio.run(
        main._record_customer_equity_snapshot(
            Database(),
            customer_id=314,
            equity=125.75,
            cash_equity=100.25,
            observed_at=observed,
        )
    )

    statement = captured["statement"]
    compiled = statement.compile(dialect=sqlite_dialect())
    assert "ON CONFLICT (customer_id, bucket_at) DO NOTHING" in str(compiled)
    assert compiled.params["customer_id"] == 314
    assert compiled.params["equity"] == 125.75
    assert compiled.params["cash_equity"] == 100.25
    assert compiled.params["bucket_at"] == datetime(
        2026, 11, 26, 12, 15, tzinfo=timezone.utc
    )
    assert captured["committed"] is True


def test_equity_snapshot_writer_does_not_persist_non_finite_values():
    captured = {}

    class Database:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement):
            captured["statement"] = statement

        async def commit(self):
            captured["committed"] = True

    asyncio.run(
        main._record_customer_equity_snapshot(
            Database(),
            customer_id=314,
            equity=float("nan"),
            cash_equity=100.0,
            observed_at=datetime(2026, 11, 26, tzinfo=timezone.utc),
        )
    )
    assert captured == {}


def test_customer_equity_history_is_customer_scoped_bounded_and_chronological(monkeypatch):
    profile = SimpleNamespace(id=314)
    newest = SimpleNamespace(
        id=2,
        customer_id=314,
        bucket_at=datetime(2026, 11, 26, 12, 15, tzinfo=timezone.utc),
        observed_at=datetime(2026, 11, 26, 12, 28, tzinfo=timezone.utc),
        equity=130.0,
        cash_equity=105.0,
        currency="USDT",
        valuation_source="TRADING_ACCOUNT_EQUITY",
    )
    oldest = SimpleNamespace(
        id=1,
        customer_id=314,
        bucket_at=datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc),
        observed_at=datetime(2026, 11, 26, 12, 5, tzinfo=timezone.utc),
        equity=125.0,
        cash_equity=100.0,
        currency="USDT",
        valuation_source="TRADING_ACCOUNT_EQUITY",
    )
    captured = {}

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [newest, oldest]

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
        return profile, {"sub": "tenant-314"}

    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main, "get_customer", get_customer)

    response = asyncio.run(
        main.customer_portfolio_equity_history(
            days=7, authorization="Bearer tenant-314-token"
        )
    )

    statement = captured["statement"]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "customer_equity_snapshots.customer_id = 314" in sql
    assert statement._limit_clause.value == 3000
    assert response["scope"] == "customer_only"
    assert response["history_available"] is True
    assert response["history_complete"] is False
    assert [point["equity"] for point in response["points"]] == [125.0, 130.0]
    assert response["points"][0]["valuation_source"] == "TRADING_ACCOUNT_EQUITY"


def test_equity_history_rejects_unbounded_or_unsupported_windows():
    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(main.customer_portfolio_equity_history(days=365))
    assert exc.value.status_code == 422


def test_workspace_preference_payload_has_safe_defaults():
    assert main._workspace_preferences_payload(None) == {
        "reduce_motion": False,
        "compact_markets": False,
        "touch_feedback": True,
    }


def test_workspace_preference_patch_uses_atomic_customer_scoped_upsert(monkeypatch):
    profile = SimpleNamespace(id=314)
    captured = {}

    class Database:
        def __init__(self):
            self.row = None

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement):
            captured["statement"] = statement
            compiled = statement.compile(dialect=sqlite_dialect())
            captured["sql"] = str(compiled)
            captured["params"] = compiled.params
            self.row = CustomerWorkspacePreference(
                customer_id=profile.id,
                reduce_motion=True,
                compact_markets=False,
                touch_feedback=False,
            )

        async def get(self, model, customer_id):
            assert model is CustomerWorkspacePreference
            captured["customer_id"] = customer_id
            return self.row

        async def commit(self):
            captured["committed"] = True

        async def refresh(self, row):
            captured["refreshed_customer_id"] = row.customer_id

    database = Database()

    class Session:
        async def __aenter__(self):
            return database

        async def __aexit__(self, *_args):
            return False

    async def get_customer(_authorization, _db, *, require_aal2):
        assert require_aal2 is False
        return profile, {"sub": "tenant-314"}

    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main, "get_customer", get_customer)

    response = asyncio.run(
        main.customer_update_workspace_preferences(
            {"reduce_motion": True, "touch_feedback": False},
            authorization="Bearer tenant-314-token",
        )
    )

    assert "ON CONFLICT (customer_id) DO UPDATE" in captured["sql"]
    assert captured["params"]["customer_id"] == 314
    assert captured["customer_id"] == captured["refreshed_customer_id"] == 314
    assert captured["committed"] is True
    assert response["scope"] == "customer_only"
    assert response["preferences"] == {
        "reduce_motion": True,
        "compact_markets": False,
        "touch_feedback": False,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"live_enabled": True},
        {"reduce_motion": "yes"},
        {"compact_markets": 1},
        {},
    ],
)
def test_workspace_preferences_reject_non_display_or_non_boolean_updates(payload):
    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(main.customer_update_workspace_preferences(payload))
    assert exc.value.status_code == 422

def test_runtime_provisioning_requires_the_0069_schema_head():
    script = Path("scripts/provision_atlas_api_runtime.sql").read_text(encoding="utf-8")
    revision = "0069_customer_equity_history_and_workspace_preferences"
    assert f"WHERE version_num = '{revision}'" in script
    assert f"database must be at {revision} before API role provisioning" in script
    assert "database must be at 0068_tron_sweep_txid_casefold_unique" not in script
