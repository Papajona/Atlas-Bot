#!/usr/bin/env python3
"""Opt-in Binance Spot Testnet acceptance drill.

This external-runtime harness must never target production. It requires an
explicit opt-in and proves the active public/private Spot REST endpoints are
the Binance Spot Test Network before placing any order. It never runs during CI.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import uuid
from decimal import Decimal
from typing import Any
from types import SimpleNamespace
from urllib.parse import urlparse

# Running a file under scripts/ sets sys.path[0] to that directory. Make the
# documented fresh-checkout invocation resolve the repository's app package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.customer_binance_execution import build_customer_binance_broker


TESTNET_SPOT_HOST = "testnet.binance.vision"
TERMINAL_ORDER_STATES = {"closed", "filled"}
CANCELED_ORDER_STATES = {"canceled", "cancelled", "rejected", "expired", "expired_in_match"}
SAFE_TERMINAL_ORDER_STATES = TERMINAL_ORDER_STATES | CANCELED_ORDER_STATES
STOP_TRIGGER_PRICE_RATIO = Decimal("0.80")
BUY_MARKET_MIN_NOTIONAL_HEADROOM = Decimal("1.25")
STOP_MIN_NOTIONAL_SIZING_HEADROOM = Decimal("1.50")
STOP_MIN_NOTIONAL_VALIDATION_HEADROOM = Decimal("1.25")


def req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0"))
    except Exception as exc:
        raise RuntimeError(f"Invalid decimal value: {value!r}") from exc


def active_spot_api_urls(client: Any) -> dict[str, str]:
    """Return only the active CCXT public/private Spot API URLs.

    Never inspect the string representation of the whole URL map: CCXT can
    retain inactive product endpoints alongside the endpoints used by Spot.
    """
    urls = getattr(client, "urls", None)
    if not isinstance(urls, dict):
        return {}
    api = urls.get("api")
    if isinstance(api, str):
        return {"public": api, "private": api}
    if not isinstance(api, dict):
        return {}
    return {
        name: api[name]
        for name in ("public", "private")
        if isinstance(api.get(name), str) and api[name].strip()
    }


def _is_testnet_spot_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == TESTNET_SPOT_HOST
        and parsed.username is None
        and parsed.password is None
    )


def require_testnet_spot_endpoints(client: Any) -> dict[str, str]:
    endpoints = active_spot_api_urls(client)
    if set(endpoints) != {"public", "private"} or not all(
        _is_testnet_spot_url(value) for value in endpoints.values()
    ):
        raise RuntimeError(
            "Refusing Spot order drill: the active CCXT public and private API "
            f"endpoints must both use HTTPS on {TESTNET_SPOT_HOST}; "
            f"active endpoints were {endpoints!r}"
        )
    return endpoints


def _free_balance(client: Any, asset: str) -> Decimal:
    balance = client.fetch_balance()
    if not isinstance(balance, dict):
        raise RuntimeError("Exchange returned a malformed balance response")
    free = balance.get("free")
    if not isinstance(free, dict):
        raise RuntimeError("Exchange balance omitted free assets")
    value = dec(free.get(asset, "0"))
    if value < 0:
        raise RuntimeError(f"Exchange reported a negative free {asset} balance")
    return value


def _net_filled_base(order: dict[str, Any] | None, base_asset: str) -> Decimal:
    """Bound cleanup to the base quantity this drill actually acquired, net of base fees."""
    if not isinstance(order, dict):
        return Decimal("0")
    gross = max(Decimal("0"), dec(order.get("filled") or "0"))
    raw_fees = order.get("fees")
    fees = raw_fees if isinstance(raw_fees, list) and raw_fees else [order.get("fee")]
    base_fee = Decimal("0")
    for fee in fees:
        if not isinstance(fee, dict):
            continue
        currency = str(fee.get("currency") or fee.get("asset") or "").upper()
        if currency == base_asset.upper():
            base_fee += max(Decimal("0"), dec(fee.get("cost") or fee.get("amount") or "0"))
    return max(Decimal("0"), gross - base_fee)


def _verified_net_filled_base(
    client: Any, symbol: str, order_id: str, order: dict[str, Any], base_asset: str
) -> Decimal:
    """Reconcile gross fills and every commission from Binance's order-specific trade ledger.

    Order-status responses may omit commissions. Cleanup quantity is therefore derived
    only from matching per-trade records with verified commission fields.
    """
    if not order_id or not isinstance(order, dict):
        raise RuntimeError("cannot verify buy commissions without a confirmed exchange order")
    expected_gross = max(Decimal("0"), dec(order.get("filled") or "0"))
    fetch_trades = getattr(client, "fetch_my_trades", None)
    if not callable(fetch_trades):
        raise RuntimeError("exchange adapter cannot fetch order-specific trade commissions")
    try:
        trades = fetch_trades(symbol, None, None, {"orderId": str(order_id)})
    except Exception as exc:
        raise RuntimeError(f"could not fetch trades for buy order {order_id}: {exc}") from exc
    if not isinstance(trades, list):
        raise RuntimeError(f"trade lookup for buy order {order_id} returned malformed data")
    matched: list[dict[str, Any]] = []
    for trade in trades:
        if not isinstance(trade, dict):
            raise RuntimeError(f"trade lookup for buy order {order_id} included malformed trade data")
        info = trade.get("info") if isinstance(trade.get("info"), dict) else {}
        trade_order_id = trade.get("order")
        if trade_order_id is None:
            trade_order_id = info.get("orderId")
        if trade_order_id is None:
            raise RuntimeError(
                f"trade lookup for buy order {order_id} included a trade without a verifiable order ID"
            )
        if str(trade_order_id) == str(order_id):
            matched.append(trade)

    if expected_gross == 0:
        if matched:
            matched_gross = sum(
                (max(Decimal("0"), dec(trade.get("amount"))) for trade in matched),
                Decimal("0"),
            )
            if matched_gross != 0:
                raise RuntimeError(
                    f"trade fill total {matched_gross} conflicts with zero-filled buy order {order_id}"
                )
        return Decimal("0")
    if not matched:
        raise RuntimeError(f"no trade records could be verified for filled buy order {order_id}")

    gross = Decimal("0")
    base_commission = Decimal("0")
    for trade in matched:
        raw_amount = trade.get("amount")
        if raw_amount is None:
            raise RuntimeError(f"trade record for buy order {order_id} omits its filled amount")
        amount = dec(raw_amount)
        if amount <= 0:
            raise RuntimeError(f"trade record for buy order {order_id} has an invalid filled amount")
        gross += amount

        info = trade.get("info") if isinstance(trade.get("info"), dict) else {}
        raw_fees = trade.get("fees")
        fee_entries: list[dict[str, Any]] = []
        if isinstance(raw_fees, list) and raw_fees and all(
            isinstance(fee, dict)
            and (fee.get("currency") or fee.get("asset"))
            and (fee.get("cost") is not None or fee.get("amount") is not None)
            for fee in raw_fees
        ):
            fee_entries = raw_fees
        else:
            raw_fee = trade.get("fee")
            if (
                isinstance(raw_fee, dict)
                and (raw_fee.get("currency") or raw_fee.get("asset"))
                and (raw_fee.get("cost") is not None or raw_fee.get("amount") is not None)
            ):
                fee_entries = [raw_fee]
            elif info.get("commission") is not None and info.get("commissionAsset"):
                fee_entries = [{
                    "cost": info["commission"],
                    "currency": info["commissionAsset"],
                }]
            else:
                raise RuntimeError(
                    f"trade record for buy order {order_id} omits verifiable commission amount or asset"
                )

        for fee in fee_entries:
            currency = str(fee.get("currency") or fee.get("asset") or "").strip().upper()
            fee_value = fee.get("cost") if fee.get("cost") is not None else fee.get("amount")
            if not currency or fee_value is None:
                raise RuntimeError(
                    f"trade record for buy order {order_id} contains an incomplete commission"
                )
            fee_amount = dec(fee_value)
            if fee_amount < 0:
                raise RuntimeError(
                    f"trade record for buy order {order_id} contains a negative commission"
                )
            if currency == base_asset.upper():
                base_commission += fee_amount

    if gross != expected_gross:
        raise RuntimeError(
            f"order-status filled amount {expected_gross} does not match verified trade total {gross} "
            f"for buy order {order_id}; refusing cleanup with incomplete trade data"
        )
    return max(Decimal("0"), gross - base_commission)


def _wait_for_order(broker: Any, order_id: str, symbol: str, timeout_seconds: int = 30) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last: dict = {}
    while True:
        last = broker.fetch_order(order_id, symbol)
        if not isinstance(last, dict):
            raise RuntimeError(f"Exchange returned malformed order state for {order_id}")
        if str(last.get("status", "")).lower() in SAFE_TERMINAL_ORDER_STATES:
            return last
        if time.monotonic() >= deadline:
            return last
        time.sleep(1)



def _fetch_order_by_client_id(client: Any, symbol: str, client_order_id: str) -> dict[str, Any]:
    """Reconcile an order whose submission response was lost using Binance's client ID."""
    order = client.fetch_order(None, symbol, {"origClientOrderId": client_order_id})
    if not isinstance(order, dict) or not order.get("id"):
        raise RuntimeError(
            f"could not reconcile buy order by client ID {client_order_id}: {order!r}"
        )
    return order


