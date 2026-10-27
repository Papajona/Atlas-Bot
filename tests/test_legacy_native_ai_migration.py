from __future__ import annotations

import ast
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_legacy_native_ai.py"
SPEC = importlib.util.spec_from_file_location("migrate_legacy_native_ai", MIGRATION_PATH)
assert SPEC and SPEC.loader
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


CANONICAL_STRATEGY = """from .native_ai import NativeAIError, compose_strategy_spec
from .strategy_evidence import evidence_for

async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):
    try:
        raw = compose_strategy_spec(prompt=prompt, name=name, asset=asset, symbol=symbol,
                                   timeframe=timeframe, max_risk_per_trade=0.01)
    except Exception as exc:
        raise NativeAIError(str(exc)) from exc
    return {"strategy": raw, "evidence": evidence_for("trend"),
            "ai": {"provider": "atlas_native", "model": "atlas-native-strategy-v1",
                   "network_calls": 0}}
"""


class LegacyNativeAIMigrationTests(unittest.TestCase):
    def test_ai_provider_error_stub_is_replaced_by_native_compatibility_alias(self):
        legacy = """class AIProviderError(RuntimeError):
    pass

async def _gemini_json(prompt):
    return {"prompt": prompt}

def retained_helper():
    raise AIProviderError('invalid input')
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value="from .native_ai import AIProviderError\n\n"
            ):
                migration.transform_ai_providers("canonical-commit")
            result = target.read_text(encoding="utf-8")
            self.assertNotIn("class AIProviderError", result)
            self.assertIn("from .native_ai import AIProviderError", result)
            self.assertIn("def retained_helper", result)
            self.assertIn("raise AIProviderError('invalid input')", result)
            self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_ai_provider_error_stub_is_replaced_when_facade_has_no_provider_text(self):
        legacy = """from .native_ai import NativeAIError

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    try:
        raise NativeAIError("failure")
    except AIProviderError:
        return "handled"
