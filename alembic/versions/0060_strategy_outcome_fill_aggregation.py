"""Persist complete, verified per-trade summaries after partial closing fills.

Revision ID: 0060_strategy_outcome_fill_aggregation
Revises: 0059_research_integrity_controls

This migration is intentionally online-only: historical reconstruction cross-checks
strategy outcome fills against their source trade orders before admitting them to
risk samples. Generate offline SQL only after deploying a release that supports this
migration, using a live database upgrade as the authoritative migration operation.
"""
from __future__ import annotations

from decimal import Decimal

from alembic import context, op
import sqlalchemy as sa


revision = "0060_strategy_outcome_fill_aggregation"
down_revision = "0059_research_integrity_controls"
branch_labels = None
depends_on = None

_BATCH_SIZE = 100
_AMOUNT_TOLERANCE = Decimal("0.000001")
_RELATIVE_TOLERANCE = Decimal("0.00001")


def _close_enough(actual: Decimal, expected: Decimal) -> bool:
    tolerance = max(_AMOUNT_TOLERANCE, abs(expected) * _RELATIVE_TOLERANCE)
    return abs(actual - expected) <= tolerance


def _reconstruct_notional(row) -> Decimal:
    """Use exact stored notional, then net and gross ratio evidence in that order."""
    current = Decimal(str(row["closing_notional"] or 0))
    if current > 0:
        return current
    net_bps = Decimal(str(row["net_return_bps"] or 0))
    net_pnl = Decimal(str(row["net_pnl"] or 0))
    if net_bps != 0:
        recovered = abs(net_pnl * Decimal("10000") / net_bps)
        if recovered > 0:
            return recovered
    gross_bps = Decimal(str(row["return_bps"] or 0))
    gross_pnl = Decimal(str(row["realized_pnl"] or 0))
    if gross_bps != 0:
        recovered = abs(gross_pnl * Decimal("10000") / gross_bps)
        if recovered > 0:
            return recovered
    return Decimal("0")


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "Revision 0060 requires an online database connection to verify historical "
            "strategy outcome fills against trades. Apply it with 'alembic upgrade head' "
            "against the target database; offline --sql generation is not supported."
        )

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    outcome_columns = {
        column["name"] for column in inspector.get_columns("strategy_outcomes")
    }
    position_columns = {
        column["name"] for column in inspector.get_columns("positions")
    }

    financial_type = sa.Numeric(38, 18, asdecimal=False)
    if "closing_quantity" not in outcome_columns:
        op.add_column(
            "strategy_outcomes",
            sa.Column(
                "closing_quantity",
                financial_type,
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    if "closing_notional" not in outcome_columns:
        op.add_column(
            "strategy_outcomes",
            sa.Column(
                "closing_notional",
                financial_type,
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    if "entry_fee" not in position_columns:
        op.add_column(
            "positions",
            sa.Column(
                "entry_fee",
                financial_type,
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    update_outcome = sa.text("""
        UPDATE strategy_outcomes
        SET closing_quantity = :closing_quantity,
            closing_notional = :closing_notional,
            realized_pnl = :realized_pnl,
            fee = :fee,
            net_pnl = :net_pnl,
            return_bps = :return_bps,
            net_return_bps = :net_return_bps,
            lifecycle_status = :lifecycle_status
        WHERE id = :id
    """)

    # Keyset through a bounded number of distinct entry-trade IDs. Each entry's
    # fills stay together for aggregation; no whole-table history is held in RAM.
    last_trade_id = 0
    trade_id_query = sa.text("""
        SELECT DISTINCT trade_id
        FROM strategy_outcomes
        WHERE trade_id > :last_trade_id
          AND lifecycle_status IN ('OPEN', 'CLOSED')
        ORDER BY trade_id
        LIMIT :batch_size
    """)
    outcome_query = sa.text("""
        SELECT id, trade_id, closing_trade_id, sequence,
               realized_pnl, fee, net_pnl, return_bps, net_return_bps,
               closing_quantity, closing_notional, lifecycle_status
        FROM strategy_outcomes
        WHERE trade_id = :trade_id
          AND lifecycle_status IN ('OPEN', 'CLOSED')
        ORDER BY sequence, id
    """)
    trade_lookup = sa.text("""
        SELECT id, filled_quantity, average_fill_price, notional, fee
        FROM trades
        WHERE id IN :trade_ids
    """).bindparams(sa.bindparam("trade_ids", expanding=True))

    while True:
        trade_ids = [
            int(value)
            for value in bind.execute(
                trade_id_query,
                {"last_trade_id": last_trade_id, "batch_size": _BATCH_SIZE},
            ).scalars().all()
        ]
        if not trade_ids:
            break
        last_trade_id = max(trade_ids)

        for entry_trade_id in trade_ids:
            rows = [
                dict(row)
                for row in bind.execute(
                    outcome_query, {"trade_id": entry_trade_id}
                ).mappings().all()
            ]
            if not rows:
                continue

            closing_ids = sorted({
                int(row["closing_trade_id"])
                for row in rows
                if row["closing_trade_id"] is not None
            })
            closing_trades = {}
            if closing_ids:
                closing_trades = {
                    int(row["id"]): dict(row)
                    for row in bind.execute(
                        trade_lookup, {"trade_ids": closing_ids}
                    ).mappings().all()
                }

            # Reconstruct fill notionals, then verify every closing order's full
            # filled notional/quantity is represented. This catches historical
            # zero-PnL fills that the old producer never inserted.
            row_notionals = {}
            row_quantities = {}
            complete = bool(closing_ids) and len(closing_ids) == len({
                int(row["closing_trade_id"])
                for row in rows
                if row["closing_trade_id"] is not None
            })
            notionals_by_closing_trade = {}
            quantities_by_closing_trade = {}

            for row in rows:
                closing_id = row["closing_trade_id"]
                if closing_id is None or int(closing_id) not in closing_trades:
                    complete = False
                    continue
                closing_trade = closing_trades[int(closing_id)]
                notional = _reconstruct_notional(row)
                quantity = Decimal(str(row["closing_quantity"] or 0))
                avg_price = Decimal(str(closing_trade["average_fill_price"] or 0))
                if quantity <= 0 and notional > 0 and avg_price > 0:
                    quantity = notional / avg_price
                if notional <= 0 or quantity <= 0:
                    complete = False
                row_notionals[int(row["id"])] = notional
                row_quantities[int(row["id"])] = quantity
                notionals_by_closing_trade[int(closing_id)] = (
                    notionals_by_closing_trade.get(int(closing_id), Decimal("0")) + notional
                )
                quantities_by_closing_trade[int(closing_id)] = (
                    quantities_by_closing_trade.get(int(closing_id), Decimal("0")) + quantity
                )

            for closing_id in closing_ids:
                order = closing_trades.get(closing_id)
                if order is None:
                    complete = False
                    continue
                expected_notional = Decimal(str(order["notional"] or 0))
                expected_quantity = Decimal(str(order["filled_quantity"] or 0))
                actual_notional = notionals_by_closing_trade.get(closing_id, Decimal("0"))
                actual_quantity = quantities_by_closing_trade.get(closing_id, Decimal("0"))
                if (
                    expected_notional <= 0
                    or expected_quantity <= 0
                    or not _close_enough(actual_notional, expected_notional)
                    or not _close_enough(actual_quantity, expected_quantity)
                ):
                    complete = False

            has_closed_fill = any(
                str(row["lifecycle_status"] or "").upper() == "CLOSED"
                for row in rows
            )
            total_realized = sum(
                (Decimal(str(row["realized_pnl"] or 0)) for row in rows),
                Decimal("0"),
            )
            total_fee = sum(
                (Decimal(str(row["fee"] or 0)) for row in rows),
                Decimal("0"),
            )
            total_net = sum(
                (Decimal(str(row["net_pnl"] or 0)) for row in rows),
                Decimal("0"),
            )
            total_notional = sum(row_notionals.values(), Decimal("0"))
            total_quantity = sum(row_quantities.values(), Decimal("0"))

            # For still-open historical positions, store only per-fill evidence;
            # leave the lifecycle OPEN so a later live close can aggregate it.
            if not has_closed_fill:
                for row in rows:
                    row_id = int(row["id"])
                    bind.execute(update_outcome, {
                        "id": row_id,
                        "closing_quantity": row_quantities.get(row_id, Decimal("0")) if complete else Decimal("0"),
                        "closing_notional": row_notionals.get(row_id, Decimal("0")) if complete else Decimal("0"),
                        "realized_pnl": row["realized_pnl"],
                        "fee": row["fee"],
                        "net_pnl": row["net_pnl"],
                        "return_bps": row["return_bps"],
                        "net_return_bps": row["net_return_bps"],
                        "lifecycle_status": row["lifecycle_status"],
                    })
                continue

            # A completed trade must have every closing order and every filled
            # unit represented. Unknown historical fills are retained for audit
            # but cannot enter dashboard, edge-decay, or execution-health samples.
            final = rows[-1]
            valid_notional = total_notional if complete and total_notional > 0 else Decimal("0")
            valid_quantity = total_quantity if complete and total_quantity > 0 else Decimal("0")
            gross_return_bps = (
                float(total_realized / total_notional * Decimal("10000"))
                if valid_notional > 0 else 0.0
            )
            net_return_bps = (
                float(total_net / total_notional * Decimal("10000"))
                if valid_notional > 0 else 0.0
            )
            for row in rows[:-1]:
                bind.execute(update_outcome, {
                    "id": int(row["id"]),
                    "closing_quantity": row_quantities.get(int(row["id"]), Decimal("0")) if complete else Decimal("0"),
                    "closing_notional": row_notionals.get(int(row["id"]), Decimal("0")) if complete else Decimal("0"),
                    "realized_pnl": row["realized_pnl"],
                    "fee": row["fee"],
                    "net_pnl": row["net_pnl"],
                    "return_bps": row["return_bps"],
                    "net_return_bps": row["net_return_bps"],
                    "lifecycle_status": "PARTIAL_CLOSED",
                })
            bind.execute(update_outcome, {
                "id": int(final["id"]),
                "closing_quantity": valid_quantity,
                "closing_notional": valid_notional,
                "realized_pnl": total_realized,
                "fee": total_fee,
                "net_pnl": total_net,
                "return_bps": gross_return_bps,
                "net_return_bps": net_return_bps,
                "lifecycle_status": "CLOSED",
            })

    # Server defaults are used only to make existing rows valid while adding
    # NOT NULL fields; the ORM owns defaults after the migration completes.
    op.alter_column("strategy_outcomes", "closing_quantity", server_default=None)
    op.alter_column("strategy_outcomes", "closing_notional", server_default=None)
    op.alter_column("positions", "entry_fee", server_default=None)

    index_names = {
        item["name"] for item in inspector.get_indexes("strategy_outcomes")
    }
    if "ix_strategy_outcome_platform_completed" not in index_names:
        op.create_index(
            "ix_strategy_outcome_platform_completed",
            "strategy_outcomes",
            ["customer_id", "lifecycle_status", "created_at"],
        )


def downgrade() -> None:
    raise RuntimeError(
        "Revision 0060 cannot be downgraded safely: it consolidates historical fill "
        "rows and adds fee/notional basis required for accurate risk evidence. Restore "
        "a pre-0060 database backup or deploy a forward corrective migration."
    )
