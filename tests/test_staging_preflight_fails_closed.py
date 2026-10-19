from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_staging_identity_preflight_fails_closed_on_rls_migration_blockers():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(
        encoding="utf-8"
    )
    assert 'role in {"postgres", "service_role", "supabase_admin", "supabase_auth_admin", "authenticator", "anon", "authenticated"}' in workflow
    assert 'role has administrative privileges' in workflow
    assert "if not is_true(bypass_rls)" in workflow
    assert 'if int(owned_count) > 0' in workflow
    assert "role_membership_count" in workflow
    assert "runtime DB role inherits other PostgreSQL roles" in workflow
    assert 'raise SystemExit("BLOCKED: public tables without RLS remain' in workflow
    assert 'print("PASS: read-only target identity' in workflow


def test_staging_preflight_allows_same_project_and_still_binds_database_host():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(
        encoding="utf-8"
    )
    assert "ATLAS_PRODUCTION_SUPABASE_PROJECT_REF" in workflow
    assert "ATLAS_STAGING_SUPABASE_PROJECT_REF" in workflow
    assert "same-project supported" in workflow
    assert 'if [[ "$STAGING_SUPABASE_PROJECT_REF" == "$PRODUCTION_SUPABASE_PROJECT_REF" ]]; then' in workflow
    assert '[[ "$STAGING_SUPABASE_PROJECT_REF" != "$PRODUCTION_SUPABASE_PROJECT_REF" ]]' not in workflow
    assert "expected = os.environ[\"STAGING_SUPABASE_PROJECT_REF\"]" in workflow
    assert "database hostname does not match the explicitly configured staging Supabase project" in workflow
    assert '"gcloud", "run", "services", "describe"' in workflow


def test_staging_preflight_decodes_percent_encoded_database_credentials():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(
        encoding="utf-8"
    )
    assert "from urllib.parse import unquote, urlparse" in workflow
    assert '"PGUSER": unquote(parsed.username or "")' in workflow
    assert '"PGPASSWORD": unquote(parsed.password or "")' in workflow