def _cancel_and_wait_for_order_terminal(
    broker: Any,
    client: Any,
    order_id: str,
    symbol: str,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    """Cancel an active/uncertain buy and verify its terminal state before cleanup."""
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    last_fetch_error: Exception | None = None
    last_cancel_error: Exception | None = None
    while True:
        # A fetch failure must not suppress cancellation when the exchange order ID
        # is already known. Continue trying both operations until terminal state is
        # positively identified or the bounded reconciliation deadline expires.
        try:
            state = broker.fetch_order(order_id, symbol)
            if not isinstance(state, dict) or str(state.get("id") or "") != str(order_id):
                raise RuntimeError(
                    f"exchange returned malformed or mismatched order state for {order_id}: {state!r}"
                )
            last = state
            if str(state.get("status", "")).lower() in SAFE_TERMINAL_ORDER_STATES:
                return state
            last_fetch_error = None
        except Exception as exc:
            last_fetch_error = exc

        try:
            client.cancel_order(order_id, symbol)
            last_cancel_error = None
        except Exception as exc:
            last_cancel_error = exc

        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"could not verify buy order {order_id} is terminal after cancellation; "
                f"last state={last!r}; fetch error={last_fetch_error!r}; "
                f"cancel error={last_cancel_error!r}"
            )
        time.sleep(0.5)


