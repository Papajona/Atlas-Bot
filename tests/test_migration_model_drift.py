"""Opt-in ORM migration drift check against a separately provisioned database.

Run after upgrading an isolated database to Alembic head:

    alembic upgrade head
    ATLAS_MIGRATION_DRIFT_URL=postgresql+asyncpg://user:pass@host/db pytest -m migration_drift

This marker is excluded from the mandatory no-skip application suite. The opt-in
job may skip when its isolated database URL is not configured.
"""
import asyncio
import os

import pytest


URL = os.environ.get("ATLAS_MIGRATION_DRIFT_URL")


def _collect_diff():
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import Base

    async def run():
        engine = create_async_engine(URL)
        try:
            async with engine.connect() as conn:
                def compare(sync_conn):
                    context = MigrationContext.configure(sync_conn)
                    return compare_metadata(context, Base.metadata)

                return await conn.run_sync(compare)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _flatten(diff):
    for entry in diff:
        if isinstance(entry, list):
            yield from _flatten(entry)
        else:
            yield entry


@pytest.mark.migration_drift
@pytest.mark.skipif(not URL, reason="ATLAS_MIGRATION_DRIFT_URL is not configured")
def test_models_have_matching_migrations():
    missing = []
    for entry in _flatten(_collect_diff()):
        kind = entry[0]
        if kind == "add_table":
            missing.append(f"table {entry[1].name}")
        elif kind == "add_column":
            missing.append(f"column {entry[2]}.{entry[3].name}")
    assert not missing, "models without migrations: " + ", ".join(sorted(missing))
