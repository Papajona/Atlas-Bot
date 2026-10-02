"""3.10.46: fill-time ledger settlement must not abort after a broker fill, and model load must verify the bytes it loads.

Style matches tests/test_review_runtime_regressions.py (real async SQLite). NOTE: authored in a container without
sqlalchemy/aiosqlite, so run `pytest tests/test_ledger_deficit_and_model_load_3_10_46.py` in CI before relying on it.
"""
import asyncio
import hashlib
import json
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.customer_funds import DEFICIT_ACCOUNT, settle_realized_pnl, settle_trading_fee
from app.config import settings
from app.db import Base, CustomerLedgerAccount, Incident, LedgerJournal, LedgerJournalLine


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


def _balanced(lines):
    return sum(l.debit for l in lines) == sum(l.credit for l in lines) > 0


def test_loss_beyond_available_nonstrict_books_deficit_never_negative():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("5")))
            await db.commit()
            await settle_realized_pnl(db, customer_id=1, amount=-8, reference_id="close:1", strict=False)
            await db.commit()
        async with sessions() as db:
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            lines = (await db.execute(select(LedgerJournalLine))).scalars().all()
            assert ledger.available == Decimal("0")
            assert _balanced(lines)
            assert sum(l.debit for l in lines if l.account_code == DEFICIT_ACCOUNT) == Decimal("3")
            assert (await db.execute(select(Incident))).scalars().all()
    asyncio.run(with_database(check))


def test_loss_beyond_available_strict_still_raises():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("5")))
            await db.commit()
            with pytest.raises(ValueError, match="cannot absorb"):
                await settle_realized_pnl(db, customer_id=1, amount=-8, reference_id="close:2")
    asyncio.run(with_database(check))


def test_fee_beyond_balance_nonstrict_drains_buckets_and_books_deficit_once():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("1"), trading_reserved=Decimal("1")))
            await db.commit()
            for _ in range(2):  # retry must be a no-op
                await settle_trading_fee(db, customer_id=1, fee=5, reference_id="fill:9", strict=False)
                await db.commit()
        async with sessions() as db:
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            lines = (await db.execute(select(LedgerJournalLine))).scalars().all()
            assert (ledger.available, ledger.trading_reserved) == (Decimal("0"), Decimal("0"))
            assert _balanced(lines)
            assert sum(l.debit for l in lines if l.account_code == DEFICIT_ACCOUNT) == Decimal("3")
            assert len((await db.execute(select(LedgerJournal))).scalars().all()) == 1
    asyncio.run(with_database(check))


def test_load_model_verifies_and_loads_the_same_bytes(tmp_path, monkeypatch):
    import joblib
    from app import trading_core
    model_file = tmp_path / "m.joblib"
    joblib.dump({"ok": True}, model_file)
    meta = {"model_sha256": hashlib.sha256(model_file.read_bytes()).hexdigest()}
    meta_file = tmp_path / "m.joblib.meta.json"
    meta_file.write_text(json.dumps(meta))
    monkeypatch.setattr(trading_core, "_model_paths", lambda _p: (model_file, meta_file))
    monkeypatch.setattr(settings, "environment", "development")
    loaded, _ = trading_core.load_model(str(model_file))
    assert loaded == {"ok": True}
    # Tampered artifact is rejected before any deserialization happens.
    model_file.write_bytes(model_file.read_bytes() + b"x")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        trading_core.load_model(str(model_file))
