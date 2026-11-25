from scripts import check_no_secrets


def test_secret_scanner_public_jwk_field_does_not_hide_private_sibling(
    tmp_path, monkeypatch
):
    private_key_field = "private" + "_key"
    public_coordinate = "0123456789abcdef" * 2
    private_sibling = "fedcba9876543210" * 2

    public_only_jwk = (
        '[{"kty":"EC","crv":"P-256","x":"' + public_coordinate
        + '","y":"' + public_coordinate + '"}]'
    )
    jwk_with_private_sibling = (
        '[{"kty":"EC","crv":"P-256","x":"' + public_coordinate
        + '","y":"' + public_coordinate
        + '","provider_context":"external HSM",'
        + '"private_blob":"' + private_sibling + '"}]'
    )

    (tmp_path / "public-only-jwk-array.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n"
        + "    " + public_only_jwk + "\n",
        encoding="utf-8",
    )
    (tmp_path / "jwk-private-sibling.yml").write_text(
        "wallet:\n  " + private_key_field + ": |\n"
        + "    " + jwk_with_private_sibling + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_secrets, "ROOT", tmp_path)

    findings = check_no_secrets.scan()

    assert not any("public-only-jwk-array.yml" in item for item in findings)
    assert any(
        "jwk-private-sibling.yml:2: seed/private-key assignment" in item
        for item in findings
    )
