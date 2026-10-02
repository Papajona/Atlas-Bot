
## Round 5 (maintainability)
- app/schemas.py: the 41 Pydantic request models moved out of main.py (mechanical extraction; verified by AST that they depend only on
  pydantic/Decimal). main.py re-imports them, so `from app.main import X` still works. main.py 5,116 -> ~4,890 lines.
- docs/history/: 81 historical changelogs/audits moved out of the repo root, with a README stating they are records, not evidence.
  .dockerignore excludes docs/. README link and one test path updated.
- Swallowed errors: the 7 `except Exception: pass` sites now log a warning (notably broker.cancel-all and the emergency-stop incident write).
  Correction: my earlier "about 30 bare pass" counted every `pass`; the true number of swallowed handlers is 7.
- customer_funds.record_ledger_incident is now public (old private name kept as an alias); execution.py no longer imports a private name.
- Test fixes: test_withdrawals_static now looks for CustomerWithdrawalCreate in schemas.py. test_customer_wallets_static asserted the literal
  `from_reserved = fee_d - from_available`, which my round-1 settle_trading_fee rewrite broke without my noticing; updated to the new
  `absorbable` semantics (behaviour intentionally changed: fills no longer raise, they book a deficit).
- NOT done: splitting routes out of main.py and decomposing execute_signal/_apply_fill_to_position. 36 test files assert on main.py/execution.py
  source text and I cannot run the DB-backed suite here, so a route split would be unverifiable. Do it after a green CI run.

## Round 6 (deployment review)
- Gemini defaults moved off `gemini-2.5-flash/pro` (Google schedules the 2.5 family for shutdown in Oct 2026) to `gemini-3.5-flash`;
  deploy scripts take `GEMINI_MODEL`/`GEMINI_STRATEGY_MODEL` from the environment. test_strategy_ai now asserts configurability and that no
  2.5 id is the deploy default instead of pinning a retiring name. scripts/check_ai_models.py checks configured ids against the providers.
- Migration job runs as a dedicated `atlas-migrator` service account (secretAccessor on the database URL only) with `--max-retries 0
  --task-timeout 900s`. Before, no `--service-account` meant the default compute identity, which bootstrap never granted the DB secret.
- cloud-run-deploy.sh warns when no Groq key is set: the adaptive-bot AI safety veto needs BOTH providers to approve (fail-closed).
- verify-live-money-runtime.sh now also runs the real-ledger Postgres concurrency test. DEPLOYMENT_ACCEPTANCE.md corrected (it said max=1,
  concurrency=1; scripts deploy max=5, concurrency=40) and migration ordering fixed. New deploy/DEPLOY_WORKFLOW.md documents order, gaps, rollback.
- Not changed: probes, canary/rollback flags and base-image digest pinning (documented as gaps; I did not want to guess gcloud flag syntax blind).

## Round 7 (failure-mode review)
- research_validation.cost_breakeven_report: break-even cost bps, annual turnover, annual cost drag, headroom and a 2x-cost stress test per strategy and for
  the ensemble; research_engine attaches it (`cost_analysis`, `cost_stress_ok`). oos_promotion_gate(require_cost_stress=...) fails closed; config
  `research_require_cost_stress` defaults **True** (promotion only; this cannot place or block an order). `research_cost_stress_multiplier` = 2.0.
  WARNING: with it on, strategies that do not clear 2x costs will stop being promoted; that is the intended behaviour, but expect fewer promotions.
- research_validation.edge_decay_check: statistical live-vs-expected check (pure function, NOT wired to the bot yet).
- customer.html: persistent risk disclosure banner (test added).
- docs/RISK_ASSESSMENT_3_10_46.md: failure-mode matrix, split rating (engineering / evidence of edge / customer-funds readiness), corrections to my earlier
  review, and the gates before customer money.
- Correction recorded: my claim that no cost-stress validation existed was wrong (cost_sensitivity existed; it was informational only).

## Round 8 (open items)
- Edge-decay halt wired into execution.risk_gate (new, non-reducing entries only): last `edge_decay_window_trades` (100) StrategyOutcome.net_return_bps for the customer
  (or platform), blocks when z <= -`edge_decay_z_halt` (2.0) with >= `edge_decay_min_trades` (60); opens HIGH incident `EDGE_DECAY:<scope>`. Null hypothesis is zero edge,
  so it fires on statistically significant losses, not on underperforming the research estimate. `edge_decay_halt_enabled` default True.
  WARNING: this is new live behaviour in the order path and is unrun against a database; closes/reductions are never blocked.
- Persistent trial counter: research_engine.backtest_all_strategies(prior_trial_labels=...) counts DISTINCT configuration labels (strategy/variant@config-fingerprint);
  daily_research stores `trial_labels` in research_runs.research_json and feeds the 365-day union back in. DSR trials = cumulative distinct labels.
- app/key_audit.py + `_customer_key_audit_loop` (worker, every `customer_key_audit_interval_seconds`=900): re-reads GET /sapi/v1/account/apiRestrictions (field names
  confirmed against Binance docs) and suspends VERIFIED accounts with withdrawals/internal-or-universal transfer/margin/futures/options enabled or spot trading missing;
  fetch failures leave a HIGH incident and do not suspend. The call goes through `client.request("account/apiRestrictions","sapi","GET",{})`; I could not run it against Binance.
- app/prompt_safety.py: untrusted news / trade-plan text is JSON-escaped (`<`,`>`), capped, wrapped in `<untrusted_data>` and preceded by a data-only instruction; markers logged.
- Correction: "enforce trade-only keys" was listed as open in round 7 but was already enforced at provisioning and build time; the genuine gap (stale flags) is what key_audit closes.

## Round 9 (deployment guide)
- deploy/DEPLOY_WORKFLOW.md rewritten as the full deployment guide (topology, secrets/config matrix, 10-step runbook, release management, alerting, troubleshooting, go-live gates).
- cloud-run-deploy.sh: fixed `--set-secrets` splice when no Groq key (missing comma produced an invalid secret reference); warning text now mentions the worker.
- cloud-run-worker-deploy.sh: optional `SECRET_GROQ_REF` and Groq model env so worker bot cycles can pass the dual-AI veto; warns when unset.
