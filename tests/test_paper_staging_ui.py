from fastapi.testclient import TestClient

from app.paper_staging import app


def test_paper_staging_serves_the_restricted_browser_workspace():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Atlas · Paper Testing" in response.text
    assert 'href="/paper-staging.css"' in response.text
    assert 'src="/paper-staging.js"' in response.text
    assert "PAPER ONLY" in response.text
    assert "Live trading" in response.text
    assert "Live payouts" in response.text
    assert 'id="retry-preflight"' in response.text
    assert "Recheck staging readiness" in response.text
    assert "<script>" not in response.text


def test_paper_staging_browser_assets_are_same_origin_and_cache_restricted():
    client = TestClient(app)
    script = client.get("/paper-staging.js")
    stylesheet = client.get("/paper-staging.css")

    assert script.status_code == 200
    assert script.headers["content-type"].startswith("application/javascript")
    assert script.headers["cache-control"] == "no-store"
    assert '"/api/customer/paper/orders"' in script.text
    assert '"/api/auth/mfa/verify"' in script.text
    assert "handlePaperGateError" in script.text
    assert '$("blocked-state").hidden = false' in script.text
    assert '$("retry-preflight").addEventListener' in script.text
    assert "localStorage" not in script.text
    assert "sessionStorage" not in script.text
    assert "/api/customer/withdraw" not in script.text
    assert "/api/customer/broker/" not in script.text

    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert stylesheet.headers["cache-control"] == "no-store"
    assert ".order-grid" in stylesheet.text
