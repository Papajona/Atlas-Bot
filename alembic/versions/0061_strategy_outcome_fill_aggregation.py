"""Persist complete, verified per-trade summaries after partial closing fills.

Revision ID: 0061_strategy_outcome_fill_aggregation
Revises: 0060_customer_identity_unmapped_index

This migration is intentionally online-only: historical reconstruction cross-checks
strategy outcome fills against their source trade orders before admitting them to
risk samples. Generate offline SQL only after deploying a release that supports this
migration, using a live database upgrade as the authoritative migration operation.
"""
from __future__ import annotations

from decimal import Decimal

from alembic import context, op
import sqlalchemy as sa


revision = "0061_strategy_outcome_fill_aggregation"
down_revision = "0060_customer_identity_unmapped_index"
branch_labels = None
depends_on = None

_BATCH_SIZE = 100
_AMOUNT_TOLERANCE = Decimal("0.000001")
_RELATIVE_TOLERANCE = Decimal("0.00001")


def _close_enough(actual: Decimal, expected: Decimal) -> bool:
    tolerance = max(_AMOUNT_TOLERANCE, abs(expected) * _RELATIVE_TOLERANCE)
    return abs(actual - expected) <= tolerance


def _reconcile_closing_order_fill_evidence(
    expected_quantity: Decimal,
    actual_quantity: Decimal,
    actual_notional: Decimal,
) -> bool:
    """Reconcile all executed units and positive per-fill notional, not last-price notional."""
    return (
        expected_quantity.is_finite()
        and actual_quantity.is_finite()
        and actual_notional.is_finite()
        and expected_quantity > 0
        and actual_notional > 0
        and _close_enough(actual_quantity, expected_quantity)
    )


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




def _reconstruct_trade_fee_total(
    entry_trade_id: int,
    closing_trade_ids: list[int],
    source_trades: dict[int, dict],
) -> Decimal | None:
    """Sum an entry order and distinct closing orders once, never repeated snapshots."""
    ids = {int(entry_trade_id), *(int(value) for value in closing_trade_ids)}
    total = Decimal("0")
    for order_id in ids:
        order = source_trades.get(order_id)
        # Older live trades did not persist the source quality of their fee.
        # A non-null amount may be an estimate, so it cannot establish a
        # verified net-return basis. Deterministic paper-model fees are known.
        if (
            order is None
            or order.get("fee") is None
            or str(order.get("mode") or "").upper() not in {"PAPER", "PAPER_SHADOW"}
        ):
            return None
        try:
            fee = Decimal(str(order["fee"]))
        except (TypeError, ValueError, ArithmeticError):
            return None
        if not fee.is_finite() or fee < 0:
            return None
        total += fee
    return total


def _reconstruct_remaining_entry_fee(
    entry_trade: dict | None,
    remaining_quantity: Decimal,
    closed_quantity: Decimal,
) -> Decimal | None:
    """Backfill only when the entry order and all recorded closes reconcile."""
    if entry_trade is None or entry_trade.get("fee") is None:
        return None
    try:
        entered = Decimal(str(entry_trade.get("filled_quantity") or 0))
        fee = Decimal(str(entry_trade["fee"]))
        remaining = abs(Decimal(str(remaining_quantity)))
        closed = abs(Decimal(str(closed_quantity)))
    except (TypeError, ValueError, ArithmeticError):
        return None
    if (
        not entered.is_finite() or not fee.is_finite()
        or not remaining.is_finite() or not closed.is_finite()
        or entered <= 0 or fee < 0 or remaining <= 0
        or not _close_enough(remaining + closed, entered)
    ):
        return None
    return fee * remaining / entered



def _split_outcome_lifecycles(rows: list[dict]) -> list[list[dict]]:
    """Split reused entry-order outcomes at each terminal close marker.

    Historical producers marked partial closes OPEN and the terminal fill CLOSED.
    A reused entry order can therefore contain multiple lifecycles under one
    trade_id; aggregating the whole trade_id would combine separate positions.
    """
    lifecycles: list[list[dict]] = []
    current: list[dict] = []
    for row in rows:
        current.append(row)
        if str(row.get("lifecycle_status") or "").upper() == "CLOSED":
            lifecycles.append(current)
            current = []
    if current:
        lifecycles.append(current)
    return lifecycles


