"""Isolated, append-only customer paper order/fill/position lifecycle.

This module deliberately never imports the real-money ledger or broker adapters. Every
operation writes only to the separately configured paper database after the full
isolation preflight passes. Feature enablement remains off by default.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Any

from sqlalchemy import text

from .customer_paper_ledger import (
    CustomerPaperIdempotencyConflict,
    CustomerPaperLedgerBlocked,
    _assert_customer_paper_ready,
    _paper_engine,
    _validated_paper_journal,
)


_AMOUNT_QUANTUM = Decimal("0.000000000000000001")
_CURRENCY_RE = re.compile(r"^[A-Z0-9]{2,12}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9/_:.-]{0,79}$")
_VENUE_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_MARKET_TYPES = {"spot", "swap", "forex", "commodity"}


class CustomerPaperOrderConflict(CustomerPaperLedgerBlocked):
    """An immutable customer paper order conflicts with an existing intent or state."""


def _customer_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("customer_id must be a positive integer")
    return value


def _key(value: str, name: str, max_length: int = 150) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{name} must contain 1 to {max_length} characters")
    return normalized


def _input_decimal(value: Any, name: str, *, positive: bool = False, non_negative: bool = False) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{name} must be an exact decimal string, integer, or Decimal")
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError(f"{name} must be finite")
        with localcontext() as context:
            context.prec = 50
            quantized = result.quantize(_AMOUNT_QUANTUM)
    except (InvalidOperation, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"{name} is outside numeric(38,18) precision") from exc
    if result != quantized or max(quantized.adjusted() + 1, 0) > 20:
        raise ValueError(f"{name} must be exactly representable as numeric(38,18)")
    if positive and quantized <= 0:
        raise ValueError(f"{name} must be positive")
    if non_negative and quantized < 0:
        raise ValueError(f"{name} must be non-negative")
    return quantized


def _money(value: Decimal, name: str) -> Decimal:
    """Round a derived monetary result once to the database's 18-place scale."""
    try:
        with localcontext() as context:
            context.prec = 50
            result = Decimal(value).quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN)
    except InvalidOperation as exc:
        raise ValueError(f"{name} is outside numeric(38,18) precision") from exc
    if not result.is_finite() or max(result.adjusted() + 1, 0) > 20:
        raise ValueError(f"{name} is outside numeric(38,18) precision")
    return result


def _fmt(value: Decimal) -> str:
    return format(value.quantize(_AMOUNT_QUANTUM), "f")


