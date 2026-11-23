from scripts import check_no_secrets


def test_secret_scanner_covers_docs_android_resources_and_key_files():
    expected = {
        ".md", ".txt", ".xml", ".properties", ".java", ".kt", ".js",
        ".html", ".css", ".sql", ".pem", ".key", ".crt",
    }
    assert expected <= check_no_secrets.TEXT_SUFFIXES


def test_secret_scanner_detects_credentials_in_docs_android_and_private_key_files(
    tmp_path, monkeypatch
):
    groq_value = "qZ7mP2xR8vN4kL9sT5yW3uJ6cA0dF1hG"
    (tmp_path / "incident-notes.md").write_text(
        "GROQ_API" + "_KEY=" + groq_value + "\n", encoding="utf-8"
    )

    placeholder_value = "atlas-model-signing-private-key:<v>"
    (tmp_path / "deploy-example.md").write_text(
        "SECRET_MODEL_SIGNING_PRIVATE_REF=" + placeholder_value + "\n",
        encoding="utf-8",
    )

    helper_call = "expected_" + "token = make_readiness_token(database_url)"
    (tmp_path / "derived-token.py").write_text(helper_call + "\n", encoding="utf-8")

    # This exact fixed fixture is allowed, but longer values containing the
    # same marker must still be detected.
    exact_fixture = "ci-only-keystore-password"
    (tmp_path / "ci-fixture.yml").write_text(
        "RELEASE_STORE_" + "PASSWORD=" + exact_fixture + "\n",
        encoding="utf-8",
    )
    extended_fixture = "ci-only-rotated-value-that-is-not-an-approved-fixture-987654"
    (tmp_path / "fixture-marker.yml").write_text(
        "RELEASE_STORE_" + "PASSWORD=" + extended_fixture + "\n",
        encoding="utf-8",
    )

    verification_fixture = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    (tmp_path / "verification-fixture.yml").write_text(
        "APP_ENCRYPTION_KEY=" + verification_fixture + "\n",
        encoding="utf-8",
    )

    seed_marker_value = "ci-only-" + "rotated-seed-material-123456789"
    (tmp_path / "wallet.env").write_text(
        "private" + "_key=" + seed_marker_value + "\\n",
        encoding="utf-8",
    )

    (tmp_path / "signing.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\n", encoding="utf-8"
    )

    password_attribute = "pass" + "word"
    android_secret = "a4V8n2K6p9Q3x7M1r5T0w4Y8b2C6d9F3"
    (tmp_path / "android.xml").write_text(
        '<entry ' + password_attribute + '="' + android_secret + '"/>\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)
    findings = check_no_secrets.scan()

    assert any("incident-notes.md" in item for item in findings)
    assert any("signing.pem" in item and "private-key" in item for item in findings)
    assert any("android.xml" in item for item in findings)
    assert any("fixture-marker.yml" in item for item in findings)
    assert any("wallet.env" in item for item in findings)
    assert not any("deploy-example.md" in item for item in findings)
    assert not any("derived-token.py" in item for item in findings)
    assert not any("ci-fixture.yml" in item for item in findings)
    assert not any("verification-fixture.yml" in item for item in findings)


def test_secret_scanner_does_not_exempt_parenthesized_config_credentials(
    tmp_path, monkeypatch
):
    token_key = "ADMIN_" + "TOKEN"
    secret = "qZ7mP2xR8vN4kL9sT5yW3uJ6cA0dF1hG"
    (tmp_path / "rotation.yml").write_text(
        token_key + ": " + secret + "(rotation-note)\n", encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("rotation.yml" in item for item in findings)


def test_secret_scanner_finds_embedded_and_following_assignments(tmp_path, monkeypatch):
    inline_key = "pass" + "word"
    first_secret = "a4V8n2K6p9Q3x7M1r5T0w4Y8b2C6d9F3"
    embedded = 'print("start"); payload = "' + inline_key + "=" + first_secret + '"'
    (tmp_path / "embedded.py").write_text(embedded + "\n", encoding="utf-8")

    another_secret = "r7V2m9Q4x1L8p5N3k6T0w2Y9c4D7f1H"
    second = 'logger.info("started"); ' + "ADMIN_" + "TOKEN=" + another_secret
    (tmp_path / "after-statement.js").write_text(second + "\n", encoding="utf-8")

    unrelated = "notpasswordish=" + "s3cUr3ValueThatMustNotMatch123456"
    (tmp_path / "unrelated.yml").write_text(unrelated + "\n", encoding="utf-8")

    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)
    findings = check_no_secrets.scan()

    assert any("embedded.py" in item for item in findings)
    assert any("after-statement.js" in item for item in findings)
    assert not any("unrelated.yml" in item for item in findings)



def test_secret_scanner_ignores_source_attribute_wiring_not_quoted_credentials(
    tmp_path, monkeypatch
):
    source = (
        "api_key = settings.exchange_api_key\n"
        "api_secret = settings.exchange_api_secret\n"
        "password = config.customer_password\n"
        "token = secrets.token_urlsafe(24)\n"
        'payload = "password=qZ7mP2xR8vN4kL9sT5yW3uJ6cA0dF1hG"\n'
    )
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("source.py" in item for item in findings)
    assert len([item for item in findings if "source.py" in item]) == 1


def test_secret_scanner_reports_bracketed_private_key_but_allows_explicit_placeholder(
    tmp_path, monkeypatch
):
    bracketed_key = "<0123456789abcdef0123456789abcdef>"
    (tmp_path / "wallet.env").write_text(
        "private_key=" + bracketed_key + "\n"
        "private_key=<YOUR_PRIVATE_KEY>\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("wallet.env:1: seed/private-key assignment" in item for item in findings)
    assert not any("wallet.env:2:" in item for item in findings)
