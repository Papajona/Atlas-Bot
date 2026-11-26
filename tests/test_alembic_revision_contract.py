from pathlib import Path

import pytest

from scripts.alembic_revision_contract import expected_alembic_heads


ROOT = Path(__file__).resolve().parents[1]


def test_repository_alembic_graph_has_the_expected_single_head():
    assert expected_alembic_heads(ROOT / "alembic" / "versions") == {
        "0065_tron_sweep_txid_casefold_unique"
    }


def test_alembic_revision_contract_supports_multiple_heads_and_merge_revisions(tmp_path):
    (tmp_path / "0001_root.py").write_text(
        "revision = '0001_root'\ndown_revision = None\n", encoding="utf-8"
    )
    (tmp_path / "0002_left.py").write_text(
        "revision = '0002_left'\ndown_revision = '0001_root'\n", encoding="utf-8"
    )
    (tmp_path / "0002_right.py").write_text(
        "revision = '0002_right'\ndown_revision = '0001_root'\n", encoding="utf-8"
    )
    assert expected_alembic_heads(tmp_path) == {"0002_left", "0002_right"}
    (tmp_path / "0003_merge.py").write_text(
        "revision = '0003_merge'\ndown_revision = ('0002_left', '0002_right')\n",
        encoding="utf-8",
    )
    assert expected_alembic_heads(tmp_path) == {"0003_merge"}


def test_alembic_revision_contract_rejects_missing_parent(tmp_path):
    (tmp_path / "0002_orphan.py").write_text(
        "revision = '0002_orphan'\ndown_revision = '0001_missing'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing parents"):
        expected_alembic_heads(tmp_path)


def test_alembic_revision_contract_rejects_cycles(tmp_path):
    (tmp_path / "0001_a.py").write_text(
        "revision = '0001_a'\ndown_revision = '0002_b'\n", encoding="utf-8"
    )
    (tmp_path / "0002_b.py").write_text(
        "revision = '0002_b'\ndown_revision = '0001_a'\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="cycle"):
        expected_alembic_heads(tmp_path)



def test_integrated_alembic_chain_has_the_expected_single_head():
    migration = (
        ROOT / "alembic" / "versions" / "0065_tron_sweep_txid_casefold_unique.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "0065_tron_sweep_txid_casefold_unique"' in migration
    assert 'down_revision = "0064_strategy_outcome_recent_index"' in migration
    assert expected_alembic_heads(ROOT / "alembic" / "versions") == {
        "0065_tron_sweep_txid_casefold_unique"
    }


def test_integrated_migration_chain_is_linear_and_keeps_all_feature_revisions():
    versions = ROOT / "alembic" / "versions"
    expected = {
        "0060_customer_identity_unmapped_index": "0059_research_integrity_controls",
        "0061_strategy_outcome_fill_aggregation": "0060_customer_identity_unmapped_index",
        "0062_customer_benchmark_capture": "0061_strategy_outcome_fill_aggregation",
        "0063_webhook_events_created_at_index": "0062_customer_benchmark_capture",
        "0064_strategy_outcome_recent_index": "0063_webhook_events_created_at_index",
        "0065_tron_sweep_txid_casefold_unique": "0064_strategy_outcome_recent_index",
    }
    for revision, parent in expected.items():
        migration = next(versions.glob(f"*{revision}.py")).read_text(encoding="utf-8")
        assert f'revision = "{revision}"' in migration
        assert f'down_revision = "{parent}"' in migration
    assert expected_alembic_heads(versions) == {"0064_strategy_outcome_recent_index"}


def test_production_provisioning_scripts_require_the_current_alembic_head():
    expected = "0065_tron_sweep_txid_casefold_unique"
    for path in (
        ROOT / "scripts" / "provision_atlas_api_runtime.sql",
        ROOT / "scripts" / "provision_atlas_audit_reader.sql",
    ):
        source = path.read_text(encoding="utf-8")
        assert expected in source
        assert "0064_strategy_outcome_recent_index" not in source
