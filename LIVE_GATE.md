
## Staging identity preflight

A protected, manual, read-only GitHub Actions workflow now checks the explicitly configured Supabase staging project reference, retrieves its database URL from Google Cloud Secret Manager without logging credentials, and compares the actual database/current role/migration revision/RLS inventory with protected staging configuration. It is fail-closed and deliberately does not run migrations. Configure the `staging` GitHub environment variables `ATLAS_STAGING_SUPABASE_PROJECT_REF`, `ATLAS_STAGING_DATABASE_SECRET`, and `ATLAS_STAGING_RUNTIME_DB_ROLE`, plus `GCP_STAGING_PREFLIGHT_SERVICE_ACCOUNT` and existing Google Cloud workload identity/project secrets, before dispatching it.

The currently connected Supabase management project is not automatically treated as staging. No database writes or migrations were performed by this change.
