# Atlas deployment guide (release 3.10.47): Google Cloud Run + Supabase PostgreSQL + Redis

**Read this first.** This guide describes what the scripts in `deploy/` do today, in the order they must run, with the checks that should pass at each step and
the gaps that remain. Nothing here was executed against a real project by its author (no `gcloud`, network, Postgres or exchange access in the authoring
environment); every command is derived from the scripts and the vendor documentation cited. **Live trading stays OFF in every script**
(`PAPER_TRADING=true LIVE_TRADING_ENABLED=false BROKER_SANDBOX=true`). Turning it on is a separate decision gated by section 11.

---------------------------------------------------------------------------------------------------------------------------------

## 1. Architecture at a glance

| Component | Cloud resource | Script | Role | Notes |
|---|---|---|---|---|
| API | Cloud Run **service** | `cloud-run-deploy.sh` | `PROCESS_ROLE=api` | HTTP only; ingress `internal-and-cloud-load-balancing`; min 1 / max 5 instances, concurrency 40, 2 vCPU, 2 GiB, timeout 300 s; models mounted **read-only** |
| Worker | Cloud Run **worker pool** | `cloud-run-worker-deploy.sh` | `PROCESS_ROLE=worker`, `python -m app.worker` | always-on, no URL, no autoscaling, 1 instance, 2 vCPU, 2 GiB; models + staging mounted **read-write** |
| Migrations | Cloud Run **job** | `cloud-run-migration-job.sh` | `alembic upgrade head` | runs as `atlas-migrator`; no retries; 900 s timeout |
| Database | Supabase PostgreSQL | (external) | system of record | `postgresql+asyncpg://...?ssl=require`; Alembic owns the schema |
| Cache/locks | Redis (`redis:8` in compose) | (external) | rate limiting, single-writer execution lease, locks | `DISTRIBUTED_RATE_LIMIT_REQUIRED=true` |
| Models | two GCS buckets | `gcp-bootstrap.sh` | `/app/models` (promoted), `/app/model-staging` (candidates) | signed with Ed25519; API holds only the public key |
| Secrets | Secret Manager | `gcp-bootstrap.sh` | all credentials, referenced by **explicit version** | never put secrets in env files or the repo |
| Edge | external HTTPS load balancer + Cloud Armor | manual | public entry point | the API is not reachable directly by design |

**What runs where** (from `startup()` in `app/main.py`): the **API runs no background loops**. The **worker** runs the reconciliation loop, the TRON deposit
monitor (if enabled and a TronGrid key is present), the heartbeat, withdrawal recovery, the **customer exchange-key audit** (new in 3.10.47), the daily research
loop, and the adaptive bot and executor controllers. A `job` role runs only the heartbeat. Consequence: Cloud Run's request-based CPU throttling cannot starve
anything on the API, while the worker pool is always-on by design.

```
 Internet -> Cloud Armor -> HTTPS LB -> Cloud Run API (role=api) --\
                                                                    +--> Supabase Postgres <-- Cloud Run worker pool (role=worker)
 Cloud Run job (alembic) ------------------------------------------/                              |  loops: reconcile, TRON, recovery,
 Redis <---- API (rate limit) ---- worker (execution lease)                                       |  key audit, research, bot controllers
 GCS atlas-models (API ro / worker rw)   GCS atlas-model-staging (worker rw)                      v
 Secret Manager (per-secret IAM: API, worker, migrator)                              exchanges (sandbox), TronGrid, Gemini, Groq
```

---------------------------------------------------------------------------------------------------------------------------------

## 2. Prerequisites

**Tools:** `gcloud` (a version that has `gcloud run worker-pools`; the docs show it under both GA and `beta`, so check `gcloud run worker-pools deploy --help`),
`git`, `python` 3.13 with the repo's `requirements.txt` + `requirements-dev.txt`, `openssl`, `psql`, `docker` (optional, local builds).

