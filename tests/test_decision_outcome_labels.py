from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
import pytest

from app.decision_outcomes import (
    DEFAULT_HORIZONS_BARS,
    LABEL_VERSION,
    MAX_DECISION_OUTCOME_LABEL_BYTES,
    MAX_DECISION_OUTCOME_LABEL_RECORDS,
    OutcomeLabelError,
    _decision_outcome_label_capacity,
    build_decision_outcome_label,
)


def _frame(*, as_of=None):
    index = pd.date_range("2026-01-01T00:00:00Z", periods=10, freq="h")
    close = np.array([100.0, 101.0, 102.0, 104.0, 103.0, 105.0, 106.0, 107.0, 108.0, 109.0])
    df = pd.DataFrame(
        {
            "open": close - 0.25,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(len(close), 1000.0),
        },
        index=index,
    )
    fetched_at = as_of if as_of is not None else (index[-1] + pd.Timedelta(hours=1))
    canonical = df.reset_index().to_csv(index=False, float_format="%.17g", lineterminator="\n")
    df.attrs["data_provenance"] = {
        "source": "ccxt_ohlcv",
        "symbol": "BTC/USDT:USDT",
        "exchange": "bybit",
        "timeframe": "1h",
        "fetched_at_utc": fetched_at.isoformat(),
        "row_count": len(df),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    return df


def _decision(timestamp=None):
    timestamp = timestamp if timestamp is not None else pd.Timestamp("2026-01-01T02:00:00Z")
    return {
        "record_type": "adaptive_trade_decision",
        "decision_id": "decision-001",
        "snapshot_sha256": "a" * 64,
        "decision": "NO_TRADE",
        "mode": "PAPER",
        "effective_mode": "NOT_EXECUTED",
        "stage": "adaptive_model_confirmation",
        "asset": "crypto",
        "symbol": "BTC/USDT:USDT",
        "exchange": "bybit",
        "timeframe": "1h",
        "market_timestamp": timestamp.isoformat(),
        "market_timestamp_source": "analysis_timestamp",
        "decision_context": {
            "analysis": {"trade_plan": {"side": "buy"}},
            "trade_plan": {},
            "strategy_router": {"strategy": "trend", "regime": "TREND_UP"},
            "decision_outcome": {"reason": "model_veto"},
        },
    }


def test_outcome_label_uses_subsequent_completed_bars_and_configured_costs():
    df = _frame()
    label = build_decision_outcome_label(_decision(), df)

    assert label["status"] == "VALID"
    assert label["label_version"] == LABEL_VERSION == "decision-outcome-v2"
    assert label["mode"] == "PAPER"
    assert label["effective_mode"] == "NOT_EXECUTED"
    assert label["research_only"] is True
    assert label["not_for_training"] is True
    assert label["automatic_promotion"] is False
    assert label["horizons_bars"] == list(DEFAULT_HORIZONS_BARS)
    assert label["candidate_side"] == "buy"
    assert label["strategy"] == "trend"
    assert label["regime"] == "TREND_UP"
    assert label["decision_reason_code"] == "model_veto"
    assert label["cost_bps_per_side"] == pytest.approx(7.5)
    one = label["outcomes"]["1"]
    assert one["baseline_bar_open"] == "2026-01-01T02:00:00+00:00"
    assert one["outcome_bar_open"] == "2026-01-01T03:00:00+00:00"
    assert one["forward_return_bps"] == pytest.approx((104.0 / 102.0 - 1.0) * 10000)
    assert one["carry_bps_per_bar"] == pytest.approx(0.125)
    assert one["carry_cost_bps"] == pytest.approx(0.125)
    assert one["candidate_side_net_return_bps"] == pytest.approx(one["forward_return_bps"] - 15.125)
    assert len(label["outcome_window_sha256"]) == 64
    assert len(label["label_id"]) == 64


def test_existing_v1_label_does_not_prevent_v2_relabel_dry_run(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app import db as db_module
    from app import decision_outcomes

    existing_v1 = {
        "record_type": "adaptive_decision_outcome_label",
        "decision_id": "decision-001",
        "label_version": "decision-outcome-v1",
        "status": "VALID",
    }
    query_results = [
        [SimpleNamespace(detail=_decision())],
        [SimpleNamespace(detail=existing_v1)],
    ]

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, query):
            return _Rows(query_results.pop(0))

    monkeypatch.setattr(db_module, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(
        decision_outcomes, "_fetch_decision_data",
        lambda record, days=365: _frame(),
    )

    result = asyncio.run(decision_outcomes.process_decision_outcome_labels(
        limit=100, persist=False,
    ))

    assert result["label_version"] == "decision-outcome-v2"
    assert result["persisted"] is False
    assert result["selected"] == 1
    assert result["created"] == 1
    assert result["pending_decisions_for_version"] == 1
    assert result["preview"][0]["decision_id"] == "decision-001"
    assert result["preview"][0]["label_version"] == "decision-outcome-v2"


def test_two_version_relabel_capacity_is_reserved_before_writes():
    # 100,000 v1 audit labels plus their v2 relabels fit the shared ceiling.
    full_pair_batch = _decision_outcome_label_capacity(
        existing_records=100_000,
        pending_decisions=100_000,
        batch_limit=10_000,
    )
    assert full_pair_batch["maximum_records"] == 200_000
    assert full_pair_batch["requested_max_new_records"] == 10_000
    assert full_pair_batch["fits"] is True

    # A batch that would cross the complete-history ceiling is rejected pre-write.
    near_capacity = _decision_outcome_label_capacity(
        existing_records=199_995,
        pending_decisions=100,
        batch_limit=10,
    )
    assert near_capacity["remaining_records"] == 5
    assert near_capacity["fits"] is False

    byte_capacity = _decision_outcome_label_capacity(
        existing_records=100_000,
        pending_decisions=10,
        batch_limit=10,
        existing_bytes=256 * 1024 * 1024 - 2,
        pending_bytes=3,
    )
    assert byte_capacity["fits"] is False


def test_label_window_hash_and_outcome_do_not_depend_on_bars_after_longest_horizon():
    df = _frame()
    first = build_decision_outcome_label(_decision(), df)

    changed = df.copy()
    changed.iloc[9, changed.columns.get_loc("close")] = 900.0
    changed.iloc[9, changed.columns.get_loc("high")] = 901.0
    canonical = changed.reset_index().to_csv(
        index=False, float_format="%.17g", lineterminator="\n"
    )
    changed.attrs["data_provenance"] = {
        **df.attrs["data_provenance"],
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    second = build_decision_outcome_label(_decision(), changed)

    assert first["outcome_window_sha256"] == second["outcome_window_sha256"]
    assert first["label_id"] == second["label_id"]
    assert first["outcomes"] == second["outcomes"]


def test_label_rejects_missing_exact_decision_candle_instead_of_nearest_match():
    df = _frame().drop(pd.Timestamp("2026-01-01T02:00:00Z"))
    canonical = df.reset_index().to_csv(index=False, float_format="%.17g", lineterminator="\n")
    df.attrs["data_provenance"] = {
        **_frame().attrs["data_provenance"],
        "row_count": len(df),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    with pytest.raises(OutcomeLabelError, match="exact decision candle") as exc:
        build_decision_outcome_label(_decision(), df)
    assert exc.value.code == "DECISION_CANDLE_NOT_FOUND"


def test_label_rejects_incomplete_future_horizon():
    df = _frame(as_of=pd.Timestamp("2026-01-01T08:30:00Z"))
    with pytest.raises(OutcomeLabelError, match="longest outcome horizon"):
        build_decision_outcome_label(_decision(), df)


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("market_timestamp_source", "unavailable", "MISSING_MARKET_TIMESTAMP"),
        ("asset", "commodity", "UNSUPPORTED_ASSET"),
        ("timeframe", "", "MISSING_MARKET_METADATA"),
        ("exchange", "", "MISSING_EXCHANGE"),
    ],
)
def test_label_refuses_missing_or_unverified_market_metadata(field, value, expected_code):
    record = _decision()
    record[field] = value
    with pytest.raises(OutcomeLabelError) as exc:
        build_decision_outcome_label(record, _frame())
    assert exc.value.code == expected_code


def test_forex_label_requires_an_explicit_supported_data_source():
    record = _decision()
    record.update(
        asset="forex",
        symbol="EUR_USD",
        exchange="deriv",
    )
    with pytest.raises(OutcomeLabelError) as exc:
        build_decision_outcome_label(record, _frame())
    assert exc.value.code == "UNSUPPORTED_EXCHANGE"


def test_label_requires_provenance_to_match_the_recorded_market():
    df = _frame()
    df.attrs["data_provenance"]["exchange"] = "binance"
    with pytest.raises(OutcomeLabelError) as exc:
        build_decision_outcome_label(_decision(), df)
    assert exc.value.code == "DATA_PROVENANCE_MISMATCH"


def test_label_with_no_candidate_side_still_reports_market_outcomes():
    record = _decision()
    record["decision_context"] = {"analysis": {}, "trade_plan": {}, "strategy_router": {}}
    label = build_decision_outcome_label(record, _frame(), horizons_bars=(1, 3))
    assert label["candidate_side"] is None
    assert label["outcomes"]["1"]["candidate_side_net_return_bps"] is None
    assert label["outcomes"]["1"]["long_net_return_bps"] is not None
    assert label["outcomes"]["1"]["short_net_return_bps"] is not None


def test_yfinance_fx_label_rejects_ambiguous_underscore_symbol():
    record = _decision()
    record.update(asset="forex", symbol="EUR_USD", exchange="yfinance")
    with pytest.raises(OutcomeLabelError) as exc:
        build_decision_outcome_label(record, _frame())
    assert exc.value.code == "UNSUPPORTED_SYMBOL"


def test_label_rejects_data_whose_bytes_do_not_match_recorded_source_hash():
    df = _frame()
    df.iloc[3, df.columns.get_loc("close")] = 999.0
    with pytest.raises(OutcomeLabelError) as exc:
        build_decision_outcome_label(_decision(), df)
    assert exc.value.code == "DATA_PROVENANCE_MISMATCH"


def test_capacity_uses_actual_label_count_not_all_pending_decisions():
    # One remaining record must still be usable even when many pending decisions
    # are skipped and only one valid candidate is actually generated.
    capacity = _decision_outcome_label_capacity(
        existing_records=MAX_DECISION_OUTCOME_LABEL_RECORDS - 1,
        pending_decisions=100,
        batch_limit=100,
        requested_new_records=1,
    )
    assert capacity["remaining_records"] == 1
    assert capacity["requested_max_new_records"] == 1
    assert capacity["fits"] is True


def test_prepared_audit_payload_includes_rate_limit_annotation_in_byte_budget():
    import asyncio
    from app import audit_chain

    async def deny_rate_limit(*args, **kwargs):
        return False, 0

    async def run():
        return await audit_chain.prepare_audit_detail(
            {"record_type": "adaptive_decision_outcome_label", "decision_id": "decision-001"},
            actor_id="research-outcome-labeler",
        )

    original = audit_chain.allow_rate_limit
    audit_chain.allow_rate_limit = deny_rate_limit
    try:
        prepared = asyncio.run(run())
    finally:
        audit_chain.allow_rate_limit = original

    assert prepared["audit_chain_rate_limited"] is True
    assert prepared["audit_chain_rate_limit"] == {"limit": 100, "window_seconds": 60}
    encoded_size = len(audit_chain.serialize_audit_detail(prepared).encode("utf-8"))
    unannotated_size = len(audit_chain.serialize_audit_detail({
        "record_type": "adaptive_decision_outcome_label",
        "decision_id": "decision-001",
    }).encode("utf-8"))
    assert encoded_size > unannotated_size
    capacity = _decision_outcome_label_capacity(
        existing_records=1,
        pending_decisions=1,
        batch_limit=1,
        existing_bytes=MAX_DECISION_OUTCOME_LABEL_BYTES - encoded_size,
        pending_bytes=encoded_size,
        requested_new_records=1,
    )
    assert capacity["fits"] is True


def test_postgres_label_history_write_lock_is_acquired_and_released(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app import decision_outcomes

    statements = []

    class _Connection:
        async def execute(self, statement, params=None):
            statements.append(str(statement).strip())
            return None

        async def commit(self):
            statements.append("COMMIT")

    connection = _Connection()

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get_bind(self):
            return SimpleNamespace(
                dialect=SimpleNamespace(name="postgresql"),
                url="postgresql://atlas/test",
            )

        async def connection(self):
            return connection

    from app import db as db_module
    monkeypatch.setattr(db_module, "SessionLocal", lambda: _Session())

    async def run():
        async with decision_outcomes._decision_outcome_label_write_lock():
            statements.append("BATCH")

    asyncio.run(run())
    assert statements[0].startswith("SELECT pg_advisory_xact_lock")
    assert "BATCH" in statements
    assert statements[-1] == "ROLLBACK"


def test_persistent_batch_reports_post_write_pending_count(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app import decision_outcomes
    from app import audit_chain
    from app import db as db_module

    decision = _decision()
    rows = [
        [SimpleNamespace(detail=decision)],
        [],
    ]
    appended = []

    class _Rows:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, query):
            return _Rows(rows.pop(0))

    @asynccontextmanager
    async def no_op_lock():
        yield

    async def prepare(detail, actor_id=""):
        return {
            **detail,
            "audit_chain_rate_limited": True,
            "audit_chain_rate_limit": {"limit": 100, "window_seconds": 60},
        }

    async def append(db, *, event, detail, actor_id=""):
        appended.append((event, detail, actor_id))
        return SimpleNamespace(id=len(appended))

    monkeypatch.setattr(db_module, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(decision_outcomes, "_decision_outcome_label_write_lock", no_op_lock)
    monkeypatch.setattr(decision_outcomes, "_fetch_decision_data", lambda record, days=365: _frame())
    monkeypatch.setattr(audit_chain, "prepare_audit_detail", prepare)
    monkeypatch.setattr(audit_chain, "append_prepared_audit", append)

    result = asyncio.run(decision_outcomes.process_decision_outcome_labels(
        limit=100, persist=True,
    ))
    assert result["persisted"] is True
    assert result["created"] == 1
    assert result["pending_decisions_before_run"] == 1
    assert result["pending_decisions_for_version"] == 0
    assert result["pending_actionable_decisions_for_version"] == 0
    assert result["candidate_labels_created"] == 1
    assert len(appended) == 1
    assert appended[0][1]["audit_chain_rate_limited"] is True


def test_unlabelable_pending_decisions_do_not_keep_actionable_stop_count_positive(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app import decision_outcomes
    from app import db as db_module

    unsupported = _decision()
    unsupported["decision_id"] = "decision-unsupported"
    unsupported["asset"] = "commodity"
    rows = [
        [SimpleNamespace(detail=unsupported)],
        [],
    ]

    class _Rows:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, query):
            return _Rows(rows.pop(0))

    monkeypatch.setattr(db_module, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(decision_outcomes, "_fetch_decision_data", lambda record, days=365: _frame())

    result = asyncio.run(decision_outcomes.process_decision_outcome_labels(
        limit=100, persist=False,
    ))
    assert result["pending_decisions_for_version"] == 1
    assert result["pending_actionable_decisions_for_version"] == 0
    assert result["pending_unlabelable_decisions_for_version"] == 1
    assert result["skip_reasons"]["UNSUPPORTED_ASSET"] == 1
