"""Classify retained GCS object generations that can serve as original Cloud SQL backups."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

SUPPORTED_POSTGRES_MAJOR_VERSIONS = frozenset({14, 15, 16, 17, 18})
BACKUP_SUFFIXES = (".sql.gz", ".sql", ".dump", ".backup", ".gz")
SOURCE_INSTANCE_METADATA_KEY = "atlas_source_instance_connection_name"
SOURCE_DATABASE_METADATA_KEY = "atlas_source_database"


def is_source_backup_candidate(item: Mapping[str, Any]) -> bool:
    """Recognize retrievable source backup generations, excluding generated import copies.

    GCS sets timeDeleted when a newer generation becomes live. Older generations
    remain independently addressable by generation and may still be retained.
    """
    name = item.get("name")
    if not isinstance(name, str) or not name or name.startswith("restore-imports/"):
        return False
    if not name.lower().endswith(BACKUP_SUFFIXES):
        return False

    generation = item.get("generation")
    if isinstance(generation, bool) or not str(generation or "").isdigit():
        return False
    if int(generation) <= 0:
        return False

    try:
        size = int(item.get("size") or 0)
    except (TypeError, ValueError):
        return False
    if size <= 0:
        return False

    return bool(
        item.get("md5Hash") or item.get("md5_hash")
        or item.get("crc32c") or item.get("crc32c_hash")
    )


def require_supported_postgresql_major(database_version: Any) -> int:
    """Return the Cloud SQL PostgreSQL major version or reject unsupported engines."""
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
    """Split backup-like generations into source-associated and unverified inventories.

    A noncurrent generation with timeDeleted remains eligible when it is still
    present in the GCS versions listing. Soft-deleted generations are not included
    because this preflight does not perform the separate soft-delete restore step.
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
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if (
            not isinstance(name, str)
            or not name.lower().endswith(BACKUP_SUFFIXES)
            or name.startswith("restore-imports/")
        ):
            continue

        reasons: list[str] = []
        generation = item.get("generation")
        if isinstance(generation, bool) or not str(generation or "").isdigit() or int(generation or 0) <= 0:
            reasons.append("missing_or_invalid_generation")

        try:
            size_value = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size_value = 0
        if size_value <= 0:
            reasons.append("empty_or_invalid_size")

        if item.get("softDeleteTime") or item.get("hardDeleteTime"):
            reasons.append("soft_or_hard_deleted_generation")

        md5_hash = item.get("md5Hash") or item.get("md5_hash")
        crc32c = item.get("crc32c") or item.get("crc32c_hash")
        if not md5_hash and not crc32c:
            reasons.append("missing_checksum")

        retention_expiration = _parse_timestamp(
            item.get("retentionExpirationTime") or item.get("retention_expiration_time")
        )
        if retention_expiration is None:
            reasons.append("missing_or_invalid_retention_expiration")
        elif retention_expiration <= reference_time:
            reasons.append("retention_expired")

        metadata = item.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        if metadata.get(SOURCE_INSTANCE_METADATA_KEY) != expected_source_instance:
            reasons.append("source_instance_metadata_mismatch")
        if metadata.get(SOURCE_DATABASE_METADATA_KEY) != expected_source_database:
            reasons.append("source_database_metadata_mismatch")

        source_matches = not any(
            reason in reasons
            for reason in (
                "source_instance_metadata_mismatch",
                "source_database_metadata_mismatch",
            )
        )
        record = {
            "name": name,
            "generation": str(generation) if generation is not None else None,
            "size": str(item.get("size")) if item.get("size") is not None else None,
            "timeCreated": item.get("timeCreated"),
            "timeDeleted": item.get("timeDeleted"),
            "checksum_type": "md5" if md5_hash else ("crc32c" if crc32c else None),
            "retentionExpirationTime": (
                item.get("retentionExpirationTime") or item.get("retention_expiration_time")
            ),
            "source_instance_connection_name": metadata.get(SOURCE_INSTANCE_METADATA_KEY),
            "source_database": metadata.get(SOURCE_DATABASE_METADATA_KEY),
            "source_association": "matched_object_metadata" if source_matches else "unverified",
        }
        if reasons:
            unverified.append({**record, "reasons": reasons})
        else:
            qualified.append(record)

    return qualified, unverified
