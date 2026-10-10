"""Database-backed regression tests for the guarded withdrawal not-sent endpoint."""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.main as main
from app.config import settings
from app.db import Base, CustomerLedgerAccount, Withdrawal
from app.payout import PayoutNotFound, PayoutResult, PayoutError, ExternalSignerPayoutProvider
from app.payout import PayoutUnknown


@pytest.fixture(autouse=True)
def _test_encryption_key(monkeypatch):
    # Withdrawal.destination is encrypted by the ORM; provide an isolated test key
    # so these DB-backed tests exercise the endpoint rather than fail during INSERT.
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "{}")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


def _db():
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


async def _call_endpoint(main_module, sessions, withdrawal, *, admin_id="approver-1", operator_id="operator-1", provider=None, monkeypatch=None):
    async def fake_auth(*args, **kwargs):
        return {"sub": operator_id, "role": "TREASURY"}

    async def fake_role(*args, **kwargs):
        return True

    async def fake_audit(*args, **kwargs):
        return None

    if monkeypatch is not None:
        monkeypatch.setattr(settings, "withdrawal_approver_tokens", "approver-1:approver-secret,same-person:approver-secret")
        monkeypatch.setattr(settings, "withdrawal_release_tokens", "operator-1:operator-secret,same-person:operator-secret")
    old = {
        "SessionLocal": main_module.SessionLocal,
        "auth": main_module.auth,
        "require_role": main_module.require_role,
        "get_payout_provider": main_module.get_payout_provider,
        "_audit": main_module._audit,
        "sync_wallet_from_ledger": main_module.sync_wallet_from_ledger,
    }
    main_module.SessionLocal = sessions
    main_module.auth = fake_auth
    main_module.require_role = fake_role
    main_module._audit = fake_audit
    main_module.sync_wallet_from_ledger = AsyncMock()
    if provider is not None:
        main_module.get_payout_provider = lambda *_args, **_kwargs: provider
    try:
        return await main_module.mark_withdrawal_not_sent(
            withdrawal.id,
            main_module.WithdrawalNotSentRequest(
                operator_id=operator_id,
                admin_id=admin_id,
                evidence="Provider recovery independently confirmed no matching payout.",
            ),
            x_admin_token="admin",
            x_approver_token="approver-secret",
            x_release_token="operator-secret",
            authorization=None,
        )
    finally:
        for key, value in old.items():
            setattr(main_module, key, value)


