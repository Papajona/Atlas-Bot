from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import settings

USDT_SCALE = Decimal("1000000")


class SweepError(Exception):
    pass


@dataclass(frozen=True)
class SweepIntent:
    wallet_id: int
    source_address: str
    treasury_address: str
    amount_usdt: Decimal
    raw_amount: int
    idempotency_key: str
    network: str = "TRON"
    contract: str = ""
    status: str = "READY_FOR_SIGNER"


def usdt_to_raw(amount: Decimal | str | float) -> int:
    try:
        value = Decimal(str(amount))
        if not value.is_finite():
            raise SweepError("sweep amount must be finite")
        if value <= 0:
            raise SweepError("sweep amount must be positive")
        raw = value * USDT_SCALE
        if raw != raw.to_integral_value():
            raise SweepError("USDT amount must have at most 6 decimal places")
        return int(raw)
    except SweepError:
        raise
    except (InvalidOperation, ValueError, TypeError, ArithmeticError) as exc:
        raise SweepError("sweep amount is not a valid finite number") from exc


def build_sweep_intent(*, wallet_id: int, source_address: str, amount_usdt: Decimal | str | float,
                       treasury_address: str | None = None, idempotency_key: str | None = None,
                       nonce: str | int | None = None) -> SweepIntent:
    """Build a sweep intent. Callers repeating a sweep should pass a retry-stable request ID
    or a serialized per-wallet sequence as nonce.
    """
    treasury = treasury_address or settings.usdt_tron_treasury_address
    if not source_address or not treasury:
        raise SweepError("source and treasury addresses are required")
    if source_address == treasury:
        raise SweepError("source address cannot equal treasury address")
    raw = usdt_to_raw(amount_usdt)
    nonce_part = f":{nonce}" if nonce not in (None, "") else ""
    key = idempotency_key or hashlib.sha256(
        f"tron-sweep:{wallet_id}:{source_address}:{treasury}:{raw}:{settings.usdt_tron_usdt_contract}{nonce_part}".encode()
    ).hexdigest()
    return SweepIntent(wallet_id=wallet_id, source_address=source_address, treasury_address=treasury,
                       amount_usdt=Decimal(raw) / USDT_SCALE, raw_amount=raw,
                       idempotency_key=key, contract=settings.usdt_tron_usdt_contract)


def serialize_sweep_intent(intent: SweepIntent) -> dict[str, Any]:
    return {
        "wallet_id": intent.wallet_id,
        "source_address": intent.source_address,
        "treasury_address": intent.treasury_address,
        "amount_usdt": format(intent.amount_usdt, "f"),
        "raw_amount": str(intent.raw_amount),
        "network": intent.network,
        "contract": intent.contract,
        "idempotency_key": intent.idempotency_key,
        "status": intent.status,
    }

TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a9df523b3ef"  # keccak256("Transfer(address,address,uint256)")


def _hex_topic_to_tron_address(value: str) -> str | None:
    raw = (value or "").lower().removeprefix("0x")
    if len(raw) != 64:
        return None
    try:
        payload = bytes.fromhex(raw)
    except ValueError:
        return None
    if any(payload[:12]):
        return None
    try:
        from bip_utils import Base58Encoder
    except ImportError as exc:
        raise RuntimeError("bip-utils is required for TRON receipt address decoding") from exc
    # ABI address topics are 32-byte left-padded values; TRON address payload is 0x41 + 20 bytes.
    return Base58Encoder.CheckEncode(b"\x41" + payload[-20:])



def _address_to_hex20(value: str) -> str | None:
    """Normalize TRON Base58/hex contract addresses to the same 20-byte lowercase hex form."""
    raw = str(value or "").strip()
    if not raw:
        return None
    hexpart = raw[2:] if raw.lower().startswith("0x") else raw
    if len(hexpart) in (40, 42) and all(c in "0123456789abcdefABCDEF" for c in hexpart):
        hexpart = hexpart.lower()
        if len(hexpart) == 42:
            if not hexpart.startswith("41"):
                return None
            hexpart = hexpart[2:]
        return hexpart
    if raw.startswith("T") and len(raw) == 34:
        try:
            from bip_utils import Base58Decoder
            decoded = Base58Decoder.CheckDecode(raw)
        except Exception:
            return None
        return decoded[1:].hex() if len(decoded) == 21 and decoded[0] == 0x41 else None
    return None

