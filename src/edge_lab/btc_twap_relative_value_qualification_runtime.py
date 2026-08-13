from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from .btc_twap_relative_value import CalibrationPoint, IsotonicProbabilityCalibrator
from .data_store import canonical_json_bytes

_HORIZONS = ("5m", "15m")
_TRAIN_DAY_COUNT = 5
_REPORT_SCHEMA_VERSION = "btc-5m-15m-relative-value-pilot-report.v2"
_VERIFICATION_VERSION = "v2"
_PREREGISTRATION_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class QualificationInsufficientData(ValueError):
    """Raised when verified prospective observations cannot yet fit a calibrator."""


def _require_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return value


def _require_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be bool")
    return value


def _require_non_negative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _require_probability(value: object, *, label: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{label} must be an exact decimal probability")
    try:
        if isinstance(value, Decimal):
            probability = value
        elif isinstance(value, (int, str)):
            probability = Decimal(value)
        else:
            raise TypeError(f"{label} must be an exact decimal probability")
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a decimal probability") from exc
    if not probability.is_finite() or not Decimal(0) <= probability <= Decimal(1):
        raise ValueError(f"{label} must be in [0, 1]")
    return probability


def _normalize_utc_day(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("utc_day must be a YYYY-MM-DD string")
    try:
        normalized = date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("utc_day must be a valid YYYY-MM-DD string") from exc
    if normalized != value:
        raise ValueError("utc_day must be canonical YYYY-MM-DD")
    return normalized


def _utc_day_from_ms(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc).date().isoformat()
    )


def _fit_at_ms_for_day(utc_day: str) -> int:
    return int(datetime.fromisoformat(f"{utc_day}T00:00:00+00:00").timestamp() * 1_000)


def _day_offset(utc_day: str, offset_days: int) -> str:
    base_day = date.fromisoformat(_normalize_utc_day(utc_day))
    return date.fromordinal(base_day.toordinal() + offset_days).isoformat()


def _classify_day(utc_day: str, fold: QualificationFold) -> str:
    if utc_day in fold.train_days:
        return "train"
    if utc_day == fold.validation_day:
        return "validation"
    if utc_day == fold.test_day:
        return "test"
    return "outside"


def _validate_preregistration_sha256(value: str) -> str:
    if _PREREGISTRATION_SHA256.fullmatch(value) is None:
        raise ValueError(
            "preregistration_sha256 must be a lowercase 64-character hex digest"
        )
    return value


def _canonical_report_hash(report: Mapping[str, object]) -> str:
    expected_hash = _require_string(report.get("report_sha256"), label="report_sha256")
    payload = dict(report)
    payload.pop("report_sha256", None)
    actual_hash = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError("qualification report hash mismatch; possible tamper")
    return actual_hash


def _validate_paper_integrity_envelope(report: Mapping[str, object]) -> None:
    if (
        report.get("paper_only") is not True
        or report.get("public_only") is not True
        or report.get("new_orders_disabled") is not True
        or report.get("orders_submitted") != 0
        or report.get("authenticated_endpoints_used") != 0
    ):
        raise ValueError("qualification report violates the paper-only guard")
    integrity = _require_mapping(report.get("integrity"), label="integrity")
    for value in integrity.values():
        if isinstance(value, Mapping):
            checks = (value,)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            checks = tuple(
                _require_mapping(item, label="integrity item").get("integrity")
                for item in value
            )
        else:
            raise TypeError("integrity entries must be objects or lists")
        for check in checks:
            check_mapping = _require_mapping(check, label="integrity check")
            if any(check_mapping.get(key) for key in check_mapping):
                raise ValueError("qualification report capture integrity is not clean")


@dataclass(frozen=True)
class QualificationFold:
    train_days: tuple[str, ...]
    validation_day: str
    test_day: str
    fit_at_ms: int

    def __post_init__(self) -> None:
        train_days = tuple(_normalize_utc_day(day) for day in self.train_days)
        if len(train_days) != _TRAIN_DAY_COUNT:
            raise ValueError("qualification folds require exactly five train days")
        validation_day = _normalize_utc_day(self.validation_day)
        test_day = _normalize_utc_day(self.test_day)
        expected_days = tuple(_day_offset(test_day, offset) for offset in range(-6, 0))
        if train_days + (validation_day,) != expected_days:
            raise ValueError(
                "qualification fold must be five train days then one embargo day"
            )
        fit_at_ms = _require_non_negative_int(self.fit_at_ms, label="fit_at_ms")
        if fit_at_ms != _fit_at_ms_for_day(test_day):
            raise ValueError("fit_at_ms must be test-day UTC midnight")
        object.__setattr__(self, "train_days", train_days)
        object.__setattr__(self, "validation_day", validation_day)
        object.__setattr__(self, "test_day", test_day)
        object.__setattr__(self, "fit_at_ms", fit_at_ms)

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": "btc-twap-relative-value-qualification-fold.v1",
            "train_days": list(self.train_days),
            "validation_day": self.validation_day,
            "test_day": self.test_day,
            "fit_at_ms": self.fit_at_ms,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> QualificationFold:
        return cls(
            train_days=tuple(str(day) for day in document["train_days"]),
            validation_day=str(document["validation_day"]),
            test_day=str(document["test_day"]),
            fit_at_ms=int(document["fit_at_ms"]),
        )


@dataclass(frozen=True)
class QualificationCalibrationProvenance:
    split: str
    fit_at_ms: int
    maximum_label_available_at_ms: int
    train_days: tuple[str, ...]
    validation_day: str
    test_day: str
    decision_tau_seconds: int
    preregistration_sha256: str
    evidence_track: str
    training_event_ids_by_horizon: dict[str, tuple[str, ...]]
    expiry_cluster_ids_by_horizon: dict[str, tuple[str, ...]]
    artifact_hashes_by_horizon: dict[str, str]

    def __post_init__(self) -> None:
        if self.split != "train":
            raise ValueError("qualification calibration provenance split must be train")
        fold = QualificationFold(
            train_days=self.train_days,
            validation_day=self.validation_day,
            test_day=self.test_day,
            fit_at_ms=self.fit_at_ms,
        )
        maximum_label_available_at_ms = _require_non_negative_int(
            self.maximum_label_available_at_ms,
            label="maximum_label_available_at_ms",
        )
        if maximum_label_available_at_ms >= fold.fit_at_ms:
            raise ValueError(
                "maximum_label_available_at_ms must be earlier than fit_at_ms"
            )
        decision_tau_seconds = _require_non_negative_int(
            self.decision_tau_seconds,
            label="decision_tau_seconds",
        )
        if decision_tau_seconds <= 0:
            raise ValueError("decision_tau_seconds must be positive")
        preregistration_sha256 = _validate_preregistration_sha256(
            self.preregistration_sha256
        )
        evidence_track = _require_string(self.evidence_track, label="evidence_track")
        expected_keys = set(_HORIZONS)
        if set(self.training_event_ids_by_horizon) != expected_keys:
            raise ValueError("training_event_ids_by_horizon must include both horizons")
        if set(self.expiry_cluster_ids_by_horizon) != expected_keys:
            raise ValueError("expiry_cluster_ids_by_horizon must include both horizons")
        if set(self.artifact_hashes_by_horizon) != expected_keys:
            raise ValueError("artifact_hashes_by_horizon must include both horizons")
        normalized_event_ids = {
            horizon: tuple(
                str(value) for value in self.training_event_ids_by_horizon[horizon]
            )
            for horizon in _HORIZONS
        }
        normalized_cluster_ids = {
            horizon: tuple(
                str(value) for value in self.expiry_cluster_ids_by_horizon[horizon]
            )
            for horizon in _HORIZONS
        }
        normalized_artifact_hashes = {
            horizon: _require_string(
                self.artifact_hashes_by_horizon[horizon],
                label=f"artifact_hashes_by_horizon.{horizon}",
            )
            for horizon in _HORIZONS
        }
        object.__setattr__(self, "fit_at_ms", fold.fit_at_ms)
        object.__setattr__(
            self, "maximum_label_available_at_ms", maximum_label_available_at_ms
        )
        object.__setattr__(self, "train_days", fold.train_days)
        object.__setattr__(self, "validation_day", fold.validation_day)
        object.__setattr__(self, "test_day", fold.test_day)
        object.__setattr__(self, "decision_tau_seconds", decision_tau_seconds)
        object.__setattr__(self, "preregistration_sha256", preregistration_sha256)
        object.__setattr__(self, "evidence_track", evidence_track)
        object.__setattr__(self, "training_event_ids_by_horizon", normalized_event_ids)
        object.__setattr__(
            self, "expiry_cluster_ids_by_horizon", normalized_cluster_ids
        )
        object.__setattr__(
            self, "artifact_hashes_by_horizon", normalized_artifact_hashes
        )

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": "btc-twap-relative-value-qualification-calibration.v1",
            "split": self.split,
            "fit_at_ms": self.fit_at_ms,
            "maximum_label_available_at_ms": self.maximum_label_available_at_ms,
            "train_days": list(self.train_days),
            "validation_day": self.validation_day,
            "test_day": self.test_day,
            "decision_tau_seconds": self.decision_tau_seconds,
            "preregistration_sha256": self.preregistration_sha256,
            "evidence_track": self.evidence_track,
            "training_event_ids_by_horizon": {
                horizon: list(self.training_event_ids_by_horizon[horizon])
                for horizon in _HORIZONS
            },
            "expiry_cluster_ids_by_horizon": {
                horizon: list(self.expiry_cluster_ids_by_horizon[horizon])
                for horizon in _HORIZONS
            },
            "artifact_hashes_by_horizon": {
                horizon: self.artifact_hashes_by_horizon[horizon]
                for horizon in _HORIZONS
            },
        }

    @classmethod
    def from_document(
        cls,
        document: Mapping[str, object],
    ) -> QualificationCalibrationProvenance:
        return cls(
            split=str(document["split"]),
            fit_at_ms=int(document["fit_at_ms"]),
            maximum_label_available_at_ms=int(
                document["maximum_label_available_at_ms"]
            ),
            train_days=tuple(str(day) for day in document["train_days"]),
            validation_day=str(document["validation_day"]),
            test_day=str(document["test_day"]),
            decision_tau_seconds=int(document["decision_tau_seconds"]),
            preregistration_sha256=str(document["preregistration_sha256"]),
            evidence_track=str(document["evidence_track"]),
            training_event_ids_by_horizon={
                str(horizon): tuple(str(event_id) for event_id in event_ids)
                for horizon, event_ids in dict(
                    document["training_event_ids_by_horizon"]
                ).items()
            },
            expiry_cluster_ids_by_horizon={
                str(horizon): tuple(str(cluster_id) for cluster_id in cluster_ids)
                for horizon, cluster_ids in dict(
                    document["expiry_cluster_ids_by_horizon"]
                ).items()
            },
            artifact_hashes_by_horizon={
                str(horizon): str(value)
                for horizon, value in dict(
                    document["artifact_hashes_by_horizon"]
                ).items()
            },
        )

    @property
    def artifact_hash(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_document())).hexdigest()


