"""Regression coverage for guarded workflow fallback during Native AI migration."""
from __future__ import annotations

import ast
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


    def test_shell_list_removal_handles_first_entry_without_leading_commas(self):
        shell = """#!/usr/bin/env bash
ENV_VARS="^@^GEMINI_MODEL=gemini-3.5-flash@DATABASE_URL=postgres://example/db"
gcloud run deploy atlas --set-env-vars "GEMINI_MODEL=gemini-3.5-flash,DATABASE_URL=postgres://example/db" --set-secrets "GEMINI_API_KEY=atlas-gemini-api-key:3,DATABASE_URL=atlas-database-url:3"
"""
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)
        self.assertIn('ENV_VARS="^@^DATABASE_URL=postgres://example/db"', cleaned)
        self.assertIn('--set-env-vars "DATABASE_URL=postgres://example/db"', cleaned)
        self.assertIn('--set-secrets "DATABASE_URL=atlas-database-url:3"', cleaned)
        self.assertNotIn('",DATABASE_URL=', cleaned)
        self.assertNotIn("GEMINI_MODEL", cleaned)
        self.assertNotIn("GEMINI_API_KEY", cleaned)

    def test_supported_groq_binding_in_ai_secret_args_is_preserved(self):
        shell = """#!/usr/bin/env bash
AI_SECRET_ARGS=",GROQ_API_KEY=atlas-groq-api-key:\${GROQ_SECRET_VERSION}"
gcloud run deploy atlas --set-secrets "DATABASE_URL=atlas-database-url:3\${AI_SECRET_ARGS}"
"""
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)
        self.assertIn("AI_SECRET_ARGS", cleaned)
        self.assertIn("GROQ_API_KEY=atlas-groq-api-key", cleaned)
        self.assertIn("\${AI_SECRET_ARGS}", cleaned)

    def test_mixed_groq_and_retired_provider_ai_secret_args_fail_closed(self):
        shell = """#!/usr/bin/env bash
AI_SECRET_ARGS=",GEMINI_API_KEY=atlas-gemini-api-key:3,GROQ_API_KEY=atlas-groq-api-key:3"
"""
        with self.assertRaisesRegex(migration.MigrationError, "mixes supported Groq and retired provider"):
            migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)

    def test_groq_condition_cannot_carry_a_deploy_command_after_then(self):
        shell = """if [[ -n "$GROQ_API_KEY" ]]; then gcloud run deploy atlas
fi
"""
        with self.assertRaisesRegex(migration.MigrationError, "executable command on a Groq-conditioned if line"):
            migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)

    def test_sequence_run_with_indicator_comment_is_scrubbed_and_surrounding_comments_survive(self):
        workflow = """name: deployed job
# keep this review comment
jobs:
  deploy:
    environment: paper
    runs-on: ubuntu-latest
    steps:
      - run: | # shell entry point
          gcloud run deploy atlas --set-env-vars "GEMINI_MODEL=gemini-3.5-flash,DATABASE_URL=postgres://example/db"
# preserve final note
"""
        cleaned = migration.scrub_workflow_ancillary(".github/workflows/production-deploy.yml", workflow)
        self.assertIn("# keep this review comment", cleaned)
        self.assertIn("# preserve final note", cleaned)
        self.assertIn("- run: | # shell entry point", cleaned)
        self.assertNotIn("GEMINI_MODEL", cleaned)
        self.assertIn('DATABASE_URL=postgres://example/db', cleaned)

    def test_supported_workflow_label_is_not_active_provider_wiring(self):
        workflow = """name: Legacy Gemini migration note
run-name: Old OpenAI build
description: Previous Cerebras backend
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: deploy
        run: echo "paper only"
"""
        cleaned = migration.scrub_workflow_ancillary(".github/workflows/production-deploy.yml", workflow)
        self.assertEqual(cleaned, workflow)
        self.assertFalse(migration._workflow_has_active_provider_wiring(cleaned))

    def test_config_migration_rejects_supported_groq_assignment_outside_settings(self):
        source = """from pydantic import BaseModel

groq_api_key = "external"

class Settings(BaseModel):
    gemini_api_key: str = ""
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            path = root / "app" / "config.py"
            path.write_text(source, encoding="utf-8")
            with patch.object(migration, "ROOT", root), self.assertRaisesRegex(
                migration.MigrationError, "provider-named assignment outside the Settings class"
            ):
                migration.transform_config()

    def test_daily_research_persistence_projection_is_updated_on_idempotent_migration(self):
        source = 'review_json=json.dumps({k: review.get(k) for k in ("ai_brain_mode", "paper_candidates", "native_ai_review")})\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            path = root / "app" / "daily_research.py"
            path.write_text(source, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                migration.transform_daily_research()
            migrated = path.read_text(encoding="utf-8")
        self.assertIn('"groq_research_review"', migrated)
        self.assertIn('"native_ai_review", "groq_research_review"', migrated)


    def test_config_groq_fields_are_inserted_after_settings_body_when_class_has_no_methods(self):
        source = """from pydantic import BaseModel

class Settings(BaseModel):
    gemini_api_key: str = ""
    timeout: int = 30
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            path = root / "app" / "config.py"
            path.write_text(source, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                migration.transform_config()
            migrated = path.read_text(encoding="utf-8")
        self.assertLess(migrated.index("timeout:"), migrated.index("groq_api_key:"))
        self.assertIn("groq_research_review_enabled: bool = True", migrated)
        ast.parse(migrated)
