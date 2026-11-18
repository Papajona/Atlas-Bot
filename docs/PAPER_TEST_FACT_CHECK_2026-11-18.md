# Paper-test fact check — 18 November 2026

Scope: fact-check of the uploaded Atlas-Bot 3.10.47 patch and the local test report supplied with it. This does not supersede `deploy/SIGNOFF_MATRIX.md` and is not exact-SHA CI evidence. No local test execution or performance claim is asserted by this document.

**Evidence provenance:** the local counts below are supplied in the uploaded patch. They have not been independently reproduced against this branch. Treat them as a historical local snapshot, not as verification of this commit or a release sign-off. The branch's exact-head CI results are authoritative for the changes here.

## Supplied local measurements (Python 3.12, PostgreSQL 16; CI uses 3.13 / 15)

| Check | Result |
|---|---|
| `pytest -m "not postgres"` (SQLite) | 2589 passed, 0 failed, 0 skipped |
| `pytest -m postgres` | 31 passed, 0 failed, 0 skipped |
| `scripts/assert_no_skips.py` | Pass on both JUnit reports |
| `alembic upgrade head` / `alembic check` | Single head `0059`; no drift |
| pip check, compileall, pyflakes, bandit `-ll`, secrets scan, node syntax, shell syntax | Clean |
| `scripts/customer_paper_acceptance_drill.py` | `status: PASS` (`real_customer_authentication_tested: false`) |
| Paper-only ASGI app boot | `/livez` -> `mode: paper-only`; unauthenticated `/readyz` -> 401 |
| Defaults | Live, customer-live, payouts, TRON, TRON sweep, Deriv off; sandbox on |

Tests need fresh databases and the CI env (`ATLAS_PAPER_*` URLs, migrated primary schema, git repo, ruby). Missing those produces environment failures, not code failures.

## Changes

- `tests/test_customer_paper_flow.py`: failure stub now closes the coroutine it receives (removes a never-awaited coroutine warning; test-only).
- `.env.example`: comment-only paper-only profile note on `EXTERNAL_CUSTODY_SIGNER_REQUIRED` and main-app `/readyz`.

## Not verified here

Docker image builds, `pip-audit`, hash-locked install, any deployed staging revision, real customer authentication, and every open operational gate in the sign-off matrix (restore drill, alert delivery, OANDA dataset/carry evidence, exchange sandbox drills). Paper fills remain `INCOMPLETE_EXECUTION_MODEL`.

## Verdict

Internal paper drills (paper-only flags, virtual balances): supported by the supplied local evidence, subject to exact-head CI. Customer-facing paper: not ready per the supplied readiness review. Live money: NO-GO.
