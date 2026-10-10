from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_staging_identity_preflight_fails_closed_on_rls_migration_blockers():
    workflow = (ROOT / ".github" / "workflows" / "staging-identity-preflight.yml").read_text(
        encoding="utf-8"
    )
    assert 'raise SystemExit("BLOCKED: runtime role neither bypasses RLS' in workflow
    assert 'raise SystemExit("BLOCKED: public tables without RLS remain' in workflow
    assert 'print("PASS: read-only target identity' in workflow
