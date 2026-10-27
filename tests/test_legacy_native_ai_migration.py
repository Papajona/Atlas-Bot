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
                return_value="from .native_ai import AIProviderError, NativeAIError\n",
            ):
                with self.assertRaisesRegex(migration.MigrationError, "canonical name .* differently"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_star_import_dependency_is_preserved_after_docstring_and_future_imports(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text("def branch_helper():\n    return True\n", encoding="utf-8")
            target.write_text(legacy, encoding="utf-8")
            canonical = '"""Canonical facade."""\nfrom __future__ import annotations\nfrom .native_ai import AIProviderError\n'
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                migration.transform_ai_providers("canonical-commit")
            result = target.read_text(encoding="utf-8")
            self.assertIn("from .branch_helpers import *", result)
            self.assertLess(
                result.index('"""Canonical facade."""'),
                result.index("from __future__ import annotations"),
            )
            self.assertLess(
                result.index("from __future__ import annotations"),
                result.index("from .branch_helpers import *"),
            )
            self.assertLess(
                result.index("from .branch_helpers import *"),
                result.index("from .native_ai import AIProviderError"),
            )
            self.assertIn("return branch_helper()", result)
            self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_star_import_with_conflicting_native_error_binding_fails_closed(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return NativeAIError("failure")
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text(
                "class NativeAIError(RuntimeError):\n    pass\n"
                "def branch_helper():\n    return True\n",
                encoding="utf-8",
            )
            target.write_text(legacy, encoding="utf-8")
            canonical = (
                '"""Canonical facade."""\n'
                "from __future__ import annotations\n"
                "from .native_ai import AIProviderError, NativeAIError\n"
            )
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "exports canonical name .* different or unknown binding"
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_explicit_import_before_overlapping_star_import_fails_closed(self):
        legacy = """from .explicit_helpers import helper
from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            explicit_module = root / "app" / "explicit_helpers.py"
            star_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            explicit_module.write_text("def helper():\n    return 'explicit'\n", encoding="utf-8")
            star_module.write_text("def helper():\n    return 'star'\n", encoding="utf-8")
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration,
                "canonical_text",
                return_value="from .native_ai import AIProviderError\n",
            ):
                with self.assertRaisesRegex(migration.MigrationError, "overlapping exports"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_absolute_app_star_import_is_resolved_under_app_package(self):
        legacy = """from app.branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text("def branch_helper():\n    return True\n", encoding="utf-8")
            target.write_text(legacy, encoding="utf-8")
            canonical = '"""Canonical facade."""\nfrom __future__ import annotations\nfrom .native_ai import AIProviderError\n'
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                migration.transform_ai_providers("canonical-commit")
            result = target.read_text(encoding="utf-8")
            self.assertIn("from app.branch_helpers import *", result)
            self.assertIn("return branch_helper()", result)
            self.assertLess(
                result.index("from __future__ import annotations"),
                result.index("from app.branch_helpers import *"),
            )
            self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_indexed_all_export_mutation_fails_closed(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text(
                "__all__ = ['branch_helper']\n"
                "__all__[0] = 'json'\n"
                "def branch_helper():\n    return True\n",
                encoding="utf-8",
            )
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration,
                "canonical_text",
                return_value="from .native_ai import AIProviderError\n",
            ):
                with self.assertRaisesRegex(migration.MigrationError, "dynamic exports or mutates __all__"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_globals_created_star_export_fails_closed_before_import_reordering(self):
        legacy = """from .explicit_helpers import helper
from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            explicit_module = root / "app" / "explicit_helpers.py"
            star_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            explicit_module.write_text("def helper():\n    return 'explicit'\n", encoding="utf-8")
            star_module.write_text("globals()['helper'] = lambda: 'star'\n", encoding="utf-8")
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration,
                "canonical_text",
                return_value="from .native_ai import AIProviderError\n",
            ):
                with self.assertRaisesRegex(migration.MigrationError, "dynamic exports or mutates __all__"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_module_level_loop_binding_fails_closed_for_star_import(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text("for helper in (lambda: True,):\n    pass\n", encoding="utf-8")
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration,
                "canonical_text",
                return_value="from .native_ai import AIProviderError\n",
            ):
                with self.assertRaisesRegex(migration.MigrationError, "dynamic exports or mutates __all__"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_indirect_globals_all_mutation_fails_closed(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text(
                "__all__ = ['branch_helper']\n"
                "globals()['__all__'].append('json')\n"
                "def branch_helper():\n    return True\n",
                encoding="utf-8",
            )
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration,
                "canonical_text",
                return_value="from .native_ai import AIProviderError\n",
            ):
                with self.assertRaisesRegex(migration.MigrationError, "dynamic exports or mutates __all__"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_literal_named_expression_all_exports_only_declared_names(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text(
                "(__all__ := ['branch_helper'])\n"
                "class NativeAIError(RuntimeError):\n    pass\n"
                "def branch_helper():\n    return True\n",
                encoding="utf-8",
            )
            target.write_text(legacy, encoding="utf-8")
            canonical = "from .native_ai import AIProviderError, NativeAIError\n"
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                migration.transform_ai_providers("canonical-commit")
            result = target.read_text(encoding="utf-8")
            self.assertIn("from .branch_helpers import *", result)
            self.assertIn("return branch_helper()", result)
            self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_star_export_inspects_package_before_same_named_module_file(self):
        legacy = """from app.branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            module_file = root / "app" / "branch_helpers.py"
            package_init = root / "app" / "branch_helpers" / "__init__.py"
            target.parent.mkdir()
            package_init.parent.mkdir()
            module_file.write_text("def branch_helper():\n    return True\n", encoding="utf-8")
            package_init.write_text(
                "class NativeAIError(RuntimeError):\n    pass\n"
                "def branch_helper():\n    return True\n",
                encoding="utf-8",
            )
            target.write_text(legacy, encoding="utf-8")
            canonical = "from .native_ai import AIProviderError, NativeAIError\n"
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                with self.assertRaisesRegex(migration.MigrationError, "exports canonical name .* different or unknown binding"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_local_definition_before_overlapping_star_import_fails_closed(self):
        legacy = """def helper():
    return 'local'

from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text("def helper():\n    return 'star'\n", encoding="utf-8")
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration,
                "canonical_text",
                return_value="from .native_ai import AIProviderError\n",
            ):
                with self.assertRaisesRegex(migration.MigrationError, "definition precedes a later star import"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_aliased_all_list_mutation_fails_closed(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text(
                "__all__ = ['branch_helper']\n"
                "names = __all__\n"
                "names.append('NativeAIError')\n"
                "class NativeAIError(RuntimeError):\n    pass\n"
                "def branch_helper():\n    return True\n",
                encoding="utf-8",
            )
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration,
                "canonical_text",
                return_value="from .native_ai import AIProviderError, NativeAIError\n",
            ):
                with self.assertRaisesRegex(migration.MigrationError, "dynamic exports or mutates __all__"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_explicit_import_after_local_definition_fails_closed(self):
        legacy = """def helper():
    return 'local'

from .explicit_helpers import helper

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            import_module = root / "app" / "explicit_helpers.py"
            target.parent.mkdir()
            import_module.write_text("def helper():\n    return 'imported'\n", encoding="utf-8")
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration,
                "canonical_text",
                return_value="from .native_ai import AIProviderError\n",
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "explicit import follows a local definition"
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_nested_all_walrus_is_not_a_module_export_declaration(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text(
                "def helper():\n"
                "    (__all__ := ['NativeAIError'])\n"
                "class NativeAIError(RuntimeError):\n"
                "    pass\n"
                "def branch_helper():\n"
                "    return True\n",
                encoding="utf-8",
            )
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text",
                return_value="from .native_ai import AIProviderError, NativeAIError\n",
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "dynamic exports or mutates __all__"
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_top_level_walrus_binding_counts_as_star_export(self):
        legacy = """def helper():
    return 'local'

from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text(
                "def factory():\n"
                "    return lambda: 'star'\n"
                "(helper := factory())\n",
                encoding="utf-8",
            )
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text",
                return_value="from .native_ai import AIProviderError\n",
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "definition precedes a later star import"
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_custom_runtime_error_base_prevents_legacy_stub_replacement(self):
        legacy = """from .custom_runtime import RuntimeError

class AIProviderError(RuntimeError):
    pass
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text",
                return_value="from .native_ai import AIProviderError\n",
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "RuntimeError is imported or rebound"
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_aliased_all_mutation_fails_closed(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text(
                "__all__ = ['branch_helper']\n"
                "names = __all__\n"
                "names.append('NativeAIError')\n"
                "class NativeAIError(RuntimeError):\n    pass\n"
                "def branch_helper():\n    return True\n",
                encoding="utf-8",
            )
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text",
                return_value="from .native_ai import AIProviderError, NativeAIError\n",
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "dynamic exports or mutates __all__"
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_conditional_runtime_error_rebinding_prevents_stub_replacement(self):
        legacy = """if use_custom_runtime:
    RuntimeError = CustomRuntimeError

class AIProviderError(RuntimeError):
    pass
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text",
                return_value="from .native_ai import AIProviderError\n",
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "RuntimeError is imported or rebound"
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_chained_all_assignment_alias_mutation_fails_closed(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text(
                "aliases = __all__ = ['branch_helper']\n"
                "aliases.append('NativeAIError')\n"
                "class NativeAIError(RuntimeError):\n    pass\n"
                "def branch_helper():\n    return True\n",
                encoding="utf-8",
            )
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text",
                return_value="from .native_ai import AIProviderError, NativeAIError\n",
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "dynamic exports or mutates __all__"
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_local_runtime_error_inside_nested_function_does_not_block_safe_stub_replacement(self):
        legacy = """if setup_enabled:
    def helper():
        RuntimeError = LocalError
        return RuntimeError

class AIProviderError(RuntimeError):
    pass
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            canonical = "from .native_ai import AIProviderError\n"
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                migration.transform_ai_providers("canonical-commit")
            result = target.read_text(encoding="utf-8")
            self.assertNotIn("class AIProviderError(RuntimeError)", result)
            self.assertIn("from .native_ai import AIProviderError", result)
            self.assertIn("RuntimeError = LocalError", result)
            self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_immutable_tuple_chained_all_alias_is_safe(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper_module = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper_module.write_text(
                "aliases = __all__ = ('branch_helper',)\n"
                "def branch_helper():\n    return True\n",
                encoding="utf-8",
            )
            target.write_text(legacy, encoding="utf-8")
            canonical = "from .native_ai import AIProviderError\n"
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                migration.transform_ai_providers("canonical-commit")
            result = target.read_text(encoding="utf-8")
            self.assertIn("from .branch_helpers import *", result)
            self.assertIn("return branch_helper()", result)
            self.assertIn("from .native_ai import AIProviderError", result)
            self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_legacy_extract_json_uses_canonical_pure_parser_contract(self):
        legacy = """import json

class AIProviderError(RuntimeError):
    pass

def _extract_json(text: str) -> dict:
    raw = (text or "").strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIProviderError(f"AI provider returned non-JSON output: {exc}") from exc
    if not isinstance(value, dict):
        raise AIProviderError("AI provider returned JSON that is not an object")
    return value

def retained_helper(text):
    return _extract_json(text)
"""
        canonical = '''"""Canonical Native AI parser."""
from __future__ import annotations
import json
from typing import Any
from .native_ai import AIProviderError

def _extract_json(text: Any) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise AIProviderError("Atlas Native AI received empty or non-text JSON input")
    raw = text.strip()
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AIProviderError(f"Atlas Native AI received invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AIProviderError("Atlas Native AI requires a JSON object")
    return value
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                migration.transform_ai_providers("canonical-commit")
            result = target.read_text(encoding="utf-8")
            self.assertIn("def _extract_json(text: Any)", result)
            self.assertIn("def retained_helper(text)", result)
            self.assertIn("return _extract_json(text)", result)
            self.assertNotIn("AI provider returned non-JSON output", result)
            self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_legacy_response_json_helper_uses_canonical_parser_contract(self):
        legacy = """class AIProviderError(RuntimeError):
    pass

def _response_json_object(response, provider: str) -> dict:
    try:
        body = response.json()
    except ValueError as exc:
        raise AIProviderError(f"{provider} returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise AIProviderError(f"{provider} response must be a JSON object")
    return body

def retained_helper(response):
    return _response_json_object(response, "local")
"""
        canonical = '''"""Canonical Native AI response parsing."""
from __future__ import annotations
from typing import Any
from .native_ai import AIProviderError

def _response_json_object(response: Any, provider: str = "local component") -> dict[str, Any]:
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError, TypeError, AttributeError) as exc:
        raise AIProviderError(f"{provider} returned an invalid JSON response body") from exc
    if not isinstance(body, dict):
        raise AIProviderError(f"{provider} response body must be a JSON object")
    return body
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                migration.transform_ai_providers("canonical-commit")
            result = target.read_text(encoding="utf-8")
            self.assertIn("def _response_json_object(response: Any", result)
            self.assertIn('return _response_json_object(response, "local")', result)
            self.assertNotIn('response must be a JSON object', result)
            self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_provider_bearing_or_side_effecting_json_name_collision_still_fails_closed(self):
        legacy = """import json

class AIProviderError(RuntimeError):
    pass

def _extract_json(text):
    audit_sink.append(text)
    return json.loads(text)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            canonical = '''import json
from .native_ai import AIProviderError

def _extract_json(text):
    return json.loads(text)
'''
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "branch-specific definition '_extract_json' that collides"
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_json_helper_collision_refuses_state_mutation_return_drift_arity_and_property_chains(self):
        unsafe_helpers = [
            (
                "state mutation",
                """def _extract_json(text):
    audit_cache["last_input"] = text
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    return value
""",
            ),
            (
                "wrong result",
                """def _extract_json(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    return text
""",
            ),
            (
                "extra argument",
                """def _extract_json(text, json_module):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    return value
""",
            ),
            (
                "property chain",
                """def _extract_json(text):
    raw = text.payload.strip()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    return value
""",
            ),
        ]
        canonical = """import json
from .native_ai import AIProviderError

def _extract_json(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    return value
"""
        for label, helper in unsafe_helpers:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / "app" / "ai_providers.py"
                target.parent.mkdir()
                legacy = (
                    "import json\n\n"
                    "class AIProviderError(RuntimeError):\n    pass\n\n"
                    + helper
                )
                target.write_text(legacy, encoding="utf-8")
                with patch.object(migration, "ROOT", root), patch.object(
                    migration, "canonical_text", return_value=canonical
                ):
                    with self.assertRaisesRegex(
                        migration.MigrationError, "branch-specific definition '_extract_json' that collides"
                    ):
                        migration.transform_ai_providers("canonical-commit")
                self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_json_helper_with_branch_specific_value_rejection_fails_closed(self):
        legacy = """import json

class AIProviderError(RuntimeError):
    pass

def _extract_json(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    if value.get("admin") is False:
        raise AIProviderError("branch-specific policy")
    return value
"""
        canonical = """import json
from .native_ai import AIProviderError

def _extract_json(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    return value
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "branch-specific definition '_extract_json' that collides"
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_json_helper_noncanonical_strip_argument_fails_closed(self):
        legacy = """import json

class AIProviderError(RuntimeError):
    pass

def _extract_json(text):
    raw = text.strip("x")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    return value
"""
        canonical = """import json
from .native_ai import AIProviderError

def _extract_json(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    return value
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "branch-specific definition '_extract_json' that collides"
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_alternate_json_module_binding_fails_closed(self):
        legacy = """import orjson as json

class AIProviderError(RuntimeError):
    pass

def _extract_json(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    return value
"""
        canonical = """import json
from .native_ai import AIProviderError

def _extract_json(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    return value
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "branch-specific definition '_extract_json' that collides"
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

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
                return_value="import json\nfrom .native_ai import AIProviderError\n",
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
