from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_adaptive_metadata_reads_only_swallow_expected_file_or_json_errors():
    src = (ROOT / "app/adaptive_bot.py").read_text()
    streak = src[src.index("def _read_streak"):src.index("def _read_meta")]
    meta = src[src.index("def _read_meta"):src.index("def _write_json_atomic")]
    assert "except (OSError, UnicodeDecodeError, json.JSONDecodeError)" in streak
    assert "except (OSError, UnicodeDecodeError, json.JSONDecodeError)" in meta
    assert "except Exception" not in streak
    assert "except Exception" not in meta


def test_prior_trial_label_database_errors_propagate_instead_of_disabling_leakage_history():
    src = (ROOT / "app/daily_research.py").read_text()
    start = src.index("async def _prior_trial_labels")
    end = src.index("\n\nasync def _persist_research_run", start)
    block = src[start:end]
    assert "except Exception" not in block
    assert "await db.execute(select(ResearchRun.research_json)" in block
    assert "except (TypeError, ValueError, AttributeError)" in block


def test_per_episode_learning_failures_are_persisted_and_counted():
    src = (ROOT / "app/trade_learning.py").read_text()
    start = src.index("async def")
    marker = 'ep.replay_status = "ERROR"'
    assert marker in src
    assert "ep.replay_error = str(exc)" in src
    assert "failed += 1" in src
