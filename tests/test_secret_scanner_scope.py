from pathlib import Path

from scripts import check_no_secrets


def test_secret_scanner_covers_docs_android_resources_and_key_files():
    expected = {
        ".md", ".txt", ".xml", ".properties", ".java", ".kt", ".js",
        ".html", ".css", ".sql", ".pem", ".key", ".crt",
    }
    assert expected <= check_no_secrets.TEXT_SUFFIXES


def test_secret_scanner_detects_credentials_in_markdown_and_private_key_files(tmp_path, monkeypatch):
    (tmp_path / "incident-notes.md").write_text(
        "GROQ_API_KEY=fixture_secret_value_abcdefghijklmnopqrstuvwxyz\n",
        encoding="utf-8",
    )
    (tmp_path / "signing.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    (tmp_path / "android.xml").write_text(
        '<entry password="test_fixture_secret_value_with_length_1234567890"/>\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("incident-notes.md" in item for item in findings)
    assert any("signing.pem" in item and "private-key" in item for item in findings)
    assert any("android.xml" in item for item in findings)