@dataclass(frozen=True)
class FittedQualificationCalibrators:
    artifacts: dict[str, IsotonicProbabilityCalibrator]
    provenance: QualificationCalibrationProvenance

    def __post_init__(self) -> None:
        if set(self.artifacts) != set(_HORIZONS):
            raise ValueError("artifacts must include both 5m and 15m calibrators")
        for horizon, calibrator in self.artifacts.items():
            if not isinstance(calibrator, IsotonicProbabilityCalibrator):
                raise TypeError("artifact values must be IsotonicProbabilityCalibrator")
            if calibrator.horizon != horizon:
                raise ValueError("artifact horizon key mismatch")


@dataclass(frozen=True)
class _ValidatedEnvelope:
    utc_day: str
    split: str
    decision_at_ms: int
    report_identity: tuple[int, int]


@dataclass(frozen=True)
class _QualificationTrainingRow:
    event_id: str
    expiry_cluster_id: str
    decision_at_ms: int
    label_available_at_ms: int
    raw_probabilities_by_horizon: dict[str, Decimal]
    outcomes_by_horizon: dict[str, bool]


def build_daily_qualification_fold(test_day: str) -> QualificationFold:
    normalized_test_day = _normalize_utc_day(test_day)
    return QualificationFold(
        train_days=tuple(
            _day_offset(normalized_test_day, offset) for offset in range(-6, -1)
        ),
        validation_day=_day_offset(normalized_test_day, -1),
        test_day=normalized_test_day,
        fit_at_ms=_fit_at_ms_for_day(normalized_test_day),
    )


