"""Index unmapped customer Auth references for bounded identity checks."""
from alembic import op
import sqlalchemy as sa


revision = "0060_customer_identity_unmapped_index"
down_revision = "0059_research_integrity_controls"
branch_labels = None
depends_on = None

_UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_UUID_GLOB = (
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {
        item["name"]
        for item in inspector.get_indexes(
            "customer_profiles",
            schema="public" if bind.dialect.name == "postgresql" else None,
        )
    }
    name = "ix_customer_profiles_unmapped_auth_user_id"
    if name in indexes:
        return

    if bind.dialect.name == "postgresql":
        op.create_index(
            name,
            "customer_profiles",
            ["auth_user_id"],
            unique=False,
            schema="public",
            postgresql_where=sa.text(f"auth_user_id !~* '{_UUID_PATTERN}'"),
        )
    elif bind.dialect.name == "sqlite":
        op.create_index(
            name,
            "customer_profiles",
            ["auth_user_id"],
            unique=False,
            sqlite_where=sa.text(f"lower(auth_user_id) NOT GLOB '{_UUID_GLOB}'"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    schema = "public" if bind.dialect.name == "postgresql" else None
    indexes = {
        item["name"]
        for item in sa.inspect(bind).get_indexes("customer_profiles", schema=schema)
    }
    if "ix_customer_profiles_unmapped_auth_user_id" in indexes:
        op.drop_index(
            "ix_customer_profiles_unmapped_auth_user_id",
            table_name="customer_profiles",
            schema=schema,
        )
