import json

from scripts import check_no_secrets


def test_secret_scanner_covers_docs_android_resources_and_key_files():
    expected = {
        ".md", ".txt", ".xml", ".properties", ".java", ".kt", ".js",
        ".ts", ".tsx", ".mjs", ".cjs", ".tf", ".tfvars", ".dockerignore",
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
        "private" + "_key=" + seed_marker_value + "\n",
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


def test_secret_scanner_detects_recovery_phrases_after_documentation_labels(
    tmp_path, monkeypatch
):
    phrase = " ".join(["abandon"] * 11 + ["about"])
    (tmp_path / "wallet-notes.md").write_text(
        "Wallet mnemonic: " + phrase + "\n", encoding="utf-8"
    )
    (tmp_path / "redacted-wallet-notes.md").write_text(
        "Wallet mnemonic: <example phrase>\n", encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "wallet-notes.md:1: seed/private-key assignment" in item
        for item in findings
    )
    assert not any("redacted-wallet-notes.md" in item for item in findings)


def test_secret_scanner_detects_escaped_json_private_keys_in_source_literals(
    tmp_path, monkeypatch
):
    token = "0123456789abcdef" * 2
    payload = '{"private_key": "' + token + '"}'
    source = "const settings = " + json.dumps(payload) + ";\n"
    (tmp_path / "escaped-private-key.js").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "escaped-private-key.js:1: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_detects_later_mnemonic_after_placeholder_in_source_string(
    tmp_path, monkeypatch
):
    phrase = " ".join(["abandon"] * 11 + ["about"])
    payload = (
        '{"private_key": "example", "mnemonic": "' + phrase + '"}'
    )
    source = "const settings = " + json.dumps(payload) + ";\n"
    (tmp_path / "later-wallet-phrase.js").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "later-wallet-phrase.js:1: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_detects_later_mnemonic_after_placeholder_in_direct_source_object(
    tmp_path, monkeypatch
):
    phrase = " ".join(["abandon"] * 11 + ["about"])
    source = (
        'const settings = { private_key: "example", mnemonic: "'
        + phrase
        + '" };'
    )
    (tmp_path / "later-wallet-object.js").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "later-wallet-object.js:1: seed/private-key assignment" in item
        for item in findings
    )



def test_secret_scanner_detects_multiline_documented_wallet_phrases(
    tmp_path, monkeypatch
):
    words = ["abandon"] * 11 + ["about"]
    markdown = "Wallet mnemonic:\n" + "".join("  " + word + "\n" for word in words)
    (tmp_path / "wallet-notes.md").write_text(markdown, encoding="utf-8")
    (tmp_path / "wallet-notes.txt").write_text(
        "Recovery phrase:\n  " + " ".join(words) + "\n", encoding="utf-8"
    )
    (tmp_path / "redacted-wallet-notes.md").write_text(
        "Wallet mnemonic:\n  <redacted>\n", encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "wallet-notes.md:1: seed/private-key assignment" in item
        for item in findings
    )
    assert any(
        "wallet-notes.txt:1: seed/private-key assignment" in item
        for item in findings
    )
    assert not any("redacted-wallet-notes.md" in item for item in findings)


def test_secret_scanner_detects_markdown_formatted_recovery_labels_and_values(
    tmp_path, monkeypatch
):
    phrase = " ".join(["abandon"] * 11 + ["about"])
    (tmp_path / "formatted-label.md").write_text(
        "**Mnemonic:**\n  " + "\n  ".join(["abandon"] * 11 + ["about"]) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "formatted-value.md").write_text(
        "Mnemonic: " + chr(96) + phrase + chr(96) + "\n", encoding="utf-8"
    )
    (tmp_path / "formatted-redacted.md").write_text(
        "**Mnemonic:** " + chr(96) + "<redacted>" + chr(96) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "formatted-label.md:1: seed/private-key assignment" in item
        for item in findings
    )
    assert any(
        "formatted-value.md:1: seed/private-key assignment" in item
        for item in findings
    )
    assert not any("formatted-redacted.md" in item for item in findings)


