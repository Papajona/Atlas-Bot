"""Customer-scoped spot paper order flow over the isolated append-only ledger.

This module never calls broker adapters or the real-money ledger. It accepts only
server-fetched, completed CCXT spot candles plus a fresh server quote. Market fills
are booked at the fresh ask/bid with explicitly labelled, uncalibrated paper cost
assumptions. This is a paper simulator, not venue-calibrated execution evidence.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from .config import settings
from .customer_paper_ledger import (
    CustomerPaperLedgerBlocked,
    get_customer_paper_balances,
    post_customer_paper_journal,
)
from .customer_paper_lifecycle import (
    get_customer_paper_order,
    get_customer_paper_positions,
    list_open_customer_paper_orders,
    record_customer_paper_fill,
    register_customer_paper_market_bar,
    submit_customer_paper_order,
)


class CustomerPaperFlowBlocked(RuntimeError):
    """The customer paper flow cannot safely execute or recover this request."""


def _finite_decimal(value: object, name: str, *, non_negative: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise CustomerPaperFlowBlocked(f"{name} is not a valid decimal") from exc
    if not result.is_finite() or (non_negative and result < 0):
        raise CustomerPaperFlowBlocked(f"{name} must be finite and non-negative")
    return result


def _quote_currency(symbol: str) -> str:
    parts = str(symbol or "").strip().upper().split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise CustomerPaperFlowBlocked("Customer paper supports BASE/QUOTE spot symbols only")
    return parts[1]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_BAR_QUANTUM = Decimal("0.000000000000000001")


def _bar_decimal_text(value: object, name: str, *, non_negative: bool = False) -> str:
    parsed = _finite_decimal(value, name, non_negative=non_negative)
    if (not non_negative and parsed <= 0) or (non_negative and parsed < 0):
        raise CustomerPaperFlowBlocked(f"{name} has an invalid sign")
    with localcontext() as context:
        context.prec = 80
        normalized = parsed.quantize(_BAR_QUANTUM, rounding=ROUND_HALF_EVEN)
    return format(normalized, "f")


def _stable_completed_bar_digest(req, df, bar_timestamp: str) -> tuple[str, Decimal]:
    """Build the same immutable OHLCV identity used by the manual paper API."""
    names = ("open", "high", "low", "close", "volume")
    values = {
        name: Decimal(_bar_decimal_text(df[name].iloc[-1], f"completed-bar {name}", non_negative=name == "volume"))
        for name in names
    }
    if (
        values["high"] < max(values["open"], values["close"], values["low"])
        or values["low"] > min(values["open"], values["close"], values["high"])
    ):
        raise CustomerPaperFlowBlocked("Completed bar has inconsistent OHLC values")
    stable_bar = {
        "venue": str(req.exchange).strip().lower(),
        "market_type": "spot",
        "volume_unit": "base_asset",
        "symbol": str(req.symbol),
        "timeframe": str(req.timeframe),
        "bar_at": bar_timestamp,
        **{name: format(values[name], "f") for name in names},
    }
    digest = hashlib.sha256(
        json.dumps(stable_bar, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return digest, values["volume"]


def _json_mapping(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def validate_customer_paper_market(req, df, market_context: Mapping) -> dict:
    """Fail closed unless both data provenance and a fresh active-spot quote agree."""
    if not bool(settings.customer_paper_ledger_enabled):
        raise CustomerPaperFlowBlocked("CUSTOMER_PAPER_LEDGER_ENABLED is false")
    if str(getattr(req, "asset", "")).lower() != "crypto":
        raise CustomerPaperFlowBlocked("Customer paper lifecycle currently supports crypto spot only")
    if df is None or getattr(df, "empty", True):
        raise CustomerPaperFlowBlocked("Completed market candles are unavailable")
    provenance = _json_mapping(getattr(df, "attrs", {}).get("data_provenance"))
    expected = {
        "source": "ccxt_ohlcv",
        "symbol": str(req.symbol),
        "exchange": str(req.exchange).lower(),
        "timeframe": str(req.timeframe),
        "asset_class": "crypto",
        "market_type": "spot",
        "volume_unit": "base_asset",
        "volume_unit_verified": True,
    }
    for key, want in expected.items():
        got = provenance.get(key)
        if (str(got).lower() if isinstance(want, str) else got) != (str(want).lower() if isinstance(want, str) else want):
            raise CustomerPaperFlowBlocked(f"Market data provenance mismatch: {key}")
    digest = str(provenance.get("sha256") or "").lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise CustomerPaperFlowBlocked("Market data provenance SHA-256 is missing or malformed")
    if int(provenance.get("row_count") or 0) != len(df):
        raise CustomerPaperFlowBlocked("Market data row count does not match its provenance")
    if not getattr(df.index, "is_monotonic_increasing", False) or getattr(df.index, "has_duplicates", True):
        raise CustomerPaperFlowBlocked("Market data timestamps must be strictly increasing and unique")
    required_columns = {"open", "high", "low", "close", "volume"}
    if not required_columns.issubset(set(df.columns)) or len(df) < 21:
        raise CustomerPaperFlowBlocked("At least 21 completed OHLCV spot bars are required")
    quote_currency = _quote_currency(req.symbol)
    configured_currency = str(settings.customer_paper_initial_currency or "").strip().upper()
    if not configured_currency or quote_currency != configured_currency:
        raise CustomerPaperFlowBlocked(
            f"Customer paper uses one virtual quote currency ({configured_currency or 'unset'}); "
            "the market quote currency must match"
        )

    context = dict(market_context)
    if (
        str(context.get("market_type") or "").lower() != "spot"
        or str(context.get("exchange") or "").lower() != str(req.exchange).lower()
        or str(context.get("symbol") or "") != str(req.symbol)
        or str(context.get("ohlcv_sha256") or "").lower() != digest
        or str(context.get("context_source") or "") != "server_ccxt_closed_spot_ohlcv_and_ticker"
    ):
        raise CustomerPaperFlowBlocked("Fresh server market context does not match the verified spot candles")
    bid = _finite_decimal(context.get("bid"), "bid")
    ask = _finite_decimal(context.get("ask"), "ask")
    if bid <= 0 or ask < bid:
        raise CustomerPaperFlowBlocked("Fresh server bid/ask is invalid")
    quote_age = _finite_decimal(context.get("quote_age_seconds"), "quote_age_seconds")
    if quote_age > Decimal("5"):
        raise CustomerPaperFlowBlocked("Fresh server quote is older than five seconds")
    quote_timestamp = str(context.get("quote_timestamp_utc") or "")
    try:
        quote_dt = datetime.fromisoformat(quote_timestamp.replace("Z", "+00:00"))
        bar_dt = datetime.fromisoformat(str(df.index[-1].isoformat()).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CustomerPaperFlowBlocked("Quote or completed-bar timestamp is invalid") from exc
    if quote_dt.tzinfo is None or bar_dt.tzinfo is None or quote_dt <= bar_dt:
        raise CustomerPaperFlowBlocked("Fresh quote must follow the latest completed signal bar")
    bar_timestamp = bar_dt.astimezone(timezone.utc).isoformat()
    bar_data_sha256, base_volume = _stable_completed_bar_digest(req, df, bar_timestamp)
    return {
        "currency": quote_currency,
        "data_sha256": digest,
        "bar_timestamp": bar_timestamp,
        "bar_data_sha256": bar_data_sha256,
        "base_volume": base_volume,
        "quote_timestamp": quote_dt.astimezone(timezone.utc).isoformat(),
        "bid": bid,
        "ask": ask,
        "spread_bps": _finite_decimal(context.get("spread_bps"), "spread_bps", non_negative=True),
        "provenance": provenance,
        "market_context": context,
    }


async def ensure_customer_paper_account(customer_id: int, currency: str) -> dict:
    """Create one idempotent virtual seed only when the customer's paper ledger is empty."""
    if isinstance(customer_id, bool) or not isinstance(customer_id, int) or customer_id <= 0:
        raise CustomerPaperFlowBlocked("Customer identity is invalid")
    currency = str(currency or "").strip().upper()
    configured_currency = str(settings.customer_paper_initial_currency or "").strip().upper()
    if not currency or currency != configured_currency:
        raise CustomerPaperFlowBlocked("Paper seed currency does not match the configured virtual quote currency")
    initial = _finite_decimal(settings.customer_paper_initial_balance, "customer_paper_initial_balance")
    if initial <= 0 or initial.adjusted() + 1 > 20:
        raise CustomerPaperFlowBlocked("Configured virtual paper starting balance must be positive and numeric(38,18)-safe")
    try:
        balances = await get_customer_paper_balances(customer_id=customer_id)
        if not balances:
            await post_customer_paper_journal(
                customer_id=customer_id,
                idempotency_key="initial-customer-paper-seed-v1",
                event_type="PAPER_SEED",
                currency=currency,
                lines=[
                    {"account_code": "PAPER_CASH", "signed_amount": str(initial)},
                    {"account_code": "PAPER_EQUITY", "signed_amount": str(-initial)},
                ],
                reference="configured-virtual-paper-starting-balance",
                metadata={
                    "seed_source": "CUSTOMER_PAPER_INITIAL_BALANCE",
                    "real_customer_funds_used": False,
                    "configuration_currency": currency,
                },
            )
            balances = await get_customer_paper_balances(customer_id=customer_id)
    except (CustomerPaperLedgerBlocked, ValueError) as exc:
        raise CustomerPaperFlowBlocked(str(exc)) from exc
    currencies = {str(row.get("currency") or "").upper() for row in balances}
    by_code = {
        str(row.get("account_code") or ""): Decimal(str(row.get("balance") or "0"))
        for row in balances
        if str(row.get("currency") or "").upper() == currency
    }
    if currencies != {currency} or not {"PAPER_CASH", "PAPER_EQUITY"}.issubset(by_code):
        raise CustomerPaperFlowBlocked("Paper ledger has an unexpected currency or incomplete seed accounts")
    cash = by_code["PAPER_CASH"]
    if cash < 0:
        raise CustomerPaperFlowBlocked("Paper cash is negative; new orders are blocked")
    return {
        "status": "PASS",
        "currency": currency,
        "paper_cash": format(cash, "f"),
        "initial_balance": format(initial, "f"),
        "real_funds_used": False,
        "balances": balances,
    }