def test_mark_not_sent_success_releases_reserved_balance_after_definitive_not_found(monkeypatch):
    async def run():
        engine = _db()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("0"), withdrawal_reserved=Decimal("25")))
                w = Withdrawal(
                    customer_id=1, request_id="req-not-found", account_ref="acct",
                    amount=25, currency="USDT", destination_masked="T***", destination="T" + "A" * 33,
                    network="TRON", provider="external_signer", provider_id=None, status="UNKNOWN",
                )
                db.add(w)
                await db.commit()
                await db.refresh(w)

                class Provider:
                    async def recover(self, *args, **kwargs):
                        raise PayoutNotFound("provider history contains no matching payout")

                monkeypatch.setattr(settings, "withdrawal_approver_tokens", "approver-1:approver-secret")
                monkeypatch.setattr(settings, "withdrawal_release_tokens", "operator-1:operator-secret")
                result = await _call_endpoint(main, sessions, w, provider=Provider(), monkeypatch=monkeypatch)

            async with sessions() as db:
                saved = await db.get(Withdrawal, w.id)
                ledger = (await db.execute(select(CustomerLedgerAccount).where(CustomerLedgerAccount.customer_id == 1))).scalar_one()
                assert result["status"] == "FAILED"
                assert saved.status == "FAILED"
                assert saved.provider_status == "NOT_SENT"
                assert ledger.withdrawal_reserved == Decimal("0.000000")
                assert ledger.available == Decimal("25.000000")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_mark_not_sent_rejects_withdrawal_that_is_not_unknown(monkeypatch):
    async def run():
        engine = _db()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                w = Withdrawal(
                    customer_id=1, request_id="req-submitted", amount=25, currency="USDT",
                    destination_masked="T***", destination="T" + "A" * 33,
                    network="TRON", provider="external_signer", provider_id="provider-id", status="SUBMITTED",
                )
                db.add(w)
                await db.commit()
                await db.refresh(w)

            monkeypatch.setattr(settings, "withdrawal_approver_tokens", "approver-1:approver-secret")
            with pytest.raises(HTTPException) as exc:
                await _call_endpoint(main, sessions, w, monkeypatch=monkeypatch)
            assert exc.value.status_code == 409
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_mark_not_sent_blocks_when_provider_finds_the_payout(monkeypatch):
    async def run():
        engine = _db()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                w = Withdrawal(
                    customer_id=1, request_id="req-found", amount=25, currency="USDT",
                    destination_masked="T***", destination="T" + "A" * 33,
                    network="TRON", provider="external_signer", provider_id="provider-id", status="UNKNOWN",
                )
                db.add(w)
                await db.commit()
                await db.refresh(w)

            class Provider:
                async def recover(self, *args, **kwargs):
                    return PayoutResult("external_signer", "real-provider-id", "SUBMITTED", "PENDING")

            monkeypatch.setattr(settings, "withdrawal_approver_tokens", "approver-1:approver-secret")
            with pytest.raises(HTTPException) as exc:
                await _call_endpoint(main, sessions, w, provider=Provider(), monkeypatch=monkeypatch)
            assert exc.value.status_code == 409
            async with sessions() as db:
                saved = await db.get(Withdrawal, w.id)
                assert saved.status == "UNKNOWN"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_mark_not_sent_does_not_release_when_provider_recovery_is_unknown(monkeypatch):
    async def run():
        engine = _db()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("0"), withdrawal_reserved=Decimal("25")))
                w = Withdrawal(
                    customer_id=1, request_id="req-unknown-recovery", amount=25, currency="USDT",
                    destination_masked="T***", destination="T" + "A" * 33,
                    network="TRON", provider="external_signer", provider_id=None, status="UNKNOWN",
                )
                db.add(w)
                await db.commit()
                await db.refresh(w)

            class Provider:
                async def recover(self, *args, **kwargs):
                    raise PayoutUnknown("signer lookup timed out")

            monkeypatch.setattr(settings, "withdrawal_approver_tokens", "approver-1:approver-secret")
            with pytest.raises(HTTPException) as exc:
                await _call_endpoint(main, sessions, w, provider=Provider(), monkeypatch=monkeypatch)
            assert exc.value.status_code == 409
            async with sessions() as db:
                saved = await db.get(Withdrawal, w.id)
                ledger = (await db.execute(select(CustomerLedgerAccount).where(CustomerLedgerAccount.customer_id == 1))).scalar_one()
                assert saved.status == "UNKNOWN"
                assert ledger.withdrawal_reserved == Decimal("25.000000")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_mark_not_sent_rejects_same_person_as_approver_and_operator(monkeypatch):
    async def run():
        engine = _db()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                w = Withdrawal(
                    customer_id=1, request_id="req-dual-control", amount=25, currency="USDT",
                    destination_masked="T***", destination="T" + "A" * 33,
                    network="TRON", provider="external_signer", provider_id="provider-id", status="UNKNOWN",
                )
                db.add(w)
                await db.commit()
                await db.refresh(w)

            monkeypatch.setattr(settings, "withdrawal_approver_tokens", "same-person:approver-secret")
            with pytest.raises(HTTPException) as exc:
                await _call_endpoint(
                    main, sessions, w,
                    admin_id="same-person",
                    operator_id="same-person",
                    monkeypatch=monkeypatch,
                )
            assert exc.value.status_code == 403
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_external_signer_recovery_lookup_error_is_not_treated_as_not_found():
    async def run():
        provider = object.__new__(ExternalSignerPayoutProvider)
        async def failed_request(*args, **kwargs):
            raise PayoutError("signer recovery lookup failed")
        provider._request = failed_request
        with pytest.raises(PayoutError):
            await provider.recover("withdrawal:never-seen", currency="USDT")
    asyncio.run(run())


