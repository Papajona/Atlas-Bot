from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_staging_identity_preflight_fails_closed_on_rls_migration_blockers():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(
        encoding="utf-8"
    )
    assert 'raise SystemExit("BLOCKED: runtime role neither bypasses RLS' in workflow
    assert 'raise SystemExit("BLOCKED: public tables without RLS remain' in workflow
    assert 'print("PASS: read-only target identity' in workflow


def test_staging_preflight_decodes_percent_encoded_database_credentials():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(
        encoding="utf-8"
    )
    assert "from urllib.parse import unquote, urlparse" in workflow
    assert '"PGUSER": unquote(parsed.username or "")' in workflow
    assert '"PGPASSWORD": unquote(parsed.password or "")' in workflow
