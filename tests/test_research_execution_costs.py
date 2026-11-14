import base64
import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.execution_aware_research import simulate_execution_aware_strategy
from app.research_evidence import canonical_json_bytes, canonical_sha256
from app.research_execution_costs import (
    PER_BAR_CONTEXT_SCHEMA,
    build_research_execution_cost_context,
    execution_cost_for_event,
    execution_cost_series_for_positions,
    is_verified_research_execution_cost_context,
    research_execution_model_fingerprint,
    slice_research_execution_context,
)


PARAMETERS = {
    "base_slippage_bps": 2.0,
    "max_participation_rate": 0.10,
    "impact_coefficient": 0.5,
    "rejection_probability": 0.0,
    "latency_min_ms": 10.0,
    "latency_median_ms": 20.0,
    "latency_p95_ms": 50.0,
    "latency_max_ms": 100.0,
    "latency_impact_coefficient": 0.5,
}


def _signed(document):
    signer = Ed25519PrivateKey.generate()
    signature = signer.sign(canonical_json_bytes(document))
    public_key = signer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    signed = {
        **document,
        "attestation": {
            "algorithm": "Ed25519",
            "key_id": "research-cost-test-key",
            "signature_base64": base64.b64encode(signature).decode("ascii"),
        },
    }
    trusted = {
        "research-cost-test-key": base64.b64encode(public_key).decode("ascii")
    }
    return signed, json.dumps(trusted, sort_keys=True)


def _source(kind):
    digest = hashlib.sha256(f"verified-test-source:{kind}".encode()).hexdigest()
    return {
        "source_type": kind,
        "uri": f"https://evidence-source.atlas-data.net/{kind}/records.csv",
        "sha256": digest,
    }


def _settings(tmp_path):
    dataset = tmp_path / "paper-fill-study.json"
    dataset.write_text('{"calibration":"test-data"}', encoding="utf-8")
    artifact = {
        "schema": "atlas-paper-execution-calibration-v1",
        "fixture_only": False,
        "provenance_source": "independently-reconciled-venue-records",
        "evidence_source": "venue-paper-fill-study",
        "calibration_method": "held-out-venue-order-and-fill-observations",
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "observation_counts": {"fills": 40, "latency": 40, "order_outcomes": 40},
        "supported_markets": ["binance:BTC/USDT"],
        "calibration_parameters": PARAMETERS,
        "dataset_path": dataset.name,
    }
    artifact_path = tmp_path / "paper-execution-calibration.json"
    artifact_path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
    artifact_id = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    return SimpleNamespace(
        research_evidence_public_keys_json="",
        research_cost_max_spread_mae_bps=1.0,
        research_cost_max_slippage_mae_bps=1.0,
        research_order_notional_usd=10_000.0,
        research_fee_source="binance-published-spot-fee-schedule",
        research_fee_evidence_id="binance-btcusdt-fee-tier-2025q4",
        paper_impact_max_participation_rate=PARAMETERS["max_participation_rate"],
        paper_impact_coefficient=PARAMETERS["impact_coefficient"],
        paper_execution_rejection_probability=PARAMETERS["rejection_probability"],
        paper_execution_latency_min_ms=PARAMETERS["latency_min_ms"],
        paper_execution_latency_median_ms=PARAMETERS["latency_median_ms"],
        paper_execution_latency_p95_ms=PARAMETERS["latency_p95_ms"],
        paper_execution_latency_max_ms=PARAMETERS["latency_max_ms"],
        paper_latency_impact_coefficient=PARAMETERS["latency_impact_coefficient"],
        paper_execution_evidence_source="venue-paper-fill-study",
        paper_execution_evidence_path=str(artifact_path),
        paper_execution_evidence_id=f"sha256:{artifact_id}",
    )


