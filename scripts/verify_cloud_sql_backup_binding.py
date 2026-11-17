#!/usr/bin/env python3
"""Bind a Cloud SQL IMPORT to the exact GCS generation live for the import interval."""
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


def _live_generation_at(
    versions: list[Mapping[str, Any]],
    *,
    object_name: str,
    instant: datetime,
    label: str,
) -> str:
    candidates: list[tuple[int, str]] = []
    for version in versions:
        if str(_pick(version, "name") or "") != object_name:
            continue
        created = _timestamp(_pick(version, "timeCreated", "time_created"), "object version timeCreated")
        if created > instant:
            continue
        deleted_value = _pick(version, "timeDeleted", "time_deleted")
        if deleted_value:
            deleted = _timestamp(deleted_value, "object version timeDeleted")
            if deleted <= instant:
                continue
        generation = str(_pick(version, "generation") or "")
        if not generation.isdigit() or int(generation) <= 0:
            raise ValueError("GCS object versions contain a missing or invalid generation")
        candidates.append((int(generation), generation))
    if not candidates:
        raise ValueError(f"no retained generation of the backup object was live at import {label}")
    # Cloud Storage generations are monotonically increasing for an object name.
    return max(candidates)[1]


def verify_backup_binding(
    *,
    backup_reference: str,
    object_metadata: Any,
    bucket_metadata: Any,
    object_versions: Any,
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
    if not isinstance(object_versions, Mapping) or not isinstance(object_versions.get("items"), list):
        raise ValueError("GCS object-version listing must include a complete items array")

    bucket_name, object_name = expected_uri[5:].split("/", 1)
    observed_generation = str(_pick(object_metadata, "generation") or "")
    if observed_generation != expected_generation:
        raise ValueError("generation-qualified GCS describe did not return the pinned backup generation")

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

    metadata_name = str(_pick(object_metadata, "name") or "")
    metadata_bucket = str(_pick(object_metadata, "bucket") or "")
    if metadata_name and metadata_name != object_name:
        raise ValueError("generation-qualified GCS object name does not match the backup reference")
    if metadata_bucket and metadata_bucket != bucket_name:
        raise ValueError("generation-qualified GCS object bucket does not match the backup reference")

    retention = _pick(bucket_metadata, "retention_policy", "retentionPolicy")
    if not isinstance(retention, Mapping):
        raise ValueError("approved backup bucket has no retention policy metadata")
    if _pick(retention, "is_locked", "isLocked") is not True:
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
            "pinned GCS object was not demonstrably retained from before import start through import completion"
        )

    versions = object_versions["items"]
    if any(not isinstance(item, Mapping) for item in versions):
        raise ValueError("GCS object-version listing contains a malformed version entry")
    exact_versions = [
        item for item in versions
        if str(_pick(item, "name") or "") == object_name
        and str(_pick(item, "generation") or "") == expected_generation
    ]
    if len(exact_versions) != 1:
        raise ValueError("GCS object-version listing does not contain the exact pinned generation exactly once")

    live_at_start = _live_generation_at(
        versions, object_name=object_name, instant=import_started, label="start"
    )
    live_at_end = _live_generation_at(
        versions, object_name=object_name, instant=import_ended, label="completion"
    )
    if live_at_start != expected_generation or live_at_end != expected_generation:
        raise ValueError(
            "pinned GCS generation was not the live object generation for the full Cloud SQL import interval"
        )

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
        "live_generation_at_import_start": live_at_start,
        "live_generation_at_import_end": live_at_end,
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
    parser.add_argument("--object-versions", required=True)
    parser.add_argument("--operation-record", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = verify_backup_binding(
            backup_reference=args.backup_reference,
            object_metadata=json.loads(Path(args.object_metadata).read_text(encoding="utf-8")),
            bucket_metadata=json.loads(Path(args.bucket_metadata).read_text(encoding="utf-8")),
            object_versions=json.loads(Path(args.object_versions).read_text(encoding="utf-8")),
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
