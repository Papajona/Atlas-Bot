#!/usr/bin/env python3
"""Bind a Cloud SQL IMPORT to a generation-pinned, retention-protected GCS backup."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_BACKUP_RE = re.compile(r"^(gs://[^/]+/.+)#([1-9][0-9]*)$")


def _pick(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"provider evidence is missing {label}")
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"provider evidence has an invalid {label}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"provider evidence {label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def verify_backup_binding(
    *,
    backup_reference: str,
    object_metadata: Any,
    bucket_metadata: Any,
    operation_record: Any,
) -> dict[str, Any]:
    match = _BACKUP_RE.fullmatch(backup_reference or "")
    if not match:
        raise ValueError("backup reference must be a generation-pinned gs:// URI ending in #<generation>")
    expected_uri, expected_generation = match.groups()
    if not isinstance(object_metadata, Mapping):
        raise ValueError("GCS object metadata must be a JSON object")
    if not isinstance(bucket_metadata, Mapping):
        raise ValueError("GCS bucket metadata must be a JSON object")
    if not isinstance(operation_record, Mapping):
        raise ValueError("Cloud SQL operation evidence must be a JSON object")

    observed_generation = str(_pick(object_metadata, "generation") or "")
    if observed_generation != expected_generation:
        raise ValueError("GCS object generation differs from the generation pinned in BACKUP_REFERENCE")

    try:
        size = int(_pick(object_metadata, "size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        raise ValueError("GCS backup object must have a positive non-zero size")

    md5_hash = _pick(object_metadata, "md5_hash", "md5Hash")
    crc32c_hash = _pick(object_metadata, "crc32c_hash", "crc32c")
    if not (md5_hash or crc32c_hash):
        raise ValueError("GCS backup object has no provider checksum metadata (md5_hash or crc32c_hash)")

    retention = _pick(bucket_metadata, "retention_policy", "retentionPolicy")
    if not isinstance(retention, Mapping):
        raise ValueError("approved backup bucket has no retention policy metadata")
    locked = _pick(retention, "is_locked", "isLocked")
    if locked is not True:
        raise ValueError("GCS backup bucket retention policy must be locked")

    operation_type = str(_pick(operation_record, "operationType", "operation_type") or "").upper()
    status = str(_pick(operation_record, "status", "state") or "").upper()
    if operation_type != "IMPORT" or status != "DONE":
        raise ValueError("Cloud SQL operation must be a completed IMPORT")
    if operation_record.get("error") not in (None, "", [], {}):
        error = operation_record.get("error")
        if not isinstance(error, Mapping) or any(v not in (None, "", [], {}) for v in error.values()):
            raise ValueError("Cloud SQL IMPORT operation reports provider errors")
    if operation_record.get("errors"):
        raise ValueError("Cloud SQL IMPORT operation reports provider errors")

    created_at = _timestamp(
        _pick(object_metadata, "time_created", "timeCreated"),
        "GCS object creation timestamp",
    )
    import_started = _timestamp(
        _pick(operation_record, "startTime", "start_time"),
        "Cloud SQL import startTime",
    )
    import_ended = _timestamp(
        _pick(operation_record, "endTime", "end_time"),
        "Cloud SQL import endTime",
    )
    retention_expiration = _pick(
        object_metadata,
        "retention_expiration_time",
        "retentionExpirationTime",
    )
    if retention_expiration is None:
        object_retention = _pick(object_metadata, "retention")
        if isinstance(object_retention, Mapping):
            retention_expiration = _pick(object_retention, "retain_until_time", "retainUntilTime")
    retention_ends = _timestamp(retention_expiration, "GCS object retention expiration timestamp")
    if not (created_at <= import_started <= import_ended < retention_ends):
        raise ValueError(
            "generation-pinned GCS object was not demonstrably retained from before import start through import completion"
        )

    object_name = _pick(object_metadata, "name")
    bucket_name = _pick(object_metadata, "bucket")
    if object_name and bucket_name:
        observed_uri = f"gs://{bucket_name}/{str(object_name).lstrip('/')}"
        if observed_uri != expected_uri:
            raise ValueError("GCS object metadata name/bucket does not match the pinned backup URI")

    return {
        "status": "PASS",
        "backup_reference": backup_reference,
        "backup_object_uri": expected_uri,
        "backup_generation": expected_generation,
        "backup_size_bytes": size,
        "backup_checksum": {"md5_hash": md5_hash, "crc32c_hash": crc32c_hash},
        "backup_object_created_at": created_at.isoformat(),
        "backup_retention_expires_at": retention_ends.isoformat(),
        "bucket_retention_policy_locked": True,
        "cloud_sql_operation_type": operation_type,
        "cloud_sql_operation_status": status,
        "cloud_sql_operation_start": import_started.isoformat(),
        "cloud_sql_operation_end": import_ended.isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-reference", required=True)
    parser.add_argument("--object-metadata", required=True)
    parser.add_argument("--bucket-metadata", required=True)
    parser.add_argument("--operation-record", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = verify_backup_binding(
            backup_reference=args.backup_reference,
            object_metadata=json.loads(Path(args.object_metadata).read_text(encoding="utf-8")),
            bucket_metadata=json.loads(Path(args.bucket_metadata).read_text(encoding="utf-8")),
            operation_record=json.loads(Path(args.operation_record).read_text(encoding="utf-8")),
        )
        Path(args.output).write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "reason": "GCS_BACKUP_BINDING_INVALID", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
