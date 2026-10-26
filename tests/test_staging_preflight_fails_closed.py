from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_staging_identity_preflight_fails_closed_on_rls_migration_blockers():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(
        encoding="utf-8"
    )
    # The paper runtime has a different contract from the production app: it may
    # connect only as the dedicated reader, in read-only mode, with no inherited
    # memberships, writable grants, extra schemas, callable public definer functions,
    # sequences, or owned tables.
    assert 'if db != "postgres" or role != os.environ["STAGING_RUNTIME_DB_ROLE"] or session_role != role:' in workflow
    assert "primary runtime role has administrative privileges or cannot login" in workflow
    assert "if not truth(bypass_rls) or truth(inherit) or int(memberships) or int(member_count):" in workflow
    assert "primary runtime role must be NOINHERIT/BYPASSRLS, with no inherited roles or unsafe Supabase-managed membership" in workflow
    assert "grantor_role.rolname='supabase_admin'" in workflow
    assert "to_jsonb(m) ? 'inherit_option'" in workflow
    assert "to_jsonb(m) ? 'set_option'" in workflow
    assert "if any(int(v) for v in (other_schema, unexpected_tables, public_sequences, callable_definers, owned_tables)):" in workflow
    assert "primary runtime role can access non-allowlisted objects or owns public tables" in workflow
    assert 'print(f"PASS: primary identity {role} is read-only, non-owner/non-admin, and can access only customer_profiles/app_state.' in workflow


def test_staging_preflight_allows_same_project_and_still_binds_database_host():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(
        encoding="utf-8"
    )
    assert "ATLAS_PRODUCTION_SUPABASE_PROJECT_REF" in workflow
    assert "ATLAS_STAGING_SUPABASE_PROJECT_REF" in workflow
    assert "Same-project staging selected; paper/live/payout safety flags and the dedicated paper schema remain mandatory." in workflow
    assert 'if [[ "$STAGING_SUPABASE_PROJECT_REF" == "$PRODUCTION_SUPABASE_PROJECT_REF" ]]; then' in workflow
    assert '[[ "$STAGING_SUPABASE_PROJECT_REF" != "$PRODUCTION_SUPABASE_PROJECT_REF" ]]' not in workflow
    assert 'expected = os.environ["STAGING_SUPABASE_PROJECT_REF"]' in workflow
    assert "DB hostname does not match the configured Supabase project" in workflow
    assert '"gcloud", "run", "services", "describe"' in workflow
    assert '"PAPER_TRADING": "true"' in workflow
    assert '"LIVE_TRADING_ENABLED": "false"' in workflow
    assert '"CUSTOMER_LIVE_TRADING_ENABLED": "false"' in workflow
    assert '"PAYOUT_LIVE_ENABLED": "false"' in workflow
    assert '"BROKER_SANDBOX": "true"' in workflow
    assert '"USDT_TRON_ENABLED": "false"' in workflow
    assert '"USDT_TRON_SWEEP_ENABLED": "false"' in workflow
    assert '"ATLAS_ASGI_APP": "app.paper_staging:app"' in workflow
    assert '"CUSTOMER_PAPER_DATABASE_URL"' in workflow


def test_staging_preflight_decodes_percent_encoded_database_credentials():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(
        encoding="utf-8"
    )
    assert "from urllib.parse import unquote, urlparse" in workflow
    assert '"PGUSER": unquote(parsed.username or "")' in workflow
    assert '"PGPASSWORD": unquote(parsed.password or "")' in workflow

def test_staging_preflight_classifies_bad_primary_password_without_exposing_database_output():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(
        encoding="utf-8"
    )
    assert 'diagnostic = (result.stderr or result.stdout or "").lower()' in workflow
    assert '"password authentication failed" in diagnostic' in workflow
    assert "existing atlas_paper_reader role" in workflow
    assert "without resetting the database role" in workflow
    assert "raw DB output and credentials withheld" in workflow
    assert "print(result.stderr" not in workflow
    assert "print(result.stdout" not in workflow

