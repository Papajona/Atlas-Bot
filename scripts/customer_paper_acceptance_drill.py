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
    reverse_customer_paper_fill,
    submit_customer_paper_order,
)


_ACTIVE_OPERATION = "startup"


def _set_operation(name: str) -> None:
    global _ACTIVE_OPERATION
    _ACTIVE_OPERATION = str(name)[:120]


def _safe_failure_reason(exc: Exception) -> str:
    """Keep useful assertion/validation reasons while withholding DB diagnostics and secrets."""
    if isinstance(exc, (RuntimeError, ValueError, AssertionError)):
        reason = str(exc).splitlines()[0].strip()
    else:
        reason = "Detailed database/runtime exception text withheld; inspect the redacted stack frames"
    reason = re.sub(
        r"(?i)\bpostgres(?:ql)?(?:\+asyncpg)?://[^\s\"'<>]+",
        "[database-uri-redacted]",
        reason,
    )
    reason = re.sub(
        r"(?i)\b(password|token|secret|api[_-]?key|authorization)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        reason,
    )
    reason = re.sub(
        r"(?i)\b(customer|tenant)[_-]?id\b\s*[:=]\s*\d+",
        "tenant_id=[redacted]",
        reason,
    )
    reason = re.sub(r"\b\d{12,}\b", "[identifier-redacted]", reason)
    reason = re.sub(r"\s+", " ", reason).strip()
    return (reason or "No safe failure reason available")[:240]


def _redacted_traceback(exc: Exception) -> str:
    """Return file/line/function frames only; do not serialize locals, SQL, or parameters."""
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    safe = [
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in frames[-8:]
    ]
    return " > ".join(safe) or "unavailable"


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
) -> dict[str, str]:
    _set_operation(f"register_market_bar:{label}")
    digest = hashlib.sha256(f"{run_key}|{label}|bar".encode("utf-8")).hexdigest()
    result = await register_customer_paper_market_bar(
        venue=venue,
        symbol=symbol,
        timeframe="1m",
        bar_at=bar_at,
        bar_data_sha256=digest,
        base_volume="1",
        participation_rate="0.01",
    )
    _require(result.get("status") in {"APPENDED", "REPLAYED"}, f"{label}: market-bar budget registration failed")
    _require(Decimal(result["capacity"]) >= Decimal("0.001"), f"{label}: market-bar capacity too small")
    return result


async def _read_journal_invariants(customer_id: int) -> int:
    _set_operation("verify_balanced_customer_paper_journals")
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


async def _verify_tenant_rls_visibility(
    *,
    visible_customer_id: int,
    hidden_customer_id: int,
) -> dict[str, int]:
    """Issue direct RLS-sensitive queries under the second tenant's DB session context."""
    _set_operation("verify_database_row_level_tenant_isolation")
    engine = _paper_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.customer_id', :customer_id, true)"),
                {"customer_id": str(visible_customer_id)},
            )
            row = (await conn.execute(text("""
                SELECT
                    (SELECT count(*) FROM customer_paper.accounts
                     WHERE customer_id=:visible_customer_id) AS visible_accounts,
                    (SELECT count(*) FROM customer_paper.orders
                     WHERE customer_id=:visible_customer_id) AS visible_orders,
                    (SELECT count(*) FROM customer_paper.journals
                     WHERE customer_id=:visible_customer_id) AS visible_journals,
                    (SELECT count(*) FROM customer_paper.accounts
                     WHERE customer_id=:hidden_customer_id) AS hidden_accounts,
                    (SELECT count(*) FROM customer_paper.orders
                     WHERE customer_id=:hidden_customer_id) AS hidden_orders,
                    (SELECT count(*) FROM customer_paper.journals
                     WHERE customer_id=:hidden_customer_id) AS hidden_journals
            """), {
                "visible_customer_id": visible_customer_id,
                "hidden_customer_id": hidden_customer_id,
            })).mappings().one()
        result = {key: int(value) for key, value in row.items()}
        _require(result["visible_accounts"] >= 2, "second tenant's own account rows were not visible under its RLS context")
        _require(result["visible_orders"] >= 1, "second tenant's own order was not visible under its RLS context")
        _require(result["visible_journals"] >= 2, "second tenant's own journal rows were not visible under its RLS context")
        _require(
            result["hidden_accounts"] == 0
            and result["hidden_orders"] == 0
            and result["hidden_journals"] == 0,
            "RLS exposed first-tenant account, order, or journal rows to the second tenant",
        )
        return result
    finally:
        await engine.dispose()


