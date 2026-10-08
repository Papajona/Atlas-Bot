# Atlas Customer Funds Architecture — 3.10.16

## Custody model

Atlas uses a **central TRON treasury/collection address** only when that address is demonstrably Atlas-controlled. If the configured address is actually a Binance deposit/collection address, it is external custody and must not be treated as Atlas treasury custody. for controlled settlement and a **unique virtual TRC-20 deposit address per customer** for automatic attribution. The treasury address must never be used as a shared auto-credit address. TRC-20 transfer history contains sender, receiver, transaction and amount, but no native Atlas customer identifier; amount-only matching is therefore unsafe.

Configured address:

`TWFuigmmGbb5gsTS4KUtY5v2FmA1rJ3yC5`

**Ownership is not established by this repository.** If Binance confirms this exact address as its deposit/collection address, configure `USDT_TRON_TREASURY_ADDRESS_ROLE=binance_deposit`; Atlas then excludes that address from Atlas-controlled TRON assets and relies on a fresh Binance custody observation instead.

USDT TRC-20 contract:

`TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`

## Customer balance lifecycle

```
TRON confirmed transfer
        |
        v
virtual customer deposit address
        |
        v
idempotent FundingTransaction
        |
        v
CustomerLedgerAccount.available
        |
        +----> customer dashboard
        |
        +----> trading reservation
        |
        +----> withdrawal reservation
```

The ledger is authoritative for the customer-facing balance. The legacy `Wallet.available_balance`/`locked_balance` fields are synchronized compatibility projections.

## Trading isolation

When `customer_cash_only_trading=true`, a new exposure must reserve the customer's own available USDT before an order is allowed. The reservation is stored in `CustomerLedgerAccount.trading_reserved` and mirrored to `TradingAccount.reserved_margin`. Closing exposure releases the reserved capital.

This prevents two common failures:

1. Customer A cannot trade using Customer B's funds.
2. Customer A cannot submit multiple new exposures that each pass an isolated balance check against the same unreserved cash.

All checks and balance mutations occur server-side under database row locks. External actions use idempotency keys.

## Shared-address mode

`usdt_tron_shared_deposit_mode` defaults to `false`. If enabled, the customer UI shows the treasury address only as a **manual-review deposit route**. Atlas does not auto-credit based on matching amount, timestamp, or a guessed sender.

## Reconciliation

The admin endpoint `/api/admin/custody/reconciliation` reports customer ledger liabilities and the configured treasury on-chain USDT balance. Full production solvency reconciliation must also include all active virtual deposit addresses, pending deposits, pending sweeps, withdrawal reserves, and any exchange/trading settlement balances.

## Sweep policy

A future sweep worker should move confirmed USDT from virtual deposit addresses to the treasury address only when `USDT_TRON_TREASURY_ADDRESS_ROLE=atlas_controlled`. A Binance deposit address must never be used as an Atlas sweep destination using an HSM/MPC or other controlled signer. The sweep must be separately idempotent and reconciled; the customer ledger credit must never depend on the sweep succeeding.