def _cost_inputs() -> tuple[Decimal, Decimal, str]:
    """Return visible configured assumptions; they are not proof of venue calibration."""
    fee_bps = _finite_decimal(settings.research_taker_bps, "research_taker_bps", non_negative=True)
    slippage_bps = _finite_decimal(settings.research_slippage_bps, "research_slippage_bps", non_negative=True)
    return fee_bps, slippage_bps, "UNVERIFIED_DEFAULT_ASSUMPTIONS"


def _fill_price_and_fee(side: str, quantity: Decimal, market: dict) -> tuple[str, str, dict]:
    fee_bps, slippage_bps, cost_status = _cost_inputs()
    side = str(side).upper()
    top_of_book = market["ask"] if side == "BUY" else market["bid"]
    slip_fraction = slippage_bps / Decimal("10000")
    price = top_of_book * (Decimal("1") + slip_fraction if side == "BUY" else Decimal("1") - slip_fraction)
    if price <= 0:
        raise CustomerPaperFlowBlocked("Slippage assumption produces an invalid fill price")
    notional = quantity * price
    fee = notional * fee_bps / Decimal("10000")
    cost_details = {
        "cost_model_status": cost_status,
        "fee_bps_assumption": format(fee_bps, "f"),
        "slippage_bps_assumption": format(slippage_bps, "f"),
        "observed_spread_bps_at_simulated_fill": format(market["spread_bps"], "f"),
        "top_of_book_price": format(top_of_book, "f"),
        "quote_timestamp_utc": market["quote_timestamp"],
        "data_sha256": market["data_sha256"],
    }
    return format(price, "f"), format(fee, "f"), cost_details


