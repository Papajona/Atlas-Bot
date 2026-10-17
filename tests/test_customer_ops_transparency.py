from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app" / "main.py").read_text()


def test_customer_csv_export_uses_authoritative_customer_ledger_statement():
    start = MAIN.index('@app.get("/api/customer/ledger/export.csv")')
    end = MAIN.index('@app.get("/api/customer/trades/{trade_id}/decision")', start)
    route = MAIN[start:end]
    assert "get_customer(authorization, db)" in route
    assert "ledger_statement(db, profile.id, \"USDT\", 5000)" in route
    assert 'media_type="text/csv; charset=utf-8"' in route
    assert "Cache-Control" in route


def test_customer_trade_decision_endpoint_is_owner_scoped_and_honest_about_scope():
    start = MAIN.index('@app.get("/api/customer/trades/{trade_id}/decision")')
    end = MAIN.index('@app.get("/api/admin/funding")', start)
    route = MAIN[start:end]
    assert "Trade.customer_id == profile.id" in route
    assert 'HTTPException(404, "Trade not found")' in route
    assert '"reason": trade.reason or ""' in route
    assert '"error": trade.error or ""' in route
    assert '"explanation_scope": "stored_trade_record"' in route
    assert "does not reconstruct decisions not recorded" in route


def test_customer_exports_do_not_query_wallet_balance_or_mutate_ledger():
    start = MAIN.index('@app.get("/api/customer/ledger/export.csv")')
    end = MAIN.index('@app.get("/api/customer/trades/{trade_id}/decision")', start)
    route = MAIN[start:end]
    assert "ledger_statement" in route
    assert "customer_balance(" not in route
    assert ".add(" not in route
    assert ".commit(" not in route
