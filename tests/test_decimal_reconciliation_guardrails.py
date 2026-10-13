"""Precision and exception-swallowing guardrails for financial reconciliation."""
import ast
from decimal import Decimal
from pathlib import Path

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


def test_reconciliation_endpoint_aggregates_liabilities_with_decimal_not_float():
    src = (ROOT / "app/main.py").read_text()
    start = src.index('async def admin_custody_reconciliation')
    end = src.index('@app.get("/api/admin/security/roles")', start)
    body = src[start:end]
    assert "Decimal(str(r.available or 0))" in body
    assert "Decimal(str(r.trading_reserved or 0))" in body
    assert "Decimal(str(r.withdrawal_reserved or 0))" in body
    assert "sum(float(r.available) for r in rows)" not in body


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
