#!/usr/bin/env python3
"""Execute and verify a generation-pinned Cloud SQL SQL import into recovery only.

This is the write-capable counterpart to verify_staging_restore.py. It refuses
production targets, requires a locked GCS backup bucket and immutable generation,
scans the dump before import, waits for the provider operation, and verifies the
completed operation against the existing audit contracts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from scripts.verify_cloud_sql_backup_binding import verify_backup_binding
from scripts.verify_cloud_sql_sanitized_artifacts import verify_sanitized_artifacts
from scripts.verify_cloud_sql_supabase_source_provenance import verify_supabase_pg_dump_source
from scripts.verify_restore_operation_evidence import verify as verify_restore_operation


BACKUP_BUCKET = "atlas-staging-510915-db-backups"
_INSTANCE_RE = re.compile(r"^([a-z0-9-]+):([a-z0-9-]+):([a-z0-9-]+)$")
_BACKUP_RE = re.compile(
    rf"^gs://{re.escape(BACKUP_BUCKET)}/([^#]+)#([1-9][0-9]*)$"
)
_DATABASE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,62}$")
_OPERATION_RE = re.compile(r"^[A-Za-z0-9._-]{1,256}$")


def _pick(mapping: Mapping[str, Any], *keys: str) -> Any:
    """Return the first populated alias from provider JSON (snake_case or camelCase)."""
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _sql_import_suffix(path: str | Path) -> str:
    """Preserve the gzip marker required by Cloud SQL for compressed SQL imports."""
    with Path(path).open("rb") as handle:
        return ".sql.gz" if handle.read(2) == bytes((0x1F, 0x8B)) else ".sql"


def validate_restore_inputs(
    *,
    project_id: str,
    region: str,
    target_instance: str,
    target_database: str,
    backup_reference: str,
    source_supabase_project_ref: str = "",
    production_instance: str | None = None,
) -> tuple[str, str, str]:
    """Validate fixed staging project/region, separate target and pinned GCS object."""
    match = _INSTANCE_RE.fullmatch(target_instance or "")
    if not match:
        raise ValueError("recovery instance must be project:region:instance")
    target_project, target_region, instance_id = match.groups()
    if target_project != project_id:
        raise ValueError("recovery instance must belong to the configured staging project")
    if target_region != region:
        raise ValueError("recovery instance must belong to the configured staging region")
    if production_instance:
        if not _INSTANCE_RE.fullmatch(production_instance):
            raise ValueError("production Cloud SQL instance identity must be project:region:instance")
        if target_instance == production_instance:
            raise ValueError("recovery instance must be different from the production instance")
    elif source_supabase_project_ref != "wauaelzyugjuepmrewaw":
        raise ValueError(
            "source Supabase project ref must be explicitly configured and match the authenticated pg_dump source wauaelzyugjuepmrewaw"
        )
    if not _DATABASE_RE.fullmatch(target_database or ""):
        raise ValueError("recovery database name is invalid")
    backup_match = _BACKUP_RE.fullmatch(backup_reference or "")
    if not backup_match:
        raise ValueError(
            f"backup reference must use the approved recovery bucket gs://{BACKUP_BUCKET}/<object>#<generation>"
        )
    object_name, generation = backup_match.groups()
    if (
        not object_name
        or object_name.startswith("/")
        or ".." in object_name.split("/")
        or object_name.startswith("restore-imports/")
    ):
        raise ValueError("backup object path is invalid or points to an import-staging object")
    return instance_id, object_name, generation


def validate_sanitized_backup_reference(reference: str) -> tuple[str, str]:
    """Accept only a generation-pinned SQL file in the reviewed sanitized-import prefix."""
    match = _BACKUP_RE.fullmatch(reference or "")
    if not match:
        raise ValueError("sanitized backup reference must be generation-pinned in the approved recovery bucket")
    object_name, generation = match.groups()
    if not object_name.startswith("sanitized-imports/") or not object_name.endswith(".sql"):
        raise ValueError("sanitized backup must be a plain .sql object under sanitized-imports/")
    if ".." in object_name.split("/"):
        raise ValueError("sanitized backup object path is invalid")
    return object_name, generation


def _run_json(
    command: list[str],
    *,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    proc = run(command, capture_output=True, text=True, check=False)
    if proc.returncode:
        # Deliberately suppress provider output: it can contain resource metadata.
        raise RuntimeError(
            f"Google Cloud command failed (exit {proc.returncode}): {' '.join(command[:3])}"
        )
    try:
        document = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Google Cloud command did not return valid JSON") from exc
    if not isinstance(document, dict):
        raise RuntimeError("Google Cloud command returned an unexpected JSON shape")
    return document


def _utc_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"backup metadata is missing {label}")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"backup metadata has invalid {label}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"backup metadata {label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_backup_metadata(
    *,
    backup_reference: str,
    object_metadata: Mapping[str, Any],
    bucket_metadata: Mapping[str, Any],
    now: datetime | None = None,
) -> None:
    """Fail closed unless the exact generation is checksummed and retention-locked."""
    match = _BACKUP_RE.fullmatch(backup_reference or "")
    if not match:
        raise ValueError("backup reference is not generation-pinned")
    object_name, expected_generation = match.groups()
    if str(_pick(object_metadata, "name") or "") != object_name:
        raise ValueError("GCS object metadata name does not match the backup reference")
    if str(_pick(object_metadata, "generation") or "") != expected_generation:
        raise ValueError("GCS object metadata generation does not match the backup reference")
    if int(_pick(object_metadata, "size") or 0) <= 0:
        raise ValueError("GCS backup object is empty")
    if not (
        _pick(object_metadata, "md5_hash", "md5Hash")
        or _pick(object_metadata, "crc32c_hash", "crc32c")
    ):
        raise ValueError("GCS backup object has no provider checksum metadata")
    bucket = _pick(object_metadata, "bucket")
    if bucket and bucket != BACKUP_BUCKET:
        raise ValueError("GCS backup object is outside the approved recovery bucket")
    retention_policy = _pick(bucket_metadata, "retention_policy", "retentionPolicy")
    if not isinstance(retention_policy, Mapping) or _pick(retention_policy, "is_locked", "isLocked") is not True:
        raise ValueError("GCS recovery backup bucket retention policy must be locked")
    retention_expiration = _pick(object_metadata, "retention_expiration_time", "retentionExpirationTime")
    if not retention_expiration:
        retention = _pick(object_metadata, "retention")
        if isinstance(retention, Mapping):
            retention_expiration = _pick(retention, "retain_until_time", "retainUntilTime")
    expires = _utc_timestamp(retention_expiration, "retention expiration")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("clock must be timezone-aware")
    if expires <= current.astimezone(timezone.utc):
        raise ValueError("GCS backup object retention has already expired")

def _extract_operation_id(document: Mapping[str, Any]) -> str:
    value = document.get("name") or document.get("operationId") or document.get("operation_id")
    operation_id = str(value or "").strip()
    if not _OPERATION_RE.fullmatch(operation_id):
        raise ValueError("Cloud SQL import did not return a valid operation ID")
    return operation_id


def _has_operation_error(operation: Mapping[str, Any]) -> bool:
    error = operation.get("error")
    if error not in (None, "", [], {}):
        if not isinstance(error, Mapping):
            return True
        if any(value not in (None, "", [], {}) for value in error.values()):
            return True
    return bool(operation.get("errors"))


def _live_backup_generation(
    versions: Mapping[str, Any],
    *,
    object_name: str,
    instant: datetime,
) -> str:
    """Return the generation that was live at an instant, for this exact object name."""
    items = versions.get("items")
    if not isinstance(items, list):
        raise ValueError("GCS object-version listing is incomplete")
    candidates: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, Mapping) or str(item.get("name") or "") != object_name:
            continue
        created = _utc_timestamp(item.get("timeCreated"), "object version creation time")
        if created > instant:
            continue
        deleted_value = item.get("timeDeleted")
        if deleted_value:
            deleted = _utc_timestamp(deleted_value, "object version deletion time")
            if deleted <= instant:
                continue
        generation = str(item.get("generation") or "")
        if not generation.isdigit() or int(generation) <= 0:
            raise ValueError("GCS object-version listing contains an invalid generation")
        candidates.append((int(generation), generation))
    if not candidates:
        raise ValueError("no retained generation of the backup object was live before import")
    return max(candidates)[1]


def _list_object_versions(
    *,
    object_name: str,
    run: Callable[..., Any] = subprocess.run,
    open_url: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    token_proc = run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=False,
    )
    if token_proc.returncode or not token_proc.stdout.strip():
        raise RuntimeError("Google Cloud access token is unavailable for GCS generation audit")
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        query = {
            "versions": "true",
            "prefix": object_name,
            "maxResults": "1000",
            "fields": "items(name,generation,timeCreated,timeDeleted),nextPageToken",
        }
        if page_token:
            query["pageToken"] = page_token
        url = (
            "https://storage.googleapis.com/storage/v1/b/"
            + quote(BACKUP_BUCKET, safe="")
            + "/o?"
            + urlencode(query)
        )
        request = Request(url, headers={"Authorization": "Bearer " + token_proc.stdout.strip()})
        try:
            with open_url(request, timeout=30) as response:
                page = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("Could not retrieve the complete GCS object-version history") from exc
        if not isinstance(page, dict) or not isinstance(page.get("items", []), list):
            raise RuntimeError("GCS object-version API returned an invalid page")
        items.extend(page.get("items", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    return {"items": items}


def _wait_for_operation(
    *,
    operation_id: str,
    project_id: str,
    target_database: str,
    backup_object_uri: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
    run: Callable[..., Any] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        operation = _run_json(
            [
                "gcloud", "sql", "operations", "describe", operation_id,
                f"--project={project_id}", "--format=json",
            ],
            run=run,
        )
        status = str(operation.get("status") or "").upper()
        if status == "DONE":
            if _has_operation_error(operation):
                raise RuntimeError("Cloud SQL import operation completed with provider errors")
            if str(operation.get("operationType") or "").upper() != "IMPORT":
                raise ValueError("Cloud SQL operation is not an IMPORT")
            context = operation.get("importContext")
            if not isinstance(context, Mapping):
                raise ValueError("Cloud SQL operation omitted importContext")
            if str(context.get("database") or "") != target_database:
                raise ValueError("Cloud SQL import operation targeted a different database")
            imported_uri = str(context.get("uri") or "").split("#", 1)[0]
            if imported_uri != backup_object_uri:
                raise ValueError("Cloud SQL import operation used a different GCS object")
            return operation
        if status in {"PENDING", "RUNNING"}:
            if time.monotonic() >= deadline:
                raise TimeoutError("Cloud SQL import did not complete before the restore timeout")
            sleep(max(1, poll_interval_seconds))
            continue
        raise RuntimeError(f"Cloud SQL import ended in an unexpected provider state: {status or 'UNKNOWN'}")


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def execute_restore(
    *,
    project_id: str,
    region: str,
    target_instance: str,
    target_database: str,
    backup_reference: str,
    sanitized_backup_reference: str,
    expected_source_prepared_canonical_sha256: str,
    output_path: str,
    source_supabase_project_ref: str = "",
    production_instance: str | None = None,
    timeout_seconds: int = 14400,
    poll_interval_seconds: int = 15,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    instance_id, _source_object_name, source_generation = validate_restore_inputs(
        project_id=project_id,
        region=region,
        target_instance=target_instance,
        target_database=target_database,
        backup_reference=backup_reference,
        source_supabase_project_ref=source_supabase_project_ref,
        production_instance=production_instance,
    )
    _sanitized_object_name, _sanitized_generation = validate_sanitized_backup_reference(
        sanitized_backup_reference
    )

    instance = _run_json(
        [
            "gcloud", "sql", "instances", "describe", instance_id,
            f"--project={project_id}", "--format=json",
        ],
        run=run,
    )
    observed_instance = (
        f"{instance.get('project', '')}:{instance.get('region', '')}:{instance.get('name', '')}"
    )
    if observed_instance != target_instance or str(instance.get("state") or "") != "RUNNABLE":
        raise ValueError("configured recovery Cloud SQL instance identity/state does not match")

    database = _run_json(
        [
            "gcloud", "sql", "databases", "describe", target_database,
            f"--instance={instance_id}", f"--project={project_id}", "--format=json",
        ],
        run=run,
    )
    if str(database.get("name") or "") != target_database:
        raise ValueError("configured recovery database does not exist on the target instance")

    bucket_metadata = _run_json(
        ["gcloud", "storage", "buckets", "describe", f"gs://{BACKUP_BUCKET}", "--format=json"],
        run=run,
    )
    source_object_metadata = _run_json(
        ["gcloud", "storage", "objects", "describe", backup_reference, "--format=json"],
        run=run,
    )
    validate_backup_metadata(
        backup_reference=backup_reference,
        object_metadata=source_object_metadata,
        bucket_metadata=bucket_metadata,
    )
    if (
        str(_pick(source_object_metadata, "size") or "") != "659332"
        or _pick(source_object_metadata, "md5_hash", "md5Hash") != "kX13N5wvc5RE4MJ19hpt6w=="
        or _pick(source_object_metadata, "crc32c_hash", "crc32c") != "Ya71XA=="
    ):
        raise ValueError("source backup generation does not match the reviewed October 22 archive fingerprint")

    source_provenance = verify_supabase_pg_dump_source(
        source_backup_reference=backup_reference,
        source_project_ref=source_supabase_project_ref,
        source_database="postgres",
        object_metadata=source_object_metadata,
        bucket_metadata=bucket_metadata,
    )

    sanitized_object_metadata = _run_json(
        ["gcloud", "storage", "objects", "describe", sanitized_backup_reference, "--format=json"],
        run=run,
    )
    validate_backup_metadata(
        backup_reference=sanitized_backup_reference,
        object_metadata=sanitized_object_metadata,
        bucket_metadata=bucket_metadata,
    )

    with tempfile.TemporaryDirectory(prefix="atlas-cloud-sql-restore-") as temp_dir:
        source_backup_path = str(Path(temp_dir) / "source-recovery-backup")
        prepared_source_sql_path = str(Path(temp_dir) / "source-prepared.sql")
        sanitized_import_path = str(Path(temp_dir) / "sanitized-import.sql")
        dump_report = str(Path(temp_dir) / "sanitized-import-dump-verification.json")
        verified_import_path = str(Path(temp_dir) / "verified-import-copy.sql")

        # The source archive is converted offline only to prove the exact reviewed
        # archive identity. Its raw SQL is never used as the Cloud SQL import input.
        copy_proc = run(
            ["gcloud", "storage", "cp", backup_reference, source_backup_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if copy_proc.returncode:
            raise RuntimeError("Could not download the exact generation-pinned source backup")
        source_backup_sha256 = _sha256_file(source_backup_path)

        prepare_proc = run(
            [
                sys.executable,
                "scripts/prepare_cloud_sql_import_dump.py",
                "--input", source_backup_path,
                "--output", prepared_source_sql_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if prepare_proc.returncode:
            raise ValueError("source backup could not be converted into the reviewed SQL fingerprint")

        candidate_copy_proc = run(
            ["gcloud", "storage", "cp", sanitized_backup_reference, sanitized_import_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if candidate_copy_proc.returncode:
            raise RuntimeError("Could not download the exact generation-pinned sanitized SQL candidate")

        prepared_import_sha256 = _sha256_file(sanitized_import_path)
        candidate_evidence = verify_sanitized_artifacts(
            source_backup_reference=backup_reference,
            source_prepared_sql_path=prepared_source_sql_path,
            expected_source_prepared_canonical_sha256=expected_source_prepared_canonical_sha256,
            sanitized_backup_reference=sanitized_backup_reference,
            sanitized_sql_path=sanitized_import_path,
        )
        scan_proc = run(
            [
                sys.executable,
                "scripts/verify_cloud_sql_import_dump.py",
                "--input", sanitized_import_path,
                "--output", dump_report,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if scan_proc.returncode:
            raise ValueError("sanitized recovery SQL did not pass the Cloud SQL import safety scanner")

        import_suffix = _sql_import_suffix(sanitized_import_path)
        if import_suffix != ".sql":
            raise ValueError("reviewed sanitized recovery candidate must be uncompressed plain SQL")

        # Cloud SQL receives only the SHA-256-pinned sanitized candidate. The raw
        # source archive and its converted SQL are never uploaded as import objects.
        import_object_name = f"restore-imports/{prepared_import_sha256}-{uuid.uuid4().hex}{import_suffix}"
        import_object_uri = f"gs://{BACKUP_BUCKET}/{import_object_name}"
        upload_proc = run(
            [
                "gcloud", "storage", "cp", "--if-generation-match=0",
                sanitized_import_path, import_object_uri,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if upload_proc.returncode:
            raise RuntimeError("Could not create the immutable, create-only sanitized Cloud SQL import object")

        import_object_metadata = _run_json(
            ["gcloud", "storage", "objects", "describe", import_object_uri, "--format=json"],
            run=run,
        )
        import_generation = str(_pick(import_object_metadata, "generation") or "")
        if not import_generation.isdigit() or int(import_generation) <= 0:
            raise ValueError("immutable sanitized import object has no valid generation")
        import_reference = f"{import_object_uri}#{import_generation}"
        validate_backup_metadata(
            backup_reference=import_reference,
            object_metadata=import_object_metadata,
            bucket_metadata=bucket_metadata,
        )

        # Read back the exact uploaded generation and compare it to the approved
        # sanitized artifact before any Cloud SQL import request is submitted.
        verify_copy_proc = run(
            ["gcloud", "storage", "cp", import_reference, verified_import_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if verify_copy_proc.returncode:
            raise RuntimeError("Could not read back the immutable sanitized import object")
        import_sha256 = _sha256_file(verified_import_path)
        verify_sanitized_artifacts(
            source_backup_reference=backup_reference,
            source_prepared_sql_path=prepared_source_sql_path,
            expected_source_prepared_canonical_sha256=expected_source_prepared_canonical_sha256,
            sanitized_backup_reference=sanitized_backup_reference,
            sanitized_sql_path=sanitized_import_path,
            imported_sql_path=verified_import_path,
        )

        import_versions_before = _list_object_versions(object_name=import_object_name, run=run)
        live_import_generation = _live_backup_generation(
            import_versions_before,
            object_name=import_object_name,
            instant=datetime.now(timezone.utc),
        )
        if live_import_generation != import_generation:
            raise ValueError("immutable import object generation is not live immediately before Cloud SQL import")

        launch_proc = run(
            [
                "gcloud", "sql", "import", "sql", instance_id, import_object_uri,
                f"--database={target_database}",
                f"--project={project_id}",
                "--async", "--quiet", "--format=json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if launch_proc.returncode:
            raise RuntimeError("Cloud SQL import request was rejected; no restore PASS is claimed")
        try:
            launch_document = json.loads(launch_proc.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Cloud SQL import request did not return its operation record") from exc
        if not isinstance(launch_document, Mapping):
            raise RuntimeError("Cloud SQL import request returned an unexpected result")
        operation_id = _extract_operation_id(launch_document)

        operation = _wait_for_operation(
            operation_id=operation_id,
            project_id=project_id,
            target_database=target_database,
            backup_object_uri=import_object_uri,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            run=run,
        )
        import_versions = _list_object_versions(object_name=import_object_name, run=run)
        backup_binding = verify_backup_binding(
            backup_reference=import_reference,
            object_metadata=import_object_metadata,
            bucket_metadata=bucket_metadata,
            object_versions=import_versions,
            operation_record=operation,
        )
        restore_evidence = verify_restore_operation(
            operation,
            operation_id=operation_id,
            backup_reference=import_reference,
            target_kind="cloud_sql",
            target_database=target_database,
            target_instance_connection_name=target_instance,
        )

    result = {
        "status": "IMPORT_COMPLETE",
        "operation_id": operation_id,
        "target_instance_connection_name": target_instance,
        "target_database": target_database,
        "backup_reference": backup_reference,
        "backup_generation": source_generation,
        "source_backup_reference": backup_reference,
        "source_backup_sha256": source_backup_sha256,
        "source_supabase_project_ref": source_supabase_project_ref,
        "source_provenance": source_provenance,
        "source_prepared_sql_sha256": candidate_evidence["source_prepared_sql_sha256"],
        "source_prepared_sql_canonical_sha256": candidate_evidence["source_prepared_sql_canonical_sha256"],
        "expected_source_prepared_sql_canonical_sha256": candidate_evidence["expected_source_prepared_sql_canonical_sha256"],
        "sanitized_backup_reference": sanitized_backup_reference,
        "sanitized_import_sha256": candidate_evidence["sanitized_import_sha256"],
        "sanitization_profile": candidate_evidence["sanitization_profile"],
        "prepared_import_sha256": prepared_import_sha256,
        "import_object_reference": import_reference,
        "import_object_sha256": import_sha256,
        "backup_binding": backup_binding,
        "restore_evidence": restore_evidence,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    Path(output_path).write_text(
        json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--target-instance", required=True)
    parser.add_argument("--source-supabase-project-ref", required=True)
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--backup-reference", required=True)
    parser.add_argument("--sanitized-backup-reference", required=True)
    parser.add_argument("--expected-source-prepared-canonical-sha256", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=14400)
    parser.add_argument("--poll-interval-seconds", type=int, default=15)
    args = parser.parse_args()
    try:
        if args.timeout_seconds < 60 or args.poll_interval_seconds < 1:
            raise ValueError("restore timeout and poll interval must be positive and bounded")
        result = execute_restore(
            project_id=args.project_id,
            region=args.region,
            target_instance=args.target_instance,
            target_database=args.target_database,
            backup_reference=args.backup_reference,
            sanitized_backup_reference=args.sanitized_backup_reference,
            expected_source_prepared_canonical_sha256=args.expected_source_prepared_canonical_sha256,
            source_supabase_project_ref=args.source_supabase_project_ref,
            output_path=args.output,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
        print(json.dumps({
            "status": result["status"],
            "operation_id": result["operation_id"],
            "target_instance_connection_name": result["target_instance_connection_name"],
            "target_database": result["target_database"],
            "source_supabase_project_ref": result["source_supabase_project_ref"],
            "source_provenance": result["source_provenance"],
            "backup_reference": result["backup_reference"],
            "backup_generation": result["backup_generation"],
            "import_object_reference": result["import_object_reference"],
            "source_backup_sha256": result["source_backup_sha256"],
            "source_prepared_sql_sha256": result["source_prepared_sql_sha256"],
            "source_prepared_sql_canonical_sha256": result["source_prepared_sql_canonical_sha256"],
            "expected_source_prepared_sql_canonical_sha256": result["expected_source_prepared_sql_canonical_sha256"],
            "sanitized_backup_reference": result["sanitized_backup_reference"],
            "sanitized_import_sha256": result["sanitized_import_sha256"],
            "sanitization_profile": result["sanitization_profile"],
            "prepared_import_sha256": result["prepared_import_sha256"],
        }, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "reason": "CLOUD_SQL_RESTORE_EXECUTION_FAILED",
            "error_type": type(exc).__name__,
            "detail": str(exc),
        }, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