**Accounts/services:** a GCP project with billing; a Supabase project (PostgreSQL URL + Auth with TOTP for admins); a managed Redis; a TronGrid API key; a
Gemini key and a Groq key; an HTTPS domain for the load balancer.

**Values you must generate once** (store them only in Secret Manager via bootstrap):

| Value | How |
|---|---|
| `APP_ENCRYPTION_KEY` (Fernet) | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `SECRET_KEY` | bootstrap generates one if unset (`secrets.token_urlsafe(48)`) |
| Model signing keys (Ed25519) | `openssl genpkey -algorithm ed25519 -out model_signing_private.pem` then `openssl pkey -in model_signing_private.pem -pubout -out model_signing_public.pem` |
| `USDT_TRON_ACCOUNT_XPUB` | `python deploy/generate_tron_xpub.py` on an **offline** machine; only the **account-level public** xpub goes to the cloud (the app holds no TRON seed) |
| `FUNDING_WEBHOOK_SECRET` | long random string shared with the funding provider |
| `ADMIN_SUPABASE_USER_IDS` | comma-separated Supabase Auth UUIDs allowed into the admin console (TOTP is enforced: `ADMIN_TOTP_REQUIRED=true`) |
| `FORWARDED_ALLOW_IPS` | the trusted load-balancer/proxy IP or CIDR. **Never `*`** |
| `BACKUP_RECOVERY_URL` | where your backup/restore procedure is documented or triggered. Production **refuses to start** without it (`REQUIRE_BACKUP_RECOVERY_CONFIG=true`) |

---------------------------------------------------------------------------------------------------------------------------------

## 3. Secrets and configuration matrix

### 3.1 Secret Manager (created by `gcp-bootstrap.sh`; IAM per secret)
| Secret | API | Worker | Migrator |
|---|---|---|---|
| `atlas-database-url` | yes | yes | **yes (only this)** |
| `atlas-secret-key`, `atlas-app-encryption-key`, `atlas-redis-url` | yes | yes | no |
| `atlas-usdt-tron-account-xpub`, `atlas-trongrid-api-key` | yes | yes | no |
| `atlas-model-signing-public-key` | yes | yes | no |
| `atlas-model-signing-private-key` | **no** | yes (to sign trained models) | no |
| `atlas-supabase-anon-key`, `atlas-funding-webhook-secret` | yes | no | no |
| `atlas-gemini-api-key` | yes | yes | no |
| `atlas-groq-api-key` | optional* | optional* | no |

\* **Both Gemini and Groq must approve every trade plan** (the safety veto fails closed). The veto runs in `customer_start_bot` (API) **and** in the persisted bot
cycles (worker). If either key is missing in a process, bots in that process return `NO_TRADE` (`ai_safety_not_configured`). Set Groq on **both** the API
(`SECRET_GROQ_API_KEY_VERSION`) and the worker (`SECRET_GROQ_REF`). Both scripts now warn when it is missing.

### 3.2 Service accounts
`SERVICE_ACCOUNT` (API, e.g. `atlas-trading-api`), `WORKER_SERVICE_ACCOUNT` (e.g. `atlas-trading-worker`), `MIGRATION_SERVICE_ACCOUNT` (default `atlas-migrator`).
Each gets `secretAccessor` only on the secrets in the table above. Review the bucket roles bootstrap grants (API read-only on models, worker read/write on both).

