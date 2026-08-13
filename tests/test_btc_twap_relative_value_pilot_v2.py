from __future__ import annotations

import importlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest


def _epoch_ms(timestamp: str) -> int:
    return int(
        datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1_000
    )


def _report_builder():
    return importlib.import_module("scripts.build_btc_twap_relative_value_pilot_report")


def _v2_preregistration(*, evidence_track_id: str = "paper-v05") -> dict[str, object]:
    return {
        "scope": {
            "paper_only": True,
            "live_orders_disabled": True,
            "prospective_only_after": "2026-08-13T06:00:00Z",
            "evidence_track_id": evidence_track_id,
        },
        "frozen_strategy": {
            "decision_tau_seconds": [60, 120],
            "clock_sync": {"source": "SNTP time.apple.com"},
        },
        "strategy_spec": {
            "path": "research/paper/STRATEGY_SPEC.md",
            "sha256": "a" * 64,
        },
    }


def _capture_config(
    root: Path,
    *,
    started_at_ms: int,
    evidence_track_id: str = "paper-v05",
) -> dict[str, object]:
    return {
        "schema_version": "edge-lab-forward-capture-config.v1",
        "data_root": str(root.resolve()),
        "capture_started_at_ms": started_at_ms,
        "evidence_track_id": evidence_track_id,
    }


def _write_capture_identity(
    root: Path,
    *,
    started_at_ms: int,
    evidence_track_id: str = "paper-v05",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "capture-config.json").write_text(
        json.dumps(
            _capture_config(
                root, started_at_ms=started_at_ms, evidence_track_id=evidence_track_id
            )
        ),
        encoding="utf-8",
    )


def _book_side(price: str) -> SimpleNamespace:
    return SimpleNamespace(
        snapshot=SimpleNamespace(asks=(SimpleNamespace(price=price),))
    )


def test_validate_prospective_identity_accepts_matching_v2_capture_roots(
    tmp_path: Path,
) -> None:
    preregistration = _v2_preregistration()
    cutoff_ms = _epoch_ms("2026-08-13T06:00:00Z")
    capture_root = tmp_path / "current"
    predictor_root = tmp_path / "predictor"
    capture_config = _capture_config(capture_root, started_at_ms=cutoff_ms)
    _write_capture_identity(predictor_root, started_at_ms=cutoff_ms)

    verification = _report_builder()._validate_prospective_report_identity(
        capture_config=capture_config,
        preregistration=preregistration,
        capture_root=capture_root.resolve(),
        predictor_root=predictor_root.resolve(),
        history_roots=(),
        decision_at_ms=cutoff_ms,
    )

    assert verification == {
        "verified": True,
        "verification_version": "v2",
        "evidence_track": "paper-v05",
        "prospective_only_after_ms": cutoff_ms,
        "capture_started_at_ms": cutoff_ms,
        "decision_at_ms": cutoff_ms,
        "history_roots_verified": 0,
    }


@pytest.mark.parametrize(
    ("capture_started_at_ms", "track", "message"),
    [
        (
            _epoch_ms("2026-08-13T05:59:59Z"),
            "paper-v05",
            "predates prospective_only_after",
        ),
        (
            _epoch_ms("2026-08-13T06:00:00Z"),
            "wrong-track",
            "evidence_track_id mismatch",
        ),
    ],
)
def test_validate_prospective_report_identity_rejects_invalid_current_capture(
    tmp_path: Path,
    capture_started_at_ms: int,
    track: str,
    message: str,
) -> None:
    preregistration = _v2_preregistration()
    cutoff_ms = _epoch_ms("2026-08-13T06:00:00Z")
    capture_root = tmp_path / "current"
    predictor_root = tmp_path / "predictor"
    capture_config = _capture_config(
        capture_root,
        started_at_ms=capture_started_at_ms,
        evidence_track_id=track,
    )
    _write_capture_identity(predictor_root, started_at_ms=cutoff_ms)

    with pytest.raises(ValueError, match=message):
        _report_builder()._validate_prospective_report_identity(
            capture_config=capture_config,
            preregistration=preregistration,
            capture_root=capture_root.resolve(),
            predictor_root=predictor_root.resolve(),
            history_roots=(),
            decision_at_ms=cutoff_ms,
        )


