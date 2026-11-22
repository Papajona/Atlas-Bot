from scripts import check_no_secrets


def test_secret_scanner_covers_docs_android_resources_and_key_files():
    expected = {
        ".md", ".txt", ".xml", ".properties", ".java", ".kt", ".js",
        ".html", ".css", ".sql", ".pem", ".key", ".crt",
    }
    assert expected <= check_no_secrets.TEXT_SUFFIXES


def test_secret_scanner_detects_credentials_in_markdown_and_private_key_files(tmp_path, monkeypatch):
    (tmp_path / "incident-notes.md").write_text(
        "GROQ_API" + "_KEY=" + "qZ7mP2xR8vN4kL9sT5yW3uJ6cA0dF1hG" + "\\n",
        encoding="utf-8",
    )
    (tmp_path / "deploy-example.md").write_text(
        "SECRET_MODEL_SIGNING_PRIVATE_REF=atlas-model-signing-private-key:<v>\\n",
        encoding="utf-8",
    )
    (tmp_path / "derived-token.py").write_text(
        "expected_token = make_readiness_token(database_url)\\n",
        encoding="utf-8",
    )
    (tmp_path / "ci-fixture.yml").write_text(
        "RELEASE_STORE_PASSWORD=ci-only-keystore-password\\n",
        encoding="utf-8",
    )
    (tmp_path / "signing.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    (tmp_path / "android.xml").write_text(
        '<entry password="a4V8n2K6p9Q3x7M1r5T0w4Y8b2C6d9F3"/>\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("incident-notes.md" in item for item in findings)
    assert any("signing.pem" in item and "private-key" in item for item in findings)
    assert any("android.xml" in item for item in findings)
    assert not any("deploy-example.md" in item for item in findings)
    assert not any("derived-token.py" in item for item in findings)
    assert not any("ci-fixture.yml" in item for item in findings)
