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
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, localcontext

from .config import settings
from .customer_paper_ledger import (
    CustomerPaperLedgerBlocked,
    get_customer_paper_balances,
    post_customer_paper_journal,
)
from .customer_paper_lifecycle import (
    CustomerPaperOrderConflict,
    get_customer_paper_fill_for_bar,
    get_customer_paper_order,
    get_customer_paper_positions,
    list_open_customer_paper_orders,
    record_customer_paper_fill,
    expire_customer_paper_order,
    register_customer_paper_market_bar,
    submit_customer_paper_order,
)


_TIMEFRAME_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400,
}


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


async def _paper_call(awaitable, action: str):
    """Convert isolated paper-ledger failures into fail-closed flow errors."""
    try:
        return await awaitable
    except CustomerPaperFlowBlocked:
        raise
    except Exception as exc:
        raise CustomerPaperFlowBlocked(f"{action}: {exc}") from exc


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
    timeframe_seconds = _TIMEFRAME_SECONDS.get(str(req.timeframe))
    if timeframe_seconds is None:
        raise CustomerPaperFlowBlocked("Customer paper timeframe is unsupported")
    if quote_dt.tzinfo is None or bar_dt.tzinfo is None or quote_dt <= bar_dt:
        raise CustomerPaperFlowBlocked("Fresh quote must follow the latest completed signal bar")
    if quote_dt.astimezone(timezone.utc) <= bar_dt.astimezone(timezone.utc) + timedelta(seconds=timeframe_seconds):
        raise CustomerPaperFlowBlocked("Fresh quote must post-date the completed candle close")
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
        balances = await _paper_call(
            get_customer_paper_balances(customer_id=customer_id), "read paper balances"
        )
        if not balances:
            await _paper_call(post_customer_paper_journal(
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
            ), "seed virtual paper account")
            balances = await _paper_call(
                get_customer_paper_balances(customer_id=customer_id), "verify paper seed"
            )
    except CustomerPaperFlowBlocked:
        raise
    except Exception as exc:
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
    with localcontext() as context:
        context.prec = 50
        slip_fraction = slippage_bps / Decimal("10000")
        raw_price = top_of_book * (
            Decimal("1") + slip_fraction if side == "BUY" else Decimal("1") - slip_fraction
        )
        if raw_price <= 0:
            raise CustomerPaperFlowBlocked("Slippage assumption produces an invalid fill price")
        price_rounding = ROUND_UP if side == "BUY" else ROUND_DOWN
        price = raw_price.quantize(_BAR_QUANTUM, rounding=price_rounding)
        if price <= 0:
            raise CustomerPaperFlowBlocked("Rounded simulated fill price is invalid")
        rounded_quantity = quantity.quantize(_BAR_QUANTUM, rounding=ROUND_DOWN)
        if rounded_quantity <= 0:
            raise CustomerPaperFlowBlocked("Simulated fill quantity is below supported precision")
        # Match the ledger's NUMERIC(..., 18) money scale before calculating costs.
        notional = (rounded_quantity * price).quantize(_BAR_QUANTUM, rounding=ROUND_HALF_EVEN)
        fee = (notional * fee_bps / Decimal("10000")).quantize(
            _BAR_QUANTUM, rounding=ROUND_UP
        )
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


def conservative_customer_paper_buy_price(market_context: Mapping) -> Decimal:
    """Price a BUY with the exact ask/slippage/rounding model used by paper fills.

    Used only for pre-submission sizing. The database fill transaction independently
    rechecks the actual eventual fill price against remaining correlation-group headroom.
    """
    context = dict(market_context)
    market = {
        "ask": _finite_decimal(context.get("ask"), "ask"),
        "bid": _finite_decimal(context.get("bid"), "bid"),
        "spread_bps": _finite_decimal(context.get("spread_bps"), "spread_bps", non_negative=True),
        "quote_timestamp": str(context.get("quote_timestamp_utc") or ""),
        "data_sha256": str(context.get("ohlcv_sha256") or ""),
    }
    if market["ask"] <= 0 or market["bid"] <= 0:
        raise CustomerPaperFlowBlocked("Fresh BUY sizing quote requires a positive bid and ask")
    price, _fee, _costs = _fill_price_and_fee("BUY", Decimal("1"), market)
    return Decimal(price)


