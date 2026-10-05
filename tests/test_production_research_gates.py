"""Regression checks for production research-promotion safety boundaries."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
DEPLOY = (ROOT / "deploy" / "DEPLOY_WORKFLOW.md").read_text(encoding="utf-8")


def test_production_adaptive_ai_requires_nonzero_dsr_floor():
    assert 'if self.environment == "production" and self.adaptive_ai_enabled and self.research_min_deflated_sharpe < 0.95:' in CONFIG
    assert 'RESEARCH_MIN_DEFLATED_SHARPE must be >= 0.95 in production' in CONFIG


def test_deployment_docs_do_not_describe_off_dsr_as_production_default():
    assert '| `RESEARCH_MIN_DEFLATED_SHARPE` | `0.95`' in DEPLOY
    assert 'production adaptive-model promotion fails closed below 0.95' in DEPLOY


def test_research_backtests_use_shared_next_open_execution_model():
    research = (ROOT / "app" / "research_engine.py").read_text(encoding="utf-8")
    validation = (ROOT / "app" / "research_validation.py").read_text(encoding="utf-8")
    router = (ROOT / "app" / "strategy_router.py").read_text(encoding="utf-8")
    strategy = (ROOT / "app" / "strategy_engine.py").read_text(encoding="utf-8")
    assert "next_open_returns" in research
    assert "next_open_returns" in validation
    assert "next_open_returns" in router
    assert "def next_open_returns" in strategy
    assert 'df["close"].pct_change()' not in research
    assert 'df["close"].pct_change()' not in strategy
