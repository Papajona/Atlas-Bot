import asyncio
import os
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_strategy_benchmark_capture_is_opt_in_and_private_by_default():
    schemas = (ROOT / "app" / "schemas.py").read_text(encoding="utf-8")
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    migration = (ROOT / "alembic" / "versions" / "0062_customer_benchmark_capture.py").read_text(encoding="utf-8")
    service = (ROOT / "app" / "benchmark_capture.py").read_text(encoding="utf-8")
    customer = (ROOT / "app" / "templates" / "customer.html").read_text(encoding="utf-8")

    assert schemas.count("benchmark_opt_in: bool = False") == 2
    assert "if req.benchmark_opt_in:" in main
    assert "prompt_ciphertext" in service and "native_output_ciphertext" in service
    assert 'encrypt_text("atlas-benchmark-prompt-v1:" + prompt)' in service
    assert 'encrypt_text(\n            "atlas-benchmark-output-v1:" + json.dumps(' in service
    assert "RETENTION_DAYS = 90" in service
    assert "MAX_CAPTURES_PER_24_HOURS = 20" in service
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "REVOKE ALL PRIVILEGES" in migration
    assert "FROM PUBLIC" in migration
    assert "strategyBenchmarkOptIn" in customer and "labBenchmarkOptIn" in customer
    assert 'type="checkbox"' in customer
    assert "Delete my benchmark submissions" in customer
    assert "id=\"labBenchmarkCaptureResult\"" in customer


def test_benchmark_submissions_can_only_be_deleted_by_the_authenticated_owner():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    start = main.index('@app.delete("/api/customer/benchmark-captures")')
    route = main[start:start + 1200]
    assert "get_customer(authorization, db, require_aal2=True)" in route
    assert "BenchmarkPromptCapture.customer_id == profile.id" in route
    assert "delete(BenchmarkPromptCapture)" in route


def test_benchmark_capture_guard_flags_obvious_emails_and_credentials():
    from app.benchmark_capture import contains_obvious_sensitive_text

    assert contains_obvious_sensitive_text("Use BTC for trading; contact me at person@example.com")
    assert contains_obvious_sensitive_text("The API key is abc123; use this prompt")
    assert not contains_obvious_sensitive_text(
        "Buy BTC after a 1h breakout, stop at 1.5 ATR, target 2R, and risk 0.5%."
    )


def test_benchmark_rate_limit_serializes_postgres_customer_writes():
    service = (ROOT / "app" / "benchmark_capture.py").read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock" in service
    assert "MAX_CAPTURES_PER_24_HOURS = 20" in service



def test_benchmark_capture_encrypts_user_text_even_when_it_starts_with_enc_prefix():
    service = (ROOT / "app" / "benchmark_capture.py").read_text(encoding="utf-8")
    assert '"atlas-benchmark-prompt-v1:" + prompt' in service
    assert '"atlas-benchmark-output-v1:" + json.dumps(' in service



def test_expired_benchmark_capture_cleanup_is_a_real_delete_operation():
    from datetime import datetime, timezone
    from app.benchmark_capture import purge_expired_benchmark_captures

    class Result:
        rowcount = 3

    class Database:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return Result()

    db = Database()
    deleted = asyncio.run(purge_expired_benchmark_captures(
        db, now=datetime(2026, 11, 19, tzinfo=timezone.utc)
    ))
    assert deleted == 3
    assert "DELETE" in str(db.statement).upper()
    assert "expires_at" in str(db.statement)


def test_benchmark_capture_retention_has_monitored_application_fallback():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    service = (ROOT / "app/benchmark_capture.py").read_text(encoding="utf-8")
    migration = (ROOT / "alembic/versions/0062_customer_benchmark_capture.py").read_text(encoding="utf-8")
    assert "async def _benchmark_capture_retention_loop" in main
    assert '"benchmark-capture-retention"' in main
    assert "_mark_worker_loop_success" in main
    assert "_mark_worker_loop_failure" in main
    assert "purge_expired_benchmark_captures(db)" in main
    assert "delete(BenchmarkPromptCapture)" in service
    assert "benchmark_capture_permissions" in main
    assert "permissions[\"can_delete\"]" in main
    assert "pg_cron" not in migration


