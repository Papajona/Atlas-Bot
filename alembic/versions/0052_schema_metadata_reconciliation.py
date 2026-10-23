"""Reconcile the live ORM contract with the established PostgreSQL migration graph.

This is a forward repair, not a rewrite of historical revisions. Timestamp values that
would be lost by a NOT NULL repair are never fabricated: if legacy rows violate the
required shape, the migration fails and names the affected columns for an explicit,
audited data repair first.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0052_schema_metadata_reconciliation"
down_revision = "0051_research_trial_archive"
branch_labels = None
depends_on = None


# Indexes whose historical names differ from the ORM-generated names. Renaming keeps
# the existing physical index and avoids a needless drop/rebuild during deployment.
_RENAME_INDEXES = [
    ("adaptive_trading_bots", "ix_adaptive_bot_account", "ix_adaptive_trading_bots_trading_account_id"),
    ("adaptive_trading_bots", "ix_adaptive_bot_strategy_candidate_id", "ix_adaptive_trading_bots_strategy_candidate_id"),
    ("custody_asset_observations", "ix_custody_asset_observation_customer", "ix_custody_asset_observations_customer_id"),
    ("model_experiments", "ix_model_experiment_model_path", "ix_model_experiments_model_path"),
    ("order_commands", "ix_order_command_customer_id", "ix_order_commands_customer_id"),
    ("research_runs", "ix_research_run_symbol", "ix_research_runs_symbol"),
    ("strategy_outcomes", "ix_strategy_outcome_closing_trade", "ix_strategy_outcomes_closing_trade_id"),
    ("strategy_outcomes", "ix_strategy_outcome_trade", "ix_strategy_outcomes_trade_id"),
    ("trade_executors", "ix_trade_executor_bot_id", "ix_trade_executors_bot_id"),
    ("trade_executors", "ix_trade_executor_trading_account_id", "ix_trade_executors_trading_account_id"),
    ("withdrawals", "ix_withdrawal_customer_id", "ix_withdrawals_customer_id"),
    ("withdrawals", "ix_withdrawal_wallet_id", "ix_withdrawals_wallet_id"),
]

# Single-column indexes removed from the ORM because an equivalent unique or composite
# index already has the same leading column. Keeping both increased write amplification.
_DROP_REDUNDANT_INDEXES = [
    ("customer_alerts", "ix_customer_alerts_customer"),
    ("positions", "ix_positions_customer_id"),
    ("withdrawal_step_up_tokens", "ix_withdrawal_step_up_tokens_auth_user_id"),
    ("customer_deriv_accounts", "ix_customer_deriv_account_customer_id"),
    ("customer_oanda_accounts", "ix_customer_oanda_account_customer_id"),
    ("customer_ledger_accounts", "ix_customer_ledger_account_customer"),
    ("dca_bots", "ix_dca_bots_customer"),
    ("grid_bots", "ix_grid_bot_customer"),
    ("order_commands", "ix_order_command_trade_id"),
    ("smart_trades", "ix_smart_trades_customer"),
    ("strategy_candidates", "ix_strategy_candidate_customer_id"),
    ("strategy_drafts", "ix_strategy_draft_customer"),
    ("trade_executors", "ix_trade_executor_customer_id"),
    ("trade_learning_episodes", "ix_trade_learning_episode_entry_trade"),
    ("trading_accounts", "ix_trading_account_customer_id"),
    ("tron_deposit_cursors", "ix_tron_deposit_cursor_wallet"),
    ("tron_sweeps", "ix_tron_sweep_wallet_id"),
    ("wallets", "ix_wallets_customer_id"),
]

# Metadata-required indexes not represented by the historical migration chain.
_CREATE_INDEXES = [
    ("adaptive_trading_bots", "ix_adaptive_trading_bots_strategy_candidate_id", ["strategy_candidate_id"]),
    ("arbitrage_opportunities", "ix_arbitrage_opportunities_trading_account_id", ["trading_account_id"]),
    ("dca_bots", "ix_dca_bots_trading_account_id", ["trading_account_id"]),
    ("grid_bots", "ix_grid_bots_trading_account_id", ["trading_account_id"]),
    ("order_commands", "ix_order_commands_customer_id", ["customer_id"]),
    ("positions", "ix_positions_trading_account_id", ["trading_account_id"]),
    ("referral_commissions", "ix_referral_commissions_referral_id", ["referral_id"]),
    ("referral_commissions", "ix_referral_commissions_referred_customer_id", ["referred_customer_id"]),
    ("referral_commissions", "ix_referral_commissions_subscription_id", ["subscription_id"]),
    ("referrals", "ix_referrals_referred_customer_id", ["referred_customer_id"]),
    ("referrals", "ix_referrals_referrer_customer_id", ["referrer_customer_id"]),
    ("revenue_ledger", "ix_revenue_ledger_customer_id", ["customer_id"]),
    ("revenue_ledger", "ix_revenue_ledger_subscription_id", ["subscription_id"]),
    ("smart_trades", "ix_smart_trades_trading_account_id", ["trading_account_id"]),
    ("strategy_outcomes", "ix_strategy_outcomes_asset", ["asset"]),
    ("strategy_outcomes", "ix_strategy_outcomes_bot_id", ["bot_id"]),
    ("strategy_outcomes", "ix_strategy_outcomes_regime", ["regime"]),
    ("strategy_outcomes", "ix_strategy_outcomes_strategy", ["strategy"]),
    ("trade_learning_episodes", "ix_trade_learning_episodes_asset", ["asset"]),
    ("trade_learning_episodes", "ix_trade_learning_episodes_bot_id", ["bot_id"]),
    ("trade_learning_episodes", "ix_trade_learning_episodes_closing_trade_id", ["closing_trade_id"]),
    ("trade_learning_episodes", "ix_trade_learning_episodes_exchange", ["exchange"]),
    ("trade_learning_episodes", "ix_trade_learning_episodes_replay_status", ["replay_status"]),
    ("trade_learning_episodes", "ix_trade_learning_episodes_regime", ["regime"]),
    ("trade_learning_episodes", "ix_trade_learning_episodes_strategy", ["strategy"]),
    ("trade_learning_episodes", "ix_trade_learning_episodes_symbol", ["symbol"]),
    ("trade_replay_results", "ix_trade_replay_results_episode_id", ["episode_id"]),
    ("trade_replay_results", "ix_trade_replay_results_strategy", ["strategy"]),
]

# These are mandatory in ORM metadata but were created nullable by historical revisions.
# Never use a fabricated timestamp to satisfy the new constraint.
_REQUIRED_NOT_NULL = [
    ("admin_roles", "created_at", sa.DateTime(timezone=True)),
    ("admin_roles", "updated_at", sa.DateTime(timezone=True)),
    ("app_state", "updated_at", sa.DateTime(timezone=True)),
    ("audit_log", "created_at", sa.DateTime(timezone=True)),
    ("custody_asset_observations", "created_at", sa.DateTime(timezone=True)),
    ("custody_asset_observations", "updated_at", sa.DateTime(timezone=True)),
    ("custody_transfers", "requested_at", sa.DateTime(timezone=True)),
    ("custody_transfers", "updated_at", sa.DateTime(timezone=True)),
    ("customer_ledger_accounts", "updated_at", sa.DateTime(timezone=True)),
    ("ledger_entries", "created_at", sa.DateTime(timezone=True)),
    ("ledger_journal_lines", "created_at", sa.DateTime(timezone=True)),
    ("ledger_journals", "created_at", sa.DateTime(timezone=True)),
    ("positions", "updated_at", sa.DateTime(timezone=True)),
    ("service_heartbeats", "updated_at", sa.DateTime(timezone=True)),
    ("trades", "created_at", sa.DateTime(timezone=True)),
    ("trades", "updated_at", sa.DateTime(timezone=True)),
    ("trading_accounts", "pilot_status", sa.String(20)),
    ("trading_accounts", "pilot_stage", sa.Integer()),
    ("trading_accounts", "pilot_requested_by", sa.String(160)),
    ("trading_accounts", "pilot_approved_by", sa.String(160)),
    ("trading_accounts", "pilot_max_position_notional_usd", sa.Numeric(38, 18)),
    ("trading_accounts", "pilot_max_total_exposure_usd", sa.Numeric(38, 18)),
    ("trading_accounts", "pilot_max_open_positions", sa.Integer()),
    ("trading_accounts", "pilot_max_leverage", sa.Float()),
    ("trading_accounts", "pilot_daily_loss_limit", sa.Float()),
    ("trading_accounts", "updated_at", sa.DateTime(timezone=True)),
    ("tron_deposit_cursors", "updated_at", sa.DateTime(timezone=True)),
    ("tron_sweeps", "created_at", sa.DateTime(timezone=True)),
    ("withdrawal_step_up_tokens", "created_at", sa.DateTime(timezone=True)),
    ("withdrawals", "updated_at", sa.DateTime(timezone=True)),
]

# Legacy single-column constraints are superseded by tenant-scoped composite constraints.
_DROP_REDUNDANT_FKS = [
    ("dca_bots", "fk_dca_bots_account"),
    ("dca_bots", "fk_dca_bots_customer"),
    ("funding_transactions", "fk_funding_transaction_wallet"),
    ("funding_transactions", "fk_funding_transaction_customer"),
    ("grid_bots", "fk_grid_bots_customer"),
    ("grid_bots", "fk_grid_bots_account"),
    ("order_commands", "order_commands_customer_id_fkey"),
    ("smart_trades", "fk_smart_trades_customer"),
    ("smart_trades", "fk_smart_trades_account"),
    ("trade_executors", "fk_trade_executors_customer"),
    ("trade_executors", "fk_trade_executors_account"),
    ("withdrawals", "fk_withdrawals_customer_id"),
    ("withdrawals", "fk_withdrawals_wallet_id"),
]

_REQUIRED_COMPOSITE_FKS = [
    ("dca_bots", "fk_dca_bot_account_customer"),
    ("funding_transactions", "fk_funding_wallet_customer"),
    ("grid_bots", "fk_grid_bot_account_customer"),
    ("order_commands", "fk_order_command_trade_customer"),
    ("smart_trades", "fk_smart_trade_account_customer"),
    ("trade_executors", "fk_executor_account_customer"),
    ("withdrawals", "fk_withdrawal_wallet_customer"),
]

# Recreated when downgrading this forward-only schema cleanup.
_RESTORE_LEGACY_FKS = [
    ("dca_bots", "fk_dca_bots_account", ["trading_account_id"], "trading_accounts", ["id"]),
    ("dca_bots", "fk_dca_bots_customer", ["customer_id"], "customer_profiles", ["id"]),
    ("funding_transactions", "fk_funding_transaction_wallet", ["wallet_id"], "wallets", ["id"]),
    ("funding_transactions", "fk_funding_transaction_customer", ["customer_id"], "customer_profiles", ["id"]),
    ("grid_bots", "fk_grid_bots_customer", ["customer_id"], "customer_profiles", ["id"]),
    ("grid_bots", "fk_grid_bots_account", ["trading_account_id"], "trading_accounts", ["id"]),
    ("order_commands", "order_commands_customer_id_fkey", ["customer_id"], "customer_profiles", ["id"]),
    ("smart_trades", "fk_smart_trades_customer", ["customer_id"], "customer_profiles", ["id"]),
    ("smart_trades", "fk_smart_trades_account", ["trading_account_id"], "trading_accounts", ["id"]),
    ("trade_executors", "fk_trade_executors_customer", ["customer_id"], "customer_profiles", ["id"]),
    ("trade_executors", "fk_trade_executors_account", ["trading_account_id"], "trading_accounts", ["id"]),
    ("withdrawals", "fk_withdrawals_customer_id", ["customer_id"], "customer_profiles", ["id"]),
    ("withdrawals", "fk_withdrawals_wallet_id", ["wallet_id"], "wallets", ["id"]),
]


def _inspector(bind):
    return sa.inspect(bind)


_DEFAULT_SCHEMA = "public"


def _index_names(bind, table: str, schema: str = _DEFAULT_SCHEMA) -> set[str]:
    return {
        item.get("name")
        for item in _inspector(bind).get_indexes(table, schema=schema)
        if item.get("name")
    }


def _foreign_key_names(bind, table: str, schema: str = _DEFAULT_SCHEMA) -> set[str]:
    return {
        item.get("name")
        for item in _inspector(bind).get_foreign_keys(table, schema=schema)
        if item.get("name")
    }


def _rename_index(bind, table: str, old: str, new: str, schema: str = _DEFAULT_SCHEMA) -> None:
    names = _index_names(bind, table, schema)
    if old in names and new not in names:
        op.execute(sa.text(f'ALTER INDEX "{schema}"."{old}" RENAME TO "{new}"'))
    elif old in names and new in names:
        op.drop_index(old, table_name=table, schema=schema)


def _drop_index(bind, table: str, name: str, schema: str = _DEFAULT_SCHEMA) -> None:
    if name in _index_names(bind, table, schema):
        op.drop_index(name, table_name=table, schema=schema)


def _create_index(bind, table: str, name: str, columns: list[str], schema: str = _DEFAULT_SCHEMA) -> None:
    if name not in _index_names(bind, table, schema):
        op.create_index(name, table, columns, unique=False, schema=schema)


# Downgrade must know which canonical-name indexes were present before upgrade.
# Alembic may execute upgrade and downgrade in different processes, so this provenance
# is stored in a private, migration-owned schema instead of module-level Python state.
_INDEX_STATE_SCHEMA = "atlas_migration_meta"
_INDEX_STATE_TABLE = "schema_metadata_reconciliation_0052_index_state"


def _visible_index_definition(
    bind, table: str, name: str, schema: str = _DEFAULT_SCHEMA
):
    # Identify the index through its parent relation OID, not search-path visibility.
    # Two schemas may legally contain indexes with identical names.
    row = bind.execute(sa.text("""
        SELECT pg_get_indexdef(idx.oid) AS definition,
               obj_description(idx.oid, 'pg_class') AS comment
        FROM pg_class AS idx
        JOIN pg_index AS ix ON ix.indexrelid = idx.oid
        JOIN pg_class AS tbl ON tbl.oid = ix.indrelid
        JOIN pg_namespace AS ns ON ns.oid = tbl.relnamespace
        WHERE idx.relkind IN ('i', 'I')
          AND idx.relname = :index_name
          AND tbl.relname = :table_name
          AND ns.nspname = :schema_name
    """), {
        "index_name": name,
        "table_name": table,
        "schema_name": schema,
    }).mappings().first()
    return dict(row) if row is not None else None


def _assert_provenance_schema_available(bind) -> None:
    # Query pg_namespace directly: to_regnamespace('name') can return NULL when
    # the existing schema is outside search_path, which is not an ownership check.
    schema_exists = bool(bind.execute(sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_namespace WHERE nspname = :schema_name
        )
    """), {"schema_name": _INDEX_STATE_SCHEMA}).scalar_one())
    if schema_exists:
        raise RuntimeError(
            "Schema reconciliation blocked: reserved migration schema atlas_migration_meta already exists; "
            "refusing to alter its permissions or reuse unverified objects."
        )