async def _fill_open_order(
    customer_id: int, order: dict, market: dict, df,
    correlation_group_marks: Mapping[str, object] | None = None,
) -> dict:
    client_id = str(order.get("client_order_id") or "")
    if not client_id:
        raise CustomerPaperFlowBlocked("Pending paper order is missing its client identifier")
    state = await _paper_call(
        get_customer_paper_order(customer_id=customer_id, client_order_id=client_id),
        "read pending paper order",
    )
    if state is None:
        raise CustomerPaperFlowBlocked("Pending order disappeared during recovery")
    if state.get("status") not in {"SUBMITTED", "PARTIALLY_FILLED"}:
        return {"status": "NOT_OPEN", "client_order_id": client_id, "order_status": state.get("status")}
    meta = _json_mapping(order.get("metadata"))
    signal_bar = str(meta.get("signal_bar_timestamp") or "")
    submission_bar = str(meta.get("submission_bar_at") or signal_bar)
    if not signal_bar or not submission_bar:
        raise CustomerPaperFlowBlocked("Pending paper order lacks its signal/submission bar timestamp")
    try:
        signal_dt = datetime.fromisoformat(signal_bar.replace("Z", "+00:00"))
        submission_dt = datetime.fromisoformat(submission_bar.replace("Z", "+00:00"))
        current_bar_dt = datetime.fromisoformat(str(market["bar_timestamp"]).replace("Z", "+00:00"))
        quote_dt = datetime.fromisoformat(str(market["quote_timestamp"]).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CustomerPaperFlowBlocked("Pending order signal, bar, or quote timestamp is invalid") from exc
    if any(dt.tzinfo is None or dt.utcoffset() is None for dt in (signal_dt, submission_dt, current_bar_dt, quote_dt)):
        raise CustomerPaperFlowBlocked("Pending order signal, submission, and market timestamps must be timezone-aware")
    signal_dt = signal_dt.astimezone(timezone.utc)
    submission_dt = submission_dt.astimezone(timezone.utc)
    current_bar_dt = current_bar_dt.astimezone(timezone.utc)
    quote_dt = quote_dt.astimezone(timezone.utc)
    if quote_dt <= signal_dt:
        raise CustomerPaperFlowBlocked("Fill quote must post-date the order's completed signal bar")
    # Never fill from the same completed bar that generated the signal/order. A
    # later completed bar must supply the fill liquidity and shared capacity.
    if current_bar_dt <= submission_dt:
        return {
            "status": "WAITING_FOR_NEXT_COMPLETED_BAR",
            "client_order_id": client_id,
            "order_id": str(state["order_id"]),
            "submission_bar_at": submission_dt.isoformat(),
            "current_bar_at": current_bar_dt.isoformat(),
            "recovered_pending_order": True,
        }

    timeframe = str(meta.get("timeframe") or "")
    timeframe_seconds = _TIMEFRAME_SECONDS.get(timeframe)
    if not timeframe_seconds:
        raise CustomerPaperFlowBlocked("Pending paper order has an unsupported timeframe")
    elapsed_seconds = (current_bar_dt - submission_dt).total_seconds()
    if elapsed_seconds < 0 or elapsed_seconds % timeframe_seconds != 0:
        raise CustomerPaperFlowBlocked("Pending paper order bars are not aligned to its timeframe")
    elapsed_bars = int(elapsed_seconds // timeframe_seconds)
    is_protective_exit = str(meta.get("order_role") or "").upper() == "PROTECTIVE_EXIT"
    ttl_bars = max(1, int(settings.paper_order_ttl_bars))
    if not is_protective_exit and elapsed_bars >= ttl_bars:
        await _paper_call(
            expire_customer_paper_order(
                customer_id=customer_id,
                client_order_id=client_id,
                idempotency_key="paper-ttl-" + _sha(f"{client_id}|{submission_dt.isoformat()}")[:48],
            ),
            "expire stale autonomous paper order",
        )
        return {
            "status": "EXPIRED_TTL", "client_order_id": client_id,
            "order_id": str(state["order_id"]), "elapsed_bars": elapsed_bars,
            "fill": {"status": "EXPIRED"}, "recovered_pending_order": True,
        }
    if not is_protective_exit and elapsed_bars < max(1, int(settings.paper_fill_latency_bars)):
        return {
            "status": "WAITING_FOR_FILL_LATENCY", "client_order_id": client_id,
            "order_id": str(state["order_id"]), "elapsed_bars": elapsed_bars,
            "required_latency_bars": max(1, int(settings.paper_fill_latency_bars)),
            "recovered_pending_order": True,
        }

    remaining = Decimal(str(state["remaining_quantity"]))
    if remaining <= 0:
        raise CustomerPaperFlowBlocked("Pending order has no positive remaining quantity")
    side = str(state.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        raise CustomerPaperFlowBlocked("Pending order side is invalid")
    position_matches = [
        p for p in await _paper_call(
            get_customer_paper_positions(customer_id=customer_id), "read paper position for fill"
        )
        if str(p.get("venue") or "").lower() == str(state.get("venue") or "").lower()
        and str(p.get("symbol") or "") == str(state.get("symbol") or "")
    ]
    if side == "SELL":
        held = Decimal(str(position_matches[0]["quantity"])) if position_matches else Decimal("0")
        if held <= 0:
            await _paper_call(
                expire_customer_paper_order(
                    customer_id=customer_id,
                    client_order_id=client_id,
                    idempotency_key="paper-no-position-" + _sha(client_id)[:48],
                ),
                "expire stale paper sell with no open position",
            )
            return {
                "status": "EXPIRED_NO_OPEN_POSITION",
                "client_order_id": client_id,
                "order_id": state["order_id"],
                "fill": {"status": "EXPIRED"},
                "recovered_pending_order": True,
            }
        # Another paper cycle can reduce the position between submitting and
        # recovering a pending SELL. Bound each attempt to currently held spot
        # quantity; any excess stays open only until later bars reduce it further,
        # then the no-position path above expires the stale remainder.
        remaining = min(remaining, held)

    try:
        budget = await _paper_call(register_customer_paper_market_bar(
            venue=str(state["venue"]).lower(),
            symbol=str(state["symbol"]).upper(),
            timeframe=str(meta.get("timeframe") or ""),
            bar_at=market["bar_timestamp"],
            bar_data_sha256=str(market["bar_data_sha256"]),
            base_volume=market["base_volume"],
            participation_rate=str(settings.paper_max_bar_volume_participation),
        ), "register paper market-bar budget")
    except (CustomerPaperLedgerBlocked, CustomerPaperOrderConflict, ValueError) as exc:
        raise CustomerPaperFlowBlocked(f"Shared paper market-bar budget is unavailable: {exc}") from exc

    bar_key = str(budget["bar_key"])
    # A completed bar may receive at most one fill attempt for an order. On retry,
    # replay its immutable fill instead of recalculating payload from a newer quote.
    prior_fill = await _paper_call(
        get_customer_paper_fill_for_bar(
            customer_id=customer_id, client_order_id=client_id, bar_key=bar_key
        ),
        "read same-bar paper fill",
    )
    if prior_fill is not None:
        return {
            "status": "REPLAYED",
            "client_order_id": client_id,
            "order_id": state["order_id"],
            "bar_key": bar_key,
            "bar_at": budget.get("bar_at"),
            "bar_capacity": budget.get("capacity"),
            "fill": prior_fill,
            "cost_model": {"cost_model_status": "REPLAYED_FROM_IMMUTABLE_LEDGER"},
            "recovered_pending_order": True,
        }

    try:
        fill_price, fee, costs = _fill_price_and_fee(side, remaining, market)
    except CustomerPaperFlowBlocked as exc:
        if side == "SELL" and "Rounded simulated fill price is invalid" in str(exc):
            # Keep the immutable exit intent pending until a representable positive
            # quote exists. Never write a zero-price fill or overstate proceeds.
            return {
                "status": "WAITING_FOR_PRICE_PRECISION",
                "client_order_id": client_id,
                "order_id": state["order_id"],
                "bar_key": bar_key,
                "fill": {"status": "NOT_FILLED"},
                "reason": "Positive SELL quote rounds to zero at numeric(38,18) precision",
                "recovered_pending_order": True,
            }
        raise
    if side == "BUY":
        balances = await _paper_call(
            get_customer_paper_balances(customer_id=customer_id), "read paper cash for fill"
        )
        cash = sum(
            (
                Decimal(str(row.get("balance") or "0"))
                for row in balances
                if str(row.get("account_code") or "") == "PAPER_CASH"
                and str(row.get("currency") or "").upper() == str(state.get("currency") or "").upper()
            ),
            Decimal("0"),
        )
        fee_bps = Decimal(str(costs["fee_bps_assumption"]))
        unit_cost = Decimal(fill_price) * (Decimal("1") + fee_bps / Decimal("10000"))
        with localcontext() as context:
            context.prec = 50
            affordable = max(Decimal("0"), cash / unit_cost).quantize(_BAR_QUANTUM, rounding=ROUND_DOWN)
        remaining = min(remaining, affordable)
        if remaining <= 0:
            await _paper_call(
                expire_customer_paper_order(
                    customer_id=customer_id,
                    client_order_id=client_id,
                    idempotency_key="paper-unaffordable-" + _sha(client_id)[:48],
                ),
                "expire unaffordable pending paper order",
            )
            return {
                "status": "EXPIRED_INSUFFICIENT_PAPER_CASH",
                "client_order_id": client_id,
                "order_id": state["order_id"],
                "bar_key": bar_key,
                "fill": {"status": "EXPIRED"},
                "recovered_pending_order": True,
                "cost_model": costs,
            }
        fill_price, fee, costs = _fill_price_and_fee(side, remaining, market)

    try:
        result = await _paper_call(record_customer_paper_fill(
            customer_id=customer_id,
            client_order_id=client_id,
            idempotency_key="paperfill-" + _sha(f"{state['order_id']}|{bar_key}")[:48],
            bar_key=bar_key,
            quantity=format(remaining, "f"),
            price=fill_price,
            fee_amount=fee,
            correlation_group_marks=correlation_group_marks,
            enforce_market_bar_budget=True,
        ), "append isolated paper fill")
    except CustomerPaperFlowBlocked as exc:
        # The paper-cash advisory lock can reveal a concurrent spend after the
        # pre-read. Do not leave an unaffordable BUY permanently blocking recovery.
        if side == "BUY" and "overdraw" in str(exc).lower():
            await _paper_call(
                expire_customer_paper_order(
                    customer_id=customer_id,
                    client_order_id=client_id,
                    idempotency_key="paper-cash-race-" + _sha(f"{client_id}|{bar_key}")[:48],
                ),
                "expire paper order after concurrent cash consumption",
            )
            return {
                "status": "EXPIRED_INSUFFICIENT_PAPER_CASH",
                "client_order_id": client_id,
                "order_id": state["order_id"],
                "bar_key": bar_key,
                "fill": {"status": "EXPIRED"},
                "recovered_pending_order": True,
                "cost_model": costs,
            }
        raise
    return {
        "status": result.get("status"),
        "client_order_id": client_id,
        "order_id": state["order_id"],
        "bar_key": bar_key,
        "bar_at": budget.get("bar_at"),
        "bar_capacity": budget.get("capacity"),
        "bar_available_before_fill": budget.get("available"),
        "fill": result,
        "cost_model": costs,
        "recovered_pending_order": True,
    }


async def process_customer_paper_orders(
    customer_id: int, req, df, market_context: Mapping,
    correlation_group_marks: Mapping[str, object] | None = None,
) -> dict | None:
    """Recover autonomous orders only and protect existing positions before pending BUY refills."""
    market = validate_customer_paper_market(req, df, market_context)
    pending = await _paper_call(
        list_open_customer_paper_orders(customer_id=customer_id), "list pending paper orders"
    )
    matching = [
        order for order in pending
        if str(order.get("venue") or "").lower() == str(req.exchange).lower()
        and str(order.get("symbol") or "").upper() == str(req.symbol).upper()
    ]
    if not matching:
        return None
    if len(matching) > 1:
        raise CustomerPaperFlowBlocked("Multiple open customer paper orders exist for this market; reconcile before proceeding")
    order = matching[0]
    meta = _json_mapping(order.get("metadata"))
    order_side = str(order.get("side") or "").upper()

    # Manual API and autonomous-controller order intents are not interchangeable.
    # Before refusing ownership, let a pending manual BUY yield to an existing stop.
    if meta.get("paper_flow_version") != 1:
        if order_side == "BUY":
            protective = await _protective_exit(customer_id, req, df, market)
            if protective is not None:
                await _paper_call(
                    expire_customer_paper_order(
                        customer_id=customer_id,
                        client_order_id=str(order.get("client_order_id") or ""),
                        idempotency_key="paper-manual-buy-expired-for-stop-" + _sha(
                            f"{order.get('client_order_id')}|{market['bar_timestamp']}"
                        )[:40],
                    ),
                    "expire manual pending BUY before protective exit",
                )
                return protective
        raise CustomerPaperFlowBlocked(
            "A manual paper order is open for this market; the autonomous flow will not take ownership or advance it"
        )

    # A partially filled autonomous BUY can leave a protected position while the
    # remainder is pending. Check all subsequent candles before another fill can move
    # the position-event cursor past a stop/target breach.
    if order_side == "BUY":
        protective = await _protective_exit(customer_id, req, df, market)
        if protective is not None:
            await _paper_call(
                expire_customer_paper_order(
                    customer_id=customer_id,
                    client_order_id=str(order.get("client_order_id") or ""),
                    idempotency_key="paper-entry-remainder-expired-for-stop-" + _sha(
                        f"{order.get('client_order_id')}|{market['bar_timestamp']}"
                    )[:40],
                ),
                "expire pending autonomous BUY remainder after protective exit",
            )
            return protective

    if str(meta.get("timeframe") or "") != str(req.timeframe):
        raise CustomerPaperFlowBlocked("Open paper order timeframe differs from the current request; reconcile before execution")
    return await _fill_open_order(
        customer_id, order, market, df, correlation_group_marks=correlation_group_marks
    )


async def _protective_exit(customer_id: int, req, df, market: dict) -> dict | None:
    positions = [
        p for p in await _paper_call(
            get_customer_paper_positions(customer_id=customer_id), "read paper positions"
        )
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
    if latest_position_bar.tzinfo is None or latest_position_bar.utcoffset() is None:
        raise CustomerPaperFlowBlocked("Position event timestamp must be timezone-aware")
    if current_bar.tzinfo is None or current_bar.utcoffset() is None:
        raise CustomerPaperFlowBlocked("Current paper bar timestamp must be timezone-aware")
    latest_position_bar = latest_position_bar.astimezone(timezone.utc)
    current_bar = current_bar.astimezone(timezone.utc)
    if current_bar <= latest_position_bar:
        return None
    timeframe_seconds = _TIMEFRAME_SECONDS.get(str(req.timeframe))
    if not timeframe_seconds:
        raise CustomerPaperFlowBlocked("Protective-exit timeframe is unsupported")
    span_seconds = (current_bar - latest_position_bar).total_seconds()
    if span_seconds % timeframe_seconds != 0:
        raise CustomerPaperFlowBlocked("Protective-exit history does not align to the position timeframe")
    bars_to_cover = int(span_seconds // timeframe_seconds)
    if bars_to_cover > len(df.index):
        raise CustomerPaperFlowBlocked("Protective-exit candle history is incomplete; fetch missing bars before proceeding")
    observed_bars = set()
    for raw_bar_at in df.index:
        try:
            bar_dt = datetime.fromisoformat(
                str(getattr(raw_bar_at, "isoformat", lambda: raw_bar_at)()).replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise CustomerPaperFlowBlocked("Protective-exit candle history contains an invalid timestamp") from exc
        if bar_dt.tzinfo is None or bar_dt.utcoffset() is None:
            raise CustomerPaperFlowBlocked("Protective-exit candle history contains a naive timestamp")
        observed_bars.add(bar_dt.astimezone(timezone.utc))
    for bar_number in range(1, bars_to_cover + 1):
        expected_bar = latest_position_bar + timedelta(seconds=bar_number * timeframe_seconds)
        if expected_bar not in observed_bars:
            raise CustomerPaperFlowBlocked("Protective-exit candle history is incomplete; a completed bar is missing")
    stop = _finite_decimal(position.get("stop_loss_price"), "position stop loss") if position.get("stop_loss_price") else Decimal("0")
    target = _finite_decimal(position.get("take_profit_price"), "position take profit") if position.get("take_profit_price") else Decimal("0")
    reason = None
    breach_bar = None
    # Evaluate every completed candle after the last persisted position event.
    # This preserves stop/target protection across worker downtime and missed cycles.
    for bar_at, bar in df.iterrows():
        try:
            bar_dt = datetime.fromisoformat(
                str(getattr(bar_at, "isoformat", lambda: bar_at)()).replace("Z", "+00:00")
            )
            if bar_dt.tzinfo is None or bar_dt.utcoffset() is None:
                raise ValueError("naive bar timestamp")
            bar_dt = bar_dt.astimezone(timezone.utc)
            if bar_dt <= latest_position_bar.astimezone(timezone.utc) or bar_dt > current_bar.astimezone(timezone.utc):
                continue
            low = _finite_decimal(bar["low"], "completed-bar low")
            high = _finite_decimal(bar["high"], "completed-bar high")
        except (KeyError, TypeError, ValueError) as exc:
            raise CustomerPaperFlowBlocked("A completed candle after the position event has invalid time or OHLC prices") from exc
        # If stop and target are crossed in one candle, choose the adverse stop first.
        if stop > 0 and low <= stop:
            reason, breach_bar = "STOP_LOSS", bar_dt
            break
        if target > 0 and high >= target:
            reason, breach_bar = "TAKE_PROFIT", bar_dt
            break
    if not reason or breach_bar is None:
        return None
    quantity = Decimal(str(position["quantity"]))
    breach_bar_at = breach_bar.isoformat()
    submission_bar_at = current_bar.astimezone(timezone.utc).isoformat()
    position_client_id = "cpx-" + _sha(
        f"{customer_id}|{req.exchange}|{req.symbol}|{req.timeframe}|{breach_bar_at}|{reason}"
    )[:48]
    await _paper_call(submit_customer_paper_order(
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
            "execution_owner": "autonomous_customer_bot",
            "exchange": str(req.exchange).lower(),
            "market_type": "spot",
            "order_role": "PROTECTIVE_EXIT",
            "timeframe": str(req.timeframe),
            "exit_reason": reason,
            "signal_bar_timestamp": breach_bar_at,
            "submission_bar_at": submission_bar_at,
            "data_sha256": market["data_sha256"],
            "stop_loss_price": None,
            "take_profit_price": None,
            "cost_model_status": "UNVERIFIED_DEFAULT_ASSUMPTIONS",
        },
    ), "submit protective paper exit")
    pending = await _paper_call(
        list_open_customer_paper_orders(customer_id=customer_id), "list pending protective exits"
    )
    match = [
        order for order in pending
        if order.get("client_order_id") == position_client_id
    ]
    if len(match) != 1:
        raise CustomerPaperFlowBlocked("Protective exit intent was not visible to recovery")
    return await _fill_open_order(customer_id, match[0], market, df)


async def recover_customer_paper_spot_flow(
    *, customer_id: int, req, df, market_context: Mapping,
    correlation_group_marks: Mapping[str, object] | None = None,
) -> dict | None:
    """Recover a durable pending order or trigger a protective exit before new signals."""
    market = validate_customer_paper_market(req, df, market_context)
    account = await ensure_customer_paper_account(customer_id, market["currency"])
    recovered = await process_customer_paper_orders(
        customer_id, req, df, market_context,
        correlation_group_marks=correlation_group_marks,
    )
    if recovered is not None:
        fill_status = (recovered.get("fill") or {}).get("status") or recovered.get("status")
        executed = fill_status in {"APPENDED", "REPLAYED"}
        return {
            "mode": "CUSTOMER_PAPER",
            "status": "RECOVERY_PROCESSED" if executed else str(recovered.get("status") or "WAITING_FOR_NEXT_COMPLETED_BAR"),
            "decision": "TRADE" if executed else "NO_TRADE",
            "execution": recovered,
            "paper_account": {k: account[k] for k in ("currency", "paper_cash", "real_funds_used")},
        }
    protective = await _protective_exit(customer_id, req, df, market)
    if protective is not None:
        executed = protective.get("status") in {"APPENDED", "REPLAYED"}
        return {
            "mode": "CUSTOMER_PAPER",
            "status": "PROTECTIVE_EXIT" if executed else "PROTECTIVE_EXIT_PENDING",
            "decision": "TRADE" if executed else "NO_TRADE",
            "execution": protective,
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
    correlation_group_marks: Mapping[str, object] | None = None,
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
            "decision": "TRADE" if str(recovered.get("fill", {}).get("status") or "").upper() in {"APPENDED", "REPLAYED"} else "NO_TRADE",
            "execution": recovered,
            "paper_account": {k: account[k] for k in ("currency", "paper_cash", "real_funds_used")},
        }

    protective = await _protective_exit(customer_id, req, df, market)
    if protective is not None:
        protective_fill_status = str(
            (protective.get("fill") or {}).get("status") or protective.get("status") or ""
        ).upper()
        protective_executed = protective_fill_status in {"APPENDED", "REPLAYED"}
        return {
            "mode": "CUSTOMER_PAPER",
            "status": "PROTECTIVE_EXIT" if protective_executed else "PROTECTIVE_EXIT_PENDING",
            "decision": "TRADE" if protective_executed else "NO_TRADE",
            "execution": protective,
            "paper_account": {k: account[k] for k in ("currency", "paper_cash", "real_funds_used")},
        }

    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise CustomerPaperFlowBlocked("Customer paper signal side must be BUY or SELL")
    positions = [
        p for p in await _paper_call(
            get_customer_paper_positions(customer_id=customer_id), "read paper positions"
        )
        if str(p.get("venue") or "").lower() == str(req.exchange).lower()
        and str(p.get("symbol") or "") == str(req.symbol)
        and Decimal(str(p.get("quantity") or "0")) > 0
    ]
    if normalized_side == "BUY" and positions:
        return {"mode": "CUSTOMER_PAPER", "status": "NO_TRADE", "decision": "NO_TRADE", "reason": "long_spot_position_already_open"}
    if normalized_side == "SELL" and not positions:
        return {"mode": "CUSTOMER_PAPER", "status": "NO_TRADE", "decision": "NO_TRADE", "reason": "short_entries_are_not_supported"}

    fee_bps, slippage_bps, cost_status = _cost_inputs()
    available_cash = Decimal(account["paper_cash"])
    raw_quantity = _finite_decimal(quantity, "proposed paper quantity")
    with localcontext() as context:
        context.prec = 50
        slip_fraction = slippage_bps / Decimal("10000")
        effective_buy_price = market["ask"] * (Decimal("1") + slip_fraction)
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
        raw_quantity = raw_quantity.quantize(_BAR_QUANTUM, rounding=ROUND_DOWN)
    if raw_quantity <= 0:
        return {"mode": "CUSTOMER_PAPER", "status": "NO_TRADE", "decision": "NO_TRADE", "reason": "quantity_below_supported_precision"}

    role = "ENTRY" if normalized_side == "BUY" else "STRATEGY_EXIT"
    exit_reason = "STRATEGY_SELL" if normalized_side == "SELL" else None
    client_id = "cp-" + _sha(
        f"{customer_id}|{req.exchange}|{req.symbol}|{req.timeframe}|{market['bar_timestamp']}|{role}|{exit_reason or ''}"
    )[:48]
    # Do not put per-request quote values or random decision IDs in immutable order
    # metadata; identical retries within the same bar must have an identical payload.
    meta = {
        "paper_flow_version": 1,
        "execution_owner": "autonomous_customer_bot",
        "exchange": str(req.exchange).lower(),
        "market_type": "spot",
        "order_role": role,
        "timeframe": str(req.timeframe),
        "signal_bar_timestamp": market["bar_timestamp"],
        "submission_bar_at": market["bar_timestamp"],
        "data_sha256": market["data_sha256"],
        "strategy": str(strategy or "unknown")[:120],
        "stop_loss_price": str(stop_loss_price) if normalized_side == "BUY" and stop_loss_price is not None else None,
        "take_profit_price": str(take_profit_price) if normalized_side == "BUY" and take_profit_price is not None else None,
        "fee_bps_assumption": format(fee_bps, "f"),
        "slippage_bps_assumption": format(slippage_bps, "f"),
        "cost_model_status": cost_status,
        "decision_metadata": _json_mapping(signal_metadata),
    }
    submitted = await _paper_call(submit_customer_paper_order(
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
    ), "submit strategy paper order")
    if submitted.get("status") == "REPLAYED":
        prior = await _paper_call(
            get_customer_paper_order(customer_id=customer_id, client_order_id=client_id),
            "read replayed strategy paper order",
        )
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
    pending = await _paper_call(
        list_open_customer_paper_orders(customer_id=customer_id), "list submitted paper orders"
    )
    match = [
        order for order in pending
        if order.get("client_order_id") == client_id
    ]
    if len(match) != 1:
        raise CustomerPaperFlowBlocked("Submitted paper order was not visible to the recovery query")
    execution = await _fill_open_order(
        customer_id, match[0], market, df, correlation_group_marks=correlation_group_marks
    )
    return {
        "mode": "CUSTOMER_PAPER",
        "status": execution.get("status", "UNKNOWN"),
        "decision": "TRADE" if execution.get("status") in {"APPENDED", "REPLAYED"} else "NO_TRADE",
        "execution": execution,
        "paper_account": {k: account[k] for k in ("currency", "paper_cash", "real_funds_used")},
        "cost_model_status": cost_status,
        "real_money_ledger_written": False,
    }
