from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from app import customer_paper_api as paper_api
from app import customer_paper_lifecycle as paper_lifecycle
from app.config import settings
from app.schemas import CustomerPaperMarketRequest, MarketRequest
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


@pytest.mark.parametrize("bar_provenance_matches", [True, False])
def test_manual_replay_validates_existing_fill_bar_provenance_before_replay(monkeypatch, bar_provenance_matches):
    frozen_now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    bar_at = frozen_now - timedelta(minutes=1)
    _freeze_paper_api_clock(monkeypatch, frozen_now)
    frame = _verified_bars(bar_at)
    frame.attrs["data_provenance"]["fetched_at_utc"] = (
        frozen_now - timedelta(seconds=1)
    ).isoformat()
    submission_bar_at = bar_at - timedelta(minutes=1)
    submission_bar_hash = next(
        bar["bar_data_sha256"]
        for bar in paper_api._market_bar_contexts(
            frame, symbol="BTC/USDT", timeframe="1m"
        )
        if bar["timestamp"] == submission_bar_at
    )

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
            "submission_bar_at": submission_bar_at.isoformat(),
            "submission_bar_sha256": submission_bar_hash,
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
    expected_bar_key = paper_api._paper_market_bar_key(
        venue="binance",
        symbol="BTC/USDT",
        timeframe="1m",
        bar_at=bar_at,
    )
    expected_bar = next(
        bar for bar in paper_api._market_bar_contexts(
            frame, symbol="BTC/USDT", timeframe="1m"
        ) if bar["timestamp"] == bar_at
    )
    registration_calls = []

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
        # Existing-fill lookup identifies the immutable fill, but registration must
        # still compare the incoming candle against its durable shared-bar budget.
        assert kwargs["bar_key"] == expected_bar_key
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
        def __init__(self):
            self.commit_count = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, _type, _value, _traceback):
            return False

        async def commit(self):
            self.commit_count += 1

    async def state_guard():
        return None

    async def register_bar(**kwargs):
        registration_calls.append(kwargs)
        assert kwargs["venue"] == "binance"
        assert kwargs["symbol"] == "BTC/USDT"
        assert kwargs["bar_at"] == bar_at
        durable_digest = (
            expected_bar["bar_data_sha256"]
            if bar_provenance_matches else "0" * 64
        )
        if kwargs["bar_data_sha256"] != durable_digest:
            raise paper_api.CustomerPaperOrderConflict(
                "completed-bar provenance conflicts with immutable shared paper budget"
            )
        return {"bar_key": expected_bar_key}

    customer_session = DummySession()
    monkeypatch.setattr(paper_api, "SessionLocal", lambda: customer_session)
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

    if not bar_provenance_matches:
        assert response.status_code == 409, response.text
        assert len(registration_calls) == 1
        assert registration_calls[0]["bar_data_sha256"] == expected_bar["bar_data_sha256"]
        assert expiry_calls == []
        assert shared_state["transitions"] == []
        assert [fill["fill_id"] for fill in shared_state["fills"]] == ["immutable-prior-fill"]
        return

    assert response.status_code == 200, response.text
    assert len(registration_calls) == 1
    assert customer_session.commit_count == 1
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

        async def commit(self):
            return None

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
    paper_api._PAPER_MARKET_SNAPSHOTS.clear()
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
    paper_api._PAPER_MARKET_SNAPSHOTS.clear()
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



