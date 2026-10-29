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
    def test_protected_deployment_environment_name_is_preserved_exactly(self):
        workflow = """name: paper deployment
jobs:
  deploy:
    environment: production-gemini
    steps:
      - name: deploy
        run: echo deploy
"""
        cleaned = migration.scrub_workflow_ancillary(
            ".github/workflows/production-deploy.yml", workflow
        )
        self.assertEqual(cleaned, workflow)
        self.assertIn("environment: production-gemini\n", cleaned)

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


if __name__ == "__main__":
    unittest.main()
