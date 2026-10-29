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
