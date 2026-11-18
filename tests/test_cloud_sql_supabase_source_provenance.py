from datetime import datetime, timezone

import pytest

from scripts import verify_cloud_sql_supabase_source_provenance as source_provenance

SOURCE_BACKUP_REFERENCE = source_provenance.SOURCE_BACKUP_REFERENCE
SOURCE_OBJECT_CRC32C = source_provenance.SOURCE_OBJECT_CRC32C
SOURCE_OBJECT_MD5 = source_provenance.SOURCE_OBJECT_MD5
SOURCE_OBJECT_SIZE_BYTES = source_provenance.SOURCE_OBJECT_SIZE_BYTES
SOURCE_PROJECT_REF = source_provenance.SOURCE_PROJECT_REF
verify_supabase_pg_dump_source = source_provenance.verify_supabase_pg_dump_source


NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def source_object(**overrides):
    value = {
        "name": "atlas-supabase-pre-customer-paper-20261022T153023Z.dump",
        "generation": "1792683068959304",
        "size": str(SOURCE_OBJECT_SIZE_BYTES),
        "md5Hash": SOURCE_OBJECT_MD5,
        "crc32c": SOURCE_OBJECT_CRC32C,
        "timeCreated": "2026-10-22T15:31:08.966Z",
        "retentionExpirationTime": "2027-10-22T15:31:08.966Z",
    }
    value.update(overrides)
    return value


def locked_bucket(**overrides):
    return {"retentionPolicy": {"isLocked": True, "retentionPeriod": "31536000", **overrides}}


def test_supabase_pg_dump_log_is_bound_to_the_exact_gcs_object_generation():
    evidence = verify_supabase_pg_dump_source(
        source_backup_reference=SOURCE_BACKUP_REFERENCE,
        source_project_ref=SOURCE_PROJECT_REF,
        source_database="postgres",
        object_metadata=source_object(),
        bucket_metadata=locked_bucket(),
        now=NOW,
    )
    assert evidence["status"] == "PASS"
    assert evidence["source_type"] == "supabase_pg_dump"
    assert evidence["source_project_ref"] == "wauaelzyugjuepmrewaw"
    assert evidence["source_database_user"] == "postgres"
    assert evidence["source_application_name"] == "pg_dump"
    assert evidence["source_pooler_mode"] == "session"
    assert evidence["seconds_from_pg_dump_termination_to_object_creation"] == pytest.approx(3.215055)
    assert evidence["bucket_retention_policy_locked"] is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"source_backup_reference": "gs://atlas-staging-510915-db-backups/other.dump#1"},
         "source backup reference"),
        ({"source_project_ref": "xhbmgdtrmyxkgnwlndif"}, "source Supabase project ref"),
        ({"source_database": "other"}, "source database"),
        ({"object_metadata": source_object(generation="1792683068959305")}, "generation"),
        ({"object_metadata": source_object(bucket="wrong-bucket")}, "bucket"),
        ({"object_metadata": source_object(size="659331")}, "size"),
        ({"object_metadata": source_object(md5Hash="wrong")}, "MD5"),
        ({"object_metadata": source_object(crc32c="wrong")}, "CRC32C"),
        ({"object_metadata": source_object(timeCreated="2026-10-22T15:31:20Z")}, "within 10 seconds"),
        ({"object_metadata": source_object(retentionExpirationTime="2026-11-17T00:00:00Z")}, "retention has expired"),
        ({"bucket_metadata": {"retentionPolicy": {"isLocked": False}}}, "not locked"),
        ({"bucket_metadata": {}}, "retention policy is missing"),
    ],
)
def test_supabase_pg_dump_source_provenance_fails_closed(kwargs, message):
    args = {
        "source_backup_reference": SOURCE_BACKUP_REFERENCE,
        "source_project_ref": SOURCE_PROJECT_REF,
        "source_database": "postgres",
        "object_metadata": source_object(),
        "bucket_metadata": locked_bucket(),
        "now": NOW,
    }
    args.update(kwargs)
    with pytest.raises(ValueError, match=message):
        verify_supabase_pg_dump_source(**args)


def test_supabase_pg_dump_object_must_follow_the_logged_disconnect_within_ten_seconds():
    with pytest.raises(ValueError, match="within 10 seconds"):
        verify_supabase_pg_dump_source(
            source_backup_reference=SOURCE_BACKUP_REFERENCE,
            source_project_ref=SOURCE_PROJECT_REF,
            source_database="postgres",
            object_metadata=source_object(timeCreated="2026-10-22T15:31:30Z"),
            bucket_metadata=locked_bucket(),
            now=NOW,
        )

def test_gcs_creation_time_is_observed_not_a_fixed_source_constant():
    observed_creation_time = "2026-10-22T15:31:09.000Z"
    evidence = verify_supabase_pg_dump_source(
        source_backup_reference=SOURCE_BACKUP_REFERENCE,
        source_project_ref=SOURCE_PROJECT_REF,
        source_database="postgres",
        object_metadata=source_object(timeCreated=observed_creation_time),
        bucket_metadata=locked_bucket(),
        now=NOW,
    )
    assert evidence["status"] == "PASS"
    assert evidence["source_object_created_at"] == "2026-10-22T15:31:09+00:00"
    assert evidence["seconds_from_pg_dump_termination_to_object_creation"] == pytest.approx(3.249055)

