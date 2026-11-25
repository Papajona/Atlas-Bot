"""Regression tests for request-body validation on raw-dict admin/auth routes.

Each case must be rejected with a 4xx before any database or provider call is made.
"""
from __future__ import annotations

import asyncio
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
