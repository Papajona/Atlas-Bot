"""Exact-decimal parsing for TRON TRC-20 token balance reconciliation."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def parse_tron_usdt_balance(data: Any, contract_address: str) -> Decimal:
    """Return the exact USDT token balance; malformed provider evidence fails closed.

    The configured Tron USDT contract is expected to use six decimals. A missing token
    row is a zero balance, but malformed rows, duplicate matches, absent decimals, a
    different decimal scale, and invalid/negative raw balances are not zero balances.
    """
    contract = str(contract_address or "").strip().lower()
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
        if str(info.get("address") or "").strip().lower() == contract:
            matches.append(item)

    if not matches:
        return Decimal("0")
    if len(matches) != 1:
        raise ValueError("TRON provider returned duplicate records for the configured USDT contract")

    token = matches[0]
    info = token["token_info"]
    try:
        decimals = int(info["decimals"])
        raw_balance = Decimal(str(token["balance"]))
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError) as exc:
        raise ValueError("TRON provider returned missing or invalid USDT balance/decimal metadata") from exc
    if decimals != 6:
        raise ValueError("TRON USDT contract returned unexpected decimals; expected exactly 6")
    if not raw_balance.is_finite() or raw_balance < 0:
        raise ValueError("TRON provider returned non-finite or negative USDT balance")
    return raw_balance / (Decimal(10) ** decimals)
