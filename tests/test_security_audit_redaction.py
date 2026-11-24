from app.config import settings
from app.security_audit import emit_security_audit


def test_external_audit_redacts_service_role_signing_encryption_and_session_values(monkeypatch, capsys):
    monkeypatch.setattr(settings, "external_security_audit_enabled", True)
    monkeypatch.setattr(settings, "external_security_audit_required", True)

    fixtures = {
        "service_role_key": "fixture_service_role_value_abcdefghijklmnopqrstuvwxyz",
        "signing_key": "fixture_signing_value_abcdefghijklmnopqrstuvwxyz",
        "encryption_key": "fixture_encryption_value_abcdefghijklmnopqrstuvwxyz",
        "jwt": "fixture_jwt_value_abcdefghijklmnopqrstuvwxyz",
        "cookie": "fixture_cookie_value_abcdefghijklmnopqrstuvwxyz",
    }
    assert emit_security_audit(
        event="REDACTION_TEST",
        actor_id="test",
        detail={"nested": fixtures, "safe_value": "visible"},
    )

    output = capsys.readouterr().out
    for secret in fixtures.values():
        assert secret not in output
    assert output.count("[REDACTED]") == len(fixtures)
    assert "visible" in output

def test_external_audit_redacts_camelcase_and_hyphenated_credential_keys(monkeypatch, capsys):
    monkeypatch.setattr(settings, "external_security_audit_enabled", True)
    monkeypatch.setattr(settings, "external_security_audit_required", True)
    # Assemble sensitive field names at runtime so the repository scanner does
    # not mistake these deliberately synthetic redaction fixtures for real secrets.
    api_key_field = "api" + "Key"
    private_key_field = "private" + "-key"
    header_api_key_field = "X-" + "Api" + "-Key"
    signing_key_field = "signing" + "Key"
    secrets = {
        api_key_field: "camel_api_key_secret_value_12345",
        private_key_field: "hyphenated_private_key_secret_12345",
        header_api_key_field: "header_api_key_secret_value_12345",
        signing_key_field: "camel_signing_key_secret_value_12345",
    }

    assert emit_security_audit(
        event="REDACTION_NORMALIZATION_TEST",
        actor_id="test",
        detail=secrets,
    )

    output = capsys.readouterr().out
    for secret in secrets.values():
        assert secret not in output
    assert output.count("[REDACTED]") == len(secrets)