"""
        self.assertIsNone(migration.PROVIDER_RE.search(legacy))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value="from .native_ai import AIProviderError\n\n"
            ):
                migration.transform_ai_providers("canonical-commit")
            result = target.read_text(encoding="utf-8")
            self.assertNotIn("class AIProviderError", result)
            self.assertIn("from .native_ai import AIProviderError", result)
            self.assertIn("from .native_ai import NativeAIError", result)
            self.assertIn("except AIProviderError", result)
            self.assertNotRegex(result, migration.PROVIDER_RE)


    def test_retained_import_that_changes_canonical_exception_binding_fails_closed(self):
        legacy = """from .branch_errors import NativeAIError

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    try:
        raise NativeAIError("failure")
    except AIProviderError:
        return "handled"
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration,
                "canonical_text",
                return_value="from .native_ai import AIProviderError, NativeAIError\\n",
            ):
                with self.assertRaisesRegex(migration.MigrationError, "canonical name .* differently"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_star_import_dependency_is_preserved_before_canonical_facade(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            canonical = (Path(__file__).resolve().parents[1] / "app" / "ai_providers.py").read_text(
                encoding="utf-8"
            )
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                migration.transform_ai_providers("canonical-commit")
            result = target.read_text(encoding="utf-8")
            ast.parse(result)
            self.assertIn("from .branch_helpers import *", result)
            self.assertLess(
                result.index("from .branch_helpers import *"),
                result.index("from .native_ai import AIProviderError"),
            )
            self.assertLess(
                result.index("from __future__ import annotations"),
                result.index("from .branch_helpers import *"),
            )
            self.assertIn("return branch_helper()", result)
            self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_chained_assignment_cannot_overwrite_canonical_facade_name(self):
        legacy = """class AIProviderError(RuntimeError):
    pass

custom = json = object()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration,
                "canonical_text",
                return_value="import json\\nfrom .native_ai import AIProviderError\\n",
            ):
                with self.assertRaisesRegex(migration.MigrationError, "canonical name"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_ai_provider_error_collision_with_custom_behavior_fails_closed(self):
        legacy = """class AIProviderError(RuntimeError):
    def __init__(self, message):
        self.message = message

async def _gemini_json(prompt):
    return {"prompt": prompt}
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value="from .native_ai import AIProviderError\n\n"
            ):
                with self.assertRaisesRegex(migration.MigrationError, "collides with a canonical name"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)


    def test_config_refuses_mixed_provider_and_unrelated_same_line_assignments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "app" / "config.py"
            config.parent.mkdir()
            config.write_text(
                "class Settings:\n"
                "    gemini_api_key: str = ''; withdrawals_enabled: bool = True\n",
                encoding="utf-8",
            )
            with patch.object(migration, "ROOT", root):
                with self.assertRaisesRegex(migration.MigrationError, "physical line with a non-provider statement"):
                    migration.transform_config()
            result = config.read_text(encoding="utf-8")
            self.assertIn("withdrawals_enabled", result)
            self.assertIn("gemini_api_key", result)

    def test_config_refuses_provider_assignment_outside_settings_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "app" / "config.py"
            config.parent.mkdir()
            source = (
                "class Settings:\n"
                "    withdrawals_enabled: bool = True\n"
                "def initialize_provider():\n"
                "    gemini_api_key = open_database_and_register_default()\n"
            )
            config.write_text(source, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                with self.assertRaisesRegex(migration.MigrationError, "outside the Settings class"):
                    migration.transform_config()
            self.assertEqual(config.read_text(encoding="utf-8"), source)

    def test_config_refuses_provider_field_sharing_line_with_setup_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "app" / "config.py"
            config.parent.mkdir()
            source = (
                "class Settings:\n"
                "    gemini_api_key: str = None; record_config_load()\n"
            )
            config.write_text(source, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                with self.assertRaisesRegex(migration.MigrationError, "physical line with a non-provider statement"):
                    migration.transform_config()
            self.assertEqual(config.read_text(encoding="utf-8"), source)

    def test_daily_research_refuses_to_replace_early_return(self):
        legacy = (
            "from .ai_providers import dual_ai_research_review\n"
            "async def run_daily_research():\n"
            "    if settings.gemini_api_key:\n"
            "        return await dual_ai_research_review(research, intelligence)\n"
            "    return {'status': 'NOT_CONFIGURED'}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "daily_research.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                with self.assertRaisesRegex(migration.MigrationError, "contains unrelated or unknown logic"):
                    migration.transform_daily_research()
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_daily_research_refuses_unrelated_not_configured_fallback_data(self):
        legacy = (
            "from .ai_providers import dual_ai_research_review\n"
            "async def run_daily_research():\n"
            "    review = {}\n"
            "    if settings.gemini_api_key:\n"
            "        review['external_ai'] = await dual_ai_research_review(research, intelligence)\n"
            "    else:\n"
            "        review['external_ai'] = {'status': 'NOT_CONFIGURED'}\n"
            "        review['audit_status'] = {'status': 'NOT_CONFIGURED'}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "daily_research.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                with self.assertRaisesRegex(migration.MigrationError, "contains unrelated or unknown logic"):
                    migration.transform_daily_research()
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_config_removes_provider_fields_but_preserves_unrelated_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "app" / "config.py"
            config.parent.mkdir()
            config.write_text(
                "class Settings:\n"
                "    database_url: str = ''\n"
                "    gemini_api_key: str = ''\n"
                "    gemini_model: str = 'gemini-3.5-flash'\n"
                "    groq_api_key: str = ''\n"
                "    withdrawals_enabled: bool = True\n",
                encoding="utf-8",
            )
            with patch.object(migration, "ROOT", root):
                migration.transform_config()
            result = config.read_text(encoding="utf-8")
            self.assertIn("database_url", result)
            self.assertIn("withdrawals_enabled", result)
            self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_config_refuses_a_provider_field_sharing_a_line_with_an_unrelated_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "app" / "config.py"
            config.parent.mkdir()
            mixed = (
                "class Settings:\n"
                "    database_url: str = 'sqlite:///data.db'; gemini_api_key: str = ''\n"
            )
            config.write_text(mixed, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                with self.assertRaisesRegex(migration.MigrationError, "sharing a physical line"):
                    migration.transform_config()
            self.assertEqual(config.read_text(encoding="utf-8"), mixed)

    def test_strategy_migration_refuses_to_drop_provider_helper_behavior(self):
        legacy = (
            "from .ai_providers import _gemini_json\n"
            "SYSTEM_PROMPT = 'Gemini prompt'\n"
            "async def _generate_with_provider(provider, prompt):\n"
            "    validate_strategy_prompt(prompt)\n"
            "    return await _gemini_json(prompt)\n\n"
            "async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):\n"
            "    return await _generate_with_provider('gemini', prompt)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "strategy_ai.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=CANONICAL_STRATEGY
            ):
                with self.assertRaisesRegex(migration.MigrationError, "_generate_with_provider contains additional"):
                    migration.transform_strategy_ai("fake-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_strategy_rewrites_provider_generator_and_preserves_schema_context(self):
        legacy = (
            "from .config import settings\n"
            "from .ai_providers import AIProviderError, _gemini_json, _groq_json\n"
            "from .strategy_evidence import evidence_for\n\n"
            "class AIStrategySpec:\n"
            "    pass\n\n"
            "SYSTEM_PROMPT = '''Gemini strategy prompt'''\n\n"
            "def _strategy_prompt(prompt, name):\n"
            "    return prompt\n\n"
            "async def _generate_with_provider(provider, prompt):\n"
            "    return await _gemini_json(prompt)\n\n"
            "def _apply_market_context(spec, asset=None, symbol=None, timeframe=None):\n"
            "    return spec\n\n"
            "async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):\n"
            "    return await _generate_with_provider('gemini', prompt)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "strategy_ai.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=CANONICAL_STRATEGY
            ):
                migration.transform_strategy_ai("fake-commit")
            result = target.read_text(encoding="utf-8")
            self.assertIn("class AIStrategySpec", result)
            self.assertIn("def _apply_market_context", result)
            self.assertIn("compose_strategy_spec", result)
            self.assertIn('provider": "atlas_native"', result)
            self.assertNotRegex(result, migration.PROVIDER_RE)
            self.assertNotIn("_generate_with_provider", result)

    def test_strategy_migration_refuses_to_discard_generator_validation(self):
        legacy = (
            "from .ai_providers import _gemini_json, _groq_json\n"
            "async def _generate_with_provider(provider, prompt):\n"
            "    return await _gemini_json(prompt)\n\n"
            "async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):\n"
            "    if not prompt.strip():\n"
            "        raise ValueError('prompt is required')\n"
            "    return await _generate_with_provider('gemini', prompt)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "strategy_ai.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=CANONICAL_STRATEGY
            ):
                with self.assertRaisesRegex(migration.MigrationError, "branch-specific behavior"):
                    migration.transform_strategy_ai("fake-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_strategy_migration_refuses_side_effecting_trampoline_arguments(self):
        cases = (
            (
                "async def _generate_with_provider(provider, prompt):\n"
                "    return await _gemini_json(prompt)\n\n"
                "async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):\n"
                "    return await _generate_with_provider('gemini', normalize_prompt(prompt))\n",
                "trampoline arguments may contain branch-specific input handling",
            ),
            (
                "async def _generate_with_provider(provider, prompt):\n"
                "    return await _gemini_json(normalize_prompt(prompt))\n\n"
                "async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):\n"
                "    return await _generate_with_provider('gemini', prompt)\n",
                "side-effecting arguments",
            ),
        )
        for definitions, message in cases:
            with self.subTest(message=message):
                legacy = "from .ai_providers import _gemini_json\n" + definitions
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    target = root / "app" / "strategy_ai.py"
                    target.parent.mkdir()
                    target.write_text(legacy, encoding="utf-8")
                    with patch.object(migration, "ROOT", root), patch.object(
                        migration, "canonical_text", return_value=CANONICAL_STRATEGY
                    ):
                        with self.assertRaisesRegex(migration.MigrationError, message):
                            migration.transform_strategy_ai("fake-commit")
                    self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_strategy_migration_refuses_duplicate_generator_definitions(self):
        legacy = (
            "from .ai_providers import _gemini_json\n"
            "async def _generate_with_provider(provider, prompt):\n"
            "    return await _gemini_json(prompt)\n\n"
            "async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):\n"
            "    return await _generate_with_provider('gemini', prompt)\n\n"
            "async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):\n"
            "    if not prompt.strip():\n"
            "        raise ValueError('prompt is required')\n"
            "    return await _generate_with_provider('gemini', prompt)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "strategy_ai.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=CANONICAL_STRATEGY
            ):
                with self.assertRaisesRegex(migration.MigrationError, "multiple top-level definitions"):
                    migration.transform_strategy_ai("fake-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_strategy_migration_refuses_aliases_that_masquerade_as_native_imports(self):
        cases = (
            ("custom_helper", "compose_strategy_spec"),
            ("custom_exception", "NativeAIError"),
        )
        for original_symbol, native_binding in cases:
            with self.subTest(original_symbol=original_symbol, native_binding=native_binding):
                legacy = "".join([
                    "from .ai_providers import _gemini_json, " + original_symbol + " as " + native_binding + "\n",
                    "SYSTEM_PROMPT = 'Gemini prompt'\n",
                    "async def _generate_with_provider(provider, prompt):\n",
                    "    return await _gemini_json(prompt)\n\n",
                    "def retained_helper():\n",
                    "    return " + native_binding + "()\n\n",
                    "async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):\n",
                    "    return await _generate_with_provider('gemini', prompt)\n",
                ])
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    target = root / "app" / "strategy_ai.py"
                    target.parent.mkdir()
                    target.write_text(legacy, encoding="utf-8")
                    with patch.object(migration, "ROOT", root), patch.object(
                        migration, "canonical_text", return_value=CANONICAL_STRATEGY
                    ):
                        with self.assertRaisesRegex(
                            migration.MigrationError, "only imports whose original symbol matches"
                        ):
                            migration.transform_strategy_ai("fake-commit")
                    self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_strategy_migration_refuses_to_drop_import_used_by_retained_helper(self):
        legacy = (
            "from .ai_providers import AIProviderError, _gemini_json, _groq_json, custom_helper\n"
            "SYSTEM_PROMPT = 'Gemini prompt'\n"
            "async def _generate_with_provider(provider, prompt):\n"
            "    return await _gemini_json(prompt)\n\n"
            "def record_metrics():\n"
            "    return custom_helper()\n\n"
            "async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):\n"
            "    return await _generate_with_provider('gemini', prompt)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "strategy_ai.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=CANONICAL_STRATEGY
            ):
                with self.assertRaisesRegex(migration.MigrationError, "retained strategy helper depends on"):
                    migration.transform_strategy_ai("fake-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_daily_research_replaces_only_provider_conditional(self):
        legacy = (
            "from .ai_providers import dual_ai_research_review\n"
            "async def run_daily_research():\n"
            "    keep_this = True\n"
            "    review = {}\n"
            "    if settings.gemini_api_key or settings.groq_api_key:\n"
            "        review['external_ai'] = await dual_ai_research_review(research, intelligence)\n"
            "    else:\n"
            "        review['external_ai'] = {'status': 'NOT_CONFIGURED'}\n"
            "    review['run_drift'] = await persist_run()\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "daily_research.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                migration.transform_daily_research()
            result = target.read_text(encoding="utf-8")
            self.assertIn("keep_this = True", result)
            self.assertIn("review['run_drift']", result)
            self.assertIn('native_research_input["paper_candidates"] = review.get("paper_candidates", research.get("paper_candidates", []))', result)
            self.assertIn("native_research_review(native_research_input, intelligence)", result)
            self.assertIn(
                '\n    native_research_input = dict(research)\n'
                '    native_research_input["paper_candidates"] = review.get("paper_candidates", research.get("paper_candidates", []))\n'
                '    review["native_ai_review"] = native_research_review(native_research_input, intelligence)\n',
                result,
            )
            ast.parse(result, filename="app/daily_research.py")
            self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_daily_research_refuses_setup_call_inside_provider_arguments(self):
        legacy = (
            "from .ai_providers import dual_ai_research_review\n"
            "async def run_daily_research():\n"
            "    review = {}\n"
            "    if settings.gemini_api_key:\n"
            "        review['external_ai'] = await dual_ai_research_review(build_research(), intelligence)\n"
            "    else:\n"
            "        review['external_ai'] = {'status': 'NOT_CONFIGURED'}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "daily_research.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                with self.assertRaisesRegex(migration.MigrationError, "contains unrelated or unknown logic"):
                    migration.transform_daily_research()
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_daily_research_refuses_dynamic_fallback_details(self):
        legacy = (
            "from .ai_providers import dual_ai_research_review\n"
            "async def run_daily_research():\n"
            "    review = {}\n"
            "    if settings.gemini_api_key:\n"
            "        review['external_ai'] = await dual_ai_research_review(research, intelligence)\n"
            "    else:\n"
            "        review['external_ai'] = {'status': 'NOT_CONFIGURED', 'reason': fallback_reason}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "daily_research.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                with self.assertRaisesRegex(migration.MigrationError, "contains unrelated or unknown logic"):
                    migration.transform_daily_research()
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_daily_research_refuses_to_drop_audit_side_effect_in_provider_conditional(self):
        legacy = (
            "from .ai_providers import dual_ai_research_review\n"
            "async def run_daily_research():\n"
            "    review = {}\n"
            "    if settings.gemini_api_key:\n"
            "        await write_audit_record('provider selected')\n"
            "        review['external_ai'] = await dual_ai_research_review(research, intelligence)\n"
            "    else:\n"
            "        review['external_ai'] = {'status': 'NOT_CONFIGURED'}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "daily_research.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            original = target.read_text(encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                with self.assertRaisesRegex(migration.MigrationError, "contains unrelated or unknown logic"):
                    migration.transform_daily_research()
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_daily_research_refuses_side_effecting_provider_condition(self):
        legacy = (
            "from .ai_providers import dual_ai_research_review\n"
            "async def run_daily_research():\n"
            "    review = {}\n"
            "    if provider_enabled(settings.gemini_api_key):\n"
            "        review['external_ai'] = await dual_ai_research_review(research, intelligence)\n"
            "    else:\n"
            "        review['external_ai'] = {'status': 'NOT_CONFIGURED'}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "daily_research.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root):
                with self.assertRaisesRegex(migration.MigrationError, "condition includes a call expression"):
                    migration.transform_daily_research()
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_scrub_workflow_removes_only_provider_entries(self):
        source = (
            "env:\n"
            "  DATABASE_URL: ${{ secrets.DATABASE_URL }}\n"
            "  SECRET_GEMINI_API_KEY_VERSION: ${{ secrets.SECRET_GEMINI_API_KEY_VERSION }}\n"
            "  GEMINI_MODEL: ${{ vars.GEMINI_MODEL }}\n"
            "  SECRET_KEY_VERSION: ${{ secrets.SECRET_KEY_VERSION }}\n"
        )
        result = migration.scrub_ancillary_text(
            ".github/workflows/production-deploy.yml", source
        )
        self.assertIn("DATABASE_URL", result)
        self.assertIn("SECRET_KEY_VERSION", result)
        self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_metadata_scrub_returns_valid_json_without_provider_keys(self):
        source = json.dumps({
            "ai_model": "gemini-3.5-flash",
            "gemini_api_key": "not-a-real-secret",
            "app_version": "3.10.47",
            "description": "Gemini research is informational only and never enables live trading.",
        })
        result = migration.scrub_ancillary_text("metadata.json", source)
        parsed = json.loads(result)
        self.assertEqual(parsed["ai_model"], "atlas-native-strategy-v1")
        self.assertNotIn("gemini_api_key", parsed)
        self.assertEqual(parsed["app_version"], "3.10.47")
        self.assertIn("informational only", parsed["description"])
        self.assertNotIn("Gemini", parsed["description"])


    def test_shell_deployment_scrub_preserves_non_ai_settings_and_shell_syntax(self):
        source = r'''#!/usr/bin/env bash
set -euo pipefail
DATABASE_URL="${DATABASE_URL:?required}"
: "${SECRET_GEMINI_API_KEY_VERSION:?Set SECRET_GEMINI_API_KEY_VERSION}"
SECRET_GROQ_API_KEY_VERSION="${SECRET_GROQ_API_KEY_VERSION:-}"
AI_SECRET_ARGS=",GEMINI_API_KEY=atlas-gemini-api-key:${SECRET_GEMINI_API_KEY_VERSION}"
if [[ -z "$SECRET_GROQ_API_KEY_VERSION" ]]; then
  echo "WARNING: no Groq key configured"
fi
if [[ -n "$SECRET_GROQ_API_KEY_VERSION" ]]; then AI_SECRET_ARGS="${AI_SECRET_ARGS},GROQ_API_KEY=atlas-groq-api-key:${SECRET_GROQ_API_KEY_VERSION}"; fi
ENV_VARS="^@^ENVIRONMENT=production@DATABASE_URL=${DATABASE_URL}@GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.5-flash}@GROQ_MODEL=openai/gpt-oss-20b@OPENAI_ORGANIZATION=org-id"
gcloud run deploy atlas --set-env-vars "ENVIRONMENT=production,DATABASE_URL=${DATABASE_URL},GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.5-flash},GROQ_MODEL=openai/gpt-oss-20b,OPENAI_ORGANIZATION=org-id" --set-secrets "DATABASE_URL=${DATABASE_SECRET_REF},GEMINI_API_KEY=${SECRET_GEMINI_API_KEY_VERSION}${AI_SECRET_ARGS}"
grant_secret_access atlas-openai-api-key atlas-runtime
for secret in atlas-database-url atlas-secret-key atlas-gemini-api-key atlas-openai-api-key; do echo "$secret"; done
'''
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cloud-run-deploy.sh"
            target.write_text(source, encoding="utf-8")
            cleaned = migration.scrub_ancillary_text("deploy/cloud-run-deploy.sh", source)
            target.write_text(cleaned, encoding="utf-8")
            import subprocess
            check = subprocess.run(
                ["bash", "-n", str(target)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("DATABASE_URL", cleaned)
        self.assertIn("DATABASE_SECRET_REF", cleaned)
        self.assertNotIn("OPENAI_ORGANIZATION", cleaned)
        self.assertNotIn("atlas-openai-api-key", cleaned)
        self.assertNotRegex(cleaned, migration.PROVIDER_RE)


    def test_shell_scrub_refuses_provider_block_containing_unrelated_deployment_command(self):
        source = (
            'if [[ -n "$SECRET_GROQ_API_KEY_VERSION" ]]; then\n'
            '  echo "WARNING: Groq key configured"\n'
            '  gcloud run deploy atlas --project "$GCP_PROJECT_ID"\n'
            'fi\n'
        )
        with self.assertRaisesRegex(migration.MigrationError, "mixed provider-conditioned shell block"):
            migration.scrub_shell_deployment("deploy/cloud-run-deploy.sh", source)

    def test_release_diagnostics_replaces_only_hosted_route_and_preserves_neighbor_routes(self):
        legacy = """# Authorized release diagnostics. The gateway key is read only from server configuration.
_RELEASE_SECRET_PATTERNS = []
def _redact_release_diagnostic_text(value):
    return value
@app.post("/api/admin/release-diagnostics")
async def admin_release_diagnostics(payload, authorization=None):
    if not settings.ai_gateway_api_key:
        raise HTTPException(503, "gateway not configured")
    async with httpx.AsyncClient(timeout=45.0) as client:
        await client.post(settings.ai_gateway_base_url + "/chat/completions")
    return {"model": settings.ai_gateway_model}
@app.get("/api/customer/keep")
async def keep_customer_route():
    return {"kept": True}
"""
        canonical = """# Authorized release diagnostics. The gateway key is read only from server configuration.
_RELEASE_SECRET_PATTERNS = []
def _redact_release_diagnostic_text(value):
    return value
def _release_diagnostic_report(safe_logs, safe_config):
    return "deterministic-local-rules-v1"
@app.post("/api/admin/release-diagnostics")
async def admin_release_diagnostics(payload, authorization=None):
    diagnostics = _release_diagnostic_report(payload.get("logs", ""), payload.get("configuration", ""))
    return {"model": "deterministic-local-rules-v1", "diagnostics": diagnostics}
@app.get("/api/customer/keep")
async def keep_customer_route():
    return {"kept": True}
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "main.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                migration.transform_release_diagnostics("fake-extra-commit")
            result = target.read_text(encoding="utf-8")
        self.assertNotIn("settings.ai_gateway_api_key", result)
        self.assertNotIn("/chat/completions", result)
        self.assertIn("deterministic-local-rules-v1", result)
        self.assertIn("/api/customer/keep", result)
        self.assertIn("async def keep_customer_route", result)
        ast.parse(result, filename="app/main.py")


if __name__ == "__main__":
    unittest.main()