async def _phase_one(
    *,
    customer_id: int,
    other_customer_id: int,
    run_key: str,
    order_id: str,
    secondary_order_id: str,
    submission_at: datetime,
    bar_one_at: datetime,
    symbol: str,
) -> dict[str, Any]:
    _set_operation("phase_one.isolation_preflight")
    preflight = await preflight_isolated_customer_paper_ledger(require_enabled=True)
    _require(preflight.get("status") == "PASS", "customer-paper isolation preflight did not pass")

    _set_operation("phase_one.seed_primary_synthetic_tenant")
    seeded = await ensure_customer_paper_account(customer_id, "USDT")
    _require(seeded.get("status") == "PASS" and seeded.get("real_funds_used") is False, "primary virtual account seed failed")
    _set_operation("phase_one.verify_primary_seed_idempotency")
    reseeded = await ensure_customer_paper_account(customer_id, "USDT")
    _require(reseeded.get("paper_cash") == seeded.get("paper_cash"), "primary virtual account initialization was not idempotent")

    _set_operation("phase_one.seed_second_synthetic_tenant")
    secondary_seed = await ensure_customer_paper_account(other_customer_id, "USDT")
    _require(secondary_seed.get("status") == "PASS", "second virtual account seed failed")

    _set_operation("phase_one.submit_primary_order")
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
    )
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
    _set_operation("phase_one.append_primary_partial_fill")
    first_fill = await record_customer_paper_fill(**fill_args)
    _require(first_fill.get("status") == "APPENDED", "first partial fill was not appended")
    _set_operation("phase_one.replay_immutable_primary_fill")
    replay = await record_customer_paper_fill(**fill_args)
    _require(
        replay.get("status") == "REPLAYED" and replay.get("fill_id") == first_fill.get("fill_id"),
        "identical fill retry did not return the immutable prior fill",
    )

    _set_operation("phase_one.reject_conflicting_fill_retry")
    try:
        await record_customer_paper_fill(**{**fill_args, "price": "100.01"})
    except CustomerPaperIdempotencyConflict:
        conflicting_retry_blocked = True
    else:
        raise RuntimeError("same fill idempotency key accepted a different price")

    _set_operation("phase_one.reject_duplicate_order_bar_fill")
    try:
        await record_customer_paper_fill(
            **{**fill_args, "idempotency_key": f"{run_key}:primary:duplicate-bar"}
        )
    except CustomerPaperIdempotencyConflict:
        duplicate_bar_blocked = True
    else:
        raise RuntimeError("one order accepted two fills against the same completed bar")

    _set_operation("phase_one.verify_primary_partial_order_recovery_checkpoint")
    persisted = await get_customer_paper_order(customer_id=customer_id, client_order_id=order_id)
    _require(
        persisted is not None
        and persisted.get("status") == "PARTIALLY_FILLED"
        and Decimal(persisted.get("filled_quantity", "0")) == Decimal("0.001"),
        "partial order state was not persisted correctly",
    )
    open_orders = await list_open_customer_paper_orders(customer_id=customer_id)
    _require(any(row.get("client_order_id") == order_id for row in open_orders), "partial order was not recoverable from persisted state")

    _set_operation("phase_one.submit_second_tenant_capacity_order")
    secondary_submission_digest = hashlib.sha256(f"{run_key}|submission|secondary-capacity".encode("utf-8")).hexdigest()
    secondary_order = await submit_customer_paper_order(
        customer_id=other_customer_id,
        client_order_id=secondary_order_id,
        venue="binance",
        market_type="spot",
        symbol=symbol,
        side="BUY",
        quantity="0.02",
        currency="USDT",
        order_type="MARKET",
        metadata=_market_metadata(submission_at, secondary_submission_digest),
    )
    _require(secondary_order.get("status") == "APPENDED", "second tenant's shared-capacity order intent was not appended")

    _set_operation("phase_one.enforce_aggregate_shared_bar_capacity")
    secondary_fill = await record_customer_paper_fill(
        customer_id=other_customer_id,
        client_order_id=secondary_order_id,
        idempotency_key=f"{run_key}:secondary:capacity-fill",
        bar_key=bar["bar_key"],
        quantity="0.02",
        price="100",
        fee_amount="0.20",
        enforce_market_bar_budget=True,
    )
    _require(
        secondary_fill.get("status") == "APPENDED"
        and secondary_fill.get("order_status") == "PARTIALLY_FILLED"
        and Decimal(secondary_fill.get("filled_quantity", "0")) == Decimal("0.009"),
        "second tenant's fill was not capped to the shared bar's remaining 0.009 quantity",
    )
    exhausted_bar = await _register_bar(
        label="primary-partial-fill",
        venue="binance",
        symbol=symbol,
        bar_at=bar_one_at,
        run_key=run_key,
    )
    _require(
        Decimal(exhausted_bar["consumed"]) == Decimal("0.01")
        and Decimal(exhausted_bar["available"]) == Decimal("0"),
        "aggregate allocations did not consume exactly the registered 0.01 shared-bar capacity",
    )

    _set_operation("phase_one.verify_primary_paper_balance")
    balances = await get_customer_paper_balances(customer_id=customer_id)
    _require(_balance_map(balances).get("PAPER_CASH", Decimal("0")) > 0, "primary paper cash balance was not persisted")
    return {
        "partial_fill": True,
        "same_payload_retry": replay.get("status"),
        "conflicting_retry_blocked": conflicting_retry_blocked,
        "duplicate_bar_blocked": duplicate_bar_blocked,
        "shared_bar_capacity": exhausted_bar["capacity"],
        "shared_bar_consumed": exhausted_bar["consumed"],
        "shared_bar_remaining": exhausted_bar["available"],
        "second_tenant_fill_quantity": secondary_fill["filled_quantity"],
        "secondary_order_status": secondary_fill["order_status"],
        "restart_checkpoint_order_status": persisted["status"],
        "restart_checkpoint_filled_quantity": persisted["filled_quantity"],
        "second_tenant_seeded": True,
    }


