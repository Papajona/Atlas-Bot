"""Customer-facing API errors should display FastAPI validation details as readable text."""
from pathlib import Path


def test_customer_api_helper_formats_validation_arrays_and_uses_the_formatter():
    html = Path("app/templates/customer.html").read_text(encoding="utf-8")
    assert "function formatApiError(value)" in html
    assert "item.loc.slice(1).join('.')" in html
    assert "formatApiError(d.detail)||formatApiError(d.message)||'Request failed'" in html
