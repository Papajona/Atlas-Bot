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
from .config import settings
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ALLOWED_TIMEFRAMES = {"5m", "15m", "30m", "1h", "4h", "1d"}

class StrategyCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=2, max_length=60)
    rule: str = Field(min_length=3, max_length=500)
    timeframe: str | None = Field(default=None, max_length=10)

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {value}")
        return value

class AIStrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["atlas-strategy-v1"] = "atlas-strategy-v1"
    title: str = Field(min_length=2, max_length=120)
    summary: str = Field(min_length=10, max_length=1000)
    asset: Literal["crypto", "forex", "commodity"]
    symbols: list[str] = Field(min_length=1, max_length=10)
    timeframes: list[str] = Field(min_length=1, max_length=5)
    direction: Literal["LONG", "SHORT", "BOTH", "AUTO"]
    strategy_family: Literal["TREND", "MOMENTUM", "BREAKOUT", "MEAN_REVERSION", "ENSEMBLE"]
    entry_conditions: list[StrategyCondition] = Field(min_length=1, max_length=12)
    exit_conditions: list[StrategyCondition] = Field(min_length=1, max_length=8)
    risk_per_trade: float = Field(gt=0, le=0.05)
    stop_loss_atr: float | None = Field(default=None, gt=0, le=10)
    take_profit_rr: float | None = Field(default=None, gt=0, le=10)
    sessions: list[str] = Field(default_factory=list, max_length=8)
    assumptions: list[str] = Field(default_factory=list, max_length=10)
    validation_warnings: list[str] = Field(default_factory=list, max_length=12)
    requires_backtest: Literal[True] = True
    requires_walk_forward: Literal[True] = True
    execution_mode: Literal["PAPER_SHADOW_ONLY"] = "PAPER_SHADOW_ONLY"
    implementation_status: Literal["ATLAS_SUPPORTED_FAMILY"] = "ATLAS_SUPPORTED_FAMILY"

    @field_validator("timeframes")
    @classmethod
    def validate_timeframes(cls, values: list[str]) -> list[str]:
        if any(v not in ALLOWED_TIMEFRAMES for v in values):
            raise ValueError(f"Unsupported timeframe; allowed: {sorted(ALLOWED_TIMEFRAMES)}")
        return list(dict.fromkeys(values))

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: list[str]) -> list[str]:
        cleaned = [str(v).strip() for v in values if str(v).strip()]
        if not cleaned:
            raise ValueError("At least one symbol is required")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def validate_risk_contract(self) -> "AIStrategySpec":
        if self.risk_per_trade > settings.risk_per_trade:
            self.validation_warnings.append(
                f"Requested AI risk was capped at the platform maximum of {settings.risk_per_trade:.4f}."
            )
            self.risk_per_trade = settings.risk_per_trade
        return self


def _apply_market_context(spec: AIStrategySpec, *, asset: str | None = None, symbol: str | None = None, timeframe: str | None = None) -> AIStrategySpec:
    data = spec.model_dump()
    if asset is not None:
        data["asset"] = asset
    if symbol is not None:
        data["symbols"] = [symbol]
    if timeframe is not None:
        data["timeframes"] = [timeframe]
    return AIStrategySpec.model_validate(data)


async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):
    try:
        raw = compose_strategy_spec(prompt=prompt, name=name, asset=asset, symbol=symbol,
                                    timeframe=timeframe, max_risk_per_trade=settings.risk_per_trade)
        spec = AIStrategySpec.model_validate(raw)
        spec = _apply_market_context(spec, asset=asset, symbol=symbol, timeframe=timeframe)
    except Exception as exc:
        raise NativeAIError(f"Atlas Native AI could not create a valid strategy draft: {exc}") from exc
    family = {"TREND":"trend", "MOMENTUM":"momentum", "BREAKOUT":"breakout",
              "MEAN_REVERSION":"mean_reversion", "ENSEMBLE":"ensemble"}[spec.strategy_family]
    return {
        "strategy": spec.model_dump(),
        "evidence": evidence_for(family),
        "ai": {"provider":"atlas_native", "model":"atlas-native-strategy-v1",
               "fallback_used":False, "provider_errors":{}, "execution_authority":False,
               "requires_backtest":True, "requires_walk_forward":True, "network_calls":0},
    }
"""


def _with_canonical_schema_context(source: str) -> str:
    """Give acceptance fixtures the real canonical validation and market-binding contract."""
    canonical_tree = ast.parse(CANONICAL_STRATEGY)
    old_tree = ast.parse(source)
    canonical_lines = CANONICAL_STRATEGY.splitlines(keepends=True)
    source_lines = source.splitlines(keepends=True)

    canonical_condition = next(node for node in canonical_tree.body if isinstance(node, ast.ClassDef) and node.name == "StrategyCondition")
    canonical_spec = next(node for node in canonical_tree.body if isinstance(node, ast.ClassDef) and node.name == "AIStrategySpec")
    canonical_context = next(node for node in canonical_tree.body if isinstance(node, ast.FunctionDef) and node.name == "_apply_market_context")
    old_spec = next(node for node in old_tree.body if isinstance(node, ast.ClassDef) and node.name == "AIStrategySpec")
    old_context = next(node for node in old_tree.body if isinstance(node, ast.FunctionDef) and node.name == "_apply_market_context")
    pydantic_import = next(
        node for node in old_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "pydantic"
    )
    condition_source = canonical_lines[canonical_condition.lineno - 1:canonical_condition.end_lineno]
    spec_source = canonical_lines[canonical_spec.lineno - 1:canonical_spec.end_lineno]
    context_source = canonical_lines[canonical_context.lineno - 1:canonical_context.end_lineno]
    pydantic_block = [
        "from typing import Literal\n",
        "from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator\n",
        'ALLOWED_TIMEFRAMES = {"5m", "15m", "30m", "1h", "4h", "1d"}\n',
        "\n",
        *condition_source,
        "\n",
    ]
    edits = [
        (old_spec.lineno, old_spec.end_lineno, spec_source),
        (old_context.lineno, old_context.end_lineno, context_source),
        (pydantic_import.lineno, pydantic_import.end_lineno, pydantic_block),
    ]
    for start, end, replacement in sorted(edits, reverse=True):
        source_lines[start - 1:end] = replacement
    return "".join(source_lines)


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

    def test_equivalent_json_helper_ignores_unrelated_function_local_json_binding(self):
        legacy = """import json

