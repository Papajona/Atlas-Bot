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


def _wait_for_order(broker: Any, order_id: str, symbol: str, timeout_seconds: int = 30) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last: dict = {}
    while True:
        last = broker.fetch_order(order_id, symbol)
        if not isinstance(last, dict):
            raise RuntimeError(f"Exchange returned malformed order state for {order_id}")
        if str(last.get("status", "")).lower() in TERMINAL_ORDER_STATES:
            return last
        if time.monotonic() >= deadline:
            return last
        time.sleep(1)


def _cancel_stop_if_present(
    client: Any,
    symbol: str,
    stop_id: str | None,
    stop_client_id: str | None,
    errors: list[str],
) -> bool:
    """Cancel a known stop or fail closed when an ambiguously-created stop cannot be ruled out."""
    if not stop_id and not stop_client_id:
        return True

    cancel_error: Exception | None = None
    cancel_requested = False
    if stop_id:
        try:
            client.cancel_order(stop_id, symbol)
            cancel_requested = True
            if not stop_client_id:
                return True
        except Exception as exc:
            cancel_error = exc

    if not stop_client_id:
        if cancel_error is not None:
            errors.append(f"could not cancel protective stop {stop_id}: {cancel_error}")
            return False
        return cancel_requested

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            open_orders = client.fetch_open_orders(symbol)
            if not isinstance(open_orders, list):
                raise RuntimeError("exchange returned malformed open-order data")
            matched = []
            for order in open_orders:
                info = order.get("info") if isinstance(order.get("info"), dict) else {}
                client_id = order.get("clientOrderId") or info.get("clientOrderId")
                if str(client_id or "") == stop_client_id and order.get("id") is not None:
                    matched.append(order)
            if matched:
                for order in matched:
                    client.cancel_order(str(order["id"]), symbol)
                cancel_requested = True
                last_error = None
            elif cancel_requested:
                # A previously accepted cancellation plus a subsequent authoritative
                # open-order read is sufficient to proceed with flattening.
                return True
            elif attempt == 2:
                # The create/cancel response may have been lost before the exchange
                # made an accepted order visible. Absence from one or more list reads
                # is not proof that no live stop exists, so do not sell around it.
                errors.append(
                    f"could not verify protective stop {stop_client_id} is absent; "
                    "refusing to flatten while a stop may remain open"
                )
                return False
        except Exception as exc:
            last_error = exc
            if attempt == 2:
                if cancel_error is not None:
                    errors.append(f"could not cancel protective stop {stop_id}: {cancel_error}")
                errors.append(
                    f"could not reconcile/cancel protective stop {stop_client_id}: {last_error}; "
                    "refusing to flatten"
                )
                return False
        if attempt < 2:
            time.sleep(0.5)

    errors.append(f"could not verify protective stop {stop_client_id} is absent; refusing to flatten")
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

    # A wallet balance delta may include unrelated deposits or concurrent trades.
    # Never sell more than this order's filled quantity, reduced by base-denominated fees.
    drill_cap = max(Decimal("0"), drill_base_amount)
    free_delta = max(Decimal("0"), current - initial_free_base)
    new_base = min(free_delta, drill_cap)
    if not stop_safe:
        return {
            "ok": False,
            "remaining_base": str(new_base),
            "residual_dust": None,
            "errors": errors,
        }

    cleanup_price: Decimal | None = None
    if min_cost > 0:
        try:
            ticker = broker.ticker(symbol)
            cleanup_price = dec(ticker.get("last") if isinstance(ticker, dict) else None)
            if cleanup_price <= 0:
                raise RuntimeError(f"exchange returned an invalid cleanup price: {cleanup_price}")
        except Exception as exc:
            errors.append(f"could not verify minimum-notional rule for cleanup: {exc}")

    def is_sellable(amount: Decimal) -> bool:
        if amount <= 0:
            return False
        if min_amount > 0 and amount < min_amount:
            return False
        if min_cost > 0:
            if cleanup_price is None:
                raise RuntimeError("current market price is unavailable for minimum-notional validation")
            if amount * cleanup_price < min_cost:
                return False
        return True

    sold_amount = Decimal("0")
    try:
        sell_amount = dec(broker.normalize_amount(symbol, float(new_base)))
        if sell_amount < 0:
            raise RuntimeError("Exchange adapter normalized a negative cleanup amount")
        if sell_amount > drill_cap:
            raise RuntimeError("Exchange adapter normalized cleanup above this drill's acquired amount")
        if is_sellable(sell_amount):
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


def main() -> int:
    if os.getenv("ATLAS_BINANCE_TESTNET", "").strip().lower() != "true":
        raise SystemExit("Set ATLAS_BINANCE_TESTNET=true to run the external testnet drill")
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
    min_cost = dec(((limits.get("cost") or {}).get("min")) or "0")
    min_amount = dec(((limits.get("amount") or {}).get("min")) or "0")
    budget = max(Decimal("15"), min_cost * Decimal("1.25"))
    amount = dec(broker.normalize_amount(symbol, float(max(min_amount, budget / last))))
    if amount <= 0 or amount * last < min_cost:
        raise RuntimeError(f"Invalid test amount {amount}; price={last}; min_cost={min_cost}")

    base_asset = symbol.split("/")[0].split(":")[0]
    initial_free_base = _free_balance(client, base_asset)
    report: dict[str, Any] = {
        "environment": "BINANCE_SPOT_TESTNET",
        "symbol": symbol,
        "active_spot_api_endpoints": active_endpoints,
        "steps": [],
    }
    report["steps"].append({"step": "authenticated_balance", "ok": True})

    buy_attempted = False
    stop_id: str | None = None
    stop_client_id: str | None = None
    operation_error: Exception | None = None
    fill: dict[str, Any] | None = None
    drill_base_amount = Decimal("0")

    try:
        buy_attempted = True
        order = broker.market_order(
            symbol, "buy", float(amount), "atlas-tn-" + uuid.uuid4().hex[:20]
        )
        if not isinstance(order, dict) or not order.get("id"):
            raise RuntimeError(f"Testnet market buy did not return an order ID: {order!r}")
        oid = str(order["id"])
        order = _wait_for_order(broker, oid, symbol)
        if str(order.get("status", "")).lower() not in TERMINAL_ORDER_STATES or dec(
            order.get("filled") or "0"
        ) <= 0:
            raise RuntimeError(f"Testnet market order did not fill: {order!r}")
        fill = order
        drill_base_amount = _net_filled_base(order, base_asset)
        if drill_base_amount <= 0:
            raise RuntimeError("Filled order did not establish a positive net base-asset quantity")
        report["steps"].append(
            {
                "step": "spot_market_fill",
                "ok": True,
                "order_id": oid,
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

        stop_price = dec(broker.normalize_price(symbol, float(last * Decimal("0.80"))))
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
            cleanup = cleanup_new_spot_base(
                broker,
                client,
                symbol=symbol,
                base_asset=base_asset,
                initial_free_base=initial_free_base,
                drill_base_amount=drill_base_amount,
                min_amount=min_amount,
                min_cost=min_cost,
                stop_id=stop_id,
                stop_client_id=stop_client_id,
            )
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
