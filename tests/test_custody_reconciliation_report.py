"""Regression for clearing a stale custody address-role alert after verification."""
import asyncio

from app import custody_reconciliation


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _EmptyDatabase:
    async def execute(self, *_args, **_kwargs):
        return _EmptyResult()


def test_valid_treasury_role_resolves_prior_address_role_incident(monkeypatch):
    monkeypatch.setattr(custody_reconciliation.settings, "usdt_tron_treasury_address_role", "atlas_controlled")
    monkeypatch.setattr(custody_reconciliation.settings, "usdt_tron_treasury_address", "T-verified-address")
    monkeypatch.setattr(custody_reconciliation.settings, "usdt_trongrid_api_key", "")

    resolved = []

    async def resolve(_db, *, key):
        resolved.append(key)

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(custody_reconciliation, "_resolve_incident", resolve)
    monkeypatch.setattr(custody_reconciliation, "_halt_live_for_unverified_custody", no_op)
    monkeypatch.setattr(custody_reconciliation, "_upsert_incident", no_op)

    report = asyncio.run(custody_reconciliation.reconcile_usdt_custody(_EmptyDatabase()))

    assert report["status"] == "ERROR"  # provider configuration is still absent
    assert resolved == ["CUSTODY_RECONCILIATION:USDT:TRON:ADDRESS_ROLE"]
