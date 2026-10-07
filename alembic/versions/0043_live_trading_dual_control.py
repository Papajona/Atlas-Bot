"""Require two distinct risk officers to enable live trading."""
from alembic import context, op
import sqlalchemy as sa

revision = "0043_live_trading_dual_control"
down_revision = "0042_research_run_macro_context"
branch_labels = None
depends_on = None


def _has_col(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade():
    if context.is_offline_mode():
        columns = (
            ("live_enable_requested_by", sa.String(160), ""),
            ("live_enable_requested_at", sa.DateTime(timezone=True), None),
            ("live_enable_approved_by", sa.String(160), ""),
            ("live_enable_approved_at", sa.DateTime(timezone=True), None),
        )
        for name, typ, default in columns:
            kwargs = {"nullable": True}
            if default is not None:
                kwargs["server_default"] = default
            op.add_column("app_state", sa.Column(name, typ, **kwargs))
        return

    bind = op.get_bind()
    if not _has_col(bind, "app_state", "live_enable_requested_by"):
        op.add_column("app_state", sa.Column("live_enable_requested_by", sa.String(160), nullable=False, server_default=""))
    if not _has_col(bind, "app_state", "live_enable_requested_at"):
        op.add_column("app_state", sa.Column("live_enable_requested_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_col(bind, "app_state", "live_enable_approved_by"):
        op.add_column("app_state", sa.Column("live_enable_approved_by", sa.String(160), nullable=False, server_default=""))
    if not _has_col(bind, "app_state", "live_enable_approved_at"):
        op.add_column("app_state", sa.Column("live_enable_approved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    if context.is_offline_mode():
        for column in ("live_enable_approved_at", "live_enable_approved_by", "live_enable_requested_at", "live_enable_requested_by"):
            op.drop_column("app_state", column)
        return

    bind = op.get_bind()
    for column in ("live_enable_approved_at", "live_enable_approved_by", "live_enable_requested_at", "live_enable_requested_by"):
        if _has_col(bind, "app_state", column):
            op.drop_column("app_state", column)
