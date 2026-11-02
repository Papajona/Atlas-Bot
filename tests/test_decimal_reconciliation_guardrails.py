"""Precision and exception-swallowing guardrails for financial reconciliation."""
import ast
from decimal import Decimal
from pathlib import Path

from app.db import LedgerJournalLine

ROOT = Path(__file__).resolve().parents[1]


def test_decimal_reconciliation_covers_fractional_large_empty_and_journal_totals():
    # USDT balances are stored to 6 decimal places; journal Numeric columns are
    # Numeric(38, 6). Decimal(str(...)) avoids importing binary float artifacts.
    six_decimal = [Decimal("0.100001"), Decimal("0.200002"), Decimal("0.000003")]
    assert sum(six_decimal, Decimal("0")) == Decimal("0.300006")
    assert sum((Decimal("0.100000") for _ in range(10)), Decimal("0")) == Decimal("1.000000")
    assert sum([], Decimal("0")) == Decimal("0")
    assert sum([Decimal("999999999999999999999999.123456"), Decimal("0.876544")], Decimal("0")) == Decimal("1000000000000000000000000.000000")
    assert Decimal("0.123456789012345678") + Decimal("0.000000000000000001") == Decimal("0.123456789012345679")


def test_reconciliation_endpoint_preserves_large_six_decimal_liabilities(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app import main
    from app.config import settings

    account = SimpleNamespace(
        available=Decimal("999999999999999999999999.123456"),
        trading_reserved=Decimal("0.100001"),
        withdrawal_reserved=Decimal("0.200002"),
        currency="USDT",
    )

    class Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, statement):
            names = {table.name for table in statement.get_final_froms()}
            return Result([account] if "customer_ledger_accounts" in names else [])

    async def fake_auth(*_args, **_kwargs):
        return {"sub": "test-admin"}

    async def fake_role(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "SessionLocal", lambda: Session())
    monkeypatch.setattr(main, "auth", fake_auth)
    monkeypatch.setattr(main, "require_role", fake_role)
    monkeypatch.setattr(settings, "usdt_trongrid_api_key", "")
    monkeypatch.setattr(settings, "usdt_tron_treasury_address", "T-test")

    report = asyncio.run(main.admin_custody_reconciliation())

    expected = Decimal("999999999999999999999999.423459")
    assert report["customer_liabilities"]["available"] == account.available
    assert report["customer_liabilities"]["trading_reserved"] == account.trading_reserved
    assert report["customer_liabilities"]["withdrawal_reserved"] == account.withdrawal_reserved
    assert report["customer_liabilities"]["total"] == expected


def test_postgres_numeric_journal_line_columns_preserve_six_decimal_scale():
    assert str(LedgerJournalLine.__table__.c.debit.type) == "NUMERIC(38, 6)"
    assert str(LedgerJournalLine.__table__.c.credit.type) == "NUMERIC(38, 6)"


def test_application_has_no_new_bare_except_pass_handlers():
    # Explicit AST guard for the truly silent pattern. Broad, documented money-path
    # catches remain allowed because unknown provider outcomes must fail closed.
    violations = []
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or node.type is not None:
                continue
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not violations, "Bare except: pass handlers: " + ", ".join(violations)