def test_production_paper_market_request_accepts_only_the_computed_bounded_history():
    from app.customer_paper_api import _TIMEFRAME_SECONDS

    # Paper orders require 25 completed bars. The calculated history window ranges
    # from one day for 1m through 25 days for 1d, all within the paper-only model.
    for timeframe, timeframe_seconds in _TIMEFRAME_SECONDS.items():
        required_bars = 25
        days = max(1, (required_bars * timeframe_seconds + 86_399) // 86_400)
        request = CustomerPaperMarketRequest(
            asset="crypto",
            symbol="BTC/USDT",
            exchange="binance",
            timeframe=timeframe,
            days=days,
        )
        assert request.days == days
        assert 1 <= request.days <= 30

    # The unrelated public/research contract must keep its stricter lower bound.
    with pytest.raises(ValidationError):
        MarketRequest(days=1)
    with pytest.raises(ValidationError):
        CustomerPaperMarketRequest(days=0)
    with pytest.raises(ValidationError):
        CustomerPaperMarketRequest(days=31)


def test_production_customer_paper_router_uses_the_bounded_request_model():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    router = source.split("app.include_router(build_customer_paper_router(", 1)[1]
    router = router.split("))", 1)[0]
    assert "market_request_type=CustomerPaperMarketRequest" in router
    assert "market_request_type=MarketRequest" not in router


def test_paper_ttl_idempotency_key_is_scoped_to_customer_order_and_bar():
    key = paper_api._paper_ttl_idempotency_key(23, "client-a", "bar-a")
    assert key == paper_api._paper_ttl_idempotency_key(23, "client-a", "bar-a")
    assert key != paper_api._paper_ttl_idempotency_key(23, "client-b", "bar-a")
    assert key != paper_api._paper_ttl_idempotency_key(24, "client-a", "bar-a")
    assert key != paper_api._paper_ttl_idempotency_key(23, "client-a", "bar-b")
    assert len(key) <= 150


def test_paper_rejection_and_queue_draws_depend_on_server_order_identity():
    draw_a = paper_api._paper_order_draw(23, "server-order-a", "bar-a", "rejection")
    assert draw_a == paper_api._paper_order_draw(23, "server-order-a", "bar-a", "rejection")
    assert draw_a != paper_api._paper_order_draw(23, "server-order-b", "bar-a", "rejection")
    assert draw_a != paper_api._paper_order_draw(24, "server-order-a", "bar-a", "rejection")
    with pytest.raises(ValueError, match="server-generated order identity"):
        paper_api._paper_order_draw(23, "", "bar-a", "queue")


def test_same_customer_order_retry_ignores_only_submission_bar_metadata():
    stored_metadata = {
        "timeframe": "1m",
        "execution_owner": "manual_customer_api",
        "submission_bar_at": "2026-11-02T10:00:00+00:00",
        "submission_bar_sha256": "a" * 64,
        "dataset_sha256": "b" * 64,
        "data_source": "ccxt_ohlcv",
    }
    existing = {
        "venue": "binance",
        "market_type": "spot",
        "symbol": "BTC/USDT",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": Decimal("0.100000000000000000"),
        "limit_price": None,
        "currency": "USDT",
        "metadata_json": stored_metadata,
    }
    requested_metadata = {
        **stored_metadata,
        "submission_bar_at": "2026-11-02T10:01:00+00:00",
        "submission_bar_sha256": "c" * 64,
        "dataset_sha256": "d" * 64,
    }
    args = {
        "venue": "binance",
        "market_type": "spot",
        "symbol": "BTC/USDT",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": Decimal("0.1"),
        "limit_price": None,
        "currency": "USDT",
        "metadata": requested_metadata,
    }
    assert paper_lifecycle._existing_order_intent_matches(existing, **args)
    assert not paper_lifecycle._existing_order_intent_matches(
        existing, **{**args, "quantity": Decimal("0.2")}
    )
    changed_owner = {**requested_metadata, "execution_owner": "autonomous_customer_bot"}
    assert not paper_lifecycle._existing_order_intent_matches(
        existing, **{**args, "metadata": changed_owner}
    )


@pytest.mark.parametrize(
    ("submission_hash_matches", "history_complete"),
    [(True, True), (False, True), (True, False)],
)
def test_manual_paper_advance_requires_original_submission_and_pre_fill_history(
    monkeypatch, submission_hash_matches, history_complete
):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    latest_bar_at = now - timedelta(minutes=1)
    submission_bar_at = latest_bar_at - timedelta(minutes=3)
    _freeze_paper_api_clock(monkeypatch, now)
    frame = _verified_bars(latest_bar_at, volume="100")
    frame.attrs["data_provenance"]["fetched_at_utc"] = (now - timedelta(seconds=1)).isoformat()
    submission_contexts = paper_api._market_bar_contexts(
        frame, symbol="BTC/USDT", timeframe="1m"
    )
    submission_bar_hash = next(
        bar["bar_data_sha256"]
        for bar in submission_contexts
        if bar["timestamp"] == submission_bar_at
    )
    if not history_complete:
        # Keep the immutable submission candle but remove enough pre-fill bars
        # that the endpoint's full 20-return volatility window cannot be formed.
        frame = frame.iloc[-22:].copy()
        frame.attrs["data_provenance"] = dict(frame.attrs["data_provenance"])
        frame.attrs["data_provenance"]["row_count"] = len(frame)

    order = {
        "client_order_id": "multi-bar-manual-order",
        "order_id": 99081,
        "status": "SUBMITTED",
        "venue": "binance",
        "market_type": "spot",
        "symbol": "BTC/USDT",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": "1.000000000000000000",
        "remaining_quantity": "1.000000000000000000",
        "filled_quantity": "0.000000000000000000",
        "limit_price": None,
        "currency": "USDT",
        "metadata": {
            "timeframe": "1m",
            "execution_model": "completed_bar_volume_capped_heuristic_v1",
            "submission_bar_at": submission_bar_at.isoformat(),
            "submission_bar_sha256": (
                submission_bar_hash if submission_hash_matches else "0" * 64
            ),
        },
    }
    prior_fills = {}
    fill_rows = []
    registered_bars = []

    async def fake_get_customer(authorization, _db, require_aal2=True):
        assert authorization == "Bearer multi-bar-customer"
        assert require_aal2 is True
        return SimpleNamespace(id=99081), {"aal": "aal2"}

    class DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def commit(self):
            return None

    async def state_guard():
        return None

    async def get_order(*, customer_id, client_order_id):
        assert customer_id == 99081
        assert client_order_id == "multi-bar-manual-order"
        return dict(order)

    async def register_bar(**kwargs):
        bar_at = kwargs["bar_at"]
        registered_bars.append(bar_at)
        key = bar_at.isoformat()
        return {
            "bar_key": key,
            "bar_at": key,
            "capacity": "0.400000000000000000",
            "available": "0.400000000000000000",
        }

    async def fill_for_bar(*, customer_id, client_order_id, bar_key):
        assert customer_id == 99081
        assert client_order_id == "multi-bar-manual-order"
        return prior_fills.get(bar_key)

    async def balances(*_args, **_kwargs):
        return [{
            "account_code": "PAPER_CASH",
            "currency": "USDT",
            "balance": "10000.000000000000000000",
        }]

    async def record_fill(**kwargs):
        bar_key = kwargs["bar_key"]
        if bar_key in prior_fills:
            return prior_fills[bar_key]
        quantity = Decimal(kwargs["quantity"])
        filled = Decimal(order["filled_quantity"]) + quantity
        remaining = max(Decimal("0"), Decimal(order["quantity"]) - filled)
        order["filled_quantity"] = format(filled, "f")
        order["remaining_quantity"] = format(remaining, "f")
        order["status"] = "FILLED" if remaining == 0 else "PARTIALLY_FILLED"
        item = {
            "status": "APPENDED",
            "bar_key": bar_key,
            "quantity": format(quantity, "f"),
            "price": kwargs["price"],
        }
        prior_fills[bar_key] = item
        fill_rows.append(item)
        return item

    monkeypatch.setattr(paper_api, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(paper_api, "get_customer_paper_order", get_order)
    monkeypatch.setattr(paper_api, "register_customer_paper_market_bar", register_bar)
    monkeypatch.setattr(paper_api, "get_customer_paper_fill_for_bar", fill_for_bar)
    monkeypatch.setattr(paper_api, "get_customer_paper_balances", balances)
    monkeypatch.setattr(paper_api, "record_customer_paper_fill", record_fill)
    monkeypatch.setattr(paper_api, "_paper_feature_flags", lambda: None)
    monkeypatch.setattr(paper_api, "settings", settings)
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 1)
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 10)
    monkeypatch.setattr(settings, "paper_rejection_probability", 0.0)
    monkeypatch.setattr(settings, "paper_queue_fill_probability", 1.0)

    api_app = FastAPI()
    api_app.include_router(paper_api.build_customer_paper_router(
        get_customer=fake_get_customer,
        market_data=lambda _request: frame,
        market_request_type=PaperMarketRequest,
        state_guard=state_guard,
    ))
    client = TestClient(api_app)
    response = client.post(
        "/api/customer/paper/orders/multi-bar-manual-order/advance",
        headers={"Authorization": "Bearer multi-bar-customer"},
    )

    if not submission_hash_matches:
        assert response.status_code == 409, response.text
        assert "submission candle" in response.json()["detail"].lower()
        assert registered_bars == []
        assert fill_rows == []
        return

    if not history_complete:
        assert response.status_code == 409, response.text
        assert "full paper market-impact volatility window" in response.json()["detail"]
        assert registered_bars == []
        assert fill_rows == []
        # Corrected history restores the missing 20-return context. The same order
        # can then advance and the shared bar registration occurs only after preflight.
        frame = _verified_bars(latest_bar_at, volume="100")
        frame.attrs["data_provenance"]["fetched_at_utc"] = (now - timedelta(seconds=1)).isoformat()
        paper_api._PAPER_MARKET_SNAPSHOTS.clear()
        paper_api._PAPER_MARKET_SNAPSHOT_LOCKS.clear()
        response = client.post(
            "/api/customer/paper/orders/multi-bar-manual-order/advance",
            headers={"Authorization": "Bearer multi-bar-customer"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert order["status"] == "FILLED"
    assert Decimal(order["filled_quantity"]) == Decimal("1.000000000000000000")
    assert len(registered_bars) == 3
    assert registered_bars == sorted(registered_bars)
    assert [row["bar_key"] for row in fill_rows] == [at.isoformat() for at in registered_bars]
    assert len(payload["bars_processed"]) == 3
    assert payload["bars_processed"][0]["bar_at"] == (submission_bar_at + timedelta(minutes=1)).isoformat()
    assert payload["bars_processed"][-1]["bar_at"] == latest_bar_at.isoformat()


def test_customer_paper_market_snapshot_rate_limits_by_customer_before_exchange_fetch(monkeypatch):
    calls = []

    async def deny(key, *, limit, window_seconds):
        calls.append((key, limit, window_seconds))
        return False, 0

    def market_data(_request):
        raise AssertionError("exchange market data must not be requested after a rate-limit denial")

    monkeypatch.setattr(paper_api, "allow_rate_limit", deny)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(paper_api._fetch_verified_market_snapshot(
            customer_id=501,
            symbol="BTC/USDT",
            timeframe="1m",
            market_data=market_data,
            market_request_type=lambda **kwargs: kwargs,
        ))

    assert exc.value.status_code == 429
    assert calls == [("customer-paper-market:501", 12, 60)]


def test_customer_paper_market_snapshot_is_shared_across_customers_and_fetches_only_once(monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    frame = _verified_bars(now - timedelta(minutes=1), volume="100")
    frame.attrs["data_provenance"]["fetched_at_utc"] = (now - timedelta(seconds=1)).isoformat()
    _freeze_paper_api_clock(monkeypatch, now)
    paper_api._PAPER_MARKET_SNAPSHOTS.clear()
    paper_api._PAPER_MARKET_SNAPSHOT_LOCKS.clear()
    rate_calls = []
    market_calls = []

    async def allow(key, *, limit, window_seconds):
        rate_calls.append((key, limit, window_seconds))
        return True, limit - 1

    class Request:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def market_data(request):
        market_calls.append(request)
        return frame

    monkeypatch.setattr(paper_api, "allow_rate_limit", allow)
    first_frame, first_bar = asyncio.run(paper_api._fetch_verified_market_snapshot(
        customer_id=501, symbol="BTC/USDT", timeframe="1m",
        market_data=market_data, market_request_type=Request,
    ))
    second_frame, second_bar = asyncio.run(paper_api._fetch_verified_market_snapshot(
        customer_id=502, symbol="BTC/USDT", timeframe="1m",
        market_data=market_data, market_request_type=Request,
    ))

    assert first_frame is frame and second_frame is frame
    assert first_bar["bar_data_sha256"] == second_bar["bar_data_sha256"]
    assert len(market_calls) == 1
    assert market_calls[0].asset == "crypto"
    assert market_calls[0].exchange == "binance"
    assert market_calls[0].timeframe == "1m"
    assert market_calls[0].days <= 1
    assert rate_calls == [
        ("customer-paper-market:501", 12, 60),
        ("customer-paper-market:502", 12, 60),
    ]


def test_customer_paper_snapshot_anchors_older_order_history_and_separates_cache_windows(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    frame = _verified_bars(now - timedelta(minutes=1), volume="100")
    frame.attrs["data_provenance"]["fetched_at_utc"] = (now - timedelta(seconds=1)).isoformat()
    _freeze_paper_api_clock(monkeypatch, now)
    paper_api._PAPER_MARKET_SNAPSHOTS.clear()
    paper_api._PAPER_MARKET_SNAPSHOT_LOCKS.clear()
    market_calls = []

    async def allow(_key, *, limit, window_seconds):
        return True, limit - 1

    class Request:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def market_data(request):
        market_calls.append(request)
        return frame

    monkeypatch.setattr(paper_api, "allow_rate_limit", allow)
    # The first call represents a current submission and needs only the normal
    # bounded context. The second advances an order submitted three days earlier.
    asyncio.run(paper_api._fetch_verified_market_snapshot(
        customer_id=501,
        symbol="BTC/USDT",
        timeframe="1m",
        market_data=market_data,
        market_request_type=Request,
    ))
    asyncio.run(paper_api._fetch_verified_market_snapshot(
        customer_id=501,
        symbol="BTC/USDT",
        timeframe="1m",
        market_data=market_data,
        market_request_type=Request,
        earliest_required_at=now - timedelta(days=3),
    ))

    assert len(market_calls) == 2
    assert market_calls[0].days == 1
    assert market_calls[1].days >= 4
    assert market_calls[1].days <= 30


def test_paper_market_impact_requires_full_contiguous_return_history():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    index = pd.date_range(end=now, periods=21, freq="1min")
    frame = pd.DataFrame({"close": [100.0 + position for position in range(21)]}, index=index)

    volatility = paper_api._paper_market_impact_volatility(
        frame, bar_position=20, timeframe="1m"
    )
    assert volatility > 0
    assert pd.notna(volatility)

    with pytest.raises(HTTPException) as exc:
        paper_api._paper_market_impact_volatility(
            frame, bar_position=19, timeframe="1m"
        )
    assert exc.value.status_code == 409
    assert "full paper market-impact volatility window" in exc.value.detail


def test_paper_market_impact_rejects_gapped_pre_fill_return_history():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    index = list(pd.date_range(end=now, periods=21, freq="1min"))
    # Preserve order and uniqueness, but remove one expected minute interval.
    for position in range(10, len(index)):
        index[position] += timedelta(minutes=1)
    frame = pd.DataFrame(
        {"close": [100.0 + position for position in range(21)]},
        index=pd.DatetimeIndex(index),
    )

    with pytest.raises(HTTPException) as exc:
        paper_api._paper_market_impact_volatility(
            frame, bar_position=20, timeframe="1m"
        )
    assert exc.value.status_code == 409
    assert "Non-contiguous completed-candle history" in exc.value.detail


def test_customer_paper_snapshot_fails_closed_when_anchored_replay_exceeds_supported_window(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    _freeze_paper_api_clock(monkeypatch, now)
    paper_api._PAPER_MARKET_SNAPSHOTS.clear()
    paper_api._PAPER_MARKET_SNAPSHOT_LOCKS.clear()
    calls = []

    async def allow(_key, *, limit, window_seconds):
        return True, limit - 1

    def market_data(_request):
        calls.append(True)
        raise AssertionError("market data must not be fetched for an unsupported replay span")

    monkeypatch.setattr(paper_api, "allow_rate_limit", allow)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(paper_api._fetch_verified_market_snapshot(
            customer_id=501,
            symbol="BTC/USDT",
            timeframe="1m",
            market_data=market_data,
            market_request_type=lambda **kwargs: kwargs,
            earliest_required_at=now - timedelta(days=31),
        ))

    assert exc.value.status_code == 409
    assert "supported 30-day candle window" in exc.value.detail
    assert calls == []


def test_anchored_daily_snapshot_allows_valid_ttl_and_latency_within_30_day_replay(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    _freeze_paper_api_clock(monkeypatch, now)
    paper_api._PAPER_MARKET_SNAPSHOTS.clear()
    paper_api._PAPER_MARKET_SNAPSHOT_LOCKS.clear()
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 29)
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 1)
    calls = []

    async def allow(_key, *, limit, window_seconds):
        return True, limit - 1

    class MarketRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MarketFetchReached(Exception):
        pass

    def market_data(request):
        calls.append(request)
        raise MarketFetchReached

    monkeypatch.setattr(paper_api, "allow_rate_limit", allow)
    with pytest.raises(MarketFetchReached):
        asyncio.run(paper_api._fetch_verified_market_snapshot(
            customer_id=501,
            symbol="BTC/USDT",
            timeframe="1d",
            market_data=market_data,
            market_request_type=MarketRequest,
            earliest_required_at=now - timedelta(hours=1),
        ))

    assert len(calls) == 1
    assert calls[0].days == 30


def test_unanchored_daily_snapshot_clamps_valid_long_ttl_to_supported_fetch_window(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    _freeze_paper_api_clock(monkeypatch, now)
    paper_api._PAPER_MARKET_SNAPSHOTS.clear()
    paper_api._PAPER_MARKET_SNAPSHOT_LOCKS.clear()
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 29)
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 1)
    calls = []

    async def allow(_key, *, limit, window_seconds):
        return True, limit - 1

    class MarketRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MarketFetchReached(Exception):
        pass

    def market_data(request):
        calls.append(request)
        raise MarketFetchReached

    monkeypatch.setattr(paper_api, "allow_rate_limit", allow)
    with pytest.raises(MarketFetchReached):
        asyncio.run(paper_api._fetch_verified_market_snapshot(
            customer_id=501,
            symbol="BTC/USDT",
            timeframe="1d",
            market_data=market_data,
            market_request_type=MarketRequest,
        ))

    assert len(calls) == 1
    assert calls[0].days == 30

def test_market_bar_key_matches_customer_paper_lifecycle_identity():
    stamp = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    expected = hashlib.sha256(
        b"paper-market-bar-v1|binance|spot|BTC/USDT|1m|2026-11-02T10:00:00+00:00"
    ).hexdigest()
    assert paper_api._paper_market_bar_key(
        venue="BINANCE",
        symbol="btc/usdt",
        timeframe="1M",
        bar_at=stamp,
    ) == expected


def test_market_bar_context_conversion_runs_outside_event_loop(monkeypatch):
    from threading import get_ident

    loop_thread = get_ident()
    worker_threads = []

    def expensive_context(_df, *, symbol, timeframe):
        worker_threads.append(get_ident())
        return [{"symbol": symbol, "timeframe": timeframe}]

    monkeypatch.setattr(paper_api, "_market_bar_contexts", expensive_context)

    async def run():
        return await paper_api._market_bar_contexts_off_loop(
            object(), symbol="BTC/USDT", timeframe="1m"
        )

    result = asyncio.run(run())
    assert result == [{"symbol": "BTC/USDT", "timeframe": "1m"}]
    assert worker_threads
    assert worker_threads[0] != loop_thread



def test_customer_paper_snapshot_rejects_daily_replay_without_required_pre_fill_context(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    _freeze_paper_api_clock(monkeypatch, now)
    paper_api._PAPER_MARKET_SNAPSHOTS.clear()
    paper_api._PAPER_MARKET_SNAPSHOT_LOCKS.clear()
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 24)
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 1)
    calls = []

    async def allow(_key, *, limit, window_seconds):
        return True, limit - 1

    def market_data(_request):
        calls.append(True)
        raise AssertionError("the provider cannot supply the complete required context")

    monkeypatch.setattr(paper_api, "allow_rate_limit", allow)
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(paper_api._fetch_verified_market_snapshot(
            customer_id=501,
            symbol="BTC/USDT",
            timeframe="1d",
            market_data=market_data,
            market_request_type=lambda **kwargs: kwargs,
            earliest_required_at=now - timedelta(days=12),
        ))

    assert rejected.value.status_code == 409
    assert "pre-fill volatility history older than the supported 30-day candle window" in rejected.value.detail
    assert calls == []