### 3.3 Key runtime settings (environment variables; field names of `app/config.py`)
| Setting | Default | Meaning |
|---|---|---|
| `ENVIRONMENT` | script sets `production` | production enables strict startup validation (xpub, Supabase, webhook, model keys, backup URL, Redis rate limit) |
| `PROCESS_ROLE` | `api` / `worker` / `job` | decides which loops start |
| `BACKGROUND_RECONCILIATION_ENABLED` | API script `false`; default `true` | the worker relies on the default `true` |
| `ADAPTIVE_BOT_CONTROLLER_ENABLED` | `true` | worker starts bot and executor controllers |
| `GEMINI_MODEL`, `GEMINI_STRATEGY_MODEL` | `gemini-3.5-flash` | `gemini-2.5-*` is scheduled for shutdown in Oct 2026; override via env at deploy |
| `GROQ_MODEL`, `GROQ_STRATEGY_MODEL` | `openai/gpt-oss-20b` | check with `python -m scripts.check_ai_models` before every deploy |
| `RESEARCH_REQUIRE_COST_STRESS` / `RESEARCH_COST_STRESS_MULTIPLIER` | `true` / `2.0` | promotion needs a positive net edge at 2x costs |
| `RESEARCH_MIN_DEFLATED_SHARPE` | `0.0` (off) | set ~0.95 once you have seen real values |
| `EDGE_DECAY_HALT_ENABLED`, `EDGE_DECAY_WINDOW_TRADES`, `EDGE_DECAY_MIN_TRADES`, `EDGE_DECAY_Z_HALT` | `true`, 100, 60, 2.0 | blocks **new** entries when recent realized net returns are significantly negative; closes always pass |
| `CUSTOMER_KEY_AUDIT_INTERVAL_SECONDS` | 900 (0 disables) | worker re-verifies customer exchange-key permissions |
| `MAX_DRAWDOWN`, `DAILY_LOSS_LIMIT`, `MAX_LEVERAGE`, `RISK_PER_TRADE` | 0.15, 0.03, 3.0, 0.005 | keep customer trading unlevered; pilot with smaller caps |
| `MAX_NOTIONAL_USD`, `MAX_TOTAL_EXPOSURE_USD`, `MAX_OPEN_POSITIONS` | 10000, 20000, 3 | **lower these for any pilot** |

---------------------------------------------------------------------------------------------------------------------------------

## 4. Step-by-step runbook

Set once per shell: `export GCP_PROJECT_ID=... GCP_REGION=europe-west1 AR_REPOSITORY=trading MODEL_BUCKET=... MODEL_STAGING_BUCKET=...`.
Abort and investigate on the first failed check; do not skip forward.

### Step 1: Bootstrap (once per project) -- `deploy/gcp-bootstrap.sh`
Required environment: `GCP_PROJECT_ID GCP_REGION AR_REPOSITORY MODEL_BUCKET MODEL_STAGING_BUCKET SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT DATABASE_URL
USDT_TRON_ACCOUNT_XPUB TRONGRID_API_KEY APP_ENCRYPTION_KEY REDIS_URL SUPABASE_URL SUPABASE_ANON_KEY FUNDING_WEBHOOK_SECRET ADMIN_SUPABASE_USER_IDS
BACKUP_RECOVERY_URL GEMINI_API_KEY FORWARDED_ALLOW_IPS MODEL_SIGNING_PUBLIC_KEY MODEL_SIGNING_PRIVATE_KEY`; optional `GROQ_API_KEY`, `MIGRATION_SERVICE_ACCOUNT`.
It enables the required APIs, creates the Artifact Registry repo, the three service accounts, the two buckets, every secret, and the per-secret IAM bindings.
**Check:** `gcloud secrets list`, `gcloud iam service-accounts list`; confirm `atlas-model-signing-private-key` is readable by the **worker only**.

### Step 2: Audit-log retention -- `deploy/gcp-security-audit-retention.sh`, then `deploy/verify-cloud-audit.sh`
Creates the log sink/bucket retention. Lock the retention policy only after reviewing it (locking is irreversible).

### Step 3: Pre-flight against a disposable PostgreSQL -- `deploy/verify-live-money-runtime.sh`
`DATABASE_URL=postgresql://... bash deploy/verify-live-money-runtime.sh` runs: `alembic upgrade head`, `alembic check`, `validate-postgres-constraints.py`,
the Postgres concurrency tests (the original probe test **and** `tests/test_postgres_ledger_concurrency_3_10_46.py`, which exercises the real ledger under
contention), the live-money static suites, `bandit -lll`, and `pip-audit --strict`. It never enables live trading.
**Pass criteria:** zero failures. Expect fixes on the first run: the 3.10.47 tests were written without a database available.
Also run the whole suite once: `pytest -q`.

