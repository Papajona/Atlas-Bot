"""Regression coverage for guarded workflow fallback during Native AI migration."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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

    def test_config_migration_preserves_groq_research_fields_but_removes_strategy_provider_field(self):
        source = """from pydantic import BaseModel

class Settings(BaseModel):
    gemini_api_key: str = ""
    groq_api_key: str = "existing-key-reference"
    groq_model: str = "custom/research-model"
    groq_strategy_model: str = "legacy-strategy-model"
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "app"
            config_dir.mkdir()
            config = config_dir / "config.py"
            config.write_text(source, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                migration.transform_config()
            migrated = config.read_text(encoding="utf-8")

        self.assertNotIn("gemini_api_key", migrated)
        self.assertNotIn("groq_strategy_model", migrated)
        self.assertIn('groq_api_key: str = "existing-key-reference"', migrated)
        self.assertIn('groq_model: str = "custom/research-model"', migrated)
        self.assertIn("groq_max_completion_tokens: int = 2048", migrated)
        self.assertIn("groq_timeout_seconds: float = 20.0", migrated)
        self.assertIn("groq_research_review_enabled: bool = True", migrated)

    def test_groq_secret_version_validation_and_assignment_blocks_are_preserved(self):
        shell = """if [[ -n "$SECRET_GROQ_API_KEY_VERSION" && ! "$SECRET_GROQ_API_KEY_VERSION" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: SECRET_GROQ_API_KEY_VERSION must be a positive numeric Secret Manager version." >&2
  exit 2
fi
GROQ_SECRET_ARGS=""
if [[ -n "$SECRET_GROQ_API_KEY_VERSION" ]]; then
  GROQ_SECRET_ARGS=",GROQ_API_KEY=atlas-groq-api-key:${SECRET_GROQ_API_KEY_VERSION}"
fi
"""
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)
        self.assertEqual(cleaned, shell)
        self.assertIn("exit 2", cleaned)
        self.assertIn("GROQ_API_KEY=atlas-groq-api-key", cleaned)

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

    def test_groq_secret_and_ordinary_yaml_step_are_preserved(self):
        workflow = """name: optional Groq research
jobs:
  review:
    environment: paper
    steps:
      - name: optional research review
        env:
          GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
        run: echo "Groq research review is advisory"
"""
        cleaned = migration.scrub_workflow_ancillary(
            ".github/workflows/production-deploy.yml", workflow
        )
        self.assertEqual(cleaned, workflow)
        self.assertIn("GROQ_API_KEY", cleaned)
        self.assertIn("steps:", cleaned)

    def test_workflow_sequence_labels_and_commented_run_blocks_survive_cleanup(self):
        workflow = """name: paper deployment
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Remove Gemini fallback
        run: | # deployment script entry
          export GEMINI_API_KEY=retired
          echo deployment ready
"""
        cleaned = migration.scrub_workflow_ancillary(
            ".github/workflows/production-deploy.yml", workflow
        )
        self.assertIn("- name: Remove Gemini fallback", cleaned)
        self.assertNotIn("GEMINI_API_KEY", cleaned)
        self.assertIn("echo deployment ready", cleaned)
        self.assertFalse(migration._workflow_has_active_provider_wiring(cleaned))

    def test_groq_shell_parameter_guard_is_preserved_and_mixed_deploy_command_refused(self):
        guard = """if [[ -n "$GROQ_API_KEY" ]]; then
  : "${GROQ_API_KEY:?required}"
fi
"""
        self.assertEqual(
            migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", guard), guard
        )
        for unsafe in (
            'if [[ -n "$GROQ_API_KEY" ]]; then gcloud run deploy atlas; fi\n',
            'if [[ -n "$GROQ_API_KEY" ]]; then\n  GROQ_MODEL="openai/gpt-oss-20b"\nelse gcloud run deploy atlas\nfi\n',
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(migration.MigrationError):
                migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", unsafe)
    def test_optional_groq_secret_creation_block_is_preserved(self):
        shell = """if [[ -n "$GROQ_API_KEY" ]]; then
  create_secret atlas-groq-api-key "$GROQ_API_KEY"
fi
"""
        cleaned = migration.scrub_shell_deployment("deploy/gcp-bootstrap.sh", shell)
        self.assertEqual(cleaned, shell)
        self.assertIn('create_secret atlas-groq-api-key "$GROQ_API_KEY"', cleaned)

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


    def test_shared_secret_args_preserve_groq_and_remove_only_retired_entries(self):
        shell = """AI_SECRET_ARGS=""
AI_SECRET_ARGS="${AI_SECRET_ARGS},GEMINI_API_KEY=atlas-gemini-api-key:latest"
AI_SECRET_ARGS="${AI_SECRET_ARGS}${AI_SECRET_ARGS:+,}GROQ_API_KEY=atlas-groq-api-key:latest"
gcloud run services update atlas-api --set-secrets="DATABASE_URL=atlas-db:latest${AI_SECRET_ARGS:+,${AI_SECRET_ARGS}}"
"""
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)
        self.assertNotIn("GEMINI_API_KEY", cleaned)
        self.assertNotIn("atlas-gemini-api-key", cleaned)
        self.assertIn('AI_SECRET_ARGS="${AI_SECRET_ARGS}${AI_SECRET_ARGS:+,}GROQ_API_KEY=atlas-groq-api-key:latest"', cleaned)
        self.assertIn('--set-secrets="DATABASE_URL=atlas-db:latest${AI_SECRET_ARGS:+,${AI_SECRET_ARGS}}"', cleaned)



if __name__ == "__main__":
    unittest.main()
