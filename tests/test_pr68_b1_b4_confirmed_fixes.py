from pathlib import Path

def test_b1_customer_reconciliation_never_falls_back_to_house_broker():
    src = Path("app/execution.py").read_text(encoding="utf-8")
    assert 'customer_binance_account_missing' in src
    assert 'build_customer_binance_broker(' in src
    assert 'customer_binance,' in src
    block = src[src.index("for trade_id, broker_order_id"):src.index("for trade_id, broker_order_id") + 6000]
    assert 'if customer_binance is None:' in block
    assert 'continue' in block[block.index('if customer_binance is None:'):]


def test_b2_first_use_ledger_creation_flushes_and_handles_unique_race():
    src = Path("app/customer_funds.py").read_text(encoding="utf-8")
    block = src[src.index("async def get_or_create_ledger"):src.index("async def post_journal")]
    assert "async with db.begin_nested():" in block
    assert "await db.flush()" in block
    assert "except IntegrityError:" in block
    assert "with_for_update()" in block


def test_b3_wallet_mirror_is_decimal_not_float():
    db = Path("app/db.py").read_text(encoding="utf-8")
    funds = Path("app/customer_funds.py").read_text(encoding="utf-8")
    assert 'available_balance: Mapped[Decimal] = mapped_column(Numeric(38, 18)' in db
    assert 'locked_balance: Mapped[Decimal] = mapped_column(Numeric(38, 18)' in db
    assert 'wallet.available_balance = D(str(ledger.available))' in funds
    assert 'wallet.locked_balance = D(str(ledger.trading_reserved)) + D(str(ledger.withdrawal_reserved))' in funds
    assert 'wallet.available_balance = float(ledger.available)' not in funds


def test_b3_customer_cash_risk_gate_compares_decimal_to_decimal():
    src = Path("app/execution.py").read_text(encoding="utf-8")
    assert 'required_cash = D("0") if reducing else Decimal(str(price)) * Decimal(str(quantity))' in src
    assert 'if D(str(ledger.available)) + Decimal("0.000000001") < required_cash:' in src
    assert 'if float(ledger.available)' not in src


def test_b4_dashboard_websocket_logs_and_closes_on_unexpected_error():
    src = Path("app/main.py").read_text(encoding="utf-8")
    assert 'logging.getLogger(__name__).exception("dashboard websocket failed")' in src
    assert 'await websocket.close(code=1011)' in src
    assert 'except Exception:\n        pass' not in src[src.index("except WebSocketDisconnect:"):src.index("except WebSocketDisconnect:") + 500]
