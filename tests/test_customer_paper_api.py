from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import customer_paper_api as paper_api
from app.config import settings
from app.schemas import MarketRequest


def _verified_bars(latest_bar_at: datetime, *, volume: str = "100") -> pd.DataFrame:
    index = pd.date_range(end=latest_bar_at, periods=25, freq="min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * len(index),
            "high": [105.0] * len(index),
            "low": [95.0] * len(index),
            "close": [101.0] * len(index),
            "volume": [float(volume)] * len(index),
        },
        index=index,
    )
    frame.attrs["data_provenance"] = {
        "source": "ccxt_ohlcv",
        "asset_class": "crypto",
        "market_type": "spot",
        "volume_unit": "base_asset",
        "volume_unit_verified": True,
        "sha256": "a" * 64,
        "row_count": len(frame),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return frame


def test_customer_paper_market_context_requires_verified_complete_spot_bar():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    frame = _verified_bars(now - timedelta(minutes=2))
    context = paper_api._market_bar_context(frame, symbol="BTC/USDT", timeframe="1m")

    assert context["market_type"] == "spot"
    assert context["volume_unit"] == "base_asset"
    assert context["bar_data_sha256"] and len(context["bar_data_sha256"]) == 64
    assert context["timestamp"] == now - timedelta(minutes=2)


def test_customer_paper_market_context_rejects_unverified_or_incomplete_candle():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    frame = _verified_bars(now - timedelta(minutes=2))
    frame.attrs["data_provenance"]["source"] = "yfinance"
    with pytest.raises(HTTPException) as rejected_source:
        paper_api._market_bar_context(frame, symbol="BTC/USDT", timeframe="1m")
    assert rejected_source.value.status_code == 409

    incomplete = _verified_bars(now - timedelta(seconds=30))
    with pytest.raises(HTTPException) as rejected_bar:
        paper_api._market_bar_context(incomplete, symbol="BTC/USDT", timeframe="1m")
    assert rejected_bar.value.status_code == 409


def test_customer_paper_order_request_rejects_malformed_limit_orders():
    with pytest.raises(ValueError):
        paper_api.CustomerPaperOrderRequest(
            client_order_id="limit-without-price",
            symbol="BTC/USDT",
            side="BUY",
            quantity="0.1",
            order_type="LIMIT",
        )
    with pytest.raises(ValueError):
        paper_api.CustomerPaperOrderRequest(
            client_order_id="market-with-limit",
            symbol="BTC/USDT",
            side="BUY",
            quantity="0.1",
            order_type="MARKET",
            limit_price="100",
        )


@pytest.mark.postgres
@pytest.mark.skipif(
    not (os.getenv("ATLAS_PAPER_TEST_DATABASE_URL") and os.getenv("ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL")),
    reason="requires the disposable isolated PostgreSQL paper ledger provisioned by CI",
)
def test_customer_paper_api_order_fill_shared_capacity_tenant_isolation_and_recovery(monkeypatch):
    # This is a disposable CI database test. No live Supabase schema, funds, broker,
    # exchange API, payout, or real-money ledger is used.
    monkeypatch.setattr(settings, "database_url", os.environ["ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_database_url", os.environ["ATLAS_PAPER_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", True)
    monkeypatch.setattr(settings, "customer_paper_shared_database_enabled", True)
    monkeypatch.setattr(settings, "paper_trading", True)
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "customer_live_trading_enabled", False)
    monkeypatch.setattr(settings, "payout_live_enabled", False)
    monkeypatch.setattr(settings, "broker_sandbox", True)
    monkeypatch.setattr(settings, "usdt_tron_enabled", False)
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 1)
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 24)
    monkeypatch.setattr(settings, "paper_max_bar_volume_participation", 0.01)
    monkeypatch.setattr(settings, "paper_queue_fill_probability", 1.0)
    monkeypatch.setattr(settings, "paper_rejection_probability", 0.0)
    monkeypatch.setattr(settings, "paper_market_impact_coefficient", 0.0)
    monkeypatch.setattr(settings, "paper_max_market_impact_bps", 0.0)

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    market = {"bar_at": now - timedelta(minutes=3)}

    def fake_market_data(_request):
        return _verified_bars(market["bar_at"], volume="100")

    async def fake_get_customer(authorization, _db, require_aal2=True):
        assert require_aal2 is True
        if authorization == "Bearer customer-a":
            return SimpleNamespace(id=929901), {"aal": "aal2"}
        if authorization == "Bearer customer-b":
            return SimpleNamespace(id=929902), {"aal": "aal2"}
        raise HTTPException(401, "test customer is not authenticated")

    class DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _type, _value, _traceback):
            return False

    async def state_guard():
        return None

    monkeypatch.setattr(paper_api, "SessionLocal", lambda: DummySession())
    api_app = FastAPI()
    api_app.include_router(paper_api.build_customer_paper_router(
        get_customer=fake_get_customer,
        market_data=fake_market_data,
        market_request_type=MarketRequest,
        state_guard=state_guard,
    ))
    client = TestClient(api_app)

    def post(path, customer, body=None):
        return client.post(path, headers={"Authorization": f"Bearer {customer}"}, json=body)

    def get(path, customer):
        return client.get(path, headers={"Authorization": f"Bearer {customer}"})

    # Initialize virtual money only. Each profile is seeded independently in the paper schema.
    for customer in ("customer-a", "customer-b"):
        seeded = post("/api/customer/paper/account/initialize", customer)
        assert seeded.status_code == 200, seeded.text
        assert seeded.json()["virtual_only"] is True
        assert Decimal(seeded.json()["starting_cash"]) == Decimal("10000.000000000000000000")

    for customer, order_id in (("customer-a", "shared-bar-order-a"), ("customer-b", "shared-bar-order-b")):
        submitted = post("/api/customer/paper/orders", customer, {
            "client_order_id": order_id,
            "symbol": "BTC/USDT",
            "side": "BUY",
            "quantity": "0.6",
            "order_type": "MARKET",
            "timeframe": "1m",
        })
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["order"]["status"] == "SUBMITTED"
        assert submitted.json()["order"]["filled_quantity"] == "0.000000000000000000"

    # Both tenants advance against the exact same completed bar. Shared capacity is 1.0:
    # customer A receives 0.6 and customer B can receive only the 0.4 remainder.
    market["bar_at"] = now - timedelta(minutes=2)
    first = post("/api/customer/paper/orders/shared-bar-order-a/advance", "customer-a")
    second = post("/api/customer/paper/orders/shared-bar-order-b/advance", "customer-b")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert Decimal(first.json()["fill"]["filled_quantity"]) == Decimal("0.600000000000000000")
    assert Decimal(second.json()["fill"]["filled_quantity"]) == Decimal("0.400000000000000000")
    assert second.json()["order"]["status"] == "PARTIALLY_FILLED"

    # Repeating the same bar after a worker/client retry cannot consume extra volume.
    replay_attempt = post("/api/customer/paper/orders/shared-bar-order-b/advance", "customer-b")
    assert replay_attempt.status_code == 200, replay_attempt.text
    assert replay_attempt.json()["status"] == "WAITING_FOR_BAR_CAPACITY"

    # A later bar completes the remainder after restart/recovery by reading persisted state.
    market["bar_at"] = now - timedelta(minutes=1)
    finish = post("/api/customer/paper/orders/shared-bar-order-b/advance", "customer-b")
    assert finish.status_code == 200, finish.text
    assert finish.json()["order"]["status"] == "FILLED"
    assert Decimal(finish.json()["order"]["filled_quantity"]) == Decimal("0.600000000000000000")
    assert get("/api/customer/paper/orders", "customer-b").json()["orders"] == []

    # Tenant B cannot inspect A's order even if it guesses the immutable client order ID.
    cross_tenant = get("/api/customer/paper/orders/shared-bar-order-a", "customer-b")
    assert cross_tenant.status_code == 404, cross_tenant.text
    assert get("/api/customer/paper/positions", "customer-a").json()["positions"][0]["quantity"] == "0.600000000000000000"

    # The database trigger independently blocks an over-capacity insert, even if application
    # code accidentally bypasses the fill helper.
    from app.customer_paper_ledger import _paper_engine

    async def assert_database_capacity_guard():
        engine = _paper_engine()
        try:
            async with engine.begin() as conn:
                budget = (await conn.execute(text(
                    "SELECT bar_key, capacity FROM customer_paper.market_bar_budgets ORDER BY created_at LIMIT 1"
                ))).mappings().first()
                assert budget is not None
                with pytest.raises(Exception, match="aggregate paper fill allocations exceed"):
                    async with conn.begin_nested():
                        await conn.execute(text(
                            """
                            INSERT INTO customer_paper.market_bar_allocations
                                (allocation_id, bar_key, allocated_quantity)
                            VALUES (:allocation_id, :bar_key, :quantity)
                            """
                        ), {
                            "allocation_id": "00000000-0000-4000-8000-000000000001",
                            "bar_key": budget["bar_key"],
                            "quantity": Decimal(budget["capacity"]),
                        })
        finally:
            await engine.dispose()

    asyncio.run(assert_database_capacity_guard())
    client.close()
