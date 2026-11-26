import numpy as np
import pandas as pd
import pytest

from app.trading_core import build_features, triple_barrier, rsi, _aligned_log_loss, predict_latest
from app.ids import client_order_id, make_signal_id


def frame(n=500):
    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    close = pd.Series(100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, .01, n))), index=idx)
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * .99, "close": close, "volume": 100}, index=idx)


def test_training_model_signature_fails_closed_when_configured_signer_cannot_sign(monkeypatch):
    from app import trading_core

    monkeypatch.setattr(trading_core.settings, "model_signing_private_key", "not-an-ed25519-key")
    monkeypatch.setattr(trading_core.settings, "model_signing_key", "")
    monkeypatch.setattr(trading_core, "artifact_signature", lambda _digest: "")

    with pytest.raises(RuntimeError, match="Model signing is configured"):
        trading_core._model_manifest_signature("a" * 64)


class _DeterministicSigningFailureModel:
    """Top-level so joblib can serialize it before the configured signer rejects the artifact."""

    def __init__(self, **_kwargs):
        self.classes_ = np.array([0, 1, 2], dtype=int)

    def fit(self, features, labels):
        assert len(features) == len(labels)
        return self

    def predict(self, features):
        return np.zeros(len(features), dtype=int)

    def predict_proba(self, features):
        return np.tile(np.array([[0.30, 0.30, 0.40]]), (len(features), 1))


def test_failed_model_signing_preserves_existing_model_and_manifest(tmp_path, monkeypatch):
    from app import trading_core

    model_path = tmp_path / "preserved.joblib"
    model_file, meta_file = trading_core._model_paths(str(model_path))
    model_file.write_bytes(b"existing-champion-model")
    meta_file.write_bytes(b'{"model_sha256":"existing-manifest"}')
    previous_model = model_file.read_bytes()
    previous_meta = meta_file.read_bytes()

    monkeypatch.setattr(trading_core, "LGBMClassifier", _DeterministicSigningFailureModel)
    monkeypatch.setattr(trading_core.settings, "model_signing_private_key", "invalid-key")
    monkeypatch.setattr(trading_core.settings, "model_signing_key", "")
    monkeypatch.setattr(trading_core, "artifact_signature", lambda _digest: "")

    with pytest.raises(RuntimeError, match="Model signing is configured"):
        trading_core.train_model(
            frame(1000),
            str(model_path),
            min_train=100,
            folds=2,
            asset="crypto",
        )

    assert model_file.read_bytes() == previous_model
    assert meta_file.read_bytes() == previous_meta
    assert list(tmp_path.rglob("*.training")) == []

def test_model_bundle_switches_with_one_atomic_pointer_and_keeps_old_version_readable(
    tmp_path, monkeypatch
):
    import hashlib
    import json
    import joblib
    from pathlib import Path
    from app import trading_core
    from app.model_registry import verify_model_file

    monkeypatch.setattr(trading_core.settings, "environment", "development")
    champion = tmp_path / "champion.joblib"

    # Seed an already-published immutable champion bundle.
    champion_versions = champion.with_name(champion.name + ".versions")
    champion_versions.mkdir()
    old_artifact = champion_versions / "old-version.joblib"
    joblib.dump({"generation": "old"}, old_artifact)
    old_hash = hashlib.sha256(old_artifact.read_bytes()).hexdigest()
    champion.write_text(
        json.dumps({
            "artifact_pointer_schema": 1,
            "artifact_file": f"{champion_versions.name}/{old_artifact.name}",
            "model_sha256": old_hash,
            "manifest_signature": "",
        }),
        encoding="utf-8",
    )

    # A legacy-format candidate is accepted and re-published in the new versioned format.
    candidate = tmp_path / "candidate.joblib"
    joblib.dump({"generation": "new"}, candidate)
    candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
    candidate.with_suffix(candidate.suffix + ".meta.json").write_text(
        json.dumps({"model_sha256": candidate_hash, "manifest_signature": ""}),
        encoding="utf-8",
    )
    champion_pointer = champion.read_bytes()
    real_replace = trading_core.os.replace

    def interrupt_pointer_replace(source, destination):
        if Path(destination) == champion:
            raise OSError("simulated atomic pointer publication failure")
        return real_replace(source, destination)

    monkeypatch.setattr(trading_core.os, "replace", interrupt_pointer_replace)
    with pytest.raises(OSError, match="atomic pointer publication failure"):
        trading_core.promote_model_artifact(str(candidate), str(champion))

    # A failed pointer commit leaves the old champion and removes the unpublished orphan.
    assert champion.read_bytes() == champion_pointer
    assert list(champion_versions.iterdir()) == [old_artifact]
    old_model, old_meta = trading_core.load_model(str(champion))
    assert old_model == {"generation": "old"}
    assert old_meta["model_sha256"] == old_hash
    assert old_artifact.is_file()

    monkeypatch.setattr(trading_core.os, "replace", real_replace)
    promoted_hash = trading_core.promote_model_artifact(str(candidate), str(champion))
    new_model, new_meta = trading_core.load_model(str(champion))
    assert promoted_hash == candidate_hash
    assert new_model == {"generation": "new"}
    assert new_meta["model_sha256"] == candidate_hash
    assert new_meta["artifact_pointer_schema"] == 1
    assert verify_model_file(champion, candidate_hash)
    # Old immutable versions are not pruned while a reader or rollback may reference them.
    assert old_artifact.is_file()