def _research_frame():
    index = pd.date_range("2026-01-01T00:00:00Z", periods=24, freq="h")
    close = 100.0 + np.arange(len(index), dtype=float) * 0.1
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(len(index), 100.0),
        },
        index=index,
    )
    canonical = frame.reset_index().to_csv(
        index=False, float_format="%.17g", lineterminator="\n"
    )
    frame.attrs["data_provenance"] = {
        "source": "ccxt_ohlcv",
        "asset_class": "crypto",
        "exchange": "binance",
        "market_type": "spot",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "volume_unit": "base_asset",
        "volume_unit_verified": True,
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    return frame


def _research_cost_evidence(frame, settings):
    fingerprint = research_execution_model_fingerprint(
        settings,
        taker_bps=5.5,
        base_slippage_bps=2.0,
        cost_stress_multiplier=2.0,
    )
    order_book = _source("order_book")
    fee_schedule = _source("fee_schedule")
    document = {
        "evidence_type": "venue_execution_cost_calibration",
        "evidence_id": "binance-btc-usdt-execution-calibration-v1",
        "venue": "binance",
        "market_type": "spot",
        "symbol": "BTC/USDT",
        "model_fingerprint": fingerprint,
        "synthetic_only": False,
        "testnet_only": False,
        "fee_source": settings.research_fee_source,
        "fee_evidence_id": settings.research_fee_evidence_id,
        "taker_fee_bps": 5.5,
        "fee_schedule_source_sha256": fee_schedule["sha256"],
        "sources": [
            _source("observed_fills"),
            order_book,
            fee_schedule,
        ],
        "observed_order_count": 40,
        "observed_fill_count": 40,
        "validation_start": "2025-01-01T00:00:00Z",
        "validation_end": "2025-12-31T23:59:59Z",
        "heldout_validation": {
            "status": "PASS",
            "sample_count": 40,
            "mean_absolute_spread_error_bps": 0.2,
            "mean_absolute_slippage_error_bps": 0.4,
        },
        "per_bar_execution_context": {
            "schema": PER_BAR_CONTEXT_SCHEMA,
            "dataset_sha256": frame.attrs["data_provenance"]["sha256"],
            "bar_index_sha256": canonical_sha256(
                [timestamp.tz_convert("UTC").isoformat() for timestamp in frame.index]
            ),
            "spread_source_sha256": order_book["sha256"],
            "spread_bps": [4.0] * len(frame),
            "spread_observation_count": len(frame),
            "spread_values_sha256": canonical_sha256([4.0] * len(frame)),
        },
    }
    signed, public_keys = _signed(document)
    settings.research_evidence_public_keys_json = public_keys
    frame.attrs["execution_cost_calibration_evidence"] = signed


def test_builds_verified_per_bar_spread_volume_volatility_and_latency_context(tmp_path):
    frame = _research_frame()
    settings = _settings(tmp_path)
    _research_cost_evidence(frame, settings)

    context, audit = build_research_execution_cost_context(
        frame,
        settings=settings,
        taker_bps=5.5,
        base_slippage_bps=2.0,
        cost_stress_multiplier=2.0,
        asset="crypto",
    )

    assert context is not None
    assert is_verified_research_execution_cost_context(context)
    sliced_context = slice_research_execution_context(context, frame.index[1:])
    assert sliced_context is not None
    assert is_verified_research_execution_cost_context(sliced_context)
    assert audit["status"] == "READY"
    assert audit["execution_cost_model_complete"] is True
    assert audit["execution_cost_calibration_status"] == "VERIFIED"
    assert audit["execution_cost_calibration_evidence_id"] == "binance-btc-usdt-execution-calibration-v1"
    assert context["execution_cost_calibration_status"] == "VERIFIED"
    assert context["execution_cost_calibration_evidence_id"] == "binance-btc-usdt-execution-calibration-v1"
    assert context["venue"] == "binance"
    assert context["market_type"] == "spot"
    assert context["symbol"] == "BTC/USDT"
    assert context["timeframe"] == "1h"
    assert context["model_fingerprint"] == research_execution_model_fingerprint(
        settings,
        taker_bps=5.5,
        base_slippage_bps=2.0,
        cost_stress_multiplier=2.0,
    )
    from app.main import _strategy_router_validation_gate

    verified_validation = {
        "status": "OK",
        "total_return": 0.12,
        "folds": [
            {"total_return": 0.04, "execution_cost_model_complete": True},
            {"total_return": 0.03, "execution_cost_model_complete": True},
            {"total_return": 0.05, "execution_cost_model_complete": True},
        ],
        "execution_cost_model_complete": True,
        "execution_cost_model_status": "VERIFIED_PER_BAR_SPREAD_VOLUME_PARTICIPATION_LATENCY",
        "execution_cost_calibration_status": "VERIFIED",
        "execution_cost_calibration_verified": True,
        "execution_cost_calibration_evidence_id": context["execution_cost_calibration_evidence_id"],
        "execution_cost_model_fingerprint": context["model_fingerprint"],
        "per_bar_execution_context_sha256": context["context_sha256"],
        "execution_cost_dataset_sha256": context["dataset_sha256"],
    }
    gate = _strategy_router_validation_gate(
        verified_validation,
        required_folds=3,
        required_positive_fold_ratio=0.5,
        current_dataset_sha256=context["dataset_sha256"],
        current_execution_cost_context=context,
    )
    assert gate["passed"] is True
    assert gate["execution_cost_calibration_verified"] is True
    assert gate["execution_cost_validation_matches_current_context"] is True
    assert gate["execution_cost_dataset_matches_current_data"] is True

    from app.main import _router_validation_cache_matches_execution_context

    cached_router_validation = {
        **verified_validation,
        "execution_cost_model_complete": True,
        "execution_cost_model_status": "VERIFIED_PER_BAR_SPREAD_VOLUME_PARTICIPATION_LATENCY",
        "execution_cost_dataset_sha256": context["dataset_sha256"],
        "execution_cost_calibration_status": "VERIFIED",
        "execution_cost_calibration_verified": True,
        "execution_cost_calibration_evidence_id": context["execution_cost_calibration_evidence_id"],
        "execution_cost_model_fingerprint": context["model_fingerprint"],
        "per_bar_execution_context_sha256": context["context_sha256"],
    }
    assert _router_validation_cache_matches_execution_context(
        cached_router_validation,
        current_dataset_sha256=context["dataset_sha256"],
        current_execution_cost_context=context,
    ) is True
    assert _router_validation_cache_matches_execution_context(
        cached_router_validation,
        current_dataset_sha256="d" * 64,
        current_execution_cost_context=context,
    ) is False

    assert np.isnan(context["volume_units"][0])
    assert context["volume_units"][21] == pytest.approx(100.0)
    assert context["spread_bps"][21] == pytest.approx(4.0)
    assert context["volatility_bps"][21] >= 0.0

    cost, reason = execution_cost_for_event(
        context,
        bar_position=21,
        turnover=0.05,
        reference_price=102.1,
        side="buy",
        namespace="test-candidate-entry",
    )
    assert reason is None
    assert cost is not None and cost > 5.5

    blocked_cost, blocked_reason = execution_cost_for_event(
        context,
        bar_position=21,
        turnover=0.20,
        reference_price=102.1,
        side="buy",
        namespace="oversized-candidate-entry",
    )
    assert blocked_cost is None
    assert blocked_reason == "ORDER_PARTICIPATION_LIMIT_EXCEEDED"


def test_signed_source_types_are_normalized_case_insensitively(tmp_path):
    frame = _research_frame()
    settings = _settings(tmp_path)
    _research_cost_evidence(frame, settings)

    document = dict(frame.attrs["execution_cost_calibration_evidence"])
    document.pop("attestation")
    document["sources"] = [
        {
            **source,
            "source_type": str(source.get("source_type") or "").upper(),
        }
        if source.get("source_type") in {"fee_schedule", "order_book"}
        else source
        for source in document["sources"]
    ]
    signed, public_keys = _signed(document)
    settings.research_evidence_public_keys_json = public_keys
    frame.attrs["execution_cost_calibration_evidence"] = signed

    context, audit = build_research_execution_cost_context(
        frame,
        settings=settings,
        taker_bps=5.5,
        base_slippage_bps=2.0,
        cost_stress_multiplier=2.0,
        asset="crypto",
    )

    assert context is not None
    assert audit["status"] == "READY"
    assert audit["execution_cost_model_complete"] is True


def test_research_execution_fingerprint_binds_session_cost_algorithm(tmp_path, monkeypatch):
    from app import research_execution_costs as costs_module

    settings = _settings(tmp_path)
    baseline = research_execution_model_fingerprint(
        settings,
        taker_bps=5.5,
        base_slippage_bps=2.0,
        cost_stress_multiplier=2.0,
    )
    monkeypatch.setattr(
        costs_module,
        "SESSION_COST_MODEL_VERSION",
        "market-sessions-test-next-version",
    )
    changed = research_execution_model_fingerprint(
        settings,
        taker_bps=5.5,
        base_slippage_bps=2.0,
        cost_stress_multiplier=2.0,
    )

    assert changed != baseline


def test_benchmark_cost_context_is_disabled_after_candidate_event_rejection():
    from app.research_engine import _benchmark_execution_inputs

    context = {"model_fingerprint": "a" * 64}
    session_schedule = {
        "LONDON": 1.0,
        "NEW_YORK": 2.0,
        "NEW_YORK_LATE": 3.0,
        "OTHER": 4.0,
    }

    fallback_context, fallback_sessions = _benchmark_execution_inputs(
        context,
        {"per_bar_execution_cost_model_status": "FALLBACK_AFTER_EVENT_REJECTION"},
        session_schedule,
    )
    verified_context, verified_sessions = _benchmark_execution_inputs(
        context,
        {"per_bar_execution_cost_model_status": "VERIFIED_PER_BAR_EXECUTION_COSTS"},
        session_schedule,
    )

    assert fallback_context is None
    assert fallback_sessions is session_schedule
    assert verified_context is context
    assert verified_sessions is None


def test_rejects_ohlcv_mutation_after_provenance_was_attached(tmp_path):
    frame = _research_frame()
    settings = _settings(tmp_path)
    _research_cost_evidence(frame, settings)
    frame.iloc[-1, frame.columns.get_loc("close")] += 0.25

    context, audit = build_research_execution_cost_context(
        frame,
        settings=settings,
        taker_bps=5.5,
        base_slippage_bps=2.0,
        cost_stress_multiplier=2.0,
        asset="crypto",
    )

    assert context is None
    assert audit["execution_cost_model_complete"] is False
    assert audit["reason"] == "MARKET_DATA_CONTENT_HASH_MISMATCH"


def test_signature_binds_spread_series_and_dataset_context(tmp_path):
    frame = _research_frame()
    settings = _settings(tmp_path)
    _research_cost_evidence(frame, settings)
    tampered = dict(frame.attrs["execution_cost_calibration_evidence"])
    signed_context = dict(tampered["per_bar_execution_context"])
    signed_context["spread_bps"] = [9.0] * len(frame)
    tampered["per_bar_execution_context"] = signed_context
    frame.attrs["execution_cost_calibration_evidence"] = tampered

    context, audit = build_research_execution_cost_context(
        frame,
        settings=settings,
        taker_bps=5.5,
        base_slippage_bps=2.0,
        cost_stress_multiplier=2.0,
        asset="crypto",
    )

    assert context is None
    assert audit["execution_cost_model_complete"] is False
    assert audit["execution_cost_calibration_status"] == "EVIDENCE_SIGNATURE_INVALID"



def test_signed_research_evidence_cannot_override_the_configured_taker_fee(tmp_path):
    frame = _research_frame()
    settings = _settings(tmp_path)
    _research_cost_evidence(frame, settings)

    document = dict(frame.attrs["execution_cost_calibration_evidence"])
    document.pop("attestation")
    document["taker_fee_bps"] = 6.0
    signed, public_keys = _signed(document)
    settings.research_evidence_public_keys_json = public_keys
    frame.attrs["execution_cost_calibration_evidence"] = signed

    context, audit = build_research_execution_cost_context(
        frame,
        settings=settings,
        taker_bps=5.5,
        base_slippage_bps=2.0,
        cost_stress_multiplier=2.0,
        asset="crypto",
    )

    assert context is None
    assert audit["execution_cost_model_complete"] is False
    assert audit["reason"] == "SIGNED_TAKER_FEE_DOES_NOT_MATCH_RESEARCH_FEE_POLICY"



def test_router_cost_series_uses_event_specific_per_bar_rates():
    index = pd.date_range("2026-01-01T00:00:00Z", periods=3, freq="h")
    context = {
        "index": index,
        "spread_bps": np.array([4.0, 4.0, 4.0]),
        "volume_units": np.array([100.0, 100.0, 100.0]),
        "volatility_bps": np.array([200.0, 200.0, 200.0]),
        "bar_duration_ms": 3_600_000.0,
        "taker_bps": 5.5,
        **PARAMETERS,
        "order_notional_usd": 10_000.0,
        "dataset_sha256": "a" * 64,
        "context_sha256": "b" * 64,
        "model_fingerprint": "c" * 64,
    }
    position = pd.Series([0.0, 0.05, 0.0], index=index)
    prices = pd.Series([100.0, 100.0, 100.0], index=index)

    costs, audit = execution_cost_series_for_positions(
        context,
        position,
        prices,
        namespace="router-test",
        fallback_cost_bps=7.5,
    )

    assert audit["execution_cost_model_complete"] is True
    assert costs.iloc[0] == pytest.approx(7.5)
    assert costs.iloc[1] > 5.5
    assert costs.iloc[2] > 5.5


def test_execution_simulator_prices_entry_and_terminal_exit_separately():
    index = pd.date_range("2026-01-01T00:00:00Z", periods=2, freq="h")
    frame = pd.DataFrame(
        {
            "open": [100.0, 100.0],
            "high": [101.0, 101.0],
            "low": [99.0, 99.0],
            "close": [100.0, 100.0],
        },
        index=index,
    )
    calls = []

    def cost_provider(bar_position, turnover, reference_price, side, event_type):
        calls.append((bar_position, turnover, reference_price, side, event_type))
        return 10.0 if event_type == "ENTRY" else 20.0

    result = simulate_execution_aware_strategy(
        frame,
        pd.Series([0.5, 0.5], index=index),
        pd.Series([10.0, 10.0], index=index),
        stop_atr=2.0,
        take_profit_atr=2.0,
        transaction_cost_bps=7.5,
        carry_bps_per_bar=0.0,
        transaction_cost_provider=cost_provider,
    )

    assert [call[4] for call in calls] == ["ENTRY", "EXIT"]
    assert result["effective_transaction_cost_bps"].tolist() == pytest.approx([10.0, 20.0])
    assert result["net_returns"].tolist() == pytest.approx([-0.0005, -0.001])
    assert result["audit"]["per_bar_execution_cost_model_complete"] is True


def test_execution_simulator_splits_position_reversals_into_close_and_open_events():
    index = pd.date_range("2026-01-01T00:00:00Z", periods=3, freq="h")
    frame = pd.DataFrame(
        {
            "open": [100.0, 100.0, 100.0],
            "high": [101.0, 101.0, 101.0],
            "low": [99.0, 99.0, 99.0],
            "close": [100.0, 100.0, 100.0],
        },
        index=index,
    )
    calls = []

    def cost_provider(bar_position, turnover, reference_price, side, event_type):
        calls.append((bar_position, turnover, reference_price, side, event_type))
        return {
            "ENTRY": 10.0,
            "ENTRY_CLOSE": 11.0,
            "ENTRY_OPEN": 12.0,
            "EXIT": 20.0,
        }[event_type]

    result = simulate_execution_aware_strategy(
        frame,
        pd.Series([0.5, -0.25, -0.25], index=index),
        pd.Series([10.0, 10.0, 10.0], index=index),
        stop_atr=2.0,
        take_profit_atr=2.0,
        transaction_cost_bps=7.5,
        carry_bps_per_bar=0.0,
        transaction_cost_provider=cost_provider,
    )

    reversal_calls = [call for call in calls if call[0] == 1]
    assert [(call[1], call[3], call[4]) for call in reversal_calls] == [
        (0.5, "sell", "ENTRY_CLOSE"),
        (0.25, "sell", "ENTRY_OPEN"),
    ]
    assert result["effective_transaction_cost_bps"].iloc[1] == pytest.approx(
        (0.5 * 11.0 + 0.25 * 12.0) / 0.75
    )


def test_router_position_cost_series_splits_sign_reversal(monkeypatch):
    from app import research_execution_costs as costs_module

    index = pd.date_range("2026-01-01T00:00:00Z", periods=2, freq="h")
    calls = []

    def fake_cost(_context, *, bar_position, turnover, reference_price, side, namespace):
        calls.append((bar_position, turnover, reference_price, side, namespace))
        return (10.0 if namespace.endswith(":OPEN") else 20.0), None

    monkeypatch.setattr(costs_module, "execution_cost_for_event", fake_cost)
    context = {"model_fingerprint": "f" * 64, "context_sha256": "c" * 64}
    positions = pd.Series([0.5, -0.25], index=index)
    prices = pd.Series([100.0, 100.0], index=index)

    per_bar, audit = execution_cost_series_for_positions(
        context, positions, prices, namespace="router-test", fallback_cost_bps=7.5
    )

    reversal_calls = [call for call in calls if call[0] == 1]
    assert [(call[1], call[3], call[4]) for call in reversal_calls] == [
        (0.5, "sell", "router-test:CLOSE"),
        (0.25, "sell", "router-test:OPEN"),
    ]
    assert per_bar.iloc[1] == pytest.approx((0.5 * 20.0 + 0.25 * 10.0) / 0.75)
    assert audit["execution_cost_model_complete"] is True


@pytest.mark.parametrize(
    "bar_position,turnover,reference_price",
    [
        (True, 1.0, 100.0),
        (1.5, 1.0, 100.0),
        (Decimal("1.0000000000000000001"), 1.0, 100.0),
        (1, True, 100.0),
        (1, 1.0, False),
        (np.True_, 1.0, 100.0),
        (1, np.True_, 100.0),
        (1, 1.0, np.False_),
    ],
)
def test_execution_event_rejects_boolean_or_fractional_indices_and_quantities(
    bar_position, turnover, reference_price
):
    from app.research_execution_costs import execution_cost_for_event

    context = {
        "index": pd.date_range("2026-01-01T00:00:00Z", periods=2, freq="h"),
    }
    cost, reason = execution_cost_for_event(
        context,
        bar_position=bar_position,
        turnover=turnover,
        reference_price=reference_price,
        side="buy",
        namespace="invalid-input-test",
    )
    assert cost is None
    assert reason == "EXECUTION_EVENT_NUMERIC_INPUT_INVALID"


def test_strategy_router_return_stream_uses_per_event_research_costs(monkeypatch):
    from app import strategy_router

    index = pd.date_range("2026-01-01T00:00:00Z", periods=3, freq="h")
    frame = pd.DataFrame(
        {
            "open": [100.0, 100.0, 101.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 99.0, 100.0],
            "close": [100.0, 101.0, 102.0],
        },
        index=index,
    )
    signals = pd.DataFrame(
        {
            "momentum": [0.05, 0.05, 0.05],
            "realized_vol": [0.2, 0.2, 0.2],
        },
        index=index,
    )
    positions = pd.Series([0.05, 0.05, 0.05], index=index)
    next_prices = pd.Series([1.0, 1.01, 1.02], index=index)
    monkeypatch.setattr(
        strategy_router,
        "strategy_signals",
        lambda *_args, **_kwargs: signals,
    )
    monkeypatch.setattr(
        strategy_router,
        "target_positions",
        lambda *_args, **_kwargs: positions,
    )
    monkeypatch.setattr(
        strategy_router,
        "next_open_returns",
        lambda _frame: next_prices,
    )
    context = {
        "index": index,
        "spread_bps": np.array([4.0, 4.0, 4.0]),
        "volume_units": np.array([1_000.0, 1_000.0, 1_000.0]),
        "volatility_bps": np.array([200.0, 200.0, 200.0]),
        "bar_duration_ms": 3_600_000.0,
        "taker_bps": 5.5,
        **PARAMETERS,
        "order_notional_usd": 10_000.0,
        "dataset_sha256": "a" * 64,
        "context_sha256": "b" * 64,
        "model_fingerprint": "c" * 64,
    }

    result = strategy_router._strategy_returns(
        frame,
        object(),
        "momentum",
        costs_bps=7.5,
        asset="crypto",
        execution_cost_context=context,
    )
    expected_cost, reason = execution_cost_for_event(
        context,
        bar_position=1,
        turnover=0.05,
        reference_price=100.0,
        side="buy",
        namespace="strategy-router:momentum:OPEN",
    )

    assert reason is None
    assert expected_cost is not None
    assert result.attrs["execution_cost_model_complete"] is True
    assert result.attrs["execution_cost_model_fingerprint"] == "c" * 64
    assert result.iloc[1] == pytest.approx(
        0.05 * 0.01 - 0.05 * expected_cost / 10_000.0
    )
    assert result.iloc[1] < 0.05 * 0.01 - 0.05 * 7.5 / 10_000.0


def test_missing_execution_context_uses_static_cost_without_event_loop(monkeypatch):
    from app import research_execution_costs

    index = pd.date_range("2026-01-01T00:00:00Z", periods=500, freq="h")
    positions = pd.Series(np.linspace(0.0, 0.5, len(index)), index=index)
    prices = pd.Series(100.0, index=index)

    def unexpected_event_call(*_args, **_kwargs):
        raise AssertionError("static fallback must not price individual events")

    monkeypatch.setattr(
        research_execution_costs, "execution_cost_for_event", unexpected_event_call
    )
    costs, audit = execution_cost_series_for_positions(
        None, positions, prices, namespace="router-static", fallback_cost_bps=12.0
    )

    assert costs.eq(12.0).all()
    assert audit["execution_cost_model_complete"] is False
    assert audit["execution_cost_model_status"] == "STATIC_FALLBACK_NO_EXECUTION_CONTEXT"
    assert audit["_execution_cost_model_complete_by_bar"] == [False] * len(index)
    assert len(audit["_execution_cost_failure_reasons_by_bar"]) == len(index)



@pytest.mark.parametrize(
    "bar_position",
    [2, 2.0, np.int64(2), np.float64(2.0), Decimal("2.0"), "2", "2.0"],
)
def test_execution_event_accepts_integral_numeric_bar_indices(bar_position):
    from app.research_execution_costs import execution_cost_for_event

    context = {
        "index": pd.date_range("2026-01-01T00:00:00Z", periods=3, freq="h"),
    }
    cost, reason = execution_cost_for_event(
        context,
        bar_position=bar_position,
        turnover=0.0,
        reference_price=100.0,
        side="buy",
        namespace="integral-index-test",
    )
    assert cost == 0.0
    assert reason is None