async def _fill_open_order(customer_id: int, order: dict, market: dict) -> dict:
    client_id = str(order.get("client_order_id") or "")
    if not client_id:
        raise CustomerPaperFlowBlocked("Pending paper order is missing its client identifier")
    state = await get_customer_paper_order(customer_id=customer_id, client_order_id=client_id)
    if state is None:
        raise CustomerPaperFlowBlocked("Pending order disappeared during recovery")
    if state.get("status") not in {"SUBMITTED", "PARTIALLY_FILLED"}:
        return {"status": "NOT_OPEN", "client_order_id": client_id, "order_status": state.get("status")}
    meta = _json_mapping(order.get("metadata"))
    signal_bar = str(meta.get("signal_bar_timestamp") or "")
    if not signal_bar:
        raise CustomerPaperFlowBlocked("Pending order lacks its signal-bar timestamp; refusing an unverifiable fill")
    try:
        signal_dt = datetime.fromisoformat(signal_bar.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CustomerPaperFlowBlocked("Pending order signal-bar timestamp is invalid") from exc
    if signal_dt.tzinfo is None or market["quote_timestamp"] <= signal_dt.astimezone(timezone.utc).isoformat():
        raise CustomerPaperFlowBlocked("Fill quote must post-date the order's completed signal bar")
    remaining = Decimal(str(state["remaining_quantity"]))
    if remaining <= 0:
        raise CustomerPaperFlowBlocked("Pending order has no positive remaining quantity")
    side = str(state.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        raise CustomerPaperFlowBlocked("Pending order side is invalid")
    position_matches = [
        p for p in await get_customer_paper_positions(customer_id=customer_id)
        if str(p.get("venue") or "").lower() == str(state.get("venue") or "").lower()
        and str(p.get("symbol") or "") == str(state.get("symbol") or "")
    ]
    if side == "SELL":
        held = Decimal(str(position_matches[0]["quantity"])) if position_matches else Decimal("0")
        if held <= 0 or remaining > held:
            raise CustomerPaperFlowBlocked("Recovery SELL does not match an open long spot position")
    fill_price, fee, costs = _fill_price_and_fee(side, remaining, market)
    bar_key = market["bar_timestamp"]
    fill_key = "paperfill-" + _sha(f"{state['order_id']}|{bar_key}")[:48]
    result = await record_customer_paper_fill(
        customer_id=customer_id,
        client_order_id=client_id,
        idempotency_key=fill_key,
        bar_key=bar_key,
        quantity=format(remaining, "f"),
        price=fill_price,
        fee_amount=fee,
    )
    return {
        "status": result.get("status"),
        "client_order_id": client_id,
        "order_id": state["order_id"],
        "fill": result,
        "cost_model": costs,
        "recovered_pending_order": True,
    }


async def process_customer_paper_orders(customer_id: int, req, df, market_context: Mapping) -> dict | None:
    """Recover one pending order for this customer/market using a fresh quote."""
    market = validate_customer_paper_market(req, df, market_context)
    pending = await list_open_customer_paper_orders(customer_id=customer_id)
    matching = [
        order for order in pending
        if str(order.get("venue") or "").lower() == str(req.exchange).lower()
        and str(order.get("symbol") or "") == str(req.symbol)
    ]
    if not matching:
        return None
    # Timeframe is part of the durable order identity. Never recover an old pending
    # order using bars from a reconfigured bot/timeframe.
    for order in matching:
        meta = _json_mapping(order.get("metadata"))
        if str(meta.get("timeframe") or "") != str(req.timeframe):
            raise CustomerPaperFlowBlocked("Open paper order timeframe differs from the current request; reconcile before execution")
    if len(matching) > 1:
        raise CustomerPaperFlowBlocked("Multiple open customer paper orders exist for this market; reconcile before proceeding")
    return await _fill_open_order(customer_id, matching[0], market)


async def _protective_exit(customer_id: int, req, df, market: dict) -> dict | None:
    positions = [
        p for p in await get_customer_paper_positions(customer_id=customer_id)
        if str(p.get("venue") or "").lower() == str(req.exchange).lower()
        and str(p.get("symbol") or "") == str(req.symbol)
        and Decimal(str(p.get("quantity") or "0")) > 0
    ]
    if not positions:
        return None
    if len(positions) != 1:
        raise CustomerPaperFlowBlocked("Multiple open position records exist for the same paper market")
    position = positions[0]
    if str(position.get("timeframe") or "") != str(req.timeframe):
        raise CustomerPaperFlowBlocked("Open paper position timeframe differs from the current request; reconcile before execution")
    try:
        latest_position_bar = datetime.fromisoformat(str(position.get("last_event_bar_key") or "").replace("Z", "+00:00"))
        current_bar = datetime.fromisoformat(market["bar_timestamp"].replace("Z", "+00:00"))
    except ValueError:
        raise CustomerPaperFlowBlocked("Position event lacks a valid completed-bar identity")
    if latest_position_bar.tzinfo is None or current_bar <= latest_position_bar:
        return None
    stop = _finite_decimal(position.get("stop_loss_price"), "position stop loss") if position.get("stop_loss_price") else Decimal("0")
    target = _finite_decimal(position.get("take_profit_price"), "position take profit") if position.get("take_profit_price") else Decimal("0")
    try:
        low = _finite_decimal(df["low"].iloc[-1], "latest completed-bar low")
        high = _finite_decimal(df["high"].iloc[-1], "latest completed-bar high")
    except Exception as exc:
        raise CustomerPaperFlowBlocked("Latest completed bar has invalid OHLC prices") from exc
    reason = None
    # If stop and target are both crossed in one candle, choose the adverse stop first.
    if stop > 0 and low <= stop:
        reason = "STOP_LOSS"
    elif target > 0 and high >= target:
        reason = "TAKE_PROFIT"
    if not reason:
        return None
    quantity = Decimal(str(position["quantity"]))
    position_client_id = "cpx-" + _sha(
        f"{customer_id}|{req.exchange}|{req.symbol}|{req.timeframe}|{market['bar_timestamp']}|{reason}"
    )[:48]
    await submit_customer_paper_order(
        customer_id=customer_id,
        client_order_id=position_client_id,
        venue=str(req.exchange).lower(),
        market_type="spot",
        symbol=str(req.symbol).upper(),
        side="SELL",
        quantity=format(quantity, "f"),
        currency=market["currency"],
        order_type="MARKET",
        metadata={
            "paper_flow_version": 1,
            "order_role": "PROTECTIVE_EXIT",
            "timeframe": str(req.timeframe),
            "exit_reason": reason,
            "signal_bar_timestamp": market["bar_timestamp"],
            "data_sha256": market["data_sha256"],
            "stop_loss_price": None,
            "take_profit_price": None,
            "cost_model_status": "UNVERIFIED_DEFAULT_ASSUMPTIONS",
        },
    )
    pending = await list_open_customer_paper_orders(customer_id=customer_id)
    match = [
        order for order in pending
        if order.get("client_order_id") == position_client_id
    ]
    if len(match) != 1:
        raise CustomerPaperFlowBlocked("Protective exit intent was not visible to recovery")
    return await _fill_open_order(customer_id, match[0], market)


async def recover_customer_paper_spot_flow(
    *, customer_id: int, req, df, market_context: Mapping
) -> dict | None:
    """Recover a durable pending order or trigger a protective exit before new signals."""
    market = validate_customer_paper_market(req, df, market_context)
    account = await ensure_customer_paper_account(customer_id, market["currency"])
    recovered = await process_customer_paper_orders(customer_id, req, df, market_context)
    if recovered is not None:
        return {
            "mode": "CUSTOMER_PAPER",
            "status": "RECOVERY_PROCESSED",
            "decision": "TRADE" if recovered.get("fill", {}).get("status") == "APPENDED" else "NO_TRADE",
            "execution": recovered,
            "paper_account": {k: account[k] for k in ("currency", "paper_cash", "real_funds_used")},
        }
    protective = await _protective_exit(customer_id, req, df, market)
    if protective is not None:
        return {
            "mode": "CUSTOMER_PAPER", "status": "PROTECTIVE_EXIT",
            "decision": "TRADE", "execution": protective,
            "paper_account": {k: account[k] for k in ("currency", "paper_cash", "real_funds_used")},
        }
    return None


async def execute_customer_paper_spot_signal(
    *,
    customer_id: int,
    req,
    df,
    market_context: Mapping,
    side: str,
    quantity: str | int | Decimal,
    stop_loss_price: str | int | Decimal | None,
    take_profit_price: str | int | Decimal | None,
    strategy: str,
    signal_metadata: Mapping | None = None,
) -> dict:
    """Seed virtual cash, recover pending intents, honor long-only exits, then submit/fill.

    The signal is generated from completed candles; execution uses only a newer fresh
    server bid/ask. It never invokes app.execution.execute_signal or the primary money ledger.
    """
    market = validate_customer_paper_market(req, df, market_context)
    account = await ensure_customer_paper_account(customer_id, market["currency"])

    recovered = await process_customer_paper_orders(customer_id, req, df, market_context)
    if recovered is not None:
        return {
            "mode": "CUSTOMER_PAPER",
            "status": "RECOVERY_PROCESSED",
            "decision": "TRADE" if recovered.get("fill", {}).get("status") == "APPENDED" else "NO_TRADE",
            "execution": recovered,
            "paper_account": {k: account[k] for k in ("currency", "paper_cash", "real_funds_used")},
        }

    protective = await _protective_exit(customer_id, req, df, market)
    if protective is not None:
        return {
            "mode": "CUSTOMER_PAPER", "status": "PROTECTIVE_EXIT",
            "decision": "TRADE", "execution": protective,
            "paper_account": {k: account[k] for k in ("currency", "paper_cash", "real_funds_used")},
        }

    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise CustomerPaperFlowBlocked("Customer paper signal side must be BUY or SELL")
    positions = [
        p for p in await get_customer_paper_positions(customer_id=customer_id)
        if str(p.get("venue") or "").lower() == str(req.exchange).lower()
        and str(p.get("symbol") or "") == str(req.symbol)
        and Decimal(str(p.get("quantity") or "0")) > 0
    ]
    if normalized_side == "BUY" and positions:
        return {"mode": "CUSTOMER_PAPER", "status": "NO_TRADE", "decision": "NO_TRADE", "reason": "long_spot_position_already_open"}
    if normalized_side == "SELL" and not positions:
        return {"mode": "CUSTOMER_PAPER", "status": "NO_TRADE", "decision": "NO_TRADE", "reason": "short_entries_are_not_supported"}

    fee_bps, slippage_bps, cost_status = _cost_inputs()
    slip_fraction = slippage_bps / Decimal("10000")
    effective_buy_price = market["ask"] * (Decimal("1") + slip_fraction)
    available_cash = Decimal(account["paper_cash"])
    raw_quantity = _finite_decimal(quantity, "proposed paper quantity")
    if raw_quantity <= 0:
        return {"mode": "CUSTOMER_PAPER", "status": "NO_TRADE", "decision": "NO_TRADE", "reason": "non_positive_quantity"}
    if normalized_side == "BUY":
        fee_fraction = fee_bps / Decimal("10000")
        affordable = available_cash / (effective_buy_price * (Decimal("1") + fee_fraction))
        raw_quantity = min(raw_quantity, affordable)
        if raw_quantity <= 0:
            return {"mode": "CUSTOMER_PAPER", "status": "NO_TRADE", "decision": "NO_TRADE", "reason": "insufficient_paper_cash"}
    else:
        raw_quantity = min(raw_quantity, Decimal(str(positions[0]["quantity"])))
        if raw_quantity <= 0:
            return {"mode": "CUSTOMER_PAPER", "status": "NO_TRADE", "decision": "NO_TRADE", "reason": "no_long_position_to_close"}

    role = "ENTRY" if normalized_side == "BUY" else "STRATEGY_EXIT"
    exit_reason = "STRATEGY_SELL" if normalized_side == "SELL" else None
    client_id = "cp-" + _sha(
        f"{customer_id}|{req.exchange}|{req.symbol}|{req.timeframe}|{market['bar_timestamp']}|{role}|{exit_reason or ''}"
    )[:48]
    # Do not put per-request quote values or random decision IDs in immutable order
    # metadata; identical retries within the same bar must have an identical payload.
    meta = {
        "paper_flow_version": 1,
        "order_role": role,
        "timeframe": str(req.timeframe),
        "signal_bar_timestamp": market["bar_timestamp"],
        "data_sha256": market["data_sha256"],
        "strategy": str(strategy or "unknown")[:120],
        "stop_loss_price": str(stop_loss_price) if normalized_side == "BUY" and stop_loss_price is not None else None,
        "take_profit_price": str(take_profit_price) if normalized_side == "BUY" and take_profit_price is not None else None,
        "fee_bps_assumption": format(fee_bps, "f"),
        "slippage_bps_assumption": format(slippage_bps, "f"),
        "cost_model_status": cost_status,
        "decision_metadata": _json_mapping(signal_metadata),
    }
    submitted = await submit_customer_paper_order(
        customer_id=customer_id,
        client_order_id=client_id,
        venue=str(req.exchange).lower(),
        market_type="spot",
        symbol=str(req.symbol).upper(),
        side=normalized_side,
        quantity=format(raw_quantity, "f"),
        currency=market["currency"],
        order_type="MARKET",
        limit_price=None,
        metadata=meta,
    )
    if submitted.get("status") == "REPLAYED":
        prior = await get_customer_paper_order(customer_id=customer_id, client_order_id=client_id)
        if prior is None:
            raise CustomerPaperFlowBlocked("Idempotent order replay has no durable order row")
        if prior.get("status") in {"FILLED", "CANCELLED", "EXPIRED", "REJECTED"}:
            return {
                "mode": "CUSTOMER_PAPER", "status": "REPLAYED",
                "decision": "NO_TRADE" if prior.get("status") != "FILLED" else "TRADE",
                "execution": {"status": "REPLAYED", "order": prior},
                "paper_account": {k: account[k] for k in ("currency", "paper_cash", "real_funds_used")},
                "cost_model_status": cost_status,
                "real_money_ledger_written": False,
            }
    pending = await list_open_customer_paper_orders(customer_id=customer_id)
    match = [
        order for order in pending
        if order.get("client_order_id") == client_id
    ]
    if len(match) != 1:
        raise CustomerPaperFlowBlocked("Submitted paper order was not visible to the recovery query")
    execution = await _fill_open_order(customer_id, match[0], market)
    return {
        "mode": "CUSTOMER_PAPER",
        "status": execution.get("status", "UNKNOWN"),
        "decision": "TRADE" if execution.get("status") in {"APPENDED", "REPLAYED"} else "NO_TRADE",
        "execution": execution,
        "paper_account": {k: account[k] for k in ("currency", "paper_cash", "real_funds_used")},
        "cost_model_status": cost_status,
        "real_money_ledger_written": False,
    }