def test_benchmark_consent_is_reset_after_each_request_and_on_prompt_edits():
    js = (ROOT / "app/static/customer-dashboard.js").read_text(encoding="utf-8")
    customer = (ROOT / "app/templates/customer.html").read_text(encoding="utf-8")
    assert "const benchmarkOptIn=!!consent?.checked" in js
    assert "if(consent)consent.checked=false" in js
    assert "document.getElementById('strategyPrompt')?.addEventListener('input'" in js
    assert "document.getElementById('labPrompt')?.addEventListener('input'" in js
    assert "labBenchmarkCaptureResult" in js
    assert js.index("labBenchmarkCaptureResult") < js.index("await validateCandidate(d.id)")
    assert "I confirm it contains no personal, account, contact, credential or other sensitive information." in customer




def test_strategy_builder_alias_returns_the_benchmark_capture_outcome(monkeypatch):
    from app import main as main_module

    async def verified_customer(_authorization, *, require_aal2=True):
        assert require_aal2 is True
        return {"sub": "customer-auth-id"}

    async def create_candidate(request, _authorization):
        assert request.benchmark_opt_in is True
        return {
            "id": 17,
            "spec": {"ai_provider": "native"},
            "status": "DRAFT",
            "benchmark_capture": {"status": "STORED"},
        }

    monkeypatch.setattr(main_module, "customer_claims", verified_customer)
    monkeypatch.setattr(main_module, "create_strategy_candidate", create_candidate)
    request = main_module.StrategyBuilderRequest(
        name="Benchmark test",
        prompt="Research a bullish breakout with clear risk rules.",
        benchmark_opt_in=True,
    )

    response = asyncio.run(
        main_module.strategy_builder(request, authorization="Bearer test-token")
    )

    assert response["benchmark_capture"] == {"status": "STORED"}


def test_benchmark_capture_runtime_role_has_only_required_table_access():
    migration = (ROOT / "alembic/versions/0062_customer_benchmark_capture.py").read_text(encoding="utf-8")
    provision = (ROOT / "scripts/provision_atlas_api_runtime.sql").read_text(encoding="utf-8")
    assert "GRANT SELECT, INSERT, DELETE ON TABLE public.customer_benchmark_captures TO atlas_api_runtime" in migration
    assert "GRANT USAGE, SELECT ON SEQUENCE public.customer_benchmark_captures_id_seq TO atlas_api_runtime" in migration
    assert "GRANT SELECT, INSERT, DELETE" in provision
    assert "ON TABLE public.customer_benchmark_captures TO atlas_api_runtime" in provision
    assert "ON SEQUENCE public.customer_benchmark_captures_id_seq TO atlas_api_runtime" in provision
    assert "'customer_benchmark_captures'" in provision
    assert "benchmark_capture_update" in provision
    assert "atlas_api_runtime must not UPDATE benchmark consent" in provision
    for forbidden in ("anon", "authenticated", "service_role", "PUBLIC"):
        assert f"GRANT SELECT, INSERT, DELETE ON TABLE public.customer_benchmark_captures TO {forbidden}" not in migration


def test_benchmark_consent_is_consumed_before_async_submission():
    js = (ROOT / "app/static/customer-dashboard.js").read_text(encoding="utf-8")
    for function_name, end_name in (
        ("async function buildStrategy(){", "async function loadDeposit()"),
        ("async function createLabCandidate(){", "async function loadBenchmarkCaptureStatus()"),
    ):
        start = js.index(function_name)
        end = js.index(end_name, start)
        block = js[start:end]
        snapshot = block.index("const benchmarkOptIn=!!consent?.checked")
        reset = block.index("if(consent)consent.checked=false")
        first_await = block.index("await api(")
        assert snapshot < reset < first_await
        assert "finally" not in block



def test_read_only_paper_runtime_does_not_attempt_benchmark_capture_queries():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    service = (ROOT / "app" / "benchmark_capture.py").read_text(encoding="utf-8")
    status_start = main.index('@app.get("/api/customer/benchmark-captures")')
    status = main[status_start:main.index('@app.delete("/api/customer/benchmark-captures")', status_start)]
    delete_start = main.index('@app.delete("/api/customer/benchmark-captures")')
    delete_route = main[delete_start:main.index('@app.post("/api/customer/strategy-lab/candidates")', delete_start)]
    assert 'if not permissions["can_select"] or not permissions["can_delete"]:' in status
    assert '"status": "UNAVAILABLE"' in status
    assert 'if not permissions["can_delete"]:' in delete_route
    assert '"status": "SKIPPED_STORAGE_UNAVAILABLE"' in service



