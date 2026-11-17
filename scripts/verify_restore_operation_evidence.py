#!/usr/bin/env python3
"""Verify provider-native evidence for a Supabase or Cloud SQL restore operation."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

_SUCCESS = {"COMPLETE", "COMPLETED", "SUCCEEDED", "SUCCESS", "DONE", "RESTORED"}
_WRAPPERS = ("restore_operation", "restoreOperation", "operation", "data", "result")
_OPERATION_ID_KEYS = ("restore_operation_id", "restoreOperationId", "operation_id", "operationId", "name", "id")
_BACKUP_KEYS = ("backup_reference", "backupReference", "source_backup_id", "sourceBackupId", "backup_id", "backupId", "uri")
_SUPABASE_TARGET_KEYS = ("target_supabase_project_ref", "target_project_ref", "targetProjectRef", "restored_project_ref")
_CLOUD_SQL_INSTANCE_KEYS = ("target_instance_connection_name", "instance_connection_name", "instanceConnectionName", "target_instance", "instance")
_CLOUD_SQL_PROJECT_KEYS = ("targetProject", "target_project", "project")
_CLOUD_SQL_REGION_KEYS = ("region", "targetRegion", "target_region")
_CLOUD_SQL_ID_KEYS = ("targetId", "target_id", "instanceName", "instance_name")
_CLOUD_SQL_INSTANCE_RE = re.compile(r"^[a-z0-9-]+:[a-z0-9-]+:[a-z0-9-]+$")
_GENERATION_BACKUP_RE = re.compile(r"^gs://[^/]+/.+#([1-9][0-9]*)$")


def _values(record: Mapping[str, Any], keys: tuple[str, ...]) -> list[str]:
    values = []
    for key in keys:
        value = record.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            values.append(str(value).strip())
    return values


def _consistent(label: str, values: list[str]) -> str:
    distinct = list(dict.fromkeys(values))
    if len(distinct) > 1:
        raise ValueError(f"provider evidence contains conflicting {label} values")
    return distinct[0] if distinct else ""


def _cloud_sql_instance(record: Mapping[str, Any], *, required: bool) -> str:
    candidates = _values(record, _CLOUD_SQL_INSTANCE_KEYS)
    project = _values(record, _CLOUD_SQL_PROJECT_KEYS)
    region = _values(record, _CLOUD_SQL_REGION_KEYS)
    instance_id = _values(record, _CLOUD_SQL_ID_KEYS)
    native_parts_present = bool(project or region or instance_id)
    if native_parts_present:
        if not project or not region or not instance_id:
            raise ValueError("provider evidence contains an incomplete native Cloud SQL target identity")
        native = ":".join((
            _consistent("Cloud SQL target project", project),
            _consistent("Cloud SQL target region", region),
            _consistent("Cloud SQL target instance", instance_id),
        ))
        candidates.append(native)
    observed = _consistent("Cloud SQL target instance", candidates)
    if required and not observed:
        raise ValueError("provider evidence does not identify a Cloud SQL target instance")
    return observed


def _backup_reference(record: Mapping[str, Any], *, required: bool) -> str:
    candidates = _values(record, _BACKUP_KEYS)
    context = record.get("importContext", record.get("import_context"))
    if isinstance(context, Mapping):
        candidates.extend(_values(context, ("uri", "backup_reference", "backupReference")))
    observed = _consistent("backup reference", candidates)
    if required and not observed:
        raise ValueError("provider evidence does not identify the source backup")
    return observed


def _has_target_identity(record: Mapping[str, Any], target_kind: str) -> bool:
    if target_kind == "supabase":
        return bool(_values(record, _SUPABASE_TARGET_KEYS))
    if target_kind == "cloud_sql":
        return bool(_values(record, _CLOUD_SQL_INSTANCE_KEYS)) or (
            bool(_values(record, _CLOUD_SQL_PROJECT_KEYS))
            and bool(_values(record, _CLOUD_SQL_REGION_KEYS))
            and bool(_values(record, _CLOUD_SQL_ID_KEYS))
        )
    raise ValueError("target kind must be supabase or cloud_sql")


def _record(document: Any, target_kind: str) -> Mapping[str, Any]:
    """Select a complete provider record, traversing supported response wrappers."""
    if not isinstance(document, Mapping):
        raise ValueError("provider restore evidence must be a JSON object")

    def complete(record: Mapping[str, Any]) -> bool:
        return (
            bool(_values(record, _OPERATION_ID_KEYS))
            and bool(_backup_reference(record, required=False))
            and bool(_values(record, ("status", "state", "phase")))
            and _has_target_identity(record, target_kind)
        )

    if complete(document):
        return document
    for key in _WRAPPERS:
        value = document.get(key)
        if isinstance(value, Mapping):
            nested = _record(value, target_kind)
            if complete(nested):
                return nested
    return document


def _first(record: Mapping[str, Any], *keys: str) -> str:
    return _consistent("operation ID", _values(record, tuple(keys)))


def _has_provider_error(record: Mapping[str, Any]) -> bool:
    if record.get("errors"):
        return True
    error = record.get("error")
    if error in (None, "", [], {}):
        return False
    if isinstance(error, Mapping):
        nested_errors = error.get("errors")
        if nested_errors:
            return True
        return any(value not in (None, "", [], {}) for key, value in error.items() if key != "errors")
    return bool(error)


def verify(
    document: Any,
    *,
    operation_id: str,
    backup_reference: str,
    target_kind: str = "supabase",
    target_project_ref: str = "",
    target_instance_connection_name: str = "",
) -> dict[str, Any]:
    if target_kind not in {"supabase", "cloud_sql"}:
        raise ValueError("target kind must be supabase or cloud_sql")
    record = _record(document, target_kind)
    observed_operation = _first(record, *_OPERATION_ID_KEYS)
    observed_backup = _backup_reference(record, required=True)
    status = _first(record, "status", "state", "phase").upper()

    if not operation_id.strip() or observed_operation != operation_id.strip():
        raise ValueError("provider evidence operation ID does not match the requested restore")
    if not backup_reference.strip() or observed_backup != backup_reference.strip():
        raise ValueError("provider evidence backup reference does not match the restored backup")
    if status not in _SUCCESS:
        raise ValueError("provider evidence does not show a successful terminal restore status")

    if target_kind == "supabase":
        observed_target = _consistent(
            "Supabase target project", _values(record, _SUPABASE_TARGET_KEYS)
        )
        if not re.fullmatch(r"[a-z0-9]{20}", target_project_ref or ""):
            raise ValueError("expected target staging project ref is invalid")
        if observed_target != target_project_ref:
            raise ValueError("provider evidence target does not match the configured Supabase project")
        return {
            "status": "PASS",
            "restore_operation_id": observed_operation,
            "backup_reference": observed_backup,
            "target_kind": "supabase",
            "target_supabase_project_ref": observed_target,
            "provider_status": status,
        }

    observed_instance = _cloud_sql_instance(record, required=True)
    if not _CLOUD_SQL_INSTANCE_RE.fullmatch(target_instance_connection_name or ""):
        raise ValueError("expected Cloud SQL target must be a project:region:instance connection name")
    if observed_instance != target_instance_connection_name:
        raise ValueError("provider evidence target does not match the configured Cloud SQL instance connection name")

    operation_type = _first(record, "operationType", "operation_type").upper()
    if operation_type != "IMPORT":
        raise ValueError("Cloud SQL restore evidence must identify an IMPORT operation")
    if _has_provider_error(record):
        raise ValueError("Cloud SQL import operation reports provider errors")

    generation_match = _GENERATION_BACKUP_RE.fullmatch(backup_reference)
    if not generation_match:
        raise ValueError("Cloud SQL backup reference must pin an immutable GCS object generation using #<generation>")
    return {
        "status": "PASS",
        "restore_operation_id": observed_operation,
        "backup_reference": observed_backup,
        "backup_generation": generation_match.group(1),
        "target_kind": "cloud_sql",
        "target_instance_connection_name": observed_instance,
        "operation_type": operation_type,
        "provider_status": status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--backup-reference", required=True)
    parser.add_argument("--target-kind", choices=("supabase", "cloud_sql"), default="supabase")
    parser.add_argument("--target-project-ref", default="")
    parser.add_argument("--target-instance-connection-name", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        raw = Path(args.input).read_bytes()
        if not raw:
            raise ValueError("provider restore evidence file is empty")
        result = verify(
            json.loads(raw.decode("utf-8-sig")),
            operation_id=args.operation_id,
            backup_reference=args.backup_reference,
            target_kind=args.target_kind,
            target_project_ref=args.target_project_ref,
            target_instance_connection_name=args.target_instance_connection_name,
        )
        result["evidence_sha256"] = hashlib.sha256(raw).hexdigest()
        result["source_file_bytes"] = len(raw)
        Path(args.output).write_text(
            json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": "RESTORE_OPERATION_EVIDENCE_INVALID",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
