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
    words = [
        "amber", "birch", "cobalt", "dahlia", "elm",
        "fuchsia", "garden", "harbor", "ivory", "juniper",
    ]
    phrase = " ".join(words)
    assert not check_no_secrets._is_bip39_wordlist_phrase(phrase)

    (tmp_path / "proprietary-recovery.yml").write_text(
        "wallet:\\n  mnemonic: |\\n"
        + "".join("    " + word + "\\n" for word in words),
        encoding="utf-8",
    )
    (tmp_path / "proprietary-recovery.env").write_text(
        "mnemonic:\\n" + "".join("  " + word + "\\n" for word in words),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any("proprietary-recovery.yml:2: seed/private-key assignment" in item for item in findings)
    assert any("proprietary-recovery.env:1: seed/private-key assignment" in item for item in findings)


def test_secret_scanner_detects_generic_key_component_inside_private_key_mapping(
    tmp_path, monkeypatch
):
    private_field = "private" + "_key"
    key_material = "0123456789abcdef0123456789abcdef"
    (tmp_path / "nested-private-key.yml").write_text(
        "wallet:\\n"
        + "  " + private_field + ":\\n"
        + "    key: " + key_material + "\\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert any(
        "nested-private-key.yml:2: seed/private-key assignment" in item
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
    lines.extend([
        "wallet:",
        f"  mnemonic: *{previous}",
        "  api_token: qZ7mP2xR8vN4kL9sT5yW3uJ6cA0dF1hG",
    ])
    (tmp_path / "alias-expansion-mnemonic.yml").write_text(
        "\\n".join(lines) + "\\n", encoding="utf-8"
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
    lines.extend([
        "wallet:",
        f"  private_key: *{previous}",
        "  api_token: qZ7mP2xR8vN4kL9sT5yW3uJ6cA0dF1hG",
    ])
    (tmp_path / "alias-expansion-private-key.yml").write_text(
        "\\n".join(lines) + "\\n", encoding="utf-8"
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

