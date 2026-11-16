from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_restore_audit_downloads_and_verifies_provider_native_operation_record():
    workflow = (ROOT / ".github/workflows/staging-restore-audit.yml").read_text(encoding="utf-8")
    assert 'gcloud storage cp "$RESTORE_EVIDENCE_URI" "$RUNNER_TEMP/restore-operation-evidence.json"' in workflow
    assert 'curl --fail --silent --show-error --location --max-redirs 0' in workflow
    assert "scripts/verify_restore_operation_evidence.py" in workflow
    assert "--operation-id \"$RESTORE_OPERATION_ID\"" in workflow
    assert "--backup-reference \"$BACKUP_REFERENCE\"" in workflow
    assert '--target-project-ref "$STAGING_SUPABASE_PROJECT_REF"' in workflow
    assert "--restore-evidence-sha256 \"$RESTORE_EVIDENCE_SHA256\"" in workflow
    assert '--verified-target-project-ref "$VERIFIED_RESTORE_TARGET_PROJECT_REF"' in workflow
    assert 'gcloud storage ls "$RESTORE_EVIDENCE_URI"' not in workflow


def test_restore_audit_rejects_unconfigured_or_malformed_cloud_identity_before_auth():
    workflow = (ROOT / ".github/workflows/staging-restore-audit.yml").read_text(encoding="utf-8")
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER variable or secret" in workflow
    assert "GCP_STAGING_RESTORE_AUDIT_SERVICE_ACCOUNT variable or secret" in workflow
    assert "workloadIdentityPools/*/providers/*" in workflow
    assert "GCP_STAGING_RESTORE_AUDIT_SERVICE_ACCOUNT" in workflow


def test_restore_audit_uses_a_distinct_read_only_audit_database_identity():
    workflow = (ROOT / ".github/workflows/staging-restore-audit.yml").read_text(encoding="utf-8")
    verifier = (ROOT / "scripts/verify_staging_restore.py").read_text(encoding="utf-8")
    assert "ATLAS_STAGING_AUDIT_DATABASE_SECRET" in workflow
    assert "ATLAS_STAGING_AUDIT_DB_ROLE" in workflow
    assert '[[ "$EXPECTED_AUDIT_DB_ROLE" == "atlas_audit_reader" ]]' in workflow
    assert '[[ "$STAGING_SUPABASE_PROJECT_REF" != "$PRODUCTION_SUPABASE_PROJECT_REF" ]]' in workflow
    assert "--expected-audit-role" in workflow
    assert "atlas-staging-audit-db-url" in workflow
    assert "atlas-staging-db-url" not in workflow
    assert "full restore audit requires a distinct non-production target" in verifier
    assert "public_tables_with_write_privileges" in verifier
    assert "public_sequences_with_privileges" in verifier
    assert "public_functions_with_execute" in verifier
    assert "expected_runtime_role" not in verifier


def test_provider_reconciliation_uses_read_only_audit_secret_not_paper_reader_secret():
    workflow = (ROOT / ".github/workflows/provider-ledger-reconciliation.yml").read_text(encoding="utf-8")
    assert "ATLAS_STAGING_AUDIT_DATABASE_SECRET" in workflow
    assert "ATLAS_STAGING_AUDIT_DB_ROLE" in workflow
    assert '[[ "$EXPECTED_AUDIT_DB_ROLE" == "atlas_audit_reader" ]]' in workflow
    assert "--expected-audit-role" in workflow
    assert "atlas-staging-audit-db-url" in workflow
    assert "STAGING_DATABASE_SECRET" not in workflow
    assert "EXPECTED_RUNTIME_DB_ROLE" not in workflow