def extract_trc20_transfers(receipt: dict[str, Any], contract: str, source: str, destination: str) -> list[int]:
    """Return every matching Transfer amount emitted by the configured token contract."""
    found: list[int] = []
    wanted_contract = _address_to_hex20(contract)
    if wanted_contract is None:
        return found
    for log in receipt.get("log", []) or receipt.get("logs", []) or []:
        if not isinstance(log, dict):
            continue
        topics = [str(x) for x in (log.get("topics") or [])]
        if len(topics) < 3 or topics[0].lower().removeprefix("0x") != TRANSFER_TOPIC:
            continue
        if _address_to_hex20(str(log.get("address") or "")) != wanted_contract:
            continue
        from_addr = _hex_topic_to_tron_address(topics[1])
        to_addr = _hex_topic_to_tron_address(topics[2])
        data = str(log.get("data") or "").removeprefix("0x")
        if (from_addr is None or to_addr is None or from_addr != source
                or to_addr != destination or len(data) != 64
                or any(ch not in "0123456789abcdefABCDEF" for ch in data)):
            continue
        try:
            found.append(int(data, 16))
        except ValueError:
            continue
    return found


def extract_trc20_transfer(receipt: dict[str, Any], contract: str, source: str, destination: str) -> int | None:
    """Return a single matching Transfer amount, or None if absent/ambiguous."""
    found = extract_trc20_transfers(receipt, contract, source, destination)
    return found[0] if len(found) == 1 else None


def classify_solidified_sweep(*, tx_body: dict[str, Any], receipt: dict[str, Any],
                              transaction_id: str, source: str, treasury: str, contract: str,
                              expected_raw_amount: int) -> tuple[str, dict[str, Any]]:
    """Classify a sweep only from solidified transaction body + receipt evidence."""
    if not tx_body or not receipt:
        return "UNKNOWN", {"reason": "solidified transaction/receipt not yet available"}
    if str(tx_body.get("txID") or tx_body.get("txid") or "").lower() != transaction_id.lower():
        return "REVIEW", {"reason": "transaction id mismatch"}
    ret_list = tx_body.get("ret")
    ret = ret_list[0] if isinstance(ret_list, list) and ret_list and isinstance(ret_list[0], dict) else {}
    contract_ret = str(ret.get("contractRet") or "").upper()
    if not contract_ret:
        return "REVIEW", {"reason": "contractRet missing from solidified transaction"}
    if contract_ret != "SUCCESS":
        return "FAILED", {"reason": f"contractRet={contract_ret}"}
    if str(receipt.get("id") or "").lower() != transaction_id.lower():
        return "REVIEW", {"reason": "receipt id mismatch or missing"}
    if str(receipt.get("result") or "").upper() == "FAILED":
        return "FAILED", {"reason": "solidified receipt result=FAILED"}
    receipt_result = str((receipt.get("receipt") or {}).get("result") or "").upper()
    if receipt_result != "SUCCESS":
        return "REVIEW", {"reason": f"solidified receipt result={receipt_result or 'missing'}"}
    matches = extract_trc20_transfers(receipt, contract, source, treasury)
    if not matches:
        return "REVIEW", {"reason": "matching TRC-20 Transfer event not found"}
    if len(matches) > 1:
        return "REVIEW", {"reason": "multiple matching TRC-20 Transfer events", "observed_raw_all": matches,
                           "expected_raw": expected_raw_amount}
    observed = matches[0]
    if observed != expected_raw_amount:
        return "REVIEW", {"reason": "transfer amount mismatch", "observed_raw": observed,
                           "expected_raw": expected_raw_amount}
    return "SETTLED", {"observed_raw": observed}