def test_staging_read_only_role_reports_capture_unavailable_without_table_access():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    service = (ROOT / "app" / "benchmark_capture.py").read_text(encoding="utf-8")
    assert "has_table_privilege" in service
    assert 'permissions["can_select"]' in main
    assert 'permissions["can_delete"]' in main
    assert '"status": "UNAVAILABLE"' in main
    assert "SKIPPED_STORAGE_UNAVAILABLE" in service
    assert "Benchmark submissions are unavailable in this read-only runtime" in main


def test_benchmark_report_uses_the_application_pydantic_output_contract():
    evaluator = (ROOT / "scripts" / "eval_native_ai.py").read_text(encoding="utf-8")
    assert "from app.strategy_ai import AIStrategySpec" in evaluator
    assert "AIStrategySpec.model_validate(spec)" in evaluator
    assert "output includes unsupported fields or invalid structure" in evaluator



def test_benchmark_capture_status_marks_success_available_for_the_ui():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    route_start = main.index('@app.get("/api/customer/benchmark-captures")')
    route_end = main.index('@app.delete("/api/customer/benchmark-captures")', route_start)
    route = main[route_start:route_end]
    assert '"status": "UNAVAILABLE"' in route
    assert '"status": "AVAILABLE"' in route
    assert '"count": count' in route
    # Expiry purges span customers; their aggregate count is never exposed here.
    assert '"deleted_expired"' not in route


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("ATLAS_POSTGRES_URL"),
    reason="requires migrated PostgreSQL (ATLAS_POSTGRES_URL)",
)
async def test_postgres_concurrent_benchmark_capture_enforces_twenty_per_24_hours(monkeypatch):
    """Exercise the advisory-lock cap using separate PostgreSQL transactions."""
    from cryptography.fernet import Fernet
    from sqlalchemy import delete, func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.benchmark_capture import (
        MAX_CAPTURES_PER_24_HOURS,
        capture_strategy_prompt,
    )
    from app.config import settings
    from app.db import BenchmarkPromptCapture, CustomerProfile

    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")

    engine = create_async_engine(
        os.environ["ATLAS_POSTGRES_URL"],
        pool_size=30,
        max_overflow=0,
        connect_args={"statement_cache_size": 0},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    marker = uuid.uuid4().hex
    customer_id = None
    primary_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    try:
        async with sessions() as db:
            customer = CustomerProfile(
                auth_user_id=f"benchmark-cap-{marker}",
                email=f"benchmark-cap-{marker}@example.invalid",
                display_name="Benchmark capture concurrency test",
                status="ACTIVE",
            )
            db.add(customer)
            await db.commit()
            customer_id = customer.id

        async def submit(index):
            async with sessions() as db:
                result = await capture_strategy_prompt(
                    db,
                    customer_id=customer_id,
                    prompt=(
                        f"Research BTC trend entry after confirmed 1h breakout, "
                        f"case {index}; use a fixed 0.5 percent risk cap."
                    ),
                    native_output={"strategy": {"title": f"Concurrency case {index}"}},
                    asset="crypto",
                    symbol="BTC/USDT",
                    exchange="binance",
                    timeframe="1h",
                )
                await db.commit()
                return result["status"]

        submissions = await asyncio.gather(
            *(submit(index) for index in range(MAX_CAPTURES_PER_24_HOURS + 5)),
            return_exceptions=True,
        )
        submission_errors = [
            result for result in submissions if isinstance(result, BaseException)
        ]
        assert not submission_errors, (
            f"{len(submission_errors)} concurrent capture transaction(s) failed"
        )
        statuses = submissions
        assert statuses.count("STORED") == MAX_CAPTURES_PER_24_HOURS
        assert statuses.count("SKIPPED_DAILY_LIMIT") == 5

        async with sessions() as db:
            stored = int(
                (await db.execute(
                    select(func.count(BenchmarkPromptCapture.id)).where(
                        BenchmarkPromptCapture.customer_id == customer_id
                    )
                )).scalar_one()
            )
            assert stored == MAX_CAPTURES_PER_24_HOURS
    except BaseException as exc:
        # Preserve the actual assertion/submission failure while cleanup proceeds.
        primary_error = exc

    try:
        if customer_id is not None:
            async with sessions() as db:
                await db.execute(
                    delete(BenchmarkPromptCapture).where(
                        BenchmarkPromptCapture.customer_id == customer_id
                    )
                )
                await db.execute(
                    delete(CustomerProfile).where(CustomerProfile.id == customer_id)
                )
                await db.commit()
    except BaseException as exc:
        cleanup_error = exc
    finally:
        try:
            await engine.dispose()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc

    if primary_error is not None:
        raise primary_error.with_traceback(primary_error.__traceback__)
    if cleanup_error is not None:
        raise cleanup_error
