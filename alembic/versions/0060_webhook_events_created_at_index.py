"""Create the webhook retention index without blocking inbound deliveries.

Revision ID: 0060_webhook_events_created_at_index
Revises: 0059_research_integrity_controls
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0060_webhook_events_created_at_index"
down_revision = "0059_research_integrity_controls"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_webhook_events_created_at_id"
TABLE_NAME = "webhook_events"
SCHEMA = "public"


def _postgres_index_valid(bind) -> bool | None:
    """Return True for a valid index, False for an invalid remnant, None if absent."""
    return bind.execute(
        sa.text(
            """
            SELECT index_data.indisvalid
            FROM pg_class AS table_class
            JOIN pg_namespace AS table_schema
              ON table_schema.oid = table_class.relnamespace
            JOIN pg_index AS index_data
              ON index_data.indrelid = table_class.oid
            JOIN pg_class AS index_class
              ON index_class.oid = index_data.indexrelid
            JOIN pg_namespace AS index_schema
              ON index_schema.oid = index_class.relnamespace
            WHERE table_schema.nspname = :schema_name
              AND table_class.relname = :table_name
              AND index_schema.nspname = :schema_name
              AND index_class.relname = :index_name
            """
        ),
        {
            "schema_name": SCHEMA,
            "table_name": TABLE_NAME,
            "index_name": INDEX_NAME,
        },
    ).scalar_one_or_none()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    schema = SCHEMA if bind.dialect.name == "postgresql" else None
    tables = set(inspector.get_table_names(schema=schema))
    if TABLE_NAME not in tables:
        raise RuntimeError(f"{TABLE_NAME} table is required before adding its retention index")

    if bind.dialect.name == "postgresql":
        valid = _postgres_index_valid(bind)
        if valid is True:
            return

        # CREATE INDEX CONCURRENTLY avoids the SHARE lock that would block the
        # webhook receiver's INSERTs while a potentially large index is built.
        # A failed concurrent build can leave an INVALID index behind; remove
        # that remnant concurrently before retrying the build.
        with op.get_context().autocommit_block():
            if valid is False:
                op.drop_index(
                    INDEX_NAME,
                    table_name=TABLE_NAME,
                    schema=SCHEMA,
                    postgresql_concurrently=True,
                )
            op.create_index(
                INDEX_NAME,
                TABLE_NAME,
                ["created_at", "id"],
                schema=SCHEMA,
                postgresql_concurrently=True,
            )
        return

    indexes = {
        index["name"] for index in inspector.get_indexes(TABLE_NAME, schema=schema)
    }
    if INDEX_NAME not in indexes:
        op.create_index(
            INDEX_NAME,
            TABLE_NAME,
            ["created_at", "id"],
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    schema = SCHEMA if bind.dialect.name == "postgresql" else None
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names(schema=schema))
    if TABLE_NAME not in tables:
        return

    if bind.dialect.name == "postgresql":
        exists = _postgres_index_valid(bind) is not None
        if exists:
            with op.get_context().autocommit_block():
                op.drop_index(
                    INDEX_NAME,
                    table_name=TABLE_NAME,
                    schema=SCHEMA,
                    postgresql_concurrently=True,
                )
        return

    indexes = {
        index["name"] for index in inspector.get_indexes(TABLE_NAME, schema=schema)
    }
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME, schema=schema)
