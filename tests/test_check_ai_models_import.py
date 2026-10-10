from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ai_model_check_bootstraps_repository_root_before_app_import():
    script = (ROOT / "scripts" / "check_ai_models.py").read_text(encoding="utf-8")
    root_bootstrap = script.index("_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]")
    app_import = script.index("from app.config import settings")
    assert root_bootstrap < app_import
    assert "sys.path.insert(0, str(_REPOSITORY_ROOT))" in script
