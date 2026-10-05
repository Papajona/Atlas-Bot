"""Behavioural tests for the TRON deposit monitor: no re-verification of already-credited deposits,
bounded parallel wallet scans, and request pacing. TronGrid is replaced by an in-process mock transport."""
import asyncio
import time
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

import app.main as main
from app.config import settings
from app.db import (CustomerLedgerAccount, CustomerProfile, FundingTransaction, SessionLocal, Wallet, init_db)


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")
    monkeypatch.setattr(settings, "usdt_trongrid_api_key", "test-key")
    monkeypatch.setattr(settings, "usdt_tron_verify_receipt", True)
    monkeypatch.setattr(settings, "usdt_tron_min_confirmations", 19)
    monkeypatch.setattr(settings, "usdt_tron_scan_concurrency", 3)
    monkeypatch.setattr(settings, "usdt_tron_max_qps", 500.0)   # pacing itself is covered by its own test
    monkeypatch.setattr(settings, "redis_url", "")


def test_rate_gate_spaces_concurrent_requests():
    async def go():
        gate = main._RateGate(qps=50)            # 20 ms apart
        stamps = []

        async def one():
            await gate.wait()
            stamps.append(time.monotonic())
        await asyncio.gather(*[one() for _ in range(8)])
        gaps = [b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:])]
        assert min(gaps) >= 0.020 * 0.8, gaps     # 20% tolerance for timer jitter
    asyncio.run(go())


def test_scan_skips_reverification_of_credited_deposits_and_runs_wallets_in_parallel(monkeypatch):
    N = 6
    real_sleep = asyncio.sleep
    calls = {"trc20": 0, "receipt": 0, "nowblock": 0}
    inflight = {"now": 0, "max": 0}
    contract = settings.usdt_tron_usdt_contract
    addr_to_tx: dict[str, dict] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        try:
            await real_sleep(0.03)               # make overlap observable
            if path.endswith("/transactions/trc20"):
                calls["trc20"] += 1
                addr = path.split("/accounts/")[1].split("/")[0]
                return httpx.Response(200, json={"data": [addr_to_tx[addr]], "meta": {}})
            if path.endswith("/gettransactioninfobyid"):
                calls["receipt"] += 1
                return httpx.Response(200, json={"receipt": {"result": "SUCCESS"}, "blockNumber": 100})
            if path.endswith("/getnowblock"):
                calls["nowblock"] += 1
                return httpx.Response(200, json={"block_header": {"raw_data": {"number": 500}}})
            return httpx.Response(404)
        finally:
            inflight["now"] -= 1

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    cycles = {"n": 0}

    async def fake_sleep(d, *a, **k):
        if d >= 15:                               # the monitor's end-of-cycle sleep
            cycles["n"] += 1
            if cycles["n"] == 1:                  # simulate the overlap window: next scan sees the same transfers again
                async with SessionLocal() as db:
                    from app.db import TronDepositCursor
                    for c in (await db.execute(select(TronDepositCursor))).scalars().all():
                        c.last_block_timestamp = 0
                    await db.commit()
                return
            raise asyncio.CancelledError()
        await real_sleep(d)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def go():
        await init_db()
        wallet_ids, customer_ids = [], []
        async with SessionLocal() as db:
            for i in range(N):
                prof = CustomerProfile(auth_user_id=f"tronscan-{uuid.uuid4().hex}")
                db.add(prof)
                await db.flush()
                addr = "T" + uuid.uuid4().hex[:33]
                w = Wallet(customer_id=prof.id, currency="USDT", wallet_type="TRADING", network="TRON",
                           deposit_address=addr, status="ACTIVE")
                db.add(w)
                await db.flush()
                wallet_ids.append(w.id)
                customer_ids.append(prof.id)
                addr_to_tx[addr] = {
                    "transaction_id": uuid.uuid4().hex, "type": "Transfer", "success": True, "to": addr,
                    "from": "TSender" + uuid.uuid4().hex[:27], "value": "25000000",
                    "block_timestamp": int(time.time() * 1000) - 600_000,
                    "token_info": {"address": contract, "decimals": 6}}
            await db.commit()
        try:
            await main._usdt_tron_monitor_loop()
        except asyncio.CancelledError:
            pass
        async with SessionLocal() as db:
            n_funding = (await db.execute(select(func.count()).select_from(FundingTransaction).where(
                FundingTransaction.wallet_id.in_(wallet_ids), FundingTransaction.provider == "tron-usdt"))).scalar_one()
            balances = [Decimal(str((await db.execute(select(CustomerLedgerAccount.available).where(
                CustomerLedgerAccount.customer_id == cid))).scalar_one())) for cid in customer_ids]
        return n_funding, balances

    n_funding, balances = asyncio.run(go())
    assert cycles["n"] == 2, "expected exactly two full scan cycles"
    assert n_funding == N and all(b == Decimal("25") for b in balances), (n_funding, balances)   # credited once, not twice
    # Two cycles x N wallets of history fetches, but each deposit verified only in the first cycle.
    assert calls["trc20"] >= 2 * N
    assert calls["receipt"] == N and calls["nowblock"] == N, calls
    assert 1 < inflight["max"] <= 3, inflight   # parallel, but never above the configured bound
