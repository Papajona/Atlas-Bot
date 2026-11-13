from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_walk_forward_result_keeps_compact_full_sample_statistics_only():
    source = (ROOT / "app" / "research_validation.py").read_text(encoding="utf-8")
    start = source.index("def fixed_signal_walk_forward_oos(")
    end = source.index("\ndef regime_conditioned_performance", start)
    body = source[start:end]

    assert '"full_sample_statistics": full_sample_statistics' in body
    assert '"oos_return_sample": values[-5000:].tolist()' in body
    assert '"oos_return_sample_full"' not in body


def test_research_engine_never_retains_full_return_lists_per_candidate():
    source = (ROOT / "app" / "research_engine.py").read_text(encoding="utf-8")

    assert "oos_return_sample_full" not in source
    assert '"full_sample_statistics": variant_oos.get("full_sample_statistics", {})' in source
    assert 'full_stats.get("annualized_sharpe")' in source
    assert "full_sample_statistics = oos.get(\"full_sample_statistics\")" in source
