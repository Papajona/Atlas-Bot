# Unified Custody and Binance Trading Allocation

## Design boundary
Atlas remains the authoritative customer-liability ledger.

### Address ownership boundary
The repository contains `TWFuigmmGbb5gsTS4KUtY5v2FmA1rJ3yC5`, historically labeled as a treasury/collection address. Repository code cannot establish who controls a blockchain address. If the approved Binance account confirms this exact address as a Binance deposit/collection address, production configuration must set `USDT_TRON_TREASURY_ADDRESS_ROLE=binance_deposit`.

In `binance_deposit` mode Atlas does not sweep to that address and does not count its on-chain balance as separate Atlas TRON custody when a Binance balance observation is also counted. Ownership must be evidenced by an approved Binance account record/export or controlled deposit workflow; the address string alone is not proof. Binance is an external trading/allocation location, not the accounting source of truth.

Supported locations include CUSTODY:TRON and EXCHANGE:BINANCE:CUSTOMER:<customer_id>.

Customer balances remain the sum of the Atlas ledger buckets. External custody/exchange balances are independently observed and reconciled against those liabilities.

## Transfer state machine
Custody-location transfers use REQUESTED -> SUBMITTED -> CONFIRMED, with failure/uncertainty branches through FAILED, UNKNOWN, or RECONCILIATION_REQUIRED.

An idempotency key is mandatory. Replays with different amount/customer/source/destination are rejected.

A transfer is not counted as an asset merely because it is requested or submitted. Destination assets count only after an independent fresh observation is recorded. This prevents double-counting and avoids assuming an unknown transfer succeeded.

## Binance observations
The observation table records the latest externally observed balance for a named location. The observer is intentionally separate from the model:
- no Binance credentials are stored in this layer;
- this layer does not call Binance;
- an observer must obtain the balance from the approved Binance account and persist a provider reference;
- stale observations are excluded;
- when Binance custody reconciliation is enabled and no fresh observation exists, the solvency result is UNKNOWN, not zero or OK.

BINANCE_CUSTODY_RECONCILIATION_ENABLED remains disabled by default until a real observer has been deployed and acceptance-tested.

## Solvency gate
When Binance reconciliation is enabled, controlled assets are fresh TRON custody plus fresh Binance observations.
Customer liabilities are available + trading reserved + withdrawal reserved.
A shortfall or unknown exchange observation fails closed by setting the application kill switch and disabling live execution.

## What this change does not prove
This change does not prove that money can currently move from TRON to Binance. It also does not prove that the configured address belongs to Binance; that requires external Binance account-control evidence. It creates the durable state and reconciliation boundary required to implement and test that transfer safely.

It also does not enable customer-live trading, Binance withdrawals, or any signer.

The next acceptance layer must use dedicated Binance Testnet credentials to prove authentication, balance observation, order execution, protection, flattening, and reconciliation. Real custody funding requires a separate production-controlled transfer drill and cannot be inferred from Binance Spot Testnet.