def _canonical_sha(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError("metadata must be a JSON-compatible mapping")
    try:
        encoded = json.dumps(dict(value or {}), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be finite JSON data") from exc


async def _set_tenant(conn: Any, customer_id: int) -> None:
    await conn.execute(
        text("SELECT set_config('app.customer_id', :customer_id, true)"),
        {"customer_id": str(customer_id)},
    )


async def _append_journal_on_connection(
    conn: Any,
    *,
    customer_id: int,
    idempotency_key: str,
    event_type: str,
    currency: str,
    lines: list[dict[str, Any]],
    reference: str,
    metadata: Mapping[str, Any],
) -> str:
    """Append a balanced journal in the caller's transaction (no partial-fill window)."""
    payload = _validated_paper_journal(
        customer_id=customer_id,
        idempotency_key=idempotency_key,
        event_type=event_type,
        currency=currency,
        lines=lines,
        reference=reference,
        metadata=metadata,
    )
    inserted = (await conn.execute(text(
        """
        INSERT INTO customer_paper.journals
            (id, customer_id, idempotency_key, event_type, reference, metadata_json)
        VALUES
            (:id, :customer_id, :idempotency_key, :event_type, :reference,
             CAST(:metadata_json AS jsonb))
        ON CONFLICT (customer_id, idempotency_key) DO NOTHING
        RETURNING id
        """
    ), {
        "id": str(uuid.uuid4()),
        "customer_id": payload["customer_id"],
        "idempotency_key": payload["idempotency_key"],
        "event_type": payload["event_type"],
        "reference": payload["reference"],
        "metadata_json": json.dumps(
            payload["metadata_json"], sort_keys=True, separators=(",", ":"), allow_nan=False
        ),
    })).first()
    if inserted is None:
        raise CustomerPaperIdempotencyConflict(
            "journal idempotency key already exists outside the atomic paper lifecycle transaction"
        )
    journal_id = str(inserted[0])
    account_ids: dict[str, int] = {}
    for line in payload["lines"]:
        code = line["account_code"]
        account = (await conn.execute(text(
            """
            INSERT INTO customer_paper.accounts (customer_id, currency, account_code)
            VALUES (:customer_id, :currency, :account_code)
            ON CONFLICT (customer_id, currency, account_code) DO NOTHING
            RETURNING id
            """
        ), {
            "customer_id": customer_id,
            "currency": payload["currency"],
            "account_code": code,
        })).first()
        if account is None:
            account = (await conn.execute(text(
                """
                SELECT id FROM customer_paper.accounts
                WHERE customer_id=:customer_id AND currency=:currency AND account_code=:account_code
                """
            ), {
                "customer_id": customer_id,
                "currency": payload["currency"],
                "account_code": code,
            })).first()
        if account is None:
            raise CustomerPaperLedgerBlocked("paper account insert/read failed under tenant policy")
        account_ids[code] = int(account[0])

    for line in payload["lines"]:
        await conn.execute(text(
            """
            INSERT INTO customer_paper.journal_lines
                (journal_id, account_id, customer_id, currency, signed_amount)
            VALUES (:journal_id, :account_id, :customer_id, :currency, :signed_amount)
            """
        ), {
            "journal_id": journal_id,
            "account_id": account_ids[line["account_code"]],
            "customer_id": customer_id,
            "currency": payload["currency"],
            "signed_amount": Decimal(line["signed_amount"]),
        })
    return journal_id


async def _insert_order_event(
    conn: Any,
    *,
    customer_id: int,
    order_id: str,
    event_key: str,
    event_type: str,
    filled_quantity: Decimal,
    metadata: Mapping[str, Any],
) -> None:
    safe_metadata = json.dumps(dict(metadata), sort_keys=True, separators=(",", ":"), allow_nan=False)
    row = (await conn.execute(text(
        """
        INSERT INTO customer_paper.order_events
            (customer_id, order_id, event_key, event_type, filled_quantity, metadata_json)
        VALUES (:customer_id, :order_id, :event_key, :event_type, :filled_quantity, CAST(:metadata AS jsonb))
        ON CONFLICT (customer_id, event_key) DO NOTHING
        RETURNING id
        """
    ), {
        "customer_id": customer_id,
        "order_id": order_id,
        "event_key": event_key,
        "event_type": event_type,
        "filled_quantity": filled_quantity,
        "metadata": safe_metadata,
    })).first()
    if row is not None:
        return
    existing = (await conn.execute(text(
        """
        SELECT order_id, event_type, filled_quantity, metadata_json
        FROM customer_paper.order_events
        WHERE customer_id=:customer_id AND event_key=:event_key
        """
    ), {"customer_id": customer_id, "event_key": event_key})).mappings().first()
    if (
        existing is None
        or str(existing["order_id"]) != order_id
        or str(existing["event_type"]) != event_type
        or Decimal(existing["filled_quantity"]) != filled_quantity
        or _metadata(existing["metadata_json"] if isinstance(existing["metadata_json"], Mapping) else json.loads(existing["metadata_json"] or "{}")) != dict(metadata)
    ):
        raise CustomerPaperIdempotencyConflict("order-event idempotency key conflicts with an immutable event")


async def _current_order_state(conn: Any, customer_id: int, order_id: str) -> str:
    row = (await conn.execute(text(
        """
        SELECT event_type FROM customer_paper.order_events
        WHERE customer_id=:customer_id AND order_id=:order_id
        ORDER BY id DESC LIMIT 1
        """
    ), {"customer_id": customer_id, "order_id": order_id})).first()
    return str(row[0]) if row is not None else "UNKNOWN"


async def submit_customer_paper_order(
    *,
    customer_id: int,
    client_order_id: str,
    venue: str,
    market_type: str,
    symbol: str,
    side: str,
    quantity: str | int | Decimal,
    currency: str,
    order_type: str = "MARKET",
    limit_price: str | int | Decimal | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist an immutable order intent in the isolated paper database."""
    customer_id = _customer_id(customer_id)
    client_id = _key(client_order_id, "client_order_id", 180)
    normalized_venue = str(venue or "").strip().lower()
    if not _VENUE_RE.fullmatch(normalized_venue):
        raise ValueError("venue must be a normalized provider identifier")
    normalized_market = str(market_type or "").strip().lower()
    if normalized_market not in _MARKET_TYPES:
        raise ValueError("market_type must be one of spot, swap, forex, or commodity")
    normalized_symbol = str(symbol or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(normalized_symbol):
        raise ValueError("symbol is malformed")
    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    normalized_order_type = str(order_type or "").strip().upper()
    if normalized_order_type not in {"MARKET", "LIMIT"}:
        raise ValueError("order_type must be MARKET or LIMIT")
    amount = _input_decimal(quantity, "quantity", positive=True)
    normalized_currency = str(currency or "").strip().upper()
    if not _CURRENCY_RE.fullmatch(normalized_currency):
        raise ValueError("currency must be 2 to 12 uppercase alphanumeric characters")
    limit = None if limit_price is None else _input_decimal(limit_price, "limit_price", positive=True)
    if normalized_order_type == "LIMIT" and limit is None:
        raise ValueError("LIMIT orders require a positive limit_price")
    if normalized_order_type == "MARKET" and limit is not None:
        raise ValueError("MARKET orders must not set a limit_price")
    safe_metadata = _metadata(metadata)
    payload = {
        "customer_id": customer_id, "client_order_id": client_id, "venue": normalized_venue,
        "market_type": normalized_market, "symbol": normalized_symbol, "side": normalized_side,
        "order_type": normalized_order_type, "quantity": _fmt(amount), "limit_price": _fmt(limit) if limit is not None else None,
        "currency": normalized_currency, "metadata": safe_metadata,
    }
    digest = _canonical_sha(payload)
    await _assert_customer_paper_ready()
    engine = _paper_engine()
    try:
        async with engine.begin() as conn:
            await _set_tenant(conn, customer_id)
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"paper-order-intent:{customer_id}:{client_id}"},
            )
            row = (await conn.execute(text(
                """
                INSERT INTO customer_paper.orders
                    (id, customer_id, client_order_id, venue, market_type, symbol, side, order_type,
                     quantity, limit_price, currency, payload_sha256, metadata_json)
                VALUES
                    (:id, :customer_id, :client_order_id, :venue, :market_type, :symbol, :side, :order_type,
                     :quantity, :limit_price, :currency, :payload_sha256, CAST(:metadata AS jsonb))
                ON CONFLICT (customer_id, client_order_id) DO NOTHING
                RETURNING id
                """
            ), {
                "id": str(uuid.uuid4()), "customer_id": customer_id, "client_order_id": client_id,
                "venue": normalized_venue, "market_type": normalized_market, "symbol": normalized_symbol,
                "side": normalized_side, "order_type": normalized_order_type, "quantity": amount,
                "limit_price": limit, "currency": normalized_currency, "payload_sha256": digest,
                "metadata": json.dumps(safe_metadata, sort_keys=True, separators=(",", ":"), allow_nan=False),
            })).first()
            if row is None:
                existing = (await conn.execute(text(
                    """
                    SELECT id, payload_sha256 FROM customer_paper.orders
                    WHERE customer_id=:customer_id AND client_order_id=:client_order_id
                    """
                ), {"customer_id": customer_id, "client_order_id": client_id})).first()
                if existing is None or str(existing[1]) != digest:
                    raise CustomerPaperIdempotencyConflict(
                        "client_order_id already exists for a different immutable order intent"
                    )
                return {"status": "REPLAYED", "order_id": str(existing[0]), "client_order_id": client_id}
            order_id = str(row[0])
            await _insert_order_event(
                conn, customer_id=customer_id, order_id=order_id,
                event_key=f"order-submitted:{client_id}", event_type="SUBMITTED",
                filled_quantity=Decimal("0"), metadata={"payload_sha256": digest},
            )
        return {"status": "APPENDED", "order_id": order_id, "client_order_id": client_id, "payload_sha256": digest}
    finally:
        await engine.dispose()


async def record_customer_paper_fill(
    *,
    customer_id: int,
    client_order_id: str,
    idempotency_key: str,
    bar_key: str,
    quantity: str | int | Decimal,
    price: str | int | Decimal,
    fee_amount: str | int | Decimal = "0",
) -> dict[str, Any]:
    """Atomically append one simulated fill, fee, position event and balanced journal.

    Only BUY entries and SELLs that close an existing long spot-style position are
    supported. Shorting, overfills, off-limit fills, negative cash and duplicate
    order/bar attempts fail closed. The unique bar key and idempotency digest make
    retries safe across worker restarts.
    """
    customer_id = _customer_id(customer_id)
    client_id = _key(client_order_id, "client_order_id", 180)
    fill_key = _key(idempotency_key, "idempotency_key", 150)
    normalized_bar_key = _key(bar_key, "bar_key", 180)
    amount = _input_decimal(quantity, "quantity", positive=True)
    fill_price = _input_decimal(price, "price", positive=True)
    fee = _input_decimal(fee_amount, "fee_amount", non_negative=True)
    await _assert_customer_paper_ready()
    engine = _paper_engine()
    try:
        async with engine.begin() as conn:
            await _set_tenant(conn, customer_id)
            order = (await conn.execute(text(
                """
                SELECT id, venue, market_type, symbol, side, order_type, quantity, limit_price, currency
                FROM customer_paper.orders
                WHERE customer_id=:customer_id AND client_order_id=:client_order_id
                """
            ), {"customer_id": customer_id, "client_order_id": client_id})).mappings().first()
            if order is None:
                raise CustomerPaperOrderConflict("customer paper order does not exist for this tenant")
            order_id = str(order["id"])
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"paper-order-fill:{customer_id}:{order_id}"},
            )
            currency = str(order["currency"])
            payload = {
                "customer_id": customer_id, "order_id": order_id, "idempotency_key": fill_key,
                "bar_key": normalized_bar_key, "quantity": _fmt(amount), "price": _fmt(fill_price),
                "fee_amount": _fmt(fee), "currency": currency,
            }
            digest = _canonical_sha(payload)
            existing = (await conn.execute(text(
                """
                SELECT id, payload_sha256 FROM customer_paper.fills
                WHERE customer_id=:customer_id AND idempotency_key=:idempotency_key
                """
            ), {"customer_id": customer_id, "idempotency_key": fill_key})).first()
            if existing is not None:
                if str(existing[1]) != digest:
                    raise CustomerPaperIdempotencyConflict(
                        "fill idempotency key already exists for a different fill payload"
                    )
                return {"status": "REPLAYED", "fill_id": str(existing[0]), "idempotency_key": fill_key}

            duplicate_bar = (await conn.execute(text(
                """
                SELECT id FROM customer_paper.fills
                WHERE customer_id=:customer_id AND order_id=:order_id AND bar_key=:bar_key
                """
            ), {"customer_id": customer_id, "order_id": order_id, "bar_key": normalized_bar_key})).first()
            if duplicate_bar is not None:
                raise CustomerPaperIdempotencyConflict(
                    "an order may receive at most one fill attempt per completed bar"
                )
            status = await _current_order_state(conn, customer_id, order_id)
            if status not in {"SUBMITTED", "PARTIALLY_FILLED"}:
                raise CustomerPaperOrderConflict(f"order is not fillable from state {status}")
            total_filled = Decimal((await conn.execute(text(
                "SELECT COALESCE(SUM(quantity), 0) FROM customer_paper.fills WHERE customer_id=:customer_id AND order_id=:order_id"
            ), {"customer_id": customer_id, "order_id": order_id})).scalar_one())
            order_quantity = Decimal(order["quantity"])
            if total_filled + amount > order_quantity:
                raise CustomerPaperOrderConflict("simulated fill would exceed the immutable order quantity")
            limit = Decimal(order["limit_price"]) if order["limit_price"] is not None else None
            side = str(order["side"])
            if limit is not None and ((side == "BUY" and fill_price > limit) or (side == "SELL" and fill_price < limit)):
                raise CustomerPaperOrderConflict("fill price violates the immutable limit price")

            venue = str(order["venue"])
            market_type = str(order["market_type"])
            symbol = str(order["symbol"])
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"paper-position:{customer_id}:{venue}:{market_type}:{symbol}"},
            )
            position = (await conn.execute(text(
                """
                SELECT quantity_after, average_entry_after, cost_basis_after, realized_pnl_after
                FROM customer_paper.position_events
                WHERE customer_id=:customer_id AND venue=:venue AND market_type=:market_type AND symbol=:symbol
                ORDER BY id DESC LIMIT 1
                """
            ), {"customer_id": customer_id, "venue": venue, "market_type": market_type, "symbol": symbol})).first()
            quantity_before = Decimal(position[0]) if position is not None else Decimal("0")
            average_before = Decimal(position[1]) if position is not None else Decimal("0")
            cost_before = Decimal(position[2]) if position is not None else Decimal("0")
            realized_before = Decimal(position[3]) if position is not None else Decimal("0")
            notional = _money(amount * fill_price, "fill notional")
            if side == "BUY":
                quantity_after = _money(quantity_before + amount, "position quantity after BUY")
                cost_after = _money(cost_before + notional, "position cost basis after BUY")
                position_delta = notional
                realized = Decimal("0")
                average_after = _money(cost_after / quantity_after, "average entry price after BUY")
            else:
                if quantity_before <= 0 or amount > quantity_before:
                    raise CustomerPaperOrderConflict("SELL fills cannot create a short position or exceed the open long quantity")
                cost_closed = cost_before if amount == quantity_before else _money(cost_before * amount / quantity_before, "closed position cost basis")
                quantity_after = _money(quantity_before - amount, "position quantity after SELL")
                cost_after = _money(cost_before - cost_closed, "position cost basis after SELL")
                if quantity_after == 0:
                    cost_after = Decimal("0")
                    average_after = Decimal("0")
                else:
                    average_after = _money(cost_after / quantity_after, "average entry price after SELL")
                position_delta = -cost_closed
                realized = _money(notional - cost_closed, "realized P&L")
            pnl_delta = -realized
            cash_delta = _money((-notional if side == "BUY" else notional) - fee, "cash delta")
            realized_after = _money(realized_before + realized, "cumulative realized P&L")
            # Serialize cash checks across different symbols for this tenant/currency.
            # The position lock alone is insufficient: concurrent orders in two symbols
            # otherwise can both spend the same available paper cash.
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": "paper-cash:" + str(customer_id) + ":" + currency},
            )
            cash_before = Decimal((await conn.execute(text(
                """
                SELECT COALESCE(SUM(l.signed_amount), 0)
                FROM customer_paper.accounts a
                LEFT JOIN customer_paper.journal_lines l
                  ON l.account_id=a.id AND l.customer_id=a.customer_id AND l.currency=a.currency
                WHERE a.customer_id=:customer_id AND a.currency=:currency AND a.account_code='PAPER_CASH'
                """
            ), {"customer_id": customer_id, "currency": currency})).scalar_one())
            if cash_before + cash_delta < 0:
                raise CustomerPaperOrderConflict("paper fill would overdraw the customer's paper cash")

            fill_id = str(uuid.uuid4())
            filled_row = (await conn.execute(text(
                """
                INSERT INTO customer_paper.fills
                    (id, customer_id, order_id, idempotency_key, bar_key, venue, market_type, symbol, side,
                     quantity, price, fee_amount, currency, realized_pnl, cash_delta, position_delta, pnl_delta,
                     quantity_before, average_entry_before, cost_basis_before,
                     quantity_after, average_entry_after, cost_basis_after,
                     realized_pnl_before, realized_pnl_after, payload_sha256, metadata_json)
                VALUES
                    (:id, :customer_id, :order_id, :idempotency_key, :bar_key, :venue, :market_type, :symbol, :side,
                     :quantity, :price, :fee_amount, :currency, :realized_pnl, :cash_delta, :position_delta, :pnl_delta,
                     :quantity_before, :average_entry_before, :cost_basis_before,
                     :quantity_after, :average_entry_after, :cost_basis_after,
                     :realized_pnl_before, :realized_pnl_after, :payload_sha256, CAST(:metadata AS jsonb))
                ON CONFLICT (customer_id, idempotency_key) DO NOTHING
                RETURNING id
                """
            ), {
                "id": fill_id, "customer_id": customer_id, "order_id": order_id, "idempotency_key": fill_key,
                "bar_key": normalized_bar_key, "venue": venue, "market_type": market_type, "symbol": symbol,
                "side": side, "quantity": amount, "price": fill_price, "fee_amount": fee, "currency": currency,
                "realized_pnl": realized, "cash_delta": cash_delta, "position_delta": position_delta, "pnl_delta": pnl_delta,
                "quantity_before": quantity_before, "average_entry_before": average_before, "cost_basis_before": cost_before,
                "quantity_after": quantity_after, "average_entry_after": average_after, "cost_basis_after": cost_after,
                "realized_pnl_before": realized_before, "realized_pnl_after": realized_after,
                "payload_sha256": digest,
                "metadata": json.dumps({"bar_key": normalized_bar_key, "input_sha256": digest}, sort_keys=True, separators=(",", ":")),
            })).first()
            if filled_row is None:
                raise CustomerPaperIdempotencyConflict("fill idempotency key raced with a concurrent request")

            journal_lines: list[dict[str, Any]] = [
                {"account_code": "PAPER_CASH", "signed_amount": cash_delta},
                {"account_code": "PAPER_POSITION", "signed_amount": position_delta},
            ]
            if pnl_delta != 0:
                journal_lines.append({"account_code": "PAPER_PNL", "signed_amount": pnl_delta})
            if fee != 0:
                journal_lines.append({"account_code": "PAPER_FEES", "signed_amount": fee})
            journal_id = await _append_journal_on_connection(
                conn, customer_id=customer_id, idempotency_key=f"fill:{fill_key}",
                event_type="SIMULATED_FILL", currency=currency, lines=journal_lines,
                reference=fill_id,
                metadata={"fill_id": fill_id, "order_id": order_id, "symbol": symbol, "side": side, "payload_sha256": digest},
            )
            await conn.execute(text(
                """
                INSERT INTO customer_paper.position_events
                    (customer_id, venue, market_type, symbol, fill_id, event_type, idempotency_key,
                     signed_quantity, fill_price, quantity_before, average_entry_before, cost_basis_before,
                     quantity_after, average_entry_after, cost_basis_after, realized_pnl_delta, realized_pnl_after)
                VALUES
                    (:customer_id, :venue, :market_type, :symbol, :fill_id, 'FILL', :idempotency_key,
                     :signed_quantity, :price, :quantity_before, :average_entry_before, :cost_basis_before,
                     :quantity_after, :average_entry_after, :cost_basis_after, :realized_pnl_delta, :realized_pnl_after)
                """
            ), {
                "customer_id": customer_id, "venue": venue, "market_type": market_type, "symbol": symbol,
                "fill_id": fill_id, "idempotency_key": f"fill:{fill_key}",
                "signed_quantity": amount if side == "BUY" else -amount, "price": fill_price,
                "quantity_before": quantity_before, "average_entry_before": average_before, "cost_basis_before": cost_before,
                "quantity_after": quantity_after, "average_entry_after": average_after, "cost_basis_after": cost_after,
                "realized_pnl_delta": realized, "realized_pnl_after": realized_after,
            })
            filled_after = total_filled + amount
            next_state = "FILLED" if filled_after == order_quantity else "PARTIALLY_FILLED"
            await _insert_order_event(
                conn, customer_id=customer_id, order_id=order_id,
                event_key=f"fill-state:{fill_key}", event_type=next_state,
                filled_quantity=filled_after,
                metadata={"fill_id": fill_id, "bar_key": normalized_bar_key, "journal_id": journal_id},
            )
        return {
            "status": "APPENDED", "fill_id": fill_id, "journal_id": journal_id,
            "order_id": order_id, "idempotency_key": fill_key, "order_status": next_state,
            "filled_quantity": _fmt(filled_after), "position_quantity": _fmt(quantity_after),
            "average_entry_price": _fmt(average_after), "realized_pnl": _fmt(realized),
        }
    finally:
        await engine.dispose()


async def _close_customer_paper_order(
    *,
    customer_id: int,
    client_order_id: str,
    idempotency_key: str,
    terminal_state: str,
) -> dict[str, Any]:
    customer_id = _customer_id(customer_id)
    client_id = _key(client_order_id, "client_order_id", 180)
    key = _key(idempotency_key, "idempotency_key", 150)
    if terminal_state not in {"CANCELLED", "EXPIRED"}:
        raise ValueError("unsupported terminal paper-order state")
    await _assert_customer_paper_ready()
    engine = _paper_engine()
    try:
        async with engine.begin() as conn:
            await _set_tenant(conn, customer_id)
            order = (await conn.execute(text(
                "SELECT id, quantity FROM customer_paper.orders WHERE customer_id=:customer_id AND client_order_id=:client_order_id"
            ), {"customer_id": customer_id, "client_order_id": client_id})).first()
            if order is None:
                raise CustomerPaperOrderConflict("customer paper order does not exist for this tenant")
            order_id, ordered_qty = str(order[0]), Decimal(order[1])
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"paper-order-fill:{customer_id}:{order_id}"},
            )
            event_key = f"order-close:{key}"
            same_key = (await conn.execute(text(
                "SELECT order_id, event_type FROM customer_paper.order_events WHERE customer_id=:customer_id AND event_key=:event_key"
            ), {"customer_id": customer_id, "event_key": event_key})).first()
            if same_key is not None:
                if str(same_key[0]) != order_id or str(same_key[1]) != terminal_state:
                    raise CustomerPaperIdempotencyConflict("order-close idempotency key conflicts with existing event")
                return {"status": "REPLAYED", "order_id": order_id, "state": terminal_state}
            state = await _current_order_state(conn, customer_id, order_id)
            if state not in {"SUBMITTED", "PARTIALLY_FILLED"}:
                raise CustomerPaperOrderConflict(f"order cannot transition from state {state}")
            filled = Decimal((await conn.execute(text(
                "SELECT COALESCE(SUM(quantity), 0) FROM customer_paper.fills WHERE customer_id=:customer_id AND order_id=:order_id"
            ), {"customer_id": customer_id, "order_id": order_id})).scalar_one())
            await _insert_order_event(
                conn, customer_id=customer_id, order_id=order_id, event_key=event_key,
                event_type=terminal_state, filled_quantity=filled,
                metadata={"remaining_quantity": _fmt(ordered_qty - filled)},
            )
        return {"status": "APPENDED", "order_id": order_id, "state": terminal_state}
    finally:
        await engine.dispose()


async def cancel_customer_paper_order(
    *, customer_id: int, client_order_id: str, idempotency_key: str
) -> dict[str, Any]:
    """Cancel an unfilled or partially filled order without mutating prior events."""
    return await _close_customer_paper_order(
        customer_id=customer_id, client_order_id=client_order_id,
        idempotency_key=idempotency_key, terminal_state="CANCELLED",
    )


async def expire_customer_paper_order(
    *, customer_id: int, client_order_id: str, idempotency_key: str
) -> dict[str, Any]:
    """Persist TTL expiry as a terminal order event."""
    return await _close_customer_paper_order(
        customer_id=customer_id, client_order_id=client_order_id,
        idempotency_key=idempotency_key, terminal_state="EXPIRED",
    )


async def get_customer_paper_order(*, customer_id: int, client_order_id: str) -> dict[str, Any] | None:
    """Read durable order status and fill progress for crash/restart recovery."""
    customer_id = _customer_id(customer_id)
    client_id = _key(client_order_id, "client_order_id", 180)
    await _assert_customer_paper_ready()
    engine = _paper_engine()
    try:
        async with engine.begin() as conn:
            await _set_tenant(conn, customer_id)
            row = (await conn.execute(text(
                """
                SELECT o.id, o.client_order_id, o.venue, o.market_type, o.symbol, o.side, o.order_type,
                       o.quantity, o.limit_price, o.currency, e.event_type AS status,
                       COALESCE(f.filled_quantity,0) AS filled_quantity
                FROM customer_paper.orders o
                LEFT JOIN LATERAL (
                    SELECT event_type FROM customer_paper.order_events
                    WHERE customer_id=o.customer_id AND order_id=o.id ORDER BY id DESC LIMIT 1
                ) e ON true
                LEFT JOIN LATERAL (
                    SELECT SUM(quantity) AS filled_quantity FROM customer_paper.fills
                    WHERE customer_id=o.customer_id AND order_id=o.id
                ) f ON true
                WHERE o.customer_id=:customer_id AND o.client_order_id=:client_order_id
                """
            ), {"customer_id": customer_id, "client_order_id": client_id})).mappings().first()
        if row is None:
            return None
        limit = row["limit_price"]
        filled = Decimal(row["filled_quantity"] or 0)
        quantity = Decimal(row["quantity"])
        return {
            "order_id": str(row["id"]), "client_order_id": str(row["client_order_id"]),
            "venue": str(row["venue"]), "market_type": str(row["market_type"]),
            "symbol": str(row["symbol"]), "side": str(row["side"]), "order_type": str(row["order_type"]),
            "quantity": _fmt(quantity), "filled_quantity": _fmt(filled),
            "remaining_quantity": _fmt(quantity - filled), "limit_price": _fmt(Decimal(limit)) if limit is not None else None,
            "currency": str(row["currency"]), "status": str(row["status"] or "UNKNOWN"),
        }
    finally:
        await engine.dispose()


async def list_open_customer_paper_orders(*, customer_id: int) -> list[dict[str, Any]]:
    """Rebuild open/partial orders from persisted intents, events, and fills after restart."""
    customer_id = _customer_id(customer_id)
    await _assert_customer_paper_ready()
    engine = _paper_engine()
    try:
        async with engine.begin() as conn:
            await _set_tenant(conn, customer_id)
            rows = (await conn.execute(text(
                """
                SELECT o.client_order_id, o.symbol, o.side, o.quantity,
                       COALESCE(f.filled_quantity,0) AS filled_quantity, e.event_type AS status
                FROM customer_paper.orders o
                LEFT JOIN LATERAL (
                    SELECT event_type FROM customer_paper.order_events
                    WHERE customer_id=o.customer_id AND order_id=o.id ORDER BY id DESC LIMIT 1
                ) e ON true
                LEFT JOIN LATERAL (
                    SELECT SUM(quantity) AS filled_quantity FROM customer_paper.fills
                    WHERE customer_id=o.customer_id AND order_id=o.id
                ) f ON true
                WHERE o.customer_id=:customer_id
                  AND e.event_type IN ('SUBMITTED','PARTIALLY_FILLED')
                  AND COALESCE(f.filled_quantity,0) < o.quantity
                ORDER BY o.created_at, o.client_order_id
                """
            ), {"customer_id": customer_id})).mappings().all()
        return [{
            "client_order_id": str(row["client_order_id"]), "symbol": str(row["symbol"]),
            "side": str(row["side"]), "quantity": _fmt(Decimal(row["quantity"])),
            "filled_quantity": _fmt(Decimal(row["filled_quantity"] or 0)), "status": str(row["status"]),
        } for row in rows]
    finally:
        await engine.dispose()


async def get_customer_paper_positions(*, customer_id: int) -> list[dict[str, str]]:
    """Return latest immutable position snapshots; no in-memory state is required on restart."""
    customer_id = _customer_id(customer_id)
    await _assert_customer_paper_ready()
    engine = _paper_engine()
    try:
        async with engine.begin() as conn:
            await _set_tenant(conn, customer_id)
            rows = (await conn.execute(text(
                """
                SELECT DISTINCT ON (venue, market_type, symbol)
                    venue, market_type, symbol, quantity_after, average_entry_after,
                    cost_basis_after, realized_pnl_after
                FROM customer_paper.position_events
                WHERE customer_id=:customer_id
                ORDER BY venue, market_type, symbol, id DESC
                """
            ), {"customer_id": customer_id})).mappings().all()
        return [{
            "venue": str(row["venue"]), "market_type": str(row["market_type"]), "symbol": str(row["symbol"]),
            "quantity": _fmt(Decimal(row["quantity_after"])),
            "average_entry_price": _fmt(Decimal(row["average_entry_after"])),
            "cost_basis": _fmt(Decimal(row["cost_basis_after"])),
            "realized_pnl": _fmt(Decimal(row["realized_pnl_after"])),
        } for row in rows if Decimal(row["quantity_after"]) > 0]
    finally:
        await engine.dispose()


async def reverse_customer_paper_fill(
    *, customer_id: int, fill_id: str, idempotency_key: str
) -> dict[str, Any]:
    """Append a compensating reversal only when the fill is the position's latest event.

    Reversing an older fill after later fills would require a full restatement of those
    later trades and realized P&L. It is rejected instead of silently corrupting history.
    """
    customer_id = _customer_id(customer_id)
    key = _key(idempotency_key, "idempotency_key", 120)
    try:
        canonical_fill_id = str(uuid.UUID(str(fill_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("fill_id must be a valid UUID") from exc
    await _assert_customer_paper_ready()
    engine = _paper_engine()
    try:
        async with engine.begin() as conn:
            await _set_tenant(conn, customer_id)
            reversal_key = f"reverse:{key}"
            previous = (await conn.execute(text(
                """
                SELECT reversal_of_fill_id, event_type FROM customer_paper.position_events
                WHERE customer_id=:customer_id AND idempotency_key=:idempotency_key
                """
            ), {"customer_id": customer_id, "idempotency_key": reversal_key})).first()
            if previous is not None:
                if str(previous[0]) != canonical_fill_id or str(previous[1]) != "REVERSAL":
                    raise CustomerPaperIdempotencyConflict("reversal idempotency key conflicts with another event")
                return {"status": "REPLAYED", "fill_id": canonical_fill_id, "idempotency_key": key}
            fill = (await conn.execute(text(
                """
                SELECT id, order_id, idempotency_key, venue, market_type, symbol, side, quantity, price,
                       fee_amount, currency, realized_pnl, cash_delta, position_delta, pnl_delta,
                       quantity_before, average_entry_before, cost_basis_before,
                       quantity_after, average_entry_after, cost_basis_after,
                       realized_pnl_before, realized_pnl_after
                FROM customer_paper.fills
                WHERE customer_id=:customer_id AND id=:fill_id
                """
            ), {"customer_id": customer_id, "fill_id": canonical_fill_id})).mappings().first()
            if fill is None:
                raise CustomerPaperOrderConflict("fill is not present in this customer's paper ledger")
            venue, market_type, symbol = str(fill["venue"]), str(fill["market_type"]), str(fill["symbol"])
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"paper-position:{customer_id}:{venue}:{market_type}:{symbol}"},
            )
            already_reversed = (await conn.execute(text(
                "SELECT id FROM customer_paper.position_events WHERE customer_id=:customer_id AND reversal_of_fill_id=:fill_id"
            ), {"customer_id": customer_id, "fill_id": canonical_fill_id})).first()
            if already_reversed is not None:
                raise CustomerPaperOrderConflict("a fill can be reversed only once")
            latest = (await conn.execute(text(
                """
                SELECT fill_id, event_type, quantity_after, average_entry_after, cost_basis_after, realized_pnl_after
                FROM customer_paper.position_events
                WHERE customer_id=:customer_id AND venue=:venue AND market_type=:market_type AND symbol=:symbol
                ORDER BY id DESC LIMIT 1
                """
            ), {"customer_id": customer_id, "venue": venue, "market_type": market_type, "symbol": symbol})).first()
            if latest is None or str(latest[0]) != canonical_fill_id or str(latest[1]) != "FILL":
                raise CustomerPaperOrderConflict("only the latest non-reversed fill for this position can be reversed safely")

            reversal_cash_delta = -Decimal(fill["cash_delta"])
            fill_currency = str(fill["currency"])
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": "paper-cash:" + str(customer_id) + ":" + fill_currency},
            )
            cash_before = Decimal((await conn.execute(text(
                """
                SELECT COALESCE(SUM(l.signed_amount), 0)
                FROM customer_paper.accounts a
                LEFT JOIN customer_paper.journal_lines l
                  ON l.account_id=a.id AND l.customer_id=a.customer_id AND l.currency=a.currency
                WHERE a.customer_id=:customer_id AND a.currency=:currency AND a.account_code='PAPER_CASH'
                """
            ), {"customer_id": customer_id, "currency": fill_currency})).scalar_one())
            if cash_before + reversal_cash_delta < 0:
                raise CustomerPaperOrderConflict(
                    "reversing this fill would overdraw paper cash after subsequent activity"
                )
            reversal_lines: list[dict[str, Any]] = [
                {"account_code": "PAPER_CASH", "signed_amount": reversal_cash_delta},
                {"account_code": "PAPER_POSITION", "signed_amount": -Decimal(fill["position_delta"])},
            ]
            if Decimal(fill["pnl_delta"]) != 0:
                reversal_lines.append({"account_code": "PAPER_PNL", "signed_amount": -Decimal(fill["pnl_delta"])})
            if Decimal(fill["fee_amount"]) != 0:
                reversal_lines.append({"account_code": "PAPER_FEES", "signed_amount": -Decimal(fill["fee_amount"])})
            journal_id = await _append_journal_on_connection(
                conn, customer_id=customer_id, idempotency_key=f"reversal:{key}",
                event_type="REVERSAL", currency=str(fill["currency"]), lines=reversal_lines,
                reference=canonical_fill_id,
                metadata={"reversal_of_fill_id": canonical_fill_id, "reversal_idempotency_key": key},
            )
            signed_original = Decimal(fill["quantity"]) if str(fill["side"]) == "BUY" else -Decimal(fill["quantity"])
            await conn.execute(text(
                """
                INSERT INTO customer_paper.position_events
                    (customer_id, venue, market_type, symbol, fill_id, reversal_of_fill_id, event_type,
                     idempotency_key, signed_quantity, fill_price, quantity_before, average_entry_before,
                     cost_basis_before, quantity_after, average_entry_after, cost_basis_after,
                     realized_pnl_delta, realized_pnl_after)
                VALUES
                    (:customer_id, :venue, :market_type, :symbol, :fill_id, :reversal_of_fill_id, 'REVERSAL',
                     :idempotency_key, :signed_quantity, :fill_price, :quantity_before, :average_entry_before,
                     :cost_basis_before, :quantity_after, :average_entry_after, :cost_basis_after,
                     :realized_pnl_delta, :realized_pnl_after)
                """
            ), {
                "customer_id": customer_id, "venue": venue, "market_type": market_type, "symbol": symbol,
                "fill_id": canonical_fill_id, "reversal_of_fill_id": canonical_fill_id,
                "idempotency_key": reversal_key, "signed_quantity": -signed_original, "fill_price": Decimal(fill["price"]),
                "quantity_before": Decimal(latest[2]), "average_entry_before": Decimal(latest[3]), "cost_basis_before": Decimal(latest[4]),
                "quantity_after": Decimal(fill["quantity_before"]), "average_entry_after": Decimal(fill["average_entry_before"]),
                "cost_basis_after": Decimal(fill["cost_basis_before"]),
                "realized_pnl_delta": -Decimal(fill["realized_pnl"]),
                "realized_pnl_after": Decimal(fill["realized_pnl_before"]),
            })
        return {"status": "APPENDED", "fill_id": canonical_fill_id, "journal_id": journal_id, "idempotency_key": key}
    finally:
        await engine.dispose()
