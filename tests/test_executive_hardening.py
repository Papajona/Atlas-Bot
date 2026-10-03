from pathlib import Path

from app.custody_reconciliation import solvency_decision

ROOT = Path(__file__).resolve().parents[1]


def test_custody_solvency_blocks_under_collateralized_book():
    result = solvency_decision(assets=99, liabilities=100, minimum_ratio=1)
    assert result["status"] == "SHORTFALL"
    assert result["coverage_gap"] == -1
    assert result["reason"] == "BELOW_THRESHOLD"


def test_custody_solvency_passes_at_full_coverage():
    result = solvency_decision(assets=100, liabilities=100, minimum_ratio=1)
    assert result["status"] == "OK"


def test_billing_routes_require_finance_role():
    source = (ROOT / "app/main.py").read_text()
    for marker in (
        "async def admin_billing_cost",
        "async def admin_billing_revenue",
        "async def admin_billing_margin",
    ):
        start = source.index(marker)
        end = source.find("\n\n", start)
        block = source[start:end if end != -1 else start + 800]
        assert 'await require_role(claims, "FINANCE")' in block


def test_adaptive_policy_receives_configured_promotion_gates():
    source = (ROOT / "app/main.py").read_text()
    assert "settings.research_min_deflated_sharpe" in source
    assert "settings.research_require_cost_stress" in source
    assert "settings.research_cost_stress_multiplier" in source


def test_adaptive_wfo_emits_promotion_evidence():
    source = (ROOT / "app/trading_core.py").read_text()
    assert "cost_stress_ok" in source
    assert "deflated_sharpe" in source


def test_release_identity_files_are_consistent():
    assert 'app_version: str = "3.10.46"' in (ROOT / "app/config.py").read_text()
    assert '__version__ = "3.10.46"' in (ROOT / "app/__init__.py").read_text()
    android = (ROOT / "android/app/build.gradle.kts").read_text()
    assert "versionCode = 1046" in android
    assert 'versionName = "3.10.46"' in android


def test_deploy_scripts_require_immutable_identity():
    api = (ROOT / "deploy/cloud-run-deploy.sh").read_text()
    worker = (ROOT / "deploy/cloud-run-worker-deploy.sh").read_text()
    assert "git status --porcelain" in api
    assert "git rev-parse HEAD" in api
    assert "image_summary.digest" in api
    assert "@$IMAGE_DIGEST" in worker
    assert "RELEASE_SHA" in worker
