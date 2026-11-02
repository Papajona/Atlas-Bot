"""Regression tests for trading-account/customer referential integrity."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import TradingAccount

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "0055_trading_account_customer_fk.py"
_SPEC = importlib.util.spec_from_file_location("migration_0055_trading_account_customer_fk", MIGRATION_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_migration)


def test_trading_account_model_restricts_customer_profile_deletion():
    foreign_keys = list(TradingAccount.__table__.c.customer_id.foreign_keys)
    assert len(foreign_keys) == 1
    assert foreign_keys[0].target_fullname == "customer_profiles.id"
    assert foreign_keys[0].ondelete == "RESTRICT"
    assert foreign_keys[0].constraint.name == "fk_trading_accounts_customer_profile"


def test_migration_fails_closed_when_legacy_account_rows_are_orphaned(monkeypatch):
    executed = []

    class ScalarResult:
        def scalar_one(self):
            return 2

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def execute(self, statement, *_args, **_kwargs):
            executed.append(str(statement))
            return ScalarResult()

    monkeypatch.setattr(_migration.op, "get_bind", lambda: Connection())
    monkeypatch.setattr(_migration.op, "execute", lambda statement: executed.append(str(statement)))

    with pytest.raises(RuntimeError, match="2 trading_accounts rows"):
        _migration.upgrade()

    assert any("SET LOCAL lock_timeout" in statement for statement in executed)
    assert not any("ADD CONSTRAINT" in statement for statement in executed)


def test_migration_adds_validated_restrict_foreign_key_after_orphan_check(monkeypatch):
    executed = []

    class QueryResult:
        def scalar_one(self):
            return 0

        def scalar_one_or_none(self):
            return None

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def execute(self, statement, *_args, **_kwargs):
            executed.append(str(statement))
            return QueryResult()

    monkeypatch.setattr(_migration.op, "get_bind", lambda: Connection())
    monkeypatch.setattr(
        _migration.op, "execute",
        lambda statement: executed.append(str(statement)),
    )

    _migration.upgrade()

    statements = "\n".join(executed)
    assert "SET LOCAL lock_timeout = '5s'" in statements
    assert "LEFT JOIN public.customer_profiles AS profile" in statements
    assert "WHERE profile.id IS NULL" in statements
    assert "ADD CONSTRAINT fk_trading_accounts_customer_profile" in statements
    assert "NOT VALID" in statements
    assert "VALIDATE CONSTRAINT fk_trading_accounts_customer_profile" in statements
    assert "ON DELETE RESTRICT" in statements
    assert statements.index("NOT VALID") < statements.index("VALIDATE CONSTRAINT")