async def _phase_two(
    *,
    customer_id: int,
    other_customer_id: int,
    run_key: str,
    primary_order_id: str,
    secondary_order_id: str,
    reversal_order_id: str,
    bar_two_at: datetime,
    bar_three_at: datetime,
    symbol: str,
) -> dict[str, Any]:
    _set_operation("phase_two.recover_primary_order_after_fresh_event_loop")
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

    _set_operation("phase_two.register_primary_completion_bar")
    bar_two = await _register_bar(
        label="primary-completing-fill",
        venue="binance",
        symbol=symbol,
        bar_at=bar_two_at,
        run_key=run_key,
    )
    async def load_current_group_marks():
        # Called by record_customer_paper_fill after its isolated-ledger readiness
        # preflight, so this synthetic quote starts its five-second freshness window
        # immediately before the fill transaction.
        return {
            ("binance", "spot", symbol): {
                "price": "100",
                "quote_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            }
        }

    _set_operation("phase_two.complete_primary_order_after_restart")
    second_fill = await record_customer_paper_fill(
        customer_id=customer_id,
        client_order_id=primary_order_id,
        idempotency_key=f"{run_key}:primary:fill-2",
        bar_key=bar_two["bar_key"],
        quantity="0.001",
        price="100",
        fee_amount="0.01",
        enforce_market_bar_budget=True,
        group_mark_prices_loader=load_current_group_marks,
    )
    _require(second_fill.get("status") == "APPENDED" and second_fill.get("order_status") == "FILLED", "partial order did not complete after restart")

    primary_filled_balances = _balance_map(await get_customer_paper_balances(customer_id=customer_id))
    primary_position = await get_customer_paper_positions(customer_id=customer_id)
    _require(
        len(primary_position) == 1 and Decimal(primary_position[0]["quantity"]) == Decimal("0.002"),
        "completed primary order position snapshot is incorrect",
    )

    _set_operation("phase_two.submit_compensating_reversal_order")
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
    _set_operation("phase_two.append_reversal_test_fill")
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

    _set_operation("phase_two.append_compensating_fill_reversal")
    reversed_result = await reverse_customer_paper_fill(
        customer_id=customer_id,
        fill_id=reversal_fill["fill_id"],
        idempotency_key=f"{run_key}:reversal",
    )
    _require(reversed_result.get("status") == "APPENDED", "compensating fill reversal was not appended")
    _set_operation("phase_two.verify_reversal_idempotency")
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

    _set_operation("phase_two.verify_second_tenant_own_ledger_reads")
    secondary_order = await get_customer_paper_order(
        customer_id=other_customer_id, client_order_id=secondary_order_id
    )
    cross_tenant_order = await get_customer_paper_order(
        customer_id=other_customer_id, client_order_id=primary_order_id
    )
    other_balances = await get_customer_paper_balances(customer_id=other_customer_id)
    other_positions = await get_customer_paper_positions(customer_id=other_customer_id)
    _require(
        secondary_order is not None
        and secondary_order["status"] == "PARTIALLY_FILLED"
        and Decimal(secondary_order["filled_quantity"]) == Decimal("0.009"),
        "second tenant could not read its own partially filled capacity order",
    )
    _require(
        cross_tenant_order is None
        and _balance_map(other_balances).get("PAPER_CASH", Decimal("0")) > 0
        and len(other_positions) == 1
        and Decimal(other_positions[0]["quantity"]) == Decimal("0.009"),
        "application-level tenant reads returned incorrect ownership results",
    )

    rls_visibility = await _verify_tenant_rls_visibility(
        visible_customer_id=other_customer_id,
        hidden_customer_id=customer_id,
    )
    open_orders = await list_open_customer_paper_orders(customer_id=customer_id)
    _require(not open_orders, "terminal primary-tenant test orders remain in the open-order recovery list")
    secondary_open_orders = await list_open_customer_paper_orders(customer_id=other_customer_id)
    _require(
        any(row.get("client_order_id") == secondary_order_id for row in secondary_open_orders),
        "second tenant's partial order was not isolated and recoverable",
    )
    journal_count = await _read_journal_invariants(customer_id)
    secondary_journal_count = await _read_journal_invariants(other_customer_id)
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
        "cross_tenant_application_read_blocked": True,
        "cross_tenant_rls_query_hidden_rows": (
            rls_visibility["hidden_accounts"] == 0
            and rls_visibility["hidden_orders"] == 0
            and rls_visibility["hidden_journals"] == 0
        ),
        "second_tenant_own_rows_visible": (
            rls_visibility["visible_accounts"] >= 2
            and rls_visibility["visible_orders"] >= 1
            and rls_visibility["visible_journals"] >= 2
        ),
        "second_tenant_partial_order_recoverable": True,
        "open_primary_order_recovery_clear": True,
        "balanced_journals_verified": journal_count + secondary_journal_count,
        "primary_tenant_balanced_journals": journal_count,
        "secondary_tenant_balanced_journals": secondary_journal_count,
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

    _set_operation("setup.synthetic_run_identity")
    digest = hashlib.sha256(f"{args.run_id}|{args.run_attempt}|{args.commit}".encode("utf-8")).hexdigest()
    customer_id = 8_000_000_000_000_000_000 + (int(digest[:12], 16) % 1_000_000_000)
    other_customer_id = customer_id + 1
    run_key = f"atlas-stage-{args.run_id}-{args.run_attempt}-{digest[:12]}"
    primary_order_id = f"{run_key}-primary"
    secondary_order_id = f"{run_key}-capacity"
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
        other_customer_id=other_customer_id,
        run_key=run_key,
        order_id=primary_order_id,
        secondary_order_id=secondary_order_id,
        submission_at=submission_at,
        bar_one_at=bar_one_at,
        symbol=symbol,
    ))
    phase_two = asyncio.run(_phase_two(
        customer_id=customer_id,
        other_customer_id=other_customer_id,
        run_key=run_key,
        primary_order_id=primary_order_id,
        secondary_order_id=secondary_order_id,
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
        "synthetic_tenants_used": 2,
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
        reason = _safe_failure_reason(exc)
        stack = _redacted_traceback(exc)
        report = {
            "status": "FAIL",
            "run_id": str(args.run_id),
            "run_attempt": str(args.run_attempt),
            "commit": str(args.commit),
            "operation": _ACTIVE_OPERATION,
            "error_type": type(exc).__name__,
            "error_reason": reason,
            "redacted_traceback": stack,
        }
        if args.output:
            args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            f"FAIL: operation={_ACTIVE_OPERATION} error_type={type(exc).__name__} "
            f"reason={reason} redacted_traceback={stack}",
            file=sys.stderr,
        )
        return 1
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print("PASS: customer-paper end-to-end ledger acceptance drill")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