def test_secret_scanner_does_not_exempt_parenthesized_config_credentials(
    tmp_path, monkeypatch
):
    token_key = "ADMIN_" + "TOKEN"
    secret = "abcdefghijklmnopqrst"
    (tmp_path / "rotation.yml").write_text(
        token_key + ": " + secret + "(rotation)\n", encoding="utf-8"
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
        "api_key = " + "settings.exchange_api_key\n"
        "api_secret = " + "settings.exchange_api_secret\n"
        "password = " + "config.customer_password\n"
        "token = " + "secrets.token_urlsafe(24)\n"
        'payload = "' + "pass" + "word=" + "qZ7mP2xR8vN4kL9sT5yW3uJ6cA0dF1hG" + '"\n'
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
    placeholder_key = "<YOUR_PRIVATE_KEY>"
    private_key_assignment = "private" + "_key="
    (tmp_path / "wallet.env").write_text(
        private_key_assignment + bracketed_key + "\n"
        + private_key_assignment + placeholder_key + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("wallet.env:1: seed/private-key assignment" in item for item in findings)
    assert not any("wallet.env:2:" in item for item in findings)


def test_secret_scanner_detects_quoted_dotenv_and_yaml_list_mnemonics(
    tmp_path, monkeypatch
):
    phrase = (" ".join(["ab" + "andon"] * 11 + ["abo" + "ut"]))
    dotenv_key = "MNE" + "MONIC"
    yaml_key = "mne" + "monic"
    (tmp_path / "quoted-recovery.env").write_text(
        dotenv_key + '="' + phrase + '"\n',
        encoding="utf-8",
    )
    (tmp_path / "yaml-recovery.yml").write_text(
        "- " + yaml_key + ": " + phrase + "\n",
        encoding="utf-8",
    )
    (tmp_path / "nested-wallet.yml").write_text(
        'wallet: {mnemonic: "' + phrase + '"}\n',
        encoding="utf-8",
    )
    (tmp_path / "ordinary-notes.yml").write_text(
        "description: mnemonic: " + phrase + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("quoted-recovery.env:1: seed/private-key assignment" in item for item in findings)
    assert any("yaml-recovery.yml:1: seed/private-key assignment" in item for item in findings)
    assert any("nested-wallet.yml:1: seed/private-key assignment" in item for item in findings)
    assert not any("ordinary-notes.yml" in item for item in findings)



def test_secret_scanner_detects_multiline_yaml_recovery_phrases_and_allows_empty_or_placeholder(
    tmp_path, monkeypatch
):
    phrase_key = "mne" + "monic"
    block_key = "seed" + "_phrase"
    single_line_value = (" ".join(["ab" + "andon"] * 11 + ["abo" + "ut"]))
    first_block_line = "abandon abandon abandon abandon abandon abandon"
    second_block_line = (" ".join(["ab" + "andon"] * 11 + ["abo" + "ut"]))

    (tmp_path / "multiline-recovery.yml").write_text(
        "wallet:\n  " + phrase_key + ":\n    " + single_line_value + "\n",
        encoding="utf-8",
    )
    (tmp_path / "block-recovery.yml").write_text(
        "wallet:\n  " + block_key + ": |-\n"
        + "    " + first_block_line + "\n"
        + "    " + second_block_line + "\n",
        encoding="utf-8",
    )
    (tmp_path / "empty-recovery.yml").write_text(
        "wallet:\n  " + phrase_key + ":\n  label: example\n",
        encoding="utf-8",
    )
    (tmp_path / "placeholder-recovery.yml").write_text(
        "wallet:\n  " + phrase_key + ":\n    <YOUR_SEED_PHRASE>\n",
        encoding="utf-8",
    )
    (tmp_path / "empty-block-recovery.yml").write_text(
        "wallet:\n  " + phrase_key + ": |-\n  label: example\n",
        encoding="utf-8",
    )
    (tmp_path / "placeholder-block-recovery.yml").write_text(
        "wallet:\n  " + phrase_key + ": >-\n    <YOUR_SEED_PHRASE>\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)
    findings = check_no_secrets.scan()

    assert any(
        "multiline-recovery.yml:2: seed/private-key assignment" in item
        for item in findings
    )
    assert any(
        "block-recovery.yml:2: seed/private-key assignment" in item
        for item in findings
    )
    assert not any("empty-recovery.yml" in item for item in findings)
    assert not any("placeholder-recovery.yml" in item for item in findings)
    assert not any("empty-block-recovery.yml" in item for item in findings)
    assert not any("placeholder-block-recovery.yml" in item for item in findings)



def test_secret_scanner_validates_supported_bip39_and_ignores_japanese_notes_and_public_keys(
    tmp_path, monkeypatch
):
    from bip_utils import (
        Bip39Languages,
        Bip39MnemonicGenerator,
        Bip39MnemonicValidator,
    )

    mnemonic_key = "mne" + "monic"
    key_field = "private" + "_key"
    english_generator = Bip39MnemonicGenerator(Bip39Languages.ENGLISH)
    english_validator = Bip39MnemonicValidator(Bip39Languages.ENGLISH)
    supported_phrase = english_generator.FromEntropy(bytes(16)).ToStr()
    supported_words = supported_phrase.split()
    invalid_checksum_phrase = None
    for entropy_byte in range(1, 16):
        alternative_last_word = english_generator.FromEntropy(
            bytes([entropy_byte]) * 16
        ).ToStr().split()[-1]
        candidate = " ".join(supported_words[:-1] + [alternative_last_word])
        if not english_validator.IsValid(candidate):
            invalid_checksum_phrase = candidate
            break
    assert invalid_checksum_phrase is not None
    japanese_notes = "これは 普通 の 日本語 の 運用 メモ です 今日 の 作業 内容 を 記録 します"
    (tmp_path / "supported-bip39-wallet.yml").write_text(
        "wallet:\n  " + mnemonic_key + ": |\n"
        + "".join("    " + word + " " for word in supported_phrase.split())
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "supported-bip39-wallet-invalid-checksum.yml").write_text(
        "wallet:\n  " + mnemonic_key + ": |\n"
        + "".join("    " + word + " " for word in invalid_checksum_phrase.split())
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "japanese-notes.yml").write_text(
        "wallet:\n  " + mnemonic_key + ": |\n"
        + "".join("    " + word + " " for word in japanese_notes.split())
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "json-private-wallet.yml").write_text(
        "wallet:\n  " + key_field + ": |\n"
        '    {\n'
        '      "kty": "RSA",\n'
        '      "d": "0123456789abcdef0123456789abcdef",\n'
        '      "n": "abcdef0123456789abcdef0123456789"\n'
        '    }\n',
        encoding="utf-8",
    )
    (tmp_path / "public-only-wallet.yml").write_text(
        "wallet:\n  " + key_field + ": |\n"
        '    {"kty":"RSA","n":"abcdef0123456789abcdef0123456789","e":"AQAB"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("supported-bip39-wallet.yml:2: seed/private-key assignment" in item for item in findings)
    assert any("supported-bip39-wallet-invalid-checksum.yml:2: seed/private-key assignment" in item for item in findings)
    assert any("json-private-wallet.yml:2: seed/private-key assignment" in item for item in findings)
    assert not any("japanese-notes.yml" in item for item in findings)
    assert not any("public-only-wallet.yml" in item for item in findings)


def test_secret_scanner_ignores_empty_private_key_objects_and_arrays(tmp_path, monkeypatch):
    private_key_field = "private" + "_key"
    (tmp_path / "empty-object.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n    {}\n",
        encoding="utf-8",
    )
    (tmp_path / "empty-array.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n    []\n",
        encoding="utf-8",
    )
    (tmp_path / "public-object.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n"
        '    {"kty":"EC","crv":"P-256","x":"public-x","y":"public-y"}\n',
        encoding="utf-8",
    )
    (tmp_path / "private-key-note.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n"
        "    This is a note about the wallet.\n"
        "    It is documentation for the operations team.\n",
        encoding="utf-8",
    )
    (tmp_path / "raw-private-key.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n"
        "    0123456789abcdef0123456789abcdef\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert not any("empty-object.yml" in item for item in findings)
    assert not any("empty-array.yml" in item for item in findings)
    assert not any("public-object.yml" in item for item in findings)
    assert not any("private-key-note.yml" in item for item in findings)
    assert any("raw-private-key.yml:2: seed/private-key assignment" in item for item in findings)


def test_secret_scanner_ignores_explicit_mapping_examples_inside_yaml_block_scalars(
    tmp_path, monkeypatch
):
    phrase = (" ".join(["ab" + "andon"] * 11 + ["abo" + "ut"]))
    (tmp_path / "yaml-doc-example.yml").write_text(
        "description: |\n"
        "  Example only; this is documentation, not a mapping:\n"
        "  ? mnemonic\n"
        "  : |\n"
        + "".join("    " + word + "\n" for word in phrase.split()),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert not any("yaml-doc-example.yml" in item for item in findings)


def test_secret_scanner_detects_yaml_explicit_sensitive_keys_with_block_values(
    tmp_path, monkeypatch
):
    mnemonic_key = "mne" + "monic"
    key_field = "private" + "_key"
    phrase = (" ".join(["ab" + "andon"] * 11 + ["abo" + "ut"]))
    (tmp_path / "explicit-mnemonic.yml").write_text(
        "? " + mnemonic_key + "\n"
        ": |\n"
        + "".join("  " + word + "\n" for word in phrase.split()),
        encoding="utf-8",
    )
    (tmp_path / "explicit-private-key.yml").write_text(
        "? " + key_field + "\n"
        ": |\n"
        '  {"kty":"EC","d":"0123456789abcdef0123456789abcdef"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("explicit-mnemonic.yml:1: seed/private-key assignment" in item for item in findings)
    assert any("explicit-private-key.yml:1: seed/private-key assignment" in item for item in findings)


def test_secret_scanner_detects_seed_phrases_in_quoted_source_mappings_and_strings(
    tmp_path, monkeypatch
):
    phrase = "amber birch cobalt dahlia elm fuchsia garden harbor ivory juniper"
    mapping_key = "mnemo" + "nic"
    source_assignment_key = "mne" + "monic"
    source = (
        "recovery = {\n"
        + '    "' + mapping_key + '": "' + phrase + '"\n'
        + "}\n"
        + 'runtime_config = "' + source_assignment_key + "=" + phrase + '"\n'
    )
    (tmp_path / "recovery-config.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("recovery-config.py:2: seed/private-key assignment" in item for item in findings)
    assert any("recovery-config.py:4: seed/private-key assignment" in item for item in findings)


def test_repository_secret_scan_ignores_ordinary_notes_string_construction():
    findings = check_no_secrets.scan()

    # This file contains a constructed ordinary-notes fixture with the word
    # "mnemonic" followed by a variable, not a literal recovery phrase.
    assert not any(
        item.startswith("tests/test_secret_scanner_scope.py:")
        for item in findings
    )


def test_secret_scanner_handles_recursive_yaml_aliases_and_scans_remaining_keys(
    tmp_path, monkeypatch
):
    mnemonic_key = "mne" + "monic"
    phrase = (" ".join(["ab" + "andon"] * 11 + ["abo" + "ut"]))
    content = (
        "loop: &loop {self: *loop}\n"
        "wallet:\n"
        + "  " + mnemonic_key + ": |\n"
        + "".join("    " + word + "\n" for word in phrase.split())
    )
    (tmp_path / "recursive-alias.yml").write_text(content, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "recursive-alias.yml:3: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_handles_recursive_private_key_aliases_and_scans_following_keys(
    tmp_path, monkeypatch
):
    secret = "qZ7mP2xR8vN4kL9sT5yW3uJ6cA0dF1hG"
    private_field = "private" + "_" + "key"
    token_field = "api" + "_" + "token"
    content = (
        "wallet:\n"
        "  " + private_field + ": &key {self: *key}\n"
        "  " + token_field + ": " + secret + "\n"
    )
    (tmp_path / "recursive-private-key.yml").write_text(content, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "recursive-private-key.yml:3: seed/private-key assignment" in item
        for item in findings
    )
    assert not any("recursive-private-key.yml:2:" in item for item in findings)


def test_secret_scanner_handles_recursive_mnemonic_aliases_and_scans_following_keys(
    tmp_path, monkeypatch
):
    secret = "qZ7mP2xR8vN4kL9sT5yW3uJ6cA0dF1hG"
    recovery_field = "mne" + "monic"
    token_field = "api" + "_" + "token"
    content = (
        "wallet:\n"
        "  " + recovery_field + ": &phrase [*phrase]\n"
        "  " + token_field + ": " + secret + "\n"
    )
    (tmp_path / "recursive-mnemonic.yml").write_text(content, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "recursive-mnemonic.yml:3: seed/private-key assignment" in item
        for item in findings
    )
    assert not any("recursive-mnemonic.yml:2:" in item for item in findings)


def test_secret_scanner_detects_seed_literal_in_concatenated_source_assignment(
    tmp_path, monkeypatch
):
    phrase = (" ".join(["ab" + "andon"] * 11 + ["abo" + "ut"]))
    mnemonic_key = "mne" + "monic"
    source = (
        "prefix = ''\n"
        "suffix = ''\n"
        + mnemonic_key + " = prefix + \"" + phrase + "\" + suffix + \"\"\n"
    )
    (tmp_path / "wallet-config.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "wallet-config.py:3: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_ignores_computed_seed_label_without_literal_material(
    tmp_path, monkeypatch
):
    mnemonic_key = "mne" + "monic"
    source = (
        "prefix = ''\n"
        "suffix = ''\n"
        + mnemonic_key + ' = prefix + "note" + suffix + ""\n'
    )
    (tmp_path / "computed-config.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert not any("computed-config.py" in item for item in findings)


def test_secret_scanner_detects_private_key_literal_in_computed_source_assignment(
    tmp_path, monkeypatch
):
    key_name = "private" + "_key"
    key_material = "0123456789abcdef0123456789abcdef"
    source = (
        "prefix = ''\n"
        + key_name + ' = prefix + "' + key_material + '"\n'
    )
    (tmp_path / "computed-wallet.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "computed-wallet.py:2: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_flags_recovery_phrase_split_across_source_variables(
    tmp_path, monkeypatch
):
    first = "abandon abandon abandon abandon abandon abandon"
    second = (" ".join(["ab" + "andon"] * 11 + ["abo" + "ut"]))
    mnemonic_key = "mne" + "monic"
    source = (
        "first = '" + first + "'\n"
        + "second = '" + second + "'\n"
        + mnemonic_key + " = first + second\n"
    )
    (tmp_path / "split-wallet.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "split-wallet.py:3: seed/private-key assignment" in item
        for item in findings
    )




def test_secret_scanner_covers_typescript_dockerfiles_and_extensionless_build_files(
    tmp_path, monkeypatch
):
    credential = "qZ7mP2xR8vN4kL9sT5yW3uJ6cA0dF1hG"
    fixtures = {
        "supabase-index.ts": 'const config = { SUPABASE_SERVICE_ROLE_KEY: "' + credential + '" };',
        "dashboard.tsx": 'const config = { API_TOKEN: "' + credential + '" };',
        "Dockerfile": "ARG API_TOKEN=" + credential,
        "Dockerfile.verify": "ENV ENCRYPTION_KEY=" + credential,
        ".dockerignore": "API_TOKEN=" + credential,
        "main.tf": 'service_role_key = "' + credential + '"',
        "environment.tfvars": 'signing_key = "' + credential + '"',
        "Makefile": "export API_TOKEN=" + credential,
        "Procfile": "web: API_TOKEN=" + credential,
    }
    for name, content in fixtures.items():
        (tmp_path / name).write_text(content + "\n", encoding="utf-8")

    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)
    findings = check_no_secrets.scan()

    for name in fixtures:
        assert any(name in item for item in findings), name


def test_secret_scanner_treats_valid_recursive_yaml_alias_as_text(
    tmp_path, monkeypatch
):
    import yaml

    # This is a real two-line YAML document whose alias points back to its
    # anchored mapping. Confirm that the fixture is valid and self-referential
    # before checking that the line-based secret scanner does not parse or
    # recursively traverse YAML documents.
    alias_text = "loop: &loop\n  self: *loop\n"
    alias_path = tmp_path / "cyclic-alias.yml"
    alias_path.write_text(alias_text, encoding="utf-8")
    assert alias_path.read_text(encoding="utf-8").splitlines() == [
        "loop: &loop",
        "  self: *loop",
    ]

    parsed = yaml.safe_load(alias_text)
    assert parsed["loop"]["self"] is parsed["loop"]

    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)
    assert check_no_secrets.scan() == []

def test_secret_scanner_preserves_repeated_yaml_mnemonic_aliases(
    tmp_path, monkeypatch
):
    import yaml

    repeated_word = "ab" + "andon"
    final_word = "abo" + "ut"
    lines = [
        f"word_a: &word_a {repeated_word}",
        f"word_b: &word_b {final_word}",
        "mnemonic" + ":",
        *["  - *word_a" for _ in range(11)],
        "  - *word_b",
    ]
    document = "\n".join(lines) + "\n"
    parsed = yaml.safe_load(document)
    assert len(parsed["mnemonic"]) == 12
    assert all(item is parsed["mnemonic"][0] for item in parsed["mnemonic"][:11])
    assert parsed["mnemonic"][-1] == final_word

    (tmp_path / "repeated-mnemonic-aliases.yml").write_text(
        document, encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)
    findings = check_no_secrets.scan()
    assert any(
        "repeated-mnemonic-aliases.yml:3: seed/private-key assignment" in item
        for item in findings
    )

def test_secret_scanner_detects_non_bip39_recovery_words_in_yaml_and_dotenv(
    tmp_path, monkeypatch
):
    mnemonic_field = "mne" + "monic"
    words = [
        "amber", "birch", "cobalt", "dahlia", "elm",
        "fuchsia", "garden", "harbor", "ivory", "juniper",
    ]
    phrase = " ".join(words)
    short_phrase = " ".join(words[:6])
    assert not check_no_secrets._is_bip39_wordlist_phrase(phrase)
    assert not check_no_secrets._is_bip39_wordlist_phrase(short_phrase)

    short_lines = ["wallet:", f"  {mnemonic_field}: {short_phrase}"]
    block_lines = ["wallet:", f"  {mnemonic_field}: |"]
    block_lines.extend("    " + word for word in words)
    env_lines = [mnemonic_field + ":"]
    env_lines.extend("  " + word for word in words)

    (tmp_path / "short-proprietary-recovery.yml").write_text(
        "\n".join(short_lines) + "\n", encoding="utf-8"
    )
    (tmp_path / "proprietary-recovery.yml").write_text(
        "\n".join(block_lines) + "\n", encoding="utf-8"
    )
    (tmp_path / "proprietary-recovery.env").write_text(
        "\n".join(env_lines) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("proprietary-recovery.yml:2: seed/private-key assignment" in item for item in findings)
    assert any("short-proprietary-recovery.yml:2: seed/private-key assignment" in item for item in findings)
    assert any("proprietary-recovery.env:1: seed/private-key assignment" in item for item in findings)


def test_secret_scanner_detects_generic_key_component_inside_private_key_mapping(
    tmp_path, monkeypatch
):
    private_field = "private" + "_key"
    key_material = "0123456789abcdef0123456789abcdef"
    (tmp_path / "nested-private-key.yml").write_text(
        "wallet:\n"
        + "  " + private_field + ":\n"
        + "    key: " + key_material + "\n",
        encoding="utf-8",
    )
    (tmp_path / "nested-private-key-value.yml").write_text(
        "wallet:\n"
        + "  " + private_field + ":\n"
        + "    value: " + key_material + "\n",
        encoding="utf-8",
    )
    (tmp_path / "nested-private-key-json-value.yml").write_text(
        "wallet:\n"
        + "  " + private_field + ": |\n"
        + '    {"value": "' + key_material + '"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "nested-private-key.yml:2: seed/private-key assignment" in item
        for item in findings
    )
    assert any(
        "nested-private-key-value.yml:2: seed/private-key assignment" in item
        for item in findings
    )
    assert any(
        "nested-private-key-json-value.yml:2: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_bounds_nested_repeated_mnemonic_alias_expansion(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(check_no_secrets, "MAX_YAML_NODE_VISITS", 64)
    lines = ["word: &word drift"]
    previous = "word"
    for index in range(1, 22):
        anchor = f"layer{index}"
        lines.append(f"{anchor}: &{anchor} [*{previous}, *{previous}]")
        previous = anchor

    mnemonic_field = "mne" + "monic"
    token_field = "api" + "_token"
    token_value = "qZ7mP2xR8vN4kL9sT5yW3uJ6cA0dF1hG"
    lines.extend([
        "wallet:",
        f"  {mnemonic_field}: *{previous}",
        f"  {token_field}: {token_value}",
    ])
    (tmp_path / "alias-expansion-mnemonic.yml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "alias-expansion-mnemonic.yml:24: seed/private-key assignment" in item
        for item in findings
    )
    assert any(
        "alias-expansion-mnemonic.yml:25: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_bounds_nested_repeated_private_key_alias_expansion(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(check_no_secrets, "MAX_YAML_NODE_VISITS", 64)
    lines = ["word: &word ordinary"]
    previous = "word"
    for index in range(1, 22):
        anchor = f"layer{index}"
        lines.append(f"{anchor}: &{anchor} [*{previous}, *{previous}]")
        previous = anchor

    private_field = "private" + "_key"
    token_field = "api" + "_token"
    token_value = "qZ7mP2xR8vN4kL9sT5yW3uJ6cA0dF1hG"
    lines.extend([
        "wallet:",
        f"  {private_field}: *{previous}",
        f"  {token_field}: {token_value}",
    ])
    (tmp_path / "alias-expansion-private-key.yml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "alias-expansion-private-key.yml:24: seed/private-key assignment" in item
        for item in findings
    )
    assert any(
        "alias-expansion-private-key.yml:25: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_detects_single_token_recovery_seed_in_yaml(
    tmp_path, monkeypatch
):
    mnemonic_field = "mne" + "monic"
    token = "0123456789abcdef" * 4
    (tmp_path / "single-token-seed.yml").write_text(
        "\n".join(["wallet:", f"  {mnemonic_field}: {token}"]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "single-token-seed.yml:2: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_detects_private_key_json_arrays_and_nested_arrays(
    tmp_path, monkeypatch
):
    private_field = "private" + "_key"
    token = "0123456789abcdef" * 4
    (tmp_path / "array-private-key.yml").write_text(
        "wallet:\n"
        + "  " + private_field + ": |\n"
        + '    ["' + token + '"]\n',
        encoding="utf-8",
    )
    (tmp_path / "nested-array-private-key.yml").write_text(
        "wallet:\n"
        + "  " + private_field + ": |\n"
        + '    [["' + token + '"]]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "array-private-key.yml:2: seed/private-key assignment" in item
        for item in findings
    )
    assert any(
        "nested-array-private-key.yml:2: seed/private-key assignment" in item
        for item in findings
    )



def test_secret_scanner_distinguishes_public_key_ops_from_private_component_arrays(
    tmp_path, monkeypatch
):
    private_key_field = "private" + "_key"
    private_component = "0123456789abcdef" * 2
    (tmp_path / "public-key-ops.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n"
        + '    {"kty":"EC","crv":"P-256","x":"public-x","y":"public-y",'
        '"key_ops":["verify"]}\n',
        encoding="utf-8",
    )
    (tmp_path / "private-component-array.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n"
        + '    {"kty":"EC","d":["' + private_component + '"]}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert not any("public-key-ops.yml" in item for item in findings)
    assert any(
        "private-component-array.yml:2: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_detects_bare_seed_and_recovery_phrase_keys(
    tmp_path, monkeypatch
):
    seed_field = "se" + "ed"
    recovery_field = "recovery" + "_" + "phrase"
    words = ["amber", "birch", "cobalt", "dahlia", "elm", "fuchsia"]
    phrase = " ".join(words)
    (tmp_path / "bare-seed.yml").write_text(
        "wallet:\n  " + seed_field + ": " + phrase + "\n",
        encoding="utf-8",
    )
    (tmp_path / "recovery-phrase.yml").write_text(
        "wallet:\n  " + recovery_field + ": " + phrase + "\n",
        encoding="utf-8",
    )
    (tmp_path / "bare-seed.env").write_text(
        seed_field + ":\n" + "".join("  " + word + "\n" for word in words),
        encoding="utf-8",
    )
    (tmp_path / "recovery-phrase.env").write_text(
        recovery_field + "=" + phrase + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    for name in ("bare-seed.yml", "recovery-phrase.yml", "bare-seed.env", "recovery-phrase.env"):
        assert any(name in item for item in findings), name


def test_secret_scanner_ignores_instructional_prose_under_mnemonic_key(
    tmp_path, monkeypatch
):
    mnemonic_field = "mne" + "monic"
    instructional_note = "write down your words and store them safely"
    (tmp_path / "mnemonic-instructions.yml").write_text(
        "wallet:\n  " + mnemonic_field + ": " + instructional_note + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert not any("mnemonic-instructions.yml" in item for item in findings)


def test_secret_scanner_ignores_boolean_false_under_private_material_fields(
    tmp_path, monkeypatch
):
    private_key_field = "private" + "_key"
    (tmp_path / "boolean-private-key.yml").write_text(
        "wallet:\n  " + private_key_field + ":\n    d: false\n",
        encoding="utf-8",
    )
    (tmp_path / "boolean-private-key-false-root.yml").write_text(
        "wallet:\n  " + private_key_field + ": false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert not any("boolean-private-key.yml" in item for item in findings)
    assert not any("boolean-private-key-false-root.yml" in item for item in findings)


def test_secret_scanner_fails_closed_on_deep_private_json_and_traversal_limit(
    tmp_path, monkeypatch
):
    private_key_field = "private" + "_key"
    deeply_nested_json = "[" * 1200 + '"leaf"' + "]" * 1200
    (tmp_path / "deep-private-json.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n"
        + "    " + deeply_nested_json + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "deep-private-json.yml:2: seed/private-key assignment" in item
        for item in findings
    )

    limited_root = tmp_path / "limited"
    limited_root.mkdir()
    (limited_root / "limited-private-key.env").write_text(
        private_key_field + ":\n  " + "0123456789abcdef" * 2 + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", limited_root)
    monkeypatch.setattr(check_no_secrets, "MAX_YAML_NODE_VISITS", 0)

    limited_findings = check_no_secrets.scan()

    assert any(
        "limited-private-key.env:1: seed/private-key assignment" in item
        for item in limited_findings
    )


def test_secret_scanner_detects_bare_seed_aliases_in_source_code(
    tmp_path, monkeypatch
):
    seed_field = "se" + "ed"
    recovery_field = "recovery" + "_" + "phrase"
    phrase = "amber birch cobalt dahlia elm fuchsia"
    (tmp_path / "source-recovery.py").write_text(
        seed_field + ' = "' + phrase + '"\n'
        + recovery_field + ' = "' + phrase + '"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("source-recovery.py:1: seed/private-key assignment" in item for item in findings)
    assert any("source-recovery.py:2: seed/private-key assignment" in item for item in findings)


def test_secret_scanner_detects_namespaced_recovery_keys_in_properties(
    tmp_path, monkeypatch
):
    phrase = "amber birch cobalt dahlia elm fuchsia"
    (tmp_path / "wallet.properties").write_text(
        "wallet.mnemonic=" + phrase + "\n"
        + "account.recovery_phrase=" + phrase + "\n",
        encoding="utf-8",
    )
    (tmp_path / "namespaced-source.py").write_text(
        'wallet.mnemonic = "' + phrase + '"\n'
        + 'account.recovery_phrase = "' + phrase + '"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    for name in ("wallet.properties", "namespaced-source.py"):
        assert any(name in item for item in findings), name

def test_secret_scanner_detects_namespaced_recovery_properties(
    tmp_path, monkeypatch
):
    mnemonic_field = "mne" + "monic"
    seed_field = "se" + "ed"
    recovery_field = "recovery" + "_phrase"
    phrase = " ".join(["amber", "birch", "cobalt", "dahlia", "elm", "fuchsia"])
    for filename, field in (
        ("namespaced-mnemonic.properties", mnemonic_field),
        ("namespaced-seed.properties", seed_field),
        ("namespaced-recovery.properties", recovery_field),
    ):
        (tmp_path / filename).write_text(
            "wallet." + field + "=" + phrase + "\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    for filename in (
        "namespaced-mnemonic.properties",
        "namespaced-seed.properties",
        "namespaced-recovery.properties",
    ):
        assert any(filename in item for item in findings), filename


def test_secret_scanner_detects_nested_private_json_material_through_arrays(
    tmp_path, monkeypatch
):
    private_key_field = "private" + "_key"
    nested_component = "0123456789abcdef" * 2
    (tmp_path / "nested-private-json-arrays.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n"
        + '    {"value":[{"chunks":["' + nested_component + '"]}]}\n',
        encoding="utf-8",
    )
    (tmp_path / "nested-public-key-ops.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n"
        + '    {"value":[{"key_ops":["verify"]}]}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "nested-private-json-arrays.yml:2: seed/private-key assignment" in item
        for item in findings
    )
    assert not any("nested-public-key-ops.yml" in item for item in findings)


def test_secret_scanner_ignores_nonsecret_seed_parameters_and_runtime_calls(
    tmp_path, monkeypatch
):
    seed_field = "se" + "ed"
    source = (
        seed_field + ": int = 17\n"
        + "bootstrap_mean_ci(returns, samples=100, " + seed_field + "=17)\n"
        + seed_field + " = bytes.fromhex(args." + seed_field + "_hex.strip())\n"
        + seed_field + " = secrets.token_bytes(32)\n"
        + seed_field + " = 59\n"
        + seed_field + ': str = "atlas-paper-v1"\n'
    )
    (tmp_path / "research-seed-defaults.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert not any("research-seed-defaults.py" in item for item in findings)

def test_secret_scanner_distinguishes_numeric_seed_defaults_from_secret_material(
    tmp_path, monkeypatch
):
    seed_field = "se" + "ed"
    mnemonic_field = "mne" + "monic"
    random_defaults = (
        seed_field + "=17\n"
        + "wallet." + seed_field + "=42\n"
    )
    long_seed = "0123456789abcdef" * 2
    proprietary_phrase = "amber birch cobalt dahlia elm safe"
    instructional_text = "Store your recovery phrase offline and never share it"
    (tmp_path / "random-seed-defaults.env").write_text(
        random_defaults, encoding="utf-8"
    )
    (tmp_path / "literal-seed-material.env").write_text(
        seed_field + "=" + long_seed + "\n", encoding="utf-8"
    )
    (tmp_path / "proprietary-safe-word.env").write_text(
        mnemonic_field + "=" + proprietary_phrase + "\n", encoding="utf-8"
    )
    (tmp_path / "recovery-instructions.env").write_text(
        mnemonic_field + "=" + instructional_text + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert not any("random-seed-defaults.env" in item for item in findings)
    assert any("literal-seed-material.env:1: seed/private-key assignment" in item for item in findings)
    assert any("proprietary-safe-word.env:1: seed/private-key assignment" in item for item in findings)
    assert not any("recovery-instructions.env" in item for item in findings)

def test_secret_scanner_detects_hyphenated_yaml_credentials(
    tmp_path, monkeypatch
):
    private_key = "private" + "-" + "key"
    mnemonic_key = "mne" + "monic"
    recovery_key = "recovery" + "-" + "phrase"
    private_material = "0123456789abcdef" * 2
    phrase = "amber birch cobalt dahlia elm safe"
    content = (
        "wallet-" + private_key + ": " + private_material + "\n"
        + "wallet-" + mnemonic_key + ": " + phrase + "\n"
        + "account.wallet-" + recovery_key + ": " + phrase + "\n"
    )
    (tmp_path / "hyphenated-wallet-credentials.yml").write_text(
        content, encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "hyphenated-wallet-credentials.yml:1: seed/private-key assignment" in item
        for item in findings
    )
    assert any(
        "hyphenated-wallet-credentials.yml:2: seed/private-key assignment" in item
        for item in findings
    )
    assert any(
        "hyphenated-wallet-credentials.yml:3: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_detects_single_and_multiline_triple_quoted_private_keys(
    tmp_path, monkeypatch
):
    private_key_field = "private" + "_key"
    token = "0123456789abcdef" * 2
    source = (
        private_key_field + ' = """' + token + '"""\n'
        + "wallet." + private_key_field + " = '''\n"
        + "    " + token + "\n"
        + "'''\n"
    )
    (tmp_path / "triple-quoted-private-keys.py").write_text(
        source, encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "triple-quoted-private-keys.py:1: seed/private-key assignment" in item
        for item in findings
    )
    assert any(
        "triple-quoted-private-keys.py:2: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_allows_public_jwk_metadata_arrays_in_private_key_array(
    tmp_path, monkeypatch
):
    private_key_field = "private" + "_key"
    metadata_value = "0123456789abcdef" * 2
    private_value = "abcdef0123456789" * 2
    public_jwk_array = (
        '[{"kty":"EC","crv":"P-256","x":"public-x","y":"public-y",'
        '"metadata":["' + metadata_value + '"]}]'
    )
    private_jwk_array = (
        '[{"kty":"EC","d":["' + private_value + '"]}]'
    )
    (tmp_path / "public-jwk-array-metadata.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n"
        + "    " + public_jwk_array + "\n",
        encoding="utf-8",
    )
    (tmp_path / "private-jwk-array-component.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n"
        + "    " + private_jwk_array + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert not any("public-jwk-array-metadata.yml" in item for item in findings)
    assert any(
        "private-jwk-array-component.yml:2: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_detects_prefixed_multiline_triple_quoted_private_keys(
    tmp_path, monkeypatch
):
    private_key_field = "private" + "_key"
    token = "0123456789abcdef" * 2
    source = (
        private_key_field + ' = r"""\n'
        + "    " + token + "\n"
        + '"""\n'
    )
    (tmp_path / "prefixed-triple-quoted-private-key.py").write_text(
        source, encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "prefixed-triple-quoted-private-key.py:1: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_detects_quoted_recovery_phrases_with_js_terminator(
    tmp_path, monkeypatch
):
    phrase = " ".join(["abandon"] * 11 + ["about"])
    source = 'const settings = "' + "mnemonic" + "=" + phrase + '";\n'
    (tmp_path / "quoted-recovery-setting.js").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "quoted-recovery-setting.js:1: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_detects_recovery_phrase_in_js_template_literal_with_terminator(
    tmp_path, monkeypatch
):
    from bip_utils import Bip39Languages, Bip39MnemonicGenerator

    mnemonic_key = "mne" + "monic"
    phrase = Bip39MnemonicGenerator(Bip39Languages.ENGLISH).FromEntropy(
        bytes(16)
    ).ToStr()
    source = "const settings = " + chr(96) + mnemonic_key + "=" + phrase + chr(96) + ";\n"
    (tmp_path / "template-recovery-setting.js").write_text(
        source, encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "template-recovery-setting.js:1: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_detects_nested_private_key_in_js_template_literal(
    tmp_path, monkeypatch
):
    private_key_field = "private" + "_key"
    private_material = "0123456789abcdef" * 2
    payload = '{"' + private_key_field + '": "' + private_material + '"}'
    source = "const settings = " + chr(96) + payload + chr(96) + ";\n"
    (tmp_path / "template-private-key.js").write_text(
        source, encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "template-private-key.js:1: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_detects_split_private_key_literals_in_source_assignments(
    tmp_path, monkeypatch
):
    private_key_field = "private" + "_key"
    first_half = "0123456789abcdef"
    second_half = "fedcba9876543210"
    source = (
        private_key_field + ' = "' + first_half + '" + "' + second_half + '"\n'
    )
    (tmp_path / "split-private-key.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "split-private-key.py:1: seed/private-key assignment" in item
        for item in findings
    )

def test_secret_scanner_keeps_scanning_password_beside_block_marker_in_malformed_yaml(
    tmp_path, monkeypatch
):
    password_key = "pass" + "word"
    password_value = "ABCDEFGHIJKLMNOPQRST"
    malformed_yaml = (
        "broken: [unterminated\n"
        + password_key + ": " + password_value + ", seed: |\n"
    )
    (tmp_path / "malformed-yaml-with-password.yml").write_text(
        malformed_yaml, encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "malformed-yaml-with-password.yml:2: credential-like assignment" in item
        for item in findings
    )

def test_secret_scanner_ignores_private_key_identifier_in_js_template_interpolation(
    tmp_path, monkeypatch
):
    dynamic_identifier = "private" + "KeyMaterialFromEnvironment"
    source = (
        "const label = "
        + chr(96)
        + "private_key=${" + dynamic_identifier + "}"
        + chr(96)
        + ";\n"
    )
    (tmp_path / "template-dynamic-private-key.js").write_text(
        source, encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert not any(
        "template-dynamic-private-key.js:" in item for item in findings
    )



def test_secret_scanner_detects_private_key_literal_inside_js_template_interpolation(
    tmp_path, monkeypatch
):
    key_field = "private" + "_key"
    private_value = "0123456789abcdef0123456789abcdef"
    source = (
        "const label = "
        + chr(96)
        + key_field + "=" + "$" + "{" + chr(34) + private_value + chr(34) + "}"
        + chr(96)
        + ";\n"
    )
    (tmp_path / "template-literal-private-key.js").write_text(
        source, encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "template-literal-private-key.js:1: seed/private-key assignment" in item
        for item in findings
    )

def test_secret_scanner_detects_recovery_phrase_in_multiline_js_template_literal(
    tmp_path, monkeypatch
):
    phrase = "abandon abandon abandon abandon abandon abandon\n"
    phrase += "abandon abandon abandon abandon abandon\nabout"
    source = (
        "const wallet = {\n"
        "  mnemonic: " + chr(96) + "\n"
        + phrase
        + "\n  " + chr(96) + "\n"
        "};\n"
    )
    (tmp_path / "multiline-wallet.ts").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "multiline-wallet.ts:2: seed/private-key assignment" in item
        for item in findings
    )


def test_secret_scanner_ignores_dynamic_mnemonic_in_multiline_template_literal(
    tmp_path, monkeypatch
):
    source = (
        "const wallet = {\n"
        "  mnemonic: " + chr(96) + "\n"
        "    " + "$" + "{runtimeMnemonicFromSecretManager}\n"
        "  " + chr(96) + "\n"
        "};\n"
    )
    (tmp_path / "dynamic-wallet.ts").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert not any("dynamic-wallet.ts:" in item for item in findings)


def test_secret_scanner_keeps_static_phrase_after_dynamic_multiline_template_expression(
    tmp_path, monkeypatch
):
    phrase = "abandon abandon abandon abandon abandon abandon "
    phrase += "abandon abandon abandon abandon abandon about"
    source = (
        "const wallet = {\n"
        "  mnemonic: " + chr(96) + "$" + "{runtimeComment}\n"
        + phrase
        + "\n  " + chr(96) + "\n"
        "};\n"
    )
    (tmp_path / "mixed-wallet.js").write_text(source, encoding="utf-8")
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "mixed-wallet.js:2: seed/private-key assignment" in item
        for item in findings
    )



def test_secret_scanner_detects_embedded_recovery_phrase_in_quoted_properties_value(
    tmp_path, monkeypatch
):
    phrase = "abandon abandon abandon abandon abandon abandon "
    phrase += "abandon abandon abandon abandon abandon about"
    (tmp_path / "wallet.properties").write_text(
        'wallet_config="mnemonic: ' + phrase + '"\\n',
        encoding="utf-8",
    )
    (tmp_path / "wallet-placeholder.properties").write_text(
        'wallet_config="mnemonic: <YOUR_RECOVERY_PHRASE>"\\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "wallet.properties:1: seed/private-key assignment" in item
        for item in findings
    )
    assert not any("wallet-placeholder.properties:" in item for item in findings)


def test_secret_scanner_completes_plain_properties_file_without_recovery_keys(
    tmp_path, monkeypatch
):
    (tmp_path / "ordinary.properties").write_text(
        "service.name=atlas\\nfeature.enabled=true\\n", encoding="utf-8"
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    assert check_no_secrets.scan() == []

def test_secret_scanner_ignores_secret_manager_placeholder_with_shell_continuation(
    tmp_path, monkeypatch
):
    (tmp_path / "deployment.md").write_text(
        "SECRET_MODEL_SIGNING_PRIVATE_REF=atlas-model-signing-private-key:<v> \\\n"
        "  BACKUP_RECOVERY_URL=... \\\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    assert check_no_secrets.scan() == []