class AIProviderError(RuntimeError):
    pass

def _extract_json(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    return value

def unrelated():
    json = {"local": True}
    return json
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
                migration.transform_ai_providers("canonical-commit")
            result = target.read_text(encoding="utf-8")
        ast.parse(result, filename="app/ai_providers.py")
        self.assertIn("def unrelated", result)
        self.assertIn('json = {"local": True}', result)
        self.assertIn("def _extract_json", result)
        self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_equivalent_json_helper_allows_duplicate_standard_json_imports(self):
        legacy = """import json
import json

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
                migration.transform_ai_providers("canonical-commit")
            result = target.read_text(encoding="utf-8")
        ast.parse(result, filename="app/ai_providers.py")
        self.assertIn("def _extract_json", result)
        self.assertNotRegex(result, migration.PROVIDER_RE)

    def test_class_global_json_rebinding_before_import_fails_closed(self):
        legacy = """class LegacyNamespace:
    global json
    json = object()

import json

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
                with self.assertRaisesRegex(migration.MigrationError, "branch-specific definition '_extract_json' that collides"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_comprehension_json_walrus_before_import_fails_closed(self):
        legacy = """[(json := object()) for _ in (0,)]

import json

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
                with self.assertRaisesRegex(migration.MigrationError, "branch-specific definition '_extract_json' that collides"):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_side_effecting_parser_annotation_fails_closed(self):
        legacy = """import json

class AIProviderError(RuntimeError):
    pass

def audit():
    return str

def _extract_json(text: audit()):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AIProviderError("response object required")
    return value
"""
        canonical = """import json
from .native_ai import AIProviderError

def _extract_json(text: str):
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
            "from .strategy_evidence import evidence_for\n"
            "from pydantic import BaseModel, Field\n\n"
            "class AIStrategySpec(BaseModel):\n"
            "    version: str = 'atlas-strategy-v1'\n"
            "    title: str = 'fixture'\n"
            "    summary: str = 'fixture summary'\n"
            "    asset: str = 'crypto'\n"
            "    symbols: list[str] = Field(default_factory=list)\n"
            "    timeframes: list[str] = Field(default_factory=list)\n"
            "    direction: str = 'LONG'\n"
            "    strategy_family: str = 'TREND'\n"
            "    entry_conditions: list[dict] = Field(default_factory=list)\n"
            "    exit_conditions: list[dict] = Field(default_factory=list)\n"
            "    risk_per_trade: float = 0.005\n"
            "    stop_loss_atr: float | None = None\n"
            "    take_profit_rr: float | None = None\n"
            "    sessions: list[str] = Field(default_factory=list)\n"
            "    assumptions: list[str] = Field(default_factory=list)\n"
            "    validation_warnings: list[str] = Field(default_factory=list)\n"
            "    requires_backtest: bool = True\n"
            "    requires_walk_forward: bool = True\n"
            "    execution_mode: str = 'PAPER_SHADOW_ONLY'\n"
            "    implementation_status: str = 'ATLAS_SUPPORTED_FAMILY'\n\n"
            "SYSTEM_PROMPT = '''Gemini strategy prompt'''\n\n"
            "def _strategy_prompt(prompt, name):\n"
            "    return prompt\n\n"
            "async def _generate_with_provider(provider, prompt):\n"
            "    return await _gemini_json(prompt)\n\n"
            "def _apply_market_context(spec, *, asset=None, symbol=None, timeframe=None):\n"
            "    data = spec.model_dump()\n"
            "    if asset is not None:\n"
            "        data['asset'] = asset\n"
            "    if symbol is not None:\n"
            "        data['symbols'] = [symbol]\n"
            "    if timeframe is not None:\n"
            "        data['timeframes'] = [timeframe]\n"
            "    return AIStrategySpec.model_validate(data)\n\n"
            "async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):\n"
            "    return await _generate_with_provider('gemini', prompt)\n"
        )
        legacy = _with_canonical_schema_context(legacy)
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
            self.assertIn('"provider":"atlas_native"', result)
            self.assertNotRegex(result, migration.PROVIDER_RE)
            self.assertNotIn("_generate_with_provider", result)

            # Execute the migrated function, not just its syntax, against a tiny
            # package that exercises the retained Pydantic schema/context contract.
            import asyncio
            import importlib
            import sys

            package = root / "fixture_app"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "strategy_ai.py").write_text(result, encoding="utf-8")
            (package / "native_ai.py").write_text(
                "class NativeAIError(RuntimeError): pass\n"
                "def compose_strategy_spec(*, prompt, name, asset=None, symbol=None, timeframe=None, max_risk_per_trade=0.01):\n"
                "    return {'version':'atlas-strategy-v1','title':'Fixture strategy','summary':'A test-only strategy fixture.',"
                "'asset':asset or 'crypto','symbols':[symbol] if symbol else ['EUR_USD'],"
                "'timeframes':[timeframe] if timeframe else ['1h'],'direction':'LONG','strategy_family':'TREND',"
                "'entry_conditions':[{'type':'indicator','rule':'RSI is above threshold','timeframe':timeframe or '1h'}],"
                "'exit_conditions':[{'type':'indicator','rule':'RSI is below threshold','timeframe':timeframe or '1h'}],"
                "'risk_per_trade':max_risk_per_trade * 2,'validation_warnings':[],'requires_backtest':True,'requires_walk_forward':True,"
                "'execution_mode':'PAPER_SHADOW_ONLY','implementation_status':'ATLAS_SUPPORTED_FAMILY'}\n",
                encoding="utf-8",
            )
            (package / "config.py").write_text(
                "class Settings: risk_per_trade = 0.01\nsettings = Settings()\n",
                encoding="utf-8",
            )
            (package / "strategy_evidence.py").write_text(
                "def evidence_for(family): return {'family': family}\n", encoding="utf-8"
            )
            sys.path.insert(0, str(root))
            try:
                module = importlib.import_module("fixture_app.strategy_ai")
                outcome = asyncio.run(module.generate_strategy_draft(
                    prompt="build a paper-only trend strategy", name="migration-test",
                    asset="forex", symbol="EUR_USD", timeframe="1h",
                ))
                self.assertEqual(outcome["strategy"]["strategy_family"], "TREND")
                self.assertEqual(outcome["strategy"]["risk_per_trade"], 0.01)
                self.assertTrue(any("capped" in warning for warning in outcome["strategy"]["validation_warnings"]))
                self.assertEqual(outcome["ai"]["provider"], "atlas_native")
                self.assertEqual(outcome["ai"]["network_calls"], 0)
            finally:
                sys.modules.pop("fixture_app.strategy_ai", None)
                sys.modules.pop("fixture_app", None)
                sys.path.remove(str(root))

    def test_strategy_migration_accepts_recognized_two_and_three_provider_flows(self):
        legacy_two = """from .config import settings
from .ai_providers import AIProviderError, _gemini_json, _groq_json
from .strategy_evidence import evidence_for

from pydantic import BaseModel, Field

class AIStrategySpec(BaseModel):
    version: str = "atlas-strategy-v1"
    title: str = "fixture"
    summary: str = "fixture summary"
    asset: str = "crypto"
    symbols: list[str] = Field(default_factory=list)
    timeframes: list[str] = Field(default_factory=list)
    direction: str = "LONG"
    strategy_family: str = "TREND"
    entry_conditions: list[dict] = Field(default_factory=list)
    exit_conditions: list[dict] = Field(default_factory=list)
    risk_per_trade: float = 0.005
    stop_loss_atr: float | None = None
    take_profit_rr: float | None = None
    sessions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    validation_warnings: list[str] = Field(default_factory=list)
    requires_backtest: bool = True
    requires_walk_forward: bool = True
    execution_mode: str = "PAPER_SHADOW_ONLY"
    implementation_status: str = "ATLAS_SUPPORTED_FAMILY"

SYSTEM_PROMPT = "Legacy strategy prompt"

def _strategy_prompt(prompt, name):
    return prompt

async def _generate_with_provider(provider: str, prompt: str) -> dict:
    if provider == "gemini":
        return await _gemini_json(prompt, system_instruction=SYSTEM_PROMPT, model=settings.gemini_strategy_model or settings.gemini_model)
    return await _groq_json(prompt, system_instruction=SYSTEM_PROMPT, model=settings.groq_strategy_model or settings.groq_model)

def _apply_market_context(spec, *, asset=None, symbol=None, timeframe=None):
    data = spec.model_dump()
    if asset is not None:
        data["asset"] = asset
    if symbol is not None:
        data["symbols"] = [symbol]
    if timeframe is not None:
        data["timeframes"] = [timeframe]
    return AIStrategySpec.model_validate(data)

async def generate_strategy_draft(*, prompt: str, name: str, asset: str | None = None, symbol: str | None = None, timeframe: str | None = None) -> dict:
    \"\"\"Generate a real LLM strategy draft, validate it, and return an auditable result.\"\"\"
    if not settings.gemini_api_key and not settings.groq_api_key:
        raise AIProviderError("No AI strategy provider is configured")

    provider_errors: dict[str, str] = {}
    provider_order = ["gemini", "groq"] if settings.gemini_api_key else ["groq"]
    if settings.gemini_api_key and settings.groq_api_key:
        provider_order = ["gemini", "groq"]

    raw: dict[str, object] | None = None
    provider_used = ""
    for provider in provider_order:
        try:
            raw = await _generate_with_provider(provider, _strategy_prompt(prompt, name))
            provider_used = provider
            break
        except Exception as exc:
            provider_errors[provider] = str(exc)

    if raw is None:
        raise AIProviderError("All configured AI strategy providers failed: " + "; ".join(
            f"{k}: {v}" for k, v in provider_errors.items()
        ))

    try:
        spec = AIStrategySpec.model_validate(raw)
        spec = _apply_market_context(spec, asset=asset, symbol=symbol, timeframe=timeframe)
    except Exception as exc:
        raise AIProviderError(f"AI strategy failed schema validation: {exc}") from exc

    family_key = {"TREND":"trend", "MOMENTUM":"momentum", "BREAKOUT":"breakout", "MEAN_REVERSION":"mean_reversion", "ENSEMBLE":"ensemble"}[spec.strategy_family]
    return {
        "strategy": spec.model_dump(),
        "evidence": evidence_for(family_key),
        "ai": {
            "provider": provider_used,
            "model": settings.gemini_strategy_model if provider_used == "gemini" else settings.groq_strategy_model,
            "fallback_used": provider_used != "gemini",
            "provider_errors": provider_errors,
            "execution_authority": False,
            "requires_backtest": True,
            "requires_walk_forward": True,
        },
    }
"""
        legacy_three = legacy_two.replace(
            "from .ai_providers import AIProviderError, _gemini_json, _groq_json",
            "from .ai_providers import AIProviderError, _gemini_json, _groq_json, _cerebras_json",
        ).replace(
            "async def _generate_with_provider(provider: str, prompt: str) -> dict:\n"
            "    if provider == \"gemini\":\n"
            "        return await _gemini_json(prompt, system_instruction=SYSTEM_PROMPT, model=settings.gemini_strategy_model or settings.gemini_model)\n"
            "    return await _groq_json(prompt, system_instruction=SYSTEM_PROMPT, model=settings.groq_strategy_model or settings.groq_model)\n",
            "async def _generate_with_provider(provider: str, prompt: str) -> dict:\n"
            "    if provider == \"gemini\":\n"
            "        return await _gemini_json(prompt, system_instruction=SYSTEM_PROMPT, model=settings.gemini_strategy_model or settings.gemini_model)\n"
            "    if provider == \"groq\":\n"
            "        return await _groq_json(prompt, system_instruction=SYSTEM_PROMPT, model=settings.groq_strategy_model or settings.groq_model)\n"
            "    return await _cerebras_json(prompt, system_instruction=SYSTEM_PROMPT, model=settings.cerebras_model)\n",
        ).replace(
            "if not settings.gemini_api_key and not settings.groq_api_key:",
            "if not settings.gemini_api_key and not settings.groq_api_key and not settings.cerebras_api_key:",
        ).replace(
            'provider_order = ["gemini", "groq"] if settings.gemini_api_key else ["groq"]\n'
            '    if settings.gemini_api_key and settings.groq_api_key:\n'
            '        provider_order = ["gemini", "groq"]',
            'provider_order = []\n'
            '    if settings.gemini_api_key:\n'
            '        provider_order.append("gemini")\n'
            '    if settings.groq_api_key:\n'
            '        provider_order.append("groq")\n'
            '    if settings.cerebras_api_key:\n'
            '        provider_order.append("cerebras")',
        ).replace(
            '"model": settings.gemini_strategy_model if provider_used == "gemini" else settings.groq_strategy_model,',
            '"model": settings.gemini_model if provider_used == "gemini" else settings.groq_strategy_model if provider_used == "groq" else settings.cerebras_model,',
        )

        for label, legacy in (("two-provider", legacy_two), ("three-provider", legacy_three)):
            legacy = _with_canonical_schema_context(legacy)
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / "app" / "strategy_ai.py"
                target.parent.mkdir()
                target.write_text(legacy, encoding="utf-8")
                with patch.object(migration, "ROOT", root), patch.object(
                    migration, "canonical_text", return_value=CANONICAL_STRATEGY
                ):
                    migration.transform_strategy_ai("fake-commit")
                result = target.read_text(encoding="utf-8")
                ast.parse(result, filename="app/strategy_ai.py")
                self.assertIn("compose_strategy_spec", result)
                self.assertIn("def _apply_market_context", result)
                self.assertNotIn("_generate_with_provider", result)
                self.assertNotRegex(result, migration.PROVIDER_RE)

        unsafe_cases = (
            (
                "cross-provider model routing",
                "model=settings.gemini_strategy_model or settings.gemini_model)",
                "model=settings.groq_strategy_model or settings.groq_model)",
                "_generate_with_provider contains additional or unknown behavior",
            ),
            (
                "custom provider output",
                '"model": settings.gemini_strategy_model if provider_used == "gemini" else settings.groq_strategy_model,',
                '"model": settings,',
                "generate_strategy_draft contains unrecognized branch-specific behavior",
            ),
            (
                "custom validation reporting",
                'f"AI strategy failed schema validation: {exc}"',
                'f"AI strategy failed schema validation: {exc}; provider={provider_used}"',
                "generate_strategy_draft contains unrecognized branch-specific behavior",
            ),
            (
                "incompatible retained context helper",
                "return AIStrategySpec.model_validate(data)",
                "return spec",
                "strategy schema/context helpers do not satisfy",
            ),
            (
                "uninitialized context data",
                "    data = spec.model_dump()\n",
                "",
                "strategy schema/context helpers do not satisfy",
            ),
            (
                "extra required hosted schema field",
                '    strategy_family: str = "TREND"\n',
                '    strategy_family: str = "TREND"\n    compliance_tag: str\n',
                "strategy schema/context helpers do not satisfy",
            ),
        )
        for label, old, new, message in unsafe_cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                altered = legacy_two.replace(old, new, 1)
                self.assertNotEqual(altered, legacy_two, f"fixture did not alter {label}")
                root = Path(tmp)
                target = root / "app" / "strategy_ai.py"
                target.parent.mkdir()
                target.write_text(altered, encoding="utf-8")
                with patch.object(migration, "ROOT", root), patch.object(
                    migration, "canonical_text", return_value=CANONICAL_STRATEGY
                ):
                    with self.assertRaisesRegex(migration.MigrationError, message):
                        migration.transform_strategy_ai("fake-commit")
                self.assertEqual(target.read_text(encoding="utf-8"), altered)

        direct_dispatch_old = (
            "async def _generate_with_provider(provider: str, prompt: str) -> dict:\n"
            "    if provider == \"gemini\":\n"
            "        return await _gemini_json(prompt, system_instruction=SYSTEM_PROMPT, model=settings.gemini_strategy_model or settings.gemini_model)\n"
            "    return await _groq_json(prompt, system_instruction=SYSTEM_PROMPT, model=settings.groq_strategy_model or settings.groq_model)\n"
        )
        direct_dispatch_new = (
            "async def _generate_with_provider(provider: str, prompt: str) -> dict:\n"
            "    return await _gemini_json(prompt)\n"
        )
        altered = legacy_two.replace(direct_dispatch_old, direct_dispatch_new, 1)
        self.assertNotEqual(altered, legacy_two, "fixture did not replace the multi-provider router")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "strategy_ai.py"
            target.parent.mkdir()
            target.write_text(altered, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=CANONICAL_STRATEGY
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "_generate_with_provider contains additional or unknown behavior"
                ):
                    migration.transform_strategy_ai("fake-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), altered)

    def test_ai_providers_only_removes_audited_cerebras_adapter(self):
        legacy = """import httpx
from .config import settings

class AIProviderError(RuntimeError):
    pass

async def _cerebras_json(prompt: str, *, timeout: float = 30.0, system_instruction: str | None = None, model: str | None = None) -> dict[str, Any]:
    \"\"\"Call Cerebras' OpenAI-compatible Chat Completions API; never grants execution authority.\"\"\"
    if not settings.cerebras_api_key:
        raise AIProviderError("CEREBRAS_API_KEY is not configured")
    url = "https://api.cerebras.ai/v1/chat/completions"
    payload = {
        "model": model or settings.cerebras_model,
        "messages": [
            {"role": "system", "content": (
                system_instruction or ("You are Atlas Trading OS's research assistant. Analyze supplied research only. "
                "Do not place trades, invent market data, or give guaranteed outcomes. Return JSON only.")
            )},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.cerebras_api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        body = _response_json_object(response, "Cerebras")
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIProviderError("Cerebras response did not contain generated text") from exc
    return _extract_json(text)
"""
        custom = legacy.replace(
            '        body = _response_json_object(response, "Cerebras")',
            '        record_provider_metrics()\n        body = _response_json_object(response, "Cerebras")',
        )
        custom_annotation = legacy.replace("prompt: str", "prompt: audit()")
        custom_prompt = legacy.replace(
            "You are Atlas Trading OS's research assistant. Analyze supplied research only.",
            "You are a branch-specific assistant with extra strategy obligations.",
        )
        for label, source, should_refuse in (
            ("audited", legacy, False),
            ("custom-side-effect", custom, True),
            ("side-effecting-annotation", custom_annotation, True),
            ("custom-system-prompt", custom_prompt, True),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / "app" / "ai_providers.py"
                target.parent.mkdir()
                target.write_text(source, encoding="utf-8")
                with patch.object(migration, "ROOT", root), patch.object(
                    migration, "canonical_text", return_value="from .native_ai import AIProviderError\n"
                ):
                    if should_refuse:
                        with self.assertRaisesRegex(migration.MigrationError, "unrecognized provider-bearing helper _cerebras_json"):
                            migration.transform_ai_providers("canonical-commit")
                        self.assertEqual(target.read_text(encoding="utf-8"), source)
                        continue
                    migration.transform_ai_providers("canonical-commit")
                result = target.read_text(encoding="utf-8")
                self.assertNotIn("_cerebras_json", result)
                self.assertNotRegex(result, migration.PROVIDER_RE)
                ast.parse(result, filename="app/ai_providers.py")

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

    def test_qualified_provider_adapter_requires_a_proven_module_binding(self):
        import_tree = ast.parse("import ai_providers")
        qualified_module = ast.parse(
            "async def sample():\n    await ai_providers._gemini_json(prompt)\n"
        )
        qualified_call = qualified_module.body[0].body[0].value.value.func
        self.assertEqual(
            migration._provider_adapter_name(qualified_call, import_tree),
            "_gemini_json",
        )
        shadowed_tree = ast.parse("import ai_providers\nai_providers = object()")
        self.assertIsNone(migration._provider_adapter_name(qualified_call, shadowed_tree))
        aliased_tree = ast.parse("import ai_providers as providers")
        self.assertIsNone(migration._provider_adapter_name(qualified_call, aliased_tree))
        aliased_call_tree = ast.parse(
            "async def sample():\n    await providers._gemini_json(prompt)\n"
        )
        aliased_call = aliased_call_tree.body[0].body[0].value.value.func
        self.assertEqual(
            migration._provider_adapter_name(aliased_call, aliased_tree),
            "_gemini_json",
        )

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

    def _legacy_strategy_with_retained_code(self, retained_code: str) -> str:
        return (
            "from .config import settings\n"
            "from .ai_providers import AIProviderError, _gemini_json, _groq_json\n"
            "from .strategy_evidence import evidence_for\n"
            'SYSTEM_PROMPT = "legacy provider prompt"\n'
            + retained_code
            + "\nasync def _generate_with_provider(provider, prompt):\n"
            + "    return await _gemini_json(prompt)\n"
            + "async def generate_strategy_draft(*, prompt, name, asset=None, symbol=None, timeframe=None):\n"
            + '    return await _generate_with_provider("gemini", prompt)\n'
        )

    def test_strategy_migration_refuses_deleted_removed_prompt(self):
        legacy = self._legacy_strategy_with_retained_code("del SYSTEM_PROMPT\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "strategy_ai.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=CANONICAL_STRATEGY
            ):
                with self.assertRaisesRegex(migration.MigrationError, "deletes a removed global"):
                    migration.transform_strategy_ai("fake-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_strategy_migration_refuses_prompt_read_before_retained_binding(self):
        retained = 'audit_value = consume(SYSTEM_PROMPT)\naudit_value = SYSTEM_PROMPT = "later audit text"\n'
        legacy = self._legacy_strategy_with_retained_code(retained)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "strategy_ai.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=CANONICAL_STRATEGY
            ):
                with self.assertRaisesRegex(migration.MigrationError, "before a retained binding is available"):
                    migration.transform_strategy_ai("fake-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_strategy_migration_refuses_removed_prompt_added_to_all_later(self):
        retained = '__all__ = []\n__all__.append("SYSTEM_PROMPT")\n'
        legacy = self._legacy_strategy_with_retained_code(retained)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "strategy_ai.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=CANONICAL_STRATEGY
            ):
                with self.assertRaisesRegex(migration.MigrationError, "__all__ exports definitions removed"):
                    migration.transform_strategy_ai("fake-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_strategy_migration_rejects_deleted_try_binding_for_canonical_dependency(self):
        retained = (
            "try:\n"
            "    from .ai_providers import evidence_for\n"
            "finally:\n"
            "    del evidence_for\n"
        )
        legacy = self._legacy_strategy_with_retained_code(retained)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "strategy_ai.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=CANONICAL_STRATEGY
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "canonical strategy generator depends on missing module binding.*evidence_for"
                ):
                    migration.transform_strategy_ai("fake-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_strategy_migration_rejects_missing_canonical_generator_dependency(self):
        legacy = self._legacy_strategy_with_retained_code("AIStrategySpec: object\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "strategy_ai.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=CANONICAL_STRATEGY
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError, "canonical strategy generator depends on missing module binding.*AIStrategySpec"
                ):
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
        self.assertEqual(parsed["description"], "Gemini research is informational only and never enables live trading.")


    def test_schema_checker_rejects_missing_canonical_title_constraints(self):
        tree = ast.parse(CANONICAL_STRATEGY)
        canonical_tree = ast.parse(CANONICAL_STRATEGY)
        spec = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AIStrategySpec")
        title = next(node for node in spec.body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "title")
        title.value = None
        with patch.object(migration, "ROOT", Path("/tmp/atlas-schema-contract-test")):
            self.assertFalse(migration._is_compatible_strategy_schema_and_context(tree, canonical_tree))

    def test_schema_checker_requires_asset_symbol_and_timeframe_binding(self):
        tree = ast.parse(CANONICAL_STRATEGY)
        canonical_tree = ast.parse(CANONICAL_STRATEGY)
        context = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_apply_market_context")
        context.body = [node for node in context.body if not isinstance(node, ast.If)]
        with patch.object(migration, "ROOT", Path("/tmp/atlas-schema-context-test")):
            self.assertFalse(migration._is_compatible_strategy_schema_and_context(tree, canonical_tree))

    def test_exact_schema_contract_rejects_conditional_or_expression_mutation_of_timeframes(self):
        canonical = ast.parse(CANONICAL_STRATEGY)
        cases = (
            "if override:\n    ALLOWED_TIMEFRAMES = {'1m'}\n",
            "ALLOWED_TIMEFRAMES.add('1m')\n",
            "other_timeframes = ALLOWED_TIMEFRAMES\n",
        )
        for mutation in cases:
            with self.subTest(mutation=mutation):
                legacy = ast.parse(CANONICAL_STRATEGY + mutation)
                with patch.object(migration, "ROOT", Path("/tmp/atlas-timeframe-binding-test")):
                    self.assertFalse(
                        migration._is_compatible_strategy_schema_and_context(legacy, canonical)
                    )

    def test_exact_schema_contract_rejects_builtin_shadowing(self):
        legacy = ast.parse("str = int\n" + CANONICAL_STRATEGY)
        canonical = ast.parse(CANONICAL_STRATEGY)
        with patch.object(migration, "ROOT", Path("/tmp/atlas-builtin-shadow-test")):
            self.assertFalse(
                migration._is_compatible_strategy_schema_and_context(legacy, canonical)
            )

    def test_semantically_compatible_optional_schema_extension_is_allowed(self):
        legacy = ast.parse(CANONICAL_STRATEGY)
        canonical = ast.parse(CANONICAL_STRATEGY)
        schema = next(node for node in legacy.body if isinstance(node, ast.ClassDef) and node.name == "AIStrategySpec")
        optional_field = ast.parse("branch_note: str | None = None").body[0]
        validator_index = next(
            index for index, node in enumerate(schema.body)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        schema.body.insert(validator_index, optional_field)
        with patch.object(migration, "ROOT", Path("/tmp/atlas-optional-schema-test")):
            self.assertTrue(
                migration._is_compatible_strategy_schema_and_context(legacy, canonical)
            )

    def test_imported_schema_checks_context_globals_in_strategy_module(self):
        legacy_tree = ast.parse(CANONICAL_STRATEGY)
        canonical_tree = ast.parse(CANONICAL_STRATEGY)

        def bind_context_annotation(tree):
            context = next(
                node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "_apply_market_context"
            )
            context.args.args[0].annotation = ast.Subscript(
                value=ast.Name(id="Annotated", ctx=ast.Load()),
                slice=ast.Tuple(
                    elts=[
                        ast.Name(id="AIStrategySpec", ctx=ast.Load()),
                        ast.Name(id="ContextMarker", ctx=ast.Load()),
                    ],
                    ctx=ast.Load(),
                ),
                ctx=ast.Load(),
            )
            context_index = tree.body.index(context)
            tree.body.insert(
                context_index,
                ast.ImportFrom(
                    module="context_types",
                    names=[ast.alias(name="ContextMarker")],
                    level=1,
                ),
            )
            tree.body.insert(
                context_index,
                ast.ImportFrom(
                    module="typing",
                    names=[ast.alias(name="Annotated")],
                    level=0,
                ),
            )

        bind_context_annotation(legacy_tree)
        bind_context_annotation(canonical_tree)
        legacy_tree.body = [
            node for node in legacy_tree.body
            if not (isinstance(node, ast.ClassDef) and node.name == "AIStrategySpec")
        ]
        insert_at = next(
            index for index, node in enumerate(legacy_tree.body)
            if isinstance(node, ast.FunctionDef) and node.name == "_apply_market_context"
        )
        legacy_tree.body.insert(
            insert_at,
            ast.ImportFrom(
                module="schema_models",
                names=[ast.alias(name="AIStrategySpec")],
                level=1,
            ),
        )

        source_tree = ast.parse(CANONICAL_STRATEGY)
        schema_nodes = [
            node for node in source_tree.body
            if (
                isinstance(node, ast.ImportFrom)
                and node.module not in {"native_ai", "strategy_evidence"}
            )
            or (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "ALLOWED_TIMEFRAMES"
                    for target in node.targets
                )
            )
            or (
                isinstance(node, ast.ClassDef)
                and node.name in {"StrategyCondition", "AIStrategySpec"}
            )
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            app_dir.mkdir()
            (app_dir / "strategy_ai.py").write_text(
                ast.unparse(legacy_tree) + "\n", encoding="utf-8"
            )
            (app_dir / "schema_models.py").write_text(
                ast.unparse(ast.Module(body=schema_nodes, type_ignores=[])) + "\n",
                encoding="utf-8",
            )
            (app_dir / "context_types.py").write_text(
                "class ContextMarker: pass\n", encoding="utf-8"
            )
            with patch.object(migration, "ROOT", root):
                self.assertTrue(
                    migration._is_compatible_strategy_schema_and_context(
                        legacy_tree, canonical_tree
                    )
                )

    def test_exact_schema_contract_rejects_missing_canonical_validator(self):
        legacy = ast.parse(CANONICAL_STRATEGY)
        canonical = ast.parse(CANONICAL_STRATEGY)
        schema = next(
            node for node in legacy.body
            if isinstance(node, ast.ClassDef) and node.name == "AIStrategySpec"
        )
        schema.body = [
            node for node in schema.body
            if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "validate_timeframes")
        ]
        self.assertFalse(
            migration._is_compatible_strategy_schema_and_context(legacy, canonical)
        )

    def test_exact_schema_contract_accepts_proven_pydantic_basemodel_alias(self):
        legacy = ast.parse(CANONICAL_STRATEGY)
        canonical = ast.parse(CANONICAL_STRATEGY)
        pydantic_import = next(
            node for node in legacy.body
            if isinstance(node, ast.ImportFrom) and node.module == "pydantic"
        )
        pydantic_import.names.append(ast.alias(name="BaseModel", asname="Model"))
        schema = next(
            node for node in legacy.body
            if isinstance(node, ast.ClassDef) and node.name == "AIStrategySpec"
        )
        schema.bases[0] = ast.Name(id="Model", ctx=ast.Load())
        self.assertTrue(
            migration._is_compatible_strategy_schema_and_context(legacy, canonical)
        )

    def test_exact_schema_contract_rejects_basemodel_alias_shadowed_by_native_import(self):
        legacy = ast.parse(CANONICAL_STRATEGY)
        canonical = ast.parse(CANONICAL_STRATEGY)
        pydantic_import = next(
            node for node in legacy.body
            if isinstance(node, ast.ImportFrom) and node.module == "pydantic"
        )
        pydantic_import.names.append(ast.alias(name="BaseModel", asname="NativeAIError"))
        schema = next(
            node for node in legacy.body
            if isinstance(node, ast.ClassDef) and node.name == "AIStrategySpec"
        )
        schema.bases[0] = ast.Name(id="NativeAIError", ctx=ast.Load())
        self.assertFalse(
            migration._is_compatible_strategy_schema_and_context(legacy, canonical)
        )

    def test_exact_schema_contract_rejects_market_binding_before_model_dump(self):
        legacy = ast.parse(CANONICAL_STRATEGY)
        canonical = ast.parse(CANONICAL_STRATEGY)
        context = next(
            node for node in legacy.body
            if isinstance(node, ast.FunctionDef) and node.name == "_apply_market_context"
        )
        context.body[0], context.body[1] = context.body[1], context.body[0]
        self.assertFalse(
            migration._is_compatible_strategy_schema_and_context(legacy, canonical)
        )

    def test_exact_schema_contract_rejects_import_time_class_body_risk_mutation(self):
        legacy = ast.parse(
            CANONICAL_STRATEGY
            + "\nclass BranchOverride:\n    settings.risk_per_trade = 0.04\n"
        )
        canonical = ast.parse(CANONICAL_STRATEGY)
        with patch.object(migration, "ROOT", Path("/tmp/atlas-class-body-risk-test")):
            self.assertFalse(
                migration._is_compatible_strategy_schema_and_context(legacy, canonical)
            )

    def test_exact_schema_contract_rejects_wildcard_import_after_validated_class(self):
        legacy = ast.parse(CANONICAL_STRATEGY + "\nfrom .branch_overrides import *\n")
        canonical = ast.parse(CANONICAL_STRATEGY)
        with patch.object(migration, "ROOT", Path("/tmp/atlas-wildcard-schema-test")):
            self.assertFalse(
                migration._is_compatible_strategy_schema_and_context(legacy, canonical)
            )

    def test_exact_schema_shortcut_rejects_rebound_strategy_condition(self):
        legacy_tree = ast.parse(CANONICAL_STRATEGY)
        canonical_tree = ast.parse(CANONICAL_STRATEGY)
        condition_index = next(
            index for index, node in enumerate(legacy_tree.body)
            if isinstance(node, ast.ClassDef) and node.name == "StrategyCondition"
        )
        legacy_tree.body[condition_index] = ast.parse("StrategyCondition = dict").body[0]
        with patch.object(migration, "ROOT", Path("/tmp/atlas-strategy-condition-test")):
            self.assertFalse(
                migration._is_compatible_strategy_schema_and_context(legacy_tree, canonical_tree)
            )

    def test_schema_checker_rejects_classvar_for_required_strategy_field(self):
        legacy = CANONICAL_STRATEGY.replace(
            '    asset: Literal["crypto", "forex", "commodity"]',
            '    asset: ClassVar[Literal["crypto", "forex", "commodity"]]',
        )
        self.assertNotEqual(legacy, CANONICAL_STRATEGY)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(migration, "ROOT", Path(tmp)):
                self.assertFalse(migration._is_compatible_strategy_schema_and_context(
                    ast.parse(legacy), ast.parse(CANONICAL_STRATEGY)
                ))

    def test_metadata_checker_rejects_provider_secret_refs_but_allows_descriptive_mentions(self):
        self.assertTrue(migration._metadata_has_active_provider_wiring(
            '{"secretRef": "atlas-openai-api-key"}'
        ))
        self.assertTrue(migration._metadata_has_active_provider_wiring(
            '{"secretRef": "OPENAI_API_KEY"}'
        ))
        self.assertEqual(
            json.loads(migration.scrub_ancillary_text("metadata.json", '{"secretRef": "OPENAI_API_KEY"}')),
            {},
        )
        self.assertTrue(migration._metadata_has_active_provider_wiring(
            '{"credentials": "OPENAI_API_KEY=example"}'
        ))
        self.assertFalse(migration._metadata_has_active_provider_wiring(
            '{"description": "Historical Gemini and Groq review note"}'
        ))
        self.assertTrue(migration._metadata_has_active_provider_wiring(
            '{"env": [{"name": "OPENAI_API_KEY", "value": "example"}]}'
        ))
        structured = json.loads(migration.scrub_ancillary_text(
            "metadata.json",
            '{"env": [{"name": "OPENAI_API_KEY", "value": "example"}]}'
        ))
        self.assertEqual(structured, {"env": []})

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




    def test_module_scope_binding_analysis_detects_starred_targets_but_not_lambda_local_walrus(self):
        starred_loop = ast.parse("for *json, rest in [(1, 2)]:\n    pass\n").body[0]
        self.assertIn("json", migration._module_scope_maybe_binds(starred_loop))
        self.assertIn("rest", migration._module_scope_maybe_binds(starred_loop))

        local_walrus = ast.parse(
            "def retained_helper():\n"
            "    return (lambda: (json := None))()\n"
        ).body[0]
        self.assertEqual(migration._module_scope_maybe_binds(local_walrus), {"retained_helper"})

        empty_loop = ast.parse("for json in []:\n    pass\n").body[0]
        self.assertNotIn("json", migration._module_scope_maybe_binds(empty_loop))

    def test_provider_facade_refuses_starred_loop_rebinding_of_canonical_json_import(self):
        legacy = (
            "import json\n\n"
            "class AIProviderError(RuntimeError):\n"
            "    pass\n\n"
            "for *json, rest in [(1, 2)]:\n"
            "    pass\n"
        )
        canonical = (
            "import json\n"
            "from .native_ai import AIProviderError\n"
            "def _extract_json(text):\n"
            "    return json.loads(text)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            target.parent.mkdir()
            target.write_text(legacy, encoding="utf-8")
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError,
                    "branch-specific definition 'json' that collides with a canonical name",
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_star_import_rejects_deleted_name_still_declared_in_all(self):
        legacy = """from .branch_helpers import *

class AIProviderError(RuntimeError):
    pass

def retained_helper():
    return branch_helper()
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app" / "ai_providers.py"
            helper = root / "app" / "branch_helpers.py"
            target.parent.mkdir()
            helper.write_text(
                "from .native_ai import NativeAIError\n"
                "__all__ = ['NativeAIError', 'branch_helper']\n"
                "def branch_helper():\n"
                "    return True\n"
                "del NativeAIError\n",
                encoding="utf-8",
            )
            target.write_text(legacy, encoding="utf-8")
            canonical = (
                "from .native_ai import AIProviderError, NativeAIError\n"
            )
            with patch.object(migration, "ROOT", root), patch.object(
                migration, "canonical_text", return_value=canonical
            ):
                with self.assertRaisesRegex(
                    migration.MigrationError,
                    "declares deleted name\\(s\\) in __all__",
                ):
                    migration.transform_ai_providers("canonical-commit")
            self.assertEqual(target.read_text(encoding="utf-8"), legacy)

    def test_strategy_migration_accepts_dependency_bound_in_both_try_and_except_paths(self):
        legacy = self._legacy_strategy_with_retained_code("")
        legacy = legacy.replace(
            "from .strategy_evidence import evidence_for\n",
            "try:\n"
            "    from .strategy_evidence import evidence_for\n"
            "except ImportError:\n"
            "    from .strategy_evidence import evidence_for\n",
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
            self.assertIn("except ImportError:", result)
            self.assertIn("from .native_ai import NativeAIError, compose_strategy_spec", result)
            self.assertNotRegex(result, migration.PROVIDER_RE)


if __name__ == "__main__":
    unittest.main()
