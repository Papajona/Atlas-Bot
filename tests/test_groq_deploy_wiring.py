"""Regression tests for Native AI with optional, research-only Groq deployment wiring."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_DEPLOY = ROOT / "deploy" / "cloud-run-deploy.sh"
WORKER_DEPLOY = ROOT / "deploy" / "cloud-run-worker-deploy.sh"
PRODUCTION_WORKFLOW = ROOT / ".github" / "workflows" / "production-deploy.yml"
ENV_EXAMPLE = ROOT / ".env.example"


def test_api_keeps_groq_secret_optional_and_gemini_removed():
    script = API_DEPLOY.read_text(encoding="utf-8")
    assert 'SECRET_GROQ_API_KEY_VERSION="${SECRET_GROQ_API_KEY_VERSION:-}"' in script
    assert "GROQ_API_KEY=atlas-groq-api-key:${SECRET_GROQ_API_KEY_VERSION}" in script
    assert "GROQ_SECRET_ARGS" in script
    assert "GROQ_MODEL=${GROQ_MODEL}" in script
    assert "GROQ_MAX_COMPLETION_TOKENS=${GROQ_MAX_COMPLETION_TOKENS}" in script
    assert "GROQ_RESEARCH_REVIEW_ENABLED=${GROQ_RESEARCH_REVIEW_ENABLED}" in script
    assert 'GROQ_MODEL="${GROQ_MODEL:-openai/gpt-oss-20b}"' in script
    assert 'GROQ_MAX_COMPLETION_TOKENS="${GROQ_MAX_COMPLETION_TOKENS:-2048}"' in script
    assert 'GROQ_RESEARCH_REVIEW_ENABLED="${GROQ_RESEARCH_REVIEW_ENABLED:-true}"' in script
    assert "validate_groq_deploy_env.py" in script
    assert "GEMINI_API_KEY" not in script
    assert "GEMINI_MODEL" not in script
    assert "GEMINI_STRATEGY_MODEL" not in script


def test_worker_keeps_groq_secret_optional_and_gemini_removed():
    script = WORKER_DEPLOY.read_text(encoding="utf-8")
    assert 'SECRET_GROQ_REF="${SECRET_GROQ_REF:-}"' in script
    assert "GROQ_API_KEY=${SECRET_GROQ_REF}" in script
    assert "${GROQ_SECRET_ARG}" in script
    assert "GROQ_RESEARCH_REVIEW_ENABLED=${GROQ_RESEARCH_REVIEW_ENABLED}" in script
    assert "GEMINI_API_KEY" not in script
    assert "GEMINI_MODEL" not in script
    assert "GEMINI_STRATEGY_MODEL" not in script
    assert "LIVE_TRADING_ENABLED=false" in script
    assert "CUSTOMER_LIVE_TRADING_ENABLED=false" in script
    assert "PAYOUT_LIVE_ENABLED=false" in script
    assert "BROKER_SANDBOX=true" in script
    assert "PAPER_TRADING=true" in script


def test_production_workflow_passes_optional_groq_secret_without_gemini():
    workflow = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
    assert "SECRET_GROQ_API_KEY_VERSION: ${{ secrets.SECRET_GROQ_API_KEY_VERSION }}" in workflow
    assert "GROQ_MODEL: ${{ vars.GROQ_MODEL }}" in workflow
    assert "GROQ_MAX_COMPLETION_TOKENS: ${{ vars.GROQ_MAX_COMPLETION_TOKENS }}" in workflow
    assert "GROQ_RESEARCH_REVIEW_ENABLED: ${{ vars.GROQ_RESEARCH_REVIEW_ENABLED }}" in workflow
    assert "SECRET_GEMINI_API_KEY_VERSION" not in workflow
    assert "GEMINI_MODEL" not in workflow
    assert "GEMINI_STRATEGY_MODEL" not in workflow


def test_example_documents_optional_groq_research_review_only():
    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "GROQ_API_KEY=" in env
    assert "GROQ_MODEL=openai/gpt-oss-20b" in env
    assert "GROQ_MAX_COMPLETION_TOKENS=2048" in env
    assert "GROQ_RESEARCH_REVIEW_ENABLED=true" in env
    assert "GEMINI_API_KEY" not in env


def test_bootstrap_creates_and_grants_groq_secret_only_when_configured():
    bootstrap = (ROOT / "deploy" / "gcp-bootstrap.sh").read_text(encoding="utf-8")
    assert 'if [[ -n "${GROQ_API_KEY:-}" ]]; then' in bootstrap
    assert 'create_secret atlas-groq-api-key "$GROQ_API_KEY"' in bootstrap
    assert 'if gcloud secrets describe atlas-groq-api-key' in bootstrap
    assert 'grant_secret_access atlas-groq-api-key "$SERVICE_ACCOUNT"' in bootstrap
    assert 'grant_secret_access atlas-groq-api-key "$WORKER_SERVICE_ACCOUNT"' in bootstrap
    assert 'GROQ_API_KEY:?' not in bootstrap

def test_daily_research_runs_bounded_optional_groq_reviews_concurrently():
    source = (ROOT / "app" / "daily_research.py").read_text(encoding="utf-8")
    assert "asyncio.Semaphore(max(1, min(3, len(intelligence[\"histories\"]))))" in source
    assert "asyncio.create_task(" in source
    assert "bounded_groq_review(native_research_input)" in source
    assert "return await groq_research_review(research_input, intelligence)" in source
    assert "await asyncio.gather(" in source
    assert "return_exceptions=True" in source
    assert 'review["groq_research_review"] = await groq_research_review' not in source
    assert source.index("persistence_result = await _persist_research_run(") < source.index(
        "groq_task = asyncio.create_task("
    )
    assert "research_run_id = (" in source
    assert "select(ResearchRun).where(ResearchRun.id == research_run_id)" in source
    assert 'await _update_persisted_research_review(item.get("research_run_id"), review)' in source
    assert "task.cancel()" in source


def test_worker_runbook_does_not_require_a_groq_secret_placeholder():
    runbook = (ROOT / "deploy" / "DEPLOY_WORKFLOW.md").read_text(encoding="utf-8")
    assert "SECRET_GROQ_REF=atlas-groq-api-key:<v>" not in runbook
    assert "is optional and must be an exact" in runbook
