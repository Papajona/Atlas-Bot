"""Classify retained GCS objects that can serve as original Cloud SQL backups."""
from __future__ import annotations

from typing import Any, Mapping


_SOURCE_BACKUP_SUFFIXES = (".sql.gz", ".sql", ".dump", ".backup", ".gz")


def is_source_backup_candidate(item: Mapping[str, Any]) -> bool:
    """Return true only for a real source-backup object, never a generated import copy.

    The recovery executor deliberately writes verified, create-only copies under
    the restore-imports/ prefix. Those files prove import bytes for an operation
    but are not independent source backups and must not satisfy recovery preflight.
    """
    name = item.get("name")
    if not isinstance(name, str) or not name or name.startswith("restore-imports/"):
        return False
    if not name.lower().endswith(_SOURCE_BACKUP_SUFFIXES):
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

    if not (item.get("md5Hash") or item.get("md5_hash") or item.get("crc32c") or item.get("crc32c_hash")):
        return False

    if item.get("timeDeleted") or item.get("time_deleted"):
        return False
    return True
