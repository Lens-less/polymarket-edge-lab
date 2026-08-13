from __future__ import annotations

import copy
import hashlib
from datetime import date, datetime

import pytest

from src.edge_lab.btc_twap_relative_value_qualification_runtime import (
    QualificationInsufficientData,
    build_daily_qualification_fold,
    fit_qualification_calibrators_from_reports,
)
from src.edge_lab.data_store import canonical_json_bytes


def _at_ms(utc_day: str, hour: int = 12) -> int:
    return int(
        datetime.fromisoformat(f"{utc_day}T{hour:02d}:00:00+00:00").timestamp() * 1_000
    )


def _day_offset(utc_day: str, offset_days: int) -> str:
    value = date.fromisoformat(utc_day)
    return date.fromordinal(value.toordinal() + offset_days).isoformat()


def _rehash(report: dict[str, object]) -> dict[str, object]:
    report.pop("report_sha256", None)
    report["report_sha256"] = hashlib.sha256(canonical_json_bytes(report)).hexdigest()
    return report


def _report(
    *,
    utc_day: str,
    index: int,
    preregistration_sha256: str = "a" * 64,
    evidence_track: str = "paper-v05",
    decision_tau_seconds: int = 60,
    available: bool = True,
    schema_version: str = "btc-5m-15m-relative-value-pilot-report.v2",
    verification_version: str = "v2",
    verified: bool = True,
    label_available_at_ms: int | None = None,
) -> dict[str, object]:
    decision_at_ms = _at_ms(utc_day) + index
    observation: dict[str, object] = {
        "available": available,
        "reason": None if available else "mechanical_label_not_yet_available",
    }
    if available:
        observation.update(
            {
                "event_id": f"event-{index:03d}",
                "expiry_cluster_id": f"expiry-{index:03d}",
                "raw_probabilities": {
                    "5m": "0.15" if index % 2 == 0 else "0.85",
                    "15m": "0.25" if index % 2 == 0 else "0.75",
                },
                "mechanical_label": {
                    "5m": index % 2 == 1,
                    "15m": index % 3 == 1,
                },
                "local_label_available_at_ms": (
                    decision_at_ms + 60_000
                    if label_available_at_ms is None
                    else label_available_at_ms
                ),
            }
        )
    report: dict[str, object] = {
        "schema_version": schema_version,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
        "verified_report_v2": verified,
        "verification": {
            "verified": verified,
            "verification_version": verification_version,
            "evidence_track": evidence_track,
        },
        "inputs": {
            "preregistration_sha256": preregistration_sha256,
            "evidence_track": evidence_track,
            "decision_tau_seconds": decision_tau_seconds,
            "decision_at_ms": decision_at_ms,
        },
        "calibration_observation": observation,
        "integrity": {
            "capture": {
                "raw_without_manifest": [],
                "manifest_without_raw": [],
                "checksum_mismatches": [],
                "invalid_manifests": [],
            }
        },
    }
    return _rehash(report)


def _training_reports(
    *,
    test_day: str,
    total_clusters: int = 20,
    unavailable: int = 0,
) -> tuple[dict[str, object], ...]:
    train_days = tuple(_day_offset(test_day, offset) for offset in (-6, -5, -4, -3, -2))
    return tuple(
        _report(
            utc_day=train_days[index % len(train_days)],
            index=index,
            available=index >= unavailable,
        )
        for index in range(total_clusters + unavailable)
    )


def _fit(
    reports: tuple[dict[str, object], ...] | list[dict[str, object]],
    *,
    test_day: str = "2026-08-13",
):
    return fit_qualification_calibrators_from_reports(
        reports,
        fold=build_daily_qualification_fold(test_day),
        preregistration_sha256="a" * 64,
        evidence_track="paper-v05",
        decision_tau_seconds=60,
        minimum_unique_expiry_clusters=20,
    )


def test_daily_fold_is_five_training_days_one_embargo_day_and_one_locked_test_day() -> (
    None
):
    fold = build_daily_qualification_fold("2026-08-13")

    assert fold.train_days == (
        "2026-08-07",
        "2026-08-08",
        "2026-08-09",
        "2026-08-10",
        "2026-08-11",
    )
    assert fold.validation_day == "2026-08-12"
    assert fold.test_day == "2026-08-13"
    assert fold.fit_at_ms == _at_ms("2026-08-13", hour=0)


def test_fit_uses_calibration_observations_without_requiring_a_qualified_cycle() -> (
    None
):
    reports = _training_reports(test_day="2026-08-13", unavailable=3)

    fitted = _fit(reports)

    assert set(fitted.artifacts) == {"5m", "15m"}
    assert fitted.provenance.decision_tau_seconds == 60
    assert len(fitted.provenance.training_event_ids_by_horizon["5m"]) == 20
    assert len(set(fitted.provenance.expiry_cluster_ids_by_horizon["15m"])) == 20
    assert len(fitted.provenance.artifact_hashes_by_horizon["5m"]) == 64
    assert "qualified_cycle" not in reports[0]


