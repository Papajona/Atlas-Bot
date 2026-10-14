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


def test_staging_preflight_rejects_the_production_project_as_staging():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(
        encoding="utf-8"
    )
    assert "ATLAS_PRODUCTION_SUPABASE_PROJECT_REF" in workflow
    assert '[[ "$STAGING_SUPABASE_PROJECT_REF" != "$PRODUCTION_SUPABASE_PROJECT_REF" ]]' in workflow
    assert '"gcloud", "run", "services", "describe"' in workflow


def test_staging_preflight_decodes_percent_encoded_database_credentials():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(
        encoding="utf-8"
    )
    assert "from urllib.parse import unquote, urlparse" in workflow
    assert '"PGUSER": unquote(parsed.username or "")' in workflow
    assert '"PGPASSWORD": unquote(parsed.password or "")' in workflow