def _validate_report_envelope(
    report: Mapping[str, object],
    *,
    fold: QualificationFold,
    preregistration_sha256: str,
    evidence_track: str,
    decision_tau_seconds: int,
) -> _ValidatedEnvelope:
    _canonical_report_hash(report)
    _validate_paper_integrity_envelope(report)
    schema_version = _require_string(
        report.get("schema_version"), label="schema_version"
    )
    if schema_version != _REPORT_SCHEMA_VERSION:
        raise ValueError(
            f"legacy or unsupported report schema {schema_version}; expected "
            f"{_REPORT_SCHEMA_VERSION}"
        )
    if (
        _require_bool(report.get("verified_report_v2"), label="verified_report_v2")
        is not True
    ):
        raise ValueError("qualification report must be verified_report_v2=true")

    verification = _require_mapping(report.get("verification"), label="verification")
    if (
        _require_bool(verification.get("verified"), label="verification.verified")
        is not True
    ):
        raise ValueError("qualification report verification must be true")
    if (
        _require_string(
            verification.get("verification_version"),
            label="verification.verification_version",
        )
        != _VERIFICATION_VERSION
    ):
        raise ValueError("legacy verification version; expected v2")
    if (
        _require_string(
            verification.get("evidence_track"),
            label="verification.evidence_track",
        )
        != evidence_track
    ):
        raise ValueError("mixed track evidence is not allowed in qualification fit")

    inputs = _require_mapping(report.get("inputs"), label="inputs")
    if (
        _require_string(
            inputs.get("preregistration_sha256"),
            label="inputs.preregistration_sha256",
        )
        != preregistration_sha256
    ):
        raise ValueError("mixed preregistration_sha256 is not allowed")
    if (
        _require_string(inputs.get("evidence_track"), label="inputs.evidence_track")
        != evidence_track
    ):
        raise ValueError("mixed track evidence is not allowed in qualification fit")
    report_tau = _require_non_negative_int(
        inputs.get("decision_tau_seconds"),
        label="inputs.decision_tau_seconds",
    )
    if report_tau != decision_tau_seconds:
        raise ValueError(
            "mixed decision_tau_seconds is not allowed in qualification fit"
        )
    decision_at_ms = _require_non_negative_int(
        inputs.get("decision_at_ms"),
        label="inputs.decision_at_ms",
    )
    utc_day = _utc_day_from_ms(decision_at_ms)
    return _ValidatedEnvelope(
        utc_day=utc_day,
        split=_classify_day(utc_day, fold),
        decision_at_ms=decision_at_ms,
        report_identity=(decision_at_ms, report_tau),
    )


