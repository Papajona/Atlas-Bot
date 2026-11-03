"""Regression coverage for guarded workflow fallback during Native AI migration."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_legacy_native_ai.py"
SPEC = importlib.util.spec_from_file_location("migrate_legacy_native_ai_conflict_tests", MIGRATION_PATH)
assert SPEC and SPEC.loader
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class AncillaryWorkflowConflictGuardTests(unittest.TestCase):
    def test_groq_research_adapter_allows_reviewed_stdlib_decimal_imports(self):
        source = """from __future__ import annotations
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

async def groq_research_review(research, intelligence):
    value = Decimal("1.25")
    try:
        return {"value": str(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))}
    except InvalidOperation:
        return {"status": "unavailable"}
"""
        migration.validate_groq_research_module(source)

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

    def test_retired_provider_block_drops_only_noop_shared_secret_reassignment(self):
        shell = """AI_SECRET_ARGS=",GEMINI_API_KEY=atlas-gemini-api-key:${SECRET_GEMINI_API_KEY_VERSION}"
if [[ -n "$SECRET_CEREBRAS_API_KEY_VERSION" ]]; then
  AI_SECRET_ARGS="${AI_SECRET_ARGS},CEREBRAS_API_KEY=atlas-cerebras-api-key:${SECRET_CEREBRAS_API_KEY_VERSION}"
fi
gcloud run deploy atlas \
  --set-secrets "DATABASE_URL=atlas-database-url:3,SECRET_KEY=atlas-secret-key:4${AI_SECRET_ARGS}"
"""
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)
        self.assertIn("DATABASE_URL=atlas-database-url:3", cleaned)
        self.assertIn("SECRET_KEY=atlas-secret-key:4", cleaned)
        self.assertNotIn("GEMINI_API_KEY", cleaned)
        self.assertNotIn("CEREBRAS_API_KEY", cleaned)
        self.assertNotIn("SECRET_CEREBRAS_API_KEY_VERSION", cleaned)
        self.assertNotIn('AI_SECRET_ARGS="${AI_SECRET_ARGS}"', cleaned)

    def test_retired_provider_block_refuses_single_quoted_shared_secret_literal(self):
        token = "$" + "{AI_SECRET_ARGS}"
        shell = (
            'AI_SECRET_ARGS="DATABASE_URL=atlas-database-url:3"\n'
            'if [[ -n "$SECRET_CEREBRAS_API_KEY_VERSION" ]]; then\n'
            "  AI_SECRET_ARGS='" + token + "'\n"
            "fi\n"
            'gcloud run deploy atlas --set-secrets "${AI_SECRET_ARGS}"\n'
        )
        with self.assertRaisesRegex(
            migration.MigrationError,
            "single-quoted shared-secret literal inside a retired-provider conditional",
        ):
            migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)

    def test_groq_secret_inside_retired_provider_shared_list_is_not_hoisted(self):
        shell = """if [[ -n "$SECRET_CEREBRAS_API_KEY_VERSION" ]]; then
  AI_SECRET_ARGS="${AI_SECRET_ARGS},CEREBRAS_API_KEY=atlas-cerebras-api-key:${SECRET_CEREBRAS_API_KEY_VERSION},GROQ_API_KEY=atlas-groq-api-key:${SECRET_GROQ_API_KEY_VERSION}"
fi
"""
        with self.assertRaisesRegex(
            migration.MigrationError, "Groq secret assignment inside a retired-provider conditional"
        ):
            migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)


    def test_native_ai_guard_allows_supported_groq_trade_review_but_rejects_retired_wiring(self):
        supported_review = """def evaluate_native_trade_packet(packet):
    envelope = packet.get("groq_trade_review")
    if isinstance(envelope, dict) and envelope.get("provider") == "groq":
        groq_audit = {
            key: envelope[key]
            for key in ("status", "provider", "model", "review")
            if key in envelope
        }
        return {"providers": {"groq": groq_audit}, "execution_authority": False}
    return {"providers": {}, "execution_authority": False}