### Step 4: Build the image (immutable, tagged by commit)
`bash deploy/cloud-run-deploy.sh` builds with `gcloud builds submit --tag <region>-docker.pkg.dev/<project>/<repo>/<service>:<git-sha>`.
**Known gap:** it builds the *working tree*, so uncommitted changes ship under a clean-looking sha. Build from a clean checkout or CI. Record the digest
(`gcloud artifacts docker images describe ... --format='value(image_summary.digest)'`) and deploy by digest where possible.

### Step 5: Migrate -- `deploy/cloud-run-migration-job.sh`
```
export CLOUD_RUN_MIGRATION_JOB=atlas-migrate IMAGE=<the image from step 4> DATABASE_SECRET_REF=atlas-database-url:<version>
bash deploy/cloud-run-migration-job.sh        # deploys the job as atlas-migrator, executes it, and waits
```
**Rules:** (a) migrations must be backward compatible with the *previous* API and worker revisions, because both keep running until steps 6 and 7 finish;
(b) schema changes follow expand then contract (add nullable/new, deploy code, remove later); do not rely on `alembic downgrade`; (c) if the job fails, stop:
`--max-retries 0` is deliberate. **Check:** job execution `Succeeded`; `alembic current` equals `alembic heads`.

### Step 6: Deploy the API -- `deploy/cloud-run-deploy.sh`
Required: all Step-1 variables plus `CLOUD_RUN_SERVICE` and explicit secret versions `SECRET_DATABASE_VERSION SECRET_KEY_VERSION SECRET_TRON_XPUB_VERSION
SECRET_APP_ENCRYPTION_VERSION SECRET_REDIS_URL_VERSION SECRET_TRONGRID_VERSION SECRET_MODEL_SIGNING_PUBLIC_VERSION SECRET_SUPABASE_ANON_KEY_VERSION
SECRET_FUNDING_WEBHOOK_VERSION SECRET_GEMINI_API_KEY_VERSION` and (recommended) `SECRET_GROQ_API_KEY_VERSION`.
It deploys with `--execution-environment gen2`, the read-only models volume, ingress `internal-and-cloud-load-balancing`, and `--allow-unauthenticated`
(authentication is enforced by the app; the ingress setting restricts who can reach it).
**Check:** `GET /healthz` (liveness) then `GET /readyz` (database, migrations current, Redis, recovery config, admin auth). `/docs` must be off
(`DOCS_ENABLED=false`) and `/metrics` must require an admin (`METRICS_REQUIRE_ADMIN=true`).

### Step 7: Deploy the worker pool -- `deploy/cloud-run-worker-deploy.sh`
```
export WORKER_POOL=atlas-trading-worker IMAGE=<same image> WORKER_SERVICE_ACCOUNT=atlas-trading-worker \
  DATABASE_SECRET_REF=atlas-database-url:<v> SECRET_REDIS_URL_REF=atlas-redis-url:<v> SECRET_KEY_REF=atlas-secret-key:<v> \
  SECRET_APP_ENCRYPTION_REF=atlas-app-encryption-key:<v> SECRET_TRON_XPUB_REF=atlas-usdt-tron-account-xpub:<v> SECRET_TRONGRID_REF=atlas-trongrid-api-key:<v> \
  SECRET_MODEL_SIGNING_PUBLIC_REF=atlas-model-signing-public-key:<v> SECRET_MODEL_SIGNING_PRIVATE_REF=atlas-model-signing-private-key:<v> \
  SECRET_GEMINI_REF=atlas-gemini-api-key:<v> SECRET_GROQ_REF=atlas-groq-api-key:<v> BACKUP_RECOVERY_URL=... 
bash deploy/cloud-run-worker-deploy.sh
```
**Check:** `ServiceHeartbeat` rows appear for role `worker`; logs show the loops starting; the single-writer execution lease is held (only one worker may hold it).
**Rollout caveat:** a worker-pool update can briefly run old and new instances together. Safety then rests on the distributed lease with fencing; keep live
trading off until a rollout drill proves it.

