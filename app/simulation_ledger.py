"""Isolated virtual ledger primitives for customer paper trading.

This module deliberately does not import or call customer_funds. Simulation money is
not a deposit, is not withdrawable, and cannot reserve or settle custodial funds.
The customer paper-execution guard remains enabled until execution integration and
cross-ledger isolation tests are complete.
"""
from __future__ import annotations

from decimal import Decimal
import json

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal, utcnow


_ALLOWED_ASSETS = {"crypto", "forex", "commodity"}
_ALLOWED_EVENTS = {
    "VIRTUAL_SEED", "ORDER_FILL", "REALIZED_PNL", "FEE", "ORDER_CANCEL",
    "STOP_LOSS", "TAKE_PROFIT", "ADJUSTMENT",
}


class SimulationLedgerError(RuntimeError):
    """A simulation-ledger request failed a safety or consistency check."""


def _amount(value: Decimal | str | int | float) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise SimulationLedgerError("Simulation amounts must be finite")
    return amount


async def create_simulation_account(
    customer_id: int, asset_class: str, *, currency: str = "USD",
    initial_virtual_balance: Decimal | str | int | float = "10000",
    daily_loss_limit: Decimal | str | int | float = "500",
) -> int:
    """Create a simulation-only account and seed virtual funds once, idempotently."""
    asset = str(asset_class).strip().lower()
    balance = _amount(initial_virtual_balance)
    loss_limit = _amount(daily_loss_limit)
    if customer_id <= 0 or asset not in _ALLOWED_ASSETS or not currency.strip():
        raise SimulationLedgerError("Invalid simulation account identity")
    if balance < 0 or loss_limit < 0:
        raise SimulationLedgerError("Virtual balance and loss limit must be nonnegative")

    async with SessionLocal() as db:
        try:
            async with db.begin():
                result = await db.execute(text("""
                    INSERT INTO customer_simulation_accounts
                        (customer_id, asset_class, currency, virtual_balance,
                         initial_virtual_balance, daily_loss_limit, status, created_at, updated_at)
                    VALUES (:customer_id, :asset, :currency, 0, :initial, :loss_limit, 'ACTIVE', :now, :now)
                    ON CONFLICT (customer_id, asset_class, currency) DO NOTHING
                    RETURNING id
                """), {"customer_id": customer_id, "asset": asset, "currency": currency.upper(),
                       "initial": balance, "loss_limit": loss_limit, "now": utcnow()})
                row = result.first()
                if row:
                    account_id = int(row[0])
                    await db.execute(text("""
                        INSERT INTO customer_simulation_events
                            (account_id, customer_id, idempotency_key, event_type, asset_class,
                             balance_delta, balance_after, metadata_json, created_at)
                        VALUES (:account_id, :customer_id, 'initial-virtual-seed', 'VIRTUAL_SEED',
                                :asset, :balance, :balance, :metadata, :now)
                    """), {"account_id": account_id, "customer_id": customer_id, "asset": asset,
                           "balance": balance, "metadata": json.dumps({"virtual_only": True}),
                           "now": utcnow()})
                    await db.execute(text("""
                        UPDATE customer_simulation_accounts SET virtual_balance=:balance, updated_at=:now
                        WHERE id=:id
                    """), {"balance": balance, "now": utcnow(), "id": account_id})
                else:
                    existing = (await db.execute(text("""
                        SELECT id, initial_virtual_balance, daily_loss_limit
                        FROM customer_simulation_accounts
                        WHERE customer_id=:customer_id AND asset_class=:asset AND currency=:currency
                        FOR UPDATE
                    """), {"customer_id": customer_id, "asset": asset, "currency": currency.upper()})).first()
                    if not existing:
                        raise SimulationLedgerError("Could not resolve existing simulation account")
                    account_id = int(existing.id)
                    if Decimal(existing.initial_virtual_balance) != balance or Decimal(existing.daily_loss_limit) != loss_limit:
                        raise SimulationLedgerError("Simulation account already exists with different limits")
                return account_id
        except IntegrityError as exc:
            raise SimulationLedgerError("Simulation account creation failed safely") from exc


async def post_simulation_event(
    *, customer_id: int, account_id: int, idempotency_key: str, event_type: str,
    balance_delta: Decimal | str | int | float, asset_class: str,
    symbol: str = "", quantity: Decimal | str | int | float = 0,
    price: Decimal | str | int | float = 0, metadata: dict | None = None,
) -> Decimal:
    """Atomically apply a virtual balance event; repeated keys return the original result."""
    asset = str(asset_class).strip().lower()
    kind = str(event_type).strip().upper()
    delta, qty, px = _amount(balance_delta), _amount(quantity), _amount(price)
    if customer_id <= 0 or account_id <= 0 or not idempotency_key.strip():
        raise SimulationLedgerError("Customer, account and idempotency key are required")
    if asset not in _ALLOWED_ASSETS or kind not in _ALLOWED_EVENTS or qty < 0 or px < 0:
        raise SimulationLedgerError("Unsupported simulation event or invalid quantity/price")
    if kind == "VIRTUAL_SEED":
        raise SimulationLedgerError("Virtual seed events are only created with a new account")
    async with SessionLocal() as db:
        async with db.begin():
            account = (await db.execute(text("""
                SELECT virtual_balance, status, asset_class FROM customer_simulation_accounts
                WHERE id=:account_id AND customer_id=:customer_id FOR UPDATE
            """), {"account_id": account_id, "customer_id": customer_id})).first()
            if not account:
                raise SimulationLedgerError("Simulation account not found")
            if account.status != "ACTIVE" or account.asset_class != asset:
                raise SimulationLedgerError("Simulation account is inactive or asset class mismatched")
            existing = (await db.execute(text("""
                SELECT balance_after FROM customer_simulation_events
                WHERE account_id=:account_id AND idempotency_key=:key
            """), {"account_id": account_id, "key": idempotency_key})).first()
            if existing:
                return Decimal(existing.balance_after)
            new_balance = Decimal(account.virtual_balance) + delta
            if new_balance < 0:
                raise SimulationLedgerError("Simulation event would make virtual balance negative")
            now = utcnow()
            await db.execute(text("""
                INSERT INTO customer_simulation_events
                    (account_id, customer_id, idempotency_key, event_type, asset_class, symbol,
                     quantity, price, balance_delta, balance_after, metadata_json, created_at)
                VALUES (:account_id, :customer_id, :key, :kind, :asset, :symbol,
                        :quantity, :price, :delta, :balance_after, :metadata, :now)
            """), {"account_id": account_id, "customer_id": customer_id, "key": idempotency_key,
                   "kind": kind, "asset": asset, "symbol": symbol[:80], "quantity": qty,
                   "price": px, "delta": delta, "balance_after": new_balance,
                   "metadata": json.dumps(metadata or {}, default=str), "now": now})
            await db.execute(text("""
                UPDATE customer_simulation_accounts SET virtual_balance=:balance, updated_at=:now
                WHERE id=:account_id AND customer_id=:customer_id
            """), {"balance": new_balance, "now": now, "account_id": account_id, "customer_id": customer_id})
            return new_balance
