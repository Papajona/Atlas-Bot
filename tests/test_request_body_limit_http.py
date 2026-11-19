"""End-to-end check that oversized bodies get 413 before any handler (or auth) runs."""
import pytest

pytest.importorskip("aiosqlite")

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_oversized_webhook_body_rejected_with_413():
    limit = settings.max_request_body_bytes
    if limit <= 0:
        pytest.skip("body limit disabled")
    with TestClient(app) as client:
        response = client.post(
            "/api/internal/funding/webhook",
            content=b"x" * (limit + 1),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 413


def test_small_webhook_body_still_reaches_handler():
    with TestClient(app) as client:
        response = client.post(
            "/api/internal/funding/webhook",
            content=b"{}",
            headers={"content-type": "application/json"},
        )
        assert response.status_code != 413
