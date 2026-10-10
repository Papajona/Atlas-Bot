"""Regression tests for the TRON sweep sequencing, JWT claim requirements and JWKS refresh cooldown."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.main as main
from app.config import settings
from app.db import Base, TronSweep, Wallet


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "{}")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


# ---- TRON sweeps ---------------------------------------------------------------------------------

def _prepare_flow(steps):
    """steps: list of callables(prepare, sessions) executed in order; returns their results."""
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        out = []
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                db.add(Wallet(customer_id=1, currency="USDT", network="TRON", deposit_address="T" + "B" * 33, status="ACTIVE"))
                await db.commit()
            old = (main.SessionLocal, main.auth, main.require_role)
            main.SessionLocal = sessions
            main.auth = AsyncMock(return_value={"sub": "treasurer"})
            main.require_role = AsyncMock(return_value=True)

            async def prepare(amount):
                return await main.admin_prepare_tron_sweep({"wallet_id": 1, "amount_usdt": amount}, authorization=None, x_admin_token="x")
            try:
                for step in steps:
                    out.append(await step(prepare, sessions))
            finally:
                main.SessionLocal, main.auth, main.require_role = old
        finally:
            await engine.dispose()
        return out
    return asyncio.run(run())


async def _settle_all(sessions):
    async with sessions() as db:
        for sw in (await db.execute(select(TronSweep))).scalars():
            sw.status = "SETTLED"
        await db.commit()


def test_same_amount_sweep_after_settlement_creates_a_new_sweep():
    async def first(prepare, sessions): return await prepare(100)
    async def settle(prepare, sessions): await _settle_all(sessions)
    async def second(prepare, sessions): return await prepare(100)
    r1, _, r2 = _prepare_flow([first, settle, second])
    assert r1["status"] == "READY_FOR_SIGNER" and r2["status"] == "READY_FOR_SIGNER"
    assert r2["sweep_id"] != r1["sweep_id"], "a later same-amount sweep must not silently return the old one"
    assert "intent" in r2 and r2["intent"]["idempotency_key"] != r1["intent"]["idempotency_key"]


def test_retry_of_in_flight_sweep_is_idempotent_and_returns_intent():
    async def first(prepare, sessions): return await prepare(100)
    async def retry(prepare, sessions): return await prepare(100)
    r1, r2 = _prepare_flow([first, retry])
    assert r2["sweep_id"] == r1["sweep_id"] and r2["idempotency_key"] == r1["intent"]["idempotency_key"] == r2["intent"]["idempotency_key"]
    assert r2["intent"]["raw_amount"] == r1["intent"]["raw_amount"] == str(100 * 10**6)


def test_different_amount_while_a_sweep_is_in_flight_is_rejected():
    async def first(prepare, sessions): return await prepare(100)
    async def other(prepare, sessions):
        with pytest.raises(HTTPException) as exc:
            await prepare(50)
        return exc.value.status_code
    _, code = _prepare_flow([first, other])
    assert code == 409


# ---- JWT / JWKS ----------------------------------------------------------------------------------

def test_jwt_decode_requires_exp_and_sub():
    src = open(main.__file__, encoding="utf-8").read()
    block = src[src.index("async def _supabase_claims"):src.index("async def _customer_mfa_state")]
    assert 'options={"require": ["exp", "sub"]}' in block


def test_jwks_forced_refresh_is_rate_limited(monkeypatch):
    calls = {"n": 0}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"keys": [{"kid": "k1", "kty": "RSA"}]}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            calls["n"] += 1
            return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(main, "_customer_jwks_cache", {"expires": 0.0, "keys": {}})

    async def run():
        await main._load_supabase_jwks(False)          # initial fetch
        for _ in range(20):                             # 20 tokens with an unknown kid
            await main._load_supabase_jwks(True)
        first = calls["n"]
        main._customer_jwks_cache["fetched_at"] = time.time() - 31   # cooldown elapsed: one more refresh is allowed
        await main._load_supabase_jwks(True)
        return first, calls["n"]

    first, after = asyncio.run(run())
    assert first == 1, f"forced refreshes inside the cooldown must not hit the upstream (got {first})"
    assert after == 2
