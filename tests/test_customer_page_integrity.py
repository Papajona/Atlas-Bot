"""Static regression checks for the Atlas customer mobile/home surface."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "templates" / "customer.html"
JS = ROOT / "app" / "static" / "customer-dashboard.js"


def _split():
    html = PAGE.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    return html, js


def test_customer_script_is_external_and_markup_has_no_inline_handlers():
    html, _ = _split()
    assert '<script src="/static/customer-dashboard.js" defer></script>' in html
    assert not re.search(r"<script\s*>", html, re.I)
    assert not re.search(r"\son(?:click|change|submit|input)\s*=", html, re.I)


def test_every_wrapped_function_is_defined():
    _, js = _split()
    for original in re.findall(r"const\s+_\w+\s*=\s*([A-Za-z_]\w*)\s*;", js):
        assert re.search(rf"(?:async\s+)?function\s+{original}\s*\(", js), f"{original} is wrapped but never defined"


def test_customer_lifecycle_has_one_wrapper_layer():
    _, js = _split()
    assert js.count("const _loadAccount=loadAccount;") == 1
    assert js.count("const _loadProductWorkspace=loadProductWorkspace;") == 1
    assert "_loadAccountHome" not in js
    assert "_loadWorkspaceHome" not in js


def test_required_bootstrap_functions_exist():
    _, js = _split()
    defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_]\w*)", js))
    required = {"showAccount", "loadAccount", "loadProductWorkspace", "continueLoginSecurity"}
    assert required <= defined, required - defined


def test_script_element_ids_exist_in_markup():
    html, js = _split()
    ids_in_html = set(re.findall(r'\bid="([^"]+)"', html))
    wanted = set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_\-]+)['\"]\s*\)", js))
    missing = sorted(wanted - ids_in_html)
    assert not missing, f"script reads ids that are not in the markup: {missing}"


def test_scanner_table_uses_value_tokens_not_labels():
    _, js = _split()
    assert "t[4]||" in js and "t[7]||" in js
    assert "t[3]||'—',t[6]" not in js


def test_customer_home_copy_is_financially_precise():
    html, _ = _split()
    assert "Account equity" in html
    assert "Open positions" in html
    assert "Open P&amp;L" in html
    assert "Realised P&amp;L &middot; 7 days" in html
    assert "Create paper trade" in html
    assert "Performance (7 days)" not in html


def test_home_chart_is_ledger_backed():
    html, js = _split()
    assert "TRADE_SETTLEMENT" in js
    assert "TRADING_FEE" in js
    assert 'aria-label="Seven day realised profit and loss chart"' in html
