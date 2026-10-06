"""Regression tests for the nine money-safety fixes layered on 3.10.45."""
import asyncio
import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.customer_funds import post_deposit, reserve_trading, reserve_withdrawal, release_trading, settle_withdrawal, customer_balance
from app.db import Base, CustomerLedgerAccount, Incident, LedgerEntry


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


async def with_database(check):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await check(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def test_customer_ledger_money_fields_are_decimal_typed():
    from sqlalchemy.orm import Mapped

    annotations = CustomerLedgerAccount.__annotations__
    for field in ("available", "trading_reserved", "withdrawal_reserved"):
        assert annotations[field] == Mapped[Decimal]


def test_release_trading_preserves_decimal_ledger_precision():
    async def check(sessions):
        async with sessions() as db:
            db.add(
                CustomerLedgerAccount(
                    customer_id=1,
                    available=Decimal("1.000000"),
                    trading_reserved=Decimal("50.123456"),
                )
            )
            await db.commit()

            await release_trading(
                db,
                customer_id=1,
                amount=50.123456,
                reference_id="release:decimal-precision",
            )
            await db.commit()

            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.available == Decimal("51.123456")
            assert ledger.trading_reserved == Decimal("0.000000")

    asyncio.run(with_database(check))


def test_reserve_rounds_up_and_amounts_are_ledger_precision():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("10")))
            await db.commit()
            await reserve_trading(db, 1, 0.0000001, reference_id="r:tiny")   # 1e-7 must not become 0 or round down
            await reserve_trading(db, 1, 1.2345671, reference_id="r:frac")
            await db.commit()
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.trading_reserved == Decimal("0.000001") + Decimal("1.234568")
            assert ledger.available == Decimal("10") - ledger.trading_reserved
    asyncio.run(with_database(check))


def test_withdrawal_settlement_shortfall_opens_critical_incident():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("0"), withdrawal_reserved=Decimal("4")))
            await db.commit()
            await settle_withdrawal(db, 1, 10, reference_id="w:1")
            await db.commit()
        async with sessions() as db:
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.withdrawal_reserved == Decimal("0")
            inc = (await db.execute(select(Incident))).scalar_one()
            assert inc.severity == "CRITICAL" and inc.incident_key == "WITHDRAWAL_RESERVE_SHORTFALL:w:1"
            assert "6" in inc.detail_json  # shortfall recorded
    asyncio.run(with_database(check))


def test_deposit_idempotency_is_provider_namespaced_but_legacy_safe():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1))
            await db.commit()
            await post_deposit(db, customer_id=1, wallet_id=1, amount=5, provider_reference="ref-1", metadata={}, provider="stripe")
            await post_deposit(db, customer_id=1, wallet_id=1, amount=5, provider_reference="ref-1", metadata={}, provider="stripe")   # replay
            await post_deposit(db, customer_id=1, wallet_id=1, amount=7, provider_reference="ref-1", metadata={}, provider="wise")     # other provider, same ref
            await db.commit()
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.available == Decimal("12")
            # a deposit posted under the legacy key is still recognised
            db.add(LedgerEntry(customer_id=1, currency="USDT", entry_type="DEPOSIT_CREDIT", debit=0, credit=3, amount=3,
                               reference_type="TRON_TX", reference_id="old-ref", idempotency_key="deposit:old-ref"))
            await db.commit()
            await post_deposit(db, customer_id=1, wallet_id=1, amount=3, provider_reference="old-ref", metadata={}, provider="tron-usdt")
            await db.commit()
            assert (await db.execute(select(CustomerLedgerAccount))).scalar_one().available == Decimal("12")
    asyncio.run(with_database(check))


@pytest.mark.parametrize("model", ["CustomerWithdrawalCreate"])
def test_withdrawal_amount_rejects_more_than_six_decimals(model):
    import app.main as main
    cls = getattr(main, model)
    kwargs = {"destination": "T" + "A" * 33}
    assert cls(amount=1.234567, **kwargs).amount == 1.234567
    for bad in (0.0000001, 1.2345678):
        with pytest.raises(ValidationError):
            cls(amount=bad, **kwargs)


def test_admin_withdrawal_create_also_rejects_excess_precision():
    import app.main as main
    base = dict(request_id="req-123", account_ref="1", currency="USDT", destination_masked="TAAAAA...AAAAAA")
    main.WithdrawalCreate(amount=2.5, **base)
    with pytest.raises(ValidationError):
        main.WithdrawalCreate(amount=2.1234567, **base)