def _capture_rename_index_state(bind) -> None:
    # Recheck immediately before creating provenance in case the schema changed after
    # upgrade() performed its read-only preflight.
    _assert_provenance_schema_available(bind)
    schema_created = True
    existing_table = bool(bind.execute(sa.text("""
        SELECT EXISTS (
            SELECT 1
            FROM pg_class AS c
            JOIN pg_namespace AS ns ON ns.oid = c.relnamespace
            WHERE ns.nspname = :schema_name AND c.relname = :table_name
        )
    """), {
        "schema_name": _INDEX_STATE_SCHEMA,
        "table_name": _INDEX_STATE_TABLE,
    }).scalar_one())
    if existing_table:
        raise RuntimeError(
            "Schema reconciliation blocked: migration 0052 index provenance table already exists; "
            "refusing to overwrite potentially stale rollback state."
        )
    if schema_created:
        op.execute(sa.text(f'CREATE SCHEMA "{_INDEX_STATE_SCHEMA}"'))
    op.execute(sa.text(f"""
        CREATE TABLE "{_INDEX_STATE_SCHEMA}"."{_INDEX_STATE_TABLE}" (
            table_name text NOT NULL,
            old_name text NOT NULL,
            new_name text NOT NULL,
            old_existed boolean NOT NULL,
            new_existed boolean NOT NULL,
            old_definition text,
            old_comment text,
            new_definition text,
            new_comment text,
            schema_created boolean NOT NULL,
            PRIMARY KEY (table_name, old_name, new_name)
        )
    """))
    op.execute(sa.text(f'REVOKE ALL ON SCHEMA "{_INDEX_STATE_SCHEMA}" FROM PUBLIC'))
    op.execute(sa.text(
        f'REVOKE ALL ON TABLE "{_INDEX_STATE_SCHEMA}"."{_INDEX_STATE_TABLE}" FROM PUBLIC'
    ))

    for table, old, new in _RENAME_INDEXES:
        names = _index_names(bind, table)
        old_existed = old in names
        new_existed = new in names
        old_info = _visible_index_definition(bind, table, old) if old_existed else None
        new_info = _visible_index_definition(bind, table, new) if new_existed else None
        if (old_existed and old_info is None) or (new_existed and new_info is None):
            raise RuntimeError(
                f"Schema reconciliation blocked: unable to capture exact index definition for {table}.{old}/{new}."
            )
        bind.execute(sa.text(f"""
            INSERT INTO "{_INDEX_STATE_SCHEMA}"."{_INDEX_STATE_TABLE}" (
                table_name, old_name, new_name, old_existed, new_existed,
                old_definition, old_comment, new_definition, new_comment, schema_created
            ) VALUES (
                :table_name, :old_name, :new_name, :old_existed, :new_existed,
                :old_definition, :old_comment, :new_definition, :new_comment, :schema_created
            )
        """), {
            "table_name": table,
            "old_name": old,
            "new_name": new,
            "old_existed": old_existed,
            "new_existed": new_existed,
            "old_definition": old_info.get("definition") if old_info else None,
            "old_comment": old_info.get("comment") if old_info else None,
            "new_definition": new_info.get("definition") if new_info else None,
            "new_comment": new_info.get("comment") if new_info else None,
            "schema_created": schema_created,
        })