def _episode_entry_quantity(
    episode: dict | None,
    filled_quantity: Decimal,
) -> Decimal | None:
    """Read an episode's actual opened leg only when its lifecycle state supports it."""
    if episode is None:
        return None
    try:
        quantity = Decimal(str(episode.get("entry_quantity") or 0))
    except (TypeError, ValueError, ArithmeticError):
        return None
    status = str(episode.get("status") or "").upper()
    if not quantity.is_finite() or quantity < 0:
        return None
    if status == "NOT_POSITION_ENTRY":
        return Decimal("0") if quantity == 0 else None
    if status not in {"OPEN", "COMPLETED"} or quantity <= 0:
        return None
    if quantity > filled_quantity + _AMOUNT_TOLERANCE:
        return None
    return min(quantity, filled_quantity)


def _expected_closing_order_quantity(
    order: dict | None,
    episode: dict | None,
) -> Decimal | None:
    """Exclude a reversal order's newly opened opposite leg from its old close."""
    if order is None:
        return None
    try:
        filled = Decimal(str(order.get("filled_quantity") or 0))
    except (TypeError, ValueError, ArithmeticError):
        return None
    if not filled.is_finite() or filled <= 0:
        return None
    if episode is None:
        return filled
    opened_quantity = _episode_entry_quantity(episode, filled)
    if opened_quantity is None:
        return None
    expected = filled - opened_quantity
    if expected <= 0:
        return None
    return expected


def _entry_order_fee_fraction(
    order: dict | None,
    episode: dict | None,
    *,
    reused_entry_order: bool,
) -> Decimal | None:
    """Allocate only the part of a reversal order that opened this position."""
    if order is None or reused_entry_order:
        return None
    try:
        filled = Decimal(str(order.get("filled_quantity") or 0))
    except (TypeError, ValueError, ArithmeticError):
        return None
    if not filled.is_finite() or filled <= 0:
        return None
    if episode is None:
        return Decimal("1")
    opened_quantity = _episode_entry_quantity(episode, filled)
    if opened_quantity is None or opened_quantity <= 0:
        return None
    return opened_quantity / filled


def _closing_order_fee_fraction(
    order: dict | None,
    episode: dict | None,
) -> Decimal | None:
    """Allocate only the part of a reversal order that closed the old position."""
    if order is None:
        return None
    try:
        filled = Decimal(str(order.get("filled_quantity") or 0))
    except (TypeError, ValueError, ArithmeticError):
        return None
    expected_close = _expected_closing_order_quantity(order, episode)
    if expected_close is None or not filled.is_finite() or filled <= 0:
        return None
    return expected_close / filled


def _reconstruct_lifecycle_fee_total(
    entry_trade_id: int,
    closing_trade_ids: list[int],
    source_trades: dict[int, dict],
    *,
    reused_entry_order: bool,
    entry_fee_fraction: Decimal | None,
    closing_fee_fractions: dict[int, Decimal | None],
) -> Decimal | None:
    """Sum each source order once, prorating reversal fees across both positions."""
    if reused_entry_order or entry_fee_fraction is None:
        return None
    if int(entry_trade_id) in {int(value) for value in closing_trade_ids}:
        return None
    total = Decimal("0")
    fractions = {int(entry_trade_id): entry_fee_fraction}
    fractions.update({
        int(order_id): fraction
        for order_id, fraction in closing_fee_fractions.items()
    })
    for order_id in {int(entry_trade_id), *(int(value) for value in closing_trade_ids)}:
        fraction = fractions.get(order_id)
        if fraction is None or not fraction.is_finite() or fraction < 0 or fraction > 1:
            return None
        order_fee = _reconstruct_trade_fee_total(order_id, [], source_trades)
        if order_fee is None:
            return None
        total += order_fee * fraction
    return total


def _fee_value_to_persist(
    reconstructed_fee: Decimal,
    recorded_fee: Decimal | None,
    *,
    fee_basis_complete: bool,
) -> Decimal:
    """Never erase historical fee evidence merely because its basis is unverified."""
    if fee_basis_complete:
        return reconstructed_fee
    try:
        previous = Decimal(str(recorded_fee or 0))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")
    return previous if previous.is_finite() and previous >= 0 else Decimal("0")