def test_model_version_retention_preserves_live_rollback_and_recent_versions(tmp_path):
    import json
    import os
    from datetime import datetime, timedelta, timezone
    from app import trading_core

    model_file = tmp_path / "retained.joblib"
    versions_dir = model_file.with_name(model_file.name + ".versions")
    versions_dir.mkdir()
    now = datetime.now(timezone.utc)

    def add_artifact(digest, nonce, hours_old):
        artifact = versions_dir / (digest + "." + nonce + ".joblib")
        artifact.write_bytes((digest + nonce).encode("ascii"))
        stamp = (now - timedelta(hours=hours_old)).timestamp()
        os.utime(artifact, (stamp, stamp))
        return artifact

    live = add_artifact("a" * 64, "1" * 32, 120)
    rollback = add_artifact("b" * 64, "2" * 32, 120)
    keep_extra = add_artifact("c" * 64, "3" * 32, 121)
    remove_one = add_artifact("d" * 64, "4" * 32, 122)
    remove_two = add_artifact("e" * 64, "5" * 32, 123)

    def pointer(artifact, digest):
        return {
            "artifact_pointer_schema": 1,
            "artifact_file": versions_dir.name + "/" + artifact.name,
            "model_sha256": digest,
        }

    model_file.write_text(json.dumps(pointer(live, "a" * 64)), encoding="utf-8")
    model_file.with_name(model_file.name + ".previous").write_text(
        json.dumps(pointer(rollback, "b" * 64)), encoding="utf-8"
    )

    removed = trading_core.prune_model_versions(
        model_file,
        now=now,
        grace_period=timedelta(hours=24),
        retain_unreferenced=1,
    )

    assert removed == 2
    assert live.is_file()
    assert rollback.is_file()
    assert keep_extra.is_file()
    assert not remove_one.exists()
    assert not remove_two.exists()


def test_features_are_nonempty_and_finite():
    f = build_features(frame())
    assert len(f) > 0
    assert list(f.columns)
    assert np.isfinite(f.to_numpy()).all()


def test_constant_volume_does_not_destroy_features():
    f = build_features(frame())
    assert f["vol_z"].notna().all()


def test_rsi_handles_monotonic_price_without_nan_collapse():
    idx = pd.date_range("2025-01-01", periods=100, freq="h", tz="UTC")
    close = pd.Series(np.arange(100.0, 200.0), index=idx)
    out = rsi(close)
    assert out.iloc[-1] == 100.0
    assert out.iloc[-1] > 99


def test_triple_barrier_does_not_label_incomplete_future_window():
    f = frame(100)
    vol = pd.Series(.01, index=f.index)
    y = triple_barrier(f.close, vol, f.high, f.low, max_hold=12)
    assert y.iloc[-12:].isna().all()
    assert set(y.dropna().unique()).issubset({-1.0, 0.0, 1.0})


def test_triple_barrier_same_bar_touch_uses_execution_stop_first_rule():
    idx = pd.date_range("2025-01-01", periods=20, freq="h", tz="UTC")
    close = pd.Series(100.0, index=idx)
    vol = pd.Series(.01, index=idx)
    high = close.copy(); low = close.copy()
    high.iloc[1] = 102.5; low.iloc[1] = 97.5
    y = triple_barrier(close, vol, high, low, pt=2.0, sl=2.0, max_hold=5)
    assert y.iloc[0] == -1


