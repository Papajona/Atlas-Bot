from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from .db import AuditChainState, AuditLog, SessionLocal
from .distributed import allow_rate_limit


# SQLite ignores SELECT ... FOR UPDATE. Serialize idempotent appends within this
# process per database, then recover from cross-process lock/unique races by
# rolling back and reading the winner's indexed idempotency row.
_SQLITE_AUDIT_LOCKS: dict[str, asyncio.Lock] = {}


def _sqlite_audit_lock(db) -> asyncio.Lock | None:
    bind = db.get_bind()
    if bind.dialect.name != "sqlite":
        return None
    key = str(bind.url)
    lock = _SQLITE_AUDIT_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SQLITE_AUDIT_LOCKS[key] = lock
    return lock


async def _existing_idempotent_audit(db, *, event: str, idempotency_key: str):
    return (
        await db.execute(
            select(AuditLog.id, AuditLog.event_hash)
            .where(
                AuditLog.event == event,
                AuditLog.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
    ).first()


async def _lock_chain_state(db) -> AuditChainState:
    """Create/read and lock the single chain head before inspecting or appending rows."""
    locked = (
        select(AuditChainState)
        .where(AuditChainState.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    state = (await db.execute(locked)).scalar_one_or_none()
    if state is None:
        try:
            async with db.begin_nested():
                db.add(AuditChainState(id=1, last_hash=""))
                await db.flush()
        except IntegrityError:
            pass
        state = (await db.execute(locked)).scalar_one()
    return state


async def _rate_limited_detail(detail: dict, actor_id: str) -> dict:
    """Perform the Redis round trip before taking the global audit-chain lock."""
    allowed, _remaining = await allow_rate_limit(
        f"audit_chain:{actor_id or 'system'}", limit=100, window_seconds=60
    )
    audit_detail = dict(detail or {})
    if not allowed:
        audit_detail["audit_chain_rate_limited"] = True
        audit_detail["audit_chain_rate_limit"] = {"limit": 100, "window_seconds": 60}
    return audit_detail


async def _append_audit_locked(
    db,
    *,
    event: str,
    audit_detail: dict,
    actor_id: str,
    state: AuditChainState,
    idempotency_key: str | None = None,
) -> AuditLog:
    """Append using an already-locked chain head; no network calls occur under the lock."""
    created = datetime.now(timezone.utc)
    previous = state.last_hash or ""
    payload = json.dumps(
        {
            "event": event,
            "detail": audit_detail,
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
        idempotency_key=idempotency_key,
        detail=json.dumps(audit_detail, default=str),
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


async def append_audit_once(
    db,
    *,
    event: str,
    detail: dict,
    actor_id: str = "",
    idempotency_fields: tuple[str, ...],
) -> tuple[AuditLog | SimpleNamespace, bool]:
    """Append once by a hashed identity, without scanning or decrypting the audit log."""
    if not idempotency_fields or any(field not in (detail or {}) for field in idempotency_fields):
        raise ValueError("audit idempotency fields must be present in the detail payload")

    audit_detail = await _rate_limited_detail(detail, actor_id)
    identity = {field: detail[field] for field in idempotency_fields}
    identity_payload = json.dumps(
        {"event": event, "identity": identity},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    idempotency_key = hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()

    # Fast indexed read before the global chain-head lock: ordinary recovery polls
    # should not serialize all platform audit writers just to detect an existing event.
    # Select only non-encrypted columns so damaged historical detail cannot block retry.
    existing = (
        await db.execute(
            select(AuditLog.id, AuditLog.event_hash)
            .where(
                AuditLog.event == event,
                AuditLog.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
    ).first()
    if existing is not None:
        return SimpleNamespace(id=existing.id, event_hash=existing.event_hash), False

    sqlite_lock = _sqlite_audit_lock(db)
    if sqlite_lock is None:
        state = await _lock_chain_state(db)
        existing = await _existing_idempotent_audit(
            db, event=event, idempotency_key=idempotency_key
        )
        if existing is not None:
            return SimpleNamespace(id=existing.id, event_hash=existing.event_hash), False
        row = await _append_audit_locked(
            db,
            event=event,
            audit_detail=audit_detail,
            actor_id=actor_id,
            state=state,
            idempotency_key=idempotency_key,
        )
        return row, True

    await sqlite_lock.acquire()
    try:
        # Process-local serialization handles same-process retries. The bounded
        # retry loop below additionally handles SQLite file-lock contention from
        # independent worker processes writing different audit identities.
        for attempt in range(20):
            try:
                existing = await _existing_idempotent_audit(
                    db, event=event, idempotency_key=idempotency_key
                )
                if existing is not None:
                    return SimpleNamespace(id=existing.id, event_hash=existing.event_hash), False

                state = await _lock_chain_state(db)
                existing = await _existing_idempotent_audit(
                    db, event=event, idempotency_key=idempotency_key
                )
                if existing is not None:
                    return SimpleNamespace(id=existing.id, event_hash=existing.event_hash), False

                row = await _append_audit_locked(
                    db,
                    event=event,
                    audit_detail=audit_detail,
                    actor_id=actor_id,
                    state=state,
                    idempotency_key=idempotency_key,
                )
                return row, True
            except (IntegrityError, OperationalError) as exc:
                await db.rollback()
                # A winning same-identity writer makes this call an idempotent
                # replay. A different-identity SQLite writer is not a replay:
                # retry the entire chain-head read + append with a fresh txn.
                try:
                    existing = await _existing_idempotent_audit(
                        db, event=event, idempotency_key=idempotency_key
                    )
                except OperationalError:
                    await db.rollback()
                    existing = None
                if existing is not None:
                    return SimpleNamespace(id=existing.id, event_hash=existing.event_hash), False
                if isinstance(exc, OperationalError):
                    error_text = str(exc).lower()
                    if not any(
                        marker in error_text
                        for marker in ("database is locked", "database table is locked", "database is busy")
                    ):
                        raise
                # An integrity race can occur while independent processes initialize
                # or advance the singleton chain head. Re-read from a fresh transaction
                # and retry the complete append; deterministic invalid rows still fail
                # after the bounded attempts rather than being acknowledged.
                await db.rollback()
                if attempt == 19:
                    raise
                await asyncio.sleep(min(0.01 * (2 ** min(attempt, 4)), 0.16))
    finally:
        sqlite_lock.release()


async def append_audit(db, *, event: str, detail: dict, actor_id: str = "") -> AuditLog:
    # Preserve the established rate-limit annotation, but complete its Redis round
    # trip before locking the chain head so all other audit writers are not blocked.
    audit_detail = await _rate_limited_detail(detail, actor_id)
    state = await _lock_chain_state(db)
    return await _append_audit_locked(
        db,
        event=event,
        audit_detail=audit_detail,
        actor_id=actor_id,
        state=state,
    )


async def record_audit(event: str, detail: dict, actor_id: str = "system") -> AuditLog:
    """Append an audit row using a dedicated session (safe to call from any module)."""
    async with SessionLocal() as db:
        return await append_audit(db, event=event, detail=detail, actor_id=actor_id)
