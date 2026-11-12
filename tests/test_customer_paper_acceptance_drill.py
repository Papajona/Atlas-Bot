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
