from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd
import pytest

from app.decision_outcome_research import evaluate_decision_outcome_labels_once
from app.execution_aware_research import simulate_execution_aware_strategy
from app.market_sessions import normalize_session_costs_bps, session_cost_series
from app.research_trial_archive import compute_trial_ledger_integrity
from app.research_validation import parameter_plateau_score
from app.trading_core import triple_barrier


def test_trial_ledger_hash_and_hmac_are_deterministic_and_bind_sequence_and_payload():
    payload = {
        "trial_key": "a" * 64,
        "trial_label": "trend@config",
        "asset_class": "forex",
        "venue": "oanda",
        "annualized_sharpe": 0.42,
    }
    first = compute_trial_ledger_integrity(
        payload, sequence=1, previous_entry_sha256="", signing_key="k" * 32
    )
    replay = compute_trial_ledger_integrity(
        payload, sequence=1, previous_entry_sha256="", signing_key="k" * 32
    )
    changed_sequence = compute_trial_ledger_integrity(
        payload, sequence=2, previous_entry_sha256=first[0], signing_key="k" * 32
    )
    changed_payload = compute_trial_ledger_integrity(
        {**payload, "annualized_sharpe": 0.43},
        sequence=1, previous_entry_sha256="", signing_key="k" * 32
    )

    assert first == replay
    assert first[2] == "HMAC_SIGNED"
    assert first[1] and len(first[1]) == 64
    assert changed_sequence[0] != first[0]
    assert changed_payload[0] != first[0]


def test_trial_ledger_explicitly_marks_hash_only_when_no_external_key_exists():
    entry, signature, status = compute_trial_ledger_integrity(
        {"trial_key": "b" * 64}, sequence=1, previous_entry_sha256=""
    )
    assert len(entry) == 64
    assert signature is None
    assert status == "HASH_CHAIN_UNSIGNED"


def test_parameter_plateau_uses_oos_values_and_counts_invalid_variants_against_plateau():
    values = [1.0, 0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.1, 0.05]
    result = parameter_plateau_score(
        {"variants": [{"oos_status": "OK", "oos_sharpe": value} for value in values]},
        metric="oos_sharpe",
        minimum_valid_variants=7,
        minimum_plateau_ratio=0.50,
    )
    assert result["status"] == "OK"
    assert result["robust"] is True
    assert result["tested_variants"] == 9
    assert result["plateau_ratio"] == pytest.approx(5 / 9)

    invalid = parameter_plateau_score(
        {"variants": [{"oos_status": "OK", "oos_sharpe": value} for value in values] + [{"oos_status": "INSUFFICIENT_DATA", "oos_sharpe": 2.0}]},
        metric="oos_sharpe",
        minimum_valid_variants=7,
        minimum_plateau_ratio=0.50,
    )
    assert invalid["status"] == "OK"
    assert invalid["robust"] is False
    assert invalid["valid_variants"] == 9
    assert invalid["tested_variants"] == 10


def test_triple_barrier_uses_next_open_as_entry_not_signal_bar_close():
    idx = pd.date_range("2026-01-01", periods=4, freq="h", tz="UTC")
    close = pd.Series([120.0, 100.0, 102.0, 103.0], index=idx)
    vol = pd.Series([0.01] * 4, index=idx)
    high = pd.Series([121.0, 102.0, 102.5, 104.0], index=idx)
    low = pd.Series([119.0, 99.5, 100.0, 101.0], index=idx)
    opens = pd.Series([120.0, 100.0, 102.0, 103.0], index=idx)

    labels = triple_barrier(
        close, vol, high, low, pt=1.0, sl=1.0, max_hold=2,
        entry_open=opens, round_trip_cost_bps=0.0,
    )
    # The old close[i]-anchored upper barrier would be 121.2 and never touch.
    # The execution-aligned open[i+1] barrier is 101 and is touched on entry bar.
    assert labels.iloc[0] == 1.0


