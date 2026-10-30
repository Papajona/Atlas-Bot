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
    assert 'python "${REPO_ROOT}/scripts/validate_groq_deploy_env.py"' in script
    assert 'dirname -- "${BASH_SOURCE[0]}") && pwd)/validate_groq_deploy_env.py' not in script
    assert "GEMINI_API_KEY" not in script
    assert "GEMINI_MODEL" not in script
    assert "GEMINI_STRATEGY_MODEL" not in script


def test_worker_keeps_groq_secret_optional_and_inherits_only_serving_api_revisions():
    script = WORKER_DEPLOY.read_text(encoding="utf-8")
    assert ': "${API_SERVICE:?Set API_SERVICE to the currently deployed Cloud Run API service}"' in script
    assert 'gcloud run services describe "$API_SERVICE"' in script
    assert 'gcloud run revisions describe "$API_REVISION"' in script
    assert "scripts/resolve_worker_groq_config.py" in script
    assert "--traffic-revisions" in script
    assert "--revision-config" in script
    assert 'GROQ_MODEL="${API_GROQ_CONFIG[0]}"' in script
    assert 'GROQ_MAX_COMPLETION_TOKENS="${API_GROQ_CONFIG[1]}"' in script
    assert 'GROQ_RESEARCH_REVIEW_ENABLED="${API_GROQ_CONFIG[2]}"' in script
    assert 'SECRET_GROQ_REF="${API_GROQ_CONFIG[3]}"' in script
    assert 'if [[ "${REVISION_GROQ_CONFIG[$index]}" != "${API_GROQ_CONFIG[$index]}" ]]' in script
    assert "API revisions receiving traffic have different Groq configuration" in script
    assert 'SECRET_GROQ_REF="${SECRET_GROQ_REF:-}"' not in script
    assert ': "${GROQ_MODEL:?Set GROQ_MODEL' not in script
    assert ': "${GROQ_MAX_COMPLETION_TOKENS:?Set GROQ_MAX_COMPLETION_TOKENS' not in script
    assert ': "${GROQ_RESEARCH_REVIEW_ENABLED:?Set GROQ_RESEARCH_REVIEW_ENABLED' not in script
    assert 'GROQ_MODEL="${GROQ_MODEL:-openai/gpt-oss-20b}"' not in script
    assert 'python "${REPO_ROOT}/scripts/validate_groq_deploy_env.py"' in script
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
    assert "API_SERVICE=atlas-api \\" in runbook
    assert "reads the active API service revision" in runbook
    assert "does not accept an independent worker Groq configuration" in runbook
    assert "optional pinned `GROQ_API_KEY` Secret Manager reference" in runbook
    assert "GROQ_STRATEGY_MODEL" in runbook


def test_worker_groq_config_is_resolved_from_the_active_api_revision():
    import json
    import subprocess
    import sys

    resolver = ROOT / "scripts" / "resolve_worker_groq_config.py"
    payload = {
        "spec": {"template": {"spec": {"containers": [{
            "env": [
                {"name": "GROQ_MODEL", "value": "openai/gpt-oss-20b"},
                {"name": "GROQ_MAX_COMPLETION_TOKENS", "value": "2048"},
                {"name": "GROQ_RESEARCH_REVIEW_ENABLED", "value": "true"},
                {"name": "GROQ_API_KEY", "valueSource": {"secretKeyRef": {
                    "secret": "atlas-groq-api-key", "version": "7"
                }}},
            ]
        }]}}}
    }
    result = subprocess.run(
        [sys.executable, str(resolver)], input=json.dumps(payload), text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["openai/gpt-oss-20b", "2048", "true", "atlas-groq-api-key:7"]


def test_worker_groq_config_resolver_fails_closed_on_missing_api_override():
    import json
    import subprocess
    import sys

    resolver = ROOT / "scripts" / "resolve_worker_groq_config.py"
    payload = {"spec": {"template": {"spec": {"containers": [{"env": [
        {"name": "GROQ_MODEL", "value": "openai/gpt-oss-20b"},
        {"name": "GROQ_MAX_COMPLETION_TOKENS", "value": "2048"},
    ]}]}}}}
    result = subprocess.run(
        [sys.executable, str(resolver)], input=json.dumps(payload), text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 2
    assert "GROQ_RESEARCH_REVIEW_ENABLED" in result.stderr


def test_worker_groq_config_resolver_supports_cloud_run_v2_without_optional_secret():
    import json
    import subprocess
    import sys

    resolver = ROOT / "scripts" / "resolve_worker_groq_config.py"
    payload = {"template": {"containers": [{"env": [
        {"name": "GROQ_MODEL", "value": "openai/gpt-oss-20b"},
        {"name": "GROQ_MAX_COMPLETION_TOKENS", "value": "1024"},
        {"name": "GROQ_RESEARCH_REVIEW_ENABLED", "value": "false"},
    ]}]}}
    result = subprocess.run(
        [sys.executable, str(resolver)], input=json.dumps(payload), text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["openai/gpt-oss-20b", "1024", "false", "__NONE__"]
