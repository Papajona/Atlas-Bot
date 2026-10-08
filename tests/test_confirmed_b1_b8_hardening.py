from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_b1_customer_reconciliation_refuses_missing_binance_account():
    src = (ROOT / "app" / "execution.py").read_text()
    assert 'customer_binance_account_missing' in src
    assert 'trade_is_customer = False' not in src


def test_b3_customer_wallet_projection_stays_decimal():
    db = (ROOT / "app" / "db.py").read_text()
    funds = (ROOT / "app" / "customer_funds.py").read_text()
    assert "class CustodialNumeric" in db
    assert 'available_balance: Mapped[Decimal] = mapped_column(CustodialNumeric' in db
    assert 'locked_balance: Mapped[Decimal] = mapped_column(CustodialNumeric' in db
    assert "wallet.available_balance = float(ledger.available)" not in funds
    assert "wallet.locked_balance = float(ledger.trading_reserved + ledger.withdrawal_reserved)" not in funds


def test_b4_risk_websocket_failure_is_logged_and_closed():
    src = (ROOT / "app" / "main.py").read_text()
    block = src[src.index('@app.websocket("/ws/risk-telemetry")'):src.index('@app.get("/api/trades")')]
    assert 'logger.exception("risk dashboard websocket failed")' in block
    assert 'await websocket.close(code=1011)' in block


def test_b5_otp_provider_error_payload_is_not_reported_as_success():
    src = (ROOT / "app" / "main.py").read_text()
    block = src[src.index('@app.post("/api/auth/otp/send")'):src.index('@app.post("/api/auth/otp/verify")')]
    assert 'provider_error = result.get("error") or result.get("error_code")' in block
    assert 'Verification code provider rejected the request' in block


def test_b7_android_sources_are_excluded_from_backend_docker_context():
    dockerignore = (ROOT / ".dockerignore").read_text()
    for entry in ("app/src/", "app/build.gradle.kts", "app/proguard-rules.pro"):
        assert entry in dockerignore


def test_b8_confirmed_unused_asdict_imports_are_removed():
    for name in ("fx_tca.py", "fx_engine.py", "executor_engine.py", "fx_execution_router.py"):
        src = (ROOT / "app" / name).read_text()
        assert "from dataclasses import dataclass, asdict" not in src
