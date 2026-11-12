from __future__ import annotations

import asyncio
from argparse import Namespace
from types import SimpleNamespace

from scripts import customer_paper_acceptance_drill as drill


def test_acceptance_recovery_crosses_a_fresh_event_loop(monkeypatch):
    loops = []
    monkeypatch.setattr(
        drill,
        "settings",
        SimpleNamespace(
            database_url="postgresql+asyncpg://reader:secret@localhost/postgres",
            customer_paper_database_url="postgresql+asyncpg://paper:secret@localhost/postgres",
            customer_paper_ledger_enabled=True,
            paper_trading=True,
            broker_sandbox=True,
            live_trading_enabled=False,
            customer_live_trading_enabled=False,
            payout_live_enabled=False,
            deriv_live_enabled=False,
            usdt_tron_enabled=False,
            usdt_tron_sweep_enabled=False,
            customer_paper_shared_database_enabled=True,
        ),
    )

    async def phase_one(**kwargs):
        loops.append(asyncio.get_running_loop())
        return {"partial_fill": True}

    async def phase_two(**kwargs):
        loops.append(asyncio.get_running_loop())
        return {"post_restart_fill": "APPENDED"}

    monkeypatch.setattr(drill, "_phase_one", phase_one)
    monkeypatch.setattr(drill, "_phase_two", phase_two)

    report = drill._run(
        Namespace(run_id="unit-test-run", run_attempt="1", commit="a" * 40)
    )

    assert len(loops) == 2
    assert loops[0] is not loops[1]
    assert loops[0].is_closed()
    assert loops[1].is_closed()
    assert report["phase_one"]["partial_fill"] is True
    assert report["phase_two"]["post_restart_fill"] == "APPENDED"
    assert report["real_customer_authentication_tested"] is False



def test_failure_reason_redacts_credentials_and_synthetic_tenant_ids():
    exc = RuntimeError(
        "database failure password=topsecret postgresql://atlas:pw@db.example/postgres "
        "customer_id=8123456789012345678"
    )
    reason = drill._safe_failure_reason(exc)

    assert "topsecret" not in reason
    assert "atlas:pw" not in reason
    assert "db.example" not in reason
    assert "8123456789012345678" not in reason
    assert "[database-uri-redacted]" in reason
    assert "password=[redacted]" in reason



def test_staging_paper_drill_derives_supabase_url_from_validated_project_ref():
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "customer-paper-staging-acceptance.yml"
    ).read_text(encoding="utf-8")
    validate_at = workflow.index(
        '[[ "$STAGING_SUPABASE_PROJECT_REF" =~ ^[a-z0-9]{20}$ ]]'
    )
    derive_at = workflow.index(
        'echo "SUPABASE_URL=https://${STAGING_SUPABASE_PROJECT_REF}.supabase.co" >> "$GITHUB_ENV"'
    )
    run_at = workflow.index("python scripts/customer_paper_acceptance_drill.py")
    assert validate_at < derive_at < run_at
    assert "PROCESS_ROLE: paper" in workflow



def test_acceptance_drill_exercises_rls_between_two_seeded_tenants():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "customer_paper_acceptance_drill.py"
    ).read_text(encoding="utf-8")
    seed_second = source.index("ensure_customer_paper_account(other_customer_id, \"USDT\")")
    rls_start = source.index("async def _verify_tenant_rls_visibility(")
    rls_end = source.index("\n\nasync def _phase_one(", rls_start)
    rls_helper = source[rls_start:rls_end]
    phase_two_start = source.index("async def _phase_two(")
    phase_two_end = source.index("\n\ndef _run(", phase_two_start)
    phase_two = source[phase_two_start:phase_two_end]

    assert "set_config('app.customer_id'" in rls_helper
    assert "FROM customer_paper.accounts" in rls_helper
    assert "FROM customer_paper.orders" in rls_helper
    assert "FROM customer_paper.journals" in rls_helper
    assert "hidden_customer_id" in rls_helper
    assert "result[\"hidden_accounts\"] == 0" in rls_helper
    assert "result[\"hidden_orders\"] == 0" in rls_helper
    assert "result[\"hidden_journals\"] == 0" in rls_helper
    assert "visible_customer_id=other_customer_id" in phase_two
    assert "hidden_customer_id=customer_id" in phase_two
    assert seed_second < rls_start


def test_acceptance_workflow_is_pinned_to_the_triggering_deployment_sha():
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "customer-paper-staging-acceptance.yml"
    ).read_text(encoding="utf-8")
    assert "github.event.workflow_run.head_sha || github.sha" in workflow
    assert 'test "$TARGET_SHA" = "${{ github.event.workflow_run.head_sha }}"' in workflow
    assert 'ref: ${{ github.event.workflow_run.head_sha || github.sha }}' in workflow
    assert 'test "$TARGET_SHA" = "$GITHUB_SHA"' in workflow
    assert "git ls-remote" not in workflow
    assert "[atlas-paper-acceptance-drill]" not in workflow


def test_acceptance_workflow_checks_project_and_least_privilege_roles_before_drill():
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "customer-paper-staging-acceptance.yml"
    ).read_text(encoding="utf-8")
    validate_ref = workflow.index(
        '[[ "$STAGING_SUPABASE_PROJECT_REF" =~ ^[a-z0-9]{20}$ ]]'
    )
    derive_url = workflow.index(
        'echo "SUPABASE_URL=https://${STAGING_SUPABASE_PROJECT_REF}.supabase.co" >> "$GITHUB_ENV"'
    )
    validate_urls = workflow.index(
        "Verify database URLs target the configured Supabase project and identities"
    )
    execute_drill = workflow.index("python scripts/customer_paper_acceptance_drill.py")
    assert validate_ref < derive_url < validate_urls < execute_drill
    assert '[[ "$EXPECTED_RUNTIME_DB_ROLE" == "atlas_paper_reader" ]]' in workflow
    assert '[[ "$EXPECTED_PAPER_DB_ROLE" == "atlas_customer_paper" ]]' in workflow
    assert "expected_user = f"{'{'}expected_role{'}'}.{ '{' }expected{'}' }" not in workflow  # keep role binding in validator
    assert "primary and paper database roles and credentials must be distinct" in workflow
    assert "PROCESS_ROLE: paper" in workflow


def test_acceptance_drill_retains_aggregate_bar_and_database_rls_probes():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "customer_paper_acceptance_drill.py"
    ).read_text(encoding="utf-8")
    assert "phase_one.enforce_aggregate_shared_bar_capacity" in source
    assert 'Decimal(secondary_fill.get("filled_quantity", "0")) == Decimal("0.009")' in source
    assert 'Decimal(exhausted_bar["consumed"]) == Decimal("0.01")' in source
    assert "verify_database_row_level_tenant_isolation" in source
    for table in ("customer_paper.accounts", "customer_paper.orders", "customer_paper.journals"):
        assert f"SELECT count(*) FROM {table}" in source
    assert 'result["hidden_accounts"] == 0' in source
    assert 'result["hidden_orders"] == 0' in source
    assert 'result["hidden_journals"] == 0' in source
