"""Structural regression checks for the administrator login/control-plane boundary."""
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MarkupAudit(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.fragments: list[str] = []
        self.body_classes: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        element_id = attrs.get("id")
        if element_id:
            self.ids.append(element_id)
        href = attrs.get("href", "")
        if href.startswith("#"):
            self.fragments.append(href[1:])
        if tag == "body":
            self.body_classes.extend((attrs.get("class") or "").split())


def test_admin_template_has_unique_ids_and_valid_withdrawal_navigation():
    source = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    audit = MarkupAudit()
    audit.feed(source)

    duplicates = sorted(key for key, count in Counter(audit.ids).items() if count > 1)
    assert not duplicates, f"Duplicate HTML id attributes: {duplicates}"
    assert "withdrawal-approvals" in audit.ids
    assert "withdrawals" in audit.ids  # the queue's tbody remains addressable by the existing JS
    assert "withdrawal-approvals" in audit.fragments


def test_admin_controls_are_hidden_until_server_state_is_verified():
    html = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    css = (ROOT / "app/static/app.css").read_text(encoding="utf-8")
    audit = MarkupAudit()
    audit.feed(html)

    assert "admin-auth-pending" in audit.body_classes
    assert "body.admin-auth-pending main > section:not(#overview)" in css
    start = html.index("async function unlockAdminConsole()")
    state_check = html.index("typeof state.live_enabled!=='boolean'", start)
    positions_check = html.index("!Array.isArray(state.open_positions)", start)
    reveal = html.index("document.body.classList.remove('admin-auth-pending')", start)
    assert start < state_check < positions_check < reveal
    assert "await api('/api/state')" in html[start:reveal]
    assert "adminEmailField" in html[start:reveal]
    assert "adminPasswordField" in html[start:reveal]
    assert "adminLogoutBtn" in html[start:reveal]
    assert "function adminLogout()" in html
    assert "ait_admin_access_token" in html[html.index("function adminLogout()"):]


def test_ui_screenshot_capture_covers_customer_and_both_admin_states():
    source = (ROOT / "scripts/capture_ui_screenshots.mjs").read_text(encoding="utf-8")
    for screen in (
        "customer-desktop",
        "customer-mobile",
        "admin-login-desktop",
        "admin-login-mobile",
        "admin-workbench-desktop",
        "admin-workbench-mobile",
    ):
        assert screen in source
    assert "synthetic API replies only" in source
    assert "admin workbench visible before session verification" in source
