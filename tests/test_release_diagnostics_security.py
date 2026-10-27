"""Security regression tests for admin release diagnostics input handling."""


def test_release_diagnostics_redacts_bearer_and_named_secrets():
    from app.main import _redact_release_diagnostic_text

    source = (
        "Authorization: Bearer abc.def.ghi\n"
        "AI_GATEWAY_API_KEY=supersecret123\n"
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


def test_hosted_ai_gateway_configuration_is_removed():
    from app.config import settings

    assert not hasattr(settings, "ai_gateway_api_key")
    assert not hasattr(settings, "ai_gateway_base_url")
    assert not hasattr(settings, "ai_gateway_model")