def _reconcile_buy_order(
    broker: Any,
    client: Any,
    *,
    symbol: str,
    base_asset: str,
    client_order_id: str,
    order_id: str | None,
) -> tuple[str, dict[str, Any], Decimal]:
    """Resolve ambiguous submissions, stop open buys, and retain verified partial fills."""
    if order_id:
        try:
            state = broker.fetch_order(order_id, symbol)
            if not isinstance(state, dict) or str(state.get("id") or "") != str(order_id):
                raise RuntimeError(
                    f"exchange returned malformed or mismatched order state for {order_id}: {state!r}"
                )
        except Exception as initial_read_error:
            try:
                state = _cancel_and_wait_for_order_terminal(broker, client, order_id, symbol)
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"known buy order {order_id} could not be read and terminal state could not "
                    f"be confirmed after cancellation; initial read error={initial_read_error!r}; "
                    f"reconciliation error={cleanup_error}"
                ) from cleanup_error
    else:
        state = _fetch_order_by_client_id(client, symbol, client_order_id)
        if not isinstance(state, dict) or not state.get("id"):
            raise RuntimeError(
                f"buy order lookup by client ID {client_order_id} returned no exchange order ID"
            )
        order_id = str(state["id"])

    if not isinstance(state, dict):
        raise RuntimeError(f"exchange returned malformed order state for {order_id}")
    if str(state.get("id") or "") != str(order_id):
        raise RuntimeError(f"exchange returned mismatched order state for {order_id}: {state!r}")
    if str(state.get("status", "")).lower() not in SAFE_TERMINAL_ORDER_STATES:
        state = _cancel_and_wait_for_order_terminal(broker, client, order_id, symbol)
    status = str(state.get("status", "")).lower()
    if status not in SAFE_TERMINAL_ORDER_STATES:
        raise RuntimeError(
            f"buy order {order_id} is not terminal; refusing cleanup while it may still fill: {state!r}"
        )
    # Preserve positive partial fills even when the final state is canceled/expired.
    return order_id, state, _net_filled_base(state, base_asset)


def _market_order_min_notional_rule(
    market: dict[str, Any],
) -> list[tuple[Decimal, int | None]]:
    """Return every active market-order minimum and its own reference-price window."""
    info = market.get("info") if isinstance(market.get("info"), dict) else {}
    filters = info.get("filters") if isinstance(info.get("filters"), list) else []
    rules: list[tuple[Decimal, int | None]] = []
    saw_notional_filter = False
    for rule in filters:
        if not isinstance(rule, dict) or rule.get("filterType") not in {"MIN_NOTIONAL", "NOTIONAL"}:
            continue
        saw_notional_filter = True
        applies = rule.get("applyToMarket", rule.get("applyMinToMarket", True))
        if str(applies).strip().lower() in {"false", "0", "no"}:
            continue
        minimum = dec(rule.get("minNotional") or rule.get("notional") or "0")
        if minimum <= 0:
            continue
        try:
            avg_price_mins: int | None = int(rule.get("avgPriceMins", 0))
            if avg_price_mins < 0:
                avg_price_mins = None
        except (TypeError, ValueError):
            avg_price_mins = None
        rules.append((minimum, avg_price_mins))
    if rules:
        # Do not collapse independent constraints: different filters can require
        # reference prices from different windows, even when one nominal minimum
        # is lower than another.
        return sorted(set(rules), key=lambda item: (item[0], -1 if item[1] is None else item[1]))
    if saw_notional_filter:
        # Exchange metadata explicitly provided notional filters and all of them
        # said the minimum does not apply to market orders. Do not resurrect an
        # inactive filter through CCXT's generic cost.min summary.
        return []
    limits = market.get("limits") if isinstance(market.get("limits"), dict) else {}
    cost = limits.get("cost") if isinstance(limits.get("cost"), dict) else {}
    fallback = dec(cost.get("min") or "0")
    return [(fallback, None if fallback > 0 else 0)]


