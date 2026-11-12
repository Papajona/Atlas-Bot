#!/usr/bin/env python3
"""Exercise customer-paper ledger invariants using a synthetic, non-customer tenant.

The script is intentionally restricted to the separately configured customer-paper
database. It never opens a real-money write path and reports no credentials or PII.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from app.config import settings
from app.customer_paper_flow import ensure_customer_paper_account
from app.customer_paper_ledger import (
    CustomerPaperIdempotencyConflict,
    _paper_engine,
    get_customer_paper_balances,
    preflight_isolated_customer_paper_ledger,
)
from app.customer_paper_lifecycle import (
    get_customer_paper_order,
    get_customer_paper_positions,
    list_open_customer_paper_orders,
    record_customer_paper_fill,
    register_customer_paper_market_bar,
    cancel_customer_paper_order,
    reverse_customer_paper_fill,
    submit_customer_paper_order,
)


_CURRENT_OPERATION = "initializing"


def _set_operation(operation: str) -> None:
    global _CURRENT_OPERATION
    _CURRENT_OPERATION = str(operation)[:120]


def _sanitized_failure(exc: Exception) -> tuple[str, list[str]]:
    message = str(exc)
    for candidate in (
        getattr(settings, "database_url", None),
        getattr(settings, "customer_paper_database_url", None),
    ):
        if candidate:
            message = message.replace(str(candidate), "[REDACTED_DATABASE_URL]")
    message = re.sub(
        r"(?i)((?:postgres(?:ql)?(?:\+\w+)?|mysql)://)[^\s'\"@]+@",
        r"\1[REDACTED]@",
        message,
    )
    message = re.sub(
        r"(?i)\b(password|token|secret|api[_-]?key|authorization)\b\s*[:=]\s*[^,\s;]+",
        r"\1=[REDACTED]",
        message,
    )
    message = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", message)
    message = re.sub(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "[REDACTED_JWT]", message)
    message = message.strip()[:600] or "No exception message was supplied."
    frames = [
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)[-20:]
    ]
    return message, frames


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _balance_map(rows: list[dict[str, Any]]) -> dict[str, Decimal]:
    return {
        str(row["account_code"]): Decimal(str(row["balance"]))
        for row in rows
        if str(row.get("currency") or "").upper() == "USDT"
    }


def _market_metadata(submission_at: datetime, digest: str) -> dict[str, str]:
    return {
        "exchange": "binance",
        "timeframe": "1m",
        "submission_bar_at": submission_at.isoformat(),
        "submission_bar_sha256": digest,
        "market_type": "spot",
        "execution_owner": "staging_acceptance_test",
        "staging_acceptance_test": "true",
        "execution_model": "completed_bar_volume_capped_heuristic_v1",
    }


async def _register_bar(
    *,
    label: str,
    venue: str,
    symbol: str,
    bar_at: datetime,
    run_key: str,
    base_volume: str = "1",
    participation_rate: str = "0.01",
) -> dict[str, str]:
    _set_operation(f"{label}:register-market-bar")
    digest = hashlib.sha256(f"{run_key}|{label}|bar".encode("utf-8")).hexdigest()
    result = await register_customer_paper_market_bar(
        venue=venue,
        symbol=symbol,
        timeframe="1m",
        bar_at=bar_at,
        bar_data_sha256=digest,
        base_volume=base_volume,
        participation_rate=participation_rate,
    )
    _require(result.get("status") in {"APPENDED", "REPLAYED"}, f"{label}: market-bar budget registration failed")
    _require(Decimal(result["capacity"]) > 0, f"{label}: market-bar capacity must be positive")
    return result


async def _read_market_bar_allocation(customer_id: int, bar_key: str) -> tuple[Decimal, Decimal]:
    _set_operation("verify-aggregate-market-bar-allocation")
    engine = _paper_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.customer_id', :customer_id, true)"),
                {"customer_id": str(customer_id)},
            )
            row = (await conn.execute(text("""
                SELECT b.capacity,
                       COALESCE(SUM(a.allocated_quantity), 0) AS allocated_quantity
                FROM customer_paper.market_bar_budgets AS b
                LEFT JOIN customer_paper.market_bar_allocations AS a
                  ON a.bar_key = b.bar_key
                WHERE b.bar_key = :bar_key
                GROUP BY b.bar_key, b.capacity
            """), {"bar_key": bar_key})).mappings().first()
        _require(row is not None, "registered market-bar budget could not be read back")
        return Decimal(row["capacity"]), Decimal(row["allocated_quantity"])
    finally:
        await engine.dispose()


async def _verify_tenant_rls(
    *,
    primary_customer_id: int,
    other_customer_id: int,
    primary_order_id: str,
    other_order_id: str,
) -> dict[str, int]:
    _set_operation("verify-cross-tenant-row-level-security")
    engine = _paper_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.customer_id', :customer_id, true)"),
                {"customer_id": str(other_customer_id)},
            )
            # This is deliberately an RLS-sensitive read: the session context belongs
            # to tenant B, while the predicate asks for tenant A's known persisted row.
            # A broken/missing tenant policy makes this count non-zero.
            primary_order_count = int((await conn.execute(text("""
                SELECT count(*) FROM customer_paper.orders
                WHERE customer_id = :primary_customer_id
                  AND client_order_id = :primary_order_id
            """), {
                "primary_customer_id": primary_customer_id,
                "primary_order_id": primary_order_id,
            })).scalar_one())
            other_order_count = int((await conn.execute(text("""
                SELECT count(*) FROM customer_paper.orders
                WHERE customer_id = :other_customer_id
                  AND client_order_id = :other_order_id
            """), {
                "other_customer_id": other_customer_id,
                "other_order_id": other_order_id,
            })).scalar_one())
            primary_account_count = int((await conn.execute(text("""
                SELECT count(*) FROM customer_paper.accounts
                WHERE customer_id = :primary_customer_id
            """), {"primary_customer_id": primary_customer_id})).scalar_one())
            other_account_count = int((await conn.execute(text("""
                SELECT count(*) FROM customer_paper.accounts
                WHERE customer_id = :other_customer_id
            """), {"other_customer_id": other_customer_id})).scalar_one())
        _require(primary_order_count == 0, "tenant B RLS context exposed tenant A's known order")
        _require(primary_account_count == 0, "tenant B RLS context exposed tenant A's account rows")
        _require(other_order_count == 1, "tenant B could not read its own known order through RLS")
        _require(other_account_count > 0, "tenant B could not read its own account rows through RLS")
        return {
            "foreign_order_rows_visible": primary_order_count,
            "foreign_account_rows_visible": primary_account_count,
            "own_order_rows_visible": other_order_count,
            "own_account_rows_visible": other_account_count,
        }
    finally:
        await engine.dispose()


async def _read_journal_invariants(customer_id: int) -> int:
    _set_operation("verify-balanced-customer-paper-journals")
    engine = _paper_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.customer_id', :customer_id, true)"),
                {"customer_id": str(customer_id)},
            )
            rows = (await conn.execute(text("""
                SELECT j.id, j.event_type, COUNT(l.id) AS line_count,
                       COALESCE(SUM(l.signed_amount), 0) AS total
                FROM customer_paper.journals AS j
                LEFT JOIN customer_paper.journal_lines AS l
                  ON l.journal_id=j.id AND l.customer_id=j.customer_id
                WHERE j.customer_id=:customer_id
                GROUP BY j.id, j.event_type
                ORDER BY j.created_at, j.id
            """), {"customer_id": customer_id})).mappings().all()
        _require(bool(rows), "no customer-paper journals were persisted")
        for row in rows:
            _require(int(row["line_count"]) >= 2, "a paper journal has fewer than two ledger lines")
            _require(Decimal(row["total"]) == 0, "a customer-paper journal is not balanced")
        return len(rows)
    finally:
        await engine.dispose()


async def _phase_one(
    *,
    customer_id: int,
    run_key: str,
    order_id: str,
    submission_at: datetime,
    bar_one_at: datetime,
    symbol: str,
) -> dict[str, Any]:
    _set_operation("phase-one:initial-isolation-preflight")
    preflight = await preflight_isolated_customer_paper_ledger(require_enabled=True)
    _require(preflight.get("status") == "PASS", "customer-paper isolation preflight did not pass")

    _set_operation("phase-one:seed-synthetic-customer")
    seeded = await ensure_customer_paper_account(customer_id, "USDT")
    _require(seeded.get("status") == "PASS" and seeded.get("real_funds_used") is False, "virtual account seed failed")
    _set_operation("phase-one:verify-idempotent-customer-seed")
    reseeded = await ensure_customer_paper_account(customer_id, "USDT")
    _require(reseeded.get("paper_cash") == seeded.get("paper_cash"), "virtual account initialization was not idempotent")

    _set_operation("phase-one:submit-primary-order")
    submission_digest = hashlib.sha256(f"{run_key}|submission|primary".encode("utf-8")).hexdigest()
    order = await submit_customer_paper_order(
        customer_id=customer_id,
        client_order_id=order_id,
        venue="binance",
        market_type="spot",
        symbol=symbol,
        side="BUY",
        quantity="0.002",
        currency="USDT",
        order_type="MARKET",
        metadata=_market_metadata(submission_at, submission_digest),
    )
    _require(order.get("status") == "APPENDED", "primary order intent was not appended")

    bar = await _register_bar(
        label="primary-partial-fill",
        venue="binance",
        symbol=symbol,
        bar_at=bar_one_at,
        run_key=run_key,
        base_volume="0.001",
        participation_rate="1",
    )
    _set_operation("phase-one:append-first-partial-fill")
    fill_args = {
        "customer_id": customer_id,
        "client_order_id": order_id,
        "idempotency_key": f"{run_key}:primary:fill-1",
        "bar_key": bar["bar_key"],
        "quantity": "0.001",
        "price": "100",
        "fee_amount": "0.01",
        "enforce_market_bar_budget": True,
    }
    first_fill = await record_customer_paper_fill(**fill_args)
    _require(first_fill.get("status") == "APPENDED", "first partial fill was not appended")
    _set_operation("phase-one:verify-same-payload-fill-replay")
    replay = await record_customer_paper_fill(**fill_args)
    _require(
        replay.get("status") == "REPLAYED" and replay.get("fill_id") == first_fill.get("fill_id"),
        "identical fill retry did not return the immutable prior fill",
    )

    _set_operation("phase-one:verify-conflicting-idempotency-key-rejected")
    try:
        await record_customer_paper_fill(**{**fill_args, "price": "100.01"})
    except CustomerPaperIdempotencyConflict:
        conflicting_retry_blocked = True
    else:
        raise RuntimeError("same fill idempotency key accepted a different price")

    _set_operation("phase-one:verify-same-order-duplicate-bar-rejected")
    try:
        await record_customer_paper_fill(
            **{**fill_args, "idempotency_key": f"{run_key}:primary:duplicate-bar"}
        )
    except CustomerPaperIdempotencyConflict:
        duplicate_bar_blocked = True
    else:
        raise RuntimeError("one order accepted two fills against the same completed bar")

    # Exhaust the registered bar with a second independent order. The first order
    # has consumed the full 0.001 capacity; a different order on that same bar
    # must receive NO_LIQUIDITY rather than passing only the duplicate-order guard.
    capacity_order_id = f"{run_key}-capacity-probe"
    _set_operation("phase-one:submit-second-order-for-shared-bar-capacity")
    capacity_submission_digest = hashlib.sha256(
        f"{run_key}|submission|capacity-probe".encode("utf-8")
    ).hexdigest()
    capacity_order = await submit_customer_paper_order(
        customer_id=customer_id,
        client_order_id=capacity_order_id,
        venue="binance",
        market_type="spot",
        symbol=symbol,
        side="BUY",
        quantity="0.001",
        currency="USDT",
        order_type="MARKET",
        metadata=_market_metadata(submission_at, capacity_submission_digest),
    )
    _require(capacity_order.get("status") == "APPENDED", "second capacity-probe order was not appended")
    _set_operation("phase-one:reject-second-order-after-aggregate-bar-capacity-exhaustion")
    capacity_fill = await record_customer_paper_fill(
        customer_id=customer_id,
        client_order_id=capacity_order_id,
        idempotency_key=f"{run_key}:capacity-probe:fill",
        bar_key=bar["bar_key"],
        quantity="0.001",
        price="100",
        fee_amount="0",
        enforce_market_bar_budget=True,
    )
    _require(
        capacity_fill.get("status") == "NO_LIQUIDITY",
        "a second order consumed a market bar after aggregate capacity was exhausted",
    )
    bar_capacity, bar_allocated = await _read_market_bar_allocation(customer_id, bar["bar_key"])
    _require(
        bar_capacity == Decimal("0.001") and bar_allocated == bar_capacity,
        "persisted market-bar allocations do not exactly respect the shared 0.001 capacity",
    )
    _set_operation("phase-one:cancel-unfilled-capacity-probe-order")
    capacity_cancel = await cancel_customer_paper_order(
        customer_id=customer_id,
        client_order_id=capacity_order_id,
        idempotency_key=f"{run_key}:capacity-probe:cancel",
    )
    _require(capacity_cancel.get("status") == "APPENDED", "unfilled capacity-probe order did not cancel")

    _set_operation("phase-one:read-persisted-partial-order-state")
    persisted = await get_customer_paper_order(customer_id=customer_id, client_order_id=order_id)
    _require(
        persisted is not None
        and persisted.get("status") == "PARTIALLY_FILLED"
        and Decimal(persisted.get("filled_quantity", "0")) == Decimal("0.001"),
        "partial order state was not persisted correctly",
    )
    _set_operation("phase-one:verify-partial-order-recovery-list")
    open_orders = await list_open_customer_paper_orders(customer_id=customer_id)
    _require(any(row.get("client_order_id") == order_id for row in open_orders), "partial order was not recoverable from persisted state")
    _set_operation("phase-one:verify-persisted-paper-balances")
    balances = await get_customer_paper_balances(customer_id=customer_id)
    _require(_balance_map(balances).get("PAPER_CASH", Decimal("0")) > 0, "paper cash balance was not persisted")
    return {
        "partial_fill": True,
        "same_payload_retry": replay.get("status"),
        "aggregate_bar_capacity_exhaustion": capacity_fill.get("status"),
        "bar_capacity": _fmt_decimal(bar_capacity),
        "bar_allocated_quantity": _fmt_decimal(bar_allocated),
        "capacity_probe_cancelled": capacity_cancel.get("status"),
        "conflicting_retry_blocked": conflicting_retry_blocked,
        "duplicate_bar_blocked": duplicate_bar_blocked,
        "restart_checkpoint_order_status": persisted["status"],
        "restart_checkpoint_filled_quantity": persisted["filled_quantity"],
    }


async def _phase_two(
    *,
    customer_id: int,
    other_customer_id: int,
    run_key: str,
    primary_order_id: str,
    reversal_order_id: str,
    bar_two_at: datetime,
    bar_three_at: datetime,
    symbol: str,
) -> dict[str, Any]:
    primary_before_restart = await get_customer_paper_order(
        customer_id=customer_id, client_order_id=primary_order_id
    )
    _require(
        primary_before_restart is not None
        and primary_before_restart.get("status") == "PARTIALLY_FILLED"
        and Decimal(primary_before_restart.get("filled_quantity", "0")) == Decimal("0.001"),
        "order state did not survive a fresh Python event loop",
    )
    _require(
        any(row.get("client_order_id") == primary_order_id for row in await list_open_customer_paper_orders(customer_id=customer_id)),
        "open order could not be reconstructed after restart",
    )

    bar_two = await _register_bar(
        label="primary-completing-fill",
        venue="binance",
        symbol=symbol,
        bar_at=bar_two_at,
        run_key=run_key,
    )
    group_marks = {
        ("binance", "spot", symbol): {
            "price": "100",
            "quote_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
    }
    second_fill = await record_customer_paper_fill(
        customer_id=customer_id,
        client_order_id=primary_order_id,
        idempotency_key=f"{run_key}:primary:fill-2",
        bar_key=bar_two["bar_key"],
        quantity="0.001",
        price="100",
        fee_amount="0.01",
        enforce_market_bar_budget=True,
        group_mark_prices=group_marks,
    )
    _require(second_fill.get("status") == "APPENDED" and second_fill.get("order_status") == "FILLED", "partial order did not complete after restart")

    primary_filled_balances = _balance_map(await get_customer_paper_balances(customer_id=customer_id))
    primary_position = await get_customer_paper_positions(customer_id=customer_id)
    _require(
        len(primary_position) == 1 and Decimal(primary_position[0]["quantity"]) == Decimal("0.002"),
        "completed primary order position snapshot is incorrect",
    )

    reversal_submission_digest = hashlib.sha256(f"{run_key}|submission|reversal".encode("utf-8")).hexdigest()
    reversal_order = await submit_customer_paper_order(
        customer_id=customer_id,
        client_order_id=reversal_order_id,
        venue="binance",
        market_type="spot",
        symbol=symbol,
        side="SELL",
        quantity="0.001",
        currency="USDT",
        order_type="MARKET",
        metadata=_market_metadata(bar_two_at, reversal_submission_digest),
    )
    _require(reversal_order.get("status") == "APPENDED", "reversal-test order intent was not appended")

    bar_three = await _register_bar(
        label="reversal-test-fill",
        venue="binance",
        symbol=symbol,
        bar_at=bar_three_at,
        run_key=run_key,
    )
    reversal_fill = await record_customer_paper_fill(
        customer_id=customer_id,
        client_order_id=reversal_order_id,
        idempotency_key=f"{run_key}:reversal:fill",
        bar_key=bar_three["bar_key"],
        quantity="0.001",
        price="100",
        fee_amount="0.01",
        enforce_market_bar_budget=True,
        group_mark_prices=group_marks,
    )
    _require(reversal_fill.get("status") == "APPENDED", "reversal-test fill was not appended")

    reversed_result = await reverse_customer_paper_fill(
        customer_id=customer_id,
        fill_id=reversal_fill["fill_id"],
        idempotency_key=f"{run_key}:reversal",
    )
    _require(reversed_result.get("status") == "APPENDED", "compensating fill reversal was not appended")
    reversal_replay = await reverse_customer_paper_fill(
        customer_id=customer_id,
        fill_id=reversal_fill["fill_id"],
        idempotency_key=f"{run_key}:reversal",
    )
    _require(reversal_replay.get("status") == "REPLAYED", "reversal retry did not replay the immutable reversal")

    final_balances = _balance_map(await get_customer_paper_balances(customer_id=customer_id))
    _require(final_balances == primary_filled_balances, "reversal failed to restore the pre-reversal paper balances")
    final_positions = await get_customer_paper_positions(customer_id=customer_id)
    _require(
        len(final_positions) == 1
        and Decimal(final_positions[0]["quantity"]) == Decimal("0.002"),
        "reversal failed to restore the prior position quantity",
    )
    final_primary_order = await get_customer_paper_order(
        customer_id=customer_id, client_order_id=primary_order_id
    )
    final_reversal_order = await get_customer_paper_order(
        customer_id=customer_id, client_order_id=reversal_order_id
    )
    _require(final_primary_order and final_primary_order["status"] == "FILLED", "primary order state changed unexpectedly")
    _require(final_reversal_order and final_reversal_order["status"] == "FILLED", "reversed order state changed unexpectedly")

    other_order = await get_customer_paper_order(
        customer_id=other_customer_id, client_order_id=primary_order_id
    )
    other_balances = await get_customer_paper_balances(customer_id=other_customer_id)
    other_positions = await get_customer_paper_positions(customer_id=other_customer_id)
    _require(other_order is None and not other_balances and not other_positions, "one synthetic tenant can read another tenant's ledger")

    open_orders = await list_open_customer_paper_orders(customer_id=customer_id)
    _require(not open_orders, "terminal test orders remain in the open-order recovery list")
    journal_count = await _read_journal_invariants(customer_id)
    final_preflight = await preflight_isolated_customer_paper_ledger(require_enabled=True)
    _require(final_preflight.get("status") == "PASS", "final customer-paper isolation preflight did not pass")
    _require(
        final_preflight.get("real_money_table_privileges") == "NONE_FOR_ALL_EXISTING_CHECKED_TABLES",
        "paper runtime has unexpected checked real-money table privileges",
    )
    return {
        "post_restart_fill": second_fill.get("status"),
        "primary_order_final_state": final_primary_order["status"],
        "compensating_reversal": reversed_result.get("status"),
        "reversal_retry": reversal_replay.get("status"),
        "balances_restored": True,
        "position_quantity_restored": "0.002",
        "cross_tenant_read_blocked": True,
        "open_order_recovery_clear": True,
        "balanced_journals_verified": journal_count,
        "isolation_preflight": "PASS",
        "real_money_table_privileges": final_preflight["real_money_table_privileges"],
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    _require(bool(settings.database_url), "primary read-only database URL is not configured")
    _require(bool(settings.customer_paper_database_url), "customer-paper database URL is not configured")
    _require(settings.customer_paper_ledger_enabled, "customer paper ledger feature flag is not enabled")
    _require(settings.paper_trading and settings.broker_sandbox, "paper trading and broker sandbox must remain enabled")
    _require(not settings.live_trading_enabled and not settings.customer_live_trading_enabled, "live trading must remain disabled")
    _require(not settings.payout_live_enabled, "live payouts must remain disabled")
    _require(not settings.deriv_live_enabled and not settings.usdt_tron_enabled and not settings.usdt_tron_sweep_enabled, "live provider/custody gates must remain disabled")

    digest = hashlib.sha256(f"{args.run_id}|{args.run_attempt}|{args.commit}".encode("utf-8")).hexdigest()
    customer_id = 8_000_000_000_000_000_000 + (int(digest[:12], 16) % 1_000_000_000)
    other_customer_id = customer_id + 1
    run_key = f"atlas-stage-{args.run_id}-{args.run_attempt}-{digest[:12]}"
    primary_order_id = f"{run_key}-primary"
    reversal_order_id = f"{run_key}-reversal"
    salt_seconds = int(digest[12:16], 16) % 45
    submission_at = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5) + timedelta(seconds=salt_seconds)
    bar_one_at = submission_at + timedelta(minutes=1)
    bar_two_at = submission_at + timedelta(minutes=2)
    bar_three_at = submission_at + timedelta(minutes=3)
    symbol = "BTC/USDT"

    # Close the first event loop completely before reconstructing the order from
    # durable rows. Each lifecycle operation owns and disposes its SQLAlchemy
    # engine, so no pooled async connection is carried across event loops.
    phase_one = asyncio.run(_phase_one(
        customer_id=customer_id,
        run_key=run_key,
        order_id=primary_order_id,
        submission_at=submission_at,
        bar_one_at=bar_one_at,
        symbol=symbol,
    ))
    phase_two = asyncio.run(_phase_two(
        customer_id=customer_id,
        other_customer_id=other_customer_id,
        run_key=run_key,
        primary_order_id=primary_order_id,
        reversal_order_id=reversal_order_id,
        bar_two_at=bar_two_at,
        bar_three_at=bar_three_at,
        symbol=symbol,
    ))
    return {
        "status": "PASS",
        "run_id": str(args.run_id),
        "run_attempt": str(args.run_attempt),
        "commit": str(args.commit),
        "synthetic_customer_id": customer_id,
        "database_isolation_mode": (
            "shared_database_schema"
            if settings.customer_paper_shared_database_enabled
            else "separate_database"
        ),
        "phase_one": phase_one,
        "phase_two": phase_two,
        "live_trading_enabled": False,
        "customer_live_trading_enabled": False,
        "payout_live_enabled": False,
        "real_customer_authentication_tested": False,
        "note": "Synthetic paper-ledger acceptance only; no real customer identity or real-money table writes. Recovery checkpoint crosses two separate asyncio.run event loops in one process.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=os.getenv("GITHUB_RUN_ID", "local"))
    parser.add_argument("--run-attempt", default=os.getenv("GITHUB_RUN_ATTEMPT", "1"))
    parser.add_argument("--commit", default=os.getenv("GITHUB_SHA", "local"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = _run(args)
    except Exception as exc:
        report = {
            "status": "FAIL",
            "run_id": str(args.run_id),
            "run_attempt": str(args.run_attempt),
            "commit": str(args.commit),
            "error_type": type(exc).__name__,
        }
        if args.output:
            args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"FAIL: customer-paper acceptance drill error_type={type(exc).__name__}")
        return 1
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print("PASS: customer-paper end-to-end ledger acceptance drill")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
