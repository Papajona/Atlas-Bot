from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts.execute_cloud_sql_restore import (
    _extract_operation_id,
    _wait_for_operation,
    validate_backup_metadata,
    validate_restore_inputs,
)


TARGET = "atlas-staging-510915:europe-west1:atlas-recovery"
PRODUCTION = "atlas-production:europe-west1:atlas-primary"
BACKUP = "gs://atlas-staging-510915-db-backups/atlas-prod-backup.sql#1750000000000000"


def test_restore_inputs_accept_only_explicit_staging_target_and_immutable_backup():
    assert validate_restore_inputs(
        project_id="atlas-staging-510915",
        region="europe-west1",
        target_instance=TARGET,
        production_instance=PRODUCTION,
        target_database="atlas-recovery-db",
        backup_reference=BACKUP,
    ) == (
        "atlas-recovery",
        "atlas-prod-backup.sql",
        "1750000000000000",
    )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"target_instance": PRODUCTION}, "staging project"),
        ({"target_instance": "atlas-staging-510915:us-east1:atlas-recovery"}, "staging region"),
        ({"production_instance": ""}, "explicitly configured"),
        ({"production_instance": TARGET}, "different from the production"),
        ({"target_database": "bad database"}, "database name is invalid"),
        ({"backup_reference": "gs://wrong-bucket/db.sql#1750000000000000"}, "approved recovery bucket"),
        ({"backup_reference": "gs://atlas-staging-510915-db-backups/db.sql"}, "generation"),
        ({"backup_reference": "gs://atlas-staging-510915-db-backups/../other.sql#1750000000000000"}, "path is invalid"),
    ],
)
def test_restore_inputs_fail_closed(overrides, message):
    values = {
        "project_id": "atlas-staging-510915",
        "region": "europe-west1",
        "target_instance": TARGET,
        "production_instance": PRODUCTION,
        "target_database": "atlas-recovery-db",
        "backup_reference": BACKUP,
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        validate_restore_inputs(**values)


def test_backup_metadata_requires_exact_generation_checksum_and_locked_retention():
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    validate_backup_metadata(
        backup_reference=BACKUP,
        object_metadata={
            "name": "atlas-prod-backup.sql",
            "bucket": "atlas-staging-510915-db-backups",
            "generation": "1750000000000000",
            "size": "12345",
            "crc32c": "AAAAAA==",
            "retentionExpirationTime": (now + timedelta(days=20)).isoformat(),
        },
        bucket_metadata={"retentionPolicy": {"isLocked": True}},
        now=now,
    )


@pytest.mark.parametrize(
    "object_change,bucket_change,message",
    [
        ({"generation": "1750000000000001"}, {}, "generation does not match"),
        ({"size": "0"}, {}, "empty"),
        ({"crc32c": None}, {}, "checksum metadata"),
        ({}, {"retentionPolicy": {"isLocked": False}}, "must be locked"),
        ({"retentionExpirationTime": "2026-11-16T00:00:00+00:00"}, {}, "already expired"),
    ],
)
def test_backup_metadata_rejects_unpinned_or_unretained_source(object_change, bucket_change, message):
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    object_metadata = {
        "name": "atlas-prod-backup.sql",
        "bucket": "atlas-staging-510915-db-backups",
        "generation": "1750000000000000",
        "size": "12345",
        "crc32c": "AAAAAA==",
        "retentionExpirationTime": (now + timedelta(days=20)).isoformat(),
    }
    object_metadata.update(object_change)
    bucket_metadata = {"retentionPolicy": {"isLocked": True}}
    bucket_metadata.update(bucket_change)
    with pytest.raises(ValueError, match=message):
        validate_backup_metadata(
            backup_reference=BACKUP,
            object_metadata=object_metadata,
            bucket_metadata=bucket_metadata,
            now=now,
        )


def test_operation_id_extraction_rejects_malformed_resource_names():
    assert _extract_operation_id({"name": "operation-123"}) == "operation-123"
    for document in ({}, {"name": "projects/prod/operations/123"}, {"name": "bad id"}):
        with pytest.raises(ValueError):
            _extract_operation_id(document)


def test_wait_for_operation_requires_successful_import_into_the_exact_database_and_object():
    operation = {
        "name": "operation-123",
        "operationType": "IMPORT",
        "status": "DONE",
        "importContext": {
            "database": "atlas-recovery-db",
            "uri": "gs://atlas-staging-510915-db-backups/atlas-prod-backup.sql",
        },
    }

    def fake_run(command, **kwargs):
        assert command[:4] == ["gcloud", "sql", "operations", "describe", "operation-123"][:4]
        return SimpleNamespace(returncode=0, stdout=json.dumps(operation), stderr="")

    result = _wait_for_operation(
        operation_id="operation-123",
        project_id="atlas-staging-510915",
        target_database="atlas-recovery-db",
        backup_object_uri="gs://atlas-staging-510915-db-backups/atlas-prod-backup.sql",
        timeout_seconds=60,
        poll_interval_seconds=1,
        run=fake_run,
    )
    assert result["status"] == "DONE"


def test_wait_for_operation_rejects_provider_error_and_wrong_target_database():
    def fake_run(document):
        def invoke(_command, **_kwargs):
            return SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr="")
        return invoke

    base = {
        "operationType": "IMPORT",
        "status": "DONE",
        "importContext": {
            "database": "atlas-recovery-db",
            "uri": "gs://atlas-staging-510915-db-backups/atlas-prod-backup.sql",
        },
    }
    with pytest.raises(RuntimeError, match="provider errors"):
        _wait_for_operation(
            operation_id="operation-123",
            project_id="atlas-staging-510915",
            target_database="atlas-recovery-db",
            backup_object_uri="gs://atlas-staging-510915-db-backups/atlas-prod-backup.sql",
            timeout_seconds=60,
            poll_interval_seconds=1,
            run=fake_run({**base, "error": {"errors": [{"reason": "backendError"}]}}),
        )
    with pytest.raises(ValueError, match="different database"):
        _wait_for_operation(
            operation_id="operation-123",
            project_id="atlas-staging-510915",
            target_database="atlas-recovery-db",
            backup_object_uri="gs://atlas-staging-510915-db-backups/atlas-prod-backup.sql",
            timeout_seconds=60,
            poll_interval_seconds=1,
            run=fake_run({
                **base,
                "importContext": {
                    **base["importContext"],
                    "database": "postgres",
                },
            }),
        )