def _market_order_reference_price(
    client: Any, symbol: str, avg_price_mins: int | None, last_price: Decimal
) -> Decimal | None:
    """Use Binance's applicable market-order reference price, never a mismatched average window."""
    if avg_price_mins is None:
        return None
    if avg_price_mins == 0:
        return last_price if last_price > 0 else None
    endpoint = getattr(client, "publicGetAvgPrice", None) or getattr(client, "public_get_avg_price", None)
    if not callable(endpoint):
        return None
    try:
        market_fn = getattr(client, "market", None)
        market_info = market_fn(symbol) if callable(market_fn) else None
        market_id = market_info.get("id") if isinstance(market_info, dict) else None
        if not market_id:
            market_id = symbol.replace("/", "").split(":")[0]
        response = endpoint({"symbol": market_id})
        if not isinstance(response, dict) or int(response.get("mins", -1)) != avg_price_mins:
            return None
        price = dec(response.get("price"))
        return price if price > 0 else None
    except Exception:
        return None


def _protective_stop_minimum_buy_budget(
    min_cost_rules: list[tuple[Decimal, int | None]],
    last_price: Decimal,
    stop_price: Decimal,
) -> Decimal:
    """Return quote budget that leaves margin for a stop sell at its trigger price."""
    if not min_cost_rules:
        return Decimal("0")
    if last_price <= 0 or stop_price <= 0 or stop_price >= last_price:
        raise RuntimeError(
            f"invalid protective-stop sizing prices: last={last_price}, trigger={stop_price}"
        )
    return max(
        (
            minimum * STOP_MIN_NOTIONAL_SIZING_HEADROOM * last_price / stop_price
            for minimum, _window in min_cost_rules
            if minimum > 0
        ),
        default=Decimal("0"),
    )


def _validate_protective_stop_min_notional(
    stop_amount: Decimal,
    stop_price: Decimal,
    min_cost_rules: list[tuple[Decimal, int | None]],
) -> None:
    """Fail closed unless the rounded stop quantity preserves headroom at its trigger."""
    if not min_cost_rules:
        return
    if stop_amount <= 0 or stop_price <= 0:
        raise RuntimeError(
            f"invalid protective-stop quantity/trigger: amount={stop_amount}, trigger={stop_price}"
        )
    trigger_notional = stop_amount * stop_price
    for minimum, window in min_cost_rules:
        required = minimum * STOP_MIN_NOTIONAL_VALIDATION_HEADROOM
        if minimum > 0 and trigger_notional < required:
            raise RuntimeError(
                f"normalized protective-stop market-sell notional {trigger_notional} at trigger "
                f"price {stop_price} is below the {STOP_MIN_NOTIONAL_VALIDATION_HEADROOM}x "
                f"minimum {minimum} for avgPriceMins={window}; refusing to place an "
                "undersized protective stop"
            )



def _cancel_stop_if_present(
    client: Any,
    symbol: str,
    stop_id: str | None,
    stop_client_id: str | None,
    errors: list[str],
) -> bool:
    """Cancel a stop and verify a terminal order state before flattening base.

    Absence from one open-orders response is not proof that a stop was never
    accepted or has been canceled. In particular, an ambiguous create response
    can leave us with only the client order ID. In that case we must find and
    verify the order, or fail closed without selling any base.
    """
    if not stop_id and not stop_client_id:
        return True

    candidate_ids: set[str] = {str(stop_id)} if stop_id else set()
    confirmed_terminal: set[str] = set()
    last_error: Exception | None = None
    safe_terminal_states = TERMINAL_ORDER_STATES | CANCELED_ORDER_STATES

    for attempt in range(3):
        # An ambiguously accepted stop may already be terminal or absent from a
        # lagging open-orders response. Resolve its retained client order ID
        # directly first, then verify the returned exchange ID's terminal state.
        if stop_client_id and not stop_id:
            try:
                discovered = client.fetch_order(
                    None, symbol, {"origClientOrderId": stop_client_id}
                )
                if not isinstance(discovered, dict) or not discovered.get("id"):
                    raise RuntimeError(
                        f"protective stop lookup by client ID {stop_client_id} returned no exchange order ID"
                    )
                discovered_id = str(discovered["id"])
                candidate_ids.add(discovered_id)
                if str(discovered.get("status", "")).lower() in safe_terminal_states:
                    confirmed_terminal.add(discovered_id)
            except Exception as exc:
                # Keep trying the open-order listing and known IDs. If neither
                # route establishes the order's identity/state, cleanup fails closed.
                last_error = exc
        try:
            open_orders = client.fetch_open_orders(symbol)
            if not isinstance(open_orders, list):
                raise RuntimeError("exchange returned malformed open-order data")
            for order in open_orders:
                if not isinstance(order, dict):
                    continue
                info = order.get("info") if isinstance(order.get("info"), dict) else {}
                client_id = order.get("clientOrderId") or info.get("clientOrderId")
                order_id = order.get("id")
                matches_client = bool(
                    stop_client_id and str(client_id or "") == stop_client_id
                )
                matches_known_id = bool(order_id is not None and str(order_id) in candidate_ids)
                if not (matches_client or matches_known_id):
                    continue
                if order_id is None:
                    last_error = RuntimeError(
                        f"matching protective stop {stop_client_id or stop_id} has no exchange order ID"
                    )
                    continue
                order_id = str(order_id)
                candidate_ids.add(order_id)
                if order_id in confirmed_terminal:
                    continue
                try:
                    # A cancellation response is an acknowledgement, not proof that
                    # the order is terminal. Only fetch_order below can confirm that.
                    client.cancel_order(order_id, symbol)
                except Exception as exc:
                    last_error = exc
        except Exception as exc:
            last_error = exc

        # A cancellation request or a missing open-orders match is insufficient.
        # Confirm every known order by ID; retry cancellation while it is active.
        for order_id in list(candidate_ids):
            if order_id in confirmed_terminal:
                continue
            try:
                order_state = client.fetch_order(order_id, symbol)
                if not isinstance(order_state, dict):
                    raise RuntimeError(f"exchange returned malformed order state for {order_id}")
                status = str(order_state.get("status", "")).lower()
                if status in safe_terminal_states:
                    confirmed_terminal.add(order_id)
                    continue
                if status:
                    # A cancellation response is an acknowledgement, not proof that
                    # the order is terminal. Only fetch_order below can confirm that.
                    client.cancel_order(order_id, symbol)
            except Exception as exc:
                last_error = exc

        if candidate_ids and candidate_ids.issubset(confirmed_terminal):
            return True

        if attempt < 2:
            time.sleep(0.5)

    errors.append(
        f"could not verify protective stop {stop_client_id or stop_id} is terminal; "
        f"refusing to flatten while a stop may remain live"
        + (f" ({last_error})" if last_error is not None else "")
    )
    return False


