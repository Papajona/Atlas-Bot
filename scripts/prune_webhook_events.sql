\set ON_ERROR_STOP on
-- Privileged maintenance job for public.webhook_events (inbound customer webhook payloads).
--
-- Run as a separate maintenance identity (for example Cloud Scheduler or pg_cron).
-- Do NOT grant DELETE to atlas_api_runtime: scripts/provision_atlas_api_runtime.sql
-- deliberately withholds DELETE on every public table and refuses to commit if the role has it.
-- The application only inserts webhook_events and never reads the retained payloads back.
--
-- Default is a DRY RUN: count expired rows only.
--   psql "$MAINTENANCE_DATABASE_URL" -f scripts/prune_webhook_events.sql
--
-- Delete one bounded batch; rerun until expired_events is zero:
--   psql "$MAINTENANCE_DATABASE_URL" -v apply=1 -v retention_days=30 -v batch_size=5000 \
--       -f scripts/prune_webhook_events.sql
--
-- The 30-day default is a placeholder. Set retention_days to the approved policy.
-- If row-level security is enabled, the maintenance identity must own the table or bypass RLS.
-- Requires psql 13+.

\if :{?retention_days}
\else
\set retention_days 30
\endif
\if :{?batch_size}
\else
\set batch_size 5000
\endif
\if :{?apply}
\else
\set apply 0
\endif

SELECT count(*) AS expired_events
FROM public.webhook_events
WHERE created_at < now() - make_interval(days => :'retention_days'::integer);

\if :apply
BEGIN;
WITH doomed AS (
    SELECT id
    FROM public.webhook_events
    WHERE created_at < now() - make_interval(days => :'retention_days'::integer)
    ORDER BY id
    LIMIT :'batch_size'::integer
    FOR UPDATE SKIP LOCKED
)
DELETE FROM public.webhook_events AS w
USING doomed AS d
WHERE w.id = d.id;
COMMIT;
\endif
