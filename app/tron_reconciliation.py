"""Exact-decimal parsing for TRON TRC-20 token balance reconciliation."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any


_RAW_BALANCE_RE = re.compile(r"^[0-9]+$")
_MAX_UINT256 = (1 << 256) - 1


def parse_tron_usdt_balance(data: Any, contract_address: str) -> Decimal:
    """Return exact USDT balance or reject missing/malformed provider evidence.

    TRON contract addresses are Base58Check strings and must be compared exactly;
    they are not case-insensitive identifiers. A valid zero must be explicit in a
    matching token record. An absent contract record is missing provider evidence,
    not proof of a zero balance. The configured USDT contract must report integer
    metadata `decimals == 6`, and the raw on-chain balance must be an integer value.
    """
    contract = str(contract_address or "").strip()
    if not contract:
        raise ValueError("Configured USDT contract address is empty")
    if not isinstance(data, list):
        raise ValueError("TRON provider balance response has no data list")

    matches: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("TRON provider returned a malformed token record")
        info = item.get("token_info")
        if not isinstance(info, dict):
            raise ValueError("TRON provider token record has invalid token_info")
        address = info.get("address")
        if not isinstance(address, str) or not address or address != address.strip():
            raise ValueError("TRON provider token record is missing a valid canonical contract address")
        if address == contract:
            matches.append(item)

    if not matches:
        raise ValueError("TRON provider response is missing the configured USDT contract balance metadata")
    if len(matches) != 1:
        raise ValueError("TRON provider returned duplicate records for the configured USDT contract")

    token = matches[0]
    info = token["token_info"]
    decimals_value = info.get("decimals")
    if type(decimals_value) is not int or decimals_value != 6:
        raise ValueError("TRON USDT contract must report integer decimals metadata equal to exactly 6")

    raw_value = token.get("balance")
    if isinstance(raw_value, str):
        raw_text = raw_value.strip()
        if not _RAW_BALANCE_RE.fullmatch(raw_text):
            raise ValueError("TRON provider returned missing or invalid integer USDT raw-balance metadata")
    elif type(raw_value) is int:
        if raw_value < 0:
            raise ValueError("TRON provider returned a negative USDT raw balance")
        raw_text = str(raw_value)
    else:
        # Reject floats (which may already have lost integer precision), booleans,
        # fractional amounts, and unexpected provider JSON types.
        raise ValueError("TRON provider returned missing or invalid integer USDT raw-balance metadata")

    # TRC-20 balances are uint256 values; a longer/out-of-range provider value is malformed.
    if len(raw_text) > 78 or int(raw_text) > _MAX_UINT256:
        raise ValueError("TRON provider USDT raw balance exceeds the uint256 token balance range")

    # Construct Decimal directly from its digit tuple with exponent -6. Dividing
    # by 10**6 uses the active Decimal context (28 significant digits by default)
    # and can round uint256-sized balances. Tuple construction is exact regardless
    # of token amount magnitude or the current Decimal context precision.
    return Decimal((0, tuple(int(digit) for digit in raw_text), -decimals_value))