def cleanup_new_spot_base(
    broker: Any,
    client: Any,
    *,
    symbol: str,
    base_asset: str,
    initial_free_base: Decimal,
    drill_base_amount: Decimal,
    min_amount: Decimal,
    min_cost: Decimal = Decimal("0"),
    min_cost_reference_mins: int | None = 0,
    min_cost_rules: list[tuple[Decimal, int | None]] | None = None,
    stop_id: str | None,
    stop_client_id: str | None,
) -> dict[str, Any]:
    """Best-effort cleanup, capped to this drill's acquired base and verified exchange minima."""
    errors: list[str] = []
    stop_safe = _cancel_stop_if_present(client, symbol, stop_id, stop_client_id, errors)

    try:
        current = _free_balance(client, base_asset)
    except Exception as exc:
        errors.append(f"could not read free {base_asset} balance for cleanup: {exc}")
        return {
            "ok": False,
            "remaining_base": None,
            "residual_dust": None,
            "errors": errors,
        }

    # The fill cap is only a quantity ceiling; a balance delta cannot prove
    # provenance when multiple actors use the same account. main() requires an
    # exclusive-account attestation and a clean open-order preflight before trading.
    drill_cap = max(Decimal("0"), drill_base_amount)
    free_delta = max(Decimal("0"), current - initial_free_base)
    if not stop_safe:
        return {
            "ok": False,
            "remaining_base": str(min(free_delta, drill_cap)),
            "residual_dust": None,
            "errors": errors,
        }
    # After canceling this drill's stop, any balance delta different from the
    # confirmed net fill means provenance is uncertain (concurrent debit/credit,
    # fee discrepancy, or external activity). Do not sell a capped approximation.
    if free_delta != drill_cap:
        errors.append(
            f"free {base_asset} balance delta {free_delta} does not exactly match this drill's "
            f"confirmed net fill {drill_cap}; refusing cleanup because asset provenance is unverified"
        )
        return {
            "ok": False,
            "remaining_base": str(free_delta),
            "residual_dust": None,
            "errors": errors,
        }
    new_base = drill_cap

    notional_rules = min_cost_rules
    if notional_rules is None:
        notional_rules = [(min_cost, min_cost_reference_mins)] if min_cost > 0 else []
    reference_prices: dict[int | None, Decimal | None] = {}
    active_rules = [(minimum, window) for minimum, window in notional_rules if minimum > 0]
    if active_rules:
        try:
            ticker = broker.ticker(symbol)
            last_price = dec(ticker.get("last") if isinstance(ticker, dict) else None)
            if last_price <= 0:
                raise RuntimeError(f"exchange returned an invalid cleanup price: {last_price}")
            for _, window in active_rules:
                if window not in reference_prices:
                    reference_prices[window] = _market_order_reference_price(
                        client, symbol, window, last_price
                    )
        except Exception as exc:
            errors.append(f"could not verify minimum-notional rule for cleanup: {exc}")

    def is_sellable(amount: Decimal) -> bool | None:
        if amount <= 0:
            return False
        if min_amount > 0 and amount < min_amount:
            return False
        unknown_reference = False
        for minimum, window in active_rules:
            reference_price = reference_prices.get(window)
            if reference_price is None:
                unknown_reference = True
                continue
            if amount * reference_price < minimum:
                return False
        return None if unknown_reference else True

    sold_amount = Decimal("0")
    try:
        sell_amount = dec(broker.normalize_amount(symbol, float(new_base)))
        if sell_amount < 0:
            raise RuntimeError("Exchange adapter normalized a negative cleanup amount")
        if sell_amount > drill_cap:
            raise RuntimeError("Exchange adapter normalized cleanup above this drill's acquired amount")
        # Unknown reference price is not proof of dust: attempt the exchange order,
        # then fail closed if the resulting residual still cannot be classified.
        if is_sellable(sell_amount) is not False:
            flat = broker.market_order(
                symbol,
                "sell",
                float(sell_amount),
                "atlas-flat-" + uuid.uuid4().hex[:20],
            )
            if not isinstance(flat, dict) or not flat.get("id"):
                raise RuntimeError(f"Cleanup market sell did not return an order ID: {flat!r}")
            flat_order = _wait_for_order(broker, str(flat["id"]), symbol)
            if str(flat_order.get("status", "")).lower() not in TERMINAL_ORDER_STATES:
                raise RuntimeError(f"Cleanup market sell did not finish before timeout: {flat_order!r}")
            sold_amount = min(drill_cap, max(Decimal("0"), dec(flat_order.get("filled") or "0")))
            if sold_amount <= 0:
                raise RuntimeError(f"Cleanup market sell did not fill: {flat_order!r}")
    except Exception as exc:
        errors.append(f"cleanup market sell failed: {exc}")

    try:
        final_free = _free_balance(client, base_asset)
        remaining_cap = max(Decimal("0"), drill_cap - sold_amount)
        residual = min(max(Decimal("0"), final_free - initial_free_base), remaining_cap)
        normalized_residual = dec(broker.normalize_amount(symbol, float(residual)))
        sellable_residual: bool | None
        try:
            sellable_residual = is_sellable(normalized_residual)
        except Exception as exc:
            errors.append(f"could not verify whether residual base is sellable: {exc}")
            sellable_residual = None
        if sellable_residual is True:
            errors.append(
                f"sellable {base_asset} residual remains after cleanup: {residual}"
            )
        elif residual > 0 and sellable_residual is None:
            errors.append(
                f"{base_asset} residual {residual} is unverified; refusing to report it as dust "
                "without the applicable Binance market-order notional reference price"
            )
        return {
            "ok": not errors,
            "remaining_base": str(residual),
            "residual_dust": (
                "0" if residual == 0 else str(residual) if sellable_residual is False else None
            ),
            "errors": errors,
        }
    except Exception as exc:
        errors.append(f"could not verify final {base_asset} balance: {exc}")
        return {
            "ok": False,
            "remaining_base": None,
            "residual_dust": None,
            "errors": errors,
        }