@pytest.mark.parametrize(
    ("started_at_ms", "track", "message"),
    [
        (
            _epoch_ms("2026-08-13T05:59:59Z"),
            "paper-v05",
            "outside the prospective evidence track",
        ),
        (
            _epoch_ms("2026-08-13T06:00:00Z"),
            "wrong-track",
            "outside the prospective evidence track",
        ),
        (
            _epoch_ms("2026-08-13T06:00:00Z"),
            "paper-v05",
            "must start strictly before current capture",
        ),
    ],
)
def test_validate_prospective_report_identity_rejects_history_roots_outside_v2_contract(
    tmp_path: Path,
    started_at_ms: int,
    track: str,
    message: str,
) -> None:
    preregistration = _v2_preregistration()
    cutoff_ms = _epoch_ms("2026-08-13T06:00:00Z")
    capture_root = tmp_path / "current"
    predictor_root = tmp_path / "predictor"
    history_root = tmp_path / "history"
    capture_config = _capture_config(capture_root, started_at_ms=cutoff_ms)
    _write_capture_identity(predictor_root, started_at_ms=cutoff_ms)
    _write_capture_identity(
        history_root,
        started_at_ms=started_at_ms,
        evidence_track_id=track,
    )

    with pytest.raises(ValueError, match=message):
        _report_builder()._validate_prospective_report_identity(
            capture_config=capture_config,
            preregistration=preregistration,
            capture_root=capture_root.resolve(),
            predictor_root=predictor_root.resolve(),
            history_roots=(history_root.resolve(),),
            decision_at_ms=cutoff_ms,
        )


def test_normalized_binary_ask_probability_uses_best_ask_ratio() -> None:
    probability = _report_builder()._normalized_binary_ask_probability(
        {
            "up-token": _book_side("0.41"),
            "down-token": _book_side("0.59"),
        },
        up_token_id="up-token",
        down_token_id="down-token",
    )

    assert probability == Decimal("0.41")


def test_qualification_event_identity_is_stable_per_inputs_and_changes_with_tau() -> (
    None
):
    first_event_id, first_cluster_id = _report_builder()._qualification_event_identity(
        evidence_track="paper-v05",
        expiry_ms=1_786_579_200_000,
        decision_tau_seconds=60,
    )
    repeated_event_id, repeated_cluster_id = (
        _report_builder()._qualification_event_identity(
            evidence_track="paper-v05",
            expiry_ms=1_786_579_200_000,
            decision_tau_seconds=60,
        )
    )
    changed_tau_event_id, changed_tau_cluster_id = (
        _report_builder()._qualification_event_identity(
            evidence_track="paper-v05",
            expiry_ms=1_786_579_200_000,
            decision_tau_seconds=120,
        )
    )

    assert first_event_id == repeated_event_id
    assert first_cluster_id == repeated_cluster_id == "paper-v05:1786579200000"
    assert changed_tau_cluster_id == first_cluster_id
    assert changed_tau_event_id != first_event_id


def test_exact_observation_returns_matching_receipt_timestamp_for_boundary_value() -> (
    None
):
    series = (
        (100_000, 100_500, Decimal("42")),
        (100_000, 100_200, Decimal("42")),
        (101_000, 101_100, Decimal("43")),
    )

    observation = _report_builder()._exact_observation(series, 100_000)

    assert observation == (100_000, 100_200, Decimal("42"))


def test_oos_forecast_stays_unavailable_until_verified_label_arrives() -> None:
    forecast = {
        "available": True,
        "split": "test",
        "actual_up": {"5m": False, "15m": False},
    }

    unavailable = _report_builder()._bind_verified_oos_label(
        forecast,
        label_available=False,
        label_available_at_ms=None,
    )
    available = _report_builder()._bind_verified_oos_label(
        forecast,
        label_available=True,
        label_available_at_ms=1_234,
    )

    assert unavailable == {
        "available": False,
        "split": "test",
        "reason_codes": ["verified_mechanical_label_not_available"],
    }
    assert "actual_up" not in unavailable
    assert available["actual_up"] == {"5m": False, "15m": False}
    assert available["local_label_available_at_ms"] == 1_234