def _historical_lifecycle_fee_basis_complete(
    rows: list[dict],
    *,
    fill_evidence_complete: bool,
    fee_total: Decimal | None,
    lifecycle_open: bool,
) -> bool:
    """Reject open lifecycles whose partial rows repeat one order's cumulative fee."""
    if not fill_evidence_complete or fee_total is None:
        return False
    if lifecycle_open:
        closing_order_counts: dict[int, int] = {}
        for row in rows:
            closing_trade_id = row.get("closing_trade_id")
            if closing_trade_id is None:
                continue
            order_id = int(closing_trade_id)
            closing_order_counts[order_id] = closing_order_counts.get(order_id, 0) + 1
        if any(count > 1 for count in closing_order_counts.values()):
            # Historical open rows can store the same cumulative trades.fee and
            # net_pnl on several fills from one closing order. The migration cannot
            # allocate that cumulative fee back to each fill without encrypted
            # per-fill metadata, so preserve the rows for audit but fail closed.
            return False
    return True


def _cross_role_reused_order_ids(
    role_rows: list[dict],
    candidate_order_ids: set[int],
) -> set[int]:
    """Find orders used both to open and close position lifecycles."""
    entry_ids = {
        int(row["trade_id"])
        for row in role_rows
        if row.get("trade_id") is not None
    }
    closing_ids = {
        int(row["closing_trade_id"])
        for row in role_rows
        if row.get("closing_trade_id") is not None
    }
    return (entry_ids & closing_ids) & {
        int(value) for value in candidate_order_ids
    }

