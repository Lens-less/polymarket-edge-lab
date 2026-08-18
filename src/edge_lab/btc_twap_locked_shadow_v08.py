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
from collections.abc import Mapping
from dataclasses import asdict, dataclass
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

    def to_document(self) -> dict[str, Any]:
        document = asdict(self)
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

    def to_document(self) -> dict[str, Any]:
        document = asdict(self)
        document["schema_version"] = "btc-5m-15m-readiness-v08-outcome.v1"
        return document


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


def _append_receipt(
    root: Path,
    *,
    kind: str,
    body: Mapping[str, Any],
    recorded_at_ms: int,
    monotonic_ns: int,
) -> dict[str, Any]:
    root = _safe_root(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / ".append.lock"
    lock_descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        receipts = _read_receipts(root)
        previous = receipts[-1]["receipt_sha256"] if receipts else None
        envelope = {
            "schema_version": RECEIPT_SCHEMA,
            "sequence": len(receipts) + 1,
            "kind": _text(kind, name="kind"),
            "recorded_at_ms": _integer(recorded_at_ms, name="recorded_at_ms"),
            "monotonic_ns": _integer(monotonic_ns, name="monotonic_ns"),
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
    if _read_receipts(_safe_root(root)):
        raise ValueError("rolling shadow policy is already initialized")
    _append_receipt(
        root,
        kind="policy_locked",
        body={"policy": policy.to_document(), "policy_sha256": policy.policy_sha256},
        recorded_at_ms=policy.received_at_ms,
        monotonic_ns=policy.monotonic_ns,
    )
    return audit_rolling_shadow(root)


def admit_rolling_cohort(
    root: str | Path, admission: RollingCohortAdmission
) -> dict[str, Any]:
    root = Path(root)
    state = _state(root)
    policy = state["policy"]
    for name in (
        "common_expiry_id",
        "canonical_pair_id",
        "capture_attempt_id",
        "market_5_id",
        "market_15_id",
        "condition_5_id",
        "condition_15_id",
    ):
        _text(getattr(admission, name), name=name)
    for name in ("expiry_ms", "discovered_at_ms", "received_at_ms", "monotonic_ns"):
        _integer(getattr(admission, name), name=name)
    if admission.common_expiry_id != canonical_expiry_cluster_id(admission.expiry_ms):
        raise ValueError("common_expiry_id is not canonical for expiry_ms")
    if admission.common_expiry_id in state["admissions"]:
        raise ValueError("common expiry was already admitted; replacement is forbidden")
    if admission.expiry_ms < policy["track_starts_at_ms"]:
        raise ValueError("cohort predates the locked rolling track")
    earliest_decision = (
        admission.expiry_ms - max(policy["decision_tau_seconds"]) * 1_000
    )
    if admission.discovered_at_ms > admission.received_at_ms:
        raise ValueError("cohort discovery cannot postdate its receipt")
    if admission.received_at_ms >= earliest_decision:
        raise ValueError("cohort must be admitted before its first decision")
    _append_receipt(
        root,
        kind="cohort_admitted",
        body={
            "policy_sha256": state["policy_sha256"],
            "admission": admission.to_document(),
        },
        recorded_at_ms=admission.received_at_ms,
        monotonic_ns=admission.monotonic_ns,
    )
    return audit_rolling_shadow(root)


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
        raise ValueError("decision receipt must be strictly pre-label")
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
        root,
        kind="decision_locked",
        body={"decision": decision.to_document()},
        recorded_at_ms=decision.receipt_received_at_ms,
        monotonic_ns=decision.monotonic_ns,
    )
    return audit_rolling_shadow(root)


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
    if outcome.outcome not in ALLOWED_OUTCOMES:
        raise ValueError("unsupported rolling cohort outcome")
    if outcome.finalized_at_ms < admission["expiry_ms"]:
        raise ValueError("cohort cannot finalize before common expiry")
    if outcome.received_at_ms < outcome.finalized_at_ms:
        raise ValueError("outcome receipt cannot precede finalization")
    if outcome.clean is not (
        outcome.outcome
        not in {
            "capture_dirty",
            "rule_or_fee_dirty",
            "rtds_gap_dirty",
        }
    ):
        raise ValueError("outcome clean flag disagrees with its status")
    _sha256(outcome.evidence_sha256, name="evidence_sha256")
    _append_receipt(
        root,
        kind="cohort_finalized",
        body={"outcome": outcome.to_document()},
        recorded_at_ms=outcome.received_at_ms,
        monotonic_ns=outcome.monotonic_ns,
    )
    return audit_rolling_shadow(root)


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
            policy = candidate
            policy_sha256 = body.get("policy_sha256")
            if policy_sha256 != _digest(policy):
                errors.append("policy_hash_mismatch")
        elif kind == "cohort_admitted":
            candidate = body.get("admission")
            if not isinstance(candidate, dict):
                errors.append(f"admission_invalid_at_{expected_sequence}")
                continue
            cohort_id = candidate.get("common_expiry_id")
            if not isinstance(cohort_id, str) or cohort_id in admissions:
                errors.append(f"duplicate_or_invalid_admission_at_{expected_sequence}")
                continue
            if body.get("policy_sha256") != policy_sha256:
                errors.append(f"admission_policy_mismatch_at_{expected_sequence}")
            admissions[cohort_id] = candidate
        elif kind == "decision_locked":
            candidate = body.get("decision")
            if not isinstance(candidate, dict):
                errors.append(f"decision_invalid_at_{expected_sequence}")
                continue
            identity = f"{candidate.get('common_expiry_id')}:{candidate.get('decision_tau_seconds')}"
            if (
                candidate.get("common_expiry_id") not in admissions
                or identity in decisions
            ):
                errors.append(f"orphan_or_duplicate_decision_at_{expected_sequence}")
                continue
            decisions[identity] = candidate
        elif kind == "cohort_finalized":
            candidate = body.get("outcome")
            if not isinstance(candidate, dict):
                errors.append(f"outcome_invalid_at_{expected_sequence}")
                continue
            cohort_id = candidate.get("common_expiry_id")
            if cohort_id not in admissions or cohort_id in outcomes:
                errors.append(f"orphan_or_duplicate_outcome_at_{expected_sequence}")
                continue
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
