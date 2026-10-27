from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_native_ai_checker_runs_offline():
    from scripts.check_native_ai import main
    assert main() == 0


def test_legacy_checker_is_only_a_shim():
    text = (ROOT / "scripts" / "check_ai_models.py").read_text()
    assert "from scripts.check_native_ai import main" in text
    for token in ("generativelanguage.googleapis.com", "api.groq.com", "api.cerebras.ai", "urllib.request.urlopen"):
        assert token not in text


def test_native_ai_and_provider_facade_have_no_remote_model_endpoints():
    native = (ROOT / "app" / "native_ai.py").read_text()
    facade = (ROOT / "app" / "ai_providers.py").read_text()
    for token in ("generativelanguage.googleapis.com", "api.groq.com", "api.cerebras.ai", "httpx", "GEMINI_API_KEY", "GROQ_API_KEY"):
        assert token not in native
        assert token not in facade
