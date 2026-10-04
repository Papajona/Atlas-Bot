from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import AuditChainState, AuditLog, SessionLocal


async def append_audit(db, *, event: str, detail: dict, actor_id: str = "") -> AuditLog:
    # Lock before reading so PostgreSQL writers always hash from the committed predecessor.
    # populate_existing is required when this session already holds a stale identity-map object.
    locked = (
        select(AuditChainState)
        .where(AuditChainState.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    state = (await db.execute(locked)).scalar_one_or_none()
    if state is None:
        # The first two writers can race on creation. Keep the unique-key loser inside a
        # savepoint so the outer transaction remains usable, then acquire the winner's row lock.
        try:
            async with db.begin_nested():
                db.add(AuditChainState(id=1, last_hash=""))
                await db.flush()
        except IntegrityError:
            pass
        state = (await db.execute(locked)).scalar_one()

    created = datetime.now(timezone.utc)
    previous = state.last_hash or ""
    payload = json.dumps(
        {
            "event": event,
            "detail": detail,
            "actor_id": actor_id,
            "created_at": created.isoformat(),
            "previous_hash": previous,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    event_hash = hashlib.sha256(payload.encode()).hexdigest()
    row = AuditLog(
        event=event,
        detail=json.dumps(detail, default=str),
        actor_id=actor_id,
        previous_hash=previous,
        event_hash=event_hash,
        created_at=created,
    )
    db.add(row)
    state.last_hash = event_hash
    state.updated_at = created
    await db.commit()
    return row


async def record_audit(event: str, detail: dict, actor_id: str = "system") -> AuditLog:
    """Append an audit row using a dedicated session (safe to call from any module)."""
    async with SessionLocal() as db:
        return await append_audit(db, event=event, detail=detail, actor_id=actor_id)
