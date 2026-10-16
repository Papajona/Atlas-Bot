"""DOM integrity checks for both dashboards; browser behavior is tested in Playwright."""
import collections
import re
from pathlib import Path

import pytest

TEMPLATES = ["app/templates/dashboard.html", "app/templates/customer.html"]


def _split(path: str):
    html = Path(path).read_text(encoding="utf-8")
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    markup = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S)
    if path == "app/templates/dashboard.html":
        js = Path("app/static/admin-dashboard.js").read_text(encoding="utf-8")
    else:
        js = "\n".join(scripts)
    return markup, js


@pytest.mark.parametrize("path", TEMPLATES)
def test_no_duplicate_element_ids(path):
    markup, _ = _split(path)
    counts = collections.Counter(re.findall(r"""\bid=["']([^"']+)["']""", markup))
    assert {key: count for key, count in counts.items() if count > 1} == {}


@pytest.mark.parametrize("path", TEMPLATES)
def test_script_only_references_ids_that_exist(path):
    markup, js = _split(path)
    ids = set(re.findall(r"""\bid=["']([^"']+)["']""", markup))
    used = {
        first or second
        for first, second in re.findall(
            r"""getElementById\(["']([\w-]+)["']\)|\$\(["']([\w-]+)["']\)""",
            js,
        )
    }
    assert sorted(used - ids) == []


def test_withdrawal_rows_are_written_into_the_table_body_not_the_panel():
    markup, js = _split("app/templates/dashboard.html")
    assert '<tbody id="withdrawalRows">' in markup
    assert "$('withdrawalRows').innerHTML=" in js


def test_admin_dashboard_script_is_external_and_has_no_inline_event_handlers():
    html = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    assert '<script src="/static/admin-dashboard.js" defer></script>' in html
    assert not re.search(r"\son(?:click|change|submit|input)=", html, re.I)
    assert not re.search(r"<script>(.*?)</script>", html, re.S)
