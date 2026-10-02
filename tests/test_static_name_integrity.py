"""Fail the build on the bug classes that shipped in 3.10.45: undefined names and shadowed imports."""
import subprocess
import sys


def _pyflakes():
    out = subprocess.run([sys.executable, "-m", "pyflakes", "app"], capture_output=True, text=True).stdout
    return out.splitlines()


_INTELLIGENCE_FILES = [
    "app/adaptive_bot.py", "app/meta_labeling.py", "app/strategy_ai.py",
    "app/strategy_router.py", "app/research_engine.py", "app/market_intelligence.py",
    "app/daily_research.py",
]


def test_intelligence_layer_has_no_undefined_names_or_shadowed_imports():
    """Same class of bug main.py/execution.py had (undefined names, shadowed imports),
    checked explicitly for the strategy/ML/research modules so it can't recur silently."""
    out = subprocess.run([sys.executable, "-m", "pyflakes", *_INTELLIGENCE_FILES],
                        capture_output=True, text=True).stdout
    assert out.strip() == "", out


def test_no_undefined_names():
    bad = [l for l in _pyflakes() if "undefined name" in l]
    assert not bad, "\n".join(bad)


def test_no_shadowed_route_or_import_redefinitions():
    bad = [l for l in _pyflakes() if "redefinition of unused" in l and "base64" not in l]
    assert not bad, "\n".join(bad)


def test_portal_pages_and_static_render():
    from fastapi.testclient import TestClient
    import app.main as main
    client = TestClient(main.app)
    for path in ("/", "/admin", "/static/customer.css"):
        assert client.get(path).status_code == 200, path
