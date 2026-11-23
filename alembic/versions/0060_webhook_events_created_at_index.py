"""Index webhook events by retention timestamp.

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


def upgrade() -> None:
    bind = op.get_bind()
    schema = "public" if bind.dialect.name == "postgresql" else None
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names(schema=schema))
    if "webhook_events" not in tables:
        raise RuntimeError("webhook_events table is required before adding its retention index")
    indexes = {index["name"] for index in inspector.get_indexes("webhook_events", schema=schema)}
    if INDEX_NAME not in indexes:
        op.create_index(
            INDEX_NAME,
            "webhook_events",
            ["created_at", "id"],
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    schema = "public" if bind.dialect.name == "postgresql" else None
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names(schema=schema))
    if "webhook_events" not in tables:
        return
    indexes = {index["name"] for index in inspector.get_indexes("webhook_events", schema=schema)}
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="webhook_events", schema=schema)
