# Risk assessment (3.10.46): why AI trading bots fail, and what this system can credibly claim

Written from three seats (system risk manager, code reviewer, market-research supervisor). It supersedes the single "x / 10" scores in earlier
review messages, which measured software quality and let a reader infer more than the evidence supports.

## 1. Three separate questions, three separate answers
| Question | Rating | Basis |
|---|---|---|
| Is the software well engineered and controlled? | **7.9 / 10** (engineering) | Ledger design, idempotency, fail-closed gates, signed models. Most DB, concurrency, sandbox and reconcile tests are written but **unexecuted**; `main.py`/`execute_signal` are still too large. |
| Is there evidence the strategies earn money after costs? | **Not demonstrated (treat as 1-2 / 10)** | No live or out-of-sample paper track record exists. Defaults (1h bars, ~6.5 bps taker+slippage assumption) are cost-heavy; the cited research is daily-frequency futures. |
| Is it ready to hold customer money? | **NOT READY (red)** | Needs the gates in section 5 first. A bot with no edge that is perfectly engineered still loses money, slowly and reliably. |

## 2. What actually makes AI trading bots fail (and where this app stands)
| # | Failure mode | Evidence | This app | Action in 3.10.46 |
|---|---|---|---|---|
| 1 | **Costs exceed edge** (fees, slippage, funding, overtrading) | Alpha Arena (Nof1, real money, Oct-Nov 2025): the reported fee bill of one model exceeded $600 on a $10,000 stake and most models lost; one run saw 1,418 trades by a single model (sources are secondary and conflict on details). Illustration with this app's own cost model: at 0.30 position change per hourly bar a strategy trades ~2,600x capital per year; at 6.5-10.5 bps all-in that is approximately a 171-276% annual cost drag. | Cost grid existed but was informational only and ensemble-only | `cost_breakeven_report` (break-even bps, annual turnover, cost drag, headroom) per strategy; **promotion now requires a positive net edge at 2x costs** (`research_require_cost_stress`, default on) |
| 2 | **Overfitting / multiple testing** | Bailey et al. (backtest overfitting, deflated Sharpe); Quantopian study: Sharpe predicted out-of-sample results very poorly (R^2 < 0.025) | Proxy only; formal DSR added last round but off by default; trial counter is per run | DSR wired (floor off until you choose). Trial count is now **cumulative over distinct configurations** (strategy/variant + config fingerprint, 365-day window, persisted in `research_runs`), so changing parameters raises the hurdle but re-running the same configs does not |
| 3 | **Edge decay / regime change** | Alpha-decay is the norm; momentum evidence for crypto is mixed | Feature-drift PSI and run-over-run flags exist, but nothing compares *realized* results with the research expectation | `edge_decay_check` now **wired into `risk_gate`**: new entries are blocked when the last 100 closed-trade net returns are significantly below zero (z <= -2, min 60 trades; closes always pass; setting `edge_decay_halt_enabled`) |
| 4 | **Leverage and ruin** | Alpha Arena commentary attributes the biggest losses to leverage and frequent trading (secondary sources) | Customer cash-only default on; risk/trade 0.5%, daily loss 3%, drawdown 15%; max leverage default 3x | No change; keep customers unlevered |
| 5 | **Operational failure** | Knight Capital 2012 (over $460M in 45 min after a deployment mismatch) | Kill switch, fencing lease, fail-closed gates; no canary/rollback in scripts | Documented in deploy/DEPLOY_WORKFLOW.md |
| 6 | **Credential/platform compromise** | 3Commas API-key leak (2022), cause disputed | Provisioning and order-time checks already reject withdrawal/universal-transfer keys (my earlier "open" was wrong). Gap was stale stored flags | `key_audit` re-reads Binance `apiRestrictions` every 15 min and suspends VERIFIED accounts that gained withdrawal, transfer, margin, futures or options rights |
| 7 | **LLM-specific**: look-ahead/memorisation, non-reproducibility, prompt injection | 2025-26 studies of LLM trading agents show large in-sample vs out-of-sample gaps (small samples); OWASP LLM01/LLM06 | AI has no execution authority; veto-only; schema-validated strategy output | Keep it that way. Untrusted news/third-party text is now escaped, length-capped and wrapped in labelled `<untrusted_data>` blocks with a standing data-only instruction; markers are logged |
| 8 | **Misrepresenting what the AI does** | SEC 2024: Delphia and Global Predictions paid $400,000 combined for "AI washing" | UI had **no** risk disclosure | Customer UI now carries a risk disclosure; claims must match the architecture (AI = safety review + research summaries) |
| 9 | **Retail loses by default** | Chague et al.: 97% of persistent Brazilian futures day traders lost money | n/a | Do not market returns; do not imply skill |

## 3. Review of my own earlier assessment (corrections)
- I scored one number; it blended engineering with profitability evidence. Replaced by section 1.
- I said there was "no cost-stress validation". Wrong: `cost_sensitivity` existed. The accurate finding is that it was informational, ensemble-only, and
  never gated promotion.
- I first recommended letting customer balances go negative; the database CHECK constraints forbid it (fixed in round 1 with a booked deficit).
- I called an audit "disagreeing" about 69 vs 383 tests; they were different suites.
- "About 30 bare pass" was really 7 swallowed handlers. These are corrected in the changelog; I note them here because they all pushed in the same
  direction (overstating problems or confidence) and a reader should weigh my numbers accordingly.
- My round-6 "open: enforce trade-only keys" was also wrong: it was largely enforced already; the real gap was staleness (now audited).
- I have executed none of the DB/Postgres/exchange tests. Every claim that depends on them is "designed", not "proven".

## 4. What is realistically possible to work
Grounded in the evidence above, not a promise of profit:
1. **Low turnover.** Trend / volatility-targeted exposure on a few liquid majors at 4h-daily horizons. Cost drag scales with turnover; the strongest published support
   (Moskowitz, Ooi, Pedersen 2012) is for daily-frequency trend in futures, and crypto replication is mixed.
2. **Cost discipline as a first-class constraint.** Break-even cost per unit traded must clear 2x the real all-in cost before anything is promoted.
3. **Small, capped, unlevered customer exposure** with staged capital increases only after live results match research (edge-decay monitor green).
4. **AI as filter and analyst, not alpha.** Veto-only safety review and research summaries are defensible; "AI generates returns" is not supported by the
   live LLM-trading experiments to date (single short runs, high variance, low reliability).
5. **Evidence discipline.** Pre-state the hypothesis, count every variant tried, require DSR >= 0.95, paper trade out-of-sample for months, and write kill criteria
   before going live.
6. **Honest product framing.** Software and risk tooling, not performance claims. Get counsel on licensing and marketing rules for each jurisdiction served.

## 5. Gates before any customer money (all must be true)
1. CI green including Postgres concurrency, live-fee, fake-broker drill and ledger invariants; sandbox drill passed on a testnet; worker `kill -9` mid-fill drill clean.
2. At least 3 months of out-of-sample paper results for the exact configuration, with net-of-cost return positive and within the research confidence band.
3. `research_require_cost_stress` on and the selected strategy passing it; DSR floor chosen (0.95 suggested) with a persistent trial counter.
4. Edge-decay halt is wired (done in 3.10.46); validate its thresholds on paper data and decide whether live accounts should require manual resume.
5. Written kill criteria (e.g. drawdown, edge-decay HALT, ledger invariant breach) and a tested rollback.
6. Legal/regulatory review of the product and its marketing; risk disclosure reviewed by counsel.
7. Start with a capped pilot (tiny notional per customer, no leverage); raise only when 1-6 keep holding.
