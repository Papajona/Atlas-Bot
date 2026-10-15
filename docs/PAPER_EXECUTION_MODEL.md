# Platform paper-execution cost model

## Scope

The platform paper executor uses a transparent deterministic cost estimate. It does not submit broker orders and does not change the live execution path. Customer paper execution remains blocked until the customer-isolated simulation ledger is implemented and tested.

## Calculation

When the caller supplies a verified market context, the estimated adverse slippage is:

`base profile slippage + half the supplied spread + estimated market impact`

Market impact is a square-root participation heuristic:

`coefficient × per-bar return volatility × sqrt(order quantity / observed bar volume) × 10,000`

Impact is capped by `PAPER_MAX_MARKET_IMPACT_BPS`. The coefficient defaults to `0.5` and the cap defaults to `250` bps. These are safety-oriented simulation parameters, **not venue-calibrated facts**. They must be stress-tested and calibrated using execution evidence before paper returns can be considered live-performance estimates.

## Data requirements and fallbacks

The context builder accepts volume as base-asset traded quantity only when the data provenance identifies a crypto source as `ccxt_ohlcv`. It does not treat OANDA candle counts or Yahoo Finance volume as exchange base-asset quantities.

When trustworthy bar volume or volatility is unavailable, the model deliberately falls back to the configured profile slippage and reports `BASELINE_ONLY_MISSING_VERIFIED_IMPACT_INPUTS`. That result must not be described as market-impact-aware. Where a valid spread is supplied, half-spread is added separately.

Every modeled fill reports `calibration_status=UNVALIDATED_HEURISTIC`. The selected cost inputs are recorded in the trade reason and audit event. Unsupported asset classes without an explicit fee/slippage profile are rejected instead of silently inheriting crypto costs.

## Known limitations

- Orders still fill fully and immediately in this platform-only simulation. Partial fills, queue priority, asynchronous latency, delayed/canceled orders, and venue-calibrated rejection scenarios are not yet simulated.
- The volume/volatility impact equation is an approximation, not a fitted execution model. It is not proof of realized slippage.
- Context-free paper requests can only use baseline slippage; this is reported explicitly.
- No authoritative point-in-time delisted-asset history or venue-specific financing evidence is manufactured by this cost model.
- Customer paper execution remains disabled until a properly isolated simulation ledger exists.

## Verification

Unit tests cover monotonic impact versus order size and volatility, bounded impact, adverse-side pricing, trusted versus untrusted volume provenance, missing-context diagnostics, and invalid numeric inputs. The full CI suite must pass on the exact candidate commit before merge.