def _restore_index_definition(
    bind, table: str, name: str, definition: str | None, comment: str | None,
    schema: str = _DEFAULT_SCHEMA,
) -> None:
    if not definition:
        raise RuntimeError(
            f"Schema reconciliation blocked: rollback lacks the original definition for index {schema}.{name}."
        )
    # pg_get_indexdef was captured for this exact parent table and schema.
    op.execute(sa.text(definition))
    if comment is not None:
        escaped_schema = schema.replace('"', '""')
        escaped_name = name.replace('"', '""')
        escaped_comment = comment.replace("'", "''")
        bind.exec_driver_sql(
            f'''COMMENT ON INDEX "{escaped_schema}"."{escaped_name}" IS '{escaped_comment}' '''
        )

def _restore_rename_index_state(bind, state) -> None:
    """Restore one rename pair to the exact existence state recorded before upgrade."""
    table = str(state["table_name"])
    old = str(state["old_name"])
    new = str(state["new_name"])
    old_before = bool(state["old_existed"])
    new_before = bool(state["new_existed"])
    names = _index_names(bind, table, _DEFAULT_SCHEMA)
    old_now = old in names
    new_now = new in names

    if old_before and new_before:
        # Upgrade drops the legacy name when both names existed; recreate that index
        # from its captured definition while preserving the pre-existing canonical one.
        if not old_now:
            _restore_index_definition(
                bind, table, old, state.get("old_definition"), state.get("old_comment")
            )
        if not new_now:
            _restore_index_definition(
                bind, table, new, state.get("new_definition"), state.get("new_comment")
            )
    elif old_before:
        if old_now and new_now:
            raise RuntimeError(
                f"Schema reconciliation blocked: unexpected duplicate rollback indexes {table}.{old}/{new}."
            )
        if new_now:
            op.execute(sa.text(f'ALTER INDEX "{_DEFAULT_SCHEMA}"."{new}" RENAME TO "{old}"'))
        elif not old_now:
            _restore_index_definition(
                bind, old, state.get("old_definition"), state.get("old_comment")
            )
    elif new_before:
        # A canonical-name index that existed before upgrade must remain canonical;
        # it was not a renamed legacy index.
        if old_now:
            raise RuntimeError(
                f"Schema reconciliation blocked: unexpected legacy index {table}.{old} appeared during downgrade."
            )
        if not new_now:
            _restore_index_definition(
                bind, new, state.get("new_definition"), state.get("new_comment")
            )
    else:
        # Neither name existed before upgrade. Remove either name if the upgrade created it.
        _drop_index(bind, table, new, _DEFAULT_SCHEMA)
        _drop_index(bind, table, old, _DEFAULT_SCHEMA)


