from pathlib import Path

import re

import pytest

from scripts.alembic_revision_contract import expected_alembic_heads


ROOT = Path(__file__).resolve().parents[1]


def test_repository_alembic_graph_has_the_expected_single_head():
    assert expected_alembic_heads(ROOT / "alembic" / "versions") == {
        "0069_customer_equity_history_and_workspace_preferences"
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
        "0069_customer_equity_history_and_workspace_preferences"
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
        "0066_pin_research_trigger_path": "0065_ledger_trigger_owner_isolation",
        "0067_service_role_customer_profiles_acl": "0066_pin_research_trigger_path",
        "0068_tron_sweep_txid_casefold_unique": "0067_service_role_customer_profiles_acl",
        "0069_customer_equity_history_and_workspace_preferences": "0068_tron_sweep_txid_casefold_unique",
    }
    for revision, parent in expected.items():
        migration = next(versions.glob(f"*{revision}.py")).read_text(encoding="utf-8")
        assert f'revision = "{revision}"' in migration
        assert f'down_revision = "{parent}"' in migration
    assert expected_alembic_heads(versions) == {"0069_customer_equity_history_and_workspace_preferences"}


def test_0066_pins_research_trigger_paths_and_revokes_public_execute():
    migration = (
        ROOT / "alembic" / "versions" / "0066_pin_research_trigger_path.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "0066_pin_research_trigger_path"' in migration
    assert 'down_revision = "0065_ledger_trigger_owner_isolation"' in migration
    for signature in (
        "public.atlas_research_trial_archive_immutable()",
        "public.atlas_research_holdout_registry_immutable()",
    ):
        assert signature in migration

    assert "to_regprocedure(function_signature) IS NULL" in migration
    assert "ALTER FUNCTION %s SET search_path = pg_catalog" in migration
    assert "REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC" in migration
    assert "ARRAY['anon', 'authenticated']" in migration
    assert "REVOKE EXECUTE ON FUNCTION %s FROM %I" in migration
    assert "has_function_privilege(" in migration
    assert "role_name::name" in migration
    assert "to_regprocedure(function_signature)::oid" in migration
    assert "p.proconfig @> ARRAY['search_path=pg_catalog']" in migration
    assert "acl.grantee = 0" in migration
    assert "PUBLIC retains EXECUTE" in migration


def test_0067_restricts_service_role_to_customer_profiles_and_its_id_sequence():
    migration = (
        ROOT / "alembic" / "versions" / "0067_service_role_customer_profiles_acl.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "0067_service_role_customer_profiles_acl"' in migration
    assert 'down_revision = "0066_pin_research_trigger_path"' in migration
    assert "GRANT SELECT, INSERT ON TABLE public.customer_profiles TO service_role" in migration
    assert "GRANT USAGE, SELECT ON SEQUENCE public.customer_profiles_id_seq TO service_role" in migration
    assert "GRANT UPDATE" not in migration
    assert "REVOKE EXECUTE ON %s %I.%I(%s) FROM service_role CASCADE" in migration
    assert "p.proowner = migration_role_oid" in migration
    assert "has_function_privilege('service_role', p.oid, 'EXECUTE')" in migration
    assert "service_role retains EXECUTE on a public function" in migration
    assert "pg_get_serial_sequence(" in migration
    assert "has_schema_privilege('service_role', 'public', 'USAGE')" in migration
    assert "service_role must retain USAGE on public" in migration
    assert "migration_role_oid" in migration
    assert "c.relowner = migration_role_oid" in migration
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public" not in migration
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public" not in migration
    assert "FROM pg_default_acl" in migration
    assert "aclexplode(defaults.defaclacl)" in migration
    assert "acl.grantee IN (service_role_oid, 0)" in migration
    assert "COALESCE(grantee_role.rolname, 'PUBLIC')" in migration
    assert "default_acl_grantee" in migration
    assert "has_schema_privilege(owner_role.oid, public_schema_oid, 'CREATE')" in migration
    assert "global_defaults.defaclnamespace = 0" in migration
    assert "NOT EXISTS (" in migration
    assert "default_function_owner" in migration
    assert "default PUBLIC EXECUTE" in migration
    assert "defaults.defaclnamespace IN (0, public_schema_oid)" in migration
    assert "0067 refuses to continue" in migration
    assert "authorized managed-owner operation" in migration
    assert "service_role retains privileges on an Atlas-owned public relation" in migration
    assert "service_role retains privileges on an Atlas-owned public sequence" in migration
    assert "# Deliberately forward-only" in migration

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "GRANT EXECUTE ON FUNCTION public.atlas_block_ledger_mutation() TO service_role" in workflow
    assert "GRANT EXECUTE ON FUNCTION public.atlas_validate_ledger_journal(integer) TO service_role" in workflow
    assert "has_function_privilege('service_role', p.oid, 'EXECUTE')" in workflow
    assert "CREATE FUNCTION public.atlas_supabase_admin_future_probe()" in workflow
    assert "public.atlas_supabase_admin_future_probe()" in workflow
    assert "atlas-0067-implicit-public-default.log" in workflow
    assert "default PUBLIC EXECUTE" in workflow
    assert "atlas-0067-pg-database-owner-default.log" in workflow
    assert "SET ROLE pg_database_owner" in workflow
    assert "CREATE FUNCTION public.atlas_pg_database_owner_future_probe()" in workflow


def test_0067_public_function_creator_scan_does_not_exclude_pg_database_owner():
    migration = (
        ROOT / "alembic" / "versions" / "0067_service_role_customer_profiles_acl.py"
    ).read_text(encoding="utf-8")

    # pg_database_owner is a valid SET ROLE creator context and must remain in
    # the default-ACL scan; this is an absence assertion, not an exclusion rule.
    assert "owner_role.rolname <> 'pg_database_owner'" not in migration
    assert "has_schema_privilege(owner_role.oid, public_schema_oid, 'CREATE')" in migration


def test_0067_checks_maintain_only_on_postgresql_17_or_newer():
    migration = (
        ROOT / "alembic" / "versions" / "0067_service_role_customer_profiles_acl.py"
    ).read_text(encoding="utf-8")

    guard = migration.index("current_setting('server_version_num')::integer >= 170000")
    dynamic = migration.index("EXECUTE $atlas_service_role_maintain_check$", guard)
    dynamic_end = migration.index("$atlas_service_role_maintain_check$", dynamic + 20)
    assert guard < dynamic < dynamic_end
    assert re.search(
        r"has_table_privilege\s*\(\s*'service_role'\s*,\s*c\.oid\s*,\s*'MAINTAIN'\s*\)",
        migration[dynamic:dynamic_end],
    )
    assert "has_table_privilege('service_role', profile_table_oid, 'MAINTAIN')" not in migration


def test_0065_trigger_owner_is_cloud_sql_compatible_and_revokes_before_transfer():
    migration = (
        ROOT / "alembic" / "versions" / "0065_ledger_trigger_owner_isolation.py"
    ).read_text(encoding="utf-8")

    assert "NOLOGIN NOINHERIT NOBYPASSRLS" in migration
    assert "AND NOT rolbypassrls" in migration
    assert "CREATE POLICY atlas_ledger_trigger_owner_select" in migration
    assert "ledger trigger owner SELECT policies are missing or have an unexpected definition" in migration
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
    assert "DROP POLICY atlas_ledger_trigger_owner_select ON public.ledger_journals" in migration
    assert "DROP POLICY atlas_ledger_trigger_owner_select ON public.ledger_journal_lines" in migration
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

    expected = "0069_customer_equity_history_and_workspace_preferences"
    for source in (
        api_provisioner,
        audit_provisioner,
        restore_verifier,
        reconciliation_verifier,
        restore_workflow,
        reconciliation_workflow,
    ):
        assert expected in source

def test_0065_rollback_provenance_tracks_exact_direct_acl_deltas_and_identity():
    migration = (
        ROOT / "alembic" / "versions" / "0065_ledger_trigger_owner_isolation.py"
    ).read_text(encoding="utf-8")

    assert "'marker', 'atlas:0065:rollback-state:v3'" in migration
    assert "'migration_identity', migration_role" in migration
    for key in (
        "pre_public_schema_usage_acl",
        "pre_ledger_journals_select_acl",
        "pre_ledger_journal_lines_select_acl",
        "pre_validate_function_execute_acl",
        "introduced_public_schema_usage_grantors",
        "introduced_ledger_journals_select_grantors",
        "introduced_ledger_journal_lines_select_grantors",
        "introduced_validate_function_execute_grantors",
    ):
        assert f"'{key}'" in migration

    assert "acl.grantee = role_oid" in migration
    assert "acl.grantor" in migration
    assert "aclexplode(" in migration
    assert "GRANTED BY %I" not in migration
    assert "role_state->>'migration_identity'" in migration
    assert "grantee.rolname <> role_name" in migration
    assert "jsonb_array_length(role_state->'pre_validate_function_execute_acl') = 0" in migration
    assert "original migration identity" in migration
    assert "pg_has_role(migration_role_oid, grantor_oid, 'SET')" in migration

    # The PostgreSQL membership regression proves an INHERIT-only membership
    # is not sufficient for SET ROLE. Keep the downgrade's complete grantor
    # preflight ahead of the first ownership or membership mutation so an
    # un-settable grantor fails closed without partially changing the schema.
    downgrade = migration.split("def downgrade()", 1)[1]
    validation_loop = downgrade.index("FOR grantor_oid IN")
    set_option_guard = downgrade.index(
        "NOT pg_has_role(migration_role_oid, grantor_oid, 'SET')",
        validation_loop,
    )
    validation_loop_end = downgrade.index("END LOOP;", set_option_guard)
    first_membership_mutation = downgrade.index(
        "EXECUTE format('GRANT %I TO %I', role_name, migration_role);",
        validation_loop_end,
    )
    ownership_mutation = downgrade.index(
        "ALTER FUNCTION public.atlas_ledger_journal_deferred_check() OWNER TO %I",
        first_membership_mutation,
    )
    assert validation_loop < set_option_guard < validation_loop_end
    assert validation_loop_end < first_membership_mutation < ownership_mutation

    # Check the data-driven ACL rollback loops semantically rather than pinning
    # exact global string counts. Each recorded grantor loop must assume that
    # grantor, perform only its corresponding REVOKE, and restore the migration
    # identity before the loop continues.
    def assert_grantor_revoke_loop(state_key: str, revoke_fragment: str) -> None:
        key_marker = f"coalesce(role_state->'{state_key}'"
        revoke_section_start = migration.index(
            "-- Revoke only entries proven by the pre/post snapshots"
        )
        key_index = migration.index(key_marker, revoke_section_start)
        loop_start = migration.rfind("FOR grantor_oid IN", 0, key_index)
        loop_end = migration.index("END LOOP;", key_index) + len("END LOOP;")
        assert loop_start >= 0
        loop = migration[loop_start:loop_end]
        assert "SELECT rolname INTO grantor_name FROM pg_roles WHERE oid = grantor_oid;" in loop
        assert loop.count("IF grantor_oid <> migration_role_oid THEN") == 2
        assume_grantor = loop.index("EXECUTE format('SET ROLE %I', grantor_name);")
        revoke = loop.index(revoke_fragment)
        restore_identity = loop.index("EXECUTE format('SET ROLE %I', migration_role);")
        assert assume_grantor < revoke < restore_identity

    assert_grantor_revoke_loop(
        "introduced_validate_function_execute_grantors",
        "REVOKE EXECUTE ON FUNCTION public.atlas_validate_ledger_journal(integer) FROM %I",
    )
    assert_grantor_revoke_loop(
        "introduced_ledger_journals_select_grantors",
        "REVOKE SELECT ON TABLE public.ledger_journals FROM %I",
    )
    assert_grantor_revoke_loop(
        "introduced_ledger_journal_lines_select_grantors",
        "REVOKE SELECT ON TABLE public.ledger_journal_lines FROM %I",
    )
    assert_grantor_revoke_loop(
        "introduced_public_schema_usage_grantors",
        "REVOKE USAGE ON SCHEMA public FROM %I",
    )

    rollback_state = migration.split("role_state := jsonb_build_object", 1)[1].split(
        "'prior_role_comment', role_comment", 1
    )[0]
    assert "has_schema_privilege(" not in rollback_state
    assert "has_table_privilege(" not in rollback_state
    assert "has_function_privilege(" not in rollback_state



def test_cloud_sql_recovery_runbook_keeps_schema_before_alembic_and_no_duplicate_steps():
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")

    schema_step = runbook.index(
        "4. While still connected as the database owner/admin, apply "
        "`scripts/customer_paper_ledger_schema.sql`"
    )
    acl_prerequisite_step = runbook.index(
        "5. Before running Alembic, inspect `pg_default_acl`"
    )
    migration_step = runbook.index(
        "Then apply the repository Alembic chain with `ENVIRONMENT=development`",
        acl_prerequisite_step,
    )
    assert schema_step < acl_prerequisite_step < migration_step
    assert runbook.count(
        "Then apply the repository Alembic chain with `ENVIRONMENT=development`"
    ) == 1
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
        "The original backup source has been independently correlated to Supabase logs."
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

    # The v3 comment marker records exact direct ACL snapshots and only the
    # introduced grantors. It also permits a narrowly isolated non-login
    # grantor to retain a pre-existing trigger-owner EXECUTE dependency.
    for state_key in (
        "'role_created_by_revision'",
        "'journals_policy_created_by_revision'",
        "'lines_policy_created_by_revision'",
        "'pre_public_schema_usage_acl'",
        "'pre_ledger_journals_select_acl'",
        "'pre_ledger_journal_lines_select_acl'",
        "'pre_validate_function_execute_acl'",
        "'introduced_public_schema_usage_grantors'",
        "'introduced_ledger_journals_select_grantors'",
        "'introduced_ledger_journal_lines_select_grantors'",
        "'introduced_validate_function_execute_grantors'",
        "'preserved_validate_function_grantor_oids'",
        "'prior_role_comment'",
    ):
        assert state_key in migration

    assert "grantor.rolcanlogin" in migration
    assert "grantor.rolsuper" in migration
    assert "membership.roleid = grantor.oid" in migration
    assert "membership.member = grantor.oid" in migration

    downgrade = migration.split("def downgrade()", 1)[1]
    assert "rollback provenance is missing" in downgrade
    assert "DROP POLICY atlas_ledger_trigger_owner_select ON public.ledger_journals" in downgrade
    assert "DROP POLICY atlas_ledger_trigger_owner_select ON public.ledger_journal_lines" in downgrade
    assert "DROP ROLE atlas_ledger_trigger_owner" in downgrade
    assert "role_created_by_revision" in downgrade
    assert "journals_policy_created_by_revision" in downgrade
    assert "lines_policy_created_by_revision" in downgrade
    assert "COMMENT ON ROLE %I IS %L" in downgrade
    assert "FROM pg_shdepend" in downgrade
    assert "AND NOT EXISTS (" in downgrade