def test_stuck_withdrawal_alerts_are_throttled():
    import app.main as main
    main._recovery_last_alert.clear()
    assert main._should_alert_recovery(7, now=1000.0, interval=900.0) is True
    assert main._should_alert_recovery(7, now=1500.0, interval=900.0) is False
    assert main._should_alert_recovery(8, now=1500.0, interval=900.0) is True   # independent per withdrawal
    assert main._should_alert_recovery(7, now=1901.0, interval=900.0) is True


def test_tron_cursor_never_skips_a_deferred_deposit():
    import app.main as main
    assert main._next_tron_cursor(50, 200, None) == 200
    assert main._next_tron_cursor(50, 200, 120) == 119          # deferred deposit at ts 120 stays inside next window
    assert main._next_tron_cursor(500, 200, 120) == 500         # never moves backwards
    assert main._next_tron_cursor(0, 0, None) == 0


def test_tron_identical_transfers_in_one_tx_get_distinct_references():
    import app.main as main
    seen = {}
    kw = dict(txid="a" * 64, sender="S" * 34, address="D" * 34, raw_value=5_000_000, block_ts=1_700_000_000_000)
    first = main._tron_deposit_reference(seen, **kw)
    second = main._tron_deposit_reference(seen, **kw)
    assert first == f"tron-usdt:{kw['txid']}:{kw['sender']}:{kw['address']}:5000000:1700000000000"   # historical format preserved
    assert second == first + "#1" and second != first
    assert len(second) <= 180                                    # fits provider_reference column


def test_tron_scanner_marks_deferrals_before_advancing_cursor():
    src = open("app/main.py").read()
    scan = src[src.index("async def _usdt_tron_monitor_loop"):]
    scan = scan[:scan.index("\nasync def ", 10)] if "\nasync def " in scan[10:] else scan
    assert scan.count("_defer(block_ts)") == 3                   # receipt, block number, confirmations
    assert scan.index("_defer(block_ts)") < scan.index("highest_ts = max(highest_ts, block_ts)")
    assert "_next_tron_cursor(" in scan


def test_confirming_a_pending_funding_credits_the_stored_amount_not_the_payload():
    from fastapi.testclient import TestClient
    import app.main as main
    from app.db import init_db, SessionLocal, CustomerProfile, Wallet, FundingTransaction

    secret = "test-funding-secret"
    settings.funding_webhook_secret = secret
    uid = "auth-" + uuid.uuid4().hex
    ref = "ref-" + uuid.uuid4().hex

    async def seed():
        await init_db()
        async with SessionLocal() as db:
            c = CustomerProfile(auth_user_id=uid)
            db.add(c); await db.flush()
            w = Wallet(customer_id=c.id, currency="USDT", wallet_type="INTERNAL_TRADING", status="PENDING")
            db.add(w); await db.flush()
            db.add(FundingTransaction(customer_id=c.id, wallet_id=w.id, provider="wire", provider_reference=ref,
                                      amount=10.0, currency="USDT", status="PENDING"))
            await db.commit()
            return c.id

    cid = asyncio.run(seed())
    body = json.dumps({"customer_auth_user_id": uid, "provider": "wire", "provider_reference": ref,
                       "amount": 1000.0, "currency": "USDT", "status": "CONFIRMED"}).encode()
    timestamp = str(int(time.time()))
    signed = timestamp.encode() + b"." + body
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    with TestClient(main.app) as client:
        r = client.post("/api/internal/funding/webhook", content=body, headers={"x-funding-signature": sig, "x-funding-timestamp": timestamp})
    assert r.status_code == 200, r.text

    async def balance():
        async with SessionLocal() as db:
            return (await db.execute(select(CustomerLedgerAccount).where(CustomerLedgerAccount.customer_id == cid))).scalar_one().available
    assert asyncio.run(balance()) == Decimal("10")


def test_position_flip_reserve_failure_does_not_abort_the_fill(monkeypatch):
    import app.execution as ex
    from app.db import Trade, Position

    opened = []

    async def fake_open_incident(**kw):
        opened.append(kw)

    monkeypatch.setattr(ex, "open_incident", fake_open_incident)

    async def check(sessions):
        async with sessions() as db:
            # long 1 @100 with 100 reserved; customer has nothing else available
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("0"), trading_reserved=Decimal("100")))
            trade = Trade(customer_id=1, exchange="binance", symbol="BTC/USDT", side="sell", timeframe="1h",
                          mode="LIVE", quantity=3.0, requested_quantity=3.0, filled_quantity=0.0, remaining_quantity=3.0,
                          requested_price=100.0, client_order_id="c-flip", signal_id="s-flip", status="FILLED")
            db.add(trade)
            await db.flush()
            db.add(Position(customer_id=1, exchange="binance", symbol="BTC/USDT", quantity=1.0,
                            average_entry_price=100.0, mark_price=100.0, reserved_capital=100.0,
                            entry_trade_id=trade.id))
            # Sell 3: closes the long (frees 100) then flips short 2 @100 which needs 200 -> deficit of 100.
            await ex._apply_fill_to_position(db, trade, 3.0, 100.0)   # must not raise
            await db.commit()
            pos = (await db.execute(select(Position))).scalar_one()
            assert pos.quantity == -2.0                                 # the broker fill is still recorded
            assert "FLIP_RESERVE_DEFICIT" in (trade.error or "")
            assert pos.reserved_capital == 0.0                          # capital not booked without a reserve
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.trading_reserved == Decimal("0") and ledger.available == Decimal("100")
        assert opened and opened[0]["key"].startswith("POSITION_FLIP_RESERVE_DEFICIT:")
    asyncio.run(with_database(check))


