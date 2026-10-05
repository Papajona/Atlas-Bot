"""Static regression checks for the Atlas customer mobile/home surface."""
import pathlib
import re

PAGE = pathlib.Path(__file__).resolve().parents[1] / "app" / "templates" / "customer.html"


def _split():
    html = PAGE.read_text(encoding="utf-8")
    start = html.index("<script>") + len("<script>")
    end = html.rindex("</script>")
    return html[:start], html[start:end]


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
    head, js = _split()
    ids_in_html = set(re.findall(r'\bid="([^"]+)"', head))
    wanted = set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_\-]+)['\"]\s*\)", js))
    missing = sorted(wanted - ids_in_html)
    assert not missing, f"script reads ids that are not in the markup: {missing}"


def test_scanner_table_uses_value_tokens_not_labels():
    _, js = _split()
    assert "t[4]||" in js and "t[7]||" in js
    assert "t[3]||'—',t[6]" not in js


def test_customer_home_copy_is_financially_precise():
    head, _ = _split()
    assert "Account equity" in head
    assert "Open positions" in head
    assert "Open P&amp;L" in head
    assert "Realised P&amp;L &middot; 7 days" in head
    assert "Create paper trade" in head
    assert "Performance (7 days)" not in head


def test_home_chart_is_ledger_backed():
    head, js = _split()
    assert "TRADE_SETTLEMENT" in js
    assert "TRADING_FEE" in js
    assert 'aria-label="Seven day realised profit and loss chart"' in head
