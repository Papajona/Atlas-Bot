# AtlasRisk synthetic simulation gate — 15 October 2026

This suite uses deterministic, locally generated scenarios. It does not call live brokers,
use customer funds, submit real orders, or infer historical facts from synthetic data.

Run the focused suite in CI or an equipped development environment:

```bash
pytest -q tests/test_six_findings_simulation_gate.py
```

## What the simulations check

| Finding | Simulated case | What a pass means | What it does not mean |
|---|---|---|---|
| Survivorship bias | Deliberately high returns while the universe carries `CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY` | The limitation stays visible and the candidate is not promotion-eligible | It does not supply authoritative delisted-asset history |
| Slippage | Increase order size and synthetic volatility against fixed synthetic volume/spread | The heuristic responds monotonically and labels itself `UNVALIDATED_HEURISTIC` | It is not calibrated to real exchange/OTC executions |
| Perfect fills | 100 requested units versus synthetic bar volume of 20 with a 10% participation ceiling | Reproduces the current defect: simulated capacity is 2 while the current paper path records 100 and zero remaining | This is an explicit gap detector, not a passing execution-simulator validation |
| Selection bias | Extreme in-sample metrics with negative held-out returns | Candidate selection rejects the strategy based on OOS metrics | DSR still requires comparable historical trial evidence |
| Customer paper isolation | Customer identity plus paper mode | Existing guard remains active | Does not certify a separate simulation ledger; customer paper execution stays blocked |
| FX/commodities | Gold symbol while OANDA practice credentials are absent | Commodity routing fails rather than being passed to Yahoo FX | Does not prove live execution costs, financing/carry, or customer readiness |

## Release interpretation

The checks exercise code paths and guardrails; they do not replace evidence gates. In particular:

- Survivorship remains unresolved until reliable point-in-time listings and delisted-asset histories are ingested and hashed.
- Slippage remains an unvalidated heuristic until predictions are compared against held-out venue execution observations by instrument and order size.
- Perfect fills remain **unresolved** because this test confirms the production paper path still sets the full requested quantity as filled. A proper event-driven fill engine (latency, partial fills, liquidity, cancellation, rejection and recovery) must be integrated and then re-tested end-to-end.
- Selection remains paper/shadow-only while comparable trial-history and promotion-grade DSR evidence are absent.
- The customer paper guard must not be removed before a physically/logically separate, transactional simulation ledger passes isolation and concurrency tests.
- OANDA stays practice/demo-only. FX and commodity data routing is not proof of customer paper or live-trading readiness.

A green test result means that the stated behavior was reproducibly observed. It must not be converted into a general "all findings fixed" or "production ready" claim.
