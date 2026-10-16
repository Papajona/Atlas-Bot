#!/usr/bin/env python3
"""Verify a fetched, machine-readable restore-operation record against expected identity.

The input must be the provider's actual operation record or API response, not a manually
written PASS note. It is only evidence that the claimed restore operation matches its
ID, source backup, target staging project, and successful terminal status; the separate
read-only database audit checks the restored schema and ledger invariants.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUCCESS = {"COMPLETE", "COMPLETED", "SUCCEEDED", "SUCCESS", "DONE", "RESTORED"}


def _record(document: Any) -> Mapping[str, Any]:
    if not isinstance(document, Mapping):
        raise ValueError("provider restore evidence must be a JSON object")
    for key in ("restore_operation", "restoreOperation", "operation", "data", "result"):
        value = document.get(key)
        if isinstance(value, Mapping):
            # API responses can wrap the operation one or more times.
            try:
                return _record(value)
            except ValueError:
                pass
    return document


def _first(record: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()
    return ""


def verify(document: Any, *, operation_id: str, backup_reference: str,
           target_project_ref: str) -> dict[str, Any]:
    record = _record(document)
    observed_operation = _first(record, "restore_operation_id", "restoreOperationId", "operation_id", "operationId", "id")
    observed_backup = _first(record, "backup_reference", "backupReference", "source_backup_id", "sourceBackupId", "backup_id", "backupId")
    observed_target = _first(record, "target_supabase_project_ref", "target_project_ref", "targetProjectRef", "restored_project_ref")
    status = _first(record, "status", "state", "phase").upper()
    if not operation_id.strip() or observed_operation != operation_id.strip():
        raise ValueError("provider evidence operation ID does not match the requested restore")
    if not backup_reference.strip() or observed_backup != backup_reference.strip():
        raise ValueError("provider evidence backup reference does not match the restored backup")
    if not re.fullmatch(r"[a-z0-9]{20}", target_project_ref or ""):
        raise ValueError("expected target staging project ref is invalid")
    if observed_target != target_project_ref:
        raise ValueError("provider evidence target does not match the configured staging project")
    if status not in _SUCCESS:
        raise ValueError("provider evidence does not show a successful terminal restore status")
    return {
        "status": "PASS",
        "restore_operation_id": observed_operation,
        "backup_reference": observed_backup,
        "target_supabase_project_ref": observed_target,
        "provider_status": status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Downloaded provider-native JSON operation record")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--backup-reference", required=True)
    parser.add_argument("--target-project-ref", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        raw = Path(args.input).read_bytes()
        if not raw:
            raise ValueError("provider restore evidence file is empty")
        document = json.loads(raw.decode("utf-8-sig"))
        result = verify(
            document,
            operation_id=args.operation_id,
            backup_reference=args.backup_reference,
            target_project_ref=args.target_project_ref,
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
        print(json.dumps({
            "status": "BLOCKED",
            "reason": "RESTORE_OPERATION_EVIDENCE_INVALID",
            "error_type": type(exc).__name__,
            "detail": str(exc),
        }, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
