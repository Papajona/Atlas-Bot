"""Sweep classification against the shapes TRON nodes actually return.

Fixture values come from a public TRC-20 USDT Transfer log documented in tronprotocol/java-tron issue #4103
(contract TR7NHqje..., hex log address WITHOUT the 41 prefix, 32-byte left-padded topics)."""
import pytest

pytest.importorskip("bip_utils")
from bip_utils import Base58Encoder

from app.tron_sweep import TRANSFER_TOPIC, _address_to_hex20, classify_solidified_sweep, extract_trc20_transfer

USDT_BASE58 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_LOG_ADDRESS = "a614f803b6fd780986a42c78ec9c7f77e6ded13c"          # as returned by gettransactioninfobyid
FROM_HEX = "85e7df5a33c88707f0b5c76764f18e7b13798969"
TO_HEX = "d95fd620d15e3a96a67a8419c0c95c8c966187c0"
RAW_AMOUNT = 0x16d4ba0                                                  # 23,874,464 = 23.874464 USDT
FROM_B58 = Base58Encoder.CheckEncode(bytes.fromhex("41" + FROM_HEX))
TO_B58 = Base58Encoder.CheckEncode(bytes.fromhex("41" + TO_HEX))
TXID = "08563c6775ef8861367e52b5bb17bf0c47207565f24351821d0a058c3fe1718c"


def _receipt(topic0=TRANSFER_TOPIC, address=USDT_LOG_ADDRESS, data=None):
    return {"id": TXID, "receipt": {"result": "SUCCESS"}, "log": [{
        "address": address,
        "topics": [topic0, "0" * 24 + FROM_HEX, "0" * 24 + TO_HEX],
        "data": data or format(RAW_AMOUNT, "064x")}]}


def _classify(receipt, contract=USDT_BASE58, expected=RAW_AMOUNT):
    return classify_solidified_sweep(
        tx_body={"txID": TXID, "ret": [{"contractRet": "SUCCESS"}]}, receipt=receipt, transaction_id=TXID,
        source=FROM_B58, treasury=TO_B58, contract=contract, expected_raw_amount=expected)


def test_real_shaped_transfer_settles_with_base58_contract_as_stored_by_atlas():
    status, evidence = _classify(_receipt())
    assert status == "SETTLED" and evidence["observed_raw"] == RAW_AMOUNT


@pytest.mark.parametrize("contract", [USDT_BASE58, "41" + USDT_LOG_ADDRESS, USDT_LOG_ADDRESS, "0x" + USDT_LOG_ADDRESS])
def test_contract_encodings_are_equivalent(contract):
    assert _address_to_hex20(contract) == USDT_LOG_ADDRESS
    assert _classify(_receipt(), contract=contract)[0] == "SETTLED"


def test_wrong_contract_is_not_settled():
    other = _receipt(address="b" * 40)
    assert _classify(other)[0] == "REVIEW"


def test_wrong_amount_is_not_settled():
    status, evidence = _classify(_receipt(), expected=RAW_AMOUNT + 1)
    assert status == "REVIEW" and evidence["reason"] == "transfer amount mismatch"


def test_garbage_addresses_do_not_normalise():
    for bad in ["", "T123", "zz" * 20, "42" + "00" * 20, None]:
        assert _address_to_hex20(bad) is None


def test_transfer_topic_matches_the_canonical_erc20_transfer_topic():
    # Canonical Keccak-256 topic for Transfer(address,address,uint256).
    # Keep the protocol vector explicit so a third-party hash backend cannot
    # silently redefine the chain-level event identifier.
    assert TRANSFER_TOPIC == "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a9df523b3ef"
    assert extract_trc20_transfer(_receipt(), USDT_BASE58, FROM_B58, TO_B58) == RAW_AMOUNT
