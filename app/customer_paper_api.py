"""Customer-authenticated, isolated spot paper-order API.

This router never calls a broker, never writes the public trading ledger, and never
accepts a caller-supplied fill price. All fills come from validated completed CCXT spot
bars and are committed with the customer-paper journal. The feature is disabled until
the database/schema preflight and independent paper-only safety gates pass.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_EVEN, localcontext
from typing import Any, Awaitable, Callable, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

from .config import settings
from .db import AppState, SessionLocal
from .customer_paper_ledger import (
    CustomerPaperIdempotencyConflict,
    CustomerPaperLedgerBlocked,
    get_customer_paper_balances,
)
from .customer_paper_lifecycle import (
    CustomerPaperOrderConflict,
    cancel_customer_paper_order,
    expire_customer_paper_order,
    get_customer_paper_fill_for_bar,
    get_customer_paper_order,
    get_customer_paper_positions,
    list_open_customer_paper_orders,
    record_customer_paper_fill,
    register_customer_paper_market_bar,
    submit_customer_paper_order,
)
from .trading_core import PROFILES


_TIMEFRAME_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400,
}
_VIRTUAL_STARTING_CASH = Decimal("10000")
_DECIMAL_QUANTUM = Decimal("0.000000000000000001")


class CustomerPaperOrderRequest(BaseModel):
    client_order_id: str = Field(min_length=1, max_length=180)
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,15}/USDT$")
    side: Literal["BUY", "SELL"]
    quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: Decimal | None = Field(default=None, gt=0, max_digits=38, decimal_places=18)
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"] = "1h"

    @model_validator(mode="after")
    def validate_order_type(self):
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("limit_price is required for LIMIT orders")
        if self.order_type == "MARKET" and self.limit_price is not None:
            raise ValueError("limit_price must be omitted for MARKET orders")
        return self


class CustomerPaperCancelRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=150)


def _decimal_text(value: Decimal | str | int | float) -> str:
    with localcontext() as context:
        context.prec = 50
        number = Decimal(str(value)).quantize(_DECIMAL_QUANTUM, rounding=ROUND_HALF_EVEN)
    if not number.is_finite():
        raise ValueError("decimal value must be finite")
    return format(number, "f")


def _timestamp(value: Any, name: str) -> datetime:
    try:
        if isinstance(value, datetime):
            result = value
        elif hasattr(value, "to_pydatetime"):
            result = value.to_pydatetime()
        else:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(409, f"Verified market data has an invalid {name} timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise HTTPException(409, f"Verified market data {name} timestamp must be timezone-aware")
    return result.astimezone(timezone.utc)


def _market_bar_context(df: Any, *, symbol: str, timeframe: str) -> dict[str, Any]:
    """Fail closed unless the latest candle is fresh, complete, and verified Binance spot OHLCV."""
    required = {"open", "high", "low", "close", "volume"}
    if df is None or len(df) < 22 or not required.issubset(set(getattr(df, "columns", ()))):
        raise HTTPException(409, "Not enough OHLCV history to verify a customer paper order")
    provenance = dict(getattr(df, "attrs", {}).get("data_provenance") or {})
    digest = str(provenance.get("sha256") or "").lower()
    source = str(provenance.get("source") or "")
    if (
        source != "ccxt_ohlcv"
        or str(provenance.get("asset_class") or "").lower() != "crypto"
        or str(provenance.get("exchange") or "").lower() != "binance"
        or str(provenance.get("symbol") or "").upper() != symbol
        or str(provenance.get("timeframe") or "") != timeframe
        or str(provenance.get("market_type") or "").lower() != "spot"
        or str(provenance.get("volume_unit") or "").lower() != "base_asset"
        or provenance.get("volume_unit_verified") is not True
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise HTTPException(409, "Customer paper fills require verified CCXT spot base-asset volume provenance")
    try:
        row_count = int(provenance.get("row_count") or 0)
        fetched_at = _timestamp(provenance.get("fetched_at_utc"), "fetch")
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, "Market-data provenance is invalid") from exc
    if row_count != len(df):
        raise HTTPException(409, "Market-data row count does not match its provenance manifest")
    now = datetime.now(timezone.utc)
    stale_seconds = max(60, int(getattr(settings, "stale_data_minutes", 15)) * 60)
    if (now - fetched_at).total_seconds() < -30 or (now - fetched_at).total_seconds() > stale_seconds:
        raise HTTPException(409, "Customer paper execution refused stale market-data provenance")
    bar_at = _timestamp(df.index[-1], "bar")
    bar_seconds = _TIMEFRAME_SECONDS[timeframe]
    bar_close_at = bar_at + timedelta(seconds=bar_seconds)
    if bar_at > now or bar_close_at > now:
        raise HTTPException(409, "Customer paper orders advance only on completed market bars")
    if (now - bar_close_at).total_seconds() > max(60, bar_seconds * 2):
        raise HTTPException(409, "Customer paper execution refused a stale completed bar")

    values: dict[str, Decimal] = {}
    for name in ("open", "high", "low", "close", "volume"):
        try:
            value = Decimal(str(df[name].iloc[-1]))
        except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(409, f"Market bar has an invalid {name}") from exc
        if not value.is_finite() or (value <= 0 if name != "volume" else value < 0):
            raise HTTPException(409, f"Market bar has an invalid {name}")
        values[name] = value
    if (
        values["high"] < max(values["open"], values["close"], values["low"])
        or values["low"] > min(values["open"], values["close"], values["high"])
    ):
        raise HTTPException(409, "Market bar OHLC values are inconsistent")
    stable_bar = {
        "venue": "binance",
        "market_type": "spot",
        "volume_unit": "base_asset",
        "symbol": symbol,
        "timeframe": timeframe,
        "bar_at": bar_at.isoformat(),
        **{name: _decimal_text(values[name]) for name in ("open", "high", "low", "close", "volume")},
    }
    bar_digest = hashlib.sha256(
        json.dumps(stable_bar, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return {
        **stable_bar,
        "timestamp": bar_at,
        "bar_seconds": bar_seconds,
        "values": values,
        "dataset_sha256": digest,
        "source": source,
        "fetched_at_utc": fetched_at.isoformat(),
        "bar_data_sha256": bar_digest,
    }


def _stable_draw(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:13], 16) / float(16**13)


def _paper_feature_flags() -> None:
    """No paper API may run under any live-money, custody, or payout configuration."""
    if (
        not settings.paper_trading
        or settings.live_trading_enabled
        or getattr(settings, "customer_live_trading_enabled", False)
        or settings.payout_live_enabled
        or not settings.broker_sandbox
        or getattr(settings, "deriv_live_enabled", False)
        or getattr(settings, "usdt_tron_enabled", False)
        or not settings.customer_paper_ledger_enabled
        or not settings.customer_paper_shared_database_enabled
        or not settings.customer_paper_database_url
    ):
        raise HTTPException(409, "Customer paper ledger is disabled by an independent safety or provisioning gate")


def _handle_paper_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (CustomerPaperOrderConflict, CustomerPaperIdempotencyConflict)):
        return HTTPException(409, str(exc))
    if isinstance(exc, CustomerPaperLedgerBlocked):
        return HTTPException(503, str(exc))
    if isinstance(exc, (ValueError, InvalidOperation)):
        return HTTPException(422, str(exc))
    return HTTPException(503, "Customer paper ledger operation could not be completed")


def build_customer_paper_router(
    *,
    get_customer: Callable[..., Awaitable[Any]],
    market_data: Callable[..., Any],
    market_request_type: type,
    state_guard: Callable[[], Awaitable[None]] | None = None,
    group_mark_price_loader: Callable[..., Awaitable[Any]] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/customer/paper", tags=["customer-paper"])

    async def _profile(authorization: str | None) -> Any:
        if not authorization:
            raise HTTPException(401, "Customer authentication is required")
        async with SessionLocal() as db:
            profile, _claims = await get_customer(authorization, db, require_aal2=True)
        if not getattr(profile, "id", None):
            raise HTTPException(401, "Customer authentication is invalid")
        return profile

    async def _guard() -> None:
        _paper_feature_flags()
        if state_guard is None:
            async with SessionLocal() as db:
                state = await db.get(AppState, 1)
                if not state or state.kill_switch:
                    raise HTTPException(409, "Customer paper trading is halted by platform risk state")
        else:
            await state_guard()

    async def _fetch_bar(symbol: str, timeframe: str) -> tuple[Any, dict[str, Any]]:
        # The execution model needs the most recent closed bar and enough history for
        # its 20-return volatility estimate. Do not fetch a fixed 30-day window:
        # that meant 43,200 candles / ~44 sequential CCXT pages for a 1-minute order.
        timeframe_seconds = _TIMEFRAME_SECONDS.get(str(timeframe))
        if not timeframe_seconds:
            raise HTTPException(422, "Unsupported paper-order timeframe")
        required_bars = 25
        days = max(1, (required_bars * timeframe_seconds + 86_399) // 86_400)
        req = market_request_type(
            asset="crypto",
            symbol=symbol,
            exchange="binance",
            timeframe=timeframe,
            days=days,
        )
        df = await asyncio.to_thread(market_data, req)
        return df, _market_bar_context(df, symbol=symbol, timeframe=timeframe)

    async def _order_detail(customer_id: int, client_order_id: str) -> dict[str, Any]:
        order = await get_customer_paper_order(customer_id=customer_id, client_order_id=client_order_id)
        if order is None:
            raise HTTPException(404, "Customer paper order not found")
        return order

    @router.post("/account/initialize")
    async def initialize_paper_account(authorization: str | None = Header(default=None)):
        profile = await _profile(authorization)
        await _guard()
        try:
            # Use the autonomous controller's exact seed key, configured amount and
            # currency. Concurrent API/controller initialization must be one event.
            from .customer_paper_flow import ensure_customer_paper_account

            before = await get_customer_paper_balances(customer_id=profile.id)
            already_initialized = any(
                str(row.get("account_code") or "") == "PAPER_CASH"
                for row in before
            )
            currency = str(settings.customer_paper_initial_currency or "").strip().upper()
            account = await ensure_customer_paper_account(
                customer_id=profile.id, currency=currency
            )
            balances = await get_customer_paper_balances(customer_id=profile.id)
            return {
                "status": "ALREADY_INITIALIZED" if already_initialized else "INITIALIZED",
                "virtual_only": True,
                "currency": currency,
                "starting_cash": account["initial_balance"],
                "balances": balances,
                "real_funds_used": False,
                "note": "This is a simulated paper allocation, not a deposit or withdrawable balance.",
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise _handle_paper_error(exc) from exc

    @router.get("/balances")
    async def customer_paper_balances(authorization: str | None = Header(default=None)):
        profile = await _profile(authorization)
        await _guard()
        try:
            return {"virtual_only": True, "balances": await get_customer_paper_balances(customer_id=profile.id)}
        except Exception as exc:
            raise _handle_paper_error(exc) from exc

    @router.get("/positions")
    async def customer_paper_positions(authorization: str | None = Header(default=None)):
        profile = await _profile(authorization)
        await _guard()
        try:
            return {"virtual_only": True, "positions": await get_customer_paper_positions(customer_id=profile.id)}
        except Exception as exc:
            raise _handle_paper_error(exc) from exc

    @router.get("/orders")
    async def customer_paper_open_orders(authorization: str | None = Header(default=None)):
        profile = await _profile(authorization)
        await _guard()
        try:
            return {"virtual_only": True, "orders": await list_open_customer_paper_orders(customer_id=profile.id)}
        except Exception as exc:
            raise _handle_paper_error(exc) from exc

    @router.post("/orders")
    async def submit_paper_order(
        req: CustomerPaperOrderRequest,
        authorization: str | None = Header(default=None),
    ):
        profile = await _profile(authorization)
        await _guard()
        try:
            existing = await get_customer_paper_order(
                customer_id=profile.id, client_order_id=req.client_order_id
            )
            if existing is not None:
                expected = (
                    existing["venue"] == "binance"
                    and existing["market_type"] == "spot"
                    and existing["symbol"] == req.symbol
                    and existing["side"] == req.side
                    and existing["quantity"] == _decimal_text(req.quantity)
                    and existing["currency"] == "USDT"
                    and existing["order_type"] == req.order_type
                    and existing["limit_price"] == (_decimal_text(req.limit_price) if req.limit_price is not None else None)
                    and existing.get("metadata", {}).get("timeframe") == req.timeframe
                )
                if not expected:
                    raise HTTPException(409, "client_order_id is already bound to a different immutable paper order")
                return {"status": "REPLAYED", "virtual_only": True, "order": existing}

            balances = await get_customer_paper_balances(customer_id=profile.id)
            if not any(row["account_code"] == "PAPER_CASH" for row in balances):
                raise HTTPException(409, "Initialize the virtual paper account before submitting orders")
            _df, bar = await _fetch_bar(req.symbol, req.timeframe)
            result = await submit_customer_paper_order(
                customer_id=profile.id,
                client_order_id=req.client_order_id,
                venue="binance",
                market_type="spot",
                symbol=req.symbol,
                side=req.side,
                quantity=_decimal_text(req.quantity),
                currency="USDT",
                order_type=req.order_type,
                limit_price=_decimal_text(req.limit_price) if req.limit_price is not None else None,
                metadata={
                    "exchange": "binance",
                    "timeframe": req.timeframe,
                    "submission_bar_at": bar["timestamp"].isoformat(),
                    "submission_bar_sha256": bar["bar_data_sha256"],
                    "dataset_sha256": bar["dataset_sha256"],
                    "data_source": bar["source"],
                    "volume_unit": "base_asset",
                    "market_type": "spot",
                    "execution_model": "completed_bar_volume_capped_heuristic_v1",
                    "execution_owner": "manual_customer_api",
                    "execution_cost_evidence": "UNVALIDATED_HEURISTIC",
                },
            )
            return {
                **result,
                "virtual_only": True,
                "fill_note": "Orders are not filled on the submission bar; advance only against a later verified completed bar.",
                "order": await _order_detail(profile.id, req.client_order_id),
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise _handle_paper_error(exc) from exc

    @router.get("/orders/{client_order_id}")
    async def get_paper_order(client_order_id: str, authorization: str | None = Header(default=None)):
        profile = await _profile(authorization)
        await _guard()
        try:
            return {"virtual_only": True, "order": await _order_detail(profile.id, client_order_id)}
        except HTTPException:
            raise
        except Exception as exc:
            raise _handle_paper_error(exc) from exc

    @router.post("/orders/{client_order_id}/advance")
    async def advance_paper_order(client_order_id: str, authorization: str | None = Header(default=None)):
        profile = await _profile(authorization)
        await _guard()
        try:
            order = await _order_detail(profile.id, client_order_id)
            if order["status"] not in {"SUBMITTED", "PARTIALLY_FILLED"}:
                return {"status": "TERMINAL", "virtual_only": True, "order": order}
            order_venue = str(order.get("venue") or "").strip().lower()
            if order_venue != "binance" or str(order.get("market_type") or "").strip().lower() != "spot":
                raise HTTPException(409, "Manual paper-order advancement supports Binance spot orders only")
            metadata = order.get("metadata") or {}
            timeframe = str(metadata.get("timeframe") or "")
            if timeframe not in _TIMEFRAME_SECONDS or metadata.get("execution_model") != "completed_bar_volume_capped_heuristic_v1":
                raise HTTPException(409, "Persisted paper order lacks a supported execution context")
            df, bar = await _fetch_bar(order["symbol"], timeframe)
            submitted_at = _timestamp(metadata.get("submission_bar_at"), "submission bar")
            if bar["timestamp"] <= submitted_at:
                return {
                    "status": "WAITING_FOR_NEXT_COMPLETED_BAR",
                    "virtual_only": True,
                    "bar_at": bar["timestamp"].isoformat(),
                    "order": order,
                }
            elapsed_bars = int((bar["timestamp"] - submitted_at).total_seconds() // _TIMEFRAME_SECONDS[timeframe])
            if elapsed_bars < max(1, int(settings.paper_fill_latency_bars)):
                return {
                    "status": "WAITING_FOR_FILL_LATENCY",
                    "virtual_only": True,
                    "elapsed_bars": elapsed_bars,
                    "order": order,
                }
            budget = await register_customer_paper_market_bar(
                venue=order_venue,
                symbol=order["symbol"],
                timeframe=timeframe,
                bar_at=bar["timestamp"],
                bar_data_sha256=bar["bar_data_sha256"],
                base_volume=_decimal_text(bar["values"]["volume"]),
                participation_rate=_decimal_text(Decimal(str(settings.paper_max_bar_volume_participation))),
            )
            prior_fill = await get_customer_paper_fill_for_bar(
                customer_id=profile.id,
                client_order_id=client_order_id,
                bar_key=budget["bar_key"],
            )
            if prior_fill is not None:
                return {
                    "status": "REPLAYED",
                    "virtual_only": True,
                    "bar_at": bar["timestamp"].isoformat(),
                    "bar_key": budget["bar_key"],
                    "fill": prior_fill,
                    "order": await _order_detail(profile.id, client_order_id),
                }
            values = bar["values"]
            limit = Decimal(order["limit_price"]) if order["limit_price"] is not None else None
            side = order["side"]
            reference_price: Decimal | None = None
            if order["order_type"] == "MARKET":
                reference_price = values["open"]
            elif side == "BUY" and values["low"] <= limit:
                reference_price = min(values["open"], limit)
            elif side == "SELL" and values["high"] >= limit:
                reference_price = max(values["open"], limit)

            if reference_price is None:
                attempt = "LIMIT_NOT_TRIGGERED"
            elif Decimal(budget["available"]) <= 0:
                attempt = "WAITING_FOR_BAR_CAPACITY"
            elif _stable_draw(client_order_id, budget["bar_key"], "rejection") < min(1.0, max(0.0, float(settings.paper_rejection_probability))):
                attempt = "DETERMINISTIC_BAR_REJECTION"
            elif _stable_draw(client_order_id, budget["bar_key"], "queue") > min(1.0, max(0.0, float(settings.paper_queue_fill_probability))):
                attempt = "DETERMINISTIC_QUEUE_MISS"
            else:
                execution_profile = PROFILES.get("crypto")
                if execution_profile is None:
                    raise HTTPException(503, "Crypto paper execution profile is not configured")

                group_mark_prices = None
                if str(side or "").upper() == "BUY" and group_mark_price_loader is not None:
                    timeframe_seconds = _TIMEFRAME_SECONDS.get(str(timeframe))
                    if not timeframe_seconds:
                        raise HTTPException(422, "Unsupported paper-order timeframe")
                    mark_days = max(1, (25 * timeframe_seconds + 86_399) // 86_400)
                    mark_req = market_request_type(
                        asset="crypto",
                        symbol=str(order["symbol"]),
                        exchange=order_venue,
                        timeframe=timeframe,
                        days=mark_days,
                    )
                    try:
                        # Candle-only order state is not a current market mark. Fetch fresh
                        # validated quotes for every open holding in the candidate's group.
                        group_mark_prices = await group_mark_price_loader(
                            profile.id, mark_req, None
                        )
                    except Exception as mark_exc:
                        raise HTTPException(
                            409, "Current paper correlation-group marks could not be verified"
                        ) from mark_exc

                remaining_quantity = Decimal(str(order.get("remaining_quantity") or order["quantity"]))
                fill_quantity = min(remaining_quantity, max(Decimal("0"), Decimal(str(budget["available"]))))
                if side == "SELL":
                    positions = await get_customer_paper_positions(customer_id=profile.id)
                    held = sum(
                        (
                            Decimal(str(position.get("quantity") or "0"))
                            for position in positions
                            if str(position.get("venue") or "").lower() == str(order.get("venue") or "").lower()
                            and str(position.get("symbol") or "").upper() == str(order["symbol"]).upper()
                            and Decimal(str(position.get("quantity") or "0")) > 0
                        ),
                        Decimal("0"),
                    )
                    if held <= 0:
                        await expire_customer_paper_order(
                            customer_id=profile.id,
                            client_order_id=client_order_id,
                            idempotency_key="paper-no-position-api:" + hashlib.sha256(client_order_id.encode("utf-8")).hexdigest()[:40],
                        )
                        return {
                            "status": "EXPIRED_NO_OPEN_POSITION",
                            "virtual_only": True,
                            "bar_at": bar["timestamp"].isoformat(),
                            "bar_key": budget["bar_key"],
                            "fill": {"status": "EXPIRED"},
                            "order": await _order_detail(profile.id, client_order_id),
                        }
                    fill_quantity = min(fill_quantity, held)

                returns = df["close"].astype(float).pct_change().replace([float("inf"), float("-inf")], float("nan")).dropna().tail(20)
                volatility = float(returns.std(ddof=0)) if len(returns) >= 2 else 0.0
                if not math.isfinite(volatility) or volatility < 0:
                    raise HTTPException(409, "Market volatility estimate is invalid")
                participation_for_impact = max(
                    0.0,
                    float(fill_quantity / max(values["volume"], Decimal("1e-18"))),
                )
                impact = (
                    float(settings.paper_market_impact_coefficient)
                    * volatility
                    * math.sqrt(participation_for_impact)
                    * 10000.0
                )
                impact = min(max(0.0, float(settings.paper_max_market_impact_bps)), max(0.0, impact))
                slippage_bps = max(0.0, float(execution_profile.slippage_bps) + impact)
                # A limit fill cannot be priced worse than its limit; market orders absorb adverse slippage.
                final_price = reference_price
                if order["order_type"] == "MARKET":
                    multiplier = Decimal("1") + Decimal(str(slippage_bps)) / Decimal("10000")
                    if side == "SELL":
                        multiplier = Decimal("1") - Decimal(str(slippage_bps)) / Decimal("10000")
                    final_price = reference_price * multiplier
                if final_price <= 0:
                    raise HTTPException(409, "Paper cost model produced a non-positive fill price")

                price_text = _decimal_text(final_price)
                fee_bps = max(Decimal("0"), Decimal(str(execution_profile.taker_bps)))
                if side == "BUY":
                    balances = await get_customer_paper_balances(customer_id=profile.id)
                    cash = sum(
                        (
                            Decimal(str(row.get("balance") or "0"))
                            for row in balances
                            if str(row.get("account_code") or "") == "PAPER_CASH"
                            and str(row.get("currency") or "").upper() == "USDT"
                        ),
                        Decimal("0"),
                    )
                    unit_cost = Decimal(price_text) * (Decimal("1") + fee_bps / Decimal("10000"))
                    with localcontext() as context:
                        context.prec = 50
                        affordable = max(Decimal("0"), cash / unit_cost).quantize(
                            _DECIMAL_QUANTUM, rounding=ROUND_DOWN
                        )
                    fill_quantity = min(fill_quantity, affordable)
                    if fill_quantity <= 0:
                        await expire_customer_paper_order(
                            customer_id=profile.id,
                            client_order_id=client_order_id,
                            idempotency_key="paper-unaffordable-api:" + hashlib.sha256(client_order_id.encode("utf-8")).hexdigest()[:40],
                        )
                        return {
                            "status": "EXPIRED_INSUFFICIENT_PAPER_CASH",
                            "virtual_only": True,
                            "bar_at": bar["timestamp"].isoformat(),
                            "bar_key": budget["bar_key"],
                            "fill": {"status": "EXPIRED"},
                            "order": await _order_detail(profile.id, client_order_id),
                        }
                if fill_quantity <= 0:
                    attempt = "WAITING_FOR_BAR_CAPACITY"
                else:
                    fee = fill_quantity * Decimal(price_text) * fee_bps / Decimal("10000")
                    fill_key = "bar-fill:" + hashlib.sha256(f"{client_order_id}|{budget['bar_key']}".encode("utf-8")).hexdigest()
                    try:
                        fill = await record_customer_paper_fill(
                            customer_id=profile.id,
                            client_order_id=client_order_id,
                            idempotency_key=fill_key,
                            bar_key=budget["bar_key"],
                            quantity=_decimal_text(fill_quantity),
                            price=price_text,
                            fee_amount=_decimal_text(fee),
                            enforce_market_bar_budget=True,
                            group_mark_prices=group_mark_prices,
                        )
                    except CustomerPaperOrderConflict as exc:
                        # A concurrent fill can invalidate the earlier cash pre-read.
                        # Re-read virtual cash and retry once at the affordable size.
                        if side == "BUY" and "overdraw" in str(exc).lower():
                            fresh_balances = await get_customer_paper_balances(customer_id=profile.id)
                            fresh_cash = sum(
                                (
                                    Decimal(str(row.get("balance") or "0"))
                                    for row in fresh_balances
                                    if str(row.get("account_code") or "") == "PAPER_CASH"
                                    and str(row.get("currency") or "").upper()
                                    == str(order.get("currency") or "USDT").upper()
                                ),
                                Decimal("0"),
                            )
                            unit_cost = Decimal(price_text) * (Decimal("1") + fee_bps / Decimal("10000"))
                            with localcontext() as context:
                                context.prec = 50
                                retry_affordable = max(Decimal("0"), fresh_cash / unit_cost).quantize(
                                    _DECIMAL_QUANTUM, rounding=ROUND_DOWN
                                )
                            retry_quantity = min(fill_quantity, retry_affordable)
                            if retry_quantity <= 0:
                                await expire_customer_paper_order(
                                    customer_id=profile.id,
                                    client_order_id=client_order_id,
                                    idempotency_key="paper-cash-race-empty-api:" + hashlib.sha256(
                                        f"{client_order_id}|{budget['bar_key']}".encode("utf-8")
                                    ).hexdigest()[:40],
                                )
                                return {
                                    "status": "EXPIRED_INSUFFICIENT_PAPER_CASH",
                                    "virtual_only": True,
                                    "bar_at": bar["timestamp"].isoformat(),
                                    "bar_key": budget["bar_key"],
                                    "fill": {"status": "EXPIRED"},
                                    "order": await _order_detail(profile.id, client_order_id),
                                }
                            retry_fee = retry_quantity * Decimal(price_text) * fee_bps / Decimal("10000")
                            try:
                                fill = await record_customer_paper_fill(
                                    customer_id=profile.id,
                                    client_order_id=client_order_id,
                                    idempotency_key=fill_key,
                                    bar_key=budget["bar_key"],
                                    quantity=_decimal_text(retry_quantity),
                                    price=price_text,
                                    fee_amount=_decimal_text(retry_fee),
                                    enforce_market_bar_budget=True,
                                    group_mark_prices=group_mark_prices,
                                )
                                fill_quantity = retry_quantity
                            except CustomerPaperOrderConflict as retry_exc:
                                if "overdraw" in str(retry_exc).lower():
                                    ttl_reached = elapsed_bars >= max(
                                        1, int(settings.paper_order_ttl_bars)
                                    )
                                    retry_order = await _order_detail(profile.id, client_order_id)
                                    if ttl_reached and retry_order["status"] in {
                                        "SUBMITTED", "PARTIALLY_FILLED"
                                    }:
                                        await expire_customer_paper_order(
                                            customer_id=profile.id,
                                            client_order_id=client_order_id,
                                            idempotency_key=f"paper-ttl:{budget['bar_key']}",
                                        )
                                        retry_order = await _order_detail(profile.id, client_order_id)
                                        return {
                                            "status": "EXPIRED_TTL",
                                            "virtual_only": True,
                                            "bar_at": bar["timestamp"].isoformat(),
                                            "bar_key": budget["bar_key"],
                                            "fill": {"status": "EXPIRED"},
                                            "order": retry_order,
                                        }
                                    return {
                                        "status": "WAITING_FOR_PAPER_CASH",
                                        "virtual_only": True,
                                        "bar_at": bar["timestamp"].isoformat(),
                                        "bar_key": budget["bar_key"],
                                        "fill": {"status": "NOT_FILLED"},
                                        "order": retry_order,
                                    }
                                raise
                        else:
                            raise
                    fresh_order = await _order_detail(profile.id, client_order_id)
                    if (
                        elapsed_bars >= max(1, int(settings.paper_order_ttl_bars))
                        and fresh_order["status"] == "PARTIALLY_FILLED"
                    ):
                        await expire_customer_paper_order(
                            customer_id=profile.id,
                            client_order_id=client_order_id,
                            idempotency_key=f"paper-ttl:{budget['bar_key']}",
                        )
                        fresh_order = await _order_detail(profile.id, client_order_id)
                    return {
                        "status": fill["status"],
                        "virtual_only": True,
                        "bar_at": bar["timestamp"].isoformat(),
                        "bar_key": budget["bar_key"],
                        "fill": fill,
                        "order": fresh_order,
                        "execution_cost_evidence": "UNVALIDATED_HEURISTIC",
                    }

            if elapsed_bars >= max(1, int(settings.paper_order_ttl_bars)):
                await expire_customer_paper_order(
                    customer_id=profile.id,
                    client_order_id=client_order_id,
                    idempotency_key=f"paper-ttl:{budget['bar_key']}",
                )
                order = await _order_detail(profile.id, client_order_id)
                return {"status": "EXPIRED", "attempt": attempt, "virtual_only": True, "order": order}
            return {
                "status": attempt,
                "virtual_only": True,
                "bar_at": bar["timestamp"].isoformat(),
                "bar_key": budget["bar_key"],
                "bar_capacity": budget["capacity"],
                "bar_available": budget["available"],
                "execution_cost_evidence": "UNVALIDATED_HEURISTIC",
                "order": order,
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise _handle_paper_error(exc) from exc

    @router.post("/orders/{client_order_id}/cancel")
    async def cancel_paper_order(
        client_order_id: str,
        req: CustomerPaperCancelRequest,
        authorization: str | None = Header(default=None),
    ):
        profile = await _profile(authorization)
        await _guard()
        try:
            result = await cancel_customer_paper_order(
                customer_id=profile.id,
                client_order_id=client_order_id,
                idempotency_key=req.idempotency_key,
            )
            return {"virtual_only": True, **result, "order": await _order_detail(profile.id, client_order_id)}
        except HTTPException:
            raise
        except Exception as exc:
            raise _handle_paper_error(exc) from exc

    return router
