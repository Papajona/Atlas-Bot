"""Regression tests for request-body validation on raw-dict admin/auth routes.

Each case must be rejected with a 4xx before any database or provider call is made.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app import main


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(main, "auth", AsyncMock(return_value={"sub": "tester"}))
    monkeypatch.setattr(main, "require_role", AsyncMock(return_value=True))


def _status(coro) -> int:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro)
    return exc.value.status_code


@pytest.mark.parametrize("active", ["false", "true", 0, 1, None, "no"])
def test_role_change_rejects_non_boolean_active(active):
    body = {"auth_user_id": "user-1", "role": "ADMINISTRATOR", "active": active}
    assert _status(
        main.admin_set_role(body, authorization=None, x_admin_token="x")
    ) == 400


def test_role_change_rejects_non_object_body():
    assert _status(
        main.admin_set_role([], authorization=None, x_admin_token="x")
    ) == 422


@pytest.mark.parametrize(
    "body",
    [
        {"amount_usdt": 1},
        {"wallet_id": None, "amount_usdt": 1},
        {"wallet_id": "abc", "amount_usdt": 1},
        {"wallet_id": True, "amount_usdt": 1},
        {"wallet_id": 1.7, "amount_usdt": 1},
        {"wallet_id": "1.7", "amount_usdt": 1},
        {"wallet_id": 1},
        {"wallet_id": 1, "amount_usdt": "NaN"},
        {"wallet_id": 1, "amount_usdt": "Infinity"},
        {"wallet_id": 1, "amount_usdt": "-Infinity"},
        {"wallet_id": 1, "amount_usdt": "abc"},
        {"wallet_id": 1, "amount_usdt": True},
        [],
    ],
)
def test_tron_sweep_prepare_rejects_malformed_input_with_422(body):
    assert _status(
        main.admin_prepare_tron_sweep(
            body, authorization=None, x_admin_token="x"
        )
    ) == 422


@pytest.mark.parametrize(
    "txid",
    [
        "not-a-hash",
        "abc123",
        "z" * 64,
        "0x" + "a" * 64,
        "a" * 63,
        "a" * 65,
    ],
)
def test_tron_broadcast_record_requires_64_hex_transaction_hash(txid):
    assert _status(
        main.admin_record_tron_sweep_broadcast(
            1, {"transaction_id": txid}, authorization=None, x_admin_token="x"
        )
    ) == 400


def test_tron_broadcast_record_rejects_non_object_body():
    assert _status(
        main.admin_record_tron_sweep_broadcast(
            1, [], authorization=None, x_admin_token="x"
        )
    ) == 422


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"email": "no-at-sign"},
        {"email": "a" * 400 + "@example.com"},
        {},
    ],
)
def test_password_reset_rejects_bad_bodies(body):
    assert _status(main.customer_password_reset(body)) == 422


@pytest.mark.parametrize(
    "email",
    [
        "@",
        "foo@",
        "@example.com",
        "foo@.com",
        "foo@example..com",
        "foo@localhost",
        "foo @example.com",
        ".foo@example.com",
        "foo..bar@example.com",
    ],
)
def test_password_reset_rejects_malformed_email_before_rate_limit_or_provider(
    monkeypatch, email
):
    rate_limit = AsyncMock(return_value=(True, None))
    provider = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(main, "allow_rate_limit", rate_limit)
    monkeypatch.setattr(main, "_supabase_request", provider)

    assert _status(main.customer_password_reset({"email": email})) == 422
    rate_limit.assert_not_awaited()
    provider.assert_not_awaited()


def test_password_reset_accepts_valid_mailbox_before_provider(monkeypatch):
    rate_limit = AsyncMock(return_value=(True, None))
    provider = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(main, "allow_rate_limit", rate_limit)
    monkeypatch.setattr(main, "_supabase_request", provider)

    result = asyncio.run(main.customer_password_reset({"email": "alice+atlas@example.co.uk"}))

    assert result == {"ok": True}
    rate_limit.assert_awaited_once()
    provider.assert_awaited_once_with(
        "/auth/v1/recover", payload={"email": "alice+atlas@example.co.uk"}
    )


class _ScalarResult:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _SweepSession:
    def __init__(self, *rows):
        self.rows = list(rows)
        self.statements = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarResult(self.rows.pop(0))

    async def commit(self):
        self.commits += 1


def test_tron_broadcast_retry_matches_historical_uppercase_hash_case_insensitively(
    monkeypatch,
):
    sweep = SimpleNamespace(id=4, status="SUBMITTED", transaction_id="A" * 64)
    session = _SweepSession(sweep)
    monkeypatch.setattr(main, "SessionLocal", lambda: session)

    result = asyncio.run(
        main.admin_record_tron_sweep_broadcast(
            4, {"transaction_id": "a" * 64}, authorization=None, x_admin_token="x"
        )
    )

    assert result == {
        "sweep_id": 4,
        "status": "SUBMITTED",
        "transaction_id": "a" * 64,
        "idempotent": True,
    }
    assert len(session.statements) == 1
    assert session.commits == 0


def test_tron_broadcast_rejects_case_variant_already_owned_by_another_sweep(
    monkeypatch,
):
    sweep = SimpleNamespace(
        id=4, status="READY_FOR_SIGNER", transaction_id="", custody_transfer_id=None
    )
    duplicate = SimpleNamespace(id=9, transaction_id="A" * 64)
    session = _SweepSession(sweep, duplicate)
    monkeypatch.setattr(main, "SessionLocal", lambda: session)

    assert _status(
        main.admin_record_tron_sweep_broadcast(
            4, {"transaction_id": "a" * 64}, authorization=None, x_admin_token="x"
        )
    ) == 409
    assert len(session.statements) == 2
    assert "lower(tron_sweeps.transaction_id)" in str(session.statements[1]).lower()
    assert session.commits == 0
