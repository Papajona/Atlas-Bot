#!/usr/bin/env python3
"""Verify the exact approved sanitized SQL artifact pair before Cloud SQL import/audit."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


BACKUP_BUCKET = "atlas-staging-510915-db-backups"
EXPECTED_SOURCE_BACKUP_REFERENCE = (
    "gs://atlas-staging-510915-db-backups/"
    "atlas-supabase-pre-customer-paper-20261022T153023Z.dump#1792683068959304"
)
EXPECTED_SOURCE_PREPARED_SQL_SHA256 = (
    "47ffcc3ae6e86decb5b125eb20ce2a6b682204e9d368a18ec18ef3439f7e5eeb"
)
EXPECTED_SANITIZED_IMPORT_SHA256 = (
    "fa3b14ec305a9d87313c19a57a441879a4b9977f0e43cd8a349476398d59dfa4"
)
EXPECTED_SANITIZED_IMPORT_BYTES = 195036
_SANITIZED_REFERENCE_RE = re.compile(
    rf"^gs://{re.escape(BACKUP_BUCKET)}/sanitized-imports/[^#]+\.sql#([1-9][0-9]*)$"
)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sanitized_artifacts(
    *,
    source_backup_reference: str,
    source_prepared_sql_path: str | Path,
    sanitized_backup_reference: str,
    sanitized_sql_path: str | Path,
    imported_sql_path: str | Path | None = None,
) -> dict[str, Any]:
    """Require the one reviewed source/candidate pair and byte-identical imported SQL."""
    if source_backup_reference != EXPECTED_SOURCE_BACKUP_REFERENCE:
        raise ValueError("source backup reference is not the approved generation for this sanitized candidate")
    match = _SANITIZED_REFERENCE_RE.fullmatch(sanitized_backup_reference or "")
    if not match:
        raise ValueError(
            "sanitized backup reference must pin a .sql generation under the approved sanitized-imports/ prefix"
        )

    source_prepared_sha256 = sha256_file(source_prepared_sql_path)
    if source_prepared_sha256 != EXPECTED_SOURCE_PREPARED_SQL_SHA256:
        raise ValueError("prepared source SQL hash does not match the reviewed source backup")

    sanitized_path = Path(sanitized_sql_path)
    sanitized_size = sanitized_path.stat().st_size
    sanitized_sha256 = sha256_file(sanitized_path)
    if sanitized_size != EXPECTED_SANITIZED_IMPORT_BYTES:
        raise ValueError("sanitized import size does not match the reviewed staging candidate")
    if sanitized_sha256 != EXPECTED_SANITIZED_IMPORT_SHA256:
        raise ValueError("sanitized import SHA-256 does not match the reviewed staging candidate")

    imported_sha256 = None
    if imported_sql_path is not None:
        imported_sha256 = sha256_file(imported_sql_path)
        if imported_sha256 != sanitized_sha256:
            raise ValueError("immutable import object bytes differ from the reviewed sanitized SQL candidate")

    return {
        "status": "PASS",
        "source_backup_reference": source_backup_reference,
        "source_prepared_sql_sha256": source_prepared_sha256,
        "sanitized_backup_reference": sanitized_backup_reference,
        "sanitized_import_sha256": sanitized_sha256,
        "sanitized_import_bytes": sanitized_size,
        "imported_sql_sha256": imported_sha256,
        "sanitization_profile": "atlas-public-staging-scrub-20261118-v1",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-backup-reference", required=True)
    parser.add_argument("--source-prepared-sql", required=True)
    parser.add_argument("--sanitized-backup-reference", required=True)
    parser.add_argument("--sanitized-sql", required=True)
    parser.add_argument("--imported-sql")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        result = verify_sanitized_artifacts(
            source_backup_reference=args.source_backup_reference,
            source_prepared_sql_path=args.source_prepared_sql,
            sanitized_backup_reference=args.sanitized_backup_reference,
            sanitized_sql_path=args.sanitized_sql,
            imported_sql_path=args.imported_sql,
        )
        rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "reason": "SANITIZED_RESTORE_ARTIFACT_VERIFICATION_FAILED",
            "error_type": type(exc).__name__,
            "detail": str(exc),
        }, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
