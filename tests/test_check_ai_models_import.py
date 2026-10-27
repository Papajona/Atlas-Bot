from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_native_ai_checker_bootstraps_repo_root_before_app_import():
    text = (ROOT / "scripts" / "check_native_ai.py").read_text(encoding="utf-8")
    assert text.index("_REPOSITORY_ROOT = ") < text.index("from app.native_ai import native_ai_health_check")
