"""Enforce case-insensitive uniqueness for TRON sweep transaction hashes."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0068_tron_sweep_txid_casefold_unique"
down_revision = "0067_service_role_customer_profiles_acl"
branch_labels = None
depends_on = None

TABLE_NAME = "tron_sweeps"
LEGACY_INDEX_NAME = "uq_tron_sweep_transaction_id_nonempty"
CASEFOLD_INDEX_NAME = "uq_tron_sweep_transaction_id_ci_nonempty"
NONEMPTY_PREDICATE = sa.text("transaction_id <> ''")


def _postgres_index_valid(bind) -> bool | None:
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
            "schema_name": "public",
            "table_name": TABLE_NAME,
            "index_name": CASEFOLD_INDEX_NAME,
        },
    ).scalar_one_or_none()


def _reject_legacy_casefold_collisions(bind) -> None:
    collisions = bind.execute(
        sa.text(
            f"""
            SELECT lower(transaction_id) AS normalized_id, COUNT(*) AS row_count
            FROM {TABLE_NAME}
            WHERE transaction_id <> ''
            GROUP BY lower(transaction_id)
            HAVING COUNT(*) > 1
            LIMIT 10
            """
        )
    ).all()
    if collisions:
        raise RuntimeError(
            "Cannot enforce case-insensitive TRON sweep transaction uniqueness: "
            f"{len(collisions)} legacy transaction-hash collision group(s) exist. "
            "Reconcile the affected sweeps and custody transfers before retrying."
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"postgresql", "sqlite"}:
        raise RuntimeError(
            f"Unsupported database dialect for {TABLE_NAME} hash uniqueness: "
            f"{bind.dialect.name}"
        )
    schema = "public" if bind.dialect.name == "postgresql" else None
    if TABLE_NAME not in sa.inspect(bind).get_table_names(schema=schema):
        raise RuntimeError(f"Required table {TABLE_NAME} does not exist")

    # Do not silently choose a winner if legacy case-sensitive rows already
    # represent the same on-chain transaction. That requires custody review.
    _reject_legacy_casefold_collisions(bind)

    indexes = {
        item["name"]
        for item in sa.inspect(bind).get_indexes(TABLE_NAME, schema=schema)
    }
    if bind.dialect.name == "postgresql":
        state = _postgres_index_valid(bind)
        if state is not True:
            with op.get_context().autocommit_block():
                if state is False:
                    op.drop_index(
                        CASEFOLD_INDEX_NAME,
                        table_name=TABLE_NAME,
                        schema=schema,
                        postgresql_concurrently=True,
                    )
                op.create_index(
                    CASEFOLD_INDEX_NAME,
                    TABLE_NAME,
                    [sa.text("lower(transaction_id)")],
                    unique=True,
                    schema=schema,
                    postgresql_where=NONEMPTY_PREDICATE,
                    postgresql_concurrently=True,
                )
            if LEGACY_INDEX_NAME in indexes:
                with op.get_context().autocommit_block():
                    op.drop_index(
                        LEGACY_INDEX_NAME,
                        table_name=TABLE_NAME,
                        schema=schema,
                        postgresql_concurrently=True,
                    )
        elif LEGACY_INDEX_NAME in indexes:
            with op.get_context().autocommit_block():
                op.drop_index(
                    LEGACY_INDEX_NAME,
                    table_name=TABLE_NAME,
                    schema=schema,
                    postgresql_concurrently=True,
                )
        return

    if CASEFOLD_INDEX_NAME not in indexes:
        op.create_index(
            CASEFOLD_INDEX_NAME,
            TABLE_NAME,
            [sa.text("lower(transaction_id)")],
            unique=True,
            schema=schema,
            sqlite_where=NONEMPTY_PREDICATE,
        )
    if LEGACY_INDEX_NAME in indexes:
        op.drop_index(LEGACY_INDEX_NAME, table_name=TABLE_NAME, schema=schema)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"postgresql", "sqlite"}:
        return
    schema = "public" if bind.dialect.name == "postgresql" else None
    indexes = {
        item["name"]
        for item in sa.inspect(bind).get_indexes(TABLE_NAME, schema=schema)
    }
    if bind.dialect.name == "postgresql":
        if LEGACY_INDEX_NAME not in indexes:
            with op.get_context().autocommit_block():
                op.create_index(
                    LEGACY_INDEX_NAME,
                    TABLE_NAME,
                    ["transaction_id"],
                    unique=True,
                    schema=schema,
                    postgresql_where=NONEMPTY_PREDICATE,
                    postgresql_concurrently=True,
                )
        if _postgres_index_valid(bind) is not None:
            with op.get_context().autocommit_block():
                op.drop_index(
                    CASEFOLD_INDEX_NAME,
                    table_name=TABLE_NAME,
                    schema=schema,
                    postgresql_concurrently=True,
                )
        return

    if LEGACY_INDEX_NAME not in indexes:
        op.create_index(
            LEGACY_INDEX_NAME,
            TABLE_NAME,
            ["transaction_id"],
            unique=True,
            schema=schema,
            sqlite_where=NONEMPTY_PREDICATE,
        )
    if CASEFOLD_INDEX_NAME in indexes:
        op.drop_index(CASEFOLD_INDEX_NAME, table_name=TABLE_NAME, schema=schema)
