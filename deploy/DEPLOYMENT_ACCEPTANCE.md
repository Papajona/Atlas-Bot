# Deployment acceptance checklist

- [ ] Supabase project created and encrypted PostgreSQL connection configured.
- [ ] Google Cloud APIs enabled and Artifact Registry repository created.
- [ ] Dedicated Cloud Run service account created.
- [ ] Dedicated model bucket created with least-privilege object access.
- [ ] Secret Manager contains database/admin secrets.
- [ ] API deployed by cloud-run-deploy.sh (currently min=1, max=5, concurrency=40, PROCESS_ROLE=api: no background loops) and the worker pool deployed by cloud-run-worker-deploy.sh (1 instance). If you want a single API instance, change the script; this checklist no longer states a number the script does not use.
- [ ] Alembic migrations completed successfully BEFORE the API and worker revisions were deployed (see DEPLOY_WORKFLOW.md for the order).
- [ ] Cloud Run `/healthz` returns 200.
- [ ] Cloud Run `/readyz` returns 200 and reports database readiness.
- [ ] Android app loads the Cloud Run dashboard over HTTPS.
- [ ] Android dashboard API calls reach Cloud Run and authenticate with the admin token.
- [ ] Admin token is required outside development.
- [ ] Paper trading remains enabled.
- [ ] Live trading remains disabled.
- [ ] TRON funding remains disabled unless separately approved: API and worker deploy scripts default `USDT_TRON_ENABLED=false` and `USDT_TRON_SWEEP_ENABLED=false`; enabling funding requires `ALLOW_TRON_FUNDING=YES`, and mainnet additionally requires `ALLOW_MAINNET_TRON_FUNDING=YES`. Paper mode is not a funding-isolation control.
- [ ] OANDA demo/sandbox connectivity tested.
- [ ] Crypto sandbox/testnet connectivity tested if enabled.
- [ ] Model train → signal flow survives a Cloud Run revision restart.
- [ ] Reconciliation tested after simulated timeout/restart.
- [ ] Kill switch tested.
- [ ] Backup and restore procedure tested.
- [ ] Logs/alerts configured.
- [ ] Four-week interim forward paper/shadow gate: at least 28 consecutive calendar days of the exact configuration, with positive net results under 2x cost-stress, DSR >= 0.95, active risk controls, retained evidence and no unresolved critical incidents.
- [ ] At least 90 days of out-of-sample paper performance recorded for the exact configuration, demonstrating positive net returns under 2x cost-stress and DSR >= 0.95. The 28-day interim gate does not replace this final evidence requirement.
- [ ] Edge-decay halt monitor active and verified on historical closed trades ($z \le -2$).
- [ ] Worker mid-fill failure drill (`kill -9` during execution) verified clean idempotent state recovery.
- [ ] Legal and regulatory review of product disclosures, jurisdiction marketing rules, and risk disclaimers.
- [ ] Only after all checks pass: evaluate a restricted, unlevered live pilot.

## 3.10.24 security deployment requirements
- Cloud Run must be reachable through an external HTTPS Load Balancer with Cloud Armor; deployment uses `internal-and-cloud-load-balancing` ingress and does not rely on direct public `run.app` ingress.
- Set the five `SECRET_*_VERSION` deployment variables to explicit Secret Manager versions before running `cloud-run-deploy.sh`.
- Customer Binance API keys must report withdrawals disabled and universal transfer disabled before an account is marked VERIFIED.
- Apply Alembic migration `0023_security_billing_hardening` before starting the application.