def _assert_no_nulls(bind, columns) -> None:
    issues = []
    for table, column, _column_type in columns:
        count = bind.execute(sa.text(f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NULL')).scalar_one()
        if count:
            issues.append(f"{table}.{column}={count}")
    if issues:
        raise RuntimeError(
            "Schema reconciliation blocked: refusing to invent/overwrite historical values for "
            "columns that must become NOT NULL. Repair these rows from an authoritative source "
            "with an audited data migration first: " + ", ".join(issues)
        )


def _assert_no_orphans(bind, table: str, column: str, parent_table: str, parent_column: str = "id") -> None:
    count = bind.execute(sa.text(
        f'SELECT COUNT(*) FROM "{table}" child '
        f'WHERE child."{column}" IS NOT NULL AND NOT EXISTS '
        f'(SELECT 1 FROM "{parent_table}" parent WHERE parent."{parent_column}" = child."{column}")'
    )).scalar_one()
    if count:
        raise RuntimeError(
            f"Schema reconciliation blocked: {table}.{column} has {count} orphan rows referencing "
            f"{parent_table}.{parent_column}; repair them from authoritative data before adding the FK."
        )


def _ensure_fk(bind, table: str, name: str, local_cols, parent_table: str, parent_cols) -> None:
    if name not in _foreign_key_names(bind, table):
        op.create_foreign_key(
            name, table, parent_table, local_cols, parent_cols,
            postgresql_not_valid=True,
        )
    op.execute(sa.text(f'ALTER TABLE "{table}" VALIDATE CONSTRAINT "{name}"'))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # The production schema contract is PostgreSQL. SQLite unit tests use create_all
        # for ORM metadata and must not pretend to validate the production migration graph.
        return

    # Refuse a reserved-schema collision before any UPDATE, DDL, constraint, or FK work.
    _assert_provenance_schema_available(bind)

    # The 0044 migration intended all pilot controls to have defaults but updated other fields
    # only when pilot_status was NULL. Normalize each default-bearing field independently.
    bind.execute(sa.text("""
        UPDATE trading_accounts SET
            pilot_status = COALESCE(pilot_status, 'NONE'),
            pilot_stage = COALESCE(pilot_stage, 0),
            pilot_requested_by = COALESCE(pilot_requested_by, ''),
            pilot_approved_by = COALESCE(pilot_approved_by, ''),
            pilot_max_position_notional_usd = COALESCE(pilot_max_position_notional_usd, 0),
            pilot_max_total_exposure_usd = COALESCE(pilot_max_total_exposure_usd, 0),
            pilot_max_open_positions = COALESCE(pilot_max_open_positions, 0),
            pilot_max_leverage = COALESCE(pilot_max_leverage, 0),
            pilot_daily_loss_limit = COALESCE(pilot_daily_loss_limit, 0)
        WHERE pilot_status IS NULL
           OR pilot_stage IS NULL
           OR pilot_requested_by IS NULL
           OR pilot_approved_by IS NULL
           OR pilot_max_position_notional_usd IS NULL
           OR pilot_max_total_exposure_usd IS NULL
           OR pilot_max_open_positions IS NULL
           OR pilot_max_leverage IS NULL
           OR pilot_daily_loss_limit IS NULL
    """))

    # Numeric(38,18) permits 20 integer digits. Refuse a potentially lossy precision change.
    for column in ("pilot_max_position_notional_usd", "pilot_max_total_exposure_usd"):
        too_large = bind.execute(sa.text(
            f'SELECT COUNT(*) FROM trading_accounts WHERE ABS("{column}") >= 100000000000000000000'
        )).scalar_one()
        if too_large:
            raise RuntimeError(
                f"Schema reconciliation blocked: trading_accounts.{column} contains {too_large} "
                "value(s) outside the integer range of NUMERIC(38,18)."
            )

    # All these fields are required by the ORM and used as durable lifecycle/audit state.
    # If any historical NULL is found, stop rather than assigning a false timestamp.
    _assert_no_nulls(bind, _REQUIRED_NOT_NULL)

    # Check the new customer FK before DDL so orphan rows are reported clearly.
    _assert_no_orphans(bind, "trades", "customer_id", "customer_profiles", "id")

    # Composite constraints must be present before dropping their legacy single-column peers.
    for table, name in _REQUIRED_COMPOSITE_FKS:
        if name not in _foreign_key_names(bind, table):
            raise RuntimeError(
                f"Schema reconciliation blocked: required tenant-scoped constraint {table}.{name} is missing."
            )

    # Fix numeric scale before asserting pilot fields are non-null.
    for column in ("pilot_max_position_notional_usd", "pilot_max_total_exposure_usd"):
        op.alter_column(
            "trading_accounts", column,
            existing_type=sa.Numeric(38, 6),
            type_=sa.Numeric(38, 18),
            existing_nullable=True,
        )

    # Tighten nullable schema to match mandatory ORM types.
    for table, column, column_type in _REQUIRED_NOT_NULL:
        op.alter_column(
            table, column,
            existing_type=column_type,
            nullable=False,
        )

    # Ensure the FK already present in the ORM is also present in the physical schema.
    _ensure_fk(bind, "trades", "trades_customer_id_fkey", ["customer_id"], "customer_profiles", ["id"])

    # Drop only redundant legacy one-column FKs after confirming tenant-scoped composite
    # replacements exist. The composite constraints prevent cross-customer account/wallet use.
    for table, name in _DROP_REDUNDANT_FKS:
        if name in _foreign_key_names(bind, table):
            op.drop_constraint(name, table_name=table, type_="foreignkey")

    # Save durable index provenance before any rename/create operations. The marker is
    # removed by downgrade; it is private and inaccessible to application roles.
    _capture_rename_index_state(bind)

    # Rename established indexes where the columns match. This avoids a build/drop cycle.
    for table, old, new in _RENAME_INDEXES:
        _rename_index(bind, table, old, new)

    # Remove only stand-alone indexes now covered by a unique/composite index or by the
    # customer-scoped composite index already retained in the ORM.
    for table, name in _DROP_REDUNDANT_INDEXES:
        _drop_index(bind, table, name)

    for table, name, columns in _CREATE_INDEXES:
        _create_index(bind, table, name, columns)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    qualified_state_table = f"{_INDEX_STATE_SCHEMA}.{_INDEX_STATE_TABLE}"
    if not bind.execute(
        sa.text("SELECT to_regclass(:qualified_name) IS NOT NULL"),
        {"qualified_name": qualified_state_table},
    ).scalar_one():
        raise RuntimeError(
            "Refusing schema-reconciliation downgrade: migration 0052 has no recorded index "
            "provenance, so the pre-upgrade index state cannot be restored safely."
        )
    index_state = bind.execute(sa.text(f"""
        SELECT table_name, old_name, new_name, old_existed, new_existed,
               old_definition, old_comment, new_definition, new_comment, schema_created
        FROM "{_INDEX_STATE_SCHEMA}"."{_INDEX_STATE_TABLE}"
        ORDER BY table_name, old_name, new_name
    """)).mappings().all()
    if len(index_state) != len(_RENAME_INDEXES):
        raise RuntimeError(
            "Refusing schema-reconciliation downgrade: recorded index provenance is incomplete."
        )
    state_schema_created = bool(index_state[0]["schema_created"])

    # Do not silently truncate any 18-scale values written after this migration.
    for column in ("pilot_max_position_notional_usd", "pilot_max_total_exposure_usd"):
        not_representable = bind.execute(sa.text(
            f'SELECT COUNT(*) FROM trading_accounts WHERE "{column}" <> ROUND("{column}", 6)'
        )).scalar_one()
        if not_representable:
            raise RuntimeError(
                f"Refusing lossy downgrade: trading_accounts.{column} has values with precision "
                "beyond six decimal places."
            )

    # Remove the FK added by this revision.
    names = _foreign_key_names(bind, "trades")
    if "trades_customer_id_fkey" in names:
        op.drop_constraint("trades_customer_id_fkey", table_name="trades", type_="foreignkey")

    # Restore legacy foreign keys for the historical revisions' downgrade paths.
    for table, name, local_cols, parent_table, parent_cols in _RESTORE_LEGACY_FKS:
        if name not in _foreign_key_names(bind, table):
            op.create_foreign_key(name, table, parent_table, local_cols, parent_cols)
            op.execute(sa.text(f'ALTER TABLE "{table}" VALIDATE CONSTRAINT "{name}"'))

    # Drop ordinary indexes created by this revision. Rename-pair targets are handled
    # from captured provenance below so a target created from an initially absent pair
    # is dropped, while a genuinely renamed legacy index is restored under its old name.
    rename_pair_targets = {(table, name) for table, _old, name in _RENAME_INDEXES}
    for table, name, _columns in reversed(_CREATE_INDEXES):
        if (table, name) not in rename_pair_targets:
            _drop_index(bind, table, name)

    for state in reversed(index_state):
        _restore_rename_index_state(bind, state)

    # Recreate stand-alone indexes dropped because a unique/composite index already covered them.
    restore_indexes = [
        ("customer_alerts", "ix_customer_alerts_customer", ["customer_id"]),
        # These indexes were removed in upgrade but are expected by the older
        # migrations' downgrade functions, so restore them before reversing 0012/0009.
        ("positions", "ix_positions_customer_id", ["customer_id"]),
        ("withdrawal_step_up_tokens", "ix_withdrawal_step_up_tokens_auth_user_id", ["auth_user_id"]),
        ("customer_deriv_accounts", "ix_customer_deriv_account_customer_id", ["customer_id"]),
        ("customer_oanda_accounts", "ix_customer_oanda_account_customer_id", ["customer_id"]),
        ("customer_ledger_accounts", "ix_customer_ledger_account_customer", ["customer_id"]),
        ("dca_bots", "ix_dca_bots_customer", ["customer_id"]),
        ("grid_bots", "ix_grid_bot_customer", ["customer_id"]),
        ("order_commands", "ix_order_command_trade_id", ["trade_id"]),
        ("smart_trades", "ix_smart_trades_customer", ["customer_id"]),
        ("strategy_candidates", "ix_strategy_candidate_customer_id", ["customer_id"]),
        ("strategy_drafts", "ix_strategy_draft_customer", ["customer_id"]),
        ("trade_executors", "ix_trade_executor_customer_id", ["customer_id"]),
        ("trade_learning_episodes", "ix_trade_learning_episode_entry_trade", ["entry_trade_id"]),
        ("trading_accounts", "ix_trading_account_customer_id", ["customer_id"]),
        ("tron_deposit_cursors", "ix_tron_deposit_cursor_wallet", ["wallet_id"]),
        ("tron_sweeps", "ix_tron_sweep_wallet_id", ["wallet_id"]),
        ("wallets", "ix_wallets_customer_id", ["customer_id"]),
    ]
    for table, name, columns in restore_indexes:
        _create_index(bind, table, name, columns)

    op.execute(sa.text(
        f'DROP TABLE "{_INDEX_STATE_SCHEMA}"."{_INDEX_STATE_TABLE}"'
    ))
    if state_schema_created:
        op.execute(sa.text(f'DROP SCHEMA "{_INDEX_STATE_SCHEMA}"'))

    # Restore the historical monetary precision after the explicit losslessness guard above.
    for column in ("pilot_max_position_notional_usd", "pilot_max_total_exposure_usd"):
        op.alter_column(
            "trading_accounts", column,
            existing_type=sa.Numeric(38, 18),
            type_=sa.Numeric(38, 6),
            existing_nullable=False,
        )

    for table, column, column_type in reversed(_REQUIRED_NOT_NULL):
        op.alter_column(
            table, column,
            existing_type=column_type,
            nullable=True,
        )
