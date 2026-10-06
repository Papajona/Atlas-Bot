"""Add Tier 1 KYC fields and required customer usernames."""
from alembic import op
import sqlalchemy as sa

revision = "0044_customer_kyc_username"
down_revision = "0043_live_trading_dual_control"
branch_labels = None
depends_on = None


def _has_col(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _has_table(bind, table: str) -> bool:
    return table in sa.inspect(bind).get_table_names()


def upgrade():
    bind = op.get_bind()

    if not _has_col(bind, "customer_profiles", "username"):
        op.add_column("customer_profiles", sa.Column("username", sa.String(32), nullable=True))
        bind.execute(sa.text(
            "UPDATE customer_profiles "
            "SET username = 'user_' || CAST(id AS VARCHAR(20)) "
            "WHERE username IS NULL OR username = ''"
        ))
        with op.batch_alter_table("customer_profiles") as batch:
            batch.alter_column("username", existing_type=sa.String(32), nullable=False)
            batch.create_unique_constraint("uq_customer_username", ["username"])
    elif "uq_customer_username" not in {c["name"] for c in sa.inspect(bind).get_unique_constraints("customer_profiles")}:
        with op.batch_alter_table("customer_profiles") as batch:
            batch.create_unique_constraint("uq_customer_username", ["username"])

    if not _has_table(bind, "customer_kyc_profiles"):
        op.create_table(
            "customer_kyc_profiles",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("customer_id", sa.Integer(), nullable=False),
            sa.Column("legal_first_name", sa.Text(), nullable=False, server_default=""),
            sa.Column("legal_middle_name", sa.Text(), nullable=False, server_default=""),
            sa.Column("legal_last_name", sa.Text(), nullable=False, server_default=""),
            sa.Column("date_of_birth", sa.Text(), nullable=False, server_default=""),
            sa.Column("citizenship_country", sa.String(2), nullable=False, server_default=""),
            sa.Column("residence_country", sa.String(2), nullable=False, server_default=""),
            sa.Column("residential_address", sa.Text(), nullable=False, server_default=""),
            sa.Column("phone", sa.Text(), nullable=False, server_default=""),
            sa.Column("document_type", sa.String(30), nullable=False, server_default=""),
            sa.Column("document_issuing_country", sa.String(2), nullable=False, server_default=""),
            sa.Column("document_reference", sa.Text(), nullable=False, server_default=""),
            sa.Column("document_issued_at", sa.Text(), nullable=False, server_default=""),
            sa.Column("document_expires_at", sa.Text(), nullable=False, server_default=""),
            sa.Column("verification_provider", sa.String(80), nullable=False, server_default=""),
            sa.Column("verification_provider_reference", sa.Text(), nullable=False, server_default=""),
            sa.Column("verification_result", sa.String(30), nullable=False, server_default="PENDING"),
            sa.Column("status", sa.String(30), nullable=False, server_default="PENDING"),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("customer_id", name="uq_customer_kyc_customer"),
        )


def downgrade():
    bind = op.get_bind()
    if _has_table(bind, "customer_kyc_profiles"):
        op.drop_table("customer_kyc_profiles")
    if _has_col(bind, "customer_profiles", "username"):
        with op.batch_alter_table("customer_profiles") as batch:
            batch.drop_constraint("uq_customer_username", type_="unique")
            batch.drop_column("username")