def test_triple_barrier_costs_widen_barriers_and_timeout_dead_zone():
    idx = pd.date_range("2026-01-01", periods=4, freq="h", tz="UTC")
    close = pd.Series([120.0, 100.0, 101.9, 103.0], index=idx)
    vol = pd.Series([0.01] * 4, index=idx)
    high = pd.Series([121.0, 101.5, 102.9, 104.0], index=idx)
    low = pd.Series([119.0, 99.5, 100.0, 101.0], index=idx)
    opens = pd.Series([120.0, 100.0, 102.0, 103.0], index=idx)

    gross_label = triple_barrier(
        close, vol, high, low, pt=1.0, sl=1.0, max_hold=2,
        entry_open=opens, round_trip_cost_bps=0.0,
    )
    net_label = triple_barrier(
        close, vol, high, low, pt=1.0, sl=1.0, max_hold=2,
        entry_open=opens, round_trip_cost_bps=200.0,
    )
    assert gross_label.iloc[0] == 1.0
    assert net_label.iloc[0] == 0.0


def test_triple_barrier_uses_entry_session_costs_for_each_label():
    idx = pd.date_range("2026-01-01", periods=4, freq="h", tz="UTC")
    close = pd.Series([120.0, 100.0, 101.9, 103.0], index=idx)
    vol = pd.Series([0.01] * 4, index=idx)
    high = pd.Series([121.0, 101.5, 102.9, 104.0], index=idx)
    low = pd.Series([119.0, 99.0, 100.0, 101.0], index=idx)
    opens = pd.Series([120.0, 100.0, 102.0, 103.0], index=idx)
    round_trip = pd.Series([0.0, 200.0, 0.0, 0.0], index=idx)

    labels = triple_barrier(
        close, vol, high, low, pt=1.0, sl=1.0, max_hold=2,
        entry_open=opens, round_trip_cost_bps=round_trip,
    )
    assert labels.iloc[0] == 0.0


def test_verified_session_schedule_is_applied_per_bar_in_the_execution_simulator():
    idx = pd.DatetimeIndex([
        pd.Timestamp("2026-01-05T08:00:00Z"),
        pd.Timestamp("2026-01-05T14:00:00Z"),
    ])
    schedule = {
        "LONDON": 10.0,
        "NEW_YORK": 20.0,
        "NEW_YORK_LATE": 30.0,
        "OTHER": 40.0,
    }
    normalized = normalize_session_costs_bps(schedule)
    series = session_cost_series(idx, normalized)
    assert series.tolist() == [10.0, 20.0]

    frame = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0]},
        index=idx[:1],
    )
    result = simulate_execution_aware_strategy(
        frame,
        pd.Series([1.0], index=frame.index),
        pd.Series([2.0], index=frame.index),
        stop_atr=2.0,
        take_profit_atr=3.0,
        transaction_cost_bps=5.0,
        session_costs_bps=schedule,
        carry_bps_per_bar=0.0,
    )
    assert result["net_returns"].iloc[0] == pytest.approx(-0.002)
    assert result["audit"]["session_cost_model_complete"] is True


def _outcome_label(position: int) -> dict:
    timestamp = pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(hours=position)
    decision = "NO_TRADE" if position % 2 else "TRADE"
    return {
        "record_type": "adaptive_decision_outcome_label",
        "label_version": "decision-outcome-v1",
        "status": "VALID",
        "decision_id": f"once-{position}",
        "original_decision": decision,
        "mode": "PAPER",
        "asset": "crypto",
        "symbol": "BTC/USDT",
        "exchange": "bybit",
        "timeframe": "1h",
        "market_timestamp": timestamp.isoformat(),
        "horizons_bars": ["1"],
        "candidate_side": "buy",
        "strategy": "trend",
        "decision_stage": "adaptive_model_confirmation",
        "decision_reason_code": "model_veto",
        "cost_source": "configured_asset_profile",
        "cost_bps_per_side": 3.5,
        "carry_bps_per_bar": 0.25,
        "regime": "TREND_UP",
        "outcomes": {
            "1": {
                "status": "VALID",
                "horizon_bars": 1,
                "candidate_side_net_return_bps": float(position - 8),
                "outcome_bar_open": (timestamp + pd.Timedelta(hours=1)).isoformat(),
            }
        },
    }


