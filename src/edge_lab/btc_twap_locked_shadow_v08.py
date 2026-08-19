"""Rolling, append-only locked-shadow journal for the v0.8 readiness track.

Unlike the frozen v0.7 full-universe receipt, this journal locks a deterministic
inclusion rule once, then admits each future common expiry before its first
decision.  The first admitted capture for an expiry can never be replaced;
dirty, no-trade, no-fill, and failed cohorts stay in the denominator.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .btc_twap_relative_value_v07 import canonical_expiry_cluster_id
from .data_store import canonical_json_bytes

POLICY_SCHEMA = "btc-5m-15m-readiness-v08-rolling-inclusion-policy.v1"
RECEIPT_SCHEMA = "btc-5m-15m-readiness-v08-rolling-receipt.v1"
AUDIT_SCHEMA = "btc-5m-15m-readiness-v08-rolling-audit.v1"
TRACK_ID = "btc_5m_15m_edge_readiness_v08_2026_08_18"
LOCAL_ATTESTATION_MODEL = "local_o_excl_fsync_hash_chain_not_external_signature"
INCLUSION_RULE_ID = (
    "first_predecision_capture_for_each_discovered_official_60s_"
    "same_expiry_5m_15m_pair_after_track_start"
)
ALLOWED_OUTCOMES = frozenset(
    {
        "no_trade",
        "causal_no_fill",
        "complete",
        "partial_unwound",
        "failed_unhedged",
        "capture_dirty",
        "rule_or_fee_dirty",
        "rtds_gap_dirty",
    }
)
WRITER_SOURCE_CLOCK_SKEW_TOLERANCE_MS = 2_000
DEFAULT_SETTLEMENT_GRACE_SECONDS = 360
_WRITER_SESSION_ID = hashlib.sha256(
    b"|".join(
        (
            str(os.getpid()).encode("ascii"),
            str(time.time_ns()).encode("ascii"),
            os.urandom(16),
        )
    )
).hexdigest()


def _text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _integer(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _sha256(value: str, *, name: str) -> str:
    value = _text(value, name=name).lower()
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


@dataclass(frozen=True)
class RollingInclusionPolicy:
    preregistration_sha256: str
    track_starts_at_ms: int
    decision_tau_seconds: tuple[int, ...]
    locked_at_ms: int
    received_at_ms: int
    monotonic_ns: int
    settlement_grace_seconds: int = DEFAULT_SETTLEMENT_GRACE_SECONDS
    track_id: str = TRACK_ID
    inclusion_rule_id: str = INCLUSION_RULE_ID

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "preregistration_sha256",
            _sha256(self.preregistration_sha256, name="preregistration_sha256"),
        )
        if self.track_id != TRACK_ID or self.inclusion_rule_id != INCLUSION_RULE_ID:
            raise ValueError("rolling policy identity is not the v0.8 frozen policy")
        for name in (
            "track_starts_at_ms",
            "locked_at_ms",
            "received_at_ms",
            "monotonic_ns",
            "settlement_grace_seconds",
        ):
            object.__setattr__(self, name, _integer(getattr(self, name), name=name))
        taus = tuple(
            _integer(value, name="decision_tau_seconds")
            for value in self.decision_tau_seconds
        )
        if not taus or len(set(taus)) != len(taus) or any(value <= 0 for value in taus):
            raise ValueError("decision_tau_seconds must be unique positive integers")
        object.__setattr__(
            self, "decision_tau_seconds", tuple(sorted(taus, reverse=True))
        )
        if self.locked_at_ms > self.received_at_ms:
            raise ValueError("policy lock cannot postdate its receipt")
        if self.received_at_ms >= self.track_starts_at_ms:
            raise ValueError("rolling policy must be received before the track starts")
        if self.settlement_grace_seconds <= 0:
            raise ValueError("settlement_grace_seconds must be positive")

    @property
    def policy_sha256(self) -> str:
        return _digest(self.to_document())

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": POLICY_SCHEMA,
            "track_id": self.track_id,
            "preregistration_sha256": self.preregistration_sha256,
            "track_starts_at_ms": self.track_starts_at_ms,
            "decision_tau_seconds": list(self.decision_tau_seconds),
            "inclusion_rule_id": self.inclusion_rule_id,
            "one_pair_per_common_expiry": True,
            "first_admitted_attempt_is_permanent": True,
            "dirty_no_trade_no_fill_failures_remain_in_denominator": True,
            "rolling_without_fixed_universe_cap": True,
            "predictive_live_fallback_allowed": False,
            "locked_at_ms": self.locked_at_ms,
            "received_at_ms": self.received_at_ms,
            "monotonic_ns": self.monotonic_ns,
            "settlement_grace_seconds": self.settlement_grace_seconds,
            "attestation_model": LOCAL_ATTESTATION_MODEL,
        }


@dataclass(frozen=True)
class RollingCohortAdmission:
    common_expiry_id: str
    canonical_pair_id: str
    expiry_ms: int
    capture_attempt_id: str
    market_5_id: str
    market_15_id: str
    condition_5_id: str
    condition_15_id: str
    discovered_at_ms: int
    received_at_ms: int
    monotonic_ns: int
    supporting_history_capture_attempt_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "common_expiry_id",
            "canonical_pair_id",
            "capture_attempt_id",
            "market_5_id",
            "market_15_id",
            "condition_5_id",
            "condition_15_id",
        ):
            _text(getattr(self, name), name=name)
        for name in ("expiry_ms", "discovered_at_ms", "received_at_ms", "monotonic_ns"):
            object.__setattr__(self, name, _integer(getattr(self, name), name=name))
        supporting_ids = tuple(
            _text(value, name="supporting_history_capture_attempt_ids")
            for value in self.supporting_history_capture_attempt_ids
        )
        if len(set(supporting_ids)) != len(supporting_ids):
            raise ValueError(
                "supporting_history_capture_attempt_ids cannot contain duplicates"
            )
        if self.capture_attempt_id in supporting_ids:
            raise ValueError(
                "supporting_history_capture_attempt_ids cannot include capture_attempt_id"
            )
        object.__setattr__(
            self,
            "supporting_history_capture_attempt_ids",
            supporting_ids,
        )
        if self.common_expiry_id != canonical_expiry_cluster_id(self.expiry_ms):
            raise ValueError("common_expiry_id is not canonical for expiry_ms")
        if self.discovered_at_ms > self.received_at_ms:
            raise ValueError("cohort discovery cannot postdate its receipt")

    def to_document(self) -> dict[str, Any]:
        document = asdict(self)
        document["supporting_history_capture_attempt_ids"] = list(
            self.supporting_history_capture_attempt_ids
        )
        document["schema_version"] = "btc-5m-15m-readiness-v08-cohort-admission.v1"
        return document


@dataclass(frozen=True)
class RollingDecisionReceipt:
    common_expiry_id: str
    decision_tau_seconds: int
    decision_at_ms: int
    receipt_received_at_ms: int
    monotonic_ns: int
    action: str
    action_payload_sha256: str
    edge_basis: str = "structural"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "common_expiry_id",
            _text(self.common_expiry_id, name="common_expiry_id"),
        )
        object.__setattr__(
            self,
            "decision_tau_seconds",
            _integer(self.decision_tau_seconds, name="decision_tau_seconds"),
        )
        if self.decision_tau_seconds <= 0:
            raise ValueError("decision_tau_seconds must be positive")
        for name in ("decision_at_ms", "receipt_received_at_ms", "monotonic_ns"):
            object.__setattr__(self, name, _integer(getattr(self, name), name=name))
        if self.receipt_received_at_ms < self.decision_at_ms:
            raise ValueError("decision receipt cannot precede decision time")
        object.__setattr__(self, "action", _text(self.action, name="action"))
        object.__setattr__(
            self,
            "action_payload_sha256",
            _sha256(self.action_payload_sha256, name="action_payload_sha256"),
        )
        object.__setattr__(self, "edge_basis", _text(self.edge_basis, name="edge_basis"))

    def to_document(self) -> dict[str, Any]:
        document = asdict(self)
        document["schema_version"] = "btc-5m-15m-readiness-v08-decision.v1"
        return document


@dataclass(frozen=True)
class RollingCohortOutcome:
    common_expiry_id: str
    outcome: str
    clean: bool
    finalized_at_ms: int
    received_at_ms: int
    monotonic_ns: int
    realized_net_pnl: str | None
    evidence_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "common_expiry_id",
            _text(self.common_expiry_id, name="common_expiry_id"),
        )
        object.__setattr__(self, "outcome", _text(self.outcome, name="outcome"))
        if self.outcome not in ALLOWED_OUTCOMES:
            raise ValueError("unsupported rolling cohort outcome")
        if not isinstance(self.clean, bool):
            raise TypeError("clean must be bool")
        for name in ("finalized_at_ms", "received_at_ms", "monotonic_ns"):
            object.__setattr__(self, name, _integer(getattr(self, name), name=name))
        if self.received_at_ms < self.finalized_at_ms:
            raise ValueError("outcome receipt cannot precede finalization")
        expected_clean = self.outcome not in {
            "capture_dirty",
            "rule_or_fee_dirty",
            "rtds_gap_dirty",
        }
        if self.clean is not expected_clean:
            raise ValueError("outcome clean flag disagrees with its status")
        if self.realized_net_pnl is not None:
            object.__setattr__(
                self,
                "realized_net_pnl",
                _text(self.realized_net_pnl, name="realized_net_pnl"),
            )
        object.__setattr__(
            self,
            "evidence_sha256",
            _sha256(self.evidence_sha256, name="evidence_sha256"),
        )

    def to_document(self) -> dict[str, Any]:
        document = asdict(self)
        document["schema_version"] = "btc-5m-15m-readiness-v08-outcome.v1"
        return document


def _validated_document(cls: type[Any], candidate: Mapping[str, Any]) -> Any:
    payload = dict(candidate)
    payload.pop("schema_version", None)
    allowed = {field.name for field in fields(cls)}
    filtered = {key: value for key, value in payload.items() if key in allowed}
    return cls(**filtered)


def _receipts_dir(root: Path) -> Path:
    return root / "receipts"


def _safe_root(value: str | Path) -> Path:
    root = Path(value).expanduser().absolute()
    current = root
    while True:
        if current.exists() and current.is_symlink():
            raise ValueError("rolling journal path cannot contain symlinks")
        if current.parent == current:
            break
        current = current.parent
    return root


def _read_receipts(root: Path) -> tuple[dict[str, Any], ...]:
    directory = _receipts_dir(root)
    if not directory.exists():
        return ()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("rolling receipt directory must be a regular directory")
    receipts: list[dict[str, Any]] = []
    for expected_sequence, path in enumerate(sorted(directory.glob("*.json")), start=1):
        if path.is_symlink() or not path.is_file():
            raise ValueError("rolling receipt must be a regular file")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("rolling receipt is unreadable") from exc
        if not isinstance(document, dict):
            raise TypeError("rolling receipt must be a JSON object")
        if document.get("sequence") != expected_sequence:
            raise ValueError("rolling receipt sequence is not contiguous")
        receipts.append(document)
    return tuple(receipts)


def _sample_writer_clock() -> tuple[int, int]:
    recorded_at_ms = time.time_ns() // 1_000_000
    monotonic_ns = time.monotonic_ns()
    return recorded_at_ms, monotonic_ns


def _writer_clock_precedes_source(
    recorded_at_ms: int,
    source_received_at_ms: int,
) -> bool:
    return (
        recorded_at_ms + WRITER_SOURCE_CLOCK_SKEW_TOLERANCE_MS
        < source_received_at_ms
    )


def _append_receipt(
    root: Path,
    *,
    kind: str,
    body: Mapping[str, Any],
    expected_receipt_count: int,
    recorded_at_not_before_ms: int,
    recorded_at_strictly_before_ms: int | None = None,
    recorded_at_without_skew_not_before_ms: int | None = None,
    before_error: str,
    after_error: str | None = None,
    without_skew_before_error: str | None = None,
) -> dict[str, Any]:
    root = _safe_root(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / ".append.lock"
    try:
        lock_descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("rolling journal append lock is already held") from exc
    try:
        receipts = _read_receipts(root)
        if len(receipts) != expected_receipt_count:
            raise ValueError("rolling journal changed during append")
        previous = receipts[-1]["receipt_sha256"] if receipts else None
        recorded_at_ms, monotonic_ns = _sample_writer_clock()
        writer_session_id = _WRITER_SESSION_ID
        if receipts:
            last = receipts[-1]
            previous_recorded_at_ms = _integer(
                last.get("recorded_at_ms"),
                name="previous recorded_at_ms",
            )
            previous_monotonic_ns = _integer(
                last.get("monotonic_ns"),
                name="previous monotonic_ns",
            )
            previous_writer_session_id = last.get("writer_session_id")
            if recorded_at_ms < previous_recorded_at_ms:
                raise ValueError("writer wall clock regressed relative to prior receipt")
            if (
                previous_writer_session_id == writer_session_id
                and monotonic_ns <= previous_monotonic_ns
            ):
                raise ValueError(
                    "writer monotonic clock regressed within the active process"
                )
        if (
            recorded_at_without_skew_not_before_ms is not None
            and recorded_at_ms < recorded_at_without_skew_not_before_ms
        ):
            raise ValueError(
                without_skew_before_error
                or "writer receipt precedes its causal deadline"
            )
        if _writer_clock_precedes_source(recorded_at_ms, recorded_at_not_before_ms):
            raise ValueError(before_error)
        if (
            recorded_at_strictly_before_ms is not None
            and recorded_at_ms >= recorded_at_strictly_before_ms
        ):
            raise ValueError(after_error or "writer receipt missed its deadline")
        envelope = {
            "schema_version": RECEIPT_SCHEMA,
            "sequence": len(receipts) + 1,
            "kind": _text(kind, name="kind"),
            "recorded_at_ms": _integer(recorded_at_ms, name="recorded_at_ms"),
            "monotonic_ns": _integer(monotonic_ns, name="monotonic_ns"),
            "writer_session_id": writer_session_id,
            "previous_receipt_sha256": previous,
            "body": dict(body),
        }
        document = {**envelope, "receipt_sha256": _digest(envelope)}
        directory = _receipts_dir(root)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{envelope['sequence']:09d}-{kind}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_json_bytes(document) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        try:
            directory_descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
            finally:
                os.close(directory_descriptor)
        return document
    finally:
        os.close(lock_descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _state(root: Path) -> dict[str, Any]:
    audit = audit_rolling_shadow(root)
    if not audit["valid"]:
        raise ValueError("rolling shadow journal is invalid")
    return audit


def initialize_rolling_shadow(
    root: str | Path, policy: RollingInclusionPolicy
) -> dict[str, Any]:
    root = Path(root)
    safe_root = _safe_root(root)
    if _read_receipts(safe_root):
        raise ValueError("rolling shadow policy is already initialized")
    _append_receipt(
        safe_root,
        kind="policy_locked",
        body={"policy": policy.to_document(), "policy_sha256": policy.policy_sha256},
        expected_receipt_count=0,
        recorded_at_not_before_ms=policy.received_at_ms,
        recorded_at_strictly_before_ms=policy.track_starts_at_ms,
        before_error="policy writer clock precedes its source receipt",
        after_error="policy receipt must be writer-recorded before track start",
    )
    return audit_rolling_shadow(safe_root)


def admit_rolling_cohort(
    root: str | Path, admission: RollingCohortAdmission
) -> dict[str, Any]:
    root = Path(root)
    state = _state(root)
    policy = state["policy"]
    if admission.common_expiry_id in state["admissions"]:
        raise ValueError("common expiry was already admitted; replacement is forbidden")
    if admission.expiry_ms < policy["track_starts_at_ms"]:
        raise ValueError("cohort predates the locked rolling track")
    earliest_decision = (
        admission.expiry_ms - max(policy["decision_tau_seconds"]) * 1_000
    )
    if admission.received_at_ms >= earliest_decision:
        raise ValueError(
            "cohort source receipt must precede its first locked decision"
        )
    safe_root = _safe_root(root)
    _append_receipt(
        safe_root,
        kind="cohort_admitted",
        body={
            "policy_sha256": state["policy_sha256"],
            "admission": admission.to_document(),
        },
        expected_receipt_count=state["receipt_count"],
        recorded_at_not_before_ms=admission.received_at_ms,
        recorded_at_strictly_before_ms=earliest_decision,
        before_error="cohort writer clock precedes its source receipt",
        after_error="cohort must be writer-recorded before its first decision",
    )
    return audit_rolling_shadow(safe_root)


def append_rolling_decision(
    root: str | Path, decision: RollingDecisionReceipt
) -> dict[str, Any]:
    root = Path(root)
    state = _state(root)
    admission = state["admissions"].get(decision.common_expiry_id)
    if admission is None:
        raise ValueError("decision cohort was not pre-admitted")
    if decision.decision_tau_seconds not in state["policy"]["decision_tau_seconds"]:
        raise ValueError("decision tau is outside the locked rolling policy")
    expected_decision_at = (
        admission["expiry_ms"] - decision.decision_tau_seconds * 1_000
    )
    if decision.decision_at_ms != expected_decision_at:
        raise ValueError("decision timestamp does not match expiry minus locked tau")
    if decision.receipt_received_at_ms >= admission["expiry_ms"]:
        raise ValueError("decision source receipt must be strictly pre-label")
    safe_root = _safe_root(root)
    identity = f"{decision.common_expiry_id}:{decision.decision_tau_seconds}"
    if identity in state["decisions"]:
        raise ValueError("decision receipt already exists and cannot be replaced")
    if decision.edge_basis != "structural":
        raise ValueError("rolling live-eligibility journal is structural-only")
    if decision.action not in {
        "no_trade",
        "long_15_up_long_5_down",
        "long_5_up_long_15_down",
    }:
        raise ValueError("decision action is not a structural-floor action")
    _sha256(decision.action_payload_sha256, name="action_payload_sha256")
    _append_receipt(
        safe_root,
        kind="decision_locked",
        body={"decision": decision.to_document()},
        expected_receipt_count=state["receipt_count"],
        recorded_at_not_before_ms=decision.receipt_received_at_ms,
        recorded_at_strictly_before_ms=admission["expiry_ms"],
        recorded_at_without_skew_not_before_ms=decision.decision_at_ms,
        before_error="decision receipt cannot be writer-recorded before decision time",
        after_error="decision receipt must be writer-recorded strictly pre-label",
        without_skew_before_error=(
            "decision receipt cannot be writer-recorded before decision time"
        ),
    )
    return audit_rolling_shadow(safe_root)


def finalize_rolling_cohort(
    root: str | Path, outcome: RollingCohortOutcome
) -> dict[str, Any]:
    root = Path(root)
    state = _state(root)
    admission = state["admissions"].get(outcome.common_expiry_id)
    if admission is None:
        raise ValueError("outcome cohort was not pre-admitted")
    if outcome.common_expiry_id in state["outcomes"]:
        raise ValueError("cohort outcome already exists and cannot be replaced")
    if outcome.finalized_at_ms < admission["expiry_ms"]:
        raise ValueError("cohort cannot finalize before common expiry")
    settlement_grace_ms = (
        _integer(
            state["policy"].get(
                "settlement_grace_seconds",
                DEFAULT_SETTLEMENT_GRACE_SECONDS,
            ),
            name="policy settlement_grace_seconds",
        )
        * 1_000
    )
    finalize_deadline_ms = admission["expiry_ms"] + settlement_grace_ms
    if outcome.finalized_at_ms > finalize_deadline_ms:
        raise ValueError("cohort finalization exceeds the locked settlement grace")
    if outcome.received_at_ms > finalize_deadline_ms:
        raise ValueError("outcome receipt exceeds the locked settlement grace")
    safe_root = _safe_root(root)
    _append_receipt(
        safe_root,
        kind="cohort_finalized",
        body={"outcome": outcome.to_document()},
        expected_receipt_count=state["receipt_count"],
        recorded_at_not_before_ms=outcome.received_at_ms,
        recorded_at_strictly_before_ms=finalize_deadline_ms + 1,
        before_error="outcome receipt cannot be writer-recorded before finalization",
        after_error="outcome receipt exceeds the locked settlement grace",
    )
    return audit_rolling_shadow(safe_root)


def audit_rolling_shadow(root: str | Path) -> dict[str, Any]:
    root = _safe_root(root)
    receipts = _read_receipts(root)
    errors: list[str] = []
    previous: str | None = None
    policy: dict[str, Any] | None = None
    policy_sha256: str | None = None
    admissions: dict[str, dict[str, Any]] = {}
    decisions: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    previous_recorded_at_ms: int | None = None
    previous_monotonic_ns: int | None = None
    previous_writer_session_id: str | None = None
    for expected_sequence, receipt in enumerate(receipts, start=1):
        unsigned = {
            key: value for key, value in receipt.items() if key != "receipt_sha256"
        }
        if receipt.get("schema_version") != RECEIPT_SCHEMA:
            errors.append(f"unsupported_schema_at_{expected_sequence}")
        if receipt.get("previous_receipt_sha256") != previous:
            errors.append(f"chain_break_at_{expected_sequence}")
        if receipt.get("receipt_sha256") != _digest(unsigned):
            errors.append(f"hash_mismatch_at_{expected_sequence}")
        recorded_at_ms = receipt.get("recorded_at_ms")
        monotonic_ns = receipt.get("monotonic_ns")
        writer_session_id = receipt.get("writer_session_id")
        if writer_session_id is not None and not isinstance(writer_session_id, str):
            errors.append(f"writer_session_invalid_at_{expected_sequence}")
            writer_session_id = None
        if (
            isinstance(recorded_at_ms, bool)
            or not isinstance(recorded_at_ms, int)
            or isinstance(monotonic_ns, bool)
            or not isinstance(monotonic_ns, int)
        ):
            errors.append(f"writer_clock_invalid_at_{expected_sequence}")
        else:
            if (
                previous_recorded_at_ms is not None
                and recorded_at_ms < previous_recorded_at_ms
            ):
                errors.append(f"writer_clock_regressed_at_{expected_sequence}")
            if (
                previous_monotonic_ns is not None
                and previous_writer_session_id == writer_session_id
                and monotonic_ns <= previous_monotonic_ns
            ):
                errors.append(f"writer_monotonic_regressed_at_{expected_sequence}")
            previous_recorded_at_ms = recorded_at_ms
            previous_monotonic_ns = monotonic_ns
            previous_writer_session_id = writer_session_id
        previous = receipt.get("receipt_sha256")
        body = receipt.get("body")
        if not isinstance(body, Mapping):
            errors.append(f"body_invalid_at_{expected_sequence}")
            continue
        kind = receipt.get("kind")
        if kind == "policy_locked":
            if expected_sequence != 1 or policy is not None:
                errors.append("policy_not_unique_first_receipt")
                continue
            candidate = body.get("policy")
            if (
                not isinstance(candidate, dict)
                or candidate.get("schema_version") != POLICY_SCHEMA
            ):
                errors.append("policy_document_invalid")
                continue
            try:
                _validated_document(RollingInclusionPolicy, candidate)
            except (TypeError, ValueError):
                errors.append("policy_document_invalid")
                continue
            policy = candidate
            policy_sha256 = body.get("policy_sha256")
            if policy_sha256 != _digest(policy):
                errors.append("policy_hash_mismatch")
            if isinstance(recorded_at_ms, int):
                if recorded_at_ms >= policy["track_starts_at_ms"]:
                    errors.append("policy_writer_clock_not_before_track_start")
                if _writer_clock_precedes_source(recorded_at_ms, policy["received_at_ms"]):
                    errors.append("policy_writer_clock_precedes_body")
        elif kind == "cohort_admitted":
            candidate = body.get("admission")
            if not isinstance(candidate, dict):
                errors.append(f"admission_invalid_at_{expected_sequence}")
                continue
            try:
                admission = _validated_document(RollingCohortAdmission, candidate)
            except (TypeError, ValueError):
                errors.append(f"admission_invalid_at_{expected_sequence}")
                continue
            cohort_id = candidate.get("common_expiry_id")
            if not isinstance(cohort_id, str) or cohort_id in admissions:
                errors.append(f"duplicate_or_invalid_admission_at_{expected_sequence}")
                continue
            if body.get("policy_sha256") != policy_sha256:
                errors.append(f"admission_policy_mismatch_at_{expected_sequence}")
            if policy is not None:
                earliest_decision = (
                    admission.expiry_ms - max(policy["decision_tau_seconds"]) * 1_000
                )
                if admission.expiry_ms < policy["track_starts_at_ms"]:
                    errors.append(f"admission_before_track_at_{expected_sequence}")
                if admission.received_at_ms >= earliest_decision:
                    errors.append(f"admission_after_first_decision_at_{expected_sequence}")
                if (
                    isinstance(recorded_at_ms, int)
                    and recorded_at_ms >= earliest_decision
                ):
                    errors.append(
                        f"admission_writer_clock_after_first_decision_at_{expected_sequence}"
                    )
            if (
                isinstance(recorded_at_ms, int)
                and _writer_clock_precedes_source(
                    recorded_at_ms,
                    admission.received_at_ms,
                )
            ):
                errors.append(
                    f"admission_writer_clock_precedes_body_at_{expected_sequence}"
                )
            admissions[cohort_id] = candidate
        elif kind == "decision_locked":
            candidate = body.get("decision")
            if not isinstance(candidate, dict):
                errors.append(f"decision_invalid_at_{expected_sequence}")
                continue
            try:
                decision = _validated_document(RollingDecisionReceipt, candidate)
            except (TypeError, ValueError):
                errors.append(f"decision_invalid_at_{expected_sequence}")
                continue
            identity = f"{candidate.get('common_expiry_id')}:{candidate.get('decision_tau_seconds')}"
            if (
                candidate.get("common_expiry_id") not in admissions
                or identity in decisions
            ):
                errors.append(f"orphan_or_duplicate_decision_at_{expected_sequence}")
                continue
            admission = admissions[candidate["common_expiry_id"]]
            expected_decision_at = admission["expiry_ms"] - decision.decision_tau_seconds * 1_000
            if decision.decision_at_ms != expected_decision_at:
                errors.append(f"decision_time_mismatch_at_{expected_sequence}")
            if decision.receipt_received_at_ms >= admission["expiry_ms"]:
                errors.append(f"decision_post_label_at_{expected_sequence}")
            if policy is not None and decision.decision_tau_seconds not in policy["decision_tau_seconds"]:
                errors.append(f"decision_tau_outside_policy_at_{expected_sequence}")
            if decision.edge_basis != "structural":
                errors.append(f"decision_edge_basis_invalid_at_{expected_sequence}")
            if isinstance(recorded_at_ms, int):
                if recorded_at_ms < decision.decision_at_ms:
                    errors.append(
                        f"decision_writer_clock_precedes_decision_at_{expected_sequence}"
                    )
                if recorded_at_ms >= admission["expiry_ms"]:
                    errors.append(
                        f"decision_writer_clock_post_label_at_{expected_sequence}"
                    )
                if _writer_clock_precedes_source(
                    recorded_at_ms,
                    decision.receipt_received_at_ms,
                ):
                    errors.append(
                        f"decision_writer_clock_precedes_body_at_{expected_sequence}"
                    )
            decisions[identity] = candidate
        elif kind == "cohort_finalized":
            candidate = body.get("outcome")
            if not isinstance(candidate, dict):
                errors.append(f"outcome_invalid_at_{expected_sequence}")
                continue
            try:
                outcome = _validated_document(RollingCohortOutcome, candidate)
            except (TypeError, ValueError):
                errors.append(f"outcome_invalid_at_{expected_sequence}")
                continue
            cohort_id = candidate.get("common_expiry_id")
            if cohort_id not in admissions or cohort_id in outcomes:
                errors.append(f"orphan_or_duplicate_outcome_at_{expected_sequence}")
                continue
            if outcome.finalized_at_ms < admissions[cohort_id]["expiry_ms"]:
                errors.append(f"outcome_pre_expiry_at_{expected_sequence}")
            settlement_grace_ms = (
                _integer(
                    policy.get(
                        "settlement_grace_seconds",
                        DEFAULT_SETTLEMENT_GRACE_SECONDS,
                    ),
                    name="policy settlement_grace_seconds",
                )
                * 1_000
                if policy is not None
                else DEFAULT_SETTLEMENT_GRACE_SECONDS * 1_000
            )
            finalize_deadline_ms = admissions[cohort_id]["expiry_ms"] + settlement_grace_ms
            if outcome.finalized_at_ms > finalize_deadline_ms:
                errors.append(f"outcome_settlement_grace_exceeded_at_{expected_sequence}")
            if outcome.received_at_ms > finalize_deadline_ms:
                errors.append(
                    f"outcome_receipt_settlement_grace_exceeded_at_{expected_sequence}"
                )
            if (
                isinstance(recorded_at_ms, int)
                and _writer_clock_precedes_source(
                    recorded_at_ms,
                    outcome.received_at_ms,
                )
            ):
                errors.append(f"outcome_writer_clock_precedes_body_at_{expected_sequence}")
            if isinstance(recorded_at_ms, int) and recorded_at_ms > finalize_deadline_ms:
                errors.append(
                    f"outcome_writer_clock_settlement_grace_exceeded_at_{expected_sequence}"
                )
            outcomes[cohort_id] = candidate
        else:
            errors.append(f"unknown_kind_at_{expected_sequence}")
    if policy is None:
        errors.append("policy_missing")
    clean = sum(1 for outcome in outcomes.values() if outcome.get("clean") is True)
    dirty = sum(1 for outcome in outcomes.values() if outcome.get("clean") is False)
    outcome_counts = {
        name: sum(1 for outcome in outcomes.values() if outcome.get("outcome") == name)
        for name in sorted(ALLOWED_OUTCOMES)
    }
    return {
        "schema_version": AUDIT_SCHEMA,
        "journal_root": str(root),
        "valid": not errors,
        "errors": errors,
        "attestation_model": LOCAL_ATTESTATION_MODEL,
        "policy": policy,
        "policy_sha256": policy_sha256,
        "receipt_count": len(receipts),
        "admitted_common_expiry_count": len(admissions),
        "finalized_common_expiry_count": len(outcomes),
        "clean_common_expiry_count": clean,
        "dirty_common_expiry_count": dirty,
        "unfinished_common_expiry_count": len(admissions) - len(outcomes),
        "decision_receipt_count": len(decisions),
        "outcome_counts": outcome_counts,
        "admissions": admissions,
        "decisions": decisions,
        "outcomes": outcomes,
        "denominator_includes_no_trade_no_fill_and_dirty": True,
        "predictive_live_fallback_allowed": False,
    }


__all__ = [
    "ALLOWED_OUTCOMES",
    "INCLUSION_RULE_ID",
    "LOCAL_ATTESTATION_MODEL",
    "RollingCohortAdmission",
    "RollingCohortOutcome",
    "RollingDecisionReceipt",
    "RollingInclusionPolicy",
    "admit_rolling_cohort",
    "append_rolling_decision",
    "audit_rolling_shadow",
    "finalize_rolling_cohort",
    "initialize_rolling_shadow",
]
