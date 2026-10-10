"""Request-id allocation for customer withdrawals.

The base id is a deterministic hash of customer/destination/amount/tag/provider so that an identical
request made while an earlier one is still active stays idempotent. After every earlier attempt has
reached a terminal state, a later identical request must get a *new* id: request_id is unique in the
database and the ledger reserve/settle/release keys are derived from it, so reusing it would collide
with the old row and journal.
"""
from __future__ import annotations

from collections.abc import Iterable


def next_customer_withdrawal_request_id(base_request_id: str, prior_request_ids: Iterable[str]) -> str:
    prior = set(prior_request_ids)
    if not prior:
        return base_request_id
    n = len(prior)
    candidate = f"{base_request_id}-r{n}"
    while candidate in prior:
        n += 1
        candidate = f"{base_request_id}-r{n}"
    return candidate