"""
        self.assertFalse(
            migration._has_conflicting_provider_wiring("app/native_ai.py", supported_review)
        )

        self.assertTrue(
            migration._has_conflicting_provider_wiring(
                "app/native_ai.py",
                'def old_provider(): return call_gemini(GEMINI_API_KEY)',
            )
        )
        self.assertTrue(
            migration._has_conflicting_provider_wiring(
                "app/native_ai.py",
                'GROQ_STRATEGY_MODEL = "legacy-model"',
            )
        )

        # Existing Native AI authority code may inspect the bounded advisory envelope,
        # but it must not instantiate a hosted Groq client or call its endpoint.
        for hosted_wiring in (
            "from groq import Groq\nclient = Groq(api_key=GROQ_API_KEY)",
            "import groq as hosted\nclient = hosted.Groq(api_key=SECRET_GROQ_API_KEY)",
            "from groq.asyncio import AsyncGroq\nclient = AsyncGroq(api_key=GROQ_API_KEY)",
            "GROQ_STRATEGY_MODEL = 'legacy-model'",
            "settings.groq_strategy_model = 'legacy-model'",
            "import httpx\nhttpx.post('https://api.groq.com/openai/v1/chat/completions')",
            "def review(): return _groq_json('prompt')",
        ):
            with self.subTest(hosted_wiring=hosted_wiring):
                self.assertTrue(
                    migration._has_conflicting_provider_wiring("app/native_ai.py", hosted_wiring)
                )
        # Other migration paths retain the broader provider detector.
        self.assertTrue(
            migration._has_conflicting_provider_wiring(
                "scripts/check_native_ai.py",
                'GROQ_API_KEY = "legacy-hosted-provider"',
            )
        )

    def test_approved_legacy_native_ai_baseline_is_replaced_with_pinned_canonical_veto(self):
        approved_legacy = """def evaluate_native_trade_packet(packet):
    return {"safe": True, "execution_authority": False}
