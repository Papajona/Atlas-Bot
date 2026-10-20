# Binance Spot Testnet + Custody/Withdrawal Acceptance Harness

This package is intentionally split into two independent drills.

## A. Binance Spot Testnet

`scripts/binance_spot_testnet_acceptance.py` is an opt-in external integration test. It never targets production.

Safety gates:
- `ATLAS_BINANCE_TESTNET=true` is mandatory.
- `ATLAS_TESTNET_BINANCE_API_KEY` and `ATLAS_TESTNET_BINANCE_API_SECRET` are required.
- Atlas customer Binance broker construction is reused with `sandbox=True`.
- The resulting CCXT endpoint must contain `testnet.binance.vision`.
- Test account withdrawals and universal transfer are disabled in the customer mapping.
- A small Spot market BUY must reach filled/closed state.
- Binance exchangeInfo must advertise `STOP_LOSS` for the selected symbol before a protective order is attempted.
- The protective order is a Spot `STOP_LOSS` sell with an explicit `stopPrice`; it is not a derivatives `reduceOnly` order.
- Emergency handling cancels the protective order and sells remaining base-asset balance.
- Final free base-asset balance must be effectively zero.

The script does not claim custody funding. Binance documents that Spot Test Network balances are virtual and cannot be transferred in or out.

Run only with a dedicated Binance Spot Testnet account:

```bash
ATLAS_BINANCE_TESTNET=true \\
ATLAS_TESTNET_BINANCE_API_KEY='...' \\
ATLAS_TESTNET_BINANCE_API_SECRET='...' \\
ATLAS_TESTNET_SYMBOL='BTC/USDT' \\
python scripts/binance_spot_testnet_acceptance.py
```

Never place production credentials in these variables.

## B. Custody/withdrawal simulation

`tests/test_custody_withdrawal_acceptance.py` runs Atlas's real customer-funds services against an isolated in-memory SQLite database.

It proves:
1. confirmed custody deposit credits the authoritative ledger;
2. duplicate provider delivery is idempotent;
3. withdrawal funds are reserved before payout;
4. failed payout releases the exact reservation;
5. completed payout settles the reserved amount;
6. customer balances remain consistent;
7. ledger entries exist for deposit/reserve/release/settlement;
8. journal idempotency keys remain unique.

No blockchain, exchange withdrawal, signer, private key, or real customer money is contacted.

## Evidence boundary

A passing CI result for the simulation harness is not proof of custody connectivity.

A passing Binance Spot Testnet run is not proof of real custody-to-Binance funding, production customer credentials, production exchange execution, real withdrawals, or regulatory readiness.

## Binance protocol facts used by the harness

Binance Spot Test Network documentation states that users receive virtual balances, those assets cannot be transferred in or out of the test network, and the Spot Test Network uses Spot `/api` endpoints.

Binance Spot order documentation defines `STOP_LOSS` with `quantity` and `stopPrice` (or trailing delta), and describes it as a market order executed when the trigger condition is met.

Therefore the acceptance harness deliberately does not use a derivatives `reduceOnly` flag for the Spot flatten step.