def test_cost_breakeven_report_uses_per_bar_session_costs_and_validates_inputs():
    from app.research_validation import cost_breakeven_report

    gross = np.full(30, 0.01, dtype=float)
    turnover = np.ones(30, dtype=float)
    session_costs = np.full(30, 10.0, dtype=float)
    report = cost_breakeven_report(
        gross,
        turnover,
        taker_bps=5.0,
        slippage_bps=5.0,
        bars_per_year=252,
        variable_cost_bps_per_bar=session_costs,
    )
    assert report["status"] == "OK"
    assert report["session_variable_costs_applied"] is True
    assert report["assumed_cost_bps"] == pytest.approx(10.0)
    assert report["annual_variable_cost_drag"] == pytest.approx(0.001 * 252)

    invalid = cost_breakeven_report(
        gross,
        turnover,
        taker_bps="not-a-number",
        slippage_bps=5.0,
        bars_per_year=252,
    )
    assert invalid["status"] == "INVALID_COST_INPUT"


def test_cost_stress_rejects_misaligned_per_bar_costs():
    from app.research_validation import cost_breakeven_report

    report = cost_breakeven_report(
        np.full(30, 0.01),
        np.ones(30),
        taker_bps=5.0,
        slippage_bps=5.0,
        bars_per_year=252,
        variable_cost_bps_per_bar=np.full(29, 10.0),
    )
    assert report["status"] == "INVALID_COST_ALIGNMENT"
    assert report["input_lengths"]["gross_returns"] == 30
    assert report["input_lengths"]["variable_cost_bps_per_bar"] == 29


def test_session_cost_schedule_rejects_missing_or_nonfinite_sessions():
    valid = {
        "LONDON": 10.0,
        "NEW_YORK": 20.0,
        "NEW_YORK_LATE": 30.0,
        "OTHER": 40.0,
    }
    with pytest.raises(ValueError):
        normalize_session_costs_bps({**valid, "OTHER": float("nan")})
    with pytest.raises(ValueError):
        normalize_session_costs_bps({"LONDON": 1.0})


def test_decision_outcome_holdout_is_consumed_once_across_subsequent_calls(monkeypatch):
    from app import db as db_module

    registry: dict[str, object] = {}

    class _Nested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeSession:
        def __init__(self):
            self.pending = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, model, key):
            return registry.get(key)

        def begin_nested(self):
            return _Nested()

        def add(self, row):
            self.pending = row

        async def flush(self):
            return None

        async def commit(self):
            registry[self.pending.scope_sha256] = self.pending

    monkeypatch.setattr(db_module, "SessionLocal", lambda: _FakeSession())
    labels = [_outcome_label(i) for i in range(20)]

    first = asyncio.run(evaluate_decision_outcome_labels_once(
        labels, min_group_samples=3, min_unique_timestamps=10
    ))
    second = asyncio.run(evaluate_decision_outcome_labels_once(
        labels + [_outcome_label(20)], min_group_samples=3, min_unique_timestamps=10
    ))

    assert first["status"] == "HOLDOUT_CONSUMED_AND_RELEASED"
    assert first["metrics_released"] is True
    assert first["report"]["out_of_sample"][0]["n_holdout"] == 6
    assert len(registry) == 1
    assert second["status"] == "HOLDOUT_ALREADY_CONSUMED"
    assert second["metrics_released"] is False
    assert second["report"] is None


