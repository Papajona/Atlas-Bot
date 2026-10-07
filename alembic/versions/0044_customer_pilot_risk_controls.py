"""Add per-customer pilot risk controls."""
from alembic import context, op
import sqlalchemy as sa

revision = "0044_customer_pilot_risk_controls"
down_revision = "0043_live_trading_dual_control"
branch_labels = None
depends_on = None


def _has_col(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade():
    columns = [
        ("pilot_status", sa.String(20), "NONE"),
        ("pilot_stage", sa.Integer(), "0"),
        ("pilot_requested_by", sa.String(160), ""),
        ("pilot_requested_at", sa.DateTime(timezone=True), None),
        ("pilot_approved_by", sa.String(160), ""),
        ("pilot_approved_at", sa.DateTime(timezone=True), None),
        ("pilot_expires_at", sa.DateTime(timezone=True), None),
        ("pilot_max_position_notional_usd", sa.Numeric(38, 6), "0"),
        ("pilot_max_total_exposure_usd", sa.Numeric(38, 6), "0"),
        ("pilot_max_open_positions", sa.Integer(), "0"),
        ("pilot_max_leverage", sa.Float(), "0"),
        ("pilot_daily_loss_limit", sa.Float(), "0"),
    ]

    if context.is_offline_mode():
        for name, typ, default in columns:
            kwargs = {"nullable": True}
            if default is not None:
                kwargs["server_default"] = default
            op.add_column("trading_accounts", sa.Column(name, typ, **kwargs))
        op.execute(sa.text(
            "UPDATE trading_accounts SET pilot_status='NONE', pilot_stage=0, "
            "pilot_requested_by='', pilot_approved_by='', "
            "pilot_max_position_notional_usd=0, pilot_max_total_exposure_usd=0, "
            "pilot_max_open_positions=0, pilot_max_leverage=0, pilot_daily_loss_limit=0 "
            "WHERE pilot_status IS NULL"
        ))
        return

    bind = op.get_bind()
    for name, typ, default in columns:
        if not _has_col(bind, "trading_accounts", name):
            kwargs = {"nullable": True}
            if default is not None:
                kwargs["server_default"] = default
            op.add_column("trading_accounts", sa.Column(name, typ, **kwargs))

    op.execute(sa.text(
        "UPDATE trading_accounts SET pilot_status='NONE', pilot_stage=0, "
        "pilot_requested_by='', pilot_approved_by='', "
        "pilot_max_position_notional_usd=0, pilot_max_total_exposure_usd=0, "
        "pilot_max_open_positions=0, pilot_max_leverage=0, pilot_daily_loss_limit=0 "
        "WHERE pilot_status IS NULL"
    ))


def downgrade():
    if context.is_offline_mode():
        for name in (
            "pilot_daily_loss_limit", "pilot_max_leverage", "pilot_max_open_positions",
            "pilot_max_total_exposure_usd", "pilot_max_position_notional_usd",
            "pilot_expires_at", "pilot_approved_at", "pilot_approved_by",
            "pilot_requested_at", "pilot_requested_by", "pilot_stage", "pilot_status",
        ):
            op.drop_column("trading_accounts", name)
        return

    bind = op.get_bind()
    for name in (
        "pilot_daily_loss_limit", "pilot_max_leverage", "pilot_max_open_positions",
        "pilot_max_total_exposure_usd", "pilot_max_position_notional_usd",
        "pilot_expires_at", "pilot_approved_at", "pilot_approved_by",
        "pilot_requested_at", "pilot_requested_by", "pilot_stage", "pilot_status",
    ):
        if _has_col(bind, "trading_accounts", name):
            op.drop_column("trading_accounts", name)
