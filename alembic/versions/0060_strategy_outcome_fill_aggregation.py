"""Store complete per-trade outcomes after partial closing fills.

Revision ID: 0060_strategy_outcome_fill_aggregation
Revises: 0059_research_integrity_controls
"""
from __future__ import annotations

from decimal import Decimal

from alembic import op
import sqlalchemy as sa


revision = "0060_strategy_outcome_fill_aggregation"
down_revision = "0059_research_integrity_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"]
        for column in inspector.get_columns("strategy_outcomes")
    }
    if "closing_notional" not in columns:
        op.add_column(
            "strategy_outcomes",
            sa.Column(
                "closing_notional",
                sa.Numeric(24, 8),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    # Historical rows stored net PnL and return bps but not the exact fill
    # notional. Recover it only when the saved ratio makes it mathematically
    # identifiable. Zero-return fills remain explicitly un-reconstructable.
    rows = bind.execute(sa.text("""
        SELECT id, trade_id, sequence, realized_pnl, fee, net_pnl,
               return_bps, net_return_bps, closing_notional
        FROM strategy_outcomes
        WHERE lifecycle_status = 'CLOSED'
        ORDER BY trade_id, sequence, id
    """)).mappings().all()

    groups: dict[int, list[dict]] = {}
    for row in rows:
        groups.setdefault(int(row["trade_id"]), []).append(dict(row))

    update = sa.text("""
        UPDATE strategy_outcomes
        SET realized_pnl = :realized_pnl,
            fee = :fee,
            net_pnl = :net_pnl,
            return_bps = :return_bps,
            net_return_bps = :net_return_bps,
            closing_notional = :closing_notional,
            lifecycle_status = :lifecycle_status
        WHERE id = :id
    """)

    for trade_rows in groups.values():
        reconstructed = []
        complete = True
        total_realized = Decimal("0")
        total_fee = Decimal("0")
        total_net = Decimal("0")
        for row in trade_rows:
            realized = Decimal(str(row["realized_pnl"] or 0))
            fee = Decimal(str(row["fee"] or 0))
            net = Decimal(str(row["net_pnl"] or 0))
            notional = Decimal(str(row["closing_notional"] or 0))
            net_bps = Decimal(str(row["net_return_bps"] or 0))
            if notional <= 0 and net_bps != 0:
                notional = abs(net * Decimal("10000") / net_bps)
            if notional <= 0:
                complete = False
            reconstructed.append((row, notional))
            total_realized += realized
            total_fee += fee
            total_net += net

        final_row, _ = reconstructed[-1]
        total_notional = sum((notional for _row, notional in reconstructed), Decimal("0"))
        valid_notional = total_notional if complete and total_notional > 0 else Decimal("0")
        total_return_bps = (
            float(total_realized / total_notional * Decimal("10000"))
            if valid_notional > 0 else 0.0
        )
        total_net_return_bps = (
            float(total_net / total_notional * Decimal("10000"))
            if valid_notional > 0 else 0.0
        )

        for row, notional in reconstructed[:-1]:
            bind.execute(update, {
                "id": int(row["id"]),
                "realized_pnl": row["realized_pnl"],
                "fee": row["fee"],
                "net_pnl": row["net_pnl"],
                "return_bps": row["return_bps"],
                "net_return_bps": row["net_return_bps"],
                "closing_notional": notional,
                "lifecycle_status": "PARTIAL_CLOSED",
            })

        bind.execute(update, {
            "id": int(final_row["id"]),
            "realized_pnl": total_realized,
            "fee": total_fee,
            "net_pnl": total_net,
            "return_bps": total_return_bps,
            "net_return_bps": total_net_return_bps,
            "closing_notional": valid_notional,
            "lifecycle_status": "CLOSED",
        })

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
    # The aggregation intentionally changes historical row semantics. A lossy
    # downgrade cannot recreate the original per-fill final statuses and ratios.
    # Keep the outcome column/index so rollback does not discard audit evidence.
    pass