def test_research_integrity_models_and_migration_are_present_and_write_once():
    from app.db import ResearchHoldoutRegistry, ResearchTrialLedgerHead, ResearchTrialArchive
    from pathlib import Path

    assert ResearchTrialLedgerHead.__tablename__ == "research_trial_ledger_head"
    assert ResearchHoldoutRegistry.__tablename__ == "research_holdout_registry"
    assert "ledger_entry_sha256" in ResearchTrialArchive.__table__.columns
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions" / "0059_research_integrity_controls.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "0059_research_integrity_controls"' in migration
    assert "atlas_research_holdout_registry_immutable" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "REVOKE ALL PRIVILEGES" in migration



def test_trial_ledger_assurance_mode_and_key_id_are_authenticated_and_rotation_works():
    from types import SimpleNamespace
    from app.research_trial_archive import (
        _LEDGER_PAYLOAD_FIELDS,
        _verify_row_integrity,
    )

    payload = {field: None for field in _LEDGER_PAYLOAD_FIELDS}
    payload.update({
        "trial_key": "a" * 64,
        "trial_label": "variant:t1.20:r0.80@config",
        "config_sha256": "b" * 64,
        "results_sha256": "c" * 64,
        "archive_status": "RECORDED_UNVERIFIED",
    })
    old_key = b"o" * 32
    new_key = b"n" * 32
    entry_hash, signature, mode = compute_trial_ledger_integrity(
        payload, sequence=1, previous_entry_sha256="",
        signing_key=old_key, signing_key_id="key-2026-01",
    )
    row = SimpleNamespace(
        **payload,
        ledger_sequence=1,
        ledger_previous_sha256="",
        ledger_entry_sha256=entry_hash,
        ledger_hmac_sha256=signature,
        ledger_hmac_key_id="key-2026-01",
        ledger_status=mode,
    )
    valid, verifiable, reason = _verify_row_integrity(
        row, keyring={"key-2026-01": old_key, "key-2026-11": new_key},
    )
    assert (valid, verifiable, reason) == (True, True, "OK")

    # New rows use the rotated active key; old and new entries verify together.
    rotated_hash, rotated_sig, rotated_mode = compute_trial_ledger_integrity(
        {**payload, "trial_key": "d" * 64}, sequence=2,
        previous_entry_sha256=entry_hash, signing_key=new_key,
        signing_key_id="key-2026-11",
    )
    rotated_row = SimpleNamespace(
        **{**payload, "trial_key": "d" * 64},
        ledger_sequence=2,
        ledger_previous_sha256=entry_hash,
        ledger_entry_sha256=rotated_hash,
        ledger_hmac_sha256=rotated_sig,
        ledger_hmac_key_id="key-2026-11",
        ledger_status=rotated_mode,
    )
    assert _verify_row_integrity(
        rotated_row, keyring={"key-2026-01": old_key, "key-2026-11": new_key},
    ) == (True, True, "OK")

    # Clearing HMAC metadata and changing the status cannot downgrade a signed row:
    # assurance mode and key identity are already bound into the entry SHA.
    downgraded = SimpleNamespace(
        **payload,
        ledger_sequence=1,
        ledger_previous_sha256="",
        ledger_entry_sha256=entry_hash,
        ledger_hmac_sha256=None,
        ledger_hmac_key_id=None,
        ledger_status="HASH_CHAIN_UNSIGNED",
    )
    assert _verify_row_integrity(downgraded, keyring={})[0] is False


def test_unsigned_entry_cannot_be_relabelled_as_hmac_signed():
    from types import SimpleNamespace
    from app.research_trial_archive import _LEDGER_PAYLOAD_FIELDS, _verify_row_integrity

    payload = {field: None for field in _LEDGER_PAYLOAD_FIELDS}
    payload.update({"trial_key": "e" * 64, "trial_label": "trend"})
    entry, signature, mode = compute_trial_ledger_integrity(
        payload, sequence=1, previous_entry_sha256="",
    )
    row = SimpleNamespace(
        **payload,
        ledger_sequence=1,
        ledger_previous_sha256="",
        ledger_entry_sha256=entry,
        ledger_hmac_sha256=signature,
        ledger_hmac_key_id=None,
        ledger_status="HMAC_SIGNED",
    )
    assert _verify_row_integrity(row, keyring={"k": b"k" * 32})[0] is False