def require_empty_starting_base(initial_free_base: Decimal, base_asset: str) -> None:
    """Require a dedicated Spot Testnet account with no pre-existing free base."""
    if initial_free_base != 0:
        raise RuntimeError(
            f"Refusing Spot Testnet drill: starting free {base_asset} balance is "
            f"{initial_free_base}. Use a dedicated account with zero base balance so "
            "cleanup cannot sell pre-existing holdings."
        )


def main() -> int:
    if os.getenv("ATLAS_BINANCE_TESTNET", "").strip().lower() != "true":
        raise SystemExit("Set ATLAS_BINANCE_TESTNET=true to run the external testnet drill")
    if os.getenv("ATLAS_TESTNET_ACCOUNT_EXCLUSIVE", "").strip().lower() != "true":
        raise SystemExit(
            "Set ATLAS_TESTNET_ACCOUNT_EXCLUSIVE=true only after confirming this dedicated "
            "Spot Testnet account will not be used by another actor/process during the drill"
        )
    key = req("ATLAS_TESTNET_BINANCE_API_KEY")
    secret = req("ATLAS_TESTNET_BINANCE_API_SECRET")
    symbol = os.getenv("ATLAS_TESTNET_SYMBOL", "BTC/USDT").strip()
    if not symbol or "/" not in symbol:
        raise SystemExit("ATLAS_TESTNET_SYMBOL must be a Spot symbol such as BTC/USDT")

    account = SimpleNamespace(
        status="VERIFIED",
        can_trade=True,
        universal_transfer=False,
        enable_withdrawals=False,
        api_key=key,
        secret_ref="testnet-env-secret",
        market_type="spot",
    )
    broker = build_customer_binance_broker(
        account, secret_fetcher=lambda _: secret, sandbox=True
    )
    client = broker.connect()
    active_endpoints = require_testnet_spot_endpoints(client)
    if not broker.config.sandbox:
        raise RuntimeError("Refusing non-sandbox broker configuration")

    market = broker.market_info(symbol)
    ticker = broker.ticker(symbol)
    last = dec(ticker.get("last"))
    if last <= 0:
        raise RuntimeError(f"Exchange returned an invalid last price: {last}")
    limits = market.get("limits") or {}
    min_cost_rules = _market_order_min_notional_rule(market)
    min_cost = max((minimum for minimum, _ in min_cost_rules), default=Decimal("0"))
    min_cost_reference_mins = (
        min_cost_rules[0][1] if len(min_cost_rules) == 1 else None
    )
    min_amount = dec(((limits.get("amount") or {}).get("min")) or "0")
    stop_price = dec(broker.normalize_price(symbol, float(last * STOP_TRIGGER_PRICE_RATIO)))
    if stop_price <= 0 or stop_price >= last:
        raise RuntimeError(
            f"exchange price normalization produced an invalid protective-stop trigger: {stop_price}"
        )
    # Size the buy against the buy-market reference windows and leave extra net-base
    # headroom for base-asset fees, quantity rounding and the 20% stop-trigger drop.
    required_budget = Decimal("15")
    for minimum, window in min_cost_rules:
        if minimum <= 0:
            continue
        reference_price = _market_order_reference_price(client, symbol, window, last)
        if reference_price is None or reference_price <= 0:
            raise RuntimeError(
                f"Cannot verify Binance market-order reference price for avgPriceMins={window}; "
                "refusing to start the testnet drill"
            )
        required_budget = max(
            required_budget,
            minimum * last / reference_price * BUY_MARKET_MIN_NOTIONAL_HEADROOM,
        )
    required_budget = max(
        required_budget,
        _protective_stop_minimum_buy_budget(min_cost_rules, last, stop_price),
    )
    amount = dec(broker.normalize_amount(symbol, float(max(min_amount, required_budget / last))))
    if amount <= 0:
        raise RuntimeError(f"Invalid test amount {amount}; price={last}; min_cost={min_cost}")
    for minimum, window in min_cost_rules:
        if minimum <= 0:
            continue
        reference_price = _market_order_reference_price(client, symbol, window, last)
        if (
            reference_price is None
            or amount * reference_price < minimum * BUY_MARKET_MIN_NOTIONAL_HEADROOM
        ):
            raise RuntimeError(
                f"Test amount {amount} does not preserve buy-market notional headroom for "
                f"minimum {minimum} at avgPriceMins={window}"
            )

    base_asset = symbol.split("/")[0].split(":")[0]
    initial_free_base = _free_balance(client, base_asset)
    require_empty_starting_base(initial_free_base, base_asset)
    # Existing open orders make it impossible to attribute subsequent balances and
    # stop activity solely to this drill. Fail closed before any market order.
    open_orders = client.fetch_open_orders(symbol)
    if not isinstance(open_orders, list) or open_orders:
        raise RuntimeError(
            "Refusing Spot Testnet drill: could not verify an empty starting open-order set"
        )
    report: dict[str, Any] = {
        "environment": "BINANCE_SPOT_TESTNET",
        "symbol": symbol,
        "active_spot_api_endpoints": active_endpoints,
        "account_exclusivity_attested": True,
        "steps": [],
    }
    report["steps"].append({"step": "authenticated_balance", "ok": True})

    buy_attempted = False
    buy_client_order_id: str | None = None
    buy_order_id: str | None = None
    buy_order_terminal = False
    stop_id: str | None = None
    stop_client_id: str | None = None
    operation_error: Exception | None = None
    drill_base_amount = Decimal("0")

    try:
        buy_attempted = True
        # Keep the client ID before network submission so a lost response can be
        # reconciled instead of losing the order identity in the finally path.
        buy_client_order_id = "atlas-tn-" + uuid.uuid4().hex[:20]
        order = broker.market_order(symbol, "buy", float(amount), buy_client_order_id)
        if isinstance(order, dict) and order.get("id"):
            buy_order_id = str(order["id"])
        if not isinstance(order, dict) or not order.get("id"):
            raise RuntimeError(f"Testnet market buy did not return an order ID: {order!r}")
        order = _wait_for_order(broker, buy_order_id, symbol)
        # Record fills before status validation: canceled/expired orders may still
        # contain a positive partial fill which cleanup must attribute and flatten.
        drill_base_amount = _net_filled_base(order, base_asset)
        if str(order.get("status", "")).lower() not in SAFE_TERMINAL_ORDER_STATES:
            buy_order_id, order, drill_base_amount = _reconcile_buy_order(
                broker, client, symbol=symbol, base_asset=base_asset,
                client_order_id=buy_client_order_id, order_id=buy_order_id,
            )
        buy_order_terminal = str(order.get("status", "")).lower() in SAFE_TERMINAL_ORDER_STATES
        if (
            str(order.get("status", "")).lower() not in TERMINAL_ORDER_STATES
            or dec(order.get("filled") or "0") <= 0
        ):
            raise RuntimeError(f"Testnet market order did not fill completely: {order!r}")
        # Do not infer zero commissions from an order-status response. Use the
        # complete, order-specific trade ledger before sizing either the stop or cleanup.
        drill_base_amount = _verified_net_filled_base(
            client, symbol, buy_order_id, order, base_asset
        )
        report["steps"].append({
            "step": "buy_trade_fee_reconciliation",
            "ok": True,
            "net_filled_base": str(drill_base_amount),
        })
        if drill_base_amount <= 0:
            raise RuntimeError("Filled order did not establish a positive verified net base-asset quantity")
        report["steps"].append(
            {
                "step": "spot_market_fill",
                "ok": True,
                "order_id": buy_order_id,
                "filled": str(order.get("filled")),
            }
        )

        order_types = set((market.get("info") or {}).get("orderTypes") or [])
        if "STOP_LOSS" not in order_types:
            raise RuntimeError(
                "Binance exchangeInfo does not advertise STOP_LOSS for this Spot "
                "symbol; refusing a false protective-stop PASS"
            )
        report["steps"].append(
            {"step": "protective_stop_capability", "ok": True, "order_type": "STOP_LOSS"}
        )

        # Use free base actually received, not the order's gross filled amount.
        # Binance may deduct commissions in the base asset.
        free_delta = max(
            Decimal("0"), _free_balance(client, base_asset) - initial_free_base
        )
        new_free_base = min(free_delta, drill_base_amount)
        stop_amount = dec(broker.normalize_amount(symbol, float(new_free_base)))
        if stop_amount <= 0 or (min_amount > 0 and stop_amount < min_amount):
            raise RuntimeError(
                f"Received base amount is not sellable for protective-stop drill: {new_free_base}"
            )

        _validate_protective_stop_min_notional(stop_amount, stop_price, min_cost_rules)
        stop_client_id = "atlas-sl-" + uuid.uuid4().hex[:20]
        stop = client.create_order(
            symbol,
            "STOP_LOSS",
            "sell",
            float(stop_amount),
            None,
            {"stopPrice": float(stop_price), "clientOrderId": stop_client_id},
        )
        if not isinstance(stop, dict) or not stop.get("id"):
            raise RuntimeError(f"Protective stop did not return an exchange order ID: {stop!r}")
        stop_id = str(stop["id"])
        report["steps"].append(
            {"step": "protective_stop_created", "ok": True, "order_id": stop_id}
        )
    except Exception as exc:
        operation_error = exc
        report["steps"].append(
            {"step": "testnet_drill_operation", "ok": False, "error": str(exc)}
        )
    finally:
        if buy_attempted:
            # Reconcile every attempted buy, including a submission whose response
            # was lost. Never flatten while a buy can still acquire additional base.
            reconciliation_error: Exception | None = None
            try:
                if not buy_client_order_id:
                    raise RuntimeError("buy client order ID was not retained before submission")
                buy_order_id, final_buy_state, final_drill_cap = _reconcile_buy_order(
                    broker, client, symbol=symbol, base_asset=base_asset,
                    client_order_id=buy_client_order_id, order_id=buy_order_id,
                )
                drill_base_amount = final_drill_cap
                buy_order_terminal = (
                    str(final_buy_state.get("status", "")).lower()
                    in SAFE_TERMINAL_ORDER_STATES
                )
                if buy_order_terminal:
                    try:
                        drill_base_amount = _verified_net_filled_base(
                            client, symbol, buy_order_id, final_buy_state, base_asset
                        )
                        report["steps"].append({
                            "step": "buy_trade_fee_reconciliation",
                            "ok": True,
                            "net_filled_base": str(drill_base_amount),
                        })
                    except Exception as exc:
                        reconciliation_error = exc
                        buy_order_terminal = False
                        report["steps"].append({
                            "step": "buy_trade_fee_reconciliation",
                            "ok": False,
                            "error": str(exc),
                        })
            except Exception as exc:
                reconciliation_error = exc
                buy_order_terminal = False
                report["steps"].append(
                    {"step": "buy_order_reconciliation", "ok": False, "error": str(exc)}
                )

            if buy_order_terminal:
                cleanup = cleanup_new_spot_base(
                    broker,
                    client,
                    symbol=symbol,
                    base_asset=base_asset,
                    initial_free_base=initial_free_base,
                    drill_base_amount=drill_base_amount,
                    min_amount=min_amount,
                    min_cost=min_cost,
                    min_cost_reference_mins=min_cost_reference_mins,
                    min_cost_rules=min_cost_rules,
                    stop_id=stop_id,
                    stop_client_id=stop_client_id,
                )
            else:
                # A stop may have been accepted even when the buy reconciliation
                # endpoint is failing. Always try to cancel it by exchange/client ID,
                # while keeping every cleanup sell gated on a confirmed terminal buy.
                stop_cancel_errors: list[str] = []
                stop_cancelled = _cancel_stop_if_present(
                    client, symbol, stop_id, stop_client_id, stop_cancel_errors
                )
                cleanup = {
                    "ok": False,
                    "remaining_base": None,
                    "residual_dust": None,
                    "stop_cancelled": stop_cancelled,
                    "errors": [
                        "buy order could not be proven terminal; no cleanup sell was attempted",
                        str(reconciliation_error) if reconciliation_error else "unknown buy-order reconciliation failure",
                        *stop_cancel_errors,
                    ],
                }
            report["steps"].append(
                {"step": "emergency_cancel_and_spot_flatten", **cleanup}
            )
        else:
            cleanup = {"ok": True, "remaining_base": "0", "residual_dust": "0", "errors": []}

    print(report, flush=True)
    if operation_error is not None:
        raise RuntimeError(
            "Testnet acceptance drill failed: "
            + str(operation_error)
            + "; cleanup="
            + str(cleanup)
        ) from operation_error
    if not cleanup.get("ok"):
        raise RuntimeError(f"Spot Testnet cleanup could not be verified: {cleanup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
