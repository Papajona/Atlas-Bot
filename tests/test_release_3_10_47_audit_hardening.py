from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_dependency_and_multidict_runtime_guard_are_declared():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "defusedxml==" in req
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    guards = (ROOT / "app" / "startup_guards.py").read_text(encoding="utf-8")
    assert "assert_multidict_safe_backend" in main
    assert 'os.environ.get("MULTIDICT_NO_EXTENSIONS") != "1"' in guards
    assert 'multidict.CIMultiDict.__module__' in guards


def test_repository_has_one_authoritative_android_source_tree():
    assert not (ROOT / "android").exists()
    assert (ROOT / "app" / "src" / "main" / "java").exists()
    assert (ROOT / "app" / "src" / "main" / "java" / "com" / "atlas" / "trading" / "MainActivity.kt").exists()


def test_no_root_level_github_actions_workflow_copy():
    assert not (ROOT / "workflows").exists()
    assert (ROOT / ".github" / "workflows" / "ci.yml").exists()


def test_application_zip_with_equal_length_invariant_is_strict():
    source = (ROOT / "app" / "trade_learning.py").read_text(encoding="utf-8")
    assert "zip(regime_path[:-1], regime_path[1:], strict=True)" in source


def test_startup_guards_are_not_duplicated_in_main():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "def _assert_multidict_safe_backend" not in main
    assert "async def _assert_database_migrations_current" not in main
    assert "from .startup_guards import" in main
