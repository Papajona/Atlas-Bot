"""Regression coverage for guarded workflow fallback during Native AI migration."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_legacy_native_ai.py"
SPEC = importlib.util.spec_from_file_location("migrate_legacy_native_ai_conflict_tests", MIGRATION_PATH)
assert SPEC and SPEC.loader
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class AncillaryWorkflowConflictGuardTests(unittest.TestCase):
    def test_retired_gemini_deployment_environment_is_refused_not_renamed(self):
        workflow = """name: paper deployment
jobs:
  deploy:
    environment: production-gemini
    steps:
      - name: deploy
        run: echo deploy
"""
        with self.assertRaisesRegex(migration.MigrationError, "deployment environment"):
            migration.scrub_workflow_ancillary(
                ".github/workflows/production-deploy.yml", workflow
            )

    def test_supported_groq_deployment_setting_is_preserved(self):
        shell = """#!/usr/bin/env bash
GROQ_API_KEY="${GROQ_API_KEY:-}"
GROQ_MODEL="${GROQ_MODEL:-openai/gpt-oss-20b}"
echo "Optional Groq research review only"
"""
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)
        self.assertEqual(cleaned, shell)
        self.assertIn("GROQ_API_KEY", cleaned)
        self.assertIn("GROQ_MODEL", cleaned)

    def test_mixed_provider_url_and_database_migration_command_fails_closed(self):
        workflow = """name: database deployment
jobs:
  deploy:
    steps:
      - name: migrate
        run: curl https://api.openai.com/v1 && python -m alembic upgrade head
"""
        with self.assertRaisesRegex(
            migration.MigrationError, "mixed shell command"
        ):
            migration.scrub_workflow_ancillary(
                ".github/workflows/production-deploy.yml", workflow
            )

    def test_provider_named_job_mapping_with_children_fails_closed(self):
        workflow = """name: legacy workflow
jobs:
  gemini:
    runs-on: ubuntu-latest
    steps:
      - run: echo no-op
"""
        with self.assertRaisesRegex(
            migration.MigrationError, "provider-named YAML mapping"
        ):
            migration.scrub_workflow_ancillary(
                ".github/workflows/production-deploy.yml", workflow
            )

    def test_groq_secret_metadata_is_not_classified_as_retired_provider_wiring(self):
        metadata = """{
  "env": [{
    "name": "GROQ_API_KEY",
    "secretRef": "atlas-groq-api-key",
    "version": "latest"
  }]
}
"""
        self.assertFalse(migration._metadata_has_active_provider_wiring(metadata))


if __name__ == "__main__":
    unittest.main()
