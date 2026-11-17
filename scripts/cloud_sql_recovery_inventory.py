"""Strict classification for Cloud SQL recovery backup inventory."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

SUPPORTED_POSTGRES_MAJOR_VERSIONS = frozenset({14, 15, 16, 17, 18})
BACKUP_SUFFIXES = (".sql", ".sql.gz", ".dump", ".backup", ".gz")
SOURCE_INSTANCE_METADATA_KEY = "atlas_source_instance_connection_name"
SOURCE_DATABASE_METADATA_KEY = "atlas_source_database"


def require_supported_postgresql_major(database_version: Any) -> int:
    """Return the PostgreSQL major version or reject non-PostgreSQL/unsupported engines."""
    if not isinstance(database_version, str):
        raise ValueError("Cloud SQL instance databaseVersion is missing or not a string")
    match = re.fullmatch(r"POSTGRES_(\d+)(?:_(\d+))?", database_version.strip().upper())
    if match is None:
        raise ValueError(
            f"Cloud SQL recovery target must use a supported PostgreSQL engine; "
            f"observed databaseVersion={database_version!r}"
        )
    major = int(match.group(1))
    if major not in SUPPORTED_POSTGRES_MAJOR_VERSIONS:
        raise ValueError(
            f"Cloud SQL PostgreSQL major version {major} is not in the supported "
            f"recovery set {sorted(SUPPORTED_POSTGRES_MAJOR_VERSIONS)}"
        )
    return major


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def classify_backup_objects(
    items: Iterable[dict[str, Any]],
    *,
    expected_source_instance: str,
    expected_source_database: str,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split backup-like GCS object versions into qualified and unverified inventories.

    A filename is only a discovery hint. A qualified candidate must be a live immutable
    generation with non-zero bytes, a checksum, an unexpired object-retention timestamp,
    and custom metadata matching the configured production source instance/database.
    This metadata match is source association, not proof that the dump can be restored.
    """
    if not expected_source_instance or not expected_source_database:
        raise ValueError("expected source instance and database must both be configured")
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    reference_time = reference_time.astimezone(timezone.utc)

    qualified: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []

    for item in items:
        name = item.get("name")
        if not isinstance(name, str) or not name.lower().endswith(BACKUP_SUFFIXES):
            continue

        reasons: list[str] = []
        generation = item.get("generation")
        try:
            generation_value = int(generation)
        except (TypeError, ValueError):
            generation_value = 0
        if generation_value <= 0:
            reasons.append("missing_or_invalid_generation")

        try:
            size_value = int(item.get("size"))
        except (TypeError, ValueError):
            size_value = 0
        if size_value <= 0:
            reasons.append("empty_or_invalid_size")

        if item.get("timeDeleted") or item.get("softDeleteTime") or item.get("hardDeleteTime"):
            reasons.append("not_a_live_object_generation")

        md5_hash = item.get("md5Hash")
        crc32c = item.get("crc32c")
        if not md5_hash and not crc32c:
            reasons.append("missing_checksum")

        retention_expiration = _parse_timestamp(item.get("retentionExpirationTime"))
        if retention_expiration is None:
            reasons.append("missing_or_invalid_retention_expiration")
        elif retention_expiration <= reference_time:
            reasons.append("retention_expired")

        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata.get(SOURCE_INSTANCE_METADATA_KEY) != expected_source_instance:
            reasons.append("source_instance_metadata_mismatch")
        if metadata.get(SOURCE_DATABASE_METADATA_KEY) != expected_source_database:
            reasons.append("source_database_metadata_mismatch")

        source_matches = not (
            "source_instance_metadata_mismatch" in reasons
            or "source_database_metadata_mismatch" in reasons
        )
        record = {
            "name": name,
            "generation": str(generation) if generation is not None else None,
            "size": str(item.get("size")) if item.get("size") is not None else None,
            "timeCreated": item.get("timeCreated"),
            "checksum_type": "md5" if md5_hash else ("crc32c" if crc32c else None),
            "retentionExpirationTime": item.get("retentionExpirationTime"),
            "source_instance_connection_name": metadata.get(SOURCE_INSTANCE_METADATA_KEY),
            "source_database": metadata.get(SOURCE_DATABASE_METADATA_KEY),
            "source_association": "matched_object_metadata" if source_matches else "unverified",
        }
        if reasons:
            unverified.append({**record, "reasons": reasons})
        else:
            qualified.append(record)

    return qualified, unverified
