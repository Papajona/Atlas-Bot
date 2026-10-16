"""Customer-facing API errors should display FastAPI validation details as readable text."""
from pathlib import Path


def test_customer_api_helper_formats_validation_arrays_and_uses_the_formatter():
    template = Path("app/templates/customer.html").read_text(encoding="utf-8")
    js = Path("app/static/customer-dashboard.js").read_text(encoding="utf-8")
    assert '<script src="/static/customer-dashboard.js" defer></script>' in template
    assert "function formatApiError(value)" in js
    assert "item.loc.slice(1).join('.')" in js
    assert "formatApiError(d.detail)||formatApiError(d.message)||'Request failed'" in js