def test_reserve_idempotency_rejects_different_amount():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=77, available=Decimal("100")))
            await db.commit()
            await reserve_withdrawal(db, 77, 25, reference_id="same-ref")
            await db.commit()
            with pytest.raises(ValueError, match="different reserve amount"):
                await reserve_withdrawal(db, 77, 30, reference_id="same-ref")
            # The endpoint/service layer must roll back an expected reserve conflict.
            await db.rollback()
            ledger = (await db.execute(select(CustomerLedgerAccount).where(
                CustomerLedgerAccount.customer_id == 77
            ))).scalar_one()
            assert ledger.available == Decimal("75")
            assert ledger.withdrawal_reserved == Decimal("25")
    asyncio.run(with_database(check))


def test_reserve_idempotency_rejects_different_customer():
    async def check(sessions):
        async with sessions() as db:
            db.add_all([
                CustomerLedgerAccount(customer_id=79, available=Decimal("100")),
                CustomerLedgerAccount(customer_id=80, available=Decimal("100")),
            ])
            await db.commit()
            await reserve_withdrawal(db, 79, 25, reference_id="shared-withdrawal-ref")
            await db.commit()
            with pytest.raises(ValueError, match="different customer"):
                await reserve_withdrawal(db, 80, 25, reference_id="shared-withdrawal-ref")
            await db.rollback()
            rows = (await db.execute(select(CustomerLedgerAccount).where(
                CustomerLedgerAccount.customer_id.in_([79, 80])
            ).order_by(CustomerLedgerAccount.customer_id))).scalars().all()
            assert rows[0].withdrawal_reserved == Decimal("25")
            assert rows[1].withdrawal_reserved == Decimal("0")
    asyncio.run(with_database(check))


def test_customer_balance_does_not_refresh_away_session_local_changes():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=78, available=Decimal("100")))
            await db.commit()
            ledger = (await db.execute(select(CustomerLedgerAccount).where(
                CustomerLedgerAccount.customer_id == 78
            ))).scalar_one()
            ledger.available = Decimal("90")
            balance = await customer_balance(db, 78, "USDT")
            assert balance["available"] == 90.0
    asyncio.run(with_database(check))


def test_customer_cash_lock_order_is_ledger_then_account():
    from pathlib import Path
    src = Path("app/execution.py").read_text()
    block = src[src.index("async def risk_gate"):src.index("\nasync def ", src.index("async def risk_gate") + 20)]
    assert block.index('ledger = await get_or_create_ledger(db, customer_id, "USDT")') < block.index(
        "select(TradingAccount).where(TradingAccount.customer_id == customer_id).with_for_update()"
    )


def test_customer_balance_lock_free_path_does_not_force_refresh():
    import inspect
    from app import customer_funds
    src = inspect.getsource(customer_funds.customer_balance)
    assert "lock: bool = False" in src
    assert "with_for_update" not in src.split("if ledger is None:", 1)[0]
    assert "populate_existing=True" not in src


def test_locked_ledger_path_may_refresh_after_lock():
    import inspect
    from app import customer_funds
    src = inspect.getsource(customer_funds.get_or_create_ledger)
    assert src.count("populate_existing=True") == 2


def test_customer_withdrawal_locks_ledger_before_wallet_and_account():
    from app import main
    import inspect
    block = inspect.getsource(main.customer_create_withdrawal)
    assert block.index('await get_or_create_ledger(db, profile.id, "USDT")') < block.index(
        'select(Wallet).where('
    )
    assert block.index('await get_or_create_ledger(db, profile.id, "USDT")') < block.index(
        'select(TradingAccount).where('
    )


def test_stepup_is_consumed_only_after_destination_gate():
    from app import main
    import inspect
    block = inspect.getsource(main.customer_create_withdrawal)
    assert block.index('raise HTTPException(409, f"Withdrawal destination is not yet trusted') < block.index(
        'stepup.used_at = datetime.now(timezone.utc)'
    )
    assert block.count('stepup.used_at = datetime.now(timezone.utc)') == 1
