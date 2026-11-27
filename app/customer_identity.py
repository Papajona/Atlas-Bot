"""Bounded customer identity integrity checks shared by API runtimes."""
from __future__ import annotations

import uuid

from sqlalchemy import func, literal_column, select

from .db import CustomerProfile, _UUID_AUTH_REFERENCE_PATTERN, _UUID_AUTH_REFERENCE_GLOB


async def has_unmapped_legacy_customer_identity(db) -> bool:
    """Return whether any customer Auth reference is not a canonical UUID.

    PostgreSQL and SQLite evaluate UUID shape in SQL and stop after one match.
    Unknown dialects use a conservative Python scan instead of failing open.
    """
    column = CustomerProfile.auth_user_id
    bind = getattr(db, "bind", None)
    dialect = getattr(getattr(bind, "dialect", None), "name", "")
    statement = select(column)

    if dialect == "postgresql":
        statement = statement.where(column.op("!~*")(literal_column("'" + _UUID_AUTH_REFERENCE_PATTERN + "'"))).limit(1)
        result = await db.execute(statement)
        return result.first() is not None

    if dialect == "sqlite":
        statement = statement.where(
            func.lower(column).op("NOT GLOB")(literal_column("'" + _UUID_AUTH_REFERENCE_GLOB + "'"))
        ).limit(1)
        result = await db.execute(statement)
        return result.first() is not None

    rows = (await db.execute(statement)).scalars().all()
    for value in rows:
        try:
            uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return True
    return False
