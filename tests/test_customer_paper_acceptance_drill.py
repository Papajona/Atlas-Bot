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