### Step 8: Edge
Create the serverless NEG for the API service, an external HTTPS load balancer with a managed certificate, and a Cloud Armor policy (rate limits, geo/IP rules as
needed). Set `FORWARDED_ALLOW_IPS` to the load balancer's range. Re-run `/healthz` and `/readyz` through the public hostname.

### Step 9: Acceptance -- `deploy/DEPLOYMENT_ACCEPTANCE.md` plus drills
1. Admin sign-in with TOTP; a non-admin Supabase user is refused.
2. `GET /api/admin/ledger/invariants` returns `ok: true` (journals balance, balances equal journal net). Any `ok: false` opens a CRITICAL incident.
3. `GET /api/admin/custody/reconciliation` (on-chain TRON vs customer liabilities; monitoring only, on demand).
4. Exchange **sandbox** drill: `python -m scripts.exchange_sandbox_drill --i-understand-sandbox` (connectivity, where fee data appears, market fill, duplicate
   `clientOrderId`, open/cancel/double-cancel, fee extraction vs the venue trade list, plus a reduce-only close on a derivatives testnet). A SKIP is not a pass.
5. **Crash drill on staging:** `kill -9` the worker during a sandbox fill; confirm reconcile completes the order once, fees are charged once, and the
   ledger invariant stays `ok`.
6. `python -m scripts.check_ai_models` passes (needs the provider keys).

### Step 10: Operate in paper mode, then decide
Live trading requires the gates in section 11. Nothing in the deploy scripts enables it.

---------------------------------------------------------------------------------------------------------------------------------

## 5. Release management

**Canary and rollback (recommended; not in the scripts today).** The `--no-traffic` and `--tag` flags on `gcloud run deploy` are documented; they are
confirmed against the gcloud reference. After a `--no-traffic` deployment the latest revision stops receiving traffic on future deploys until you restore it with
`update-traffic`.
```
gcloud run deploy "$CLOUD_RUN_SERVICE" ...same flags... --no-traffic --tag canary        # revision gets its own URL, 0% traffic
curl -fsS https://canary---<service>-<hash>-<region>.a.run.app/readyz                    # smoke test the tagged URL
gcloud run services update-traffic "$CLOUD_RUN_SERVICE" --to-tags canary=10 --region "$GCP_REGION"   # 10% (check flag with --help)
gcloud run services update-traffic "$CLOUD_RUN_SERVICE" --to-latest --region "$GCP_REGION"           # promote
gcloud run services update-traffic "$CLOUD_RUN_SERVICE" --to-revisions <previous-revision>=100 --region "$GCP_REGION"   # rollback
```
**Health probes (recommended; not in the scripts today).** Cloud Run health checks are configured on the service; a Dockerfile `HEALTHCHECK` is a Docker-engine
feature and is not how Cloud Run decides health. The documented startup-probe syntax is:
`--startup-probe httpGet.path=/readyz,httpGet.port=8000,initialDelaySeconds=5,failureThreshold=12,timeoutSeconds=3,periodSeconds=5`.
A liveness probe uses the same style (`--liveness-probe httpGet.path=/healthz,httpGet.port=8000,...`); confirm exact keys with `gcloud run deploy --help`.
Use `/readyz` for startup and `/healthz` for liveness so a slow database does not cause restart loops. When a startup probe is configured, liveness and
readiness checks are held off until it passes.

**Database changes:** expand/contract only; take a backup before each migration; keep the previous revision deployable until the migration is proven.
**Secrets rotation:** create a new Secret Manager version, redeploy with the new version number (scripts pin versions), then disable the old one.
**Model promotion:** the worker trains and signs candidates into `/app/model-staging`, promotes into `/app/models`; the API loads read-only and verifies the
Ed25519 signature. Cloud Storage FUSE gives no write concurrency control, so keep **one** writer (the worker, 1 instance). Outside staging/production only a
co-located hash is checked; treat any shared bucket as production.

