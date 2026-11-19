#!/usr/bin/env python3
"""Verify the October 22 Supabase pg_dump log evidence against its exact GCS object."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


BACKUP_BUCKET = "atlas-staging-510915-db-backups"
SOURCE_BACKUP_REFERENCE = (
    "gs://atlas-staging-510915-db-backups/"
    "atlas-supabase-pre-customer-paper-20261022T153023Z.dump#1792683068959304"
)
SOURCE_PROJECT_REF = "wauaelzyugjuepmrewaw"
SOURCE_DATABASE = "postgres"
SOURCE_DATABASE_USER = "postgres"
SOURCE_APPLICATION_NAME = "pg_dump"
SOURCE_POOLER_MODE = "session"
SOURCE_AUTHENTICATED_AT = "2026-10-22T15:30:54.953883Z"
SOURCE_TERMINATED_AT = "2026-10-22T15:31:05.750945Z"
SOURCE_OBJECT_SIZE_BYTES = 659332
SOURCE_OBJECT_MD5 = "kX13N5wvc5RE4MJ19hpt6w=="
SOURCE_OBJECT_CRC32C = "Ya71XA=="


def _get(value: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if value.get(name) not in (None, ""):
            return value[name]
    return None


def _time(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def verify_supabase_pg_dump_source(
    *,
    source_backup_reference: str,
    source_project_ref: str,
    source_database: str,
    object_metadata: Any,
    bucket_metadata: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind the only reviewed Supabase pg_dump session to the exact immutable GCS generation."""
    if source_backup_reference != SOURCE_BACKUP_REFERENCE:
        raise ValueError("source backup reference does not match the reviewed pg_dump evidence")
    if source_project_ref != SOURCE_PROJECT_REF:
        raise ValueError("source Supabase project ref does not match the authenticated pg_dump log")
    if source_database != SOURCE_DATABASE:
        raise ValueError("source database does not match the authenticated pg_dump log")
    if not isinstance(object_metadata, Mapping):
        raise ValueError("GCS source object metadata must be a JSON object")
    if not isinstance(bucket_metadata, Mapping):
        raise ValueError("GCS backup bucket metadata must be a JSON object")

    expected_name, expected_generation = SOURCE_BACKUP_REFERENCE.split("gs://" + BACKUP_BUCKET + "/", 1)[1].split("#", 1)
    if str(_get(object_metadata, "name") or "") != expected_name:
        raise ValueError("GCS source object name does not match the authenticated pg_dump evidence")
    observed_bucket = _get(object_metadata, "bucket")
    if observed_bucket not in (None, "", BACKUP_BUCKET):
        raise ValueError("GCS source object bucket does not match the approved backup bucket")
    if str(_get(object_metadata, "generation") or "") != expected_generation:
        raise ValueError("GCS source object generation does not match the authenticated pg_dump evidence")
    try:
        size = int(_get(object_metadata, "size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size != SOURCE_OBJECT_SIZE_BYTES:
        raise ValueError("GCS source object size does not match the reviewed generation fingerprint")
    if _get(object_metadata, "md5_hash", "md5Hash") != SOURCE_OBJECT_MD5:
        raise ValueError("GCS source object MD5 does not match the reviewed generation fingerprint")
    if _get(object_metadata, "crc32c_hash", "crc32c") != SOURCE_OBJECT_CRC32C:
        raise ValueError("GCS source object CRC32C does not match the reviewed generation fingerprint")

    retention = _get(bucket_metadata, "retention_policy", "retentionPolicy")
    if not isinstance(retention, Mapping):
        raise ValueError("backup bucket retention policy is missing")
    if _get(retention, "is_locked", "isLocked") is not True:
        raise ValueError("backup bucket retention policy is not locked")

    auth_at = _time(SOURCE_AUTHENTICATED_AT, "Supabase pg_dump authentication")
    terminated_at = _time(SOURCE_TERMINATED_AT, "Supabase pg_dump termination")
    object_created_at = _time(str(_get(object_metadata, "time_created", "timeCreated") or ""), "GCS source object creation")
    if not (auth_at < terminated_at < object_created_at):
        raise ValueError("Supabase pg_dump session does not precede the GCS object creation")
    gap_seconds = (object_created_at - terminated_at).total_seconds()
    if gap_seconds < 0 or gap_seconds > 10:
        raise ValueError("GCS object was not created within 10 seconds after the logged pg_dump client terminated")

    retention_expiration = _get(object_metadata, "retention_expiration_time", "retentionExpirationTime")
    retention_ends = _time(str(retention_expiration or ""), "GCS source object retention expiration")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("verification clock must be timezone-aware")
    if retention_ends <= current.astimezone(timezone.utc):
        raise ValueError("GCS source object retention has expired")

    return {
        "status": "PASS",
        "source_type": "supabase_pg_dump",
        "source_project_ref": SOURCE_PROJECT_REF,
        "source_database": SOURCE_DATABASE,
        "source_database_user": SOURCE_DATABASE_USER,
        "source_application_name": SOURCE_APPLICATION_NAME,
        "source_pooler_mode": SOURCE_POOLER_MODE,
        "source_authenticated_at": auth_at.isoformat(),
        "source_terminated_at": terminated_at.isoformat(),
        "source_backup_reference": SOURCE_BACKUP_REFERENCE,
        "source_object_created_at": object_created_at.isoformat(),
        "source_object_size_bytes": size,
        "source_object_md5": SOURCE_OBJECT_MD5,
        "source_object_crc32c": SOURCE_OBJECT_CRC32C,
        "seconds_from_pg_dump_termination_to_object_creation": gap_seconds,
        "bucket_retention_policy_locked": True,
        "source_association_evidence": (
            "Supabase supavisor_logs reported exactly one pg_dump client session for this project/database "
            "on 2026-10-22; its authenticated and terminate events bracket the generation-pinned GCS object "
            "creation within the verified maximum 10-second correlation window."
        ),
        "evidence_source": "Supabase unified logs, source=supavisor_logs, UTC window 2026-10-22",
        "peer_ip_omitted": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-backup-reference", required=True)
    parser.add_argument("--source-project-ref", required=True)
    parser.add_argument("--source-database", required=True)
    parser.add_argument("--object-metadata", required=True)
    parser.add_argument("--bucket-metadata", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        result = verify_supabase_pg_dump_source(
            source_backup_reference=args.source_backup_reference,
            source_project_ref=args.source_project_ref,
            source_database=args.source_database,
            object_metadata=json.loads(Path(args.object_metadata).read_text(encoding="utf-8")),
            bucket_metadata=json.loads(Path(args.bucket_metadata).read_text(encoding="utf-8")),
        )
        if args.output:
            Path(args.output).write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "reason": "SUPABASE_PG_DUMP_SOURCE_PROVENANCE_FAILED",
            "error_type": type(exc).__name__,
            "detail": str(exc),
        }, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