def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "Revision 0061 requires an online database connection to verify historical "
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
    trade_columns = {
        column["name"] for column in inspector.get_columns("trades")
    }

    # Historical 0035 schema declared net-result fields NOT NULL. Revision
    # 0061 writes NULL when their fee basis cannot be verified, so relax the
    # existing columns before the outcome backfill runs. Keep server defaults
    # for mixed-version writers during rolling deployment.
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("strategy_outcomes") as batch_op:
            if "net_pnl" in outcome_columns:
                batch_op.alter_column(
                    "net_pnl",
                    existing_type=sa.Numeric(38, 18),
                    existing_nullable=False,
                    nullable=True,
                )
            if "net_return_bps" in outcome_columns:
                batch_op.alter_column(
                    "net_return_bps",
                    existing_type=sa.Float(),
                    existing_nullable=False,
                    nullable=True,
                )
    else:
        if "net_pnl" in outcome_columns:
            op.alter_column(
                "strategy_outcomes",
                "net_pnl",
                existing_type=sa.Numeric(38, 18),
                existing_nullable=False,
                nullable=True,
            )
        if "net_return_bps" in outcome_columns:
            op.alter_column(
                "strategy_outcomes",
                "net_return_bps",
                existing_type=sa.Float(),
                existing_nullable=False,
                nullable=True,
            )

    financial_type = sa.Numeric(38, 18, asdecimal=False)
    if "fee_reconciliation_pending" not in trade_columns:
        op.add_column(
            "trades",
            sa.Column(
                "fee_reconciliation_pending",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
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
    if "entry_fee_basis_known" not in position_columns:
        op.add_column(
            "positions",
            sa.Column(
                "entry_fee_basis_known",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
    if "fee_basis_complete" not in outcome_columns:
        op.add_column(
            "strategy_outcomes",
            sa.Column(
                "fee_basis_complete",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
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
            lifecycle_status = :lifecycle_status,
            fee_basis_complete = :fee_basis_complete
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
        SELECT id, filled_quantity, average_fill_price, notional, fee, mode
        FROM trades
        WHERE id IN :trade_ids
    """).bindparams(sa.bindparam("trade_ids", expanding=True))
    episode_lookup = sa.text("""
        SELECT entry_trade_id, entry_quantity, status
        FROM trade_learning_episodes
        WHERE entry_trade_id IN :trade_ids
    """).bindparams(sa.bindparam("trade_ids", expanding=True))
    order_role_lookup = sa.text("""
        SELECT trade_id, closing_trade_id
        FROM strategy_outcomes
        WHERE trade_id IN :trade_ids
           OR closing_trade_id IN :trade_ids
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

            lifecycle_groups = _split_outcome_lifecycles(rows)
            reused_entry_order = len(lifecycle_groups) > 1
            for lifecycle_rows in lifecycle_groups:
                if not lifecycle_rows:
                    continue
                rows = lifecycle_rows
                closing_ids = sorted({
                    int(row["closing_trade_id"])
                    for row in rows
                    if row["closing_trade_id"] is not None
                })
                source_trade_ids = sorted({entry_trade_id, *closing_ids})
                source_trades = {
                    int(row["id"]): dict(row)
                    for row in bind.execute(
                        trade_lookup, {"trade_ids": source_trade_ids}
                    ).mappings().all()
                }
                closing_trades = {
                    closing_id: source_trades[closing_id]
                    for closing_id in closing_ids
                    if closing_id in source_trades
                }
                episode_rows = bind.execute(
                    episode_lookup, {"trade_ids": source_trade_ids}
                ).mappings().all()
                episodes_by_trade_id = {
                    int(row["entry_trade_id"]): dict(row) for row in episode_rows
                }
                role_rows = [
                    dict(row)
                    for row in bind.execute(
                        order_role_lookup, {"trade_ids": source_trade_ids}
                    ).mappings().all()
                ]
                cross_role_reused_ids = _cross_role_reused_order_ids(
                    role_rows, set(source_trade_ids)
                )
                entry_order_reused = (
                    reused_entry_order or entry_trade_id in cross_role_reused_ids
                )
                entry_fee_fraction = _entry_order_fee_fraction(
                    source_trades.get(entry_trade_id),
                    episodes_by_trade_id.get(entry_trade_id),
                    reused_entry_order=entry_order_reused,
                )
                expected_closing_quantities: dict[int, Decimal | None] = {}
                closing_fee_fractions: dict[int, Decimal | None] = {}
                for closing_id in closing_ids:
                    closing_order = closing_trades.get(closing_id)
                    closing_episode = episodes_by_trade_id.get(closing_id)
                    if closing_id in cross_role_reused_ids:
                        # The unique episode row now describes the later entry leg,
                        # not necessarily the historical closing leg. Preserve the
                        # outcomes but do not invent either quantity or fee shares.
                        expected_closing_quantities[closing_id] = None
                        closing_fee_fractions[closing_id] = None
                    else:
                        expected_closing_quantities[closing_id] = (
                            _expected_closing_order_quantity(
                                closing_order, closing_episode
                            )
                        )
                        closing_fee_fractions[closing_id] = (
                            _closing_order_fee_fraction(
                                closing_order, closing_episode
                            )
                        )
                fee_total = _reconstruct_lifecycle_fee_total(
                    entry_trade_id,
                    closing_ids,
                    source_trades,
                    reused_entry_order=entry_order_reused,
                    entry_fee_fraction=entry_fee_fraction,
                    closing_fee_fractions=closing_fee_fractions,
                )
    
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
                    expected_quantity = expected_closing_quantities.get(closing_id)
                    if expected_quantity is None:
                        complete = False
                        continue
                    actual_notional = notionals_by_closing_trade.get(closing_id, Decimal("0"))
                    actual_quantity = quantities_by_closing_trade.get(closing_id, Decimal("0"))
                    if not _reconcile_closing_order_fill_evidence(
                        expected_quantity, actual_quantity, actual_notional
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
                # Historical outcome rows can repeat a closing order's cumulative
                # fee. Reconstruct from each source order once rather than summing rows.
                fee_basis_complete = _historical_lifecycle_fee_basis_complete(
                    rows,
                    fill_evidence_complete=complete,
                    fee_total=fee_total,
                    lifecycle_open=not has_closed_fill,
                )
                total_fee = fee_total if fee_basis_complete else Decimal("0")
                total_net = (
                    total_realized - total_fee
                    if fee_basis_complete else Decimal("0")
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
                            "fee_basis_complete": fee_basis_complete,
                        })
                    continue
    
                # A completed trade must have every closing order and every filled
                # unit represented. Unknown historical fills are retained for audit
                # but cannot enter dashboard, edge-decay, or execution-health samples.
                final = rows[-1]
                # Keep the reconstructed historical quantities/notionals for audit even
                # when another evidence gate fails. fee_basis_complete remains false,
                # so these rows cannot enter net-return risk samples.
                valid_notional = total_notional if total_notional > 0 else Decimal("0")
                valid_quantity = total_quantity if total_quantity > 0 else Decimal("0")
                gross_return_bps = (
                    float(total_realized / total_notional * Decimal("10000"))
                    if valid_notional > 0 else 0.0
                )
                net_return_bps = (
                    float(total_net / total_notional * Decimal("10000"))
                    if valid_notional > 0 and fee_basis_complete else 0.0
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
                        "fee_basis_complete": fee_basis_complete,
                    })
                bind.execute(update_outcome, {
                    "id": int(final["id"]),
                    "closing_quantity": valid_quantity,
                    "closing_notional": valid_notional,
                    "realized_pnl": total_realized,
                    "fee": _fee_value_to_persist(
                        total_fee,
                        Decimal(str(final["fee"] or 0)) if final["fee"] is not None else None,
                        fee_basis_complete=fee_basis_complete,
                    ),
                    "net_pnl": total_net if fee_basis_complete else final["net_pnl"],
                    "return_bps": gross_return_bps,
                    "net_return_bps": (
                        net_return_bps if fee_basis_complete else final["net_return_bps"]
                    ),
                    "lifecycle_status": "CLOSED",
                    "fee_basis_complete": fee_basis_complete,
                })
    
    # Server defaults are used only to make existing rows valid while adding
    # NOT NULL fields; the ORM owns defaults after the migration completes.
    op.alter_column("strategy_outcomes", "closing_quantity", server_default=None)
    op.alter_column("strategy_outcomes", "closing_notional", server_default=None)
    op.alter_column("positions", "entry_fee", server_default=None)
    op.alter_column("positions", "entry_fee_basis_known", server_default=None)
    op.alter_column("strategy_outcomes", "fee_basis_complete", server_default=None)

    # Backfill remaining entry fees only where the original entry order and all
    # recorded close quantities exactly reconcile to the still-open position.
    position_query = sa.text("""
        SELECT id, entry_trade_id, quantity
        FROM positions
        WHERE id > :last_id
          AND entry_trade_id IS NOT NULL
          AND quantity <> 0
        ORDER BY id
        LIMIT :batch_size
    """)
    entry_trade_query = sa.text("""
        SELECT id, filled_quantity, fee
        FROM trades
        WHERE id IN :trade_ids
    """).bindparams(sa.bindparam("trade_ids", expanding=True))
    closed_quantity_query = sa.text("""
        SELECT COALESCE(SUM(closing_quantity), 0)
        FROM strategy_outcomes
        WHERE trade_id = :entry_trade_id
          AND closing_trade_id IS NOT NULL
          AND lifecycle_status = 'OPEN'
    """)
    update_position = sa.text("""
        UPDATE positions
        SET entry_fee = :entry_fee,
            entry_fee_basis_known = :known
        WHERE id = :id
    """)
    last_position_id = 0
    while True:
        position_rows = [
            dict(row)
            for row in bind.execute(
                position_query,
                {"last_id": last_position_id, "batch_size": _BATCH_SIZE},
            ).mappings().all()
        ]
        if not position_rows:
            break
        last_position_id = max(int(row["id"]) for row in position_rows)
        entry_ids = sorted({
            int(row["entry_trade_id"])
            for row in position_rows if row["entry_trade_id"] is not None
        })
        entry_trades = {
            int(row["id"]): dict(row)
            for row in bind.execute(
                entry_trade_query, {"trade_ids": entry_ids}
            ).mappings().all()
        }
        for position in position_rows:
            entry_id = int(position["entry_trade_id"])
            closed_quantity = Decimal(str(bind.execute(
                closed_quantity_query, {"entry_trade_id": entry_id}
            ).scalar_one() or 0))
            remaining_quantity = abs(Decimal(str(position["quantity"] or 0)))
            remaining_fee = _reconstruct_remaining_entry_fee(
                entry_trades.get(entry_id), remaining_quantity, closed_quantity
            )
            known = remaining_fee is not None
            bind.execute(update_position, {
                "id": int(position["id"]),
                "entry_fee": remaining_fee if known else Decimal("0"),
                "known": known,
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
    trade_index_names = {
        item["name"] for item in inspector.get_indexes("trades")
    }
    if "ix_trade_fee_reconciliation_pending" not in trade_index_names:
        op.create_index(
            "ix_trade_fee_reconciliation_pending",
            "trades",
            ["fee_reconciliation_pending", "updated_at"],
        )


def downgrade() -> None:
    raise RuntimeError(
        "Revision 0061 cannot be downgraded safely: it consolidates historical fill "
        "rows and adds fee/notional basis required for accurate risk evidence. Restore "
        "a pre-0061 database backup or deploy a forward corrective migration."
    )
