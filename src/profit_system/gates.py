from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from src.edge_lab.data_store import canonical_json_bytes

ZERO = Decimal("0")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _required_bool(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be bool")
    return value


def _required_int(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be int")
    if value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return int(value)


def _required_decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise TypeError(f"{label} must be decimal-compatible")
    else:
        result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _required_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _required_hash(value: Any, *, label: str) -> str:
    digest = _required_string(value, label=label)
    if _SHA256.fullmatch(digest) is None:
        raise ValueError(f"{label} must be a lowercase sha256 hex digest")
    return digest


def _serialize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value


@dataclass(frozen=True)
class GateDecision:
    gate_name: str
    strategy_id: str
    strategy_version: str
    status: str
    go: bool
    preregistration_sha256: str
    config_sha256: str
    reasons: tuple[str, ...]
    metrics: dict[str, Any]
    thresholds: dict[str, Any]
    evidence_refs: tuple[str, ...] = ()
    report_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "gate_name", _required_string(self.gate_name, label="gate_name"))
        object.__setattr__(
            self, "strategy_id", _required_string(self.strategy_id, label="strategy_id")
        )
        object.__setattr__(
            self,
            "strategy_version",
            _required_string(self.strategy_version, label="strategy_version"),
        )
        status = _required_string(self.status, label="status")
        if status not in {"GO", "NO_GO"}:
            raise ValueError("status must be GO or NO_GO")
        object.__setattr__(self, "status", status)
        object.__setattr__(
            self,
            "preregistration_sha256",
            _required_hash(self.preregistration_sha256, label="preregistration_sha256"),
        )
        object.__setattr__(
            self, "config_sha256", _required_hash(self.config_sha256, label="config_sha256")
        )
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))
        object.__setattr__(self, "metrics", dict(self.metrics))
        object.__setattr__(self, "thresholds", dict(self.thresholds))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        unsigned = self.to_document(include_hash=False)
        object.__setattr__(
            self,
            "report_sha256",
            hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest(),
        )

    def to_document(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload = cast(
            dict[str, Any],
            _serialize(
                {
                    "gate_name": self.gate_name,
                    "strategy_id": self.strategy_id,
                    "strategy_version": self.strategy_version,
                    "status": self.status,
                    "go": self.go,
                    "preregistration_sha256": self.preregistration_sha256,
                    "config_sha256": self.config_sha256,
                    "reasons": list(self.reasons),
                    "metrics": dict(self.metrics),
                    "thresholds": dict(self.thresholds),
                    "evidence_refs": list(self.evidence_refs),
                }
            ),
        )
        if include_hash:
            payload["report_sha256"] = self.report_sha256
        return payload


class ResearchGate:
    def evaluate(self, evidence: dict[str, Any]) -> GateDecision:
        reasons: list[str] = []
        baseline_expected_net_edge = _required_decimal(
            evidence["baseline_expected_net_edge"], label="baseline_expected_net_edge"
        )
        ablation_expected_net_edge = _required_decimal(
            evidence["ablation_expected_net_edge"], label="ablation_expected_net_edge"
        )
        stressed_expected_net_edge = _required_decimal(
            evidence["stressed_expected_net_edge"], label="stressed_expected_net_edge"
        )
        if not _required_bool(evidence["chronological_oos"], label="chronological_oos"):
            reasons.append("chronological_oos_required")
        if not _required_bool(evidence["no_future_leakage"], label="no_future_leakage"):
            reasons.append("future_leakage_detected")
        if not _required_bool(evidence["preregistered"], label="preregistered"):
            reasons.append("missing_preregistration")
        if not _required_bool(evidence["baseline_present"], label="baseline_present"):
            reasons.append("baseline_missing")
        if not _required_bool(evidence["ablation_present"], label="ablation_present"):
            reasons.append("ablation_missing")
        if not _required_bool(evidence["thresholds_frozen"], label="thresholds_frozen"):
            reasons.append("thresholds_not_frozen")
        if _required_decimal(
            evidence["stress_cost_multiplier"], label="stress_cost_multiplier"
        ) < Decimal("1.25"):
            reasons.append("stress_cost_multiplier_too_low")
        if _required_decimal(
            evidence["stress_slippage_multiplier"], label="stress_slippage_multiplier"
        ) < Decimal("1.25"):
            reasons.append("stress_slippage_multiplier_too_low")
        if _required_decimal(
            evidence["stress_fill_rate_multiplier"], label="stress_fill_rate_multiplier"
        ) > Decimal("0.75"):
            reasons.append("stress_fill_rate_not_harsh_enough")
        if baseline_expected_net_edge <= ZERO:
            reasons.append("baseline_edge_non_positive")
        if stressed_expected_net_edge <= ZERO:
            reasons.append("stressed_edge_non_positive")
        if baseline_expected_net_edge <= ablation_expected_net_edge:
            reasons.append("ablation_does_not_worsen_results")

        status = "GO" if not reasons else "NO_GO"
        return GateDecision(
            gate_name="ResearchGate",
            strategy_id=_required_string(evidence["strategy_id"], label="strategy_id"),
            strategy_version=_required_string(
                evidence["strategy_version"], label="strategy_version"
            ),
            status=status,
            go=not reasons,
            preregistration_sha256=_required_hash(
                evidence["preregistration_sha256"], label="preregistration_sha256"
            ),
            config_sha256=_required_hash(evidence["config_sha256"], label="config_sha256"),
            reasons=tuple(reasons),
            metrics={
                "baseline_expected_net_edge": baseline_expected_net_edge,
                "ablation_expected_net_edge": ablation_expected_net_edge,
                "stressed_expected_net_edge": stressed_expected_net_edge,
                "candidate_count": _required_int(
                    evidence["candidate_count"], label="candidate_count", minimum=0
                ),
                "executable_count": _required_int(
                    evidence["executable_count"], label="executable_count", minimum=0
                ),
            },
            thresholds={
                "chronological_oos_required": True,
                "no_future_leakage_required": True,
                "baseline_required": True,
                "ablation_required": True,
                "stress_cost_multiplier_min": "1.25",
                "stress_slippage_multiplier_min": "1.25",
                "stress_fill_rate_multiplier_max": "0.75",
                "thresholds_frozen_required": True,
            },
            evidence_refs=tuple(evidence.get("evidence_refs", ())),
        )


class ShadowGate:
    def evaluate(self, evidence: dict[str, Any]) -> GateDecision:
        reasons: list[str] = []
        observation_days = _required_int(
            evidence["observation_days"], label="observation_days", minimum=0
        )
        candidate_count = _required_int(
            evidence["candidate_count"], label="candidate_count", minimum=0
        )
        executable_count = _required_int(
            evidence["executable_count"], label="executable_count", minimum=0
        )
        expected_net_edge = _required_decimal(
            evidence["expected_net_edge"], label="expected_net_edge"
        )
        if observation_days < 14:
            reasons.append("shadow_days_below_minimum")
        if candidate_count < 50:
            reasons.append("shadow_candidates_below_minimum")
        if executable_count < 20:
            reasons.append("shadow_executable_below_minimum")
        if not _required_bool(evidence["all_decisions_recorded"], label="all_decisions_recorded"):
            reasons.append("shadow_decisions_not_fully_recorded")
        if expected_net_edge <= ZERO:
            reasons.append("shadow_expected_edge_non_positive")
        if not _required_bool(evidence["data_integrity_ok"], label="data_integrity_ok"):
            reasons.append("shadow_data_integrity_failed")
        if not _required_bool(evidence["state_drift_ok"], label="state_drift_ok"):
            reasons.append("shadow_state_drift_failed")
        if not _required_bool(evidence["thresholds_frozen"], label="thresholds_frozen"):
            reasons.append("shadow_thresholds_not_frozen")

        status = "GO" if not reasons else "NO_GO"
        return GateDecision(
            gate_name="ShadowGate",
            strategy_id=_required_string(evidence["strategy_id"], label="strategy_id"),
            strategy_version=_required_string(
                evidence["strategy_version"], label="strategy_version"
            ),
            status=status,
            go=not reasons,
            preregistration_sha256=_required_hash(
                evidence["preregistration_sha256"], label="preregistration_sha256"
            ),
            config_sha256=_required_hash(evidence["config_sha256"], label="config_sha256"),
            reasons=tuple(reasons),
            metrics={
                "observation_days": observation_days,
                "candidate_count": candidate_count,
                "executable_count": executable_count,
                "expected_net_edge": expected_net_edge,
            },
            thresholds={
                "observation_days_min": 14,
                "candidate_count_min": 50,
                "executable_count_min": 20,
                "expected_net_edge_positive": True,
                "all_decisions_recorded_required": True,
                "thresholds_frozen_required": True,
            },
            evidence_refs=tuple(evidence.get("evidence_refs", ())),
        )
