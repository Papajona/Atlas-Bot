from pathlib import Path

import pytest

from scripts.alembic_revision_contract import expected_alembic_heads


ROOT = Path(__file__).resolve().parents[1]


def test_repository_alembic_graph_has_the_expected_single_head():
    assert expected_alembic_heads(ROOT / "alembic" / "versions") == {
        "0065_ledger_trigger_owner_isolation"
    }


def test_alembic_revision_contract_supports_multiple_heads_and_merge_revisions(tmp_path):
    (tmp_path / "0001_root.py").write_text(
        "revision = '0001_root'\ndown_revision = None\n", encoding="utf-8"
    )
    (tmp_path / "0002_left.py").write_text(
        "revision = '0002_left'\ndown_revision = '0001_root'\n", encoding="utf-8"
    )
    (tmp_path / "0002_right.py").write_text(
        "revision = '0002_right'\ndown_revision = '0001_root'\n", encoding="utf-8"
    )
    assert expected_alembic_heads(tmp_path) == {"0002_left", "0002_right"}
    (tmp_path / "0003_merge.py").write_text(
        "revision = '0003_merge'\ndown_revision = ('0002_left', '0002_right')\n",
        encoding="utf-8",
    )
    assert expected_alembic_heads(tmp_path) == {"0003_merge"}


def test_alembic_revision_contract_rejects_missing_parent(tmp_path):
    (tmp_path / "0002_orphan.py").write_text(
        "revision = '0002_orphan'\ndown_revision = '0001_missing'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing parents"):
        expected_alembic_heads(tmp_path)