def test_provenance_and_artifacts_are_deterministic_across_report_order() -> None:
    reports = _training_reports(test_day="2026-08-13")

    forward = _fit(reports)
    reverse = _fit(tuple(reversed(reports)))

    assert forward.provenance.to_document() == reverse.provenance.to_document()
    assert forward.provenance.artifact_hash == reverse.provenance.artifact_hash
    assert {
        horizon: artifact.artifact_hash
        for horizon, artifact in forward.artifacts.items()
    } == {
        horizon: artifact.artifact_hash
        for horizon, artifact in reverse.artifacts.items()
    }


def test_valid_non_training_reports_are_envelope_checked_but_labels_are_not_read() -> (
    None
):
    reports = list(_training_reports(test_day="2026-08-13"))
    ignored = _report(utc_day="2026-08-12", index=999)
    ignored["calibration_observation"] = "not inspected"
    _rehash(ignored)
    reports.append(ignored)

    fitted = _fit(reports)

    assert len(fitted.provenance.training_event_ids_by_horizon["5m"]) == 20


def test_unavailable_training_rows_are_valid_but_do_not_satisfy_minimum() -> None:
    reports = tuple(
        _report(utc_day="2026-08-07", index=index, available=False)
        for index in range(20)
    )

    with pytest.raises(QualificationInsufficientData, match="no available"):
        _fit(reports)


def test_too_few_unique_expiry_clusters_has_distinct_insufficient_data_result() -> None:
    reports = _training_reports(test_day="2026-08-13", total_clusters=19)

    with pytest.raises(QualificationInsufficientData, match="19 < 20"):
        _fit(reports)


@pytest.mark.parametrize(
    ("mutation", "pattern"),
    (
        pytest.param(
            lambda report: report.update(report_sha256="0" * 64),
            "hash|tamper",
            id="tamper",
        ),
        pytest.param(
            lambda report: (
                report["verification"].update(evidence_track="other-track"),
                _rehash(report),
            ),
            "track|mixed",
            id="mixed-verification-track",
        ),
        pytest.param(
            lambda report: (
                report["inputs"].update(preregistration_sha256="b" * 64),
                _rehash(report),
            ),
            "preregistration",
            id="mixed-preregistration",
        ),
        pytest.param(
            lambda report: (
                report.update(
                    schema_version="btc-5m-15m-relative-value-pilot-report.v1"
                ),
                _rehash(report),
            ),
            "legacy|v2",
            id="legacy-schema",
        ),
        pytest.param(
            lambda report: (
                report["verification"].update(verification_version="v1"),
                _rehash(report),
            ),
            "legacy|v2",
            id="legacy-verification",
        ),
        pytest.param(
            lambda report: (
                report["integrity"]["capture"]["checksum_mismatches"].append(
                    {"raw_path": "raw.jsonl"}
                ),
                _rehash(report),
            ),
            "integrity",
            id="dirty-capture-even-when-rehashed",
        ),
    ),
)
def test_fit_fails_closed_on_invalid_report_envelopes(mutation, pattern: str) -> None:
    reports = list(_training_reports(test_day="2026-08-13"))
    mutation(reports[0])

    with pytest.raises(Exception, match=pattern):
        _fit(reports)


@pytest.mark.parametrize(
    ("label_at", "pattern"),
    (
        pytest.param("decision", "pre-decision", id="label-at-decision"),
        pytest.param("fit", "future|leak", id="label-at-fit"),
    ),
)
def test_training_label_must_be_strictly_after_decision_and_before_fit(
    label_at: str,
    pattern: str,
) -> None:
    reports = list(_training_reports(test_day="2026-08-13"))
    inputs = reports[0]["inputs"]
    assert isinstance(inputs, dict)
    observation = reports[0]["calibration_observation"]
    assert isinstance(observation, dict)
    observation["local_label_available_at_ms"] = (
        inputs["decision_at_ms"]
        if label_at == "decision"
        else _at_ms("2026-08-13", hour=0)
    )
    _rehash(reports[0])

    with pytest.raises(Exception, match=pattern):
        _fit(reports)


def test_duplicate_event_identity_is_rejected() -> None:
    reports = list(_training_reports(test_day="2026-08-13"))
    first = reports[0]["calibration_observation"]
    second = reports[1]["calibration_observation"]
    assert isinstance(first, dict) and isinstance(second, dict)
    second["event_id"] = first["event_id"]
    _rehash(reports[1])

    with pytest.raises(Exception, match="duplicate.*identity"):
        _fit(reports)


def test_duplicate_unavailable_report_identity_is_rejected() -> None:
    reports = list(_training_reports(test_day="2026-08-13"))
    duplicate = copy.deepcopy(reports[0])
    duplicate["calibration_observation"] = {"available": False}
    _rehash(duplicate)
    reports.append(duplicate)

    with pytest.raises(Exception, match="duplicate report identity"):
        _fit(reports)
