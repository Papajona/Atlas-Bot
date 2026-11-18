"""Runtime configuration guard tests for API hardening."""
import pytest

from app.config import Settings


def _settings():
    return Settings(environment="test")


def test_production_requires_admin_metrics_and_disables_api_docs():
    with pytest.raises(ValueError, match="METRICS_REQUIRE_ADMIN must remain true"):
        _settings().model_copy(update={
            "environment": "production",
            "metrics_require_admin": False,
        })._production_security_requirements()

    with pytest.raises(ValueError, match="DOCS_ENABLED must remain false"):
        _settings().model_copy(update={
            "environment": "production",
            "docs_enabled": True,
        })._production_security_requirements()


def test_request_body_limit_must_be_non_negative_and_enabled_in_staging_and_production():
    with pytest.raises(ValueError, match="MAX_REQUEST_BODY_BYTES must be >= 0"):
        _settings().model_copy(update={
            "max_request_body_bytes": -1,
        })._production_security_requirements()

    with pytest.raises(ValueError, match="MAX_REQUEST_BODY_BYTES must be > 0"):
        _settings().model_copy(update={
            "environment": "staging",
            "max_request_body_bytes": 0,
        })._production_security_requirements()


def test_development_or_test_may_disable_request_body_limit():
    _settings().model_copy(update={
        "max_request_body_bytes": 0,
    })._production_security_requirements()