def _training_row(
    report: Mapping[str, object],
    *,
    envelope: _ValidatedEnvelope,
    fit_at_ms: int,
) -> _QualificationTrainingRow | None:
    observation = _require_mapping(
        report.get("calibration_observation"),
        label="calibration_observation",
    )
    if not _require_bool(
        observation.get("available"), label="calibration_observation.available"
    ):
        return None
    event_id = _require_string(
        observation.get("event_id"),
        label="calibration_observation.event_id",
    )
    expiry_cluster_id = _require_string(
        observation.get("expiry_cluster_id"),
        label="calibration_observation.expiry_cluster_id",
    )
    raw_probabilities = _require_mapping(
        observation.get("raw_probabilities"),
        label="calibration_observation.raw_probabilities",
    )
    raw_probabilities_by_horizon = {
        horizon: _require_probability(
            raw_probabilities.get(horizon),
            label=f"calibration_observation.raw_probabilities.{horizon}",
        )
        for horizon in _HORIZONS
    }
    mechanical_label = _require_mapping(
        observation.get("mechanical_label"),
        label="calibration_observation.mechanical_label",
    )
    outcomes_by_horizon = {
        horizon: _require_bool(
            mechanical_label.get(horizon),
            label=f"calibration_observation.mechanical_label.{horizon}",
        )
        for horizon in _HORIZONS
    }
    label_available_at_ms = _require_non_negative_int(
        observation.get("local_label_available_at_ms"),
        label="calibration_observation.local_label_available_at_ms",
    )
    if label_available_at_ms <= envelope.decision_at_ms:
        raise ValueError(f"pre-decision label detected for event {event_id}")
    if label_available_at_ms >= fit_at_ms:
        raise ValueError(f"future label leak detected for event {event_id}")
    return _QualificationTrainingRow(
        event_id=event_id,
        expiry_cluster_id=expiry_cluster_id,
        decision_at_ms=envelope.decision_at_ms,
        label_available_at_ms=label_available_at_ms,
        raw_probabilities_by_horizon=raw_probabilities_by_horizon,
        outcomes_by_horizon=outcomes_by_horizon,
    )


