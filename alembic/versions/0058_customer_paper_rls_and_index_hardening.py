"""Capture the customer-paper database hardening in Alembic history.

Revision ID: 0058_customer_paper_rls_and_index_hardening
Revises: 0057_customer_paper_submission_audit_retries

The migration is deliberately forward-only for security controls. It pins the
customer-paper trigger functions, makes tenant-setting reads planner-safe,
removes only a proven redundant standalone unique index, and covers the known
foreign-key lookup paths. Existing live databases can run it idempotently.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0058_customer_paper_rls_and_index_hardening"
down_revision = "0057_customer_paper_submission_audit_retries"
branch_labels = None
depends_on = None


_CUSTOMER_PAPER_FUNCTIONS = (
    "customer_paper.reject_mutation()",
    "customer_paper.assert_market_bar_capacity()",
    "customer_paper.assert_balanced_journal()",
    "customer_paper.assert_order_event_state()",
)

_CUSTOMER_PAPER_TENANT_TABLES = (
    "accounts",
    "journals",
    "journal_lines",
    "orders",
    "order_events",
    "fills",
    "position_events",
)

# (schema, table, index, ordered child columns)
_FOREIGN_KEY_INDEXES = (
    ("customer_paper", "fills", "ix_customer_paper_fills_order_customer_fk",
     ("order_id", "customer_id")),
    ("customer_paper", "journal_lines", "ix_customer_paper_journal_lines_account_customer_currency_fk",
     ("account_id", "customer_id", "currency")),
    ("customer_paper", "journal_lines", "ix_customer_paper_journal_lines_journal_customer_fk",
     ("journal_id", "customer_id")),
    ("customer_paper", "order_events", "ix_customer_paper_order_events_order_customer_fk",
     ("order_id", "customer_id")),
    ("customer_paper", "position_events", "ix_customer_paper_position_events_fill_customer_fk",
     ("fill_id", "customer_id")),
    ("customer_paper", "position_events", "ix_customer_paper_position_events_reversal_customer_fk",
     ("reversal_of_fill_id", "customer_id")),
    ("public", "adaptive_trading_bots", "ix_adaptive_bot_account_customer_fk",
     ("trading_account_id", "customer_id")),
    ("public", "dca_bots", "ix_dca_bot_account_customer_fk",
     ("trading_account_id", "customer_id")),
    ("public", "funding_transactions", "ix_funding_transaction_wallet_customer_fk",
     ("wallet_id", "customer_id")),
    ("public", "grid_bots", "ix_grid_bot_account_customer_fk",
     ("trading_account_id", "customer_id")),
    ("public", "order_commands", "ix_order_command_trade_customer_fk",
     ("trade_id", "customer_id")),
    ("public", "positions", "ix_position_account_customer_fk",
     ("trading_account_id", "customer_id")),
    ("public", "positions", "ix_position_entry_trade_customer_fk",
     ("entry_trade_id", "customer_id")),
    ("public", "smart_trades", "ix_smart_trade_account_customer_fk",
     ("trading_account_id", "customer_id")),
    ("public", "trade_executors", "ix_trade_executor_account_customer_fk",
     ("trading_account_id", "customer_id")),
    ("public", "trading_accounts", "ix_trade_account_customer_fk",
     ("customer_id",)),
    ("public", "withdrawals", "ix_withdrawal_wallet_customer_fk",
     ("wallet_id", "customer_id")),
)


def _pin_trigger_function_paths(bind) -> None:
    for signature in _CUSTOMER_PAPER_FUNCTIONS:
        exists = bind.execute(
            sa.text("SELECT to_regprocedure(:signature) IS NOT NULL"),
            {"signature": signature},
        ).scalar_one()
        if exists:
            # The function bodies use schema-qualified application objects. Keeping
            # only pg_catalog on the path prevents objects in writable schemas from
            # shadowing built-ins during trigger execution.
            op.execute(sa.text(
                f"ALTER FUNCTION {signature} SET search_path = pg_catalog"
            ))


def _harden_customer_paper_tenant_policies(bind) -> None:
    expression = (
        "customer_id = "
        "(NULLIF((SELECT current_setting('app.customer_id', true)), '')::bigint)"
    )
    schema_exists = bind.execute(
        sa.text("SELECT to_regnamespace('customer_paper') IS NOT NULL")
    ).scalar_one()
    if not schema_exists:
        return

    for table in _CUSTOMER_PAPER_TENANT_TABLES:
        policy_exists = bind.execute(
            sa.text("""
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_policy p
                    JOIN pg_class c ON c.oid = p.polrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'customer_paper'
                      AND c.relname = :table_name
                      AND p.polname = 'customer_paper_tenant'
                )
            """),
            {"table_name": table},
        ).scalar_one()
        if policy_exists:
            op.execute(sa.text(
                f'ALTER POLICY customer_paper_tenant '
                f'ON customer_paper."{table}" '
                f'USING ({expression}) WITH CHECK ({expression})'
            ))


def _remove_only_proven_duplicate_unique_index(bind) -> None:
    # Never remove a constraint-owned index. The duplicate must be an ordinary
    # index, and a semantically identical unique index owned by a constraint on
    # the same table must exist before the redundant copy is dropped.
    op.execute(sa.text("""
        DO $atlas_customer_paper_duplicate_index$
        DECLARE
            duplicate_oid oid;
            duplicate_table oid;
            replacement_oid oid;
        BEGIN
            SELECT idx.oid, ix.indrelid
              INTO duplicate_oid, duplicate_table
            FROM pg_class idx
            JOIN pg_namespace ns ON ns.oid = idx.relnamespace
            JOIN pg_index ix ON ix.indexrelid = idx.oid
            WHERE ns.nspname = 'customer_paper'
              AND idx.relname = 'uq_customer_paper_account_code';

            IF duplicate_oid IS NULL THEN
                RETURN;
            END IF;

            IF EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conindid = duplicate_oid
            ) THEN
                RAISE EXCEPTION
                    'Refusing to drop customer_paper.uq_customer_paper_account_code: it is constraint-owned';
            END IF;

            SELECT candidate.indexrelid
              INTO replacement_oid
            FROM pg_index duplicate
            JOIN pg_index candidate
              ON candidate.indrelid = duplicate.indrelid
             AND candidate.indisunique
             AND candidate.indisvalid
             AND candidate.indisready
             AND candidate.indnkeyatts = duplicate.indnkeyatts
             AND candidate.indnatts = duplicate.indnatts
             AND candidate.indkey = duplicate.indkey
             AND candidate.indclass = duplicate.indclass
             AND candidate.indcollation = duplicate.indcollation
             AND candidate.indoption = duplicate.indoption
             AND candidate.indexprs IS NOT DISTINCT FROM duplicate.indexprs
             AND candidate.indpred IS NOT DISTINCT FROM duplicate.indpred
            WHERE duplicate.indexrelid = duplicate_oid
              AND candidate.indexrelid <> duplicate_oid
              AND EXISTS (
                  SELECT 1 FROM pg_constraint c
                  WHERE c.conindid = candidate.indexrelid
              )
            LIMIT 1;

            IF replacement_oid IS NULL THEN
                RAISE EXCEPTION
                    'Refusing to drop customer_paper.uq_customer_paper_account_code: no equivalent constraint-owned unique index exists';
            END IF;

            EXECUTE 'DROP INDEX customer_paper.uq_customer_paper_account_code';
        END
        $atlas_customer_paper_duplicate_index$;
    """))


def _create_and_verify_fk_index(bind, schema: str, table: str,
                                index_name: str, columns: tuple[str, ...]) -> None:
    relation_exists = bind.execute(
        sa.text("SELECT to_regclass(:relation_name) IS NOT NULL"),
        {"relation_name": f"{schema}.{table}"},
    ).scalar_one()
    if not relation_exists:
        # The customer_paper schema is installed by its own migration stream.
        # It is optional in local/minimal deployments; public ORM tables are not.
        if schema == "customer_paper":
            return
        raise RuntimeError(
            f"0058 expected {schema}.{table}; preceding Alembic revisions are incomplete"
        )

    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    op.execute(sa.text(
        f'CREATE INDEX IF NOT EXISTS "{index_name}" '
        f'ON "{schema}"."{table}" ({quoted_columns})'
    ))

    row = bind.execute(sa.text("""
        SELECT parent.relname AS table_name,
               ix.indisunique,
               ix.indisvalid,
               ix.indisready,
               ix.indpred IS NULL AS has_no_predicate,
               ix.indexprs IS NULL AS has_no_expressions,
               ARRAY(
                   SELECT att.attname
                   FROM unnest(ix.indkey) WITH ORDINALITY
                       AS key(attnum, ordinality)
                   JOIN pg_attribute att
                     ON att.attrelid = parent.oid AND att.attnum = key.attnum
                   ORDER BY key.ordinality
               ) AS column_names
        FROM pg_class idx
        JOIN pg_namespace ns ON ns.oid = idx.relnamespace
        JOIN pg_index ix ON ix.indexrelid = idx.oid
        JOIN pg_class parent ON parent.oid = ix.indrelid
        WHERE ns.nspname = :schema_name
          AND idx.relname = :index_name
    """), {
        "schema_name": schema,
        "index_name": index_name,
    }).mappings().one_or_none()

    if row is None:
        raise RuntimeError(f"0058 failed to create {schema}.{index_name}")
    if (
        row["table_name"] != table
        or row["indisunique"]
        or not row["indisvalid"]
        or not row["indisready"]
        or not row["has_no_predicate"]
        or not row["has_no_expressions"]
        or tuple(row["column_names"]) != columns
    ):
        raise RuntimeError(
            f"0058 found an incompatible pre-existing index {schema}.{index_name}; "
            "manual review is required"
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    _pin_trigger_function_paths(bind)
    _harden_customer_paper_tenant_policies(bind)
    _remove_only_proven_duplicate_unique_index(bind)

    for schema, table, index_name, columns in _FOREIGN_KEY_INDEXES:
        _create_and_verify_fk_index(bind, schema, table, index_name, columns)


def downgrade() -> None:
    # This revision hardens function resolution and tenant-policy planning, and
    # removes only an index proven redundant with a constraint-owned equivalent.
    # Reverting would weaken security or recreate write amplification, so the
    # hardening is intentionally retained on downgrade.
    pass
