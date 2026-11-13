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



def test_acceptance_phase_two_invokes_database_rls_helper():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "customer_paper_acceptance_drill.py"
    ).read_text(encoding="utf-8")
    phase_two_start = source.index("async def _phase_two(")
    phase_two_end = source.index("\n\ndef _run(", phase_two_start)
    phase_two = source[phase_two_start:phase_two_end]
    assert "_verify_tenant_rls_visibility(" in phase_two
    assert "visible_customer_id=other_customer_id" in phase_two
    assert "hidden_customer_id=customer_id" in phase_two


@pytest.mark.postgres
@pytest.mark.skipif(
    not (
        os.getenv("ATLAS_PAPER_TEST_DATABASE_URL")
        and os.getenv("ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL")
    ),
    reason="requires disposable PostgreSQL paper and primary databases provisioned by CI",
)
def test_acceptance_rls_helper_hides_existing_other_tenant_rows(monkeypatch):
    """Execute the production RLS helper against real rows and prove rows exist via a bypass role."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.customer_paper_ledger import post_customer_paper_journal
    from app.customer_paper_lifecycle import submit_customer_paper_order

    paper_url = os.environ["ATLAS_PAPER_TEST_DATABASE_URL"]
    primary_url = os.environ["ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL"]
    monkeypatch.setattr(settings, "database_url", primary_url)
    monkeypatch.setattr(settings, "customer_paper_database_url", paper_url)
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", True)
    monkeypatch.setattr(settings, "customer_paper_shared_database_enabled", False)
    monkeypatch.setattr(settings, "paper_trading", True)
    monkeypatch.setattr(settings, "broker_sandbox", True)
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "customer_live_trading_enabled", False)
    monkeypatch.setattr(settings, "payout_live_enabled", False)
    monkeypatch.setattr(settings, "deriv_live_enabled", False)
    monkeypatch.setattr(settings, "usdt_tron_enabled", False)
    monkeypatch.setattr(settings, "usdt_tron_sweep_enabled", False)

    import hashlib
    import uuid

    customer_a = 8_100_000_000_000_000_000 + (uuid.uuid4().int % 1_000_000_000)
    customer_b = customer_a + 1
    run_key = uuid.uuid4().hex

    async def run():
        # Seed each tenant with real accounts and two separate balanced journals.
        for customer_id, label in ((customer_a, "a"), (customer_b, "b")):
            for index in (1, 2):
                seeded = await post_customer_paper_journal(
                    customer_id=customer_id,
                    idempotency_key=f"rls-{run_key}-{label}-seed-{index}",
                    event_type="PAPER_SEED",
                    currency="USDT",
                    lines=_seed_lines("100" if index == 1 else "1"),
                    reference=f"disposable-ci-rls-{label}-{index}",
                    metadata={"environment": "disposable-ci", "rls_test": True},
                )
                assert seeded["status"] == "APPENDED"

            order = await submit_customer_paper_order(
                customer_id=customer_id,
                client_order_id=f"rls-{run_key}-{label}-order",
                venue="binance",
                market_type="spot",
                symbol="BTC/USDT",
                side="BUY",
                quantity="0.001",
                currency="USDT",
                metadata={"execution_owner": "staging_acceptance_test", "rls_test": True},
            )
            assert order["status"] == "APPENDED"

        # Use the existing primary test login (superuser in the disposable service)
        # to prove the rows to be hidden really exist in the same paper database.
        owner_url = make_url(primary_url).set(
            database=make_url(paper_url).database
        ).render_as_string(hide_password=False)
        owner_engine = create_async_engine(owner_url, pool_pre_ping=True)
        try:
            async with owner_engine.connect() as conn:
                bypasses_rls = await conn.scalar(text("""
                    SELECT rolsuper OR rolbypassrls
                    FROM pg_roles
                    WHERE rolname = current_user
                """))
                assert bypasses_rls is True
                owner_rows = (await conn.execute(text("""
                    SELECT
                        (SELECT count(*) FROM customer_paper.accounts WHERE customer_id=:hidden) AS accounts,
                        (SELECT count(*) FROM customer_paper.orders WHERE customer_id=:hidden) AS orders,
                        (SELECT count(*) FROM customer_paper.journals WHERE customer_id=:hidden) AS journals
                """), {"hidden": customer_a})).mappings().one()
                assert int(owner_rows["accounts"]) >= 2
                assert int(owner_rows["orders"]) >= 1
                assert int(owner_rows["journals"]) >= 2
        finally:
            await owner_engine.dispose()

        # The helper switches to tenant B's session context and queries both B and A.
        # B's own rows must remain visible while A's known, owner-verified rows disappear.
        visibility = await drill._verify_tenant_rls_visibility(
            visible_customer_id=customer_b,
            hidden_customer_id=customer_a,
        )
        assert visibility["visible_accounts"] >= 2
        assert visibility["visible_orders"] >= 1
        assert visibility["visible_journals"] >= 2
        assert visibility["hidden_accounts"] == 0
        assert visibility["hidden_orders"] == 0
        assert visibility["hidden_journals"] == 0

    asyncio.run(run())


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
    assert 'expected_user = f"{expected_role}.{expected}"' in workflow
    assert "primary and paper database roles and credentials must be distinct" in workflow
    assert "PROCESS_ROLE: paper" in workflow


def test_acceptance_drill_wires_aggregate_capacity_and_rls_helper_calls():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "customer_paper_acceptance_drill.py"
    ).read_text(encoding="utf-8")
    phase_one_start = source.index("async def _phase_one(")
    phase_two_start = source.index("async def _phase_two(")
    phase_two_end = source.index("\n\ndef _run(", phase_two_start)
    phase_one = source[phase_one_start:phase_two_start]
    phase_two = source[phase_two_start:phase_two_end]
    assert "phase_one.enforce_aggregate_shared_bar_capacity" in phase_one
    assert "_verify_tenant_rls_visibility(" in phase_two