def test_signal_id_and_client_order_id_are_deterministic():
    sid = make_signal_id("bybit", "BTC/USDT:USDT", "1h", "2025-01-01T00:00:00+00:00", "buy")
    assert sid == make_signal_id("bybit", "BTC/USDT:USDT", "1h", "2025-01-01T00:00:00+00:00", "buy")
    assert client_order_id(sid) == client_order_id(sid)
    assert client_order_id(sid).startswith("ait-")


def test_triple_barrier_training_labels_encode_as_classifier_classes():
    from app.trading_core import _encode_triple_barrier_labels

    labels = pd.Series([-1.0, 0.0, 1.0], index=["short", "flat", "long"])
    encoded = _encode_triple_barrier_labels(labels)

    assert encoded.tolist() == [0, 1, 2]
    assert encoded.index.tolist() == ["short", "flat", "long"]
    with pytest.raises(ValueError, match="finite and non-null"):
        _encode_triple_barrier_labels(pd.Series([-1.0, np.nan, 1.0]))
    with pytest.raises(ValueError, match="one of -1, 0, or 1"):
        _encode_triple_barrier_labels(pd.Series([-1.0, 2.0]))


def test_training_refuses_collapsed_target():
    from app.trading_core import train_model
    idx = pd.date_range("2025-01-01", periods=1400, freq="h", tz="UTC")
    close = pd.Series(100.0 * np.exp(np.linspace(0, 0.001, len(idx))), index=idx)
    df = pd.DataFrame({"open": close, "high": close * (1 + 1e-7), "low": close * (1 - 1e-7), "close": close,
                       "volume": np.linspace(100, 120, len(idx))}, index=idx)
    try:
        train_model(df, "models/collapsed-test.joblib", min_train=800, folds=2, asset="crypto")
    except RuntimeError as exc:
        assert "two target classes" in str(exc) or "collapsed" in str(exc)
    else:
        raise AssertionError("Training should refuse a collapsed target")



def test_triple_barrier_labels_timeouts_by_terminal_return_with_dead_zone():
    idx = pd.date_range("2025-01-01", periods=6, freq="h", tz="UTC")
    # None of these bars reaches a +/-2-volatility barrier. t=2 has a +1%
    # terminal return; t=3's return is within the half-volatility dead zone.
    close = pd.Series([100.0, 100.0, 101.0, 100.1, 100.1, 100.1], index=idx)
    vol = pd.Series(0.01, index=idx)
    y = triple_barrier(close, vol, close, close, pt=2.0, sl=2.0, max_hold=2)
    assert y.iloc[0] == 1.0
    assert y.iloc[1] == 0.0
    assert y.iloc[-2:].isna().all()


def test_triple_barrier_timeout_dead_zone_neutralizes_sub_half_sigma_return():
    idx = pd.date_range("2025-02-01", periods=6, freq="h", tz="UTC")
    close = pd.Series([100.0, 100.0, 100.2, 100.4, 100.4, 100.4], index=idx)
    vol = pd.Series(0.01, index=idx)
    y = triple_barrier(close, vol, close, close, pt=2.0, sl=2.0, max_hold=2)
    # At t=1, timeout return is +0.4%; half-sigma is +0.5%, so it must be flat.
    # This assertion fails with the previous 0.25*sigma threshold.
    assert y.iloc[1] == 0.0


def test_aligned_log_loss_handles_classes_absent_from_training_fold():
    y_true = np.array([0, 1, 2], dtype=int)
    # The fold trained only on labels 0 and 2, so class 1 has no probability column.
    probs = np.array([[0.9, 0.1], [0.5, 0.5], [0.1, 0.9]], dtype=float)
    score = _aligned_log_loss(y_true, probs, np.array([0, 2]))
    assert np.isfinite(score)
    assert score > 0



def test_predict_latest_maps_sparse_reordered_class_probabilities(monkeypatch):
    class SparseClassModel:
        # Flat is absent and predict_proba columns are not in canonical class order.
        classes_ = np.array([2, 0], dtype=int)

        def predict_proba(self, _features):
            return np.array([[0.75, 0.25]], dtype=float)

    monkeypatch.setattr(
        "app.trading_core.load_model",
        lambda _model_path: (SparseClassModel(), {"model_sha256": "test-model"}),
    )

    result = predict_latest(frame(500), "unused-model-path")

    assert result["long_probability"] == pytest.approx(0.75)
    assert result["short_probability"] == pytest.approx(0.25)
    assert result["flat_probability"] == pytest.approx(0.0)
    assert result["score"] == pytest.approx(0.5)
    assert result["signal"] == 1
