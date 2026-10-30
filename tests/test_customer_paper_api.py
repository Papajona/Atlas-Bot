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
from app.paper_staging import PaperMarketRequest


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
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "timeframe": "1m",
        "market_type": "spot",
        "volume_unit": "base_asset",
        "volume_unit_verified": True,
        "sha256": "a" * 64,
        "row_count": len(frame),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return frame


def _freeze_paper_api_clock(monkeypatch, frozen_now: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen_now.replace(tzinfo=None)
            return frozen_now.astimezone(tz)

    monkeypatch.setattr(paper_api, "datetime", FrozenDateTime)


def test_customer_paper_market_context_requires_verified_complete_spot_bar(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    _freeze_paper_api_clock(monkeypatch, now)
    frame = _verified_bars(now - timedelta(minutes=2))
    frame.attrs["data_provenance"]["fetched_at_utc"] = (now - timedelta(seconds=1)).isoformat()
    context = paper_api._market_bar_context(frame, symbol="BTC/USDT", timeframe="1m")

    assert context["market_type"] == "spot"
    assert context["volume_unit"] == "base_asset"
    assert context["bar_data_sha256"] and len(context["bar_data_sha256"]) == 64
    assert context["timestamp"] == now - timedelta(minutes=2)


def test_customer_paper_market_context_rejects_unverified_or_incomplete_candle(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    _freeze_paper_api_clock(monkeypatch, now)
    frame = _verified_bars(now - timedelta(minutes=2))
    frame.attrs["data_provenance"]["fetched_at_utc"] = (now - timedelta(seconds=1)).isoformat()
    frame.attrs["data_provenance"]["source"] = "yfinance"
    with pytest.raises(HTTPException) as rejected_source:
        paper_api._market_bar_context(frame, symbol="BTC/USDT", timeframe="1m")
    assert rejected_source.value.status_code == 409

    incomplete = _verified_bars(now - timedelta(seconds=30))
    incomplete.attrs["data_provenance"]["fetched_at_utc"] = (now - timedelta(seconds=1)).isoformat()
    with pytest.raises(HTTPException) as rejected_bar:
        paper_api._market_bar_context(incomplete, symbol="BTC/USDT", timeframe="1m")
    assert rejected_bar.value.status_code == 409


def test_manual_replay_ttl_expiry_rechecks_order_state_and_handles_race(monkeypatch):
    frozen_now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    bar_at = frozen_now - timedelta(minutes=1)
    _freeze_paper_api_clock(monkeypatch, frozen_now)
    frame = _verified_bars(bar_at)
    frame.attrs["data_provenance"]["fetched_at_utc"] = (
        frozen_now - timedelta(seconds=1)
    ).isoformat()

    open_order = {
        "client_order_id": "manual-replay-race",
        "order_id": 88001,
        "status": "PARTIALLY_FILLED",
        "venue": "binance",
        "market_type": "spot",
        "symbol": "BTC/USDT",
        "side": "BUY",
        "metadata": {
            "timeframe": "1m",
            "execution_model": "completed_bar_volume_capped_heuristic_v1",
            "submission_bar_at": (bar_at - timedelta(minutes=1)).isoformat(),
        },
    }
    prior_fill = {
        "fill_id": "immutable-prior-fill",
        "status": "APPENDED",
        "quantity": "0.25",
    }
    concurrent_fill = {
        "fill_id": "concurrent-final-fill",
        "status": "APPENDED",
        "quantity": "0.50",
    }
    shared_state = {
        "order": dict(open_order),
        "fills": [dict(prior_fill)],
        "transitions": [],
    }
    order_reads = []
    expiry_calls = []
    fill_lookups = []

    async def get_customer_paper_order(*, customer_id, client_order_id):
        assert customer_id == 99031
        assert client_order_id == "manual-replay-race"
        order_reads.append(len(order_reads) + 1)
        # Every read reflects the same shared lifecycle state; the result is not
        # selected by call count.
        return dict(shared_state["order"])

    async def get_prior_fill(**kwargs):
        assert kwargs["customer_id"] == 99031
        assert kwargs["client_order_id"] == "manual-replay-race"
        assert kwargs["bar_key"] == "manual-replay-bar"
        fill_lookups.append(kwargs)
        assert shared_state["fills"]
        return dict(shared_state["fills"][0])

    async def expire_order(**kwargs):
        expiry_calls.append(kwargs)
        assert shared_state["order"]["status"] == "PARTIALLY_FILLED"
        # Model the concurrent transaction winning the order lock: it appends
        # the final fill and changes the order to FILLED before TTL expiry can
        # commit. The expiry helper reports the observed lifecycle conflict.
        shared_state["fills"].append(dict(concurrent_fill))
        shared_state["order"] = {
            **shared_state["order"],
            "status": "FILLED",
            "filled_quantity": "0.75",
        }
        shared_state["transitions"].append(
            {
                "from": "PARTIALLY_FILLED",
                "to": "FILLED",
                "fill_id": concurrent_fill["fill_id"],
            }
        )
        raise paper_api.CustomerPaperOrderConflict(
            "order became terminal after a concurrent fill"
        )

    async def forbidden_record_fill(**_kwargs):
        raise AssertionError("a replay must not append a duplicate fill")

    async def fake_get_customer(authorization, _db, require_aal2=True):
        assert authorization == "Bearer replay-customer"
        assert require_aal2 is True
        return SimpleNamespace(id=99031), {"aal": "aal2"}

    class DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _type, _value, _traceback):
            return False

    async def state_guard():
        return None

    async def register_bar(**kwargs):
        assert kwargs["venue"] == "binance"
        assert kwargs["symbol"] == "BTC/USDT"
        return {"bar_key": "manual-replay-bar"}

    monkeypatch.setattr(paper_api, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(paper_api, "_paper_feature_flags", lambda: None)
    monkeypatch.setattr(paper_api, "get_customer_paper_order", get_customer_paper_order)
    monkeypatch.setattr(paper_api, "get_customer_paper_fill_for_bar", get_prior_fill)
    monkeypatch.setattr(paper_api, "expire_customer_paper_order", expire_order)
    monkeypatch.setattr(paper_api, "record_customer_paper_fill", forbidden_record_fill)
    monkeypatch.setattr(paper_api, "register_customer_paper_market_bar", register_bar)
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 1)
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 1)

    api_app = FastAPI()
    api_app.include_router(paper_api.build_customer_paper_router(
        get_customer=fake_get_customer,
        market_data=lambda _request: frame,
        market_request_type=PaperMarketRequest,
        state_guard=state_guard,
    ))
    response = TestClient(api_app).post(
        "/api/customer/paper/orders/manual-replay-race/advance",
        headers={"Authorization": "Bearer replay-customer"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "REPLAYED"
    assert payload["fill"]["fill_id"] == "immutable-prior-fill"
    assert payload["order"]["status"] == "FILLED"
    assert shared_state["order"]["filled_quantity"] == "0.75"
    assert shared_state["transitions"] == [
        {
            "from": "PARTIALLY_FILLED",
            "to": "FILLED",
            "fill_id": "concurrent-final-fill",
        }
    ]
    assert [fill["fill_id"] for fill in shared_state["fills"]] == [
        "immutable-prior-fill",
        "concurrent-final-fill",
    ]
    assert len(order_reads) == 3
    assert len(fill_lookups) == 1
    assert len(expiry_calls) == 1
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


def test_unauthenticated_customer_paper_request_rejects_before_state_guard():
    calls = {"customer": 0, "guard": 0}

    async def get_customer(authorization, _db, require_aal2=True):
        calls["customer"] += 1
        raise AssertionError("missing Authorization must be rejected before customer lookup")

    async def state_guard():
        calls["guard"] += 1

    api_app = FastAPI()
    api_app.include_router(paper_api.build_customer_paper_router(
        get_customer=get_customer,
        market_data=lambda _request: (_ for _ in ()).throw(AssertionError("market data must not be fetched")),
        market_request_type=MarketRequest,
        state_guard=state_guard,
    ))
    response = TestClient(api_app).get("/api/customer/paper/balances")

    assert response.status_code == 401
    assert calls == {"customer": 0, "guard": 0}


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
    monkeypatch.setattr(settings, "discipline_enabled", False)
    monkeypatch.setattr(settings, "paper_max_bar_volume_participation", 0.01)
    monkeypatch.setattr(settings, "paper_queue_fill_probability", 1.0)
    monkeypatch.setattr(settings, "paper_rejection_probability", 0.0)
    monkeypatch.setattr(settings, "paper_market_impact_coefficient", 0.0)
    monkeypatch.setattr(settings, "paper_max_market_impact_bps", 0.0)

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    frozen_now = now + timedelta(minutes=3)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen_now.replace(tzinfo=None)
            return frozen_now.astimezone(tz)

    # Freeze only the router's clock so three completed minute-bars can be replayed
    # deterministically without sleeping and without using future live market data.
    monkeypatch.setattr(paper_api, "datetime", FrozenDateTime)
    market = {"bar_at": now}
    market_requests = []

    def fake_market_data(request):
        market_requests.append(request)
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

    async def fresh_group_marks(customer_id, request, _market_context):
        # This API integration test uses deterministic fixture candles and a fresh
        # UTC quote mark for the existing position; production wires the real server
        # market-context loader from app.main.
        return {
            (
                str(request.exchange).strip().lower(),
                "spot",
                str(request.symbol).strip().upper(),
            ): {
                "price": "100",
                "quote_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            }
        }

    api_app = FastAPI()
    api_app.include_router(paper_api.build_customer_paper_router(
        get_customer=fake_get_customer,
        market_data=fake_market_data,
        market_request_type=PaperMarketRequest,
        state_guard=state_guard,
        group_mark_price_loader=fresh_group_marks,
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

    # A one-minute order requests a one-day history window (two CCXT pages at most),
    # not the previous 30 days / roughly 44 sequential pages.
    assert market_requests
    assert all(request.timeframe == "1m" and request.days == 1 for request in market_requests)

    # Both tenants advance against the exact same completed bar. Shared capacity is 1.0:
    # customer A receives 0.6 and customer B can receive only the 0.4 remainder.
    market["bar_at"] = now + timedelta(minutes=1)
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
    assert replay_attempt.json()["status"] == "REPLAYED"
    assert Decimal(replay_attempt.json()["fill"]["quantity"]) == Decimal("0.400000000000000000")
    assert replay_attempt.json()["order"]["filled_quantity"] == "0.400000000000000000"

    # A later bar completes the remainder after restart/recovery by reading persisted state.
    market["bar_at"] = now + timedelta(minutes=2)
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

    # A stale pending manual order must expire even when fill latency would otherwise
    # return early before the TTL check.
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 3)
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 1)
    market["bar_at"] = now + timedelta(minutes=2)
    ttl_submitted = post("/api/customer/paper/orders", "customer-a", {
        "client_order_id": "manual-order-ttl-boundary",
        "symbol": "BTC/USDT",
        "side": "BUY",
        "quantity": "0.1",
        "order_type": "MARKET",
        "timeframe": "1m",
    })
    assert ttl_submitted.status_code == 200, ttl_submitted.text
    assert ttl_submitted.json()["order"]["status"] == "SUBMITTED"
    market["bar_at"] = now + timedelta(minutes=3)
    frozen_now = now + timedelta(minutes=4)
    ttl_advance = post(
        "/api/customer/paper/orders/manual-order-ttl-boundary/advance", "customer-a"
    )
    assert ttl_advance.status_code == 200, ttl_advance.text
    assert ttl_advance.json()["status"] == "EXPIRED_TTL"
    assert ttl_advance.json()["order"]["status"] == "EXPIRED"
    client.close()
