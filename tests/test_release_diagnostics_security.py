"""Security regression tests for admin release diagnostics input handling."""


def test_release_diagnostics_redacts_bearer_and_named_secrets():
    from app.main import _redact_release_diagnostic_text

    source = (
        "Authorization: Bearer abc.def.ghi\n"
        "SERVICE_API_KEY=supersecret123\n"
        "password: dont-log-me\n"
        "ordinary gradle task failed"
    )
    redacted = _redact_release_diagnostic_text(source)
    assert "abc.def.ghi" not in redacted
    assert "supersecret123" not in redacted
    assert "dont-log-me" not in redacted
    assert "[REDACTED]" in redacted
    assert "ordinary gradle task failed" in redacted


def test_release_diagnostics_redacts_private_key_material():
    from app.main import _redact_release_diagnostic_text

    source = (
        "-----BEGIN PRIVATE KEY-----\n"
        "very-secret-private-key-material\n"
        "-----END PRIVATE KEY-----"
    )
    redacted = _redact_release_diagnostic_text(source)
    assert "very-secret-private-key-material" not in redacted
    assert "BEGIN PRIVATE KEY" in redacted
    assert "END PRIVATE KEY" in redacted


def test_external_release_analysis_configuration_and_calls_are_removed():
    from pathlib import Path
    from app.config import settings

    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "main.py").read_text(encoding="utf-8")
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    route_start = source.index('@app.post("/api/admin/release-diagnostics")')
    route_end = source.index("# Customer-authenticated spot paper orders", route_start)
    route_source = source[route_start:route_end]

    assert not hasattr(settings, "ai_gateway_api_key")
    assert not hasattr(settings, "ai_gateway_base_url")
    assert not hasattr(settings, "ai_gateway_model")
    assert "settings.ai_gateway_" not in source
    assert "httpx.AsyncClient" not in route_source
    assert "chat/completions" not in route_source
    assert "deterministic_local_rules" in source
    assert "AI_GATEWAY_API_KEY=" not in env_example
    assert "AI_GATEWAY_BASE_URL=" not in env_example
    assert "AI_GATEWAY_MODEL=" not in env_example
    assert "CEREBRAS_API_KEY=" not in env_example
    assert "CEREBRAS_MODEL=" not in env_example


def test_local_release_rules_flag_gradle_dependency_failures_without_network_calls():
    import json
    from app.main import _release_diagnostic_report

    report = json.loads(
        _release_diagnostic_report(
            "Could not resolve com.example:missing-library:1.2.3",
            "android { compileSdk = 35 }",
        )
    )

    assert report["analysis_mode"] == "deterministic_local_rules"
    assert "Gradle dependency failure" in report["likely_blockers"]
    assert "Could not resolve com.example:missing-library:1.2.3" in report["findings"][0]["evidence"][0]
