from __future__ import annotations

import pytest

from scripts.verify_restore_operation_evidence import verify


OP = {
    "restore_operation_id": "restore-2026-10-16-01",
    "backup_reference": "immutable-backup-123",
    "target_supabase_project_ref": "abcdefghijklmnopqrst",
    "status": "COMPLETED",
}


def test_restore_operation_evidence_requires_exact_provider_identity():
    result = verify(
        {"operation": OP},
        operation_id="restore-2026-10-16-01",
        backup_reference="immutable-backup-123",
        target_project_ref="abcdefghijklmnopqrst",
    )
    assert result["status"] == "PASS"
    assert result["provider_status"] == "COMPLETED"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("restore_operation_id", "other-operation", "operation ID"),
        ("backup_reference", "other-backup", "backup reference"),
        ("target_supabase_project_ref", "ponmlkjihgfedcbazyxw", "target"),
        ("status", "RUNNING", "successful terminal"),
    ],
)
def test_restore_operation_evidence_rejects_mismatched_or_incomplete_record(field, value, message):
    record = {**OP, field: value}
    with pytest.raises(ValueError, match=message):
        verify(
            record,
            operation_id="restore-2026-10-16-01",
            backup_reference="immutable-backup-123",
            target_project_ref="abcdefghijklmnopqrst",
        )


def test_restore_operation_evidence_requires_machine_readable_object():
    with pytest.raises(ValueError, match="JSON object"):
        verify(
            "<html>Restore details</html>",
            operation_id="restore-2026-10-16-01",
            backup_reference="immutable-backup-123",
            target_project_ref="abcdefghijklmnopqrst",
        )

def test_restore_operation_evidence_prefers_complete_root_record_over_nested_metadata():
    record = {
        **OP,
        "data": {"status": "RUNNING", "id": "unrelated-metadata"},
    }
    result = verify(
        record,
        operation_id="restore-2026-10-16-01",
        backup_reference="immutable-backup-123",
        target_project_ref="abcdefghijklmnopqrst",
    )
    assert result["status"] == "PASS"
    assert result["restore_operation_id"] == "restore-2026-10-16-01"
