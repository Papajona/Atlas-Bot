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
        "apiKey": "fixture_api_key_camel_abcdefghijklmnopqrstuvwxyz",
        "serviceRoleKey": "fixture_service_role_camel_abcdefghijklmnopqrstuvwxyz",
        "privateKey": "fixture_private_key_camel_abcdefghijklmnopqrstuvwxyz",
        "signingKey": "fixture_signing_key_camel_abcdefghijklmnopqrstuvwxyz",
        "encryptionKey": "fixture_encryption_key_camel_abcdefghijklmnopqrstuvwxyz",
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