def test_alembic_revision_contract_rejects_cycles(tmp_path):
    (tmp_path / "0001_a.py").write_text(
        "revision = '0001_a'\ndown_revision = '0002_b'\n", encoding="utf-8"
    )
    (tmp_path / "0002_b.py").write_text(
        "revision = '0002_b'\ndown_revision = '0001_a'\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="cycle"):
        expected_alembic_heads(tmp_path)



def test_integrated_alembic_chain_has_the_expected_single_head():
    migration = (
        ROOT / "alembic" / "versions" / "0064_strategy_outcome_recent_index.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "0064_strategy_outcome_recent_index"' in migration
    assert 'down_revision = "0063_webhook_events_created_at_index"' in migration
    assert expected_alembic_heads(ROOT / "alembic" / "versions") == {
        "0065_ledger_trigger_owner_isolation"
    }


def test_integrated_migration_chain_is_linear_and_keeps_all_feature_revisions():
    versions = ROOT / "alembic" / "versions"
    expected = {
        "0060_customer_identity_unmapped_index": "0059_research_integrity_controls",
        "0061_strategy_outcome_fill_aggregation": "0060_customer_identity_unmapped_index",
        "0062_customer_benchmark_capture": "0061_strategy_outcome_fill_aggregation",
        "0063_webhook_events_created_at_index": "0062_customer_benchmark_capture",
        "0064_strategy_outcome_recent_index": "0063_webhook_events_created_at_index",
        "0065_ledger_trigger_owner_isolation": "0064_strategy_outcome_recent_index",
    }
    for revision, parent in expected.items():
        migration = next(versions.glob(f"*{revision}.py")).read_text(encoding="utf-8")
        assert f'revision = "{revision}"' in migration
        assert f'down_revision = "{parent}"' in migration
    assert expected_alembic_heads(versions) == {"0065_ledger_trigger_owner_isolation"}


def test_0065_trigger_owner_is_cloud_sql_compatible_and_revokes_before_transfer():
    migration = (
        ROOT / "alembic" / "versions" / "0065_ledger_trigger_owner_isolation.py"
    ).read_text(encoding="utf-8")

    assert "NOLOGIN NOINHERIT NOBYPASSRLS" in migration
    assert "AND NOT rolbypassrls" in migration
    assert "CREATE POLICY atlas_ledger_trigger_owner_select" in migration
    assert "atlas_ledger_trigger_owner SELECT policies are missing" in migration
    assert "migration identity must own every internal ledger function" in migration
    assert "grantee.rolname" in migration
    assert "acl.grantee <> p.proowner" in migration
    assert "unapproved explicit EXECUTE grant" in migration
    assert "aclexplode(" in migration
    assert "'REVOKE EXECUTE ON FUNCTION %s FROM %I CASCADE'" in migration

    revoke = migration.index("'REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC'")
    transfer = migration.index(
        "ALTER FUNCTION public.atlas_ledger_journal_deferred_check() OWNER TO atlas_ledger_trigger_owner"
    )
    revoke_membership = migration.index(
        "EXECUTE format('REVOKE %I FROM %I', role_name, migration_role)"
    )
    assert revoke < transfer < revoke_membership


def test_0065_downgrade_restores_owner_for_round_trip_and_all_recovery_gates_expect_head():
    migration = (
        ROOT / "alembic" / "versions" / "0065_ledger_trigger_owner_isolation.py"
    ).read_text(encoding="utf-8")
    assert "Restore the original migration owner" in migration
    assert "ALTER FUNCTION public.atlas_ledger_journal_deferred_check() OWNER TO %I" in migration
    assert "DROP POLICY IF EXISTS atlas_ledger_trigger_owner_select" in migration
    assert "DROP ROLE atlas_ledger_trigger_owner" in migration
    assert "downgrade past 0065_ledger_trigger_owner_isolation is blocked" not in migration

    api_provisioner = (
        ROOT / "scripts" / "provision_atlas_api_runtime.sql"
    ).read_text(encoding="utf-8")
    audit_provisioner = (
        ROOT / "scripts" / "provision_atlas_audit_reader.sql"
    ).read_text(encoding="utf-8")
    restore_verifier = (
        ROOT / "scripts" / "verify_staging_restore.py"
    ).read_text(encoding="utf-8")
    reconciliation_verifier = (
        ROOT / "scripts" / "verify_provider_ledger_reconciliation.py"
    ).read_text(encoding="utf-8")
    restore_workflow = (
        ROOT / ".github" / "workflows" / "staging-restore-audit.yml"
    ).read_text(encoding="utf-8")
    reconciliation_workflow = (
        ROOT / ".github" / "workflows" / "provider-ledger-reconciliation.yml"
    ).read_text(encoding="utf-8")

    expected = "0065_ledger_trigger_owner_isolation"
    for source in (
        api_provisioner,
        audit_provisioner,
        restore_verifier,
        reconciliation_verifier,
        restore_workflow,
        reconciliation_workflow,
    ):
        assert expected in source

def test_cloud_sql_recovery_runbook_keeps_schema_before_alembic_and_no_duplicate_steps():
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")

    schema_step = runbook.index(
        "4. While still connected as the database owner/admin, apply "
        "`scripts/customer_paper_ledger_schema.sql`"
    )
    migration_step = runbook.index(
        "5. Apply the repository Alembic chain with `ENVIRONMENT=development`"
    )
    assert schema_step < migration_step
    assert runbook.count("5. Apply the repository Alembic chain") == 1
    assert runbook.count("Once the bucket retention policy is locked") == 1
    assert runbook.count(
        "Revision `0065_ledger_trigger_owner_isolation` creates the internal "
        "`atlas_ledger_trigger_owner` identity"
    ) == 1
    assert runbook.count(
        "The executor verifies the source object's generation, provider checksum, "
        "Supabase log correlation and locked retention"
    ) == 1
    assert runbook.count(
        "After import, run the post-restore database steps below"
    ) == 1
    assert runbook.count(
        "After the Supabase backup restore or the Cloud SQL post-import migration/schema steps"
    ) == 1
    assert runbook.count(
        "For payout statement matching, upload a provider-generated"
    ) == 1
    assert "**Provenance evidence is now available from the source provider logs**" not in runbook



def test_0065_downgrade_preserves_preexisting_trigger_role_and_policies():
    migration = (
        ROOT / "alembic" / "versions" / "0065_ledger_trigger_owner_isolation.py"
    ).read_text(encoding="utf-8")

    # The role comment records whether each object/grant predated this revision.
    # Downgrade must consult that state rather than deleting every matching name.
    for state_key in (
        "'role_created_by_revision'",
        "'journals_policy_created_by_revision'",
        "'lines_policy_created_by_revision'",
        "'had_public_schema_usage'",
        "'had_ledger_journals_select'",
        "'had_ledger_journal_lines_select'",
        "'had_validate_function_execute'",
        "'prior_role_comment'",
    ):
        assert state_key in migration

    downgrade = migration.split("def downgrade()", 1)[1]
    assert "rollback provenance is missing" in downgrade
    assert "DROP POLICY atlas_ledger_trigger_owner_select ON public.ledger_journals" in downgrade
    assert "DROP POLICY atlas_ledger_trigger_owner_select ON public.ledger_journal_lines" in downgrade
    assert "DROP ROLE atlas_ledger_trigger_owner" in downgrade
    assert "role_created_by_revision" in downgrade
    assert "journals_policy_created_by_revision" in downgrade
    assert "lines_policy_created_by_revision" in downgrade
    assert "COMMENT ON ROLE %I IS %L" in downgrade