def test_sqlite_trial_archive_uses_serialized_write_transaction():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    archive = (root / "app" / "research_trial_archive.py").read_text(encoding="utf-8")
    db_source = (root / "app" / "db.py").read_text(encoding="utf-8")
    assert 'BEGIN IMMEDIATE' in archive
    assert '"timeout": 30' in db_source
    assert '.with_for_update()' in archive


def test_trial_archive_append_uses_bounded_head_check_not_full_history_scan():
    from pathlib import Path
    source = (
        Path(__file__).resolve().parents[1]
        / "app" / "research_trial_archive.py"
    ).read_text(encoding="utf-8")
    append = source[source.index("async def archive_research_trials("):]
    assert "_verify_trial_ledger_head_in_session" in append
    assert "verify_research_trial_ledger()" not in append



def test_large_complete_label_history_fails_closed_without_truncation(monkeypatch):
    import json
    from app import db as db_module
    from app.decision_outcome_research import (
        DecisionOutcomeHistoryResourceLimit,
        load_all_decision_outcome_labels,
    )

    class _Rows:
        def all(self):
            return [
                (1, json.dumps(_outcome_label(1))),
                (2, json.dumps(_outcome_label(2))),
            ]

    class _Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _query):
            return _Rows()

    monkeypatch.setattr(db_module, "SessionLocal", _Db)
    with pytest.raises(DecisionOutcomeHistoryResourceLimit, match="holdout was not consumed"):
        asyncio.run(load_all_decision_outcome_labels(batch_size=100, max_records=1))


def test_holdout_route_checks_consumption_before_loading_history():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    route = source.split('@app.post("/api/research/decision-outcomes/holdout-once")', 1)[1]
    route = route.split('@app.post("/api/research/backtest-all")', 1)[0]
    assert route.index("decision_outcome_holdout_status()") < route.index("load_all_decision_outcome_labels(")
    assert "DecisionOutcomeHistoryResourceLimit" in route
    assert "no holdout was consumed" in route



def test_session_cost_series_preserves_naive_utc_index_for_oos_alignment():
    index = pd.date_range("2026-01-05T08:00:00", periods=3, freq="h")
    series = session_cost_series(index, _schedule())
    assert series.index.equals(index)
    positions = pd.Series([1.0, 1.0, 0.0], index=index)
    returns = pd.Series([0.0, 0.0, 0.0], index=index)
    costs = costed_execution_returns(
        positions,
        returns,
        transaction_cost_bps=series,
        carry_bps_per_bar=None,
    )
    assert costs.index.equals(index)
    assert costs.iloc[0] < 0


def test_thin_decision_arm_does_not_consume_one_time_holdout(monkeypatch):
    from app import db as db_module
    from app.decision_outcome_research import evaluate_decision_outcome_labels_once

    registry: dict[str, object] = {}

    class _Nested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeSession:
        def __init__(self):
            self.pending = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, model, key):
            return registry.get(key)

        def begin_nested(self):
            return _Nested()

        def add(self, row):
            self.pending = row

        async def flush(self):
            return None

        async def commit(self):
            registry[self.pending.scope_sha256] = self.pending

    monkeypatch.setattr(db_module, "SessionLocal", lambda: _FakeSession())
    labels = []
    for position in range(80):
        row = _outcome_label(position)
        row["original_decision"] = "NO_TRADE" if position == 0 else "TRADE"
        labels.append(row)

    result = asyncio.run(evaluate_decision_outcome_labels_once(
        labels, holdout_fraction=0.30, min_group_samples=10, min_unique_timestamps=10,
    ))
    assert result["status"] == "INSUFFICIENT_HOLDOUT"
    assert result["metrics_released"] is False
    assert result["consumed"] is False
    assert registry == {}