"""
        canonical = """def evaluate_native_trade_packet(packet):
    reasons = []
    if packet.get("data_quality") != "OK":
        reasons.append("native_data_quality_invalid")
    if packet.get("protective_prices_valid") is not True:
        reasons.append("protective_price_order_invalid")
    groq = packet.get("groq_trade_review")
    if isinstance(groq, dict) and (
        groq.get("provider") != "groq" or groq.get("execution_authority") is not False
    ):
        reasons.append("groq_trade_review_invalid")
    return {"safe": not reasons, "reasons": reasons, "execution_authority": False}
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            (root / "app" / "native_ai.py").write_text(approved_legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                migration._install_canonical_native_ai(
                    canonical,
                    approved_legacy_source=approved_legacy,
                )
            installed = (root / "app" / "native_ai.py").read_text(encoding="utf-8")

        self.assertEqual(installed, canonical)
        self.assertIn("native_data_quality_invalid", installed)
        self.assertIn("protective_price_order_invalid", installed)
        self.assertIn("groq_trade_review_invalid", installed)
        self.assertNotIn('return {"safe": True', installed)

    def test_native_ai_installer_refuses_unreviewed_branch_local_safety_and_composer(self):
        branch_local = """class NativeAIError(RuntimeError):
    pass

AIProviderError = NativeAIError

def compose_strategy_spec(*, prompt, name, **kwargs):
    # This draft omits the production schema and must not be propagated.
    return {"title": name, "execution_mode": "PAPER_SHADOW_ONLY"}

def native_research_review(research, intelligence):
    return {"status": "LOCAL", "execution_authority": False}

def evaluate_native_trade_packet(packet):
    # This branch-local veto omits protective-price and advisory-evidence checks.
    return {"safe": packet.get("data_quality") == "OK", "execution_authority": False}

async def native_trade_safety_review(packet):
    return evaluate_native_trade_packet(packet)

def native_ai_health_check():
    return {"status": "PASS", "network_calls": 0}

async def dual_ai_research_review(research, intelligence):
    return native_research_review(research, intelligence)

async def dual_ai_trade_safety_review(packet):
    return await native_trade_safety_review(packet)
"""
        approved_legacy = """def evaluate_native_trade_packet(packet):
    return {"safe": True}
"""
        canonical = """def evaluate_native_trade_packet(packet):
    return {"safe": False}
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            target = root / "app" / "native_ai.py"
            target.write_text(branch_local, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                with self.assertRaisesRegex(
                    migration.MigrationError,
                    "differs from both reviewed Native AI baselines; preserving branch-specific safety logic",
                ):
                    migration._install_canonical_native_ai(
                        canonical,
                        approved_legacy_source=approved_legacy,
                    )
            installed = target.read_text(encoding="utf-8")
        self.assertEqual(installed, branch_local)

    def test_native_ai_installer_refuses_existing_hosted_groq_wiring(self):
        obsolete = """from groq import Groq
client = Groq(api_key=GROQ_API_KEY)
async def native_trade_safety_review(packet):
    return {"safe": True}
"""
        approved_legacy = """def evaluate_native_trade_packet(packet):
    return {"safe": True}
"""
        canonical = """def evaluate_native_trade_packet(packet):
    return {"safe": False}
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            target = root / "app" / "native_ai.py"
            target.write_text(obsolete, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                with self.assertRaisesRegex(migration.MigrationError, "retired or hosted provider wiring"):
                    migration._install_canonical_native_ai(
                        canonical,
                        approved_legacy_source=approved_legacy,
                    )
            installed = target.read_text(encoding="utf-8")
        self.assertEqual(installed, obsolete)

    def test_optional_groq_secret_creation_block_is_preserved(self):
        shell = """if [[ -n "$GROQ_API_KEY" ]]; then
  create_secret atlas-groq-api-key "$GROQ_API_KEY"
fi
"""
        cleaned = migration.scrub_shell_deployment("deploy/gcp-bootstrap.sh", shell)
        self.assertEqual(cleaned, shell)
        self.assertIn('create_secret atlas-groq-api-key "$GROQ_API_KEY"', cleaned)

    def test_optional_groq_secret_access_grants_are_preserved(self):
        shell = """if [[ -n "$GROQ_API_KEY" ]]; then grant_secret_access atlas-groq-api-key "$SERVICE_ACCOUNT"; grant_secret_access atlas-groq-api-key "$WORKER_SERVICE_ACCOUNT"; fi
"""
        cleaned = migration.scrub_shell_deployment("deploy/gcp-bootstrap.sh", shell)
        self.assertEqual(cleaned, shell)
        self.assertIn('grant_secret_access atlas-groq-api-key "$SERVICE_ACCOUNT"', cleaned)
        self.assertIn('grant_secret_access atlas-groq-api-key "$WORKER_SERVICE_ACCOUNT"', cleaned)

    def test_groq_secret_access_grant_to_unapproved_identity_is_refused(self):
        shell = """if [[ -n "$GROQ_API_KEY" ]]; then
  grant_secret_access atlas-groq-api-key "$PROJECT_NUMBER"
fi
"""
        with self.assertRaisesRegex(
            migration.MigrationError, "mixed provider-conditioned shell block"
        ):
            migration.scrub_shell_deployment("deploy/gcp-bootstrap.sh", shell)

    def test_shell_refusal_redacts_the_exact_unrecognized_provider_line(self):
        shell = 'printf -v deployment_status "Gemini provider not configured"\n'
        with self.assertRaisesRegex(
            migration.MigrationError,
            "provider text in a non-provider shell statement; refusing lossy deletion",
        ) as raised:
            migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)
        self.assertNotIn("printf -v deployment_status", str(raised.exception))
        self.assertNotIn("Gemini provider not configured", str(raised.exception))

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

    def test_quoted_provider_key_is_removed_without_leaving_invalid_yaml(self):
        workflow = """name: legacy deployment
jobs:
  deploy:
    steps:
      - name: deploy
        env:
          "GEMINI_API_KEY": ${{ secrets.GEMINI_API_KEY }}
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: echo deploy
"""
        cleaned = migration.scrub_workflow_ancillary(
            ".github/workflows/production-deploy.yml", workflow
        )
        self.assertNotIn("GEMINI_API_KEY", cleaned)
        self.assertIn("DATABASE_URL:", cleaned)
        self.assertIn("run: echo deploy", cleaned)
        self.assertFalse(migration._workflow_has_active_provider_wiring(cleaned))

    def test_workflow_shell_error_names_the_workflow_file(self):
        workflow = """name: database deployment
jobs:
  deploy:
    steps:
      - name: migrate
        run: curl https://api.openai.com/v1 && python -m alembic upgrade head
"""
        with self.assertRaisesRegex(
            migration.MigrationError, r"\.github/workflows/production-deploy\.yml.*mixed shell command"
        ) as caught:
            migration.scrub_workflow_ancillary(
                ".github/workflows/production-deploy.yml", workflow
            )
        self.assertNotIn("deploy/cloud-run-deploy.sh", str(caught.exception))

    def test_config_groq_fields_follow_two_space_settings_indentation(self):
        source = """from pydantic import BaseModel

class Settings(BaseModel):
  model_config = dict(
    env_file=".env",
  )
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            app_dir.mkdir()
            config = app_dir / "config.py"
            config.write_text(source, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                migration.transform_config()
            result = config.read_text(encoding="utf-8")
        self.assertIn("  )\n  groq_api_key: str = \"\"", result)
        self.assertNotIn("    groq_api_key: str = \"\"", result)
        import ast
        ast.parse(result, filename="app/config.py")
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
        self.assertEqual(
            migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", cleaned),
            cleaned,
        )


    def test_inline_groq_assignment_and_else_header_is_preserved(self):
        shell = """SECRET_GROQ_REF="${SECRET_GROQ_REF:-}"
GROQ_SECRET_ARG=""
if [[ -n "$SECRET_GROQ_REF" ]]; then GROQ_SECRET_ARG=",GROQ_API_KEY=${SECRET_GROQ_REF}"; else
  echo "WARNING: optional Groq research review is not configured." >&2
fi
gcloud run worker-pools deploy atlas --set-secrets="DATABASE_URL=atlas-db:latest${GROQ_SECRET_ARG}"
"""
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-worker-deploy.sh", shell)
        self.assertEqual(cleaned, shell)
        self.assertIn('GROQ_SECRET_ARG=",GROQ_API_KEY=${SECRET_GROQ_REF}"', cleaned)

    def test_inline_groq_assignment_header_rejects_command_substitution(self):
        shell = """if [[ -n "$SECRET_GROQ_REF" ]]; then GROQ_SECRET_ARG="${$(touch /tmp/atlas-migration-sentinel)}"; else
  echo "optional research review is disabled"
fi
"""
        with self.assertRaisesRegex(
            migration.MigrationError, "executable command on a Groq if-header"
        ):
            migration.scrub_shell_deployment("deploy/cloud-run-worker-deploy.sh", shell)



    def test_groq_initial_if_condition_rejects_command_substitution(self):
        shell = """if [[ -n "$SECRET_GROQ_REF" && $(touch /tmp/atlas-migration-sentinel) ]]; then
  GROQ_SECRET_ARG=",GROQ_API_KEY=${SECRET_GROQ_REF}"
else
  echo "optional Groq research review is disabled"
fi
"""
        with self.assertRaisesRegex(
            migration.MigrationError, "unsafe Groq if-condition"
        ):
            migration.scrub_shell_deployment("deploy/cloud-run-worker-deploy.sh", shell)

    def test_groq_elif_condition_rejects_command_substitution(self):
        shell = """if [[ -n "$SECRET_GROQ_REF" ]]; then
  GROQ_SECRET_ARG=",GROQ_API_KEY=${SECRET_GROQ_REF}"
elif [[ $(touch /tmp/atlas-migration-sentinel) ]]; then
  GROQ_SECRET_ARG=""
else
  echo "optional Groq research review is disabled"
fi
"""
        with self.assertRaisesRegex(
            migration.MigrationError, "mixed provider-conditioned shell block"
        ):
            migration.scrub_shell_deployment("deploy/cloud-run-worker-deploy.sh", shell)

    def test_groq_inline_assignment_and_else_header_remains_supported(self):
        shell = """if [[ -n "$SECRET_GROQ_REF" ]]; then GROQ_SECRET_ARG=",GROQ_API_KEY=${SECRET_GROQ_REF}"; else
  echo "optional Groq research review is disabled"
fi
"""
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-worker-deploy.sh", shell)
        self.assertEqual(cleaned, shell)

    def test_groq_bracket_condition_rejects_unquoted_command_separator(self):
        shell = """if [ -n "$GROQ_API_KEY" ; touch /tmp/atlas-migration-sentinel ]; then
  GROQ_SECRET_ARG=""
else
  echo "optional Groq research review is disabled"
fi
"""
        with self.assertRaisesRegex(
            migration.MigrationError, "unsafe Groq if-condition"
        ):
            migration.scrub_shell_deployment("deploy/cloud-run-worker-deploy.sh", shell)

    def test_groq_then_on_next_line_is_preserved(self):
        shell = """if [[ -n "$SECRET_GROQ_REF" ]]
then
  GROQ_SECRET_ARG=",GROQ_API_KEY=atlas-groq-api-key:1"
else
  echo "optional Groq research review is disabled"
fi
"""
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-worker-deploy.sh", shell)
        self.assertEqual(cleaned, shell)


    def test_optional_groq_warning_keeps_the_guard_shell_valid(self):
        shell = """SECRET_GROQ_API_KEY_VERSION=\"${SECRET_GROQ_API_KEY_VERSION:-}\"
AI_SECRET_ARGS=\",GEMINI_API_KEY=atlas-gemini-api-key:${SECRET_GEMINI_API_KEY_VERSION}\"
if [[ -z \"$SECRET_GROQ_API_KEY_VERSION\" ]]; then
  echo \"WARNING: no Groq key. The AI safety veto requires BOTH Gemini and Groq to approve (fail-closed), so customer bot starts via the API will return NO_TRADE until GROQ is configured.\" >&2
fi
if [[ -n \"$SECRET_GROQ_API_KEY_VERSION\" ]]; then AI_SECRET_ARGS=\"${AI_SECRET_ARGS},GROQ_API_KEY=atlas-groq-api-key:${SECRET_GROQ_API_KEY_VERSION}\"; fi
"""
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)
        self.assertNotIn("Gemini", cleaned)
        self.assertIn(
            "Groq remains optional research-only support; Atlas Native AI continues locally.",
            cleaned,
        )
        self.assertIn('if [[ -z "$SECRET_GROQ_API_KEY_VERSION" ]]; then', cleaned)
        self.assertIn('if [[ -n "$SECRET_GROQ_API_KEY_VERSION" ]]; then', cleaned)
        result = subprocess.run(
            ["bash", "-n"], input=cleaned, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_retired_cerebras_secret_version_assignment_is_removed_but_groq_is_kept(self):
        shell = """SECRET_CEREBRAS_API_KEY_VERSION=\"${SECRET_CEREBRAS_API_KEY_VERSION:-}\"
SECRET_GROQ_API_KEY_VERSION=\"${SECRET_GROQ_API_KEY_VERSION:-}\"
"""
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)
        self.assertNotIn("CEREBRAS", cleaned)
        self.assertIn('SECRET_GROQ_API_KEY_VERSION="${SECRET_GROQ_API_KEY_VERSION:-}"', cleaned)

    def test_compound_retired_provider_assignment_cannot_erase_deploy_command(self):
        shell = "OPENAI_API_KEY=legacy; gcloud run deploy atlas-trading-api\n"
        with self.assertRaisesRegex(
            migration.MigrationError, "compound retired-provider assignment|mixed shell command"
        ):
            migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)

    def test_configured_groq_message_is_not_rewritten_as_missing_key_warning(self):
        shell = 'echo "Optional Groq review configured; Gemini removed"\n'
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)
        self.assertIn(
            'Groq remains optional research-only support; Atlas Native AI continues locally.',
            cleaned,
        )
        self.assertNotIn("not configured", cleaned)

    def test_empty_and_commented_retired_assignments_are_removed(self):
        shell = """OPENAI_API_KEY=
OPENAI_API_KEY=legacy # historical value
SECRET_CEREBRAS_API_KEY_VERSION=
"""
        cleaned = migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)
        self.assertNotIn("OPENAI_API_KEY", cleaned)
        self.assertNotIn("SECRET_CEREBRAS_API_KEY_VERSION", cleaned)

    def test_provider_assignment_command_substitution_is_refused(self):
        unsafe = (
            'SECRET_CEREBRAS_API_KEY="$(touch /tmp/atlas-secret-side-effect)"\n',
            'if [[ -n "$SECRET_CEREBRAS_API_KEY_VERSION" ]]; then\n'
            '  SECRET_CEREBRAS_API_KEY="$(touch /tmp/atlas-secret-side-effect)"\n'
            'fi\n',
            'if [[ -n "$SECRET_CEREBRAS_API_KEY_VERSION" ]]; then '
            'SECRET_CEREBRAS_API_KEY="$(touch /tmp/atlas-secret-side-effect)"; fi\n',
        )
        for shell in unsafe:
            with self.subTest(shell=shell), self.assertRaises(migration.MigrationError):
                migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)

    def test_secret_creation_command_with_trailing_deploy_command_is_refused(self):
        shell = 'create_secret atlas-gemini-api-key "$GEMINI_API_KEY"; gcloud run deploy atlas\n'
        with self.assertRaises(migration.MigrationError):
            migration.scrub_shell_deployment("deploy/gcp-bootstrap.sh", shell)

    def test_unquoted_retired_provider_parameter_guard_is_removed(self):
        unquoted = ": ${SECRET_GEMINI_API_KEY_VERSION:?Set SECRET_GEMINI_API_KEY_VERSION}\n"
        quoted = ': "${SECRET_CEREBRAS_API_KEY_VERSION:?Set SECRET_CEREBRAS_API_KEY_VERSION}"\n'
        self.assertEqual(migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", unquoted), "")
        self.assertEqual(migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", quoted), "")
        unsafe = ": ${SECRET_GEMINI_API_KEY_VERSION:?Set $(touch /tmp/atlas-migration-sentinel)}\n"
        with self.assertRaisesRegex(migration.MigrationError, "retains provider text"):
            migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", unsafe)

    def test_inline_retired_provider_noop_guard_is_removed(self):
        shell = 'if [[ -n "$SECRET_OPENAI_API_KEY" ]]; then :; fi\n'
        self.assertEqual(migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell), "")

    def test_groq_secret_inside_retired_provider_block_is_not_made_unconditional(self):
        shell = """if [[ -n "$SECRET_CEREBRAS_API_KEY_VERSION" ]]; then
  AI_SECRET_ARGS="${AI_SECRET_ARGS},GROQ_API_KEY=atlas-groq-api-key:1"
fi
"""
        with self.assertRaisesRegex(
            migration.MigrationError, "Groq secret assignment inside a retired-provider conditional"
        ):
            migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)

    def test_secret_creation_and_grant_commands_allow_trailing_comments(self):
        shell = """create_secret atlas-gemini-api-key "$GEMINI_API_KEY" # retired provider secret
grant_secret_access atlas-gemini-api-key "$SERVICE_ACCOUNT" # retired provider access
"""
        self.assertEqual(migration.scrub_shell_deployment("deploy/gcp-bootstrap.sh", shell), "")

    def test_parameter_guard_allows_parentheses_but_rejects_command_substitution(self):
        valid = ': "${SECRET_GEMINI_API_KEY_VERSION:?Set (or verify) the key}"\n'
        self.assertEqual(migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", valid), "")
        unsafe = ': "${SECRET_GEMINI_API_KEY_VERSION:?Set $(touch /tmp/atlas-migration-sentinel)}"\n'
        with self.assertRaisesRegex(migration.MigrationError, "retains provider text"):
            migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", unsafe)

    def test_inline_retired_provider_parameter_guard_is_removed(self):
        shell = 'if [[ -n "$SECRET_OPENAI_API_KEY" ]]; then : "${SECRET_OPENAI_API_KEY:?required}"; fi\n'
        self.assertEqual(migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell), "")
    def test_retired_assignment_rejects_quotes_and_escaped_comment_commands(self):
        unsafe = (
            'SECRET_GEMINI_KEY=a"b #"; gcloud run deploy atlas\n',
            "SECRET_GEMINI_KEY=a\\ #; gcloud run deploy atlas\n",
        )
        for shell in unsafe:
            with self.subTest(shell=shell), self.assertRaises(migration.MigrationError):
                migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", shell)

    def test_retired_secret_command_rejects_quotes_and_parameter_side_effects(self):
        unsafe = (
            'create_secret atlas-gemini-api-key a"b #"; gcloud run deploy atlas\n',
            'create_secret atlas-gemini-api-key "${REQUIRED:?message}"\n',
            'create_secret atlas-gemini-api-key "${OTHER:=value}"\n',
        )
        for shell in unsafe:
            with self.subTest(shell=shell), self.assertRaises(migration.MigrationError):
                migration.scrub_shell_deployment("deploy/gcp-bootstrap.sh", shell)

if __name__ == "__main__":
    unittest.main()