---------------------------------------------------------------------------------------------------------------------------------

## 6. Observability and alerting

Alert on, at minimum: any open **CRITICAL** incident (`LEDGER_INVARIANT_BREACH`, `CUSTOMER_DEFICIT:*`, `KEY_PERMISSIONS:*`, reconcile/recovery failures); **HIGH**
incidents (`EDGE_DECAY:*`, `LIVE_FEE_ESTIMATED:*`, `KEY_AUDIT_UNVERIFIED:*`); worker heartbeat older than a few minutes; `readyz` failing; Cloud Run 5xx rate and latency;
Redis availability; Cloud Armor blocks. `/metrics` (Prometheus format, admin only) exposes application counters. Schedule `GET /api/admin/ledger/invariants` and
`GET /api/admin/custody/reconciliation` from an external scheduler and alert on a non-200 or `ok: false`: they are on-demand today, not scheduled.

---------------------------------------------------------------------------------------------------------------------------------

## 7. Security controls in the deployment

Per-secret IAM with a dedicated identity per workload; migrator limited to the database URL; ingress limited to the load balancer; TOTP-protected admin;
withdrawal step-up and address cooling-off; customer exchange keys must be trade-only (provisioning and broker construction reject withdrawal/universal-transfer
rights, and since 3.10.47 the worker re-reads Binance `apiRestrictions` every 15 minutes and **suspends** accounts whose live permissions exceed policy);
AI layer has no execution authority and external text is wrapped as untrusted data; non-root container; audit log retention. Run `bandit` and `pip-audit` in CI
and before each release (the verify script does).

---------------------------------------------------------------------------------------------------------------------------------

## 8. Defects found and fixed while preparing this guide (3.10.47)

| Defect | Effect before | Fix |
|---|---|---|
| Migration job had no `--service-account` | ran as the default compute identity, which bootstrap never granted the DB secret (or ran over-privileged) | dedicated `atlas-migrator`, DB-secret-only, `--max-retries 0 --task-timeout 900s` |
| Worker script never set `BACKUP_RECOVERY_URL` | production startup refused to boot: the worker pool crash-looped | variable now required and passed |
| API script spliced the Gemini secret without a leading comma when Groq was unset | `--set-secrets` became `...atlas-trongrid-api-key:2GEMINI_API_KEY=...`: an invalid deploy command on the no-Groq path | `AI_SECRET_ARGS` always starts with a comma; both branches checked |
| Worker had no way to receive a Groq key | every worker bot cycle ended `ai_safety_not_configured` (NO_TRADE) because the veto needs both providers | optional `SECRET_GROQ_REF` + Groq model env added; warns when unset |
| `gemini-2.5-*` defaults | models scheduled for shutdown Oct 2026 | defaults `gemini-3.5-flash`, env-configurable; `scripts/check_ai_models.py` |
| `DEPLOYMENT_ACCEPTANCE.md` stated `max=1, concurrency=1` and listed migrations after deploy | wrong numbers and order | corrected |
| New real-ledger Postgres test not in the pre-flight script | concurrency of the actual ledger never exercised | added to `verify-live-money-runtime.sh` |

## 9. Known gaps (not fixed by scripts)
No canary/rollback or explicit probes in the scripts (section 5); base image `python:3.13-slim` is not pinned by digest and there is no image-vulnerability gate
between build and deploy; builds use the working tree; the worker holds the model-signing **private** key (acceptable for paper; for live, sign in CI/KMS and give the
worker only the public key); invariant/custody checks are not scheduled; no automated exchange-venue-balance versus reserved-funds reconciliation; `main.py` and `execute_signal`
remain very large; docker-compose is for local use only (`redis:8-alpine`, `postgres:17-alpine`, single `trading-app`), not a production topology.