def test_external_signer_recovery_without_provider_id_is_unknown_not_not_found():
    async def run():
        provider = object.__new__(ExternalSignerPayoutProvider)

        async def empty_recovery(*args, **kwargs):
            return {}

        provider._request = empty_recovery
        with pytest.raises(PayoutError) as exc:
            await provider.recover("withdrawal:never-seen", currency="USDT")
        assert not isinstance(exc.value, PayoutNotFound)

    asyncio.run(run())


def test_external_signer_recovery_requires_explicit_not_found_evidence():
    async def run():
        provider = object.__new__(ExternalSignerPayoutProvider)

        async def explicit_not_found(*args, **kwargs):
            return {"status": "NOT_FOUND"}

        provider._request = explicit_not_found
        with pytest.raises(PayoutNotFound):
            await provider.recover("withdrawal:never-seen", currency="USDT")

    asyncio.run(run())


def test_external_signer_recovery_404_is_definite_error_not_not_found(monkeypatch):
    import httpx
    from app.payout import classify_payout_exception

    response = httpx.Response(404, request=httpx.Request("POST", "https://signer.test/recover"))
    error = httpx.HTTPStatusError("not found", request=response.request, response=response)
    classified = classify_payout_exception(error)
    assert isinstance(classified, PayoutError)
    assert not isinstance(classified, PayoutNotFound)


# The release route itself is exercised through FastAPI's ASGI stack in CI when
# the application integration fixtures are available. This structural assertion
# prevents the separation check from being accidentally moved below payout I/O.
def test_release_withdrawal_route_contains_separation_gate():
    import inspect
    src = inspect.getsource(main.release_withdrawal)
    assert "release_separation_violation(" in src
    assert src.index("release_separation_violation(") < src.index("destination_allowed(")


def test_release_route_binds_operator_identity_over_http(monkeypatch):
    from fastapi.testclient import TestClient

    async def fake_auth(*args, **kwargs):
        return {"sub": "authenticated-operator", "role": "TREASURY"}

    async def fake_role(*args, **kwargs):
        return True

    monkeypatch.setattr(main, "auth", fake_auth)
    monkeypatch.setattr(main, "require_role", fake_role)
    monkeypatch.setattr(settings, "withdrawals_enabled", True)
    monkeypatch.setattr(settings, "payout_live_enabled", True)
    monkeypatch.setattr(settings, "withdrawal_release_tokens", "operator-1:operator-secret")

    client = TestClient(main.app)
    response = client.post(
        "/api/admin/withdrawals/1/release",
        json={"operator_id": "operator-1", "signature": None},
        headers={"X-Admin-Token": "admin", "X-Release-Token": "operator-secret"},
    )
    assert response.status_code == 403
    assert "identity" in response.json()["detail"].lower()


def test_mark_not_sent_rejects_provider_recovery_result_without_transaction_id(monkeypatch):
    async def run():
        engine = _db()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                db.add(CustomerLedgerAccount(customer_id=1, available=0, withdrawal_reserved=25))
                w = Withdrawal(
                    customer_id=1, request_id="req-empty-recovery", amount=25, currency="USDT",
                    destination_masked="T***", destination="T" + "A" * 33,
                    network="TRON", provider="external_signer", provider_id=None, status="UNKNOWN",
                )
                db.add(w)
                await db.commit()
                await db.refresh(w)

            class Provider:
                async def recover(self, *args, **kwargs):
                    return PayoutResult("external_signer", "", "UNKNOWN", "UNKNOWN")

            monkeypatch.setattr(settings, "withdrawal_approver_tokens", "approver-1:approver-secret")
            with pytest.raises(HTTPException) as exc:
                await _call_endpoint(main, sessions, w, provider=Provider(), monkeypatch=monkeypatch)
            assert exc.value.status_code == 409
            async with sessions() as db:
                saved = await db.get(Withdrawal, w.id)
                ledger = (await db.execute(select(CustomerLedgerAccount).where(CustomerLedgerAccount.customer_id == 1))).scalar_one()
                assert saved.status == "UNKNOWN"
                assert ledger.withdrawal_reserved == 25
                assert ledger.available == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
