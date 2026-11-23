from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.execute_cloud_sql_restore import (
    _extract_operation_id,
    _live_backup_generation,
    _pick,
    _sql_import_suffix,
    _wait_for_operation,
    validate_backup_metadata,
    validate_restore_inputs,
    validate_sanitized_backup_reference,
)
from scripts.cloud_sql_recovery_inventory import is_source_backup_candidate


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
        ({"backup_reference": "gs://atlas-staging-510915-db-backups/restore-imports/abc.sql#1750000000000000"}, "import-staging object"),
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


@pytest.mark.parametrize(
    "document, expected",
    [
        ({"md5_hash": "md5", "crc32c_hash": "crc"}, "md5"),
        ({"md5Hash": "md5", "crc32c": "crc"}, "md5"),
        ({"retain_until_time": "2030-01-01T00:00:00Z"}, "2030-01-01T00:00:00Z"),
    ],
)


def test_provider_metadata_alias_picker_supports_snake_case_and_camel_case(document, expected):
    assert _pick(document, "md5_hash", "md5Hash", "crc32c_hash", "crc32c", "retain_until_time", "retainUntilTime") == expected


def test_backup_metadata_accepts_gcloud_snake_case_cli_output():
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    validate_backup_metadata(
        backup_reference=BACKUP,
        object_metadata={
            "name": "atlas-prod-backup.sql",
            "bucket": "atlas-staging-510915-db-backups",
            "generation": "1750000000000000",
            "size": "12345",
            "crc32c_hash": "AAAAAA==",
            "retention_expiration_time": (now + timedelta(days=20)).isoformat(),
        },
        bucket_metadata={"retention_policy": {"is_locked": True}},
        now=now,
    )


def test_cloud_sql_import_object_suffix_matches_source_compression(tmp_path):
    plain_dump = tmp_path / "source.sql"
    gzip_dump = tmp_path / "source.sql.gz"
    plain_dump.write_bytes(b"-- plain SQL\\nSELECT 1;\\n")
    gzip_dump.write_bytes(gzip.compress(b"-- compressed SQL\\nSELECT 1;\\n"))

    assert _sql_import_suffix(plain_dump) == ".sql"
    assert _sql_import_suffix(gzip_dump) == ".sql.gz"


def test_recovery_preflight_excludes_generated_import_copies_even_when_no_source_backup_exists():
    generated_import_objects = [
        {
            "name": "restore-imports/" + ("a" * 64) + "-" + ("b" * 32) + ".sql",
            "generation": "1750000000000000",
            "size": "12345",
            "crc32c": "AAAAAA==",
        },
        {
            "name": "restore-imports/" + ("c" * 64) + "-" + ("d" * 32) + ".sql.gz",
            "generation": "1750000000000001",
            "size": "12345",
            "md5Hash": "AAAAAA==",
        },
    ]

    candidates = [item for item in generated_import_objects if is_source_backup_candidate(item)]

    assert candidates == []
    assert not any(is_source_backup_candidate(item) for item in generated_import_objects)
    assert is_source_backup_candidate({
        "name": "scheduled/postgres-backup.sql.gz",
        "generation": "1750000000000002",
        "size": "54321",
        "crc32c": "BBBBBB==",
    })


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


def test_pinned_generation_must_be_the_live_object_before_import():
    instant = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    versions = {
        "items": [
            {
                "name": "atlas-prod-backup.sql",
                "generation": "1750000000000000",
                "timeCreated": "2026-11-16T00:00:00Z",
            },
            {
                "name": "atlas-prod-backup.sql",
                "generation": "1750000000000001",
                "timeCreated": "2026-11-17T11:00:00Z",
            },
        ]
    }
    assert _live_backup_generation(
        versions, object_name="atlas-prod-backup.sql", instant=instant
    ) == "1750000000000001"
    with pytest.raises(ValueError, match="no retained generation"):
        _live_backup_generation(
            {"items": []}, object_name="atlas-prod-backup.sql", instant=instant
        )