## 10. Troubleshooting
| Symptom | Likely cause | Action |
|---|---|---|
| Worker pool restarts immediately | missing `BACKUP_RECOVERY_URL`, xpub, signing private key, or Redis | `gcloud run worker-pools logs`/Cloud Logging; config validation names the missing setting |
| API `readyz` fails | migrations behind, DB unreachable (`?ssl=require`), Redis down | run the migration job; check secrets and VPC egress |
| Bots always `NO_TRADE` / `ai_safety_not_configured` | a provider key missing in that process (API or worker) | set Groq (and Gemini) in both |
| `ai_safety_gate` vetoes | provider disagreed or returned invalid JSON | see `ADAPTIVE_BOT_AI_VETO` audit events; retry later |
| Entries blocked "Edge-decay halt" | recent realized net returns significantly negative | review the strategy; the halt lifts as the window recovers; closes are never blocked |
| Customer account `SUSPENDED` | key audit found extra permissions | the customer must re-issue a trade-only, no-withdraw key; incident `KEY_PERMISSIONS:<id>` |
| Promotions stopped | cost-stress gate on (`RESEARCH_REQUIRE_COST_STRESS`) and no strategy clears 2x costs | read `cost_analysis` in the research output; do not simply disable it |
| Model not loading | signature/hash mismatch, wrong bucket, UID/GID | check `MODEL_SIGNING_PUBLIC_KEY`, bucket mount options `uid=10001;gid=10001` |

## 11. Go-live gates (all must hold before any customer money)
1. CI green: full `pytest`, Postgres concurrency (both tests), live-fee, fake-broker drill, ledger invariants, static suites; `bandit` and `pip-audit` clean.
2. Sandbox drill passed on the venue testnet incl. reduce-only close; worker `kill -9` drill clean; ledger invariants `ok` afterwards.
3. At least 3 months of out-of-sample **paper** results for the exact configuration, net of costs, passing the 2x cost stress; DSR floor chosen from real values.
4. Canary and rollback exercised once; probes configured; alerts wired and tested; invariant and custody checks scheduled.
5. Written kill criteria and incident runbook; backups restored once in a drill (`BACKUP_RECOVERY_URL` points at a real procedure).
6. Legal/regulatory review of the product and its marketing in each jurisdiction served; risk disclosure reviewed by counsel.
7. Capped pilot: tiny notional per customer, no leverage, `MAX_NOTIONAL_USD` and `MAX_TOTAL_EXPOSURE_USD` lowered; raise only while 1 to 6 keep holding.
See `docs/RISK_ASSESSMENT_3_10_46.md` for the failure-mode analysis behind these gates.

## Appendix A: command cheat sheet
```
bash deploy/gcp-bootstrap.sh                                  # once
DATABASE_URL=postgresql://... bash deploy/verify-live-money-runtime.sh
bash deploy/cloud-run-deploy.sh                               # builds + deploys API
IMAGE=... DATABASE_SECRET_REF=atlas-database-url:N CLOUD_RUN_MIGRATION_JOB=atlas-migrate bash deploy/cloud-run-migration-job.sh
IMAGE=... (worker refs) bash deploy/cloud-run-worker-deploy.sh
python -m scripts.check_ai_models
python -m scripts.exchange_sandbox_drill --i-understand-sandbox
gcloud run services describe "$CLOUD_RUN_SERVICE" --region "$GCP_REGION" --format='value(status.url)'
```
## Appendix B: sources checked for this guide
Google Cloud Run docs: health checks (startup/liveness probes and the `--startup-probe` syntax), `gcloud run deploy` and `services update` references
(`--no-traffic`, `--tag`, `update-traffic`), worker pools (always-on, no URL, no autoscaling). Binance docs for `GET /sapi/v1/account/apiRestrictions`. Google's model
deprecation notices for `gemini-2.5-*`. The repository's own scripts, `app/config.py` and `app/main.py` for every default and role-gating statement above.
