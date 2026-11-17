#!/usr/bin/env python3
"""Verify authenticated provider-native Supabase or Cloud SQL restore evidence."""
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
_GENERATION_BACKUP_RE = re.compile(r"^(gs://[^/]+/.+)#([1-9][0-9]*)$")
_TARGET_LINK_RE = re.compile(r"(?:^|/)projects/([^/]+)/instances/([^/?#]+)(?:$|[/?#])")


def _values(record: Mapping[str, Any], keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
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


def _backup_values(record: Mapping[str, Any]) -> list[str]:
    values = _values(record, _BACKUP_KEYS)
    context = record.get("importContext", record.get("import_context"))
    if isinstance(context, Mapping):
        values.extend(_values(context, ("uri", "backup_reference", "backupReference")))
    return values


def _has_target_identity(record: Mapping[str, Any], target_kind: str) -> bool:
    if target_kind == "supabase":
        return bool(_values(record, _SUPABASE_TARGET_KEYS))
    if target_kind == "cloud_sql":
        if _values(record, _CLOUD_SQL_INSTANCE_KEYS):
            return True
        has_native_pair = bool(_values(record, _CLOUD_SQL_PROJECT_KEYS)) and bool(_values(record, _CLOUD_SQL_ID_KEYS))
        return has_native_pair or bool(_values(record, ("targetLink", "target_link")))
    raise ValueError("target kind must be supabase or cloud_sql")


def _record(document: Any, target_kind: str) -> Mapping[str, Any]:
    """Select a complete provider record, recursively traversing known wrappers."""
    if not isinstance(document, Mapping):
        raise ValueError("provider restore evidence must be a JSON object")

    def complete(record: Mapping[str, Any]) -> bool:
        return (
            bool(_values(record, _OPERATION_ID_KEYS))
            and bool(_backup_values(record))
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


def _cloud_sql_instance(record: Mapping[str, Any], expected_instance: str) -> str:
    if not _CLOUD_SQL_INSTANCE_RE.fullmatch(expected_instance or ""):
        raise ValueError("expected Cloud SQL target must be a project:region:instance connection name")
    expected_project, expected_region, expected_id = expected_instance.split(":", 2)

    aliases = _values(record, _CLOUD_SQL_INSTANCE_KEYS)
    projects = _values(record, _CLOUD_SQL_PROJECT_KEYS)
    instance_ids = _values(record, _CLOUD_SQL_ID_KEYS)
    regions = _values(record, _CLOUD_SQL_REGION_KEYS)
    links = _values(record, ("targetLink", "target_link"))

    if projects or instance_ids:
        if not projects or not instance_ids:
            raise ValueError("provider evidence contains an incomplete native Cloud SQL target identity")
        observed_project = _consistent("Cloud SQL target project", projects)
        observed_id = _consistent("Cloud SQL target instance", instance_ids)
        if observed_project != expected_project or observed_id != expected_id:
            raise ValueError("provider evidence contains conflicting Cloud SQL target project/instance values")
    if regions and _consistent("Cloud SQL target region", regions) != expected_region:
        raise ValueError("provider evidence target region does not match the configured Cloud SQL target")

    for link in links:
        match = _TARGET_LINK_RE.search(link)
        if not match:
            raise ValueError("provider evidence contains an unrecognized Cloud SQL targetLink")
        if match.group(1) != expected_project or match.group(2) != expected_id:
            raise ValueError("provider evidence targetLink does not match the configured Cloud SQL target")

    for alias in aliases:
        if alias != expected_instance:
            raise ValueError("provider evidence target instance alias does not match the configured Cloud SQL target")
    if not (aliases or projects or instance_ids or links):
        raise ValueError("provider evidence does not identify a Cloud SQL target instance")
    return expected_instance


def _gcs_uri_parts(value: str) -> tuple[str, str | None]:
    if not value.startswith("gs://"):
        raise ValueError("Cloud SQL import evidence must identify a GCS backup object")
    object_uri, separator, generation = value.partition("#")
    if separator and not re.fullmatch(r"[1-9][0-9]*", generation):
        raise ValueError("Cloud SQL GCS generation suffix must be a positive integer")
    return object_uri, generation if separator else None


def _cloud_sql_backup(record: Mapping[str, Any], expected_reference: str) -> tuple[str, str, str, bool]:
    match = _GENERATION_BACKUP_RE.fullmatch(expected_reference or "")
    if not match:
        raise ValueError("Cloud SQL backup reference must pin an immutable GCS object generation using #<generation>")
    expected_object_uri, expected_generation = match.groups()
    observed_values = _backup_values(record)
    if not observed_values:
        raise ValueError("provider evidence does not identify the source GCS backup")
    normalized_objects: list[str] = []
    observed_generations: list[str] = []
    native_import_uris: list[str] = []
    context = record.get("importContext", record.get("import_context"))
    if isinstance(context, Mapping):
        native_import_uris = _values(context, ("uri",))
    for value in observed_values:
        object_uri, generation = _gcs_uri_parts(value)
        normalized_objects.append(object_uri)
        if generation is not None:
            observed_generations.append(generation)
    if _consistent("backup object URI", normalized_objects) != expected_object_uri:
        raise ValueError("provider evidence contains conflicting backup reference values")
    if observed_generations and _consistent("backup generation", observed_generations) != expected_generation:
        raise ValueError("provider evidence contains conflicting backup reference generations")
    native_import_uri_has_generation = False
    for native_uri in native_import_uris:
        native_object_uri, native_generation = _gcs_uri_parts(native_uri)
        if native_object_uri != expected_object_uri:
            raise ValueError("provider evidence contains conflicting backup reference values")
        if native_generation is not None:
            native_import_uri_has_generation = True
            if native_generation != expected_generation:
                raise ValueError("provider evidence contains conflicting backup reference generations")
    # The native Cloud SQL import URI may be unversioned or generation-qualified.
    # The workflow independently proves which generation was live for the full import interval.
    return expected_reference, expected_object_uri, expected_generation, native_import_uri_has_generation




def _cloud_sql_database(record: Mapping[str, Any], expected_database: str) -> str:
    context = record.get("importContext", record.get("import_context"))
    if not isinstance(context, Mapping):
        raise ValueError("Cloud SQL import evidence does not identify importContext.database")
    observed = _consistent(
        "Cloud SQL import database",
        _values(context, ("database", "databaseName", "database_name")),
    )
    if not expected_database or not expected_database.strip():
        raise ValueError("expected Cloud SQL database must be explicitly configured")
    if not observed:
        raise ValueError("Cloud SQL import evidence does not identify importContext.database")
    if observed != expected_database.strip():
        raise ValueError("Cloud SQL import database does not match the configured Cloud SQL database")
    return observed


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
    target_database: str = "",
) -> dict[str, Any]:
    if target_kind not in {"supabase", "cloud_sql"}:
        raise ValueError("target kind must be supabase or cloud_sql")
    record = _record(document, target_kind)
    observed_operation = _first(record, *_OPERATION_ID_KEYS)
    status = _first(record, "status", "state", "phase").upper()

    if not operation_id.strip() or observed_operation != operation_id.strip():
        raise ValueError("provider evidence operation ID does not match the requested restore")
    if status not in _SUCCESS:
        raise ValueError("provider evidence does not show a successful terminal restore status")

    if target_kind == "supabase":
        observed_backup = _consistent("backup reference", _backup_values(record))
        if not backup_reference.strip() or observed_backup != backup_reference.strip():
            raise ValueError("provider evidence backup reference does not match the restored backup")
        observed_target = _consistent("Supabase target project", _values(record, _SUPABASE_TARGET_KEYS))
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

    observed_instance = _cloud_sql_instance(record, target_instance_connection_name)
    pinned_reference, object_uri, generation, native_import_uri_has_generation = _cloud_sql_backup(record, backup_reference)
    operation_type = _first(record, "operationType", "operation_type").upper()
    if operation_type != "IMPORT":
        raise ValueError("Cloud SQL restore evidence must identify an IMPORT operation")
    if _has_provider_error(record):
        raise ValueError("Cloud SQL import operation reports provider errors")
    observed_database = _cloud_sql_database(record, target_database)

    return {
        "status": "PASS",
        "restore_operation_id": observed_operation,
        "backup_reference": pinned_reference,
        "backup_object_uri": object_uri,
        "backup_generation": generation,
        "native_import_uri_has_generation": native_import_uri_has_generation,
        "target_kind": "cloud_sql",
        "target_instance_connection_name": observed_instance,
        "target_database": observed_database,
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
    parser.add_argument("--target-database", default="")
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
            target_database=args.target_database,
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
