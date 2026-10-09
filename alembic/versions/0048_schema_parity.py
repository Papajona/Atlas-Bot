"""Reconcile migrated PostgreSQL schema with ORM metadata without dropping established constraints.

The migration fails before DDL if existing data violates the intended NOT NULL,
numeric precision, or customer-reference requirements. Existing index names are
renamed in place; only indexes absent from the migrated schema are created.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0048_schema_parity"
down_revision = "0047_strategy_outcome_lifecycle"
branch_labels = None
depends_on = None

INDEX_RENAMES = [
    ("adaptive_trading_bots", "ix_adaptive_bot_account", "ix_adaptive_trading_bots_trading_account_id", ["trading_account_id"]),
    ("adaptive_trading_bots", "ix_adaptive_bot_strategy_candidate_id", "ix_adaptive_trading_bots_strategy_candidate_id", ["strategy_candidate_id"]),
    ("custody_asset_observations", "ix_custody_asset_observation_customer", "ix_custody_asset_observations_customer_id", ["customer_id"]),
    ("customer_alerts", "ix_customer_alerts_customer", "ix_customer_alerts_customer_id", ["customer_id"]),
    ("customer_deriv_accounts", "ix_customer_deriv_account_customer_id", "ix_customer_deriv_accounts_customer_id", ["customer_id"]),
    ("customer_oanda_accounts", "ix_customer_oanda_account_customer_id", "ix_customer_oanda_accounts_customer_id", ["customer_id"]),
    ("dca_bots", "ix_dca_bots_customer", "ix_dca_bots_customer_id", ["customer_id"]),
    ("grid_bots", "ix_grid_bot_customer", "ix_grid_bots_customer_id", ["customer_id"]),
    ("model_experiments", "ix_model_experiment_model_path", "ix_model_experiments_model_path", ["model_path"]),
    ("order_commands", "ix_order_command_customer_id", "ix_order_commands_customer_id", ["customer_id"]),
    ("order_commands", "ix_order_command_trade_id", "ix_order_commands_trade_id", ["trade_id"]),
    ("research_runs", "ix_research_run_symbol", "ix_research_runs_symbol", ["symbol"]),
    ("smart_trades", "ix_smart_trades_customer", "ix_smart_trades_customer_id", ["customer_id"]),
    ("strategy_candidates", "ix_strategy_candidate_customer_id", "ix_strategy_candidates_customer_id", ["customer_id"]),
    ("strategy_drafts", "ix_strategy_draft_customer", "ix_strategy_drafts_customer_id", ["customer_id"]),
    ("strategy_outcomes", "ix_strategy_outcome_closing_trade", "ix_strategy_outcomes_closing_trade_id", ["closing_trade_id"]),
    ("strategy_outcomes", "ix_strategy_outcome_trade", "ix_strategy_outcomes_trade_id", ["trade_id"]),
    ("trade_executors", "ix_trade_executor_bot_id", "ix_trade_executors_bot_id", ["bot_id"]),
    ("trade_executors", "ix_trade_executor_customer_id", "ix_trade_executors_customer_id", ["customer_id"]),
    ("trade_executors", "ix_trade_executor_trading_account_id", "ix_trade_executors_trading_account_id", ["trading_account_id"]),
    ("trade_learning_episodes", "ix_trade_learning_episode_entry_trade", "ix_trade_learning_episodes_entry_trade_id", ["entry_trade_id"]),
    ("trading_accounts", "ix_trading_account_customer_id", "ix_trading_accounts_customer_id", ["customer_id"]),
    ("tron_deposit_cursors", "ix_tron_deposit_cursor_wallet", "ix_tron_deposit_cursors_wallet_id", ["wallet_id"]),
    ("tron_sweeps", "ix_tron_sweep_wallet_id", "ix_tron_sweeps_wallet_id", ["wallet_id"]),
    ("withdrawals", "ix_withdrawal_customer_id", "ix_withdrawals_customer_id", ["customer_id"]),
    ("withdrawals", "ix_withdrawal_wallet_id", "ix_withdrawals_wallet_id", ["wallet_id"]),
]

NEW_INDEXES = [
    ("ix_adaptive_trading_bots_customer_id", "adaptive_trading_bots", ["customer_id"]),
    ("ix_arbitrage_opportunities_customer_id", "arbitrage_opportunities", ["customer_id"]),
    ("ix_arbitrage_opportunities_trading_account_id", "arbitrage_opportunities", ["trading_account_id"]),
    ("ix_cost_ledger_customer_id", "cost_ledger", ["customer_id"]),
    ("ix_custody_transfers_customer_id", "custody_transfers", ["customer_id"]),
    ("ix_customer_binance_accounts_customer_id", "customer_binance_accounts", ["customer_id"]),
    ("ix_customer_ledger_accounts_customer_id", "customer_ledger_accounts", ["customer_id"]),
    ("ix_dca_bots_trading_account_id", "dca_bots", ["trading_account_id"]),
    ("ix_funding_transactions_customer_id", "funding_transactions", ["customer_id"]),
    ("ix_grid_bots_trading_account_id", "grid_bots", ["trading_account_id"]),
    ("ix_incidents_customer_id", "incidents", ["customer_id"]),
    ("ix_ledger_entries_customer_id", "ledger_entries", ["customer_id"]),
    ("ix_ledger_journal_lines_customer_id", "ledger_journal_lines", ["customer_id"]),
    ("ix_ledger_journal_lines_journal_id", "ledger_journal_lines", ["journal_id"]),
    ("ix_positions_trading_account_id", "positions", ["trading_account_id"]),
    ("ix_referral_codes_customer_id", "referral_codes", ["customer_id"]),
    ("ix_referral_commissions_referral_customer_id", "referral_commissions", ["referral_customer_id"]),
    ("ix_referral_commissions_referral_id", "referral_commissions", ["referral_id"]),
    ("ix_referral_commissions_referred_customer_id", "referral_commissions", ["referred_customer_id"]),
    ("ix_referral_commissions_subscription_id", "referral_commissions", ["subscription_id"]),
    ("ix_referrals_referred_customer_id", "referrals", ["referred_customer_id"]),
    ("ix_referrals_referrer_customer_id", "referrals", ["referrer_customer_id"]),
    ("ix_revenue_ledger_customer_id", "revenue_ledger", ["customer_id"]),
    ("ix_revenue_ledger_subscription_id", "revenue_ledger", ["subscription_id"]),
    ("ix_smart_trades_trading_account_id", "smart_trades", ["trading_account_id"]),
    ("ix_strategy_candidate_runs_candidate_id", "strategy_candidate_runs", ["candidate_id"]),
    ("ix_strategy_outcomes_asset", "strategy_outcomes", ["asset"]),
    ("ix_strategy_outcomes_bot_id", "strategy_outcomes", ["bot_id"]),
    ("ix_strategy_outcomes_customer_id", "strategy_outcomes", ["customer_id"]),
    ("ix_strategy_outcomes_lifecycle_status", "strategy_outcomes", ["lifecycle_status"]),
    ("ix_strategy_outcomes_regime", "strategy_outcomes", ["regime"]),
    ("ix_strategy_outcomes_strategy", "strategy_outcomes", ["strategy"]),
    ("ix_strategy_outcomes_symbol", "strategy_outcomes", ["symbol"]),
    ("ix_subscriptions_customer_id", "subscriptions", ["customer_id"]),
    ("ix_trade_learning_episodes_asset", "trade_learning_episodes", ["asset"]),
    ("ix_trade_learning_episodes_bot_id", "trade_learning_episodes", ["bot_id"]),
    ("ix_trade_learning_episodes_closing_trade_id", "trade_learning_episodes", ["closing_trade_id"]),
    ("ix_trade_learning_episodes_customer_id", "trade_learning_episodes", ["customer_id"]),
    ("ix_trade_learning_episodes_exchange", "trade_learning_episodes", ["exchange"]),
    ("ix_trade_learning_episodes_regime", "trade_learning_episodes", ["regime"]),
    ("ix_trade_learning_episodes_replay_status", "trade_learning_episodes", ["replay_status"]),
    ("ix_trade_learning_episodes_status", "trade_learning_episodes", ["status"]),
    ("ix_trade_learning_episodes_strategy", "trade_learning_episodes", ["strategy"]),
    ("ix_trade_learning_episodes_symbol", "trade_learning_episodes", ["symbol"]),
    ("ix_trade_replay_results_episode_id", "trade_replay_results", ["episode_id"]),
    ("ix_trade_replay_results_strategy", "trade_replay_results", ["strategy"]),
    ("ix_webhook_endpoints_customer_id", "webhook_endpoints", ["customer_id"]),
    ("ix_webhook_events_endpoint_id", "webhook_events", ["endpoint_id"]),
    ("ix_withdrawal_otp_intents_auth_user_id", "withdrawal_otp_intents", ["auth_user_id"]),
]

NOT_NULL_COLUMNS = [
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
    ("trading_accounts", "pilot_status", sa.String(length=20)),
    ("trading_accounts", "pilot_stage", sa.Integer()),
    ("trading_accounts", "pilot_requested_by", sa.String(length=160)),
    ("trading_accounts", "pilot_approved_by", sa.String(length=160)),
    ("trading_accounts", "pilot_max_position_notional_usd", sa.Numeric(precision=38, scale=6)),
    ("trading_accounts", "pilot_max_total_exposure_usd", sa.Numeric(precision=38, scale=6)),
    ("trading_accounts", "pilot_max_open_positions", sa.Integer()),
    ("trading_accounts", "pilot_max_leverage", sa.Float(precision=53)),
    ("trading_accounts", "pilot_daily_loss_limit", sa.Float(precision=53)),
    ("trading_accounts", "updated_at", sa.DateTime(timezone=True)),
    ("tron_deposit_cursors", "updated_at", sa.DateTime(timezone=True)),
    ("tron_sweeps", "created_at", sa.DateTime(timezone=True)),
    ("withdrawal_step_up_tokens", "created_at", sa.DateTime(timezone=True)),
    ("withdrawals", "updated_at", sa.DateTime(timezone=True)),
]

PILOT_NUMERIC_COLUMNS = (
    "pilot_max_position_notional_usd",
    "pilot_max_total_exposure_usd",
)


def _bind():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("0048_schema_parity is PostgreSQL-only; use PostgreSQL 15 for migrations")
    return bind


def _indexes(table):
    return {
        item["name"]: tuple(item.get("column_names") or ())
        for item in sa.inspect(_bind()).get_indexes(table)
    }


def _rename_index(table, old_name, new_name, columns):
    current = _indexes(table)
    old_columns = current.get(old_name)
    new_columns = current.get(new_name)
    expected = tuple(columns)
    if old_columns is not None and tuple(old_columns) != expected:
        raise RuntimeError(f"{table}.{old_name} columns {old_columns!r} != expected {expected!r}")
    if new_columns is not None and tuple(new_columns) != expected:
        raise RuntimeError(f"{table}.{new_name} columns {new_columns!r} != expected {expected!r}")
    if old_columns is not None and new_columns is not None:
        raise RuntimeError(f"Both legacy and replacement indexes exist: {table}.{old_name}, {new_name}")
    if old_columns is None and new_columns is None:
        raise RuntimeError(f"Neither legacy nor replacement index exists: {table}.{old_name}, {new_name}")
    if old_columns is not None:
        op.execute(sa.text(f'ALTER INDEX "{old_name}" RENAME TO "{new_name}"'))


def _ensure_index(name, table, columns):
    current = _indexes(table)
    expected = tuple(columns)
    if name in current:
        if tuple(current[name]) != expected:
            raise RuntimeError(f"Existing index {table}.{name} has columns {current[name]!r}; expected {expected!r}")
        return
    op.create_index(name, table, list(columns), unique=False)


def _drop_index_if_present(name, table, columns):
    current = _indexes(table)
    if name not in current:
        return
    if tuple(current[name]) != tuple(columns):
        raise RuntimeError(f"Refusing to drop unexpected index {table}.{name}: {current[name]!r}")
    op.drop_index(name, table_name=table)


def _count_nulls(bind, table, column):
    return bind.execute(
        sa.text(f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NULL')
    ).scalar_one()


def _assert_numeric_fits_scale_18(bind, column):
    count = bind.execute(
        sa.text(
            f'SELECT count(*) FROM "trading_accounts" '
            f'WHERE "{column}" IS NOT NULL AND abs("{column}") >= 100000000000000000000::numeric'
        )
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"Cannot widen trading_accounts.{column} to NUMERIC(38,18): "
            f"{count} value(s) exceed its 20-digit integer range."
        )


def _assert_numeric_fits_scale_6(bind, column):
    count = bind.execute(
        sa.text(
            f'SELECT count(*) FROM "trading_accounts" '
            f'WHERE "{column}" IS NOT NULL AND "{column}" <> round("{column}", 6)'
        )
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"Refusing downgrade: trading_accounts.{column} contains {count} value(s) "
            "with more than six decimal places."
        )


def _assert_trade_customer_references(bind):
    count = bind.execute(sa.text(
        'SELECT count(*) FROM "trades" t '
        'LEFT JOIN "customer_profiles" c ON c.id = t.customer_id '
        'WHERE t.customer_id IS NOT NULL AND c.id IS NULL'
    )).scalar_one()
    if count:
        raise RuntimeError(
            f"Cannot add trades.customer_id foreign key: {count} orphan row(s); "
            "migration made no data substitutions."
        )


def _column_info(table, column):
    columns = {item["name"]: item for item in sa.inspect(_bind()).get_columns(table)}
    if column not in columns:
        raise RuntimeError(f"Expected column is missing: {table}.{column}")
    return columns[column]


def upgrade():
    bind = _bind()

    # Preflight all data constraints before any schema mutation.
    null_violations = []
    for table, column, _existing_type in NOT_NULL_COLUMNS:
        count = _count_nulls(bind, table, column)
        if count:
            null_violations.append(f"{table}.{column}={count}")
    if null_violations:
        raise RuntimeError(
            "Cannot enforce NOT NULL without changing existing data; "
            "NULL rows found: " + ", ".join(null_violations) +
            ". Migration made no data substitutions; reconcile from authoritative records and retry."
        )
    for column in PILOT_NUMERIC_COLUMNS:
        _assert_numeric_fits_scale_18(bind, column)
    _assert_trade_customer_references(bind)

    # Preserve existing indexes: rename verified single-column indexes in place.
    for table, old_name, new_name, columns in INDEX_RENAMES:
        _rename_index(table, old_name, new_name, columns)

    # Create only indexes confirmed absent from the migrated schema.
    for name, table, columns in NEW_INDEXES:
        _ensure_index(name, table, columns)

    # Match ORM NOT NULL semantics only after the explicit NULL preflight.
    for table, column, existing_type in NOT_NULL_COLUMNS:
        info = _column_info(table, column)
        is_pilot_numeric = table == "trading_accounts" and column in PILOT_NUMERIC_COLUMNS
        type_needs_change = False
        if is_pilot_numeric:
            current_type = info["type"]
            precision = getattr(current_type, "precision", None)
            scale = getattr(current_type, "scale", None)
            if (precision, scale) == (38, 6):
                type_needs_change = True
            elif (precision, scale) != (38, 18):
                raise RuntimeError(
                    f"Unexpected type for trading_accounts.{column}: {current_type!r}"
                )
        if not info["nullable"] and not type_needs_change:
            continue
        kwargs = {
            "existing_type": info["type"] if is_pilot_numeric else existing_type,
            "nullable": False,
        }
        if is_pilot_numeric:
            if type_needs_change:
                kwargs["type_"] = sa.Numeric(precision=38, scale=18)
            kwargs["existing_server_default"] = sa.text("'0'::numeric")
        elif table == "trading_accounts" and column == "pilot_status":
            kwargs["existing_server_default"] = sa.text("'NONE'::character varying")
        elif table == "trading_accounts" and column in ("pilot_requested_by", "pilot_approved_by"):
            kwargs["existing_server_default"] = sa.text("''::character varying")
        elif table == "trading_accounts" and column == "pilot_stage":
            kwargs["existing_server_default"] = sa.text("0")
        elif table == "trading_accounts" and column == "pilot_max_open_positions":
            kwargs["existing_server_default"] = sa.text("0")
        elif table == "trading_accounts" and column in ("pilot_max_leverage", "pilot_daily_loss_limit"):
            kwargs["existing_server_default"] = sa.text("'0'::double precision")
        op.alter_column(table, column, **kwargs)

    # The one FK genuinely absent from the migrated schema. The preflight above
    # proves all non-NULL customer IDs resolve before enforcing the relationship.
    existing_fks = sa.inspect(bind).get_foreign_keys("trades")
    matching = [
        fk for fk in existing_fks
        if fk.get("constrained_columns") == ["customer_id"]
        and fk.get("referred_table") == "customer_profiles"
        and fk.get("referred_columns") == ["id"]
    ]
    if not matching:
        op.create_foreign_key(
            "fk_trades_customer_id", "trades", "customer_profiles",
            ["customer_id"], ["id"],
        )
    elif len(matching) != 1 or matching[0].get("name") != "fk_trades_customer_id":
        raise RuntimeError("Unexpected existing trades.customer_id foreign-key state; refusing to guess.")


def downgrade():
    bind = _bind()

    # Never truncate financial configuration precision silently.
    for column in PILOT_NUMERIC_COLUMNS:
        _assert_numeric_fits_scale_6(bind, column)

    existing_fks = sa.inspect(bind).get_foreign_keys("trades")
    if any(fk.get("name") == "fk_trades_customer_id" for fk in existing_fks):
        op.drop_constraint("fk_trades_customer_id", "trades", type_="foreignkey")

    # Remove only the indexes created by this revision.
    for name, table, columns in reversed(NEW_INDEXES):
        _drop_index_if_present(name, table, columns)

    # Restore the historical index names without rebuilding their data.
    for table, old_name, new_name, columns in reversed(INDEX_RENAMES):
        _rename_index(table, new_name, old_name, columns)

    for table, column, existing_type in reversed(NOT_NULL_COLUMNS):
        info = _column_info(table, column)
        is_pilot_numeric = table == "trading_accounts" and column in PILOT_NUMERIC_COLUMNS
        type_needs_change = False
        if is_pilot_numeric:
            current_type = info["type"]
            precision = getattr(current_type, "precision", None)
            scale = getattr(current_type, "scale", None)
            if (precision, scale) == (38, 18):
                type_needs_change = True
            elif (precision, scale) != (38, 6):
                raise RuntimeError(
                    f"Unexpected downgrade type for trading_accounts.{column}: {current_type!r}"
                )
        if not info["nullable"] or type_needs_change:
            kwargs = {
                "existing_type": info["type"] if is_pilot_numeric else existing_type,
                "nullable": True,
            }
            if is_pilot_numeric:
                if type_needs_change:
                    kwargs["type_"] = sa.Numeric(precision=38, scale=6)
                kwargs["existing_server_default"] = sa.text("'0'::numeric")
            elif table == "trading_accounts" and column == "pilot_status":
                kwargs["existing_server_default"] = sa.text("'NONE'::character varying")
            elif table == "trading_accounts" and column in ("pilot_requested_by", "pilot_approved_by"):
                kwargs["existing_server_default"] = sa.text("''::character varying")
            elif table == "trading_accounts" and column == "pilot_stage":
                kwargs["existing_server_default"] = sa.text("0")
            elif table == "trading_accounts" and column == "pilot_max_open_positions":
                kwargs["existing_server_default"] = sa.text("0")
            elif table == "trading_accounts" and column in ("pilot_max_leverage", "pilot_daily_loss_limit"):
                kwargs["existing_server_default"] = sa.text("'0'::double precision")
            op.alter_column(table, column, **kwargs)