def fit_qualification_calibrators_from_reports(
    reports: Sequence[Mapping[str, object]],
    *,
    fold: QualificationFold,
    preregistration_sha256: str,
    evidence_track: str,
    decision_tau_seconds: int,
    minimum_unique_expiry_clusters: int,
) -> FittedQualificationCalibrators:
    preregistration_sha256 = _validate_preregistration_sha256(preregistration_sha256)
    evidence_track = _require_string(evidence_track, label="evidence_track")
    decision_tau_seconds = _require_non_negative_int(
        decision_tau_seconds,
        label="decision_tau_seconds",
    )
    if decision_tau_seconds <= 0:
        raise ValueError("decision_tau_seconds must be positive")
    minimum_unique_expiry_clusters = _require_non_negative_int(
        minimum_unique_expiry_clusters,
        label="minimum_unique_expiry_clusters",
    )
    if minimum_unique_expiry_clusters <= 0:
        raise ValueError("minimum_unique_expiry_clusters must be positive")

    validated_rows: list[_QualificationTrainingRow] = []
    seen_report_identities: set[tuple[int, int]] = set()
    seen_event_ids: set[str] = set()
    for report in reports:
        envelope = _validate_report_envelope(
            report,
            fold=fold,
            preregistration_sha256=preregistration_sha256,
            evidence_track=evidence_track,
            decision_tau_seconds=decision_tau_seconds,
        )
        if envelope.report_identity in seen_report_identities:
            raise ValueError(
                "duplicate report identity in qualification fit: "
                f"decision_at_ms={envelope.decision_at_ms}, tau={decision_tau_seconds}"
            )
        seen_report_identities.add(envelope.report_identity)
        if envelope.split != "train":
            continue
        row = _training_row(report, envelope=envelope, fit_at_ms=fold.fit_at_ms)
        if row is None:
            continue
        if row.event_id in seen_event_ids:
            raise ValueError(
                f"duplicate event identity in qualification fit: {row.event_id}"
            )
        seen_event_ids.add(row.event_id)
        validated_rows.append(row)

    if not validated_rows:
        raise QualificationInsufficientData(
            "no available calibration observations in the five-day training window"
        )

    unique_expiry_clusters = {row.expiry_cluster_id for row in validated_rows}
    if len(unique_expiry_clusters) < minimum_unique_expiry_clusters:
        raise QualificationInsufficientData(
            "insufficient unique expiry clusters for each horizon: "
            f"{len(unique_expiry_clusters)} < {minimum_unique_expiry_clusters}"
        )

    points_by_horizon: dict[str, list[CalibrationPoint]] = {
        horizon: [] for horizon in _HORIZONS
    }
    cluster_by_event_id = {
        row.event_id: row.expiry_cluster_id for row in validated_rows
    }
    for row in validated_rows:
        for horizon in _HORIZONS:
            points_by_horizon[horizon].append(
                CalibrationPoint(
                    event_id=row.event_id,
                    prediction=row.raw_probabilities_by_horizon[horizon],
                    outcome=row.outcomes_by_horizon[horizon],
                    split="train",
                    label_available_at_ms=row.label_available_at_ms,
                )
            )

    artifacts: dict[str, IsotonicProbabilityCalibrator] = {}
    deterministic_event_ids: dict[str, tuple[str, ...]] = {}
    deterministic_expiry_cluster_ids: dict[str, tuple[str, ...]] = {}
    for horizon in _HORIZONS:
        ordered_points = tuple(
            sorted(
                points_by_horizon[horizon],
                key=lambda point: (
                    point.label_available_at_ms,
                    point.event_id,
                    point.prediction,
                    int(point.outcome),
                ),
            )
        )
        calibrator = IsotonicProbabilityCalibrator.fit(
            ordered_points,
            fit_at_ms=fold.fit_at_ms,
            horizon=horizon,
        )
        artifacts[horizon] = calibrator
        deterministic_event_ids[horizon] = tuple(calibrator.training_event_ids)
        deterministic_expiry_cluster_ids[horizon] = tuple(
            cluster_by_event_id[event_id] for event_id in calibrator.training_event_ids
        )

    provenance = QualificationCalibrationProvenance(
        split="train",
        fit_at_ms=fold.fit_at_ms,
        maximum_label_available_at_ms=max(
            calibrator.maximum_label_available_at_ms
            for calibrator in artifacts.values()
        ),
        train_days=fold.train_days,
        validation_day=fold.validation_day,
        test_day=fold.test_day,
        decision_tau_seconds=decision_tau_seconds,
        preregistration_sha256=preregistration_sha256,
        evidence_track=evidence_track,
        training_event_ids_by_horizon=deterministic_event_ids,
        expiry_cluster_ids_by_horizon=deterministic_expiry_cluster_ids,
        artifact_hashes_by_horizon={
            horizon: artifacts[horizon].artifact_hash for horizon in _HORIZONS
        },
    )
    return FittedQualificationCalibrators(artifacts=artifacts, provenance=provenance)


__all__ = [
    "FittedQualificationCalibrators",
    "QualificationCalibrationProvenance",
    "QualificationFold",
    "QualificationInsufficientData",
    "build_daily_qualification_fold",
    "fit_qualification_calibrators_from_reports",
]