def test_cloud_sql_restore_workflow_stops_after_import_and_keeps_certification_manual():
    workflow = Path(".github/workflows/staging-cloud-sql-restore.yml").read_text(encoding="utf-8")
    audit_workflow = Path(".github/workflows/staging-restore-audit.yml").read_text(encoding="utf-8")
    executor = Path("scripts/execute_cloud_sql_restore.py").read_text(encoding="utf-8")
    preflight = Path(".github/workflows/cloud-sql-recovery-restore-preflight.yml").read_text(encoding="utf-8")
    discovery_preflight = Path(".github/workflows/cloud-sql-recovery-preflight.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "confirm_restore_target" in workflow
    assert '== "RESTORE"' in workflow
    assert "GCP_STAGING_RESTORE_EXECUTOR_SERVICE_ACCOUNT" in workflow
    assert "scripts/execute_cloud_sql_restore.py" in workflow
    assert 'RESTORE_TARGET_KIND" == "cloud_sql"' in workflow
    assert "source_supabase_project_ref" in workflow
    assert "sanitized_backup_reference" in workflow
    assert "--sanitized-backup-reference" in workflow
    assert "ATLAS_CLOUD_SQL_SOURCE_PREPARED_CANONICAL_SHA256" in workflow
    assert "--expected-source-prepared-canonical-sha256" in workflow
    assert "expected_source_prepared_sql_canonical_sha256" in workflow
    assert '"expected_source_prepared_sql_canonical_sha256": candidate_evidence[' in executor
    assert "verify_sanitized_artifacts(" in executor
    assert '"status": "IMPORT_COMPLETE"' in executor
    assert "IMPORT_COMPLETE_NOT_CERTIFIED" in workflow
    assert "migration_and_schema_certification_pending" in workflow
    assert "read_only_restore_audit_pending" in workflow
    assert "gh api --method POST" not in workflow
    assert "staging-restore-audit.yml/dispatches" not in workflow
    assert "gh run watch" not in workflow

    # Certification remains a separately dispatched read-only workflow.
    assert "workflow_dispatch:" in audit_workflow
    assert "source_supabase_project_ref" in audit_workflow
    assert "verify_cloud_sql_supabase_source_provenance.py" in audit_workflow
    assert "verify_cloud_sql_sanitized_artifacts.py" in audit_workflow
    assert "ATLAS_CLOUD_SQL_SOURCE_PREPARED_CANONICAL_SHA256" in audit_workflow
    assert "--expected-source-prepared-canonical-sha256" in audit_workflow
    assert "SOURCE_BACKUP_REFERENCE" in audit_workflow
    assert "prepare_cloud_sql_import_dump.py" in audit_workflow
    assert "atlas_audit_reader" in audit_workflow
    assert "[.]sql([.]gz)?" in audit_workflow
    assert "RESTORE_TARGET_KIND" in audit_workflow

    assert "from scripts.verify_cloud_sql_supabase_source_provenance import verify_supabase_pg_dump_source" in preflight
    assert "SOURCE_SUPABASE_PROJECT_REF: ${{ vars.ATLAS_PRODUCTION_SUPABASE_PROJECT_REF }}" in preflight
    assert "ATLAS_PRODUCTION_CLOUD_SQL_DATABASE" not in preflight
    assert "require_supported_postgresql_major(" in preflight
    assert "scripts/cloud_sql_recovery_discovery.py" in discovery_preflight

    installer = Path("scripts/install_pinned_postgresql_client.sh").read_text(encoding="utf-8")
    assert "POSTGRES_CLIENT_MAJOR=17" in installer
    assert "POSTGRES_CLIENT_RELEASE=17.12" in installer
    assert "apt-cache madison" in installer
    assert "apt-archive.postgresql.org/pub/repos/apt" in installer
    assert "postgresql-pgdg-archive.list" in installer
    assert "find_pinned_package_version" in installer
    assert "both signed PGDG main and archive suites" in installer
    assert 'client_package="postgresql-client-${POSTGRES_CLIENT_MAJOR}"' in installer
    assert '"$client_package=${package_version}"' in installer
    assert "dpkg-query -W" in installer
    assert "dpkg --compare-versions" in installer
    assert "--no-remove" in installer
    assert "--allow-downgrades" not in installer
    assert "refusing package downgrades" in installer
    verifier = Path("scripts/verify_cloud_sql_sanitized_artifacts.py").read_text(encoding="utf-8")
    assert "APPROVED_SOURCE_PREPARED_CANONICAL_SHA256" in verifier
    assert "does not match the code-reviewed approved fingerprint" in verifier
    assert "must not equal the sanitized candidate fingerprint" in verifier
    assert r"\(PostgreSQL\) 17\.12([[:space:]]|$)" in installer
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "Verify a real PostgreSQL 17 custom archive conversion" in ci
    assert "pg_dump --format=custom" in ci
    assert "bash -n scripts/install_pinned_postgresql_client.sh" in ci


def test_execute_restore_signature_keeps_required_output_before_optional_source_settings():
    import inspect
    from scripts.execute_cloud_sql_restore import execute_restore

    parameters = list(inspect.signature(execute_restore).parameters)
    assert "expected_source_prepared_canonical_sha256" in parameters
    assert parameters.index("expected_source_prepared_canonical_sha256") < parameters.index("output_path")
    assert parameters.index("output_path") < parameters.index("source_supabase_project_ref")
    assert parameters.index("output_path") < parameters.index("production_instance")


def test_execute_restore_retains_import_safety_generation_and_operation_evidence_gates():
    source = Path("scripts/execute_cloud_sql_restore.py").read_text(encoding="utf-8")
    body = source.split("def execute_restore(", 1)[1].split("\ndef main(", 1)[0]

    # Protect the executor orchestration, not just the helpers in isolation.
    required_calls = (
        "verify_supabase_pg_dump_source(",
        "verify_sanitized_artifacts(",
        '\"scripts/verify_cloud_sql_import_dump.py\"',
        '\"--if-generation-match=0\"',
        "_live_backup_generation(",
        "_wait_for_operation(",
        "verify_backup_binding(",
        "verify_restore_operation(",
    )
    for call in required_calls:
        assert call in body, f"execute_restore no longer enforces {call}"

    # Do not import until SQL scanning, immutable upload/read-back, generation
    # verification, and completed-operation evidence checks have all run.
    scanner = body.index('\"scripts/verify_cloud_sql_import_dump.py\"')
    create_only_upload = body.index('\"--if-generation-match=0\"')
    read_back = body.index("[\"gcloud\", \"storage\", \"cp\", import_reference, verified_import_path]")
    live_generation = body.index("_live_backup_generation(")
    import_request = body.index('\"gcloud\", \"sql\", \"import\"')
    wait_for_completion = body.index("_wait_for_operation(")
    backup_binding = body.index("backup_binding = verify_backup_binding(")
    operation_evidence = body.index("restore_evidence = verify_restore_operation(")

    assert scanner < create_only_upload < read_back < live_generation < import_request
    assert import_request < wait_for_completion < backup_binding < operation_evidence


def test_restore_inputs_accept_verified_supabase_pg_dump_source_without_production_cloud_sql_instance():
    assert validate_restore_inputs(
        project_id="atlas-staging-510915",
        region="europe-west1",
        target_instance=TARGET,
        target_database="atlas-recovery-db",
        backup_reference=(
            "gs://atlas-staging-510915-db-backups/"
            "atlas-supabase-pre-customer-paper-20261022T153023Z.dump#1792683068959304"
        ),
        source_supabase_project_ref="wauaelzyugjuepmrewaw",
    ) == (
        "atlas-recovery",
        "atlas-supabase-pre-customer-paper-20261022T153023Z.dump",
        "1792683068959304",
    )


def test_restore_inputs_reject_wrong_supabase_source_project_ref():
    with pytest.raises(ValueError, match="source Supabase project ref"):
        validate_restore_inputs(
            project_id="atlas-staging-510915",
            region="europe-west1",
            target_instance=TARGET,
            target_database="atlas-recovery-db",
            backup_reference=(
                "gs://atlas-staging-510915-db-backups/"
                "atlas-supabase-pre-customer-paper-20261022T153023Z.dump#1792683068959304"
            ),
            source_supabase_project_ref="wrong-project-ref",
        )


def test_sanitized_backup_reference_accepts_only_generation_pinned_sql_under_sanitized_prefix():
    assert validate_sanitized_backup_reference(
        "gs://atlas-staging-510915-db-backups/sanitized-imports/public-staging-scrubbed.sql#1792683068959305"
    ) == ("sanitized-imports/public-staging-scrubbed.sql", "1792683068959305")


@pytest.mark.parametrize("reference", [
    "gs://atlas-staging-510915-db-backups/public-staging-scrubbed.sql#1792683068959305",
    "gs://atlas-staging-510915-db-backups/sanitized-imports/public-staging-scrubbed.sql",
    "gs://atlas-staging-510915-db-backups/sanitized-imports/../raw.dump#1792683068959305",
    "gs://atlas-staging-510915-db-backups/sanitized-imports/../raw.sql#1792683068959305",
    "gs://wrong-bucket/sanitized-imports/public-staging-scrubbed.sql#1792683068959305",
    "gs://atlas-staging-510915-db-backups/sanitized-imports/raw.dump#1792683068959305",
])


def test_sanitized_backup_reference_rejects_unpinned_or_unapproved_objects(reference):
    with pytest.raises(ValueError):
        validate_sanitized_backup_reference(reference)


def test_sanitized_import_verifier_binds_candidate_and_import_to_pinned_source_sql(tmp_path, monkeypatch):
    from scripts import verify_cloud_sql_sanitized_artifacts as verifier

    source = tmp_path / "source-prepared.sql"
    candidate = tmp_path / "candidate.sql"
    imported = tmp_path / "imported.sql"
    source.write_bytes(b"reviewed source prepared SQL")
    candidate.write_bytes(b"scrubbed candidate SQL")
    imported.write_bytes(b"scrubbed candidate SQL")

    import hashlib
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    source_canonical_sha, _ = verifier.canonical_dump_sha256(source)
    candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    monkeypatch.setattr(verifier, "APPROVED_SOURCE_PREPARED_CANONICAL_SHA256", source_canonical_sha)
    monkeypatch.setattr(verifier, "EXPECTED_SANITIZED_IMPORT_SHA256", candidate_sha)
    monkeypatch.setattr(verifier, "EXPECTED_SANITIZED_IMPORT_BYTES", candidate.stat().st_size)
    monkeypatch.setattr(
        verifier, "EXPECTED_SOURCE_BACKUP_REFERENCE",
        "gs://atlas-staging-510915-db-backups/source.dump#1792683068959304",
    )

    result = verifier.verify_sanitized_artifacts(
        source_backup_reference="gs://atlas-staging-510915-db-backups/source.dump#1792683068959304",
        source_prepared_sql_path=source,
        expected_source_prepared_canonical_sha256=source_canonical_sha,
        sanitized_backup_reference="gs://atlas-staging-510915-db-backups/sanitized-imports/candidate.sql#1792683068959305",
        sanitized_sql_path=candidate,
        imported_sql_path=imported,
    )
    assert result["status"] == "PASS"
    assert result["source_prepared_sql_sha256"] == source_sha
    assert result["source_prepared_sql_canonical_sha256"] == source_canonical_sha
    assert result["expected_source_prepared_sql_canonical_sha256"] == source_canonical_sha
    assert result["sanitized_import_sha256"] == candidate_sha
    assert result["imported_sql_sha256"] == candidate_sha


def test_sanitized_import_verifier_rejects_changed_source_prepared_sql(tmp_path, monkeypatch):
    from scripts import verify_cloud_sql_sanitized_artifacts as verifier

    source = tmp_path / "source-prepared.sql"
    candidate = tmp_path / "candidate.sql"
    source.write_bytes(b"changed or substituted source prepared SQL")
    candidate.write_bytes(b"scrubbed candidate SQL")

    import hashlib
    candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    monkeypatch.setattr(verifier, "APPROVED_SOURCE_PREPARED_CANONICAL_SHA256", "0" * 64)
    monkeypatch.setattr(verifier, "EXPECTED_SANITIZED_IMPORT_SHA256", candidate_sha)
    monkeypatch.setattr(verifier, "EXPECTED_SANITIZED_IMPORT_BYTES", candidate.stat().st_size)
    monkeypatch.setattr(
        verifier, "EXPECTED_SOURCE_BACKUP_REFERENCE",
        "gs://atlas-staging-510915-db-backups/source.dump#1792683068959304",
    )

    with pytest.raises(ValueError, match="does not match the independently reviewed source fingerprint"):
        verifier.verify_sanitized_artifacts(
            source_backup_reference="gs://atlas-staging-510915-db-backups/source.dump#1792683068959304",
            source_prepared_sql_path=source,
            expected_source_prepared_canonical_sha256="0" * 64,
            sanitized_backup_reference="gs://atlas-staging-510915-db-backups/sanitized-imports/candidate.sql#1792683068959305",
            sanitized_sql_path=candidate,
        )


@pytest.mark.parametrize("fingerprint", ["", "not-a-hash", "A" * 64, "0" * 63, None, 123])


def test_sanitized_import_verifier_rejects_missing_or_malformed_source_fingerprint(
    tmp_path, monkeypatch, fingerprint
):
    from scripts import verify_cloud_sql_sanitized_artifacts as verifier

    source = tmp_path / "source-prepared.sql"
    candidate = tmp_path / "candidate.sql"
    source.write_bytes(b"reviewed source prepared SQL")
    candidate.write_bytes(b"scrubbed candidate SQL")

    import hashlib
    candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    monkeypatch.setattr(verifier, "EXPECTED_SANITIZED_IMPORT_SHA256", candidate_sha)
    monkeypatch.setattr(verifier, "EXPECTED_SANITIZED_IMPORT_BYTES", candidate.stat().st_size)
    monkeypatch.setattr(
        verifier, "EXPECTED_SOURCE_BACKUP_REFERENCE",
        "gs://atlas-staging-510915-db-backups/source.dump#1792683068959304",
    )

    with pytest.raises(ValueError, match="must be a lowercase 64-character hex fingerprint"):
        verifier.verify_sanitized_artifacts(
            source_backup_reference="gs://atlas-staging-510915-db-backups/source.dump#1792683068959304",
            source_prepared_sql_path=source,
            expected_source_prepared_canonical_sha256=fingerprint,
            sanitized_backup_reference="gs://atlas-staging-510915-db-backups/sanitized-imports/candidate.sql#1792683068959305",
            sanitized_sql_path=candidate,
        )


def test_sanitized_import_verifier_rejects_raw_source_or_modified_import(tmp_path, monkeypatch):
    from scripts import verify_cloud_sql_sanitized_artifacts as verifier
    import hashlib

    source = tmp_path / "source-prepared.sql"
    candidate = tmp_path / "candidate.sql"
    imported = tmp_path / "imported.sql"
    source.write_bytes(b"reviewed source prepared SQL")
    candidate.write_bytes(b"scrubbed candidate SQL")
    imported.write_bytes(b"original unsanitized source SQL")

    source_canonical_sha, _ = verifier.canonical_dump_sha256(source)
    monkeypatch.setattr(verifier, "APPROVED_SOURCE_PREPARED_CANONICAL_SHA256", source_canonical_sha)
    monkeypatch.setattr(verifier, "EXPECTED_SOURCE_BACKUP_REFERENCE",
        "gs://atlas-staging-510915-db-backups/source.dump#1792683068959304")
    monkeypatch.setattr(verifier, "EXPECTED_SANITIZED_IMPORT_SHA256",
        hashlib.sha256(candidate.read_bytes()).hexdigest())
    monkeypatch.setattr(verifier, "EXPECTED_SANITIZED_IMPORT_BYTES", candidate.stat().st_size)

    with pytest.raises(ValueError, match="immutable import object bytes differ"):
        verifier.verify_sanitized_artifacts(
            source_backup_reference="gs://atlas-staging-510915-db-backups/source.dump#1792683068959304",
            source_prepared_sql_path=source,
            expected_source_prepared_canonical_sha256=verifier.canonical_dump_sha256(source)[0],
            sanitized_backup_reference="gs://atlas-staging-510915-db-backups/sanitized-imports/candidate.sql#1792683068959305",
            sanitized_sql_path=candidate,
            imported_sql_path=imported,
        )


def test_sanitized_import_verifier_rejects_mutated_expected_fingerprint_even_when_source_matches(tmp_path, monkeypatch):
    from scripts import verify_cloud_sql_sanitized_artifacts as verifier

    source = tmp_path / "source-prepared.sql"
    candidate = tmp_path / "candidate.sql"
    source.write_bytes(b"reviewed source prepared SQL")
    candidate.write_bytes(b"scrubbed candidate SQL")
    source_canonical_sha, _ = verifier.canonical_dump_sha256(source)

    import hashlib
    candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    monkeypatch.setattr(verifier, "APPROVED_SOURCE_PREPARED_CANONICAL_SHA256", "a" * 64)
    monkeypatch.setattr(verifier, "EXPECTED_SANITIZED_IMPORT_SHA256", candidate_sha)
    monkeypatch.setattr(verifier, "EXPECTED_SANITIZED_IMPORT_BYTES", candidate.stat().st_size)
    monkeypatch.setattr(
        verifier, "EXPECTED_SOURCE_BACKUP_REFERENCE",
        "gs://atlas-staging-510915-db-backups/source.dump#1792683068959304",
    )

    with pytest.raises(ValueError, match="does not match the code-reviewed approved fingerprint"):
        verifier.verify_sanitized_artifacts(
            source_backup_reference="gs://atlas-staging-510915-db-backups/source.dump#1792683068959304",
            source_prepared_sql_path=source,
            expected_source_prepared_canonical_sha256=source_canonical_sha,
            sanitized_backup_reference="gs://atlas-staging-510915-db-backups/sanitized-imports/candidate.sql#1792683068959305",
            sanitized_sql_path=candidate,
        )


def test_sanitized_import_verifier_rejects_source_pin_equal_to_candidate_fingerprint(tmp_path, monkeypatch):
    from scripts import verify_cloud_sql_sanitized_artifacts as verifier

    source = tmp_path / "source-prepared.sql"
    candidate = tmp_path / "candidate.sql"
    source.write_bytes(b"reviewed source prepared SQL")
    candidate.write_bytes(b"scrubbed candidate SQL")

    import hashlib
    candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    monkeypatch.setattr(verifier, "APPROVED_SOURCE_PREPARED_CANONICAL_SHA256", candidate_sha)
    monkeypatch.setattr(verifier, "EXPECTED_SANITIZED_IMPORT_SHA256", candidate_sha)
    monkeypatch.setattr(verifier, "EXPECTED_SANITIZED_IMPORT_BYTES", candidate.stat().st_size)
    monkeypatch.setattr(
        verifier, "EXPECTED_SOURCE_BACKUP_REFERENCE",
        "gs://atlas-staging-510915-db-backups/source.dump#1792683068959304",
    )

    with pytest.raises(ValueError, match="must not equal the sanitized candidate fingerprint"):
        verifier.verify_sanitized_artifacts(
            source_backup_reference="gs://atlas-staging-510915-db-backups/source.dump#1792683068959304",
            source_prepared_sql_path=source,
            expected_source_prepared_canonical_sha256=candidate_sha,
            sanitized_backup_reference="gs://atlas-staging-510915-db-backups/sanitized-imports/candidate.sql#1792683068959305",
            sanitized_sql_path=candidate,
        )


def test_cloud_sql_restore_iam_bootstrap_is_separate_and_least_privilege():
    bootstrap = Path("scripts/bootstrap_cloud_sql_restore_iam.sh").read_text(encoding="utf-8")
    assert "atlas-staging-restore-executor" in bootstrap
    assert "atlas-staging-restore-audit" in bootstrap
    assert "cloudsql.instances.import" in bootstrap
    assert "cloudsql.instances.connect" in bootstrap
    assert "roles/storage.objectViewer" in bootstrap
    assert "roles/storage.objectCreator" in bootstrap
    assert "roles/secretmanager.secretAccessor" in bootstrap
    assert "roles/storage.objectCreator" in bootstrap
    assert "RECOVERY_INSTANCE_SA" in bootstrap
    assert "serviceAccountEmailAddress" in bootstrap
    assert "--condition=" in bootstrap
    assert "cloudsql.instances.import" in bootstrap
    assert "secretmanager.versions.access" not in bootstrap
    assert "refs/heads/main" in bootstrap
    assert "gcloud sql instances create" not in bootstrap
    assert "gcloud sql databases create" not in bootstrap
    assert "gcloud sql import" not in bootstrap
    assert "ATLAS_PRODUCTION_SUPABASE_PROJECT_REF" in bootstrap
    assert "wauaelzyugjuepmrewaw" in bootstrap
    assert "ATLAS_PRODUCTION_CLOUD_SQL_INSTANCE_CONNECTION_NAME" not in bootstrap


def test_recovery_preflight_accepts_retained_noncurrent_source_backup_generation():
    # GCS reports timeDeleted for an archived object generation. Its presence in
    # the versions listing means this exact generation remains retrievable.
    retained_generation = {
        "name": "scheduled/postgres-backup.sql.gz",
        "generation": "1750000000000003",
        "size": "54321",
        "crc32c": "CCCCCC==",
        "timeDeleted": "2026-11-17T10:00:00Z",
    }

    assert is_source_backup_candidate(retained_generation)
