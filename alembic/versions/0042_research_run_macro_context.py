"""Persist the daily macro/news snapshot on research_runs so it survives past the single async call that computes it."""
from alembic import op
import sqlalchemy as sa

revision = "0042_research_run_macro_context"
down_revision = "0041_runtime_schema_repairs"
branch_labels = None
depends_on = None


def _has_col(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade():
    bind = op.get_bind()
    if not _has_col(bind, "research_runs", "macro_risk_state"):
        op.add_column("research_runs", sa.Column("macro_risk_state", sa.String(20), nullable=False, server_default=""))
    if not _has_col(bind, "research_runs", "macro_risk_score"):
        op.add_column("research_runs", sa.Column("macro_risk_score", sa.Float(), nullable=False, server_default="0"))


def downgrade():
    bind = op.get_bind()
    if _has_col(bind, "research_runs", "macro_risk_score"):
        op.drop_column("research_runs", "macro_risk_score")
    if _has_col(bind, "research_runs", "macro_risk_state"):
        op.drop_column("research_runs", "macro_risk_state")
