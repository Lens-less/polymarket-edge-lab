"""Offline-safe readiness and gate reporting helpers for V0.2."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, TypeAlias

JSONValue: TypeAlias = (  # noqa: UP040
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_CANARY_CONFIG_PATH: Final = PROJECT_ROOT / "config" / "v0.2" / "canary.template.json"

GATE_REPORT_SCHEMA_VERSION: Final = "polymm-gate-report.v0.2"
LIVE_PROBE_RESULT_SCHEMA_VERSION: Final = "polymm-live-probe-result.v0.2"
FAULT_DRILL_RESULT_SCHEMA_VERSION: Final = "polymm-fault-drill-result.v0.2"
CANARY_CONFIG_SCHEMA_VERSION: Final = "polymm-canary-config.v0.2"

CHECK_READY: Final = "READY"
CHECK_BLOCKED: Final = "BLOCKED"

GATE_RESEARCH: Final = "RESEARCH"
GATE_SHADOW: Final = "SHADOW"
GATE_CANARY: Final = "CANARY"
GATE_LIMITED_LIVE: Final = "LIMITED_LIVE"

STATUS_PASS: Final = "PASS"
STATUS_NO_GO: Final = "NO_GO"
STATUS_LIVE_BLOCKED: Final = "LIVE_BLOCKED"
STATUS_LIVE_CANARY_READY: Final = "LIVE_CANARY_READY"
STATUS_LIVE_LIMITED_READY: Final = "LIVE_LIMITED_READY"

REQUIRED_CANARY_CHECKS: Final[tuple[str, ...]] = (
    "explicit_user_authorization",
    "geoblock",
    "credentials_signing",
    "balance_allowance",
    "market_constraints",
    "user_stream",
    "heartbeat",
    "reconciliation",
    "risk_config",
    "qualification",
    "kill_restart_drills",
)

REQUIRED_LIVE_PROBE_CHECKS: Final[tuple[str, ...]] = (
    "geoblock",
    "credentials_signing",
    "balance_allowance",
    "market_constraints",
    "user_stream",
    "heartbeat",
    "reconciliation",
)

REQUIRED_FAULT_DRILLS: Final[tuple[str, ...]] = (
    "kill_switch_manual_reset",
    "restart_recovery",
    "user_stream_disconnect_cancel_all",
    "heartbeat_timeout_blocks_new_orders",
    "reconciliation_mismatch_blocks_new_orders",
)

SAFE_PROBE_BOOL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "offline_safe",
        "verified",
        "close_only",
        "geoblocked",
        "backfill_complete",
    }
)
SAFE_ATTESTATION_BOOL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "certificate_chain_present",
        "signature_verified",
        "transparency_log_recorded",
    }
)
SAFE_ATTESTATION_STRING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "attestation_type",
        "issuer",
        "verifier",
        "rekor_log_id",
    }
)
_SAFE_HASH_RE: Final = re.compile(r"^[0-9a-f]{32,128}$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def utc_timestamp(value: datetime | None = None) -> str:
    timestamp = _utc_now() if value is None else value.astimezone(UTC)
    return timestamp.isoformat().replace("+00:00", "Z")


def decimal_to_string(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def to_jsonable(value: object) -> JSONValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return decimal_to_string(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return utc_timestamp(value)
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    raise TypeError(f"unsupported JSON value type: {type(value)!r}")


def _jsonable_object(value: object) -> dict[str, JSONValue]:
    normalized = to_jsonable(value)
    if not isinstance(normalized, dict):
        raise TypeError("value must serialize to a JSON object")
    return normalized


def dumps_json(document: dict[str, JSONValue]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _placeholder(value: object) -> bool:
    return (
        not isinstance(value, str)
        or not value.strip()
        or value.startswith("replace-with-")
        or value.startswith("TBD")
        or value.startswith("TODO")
    )


def _json_object(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _json_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _json_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _json_list(value: object) -> list[object] | None:
    return value if isinstance(value, list) else None


def _read_json_object(path: Path) -> tuple[dict[str, object] | None, str | None]:
    if not path.exists():
        return None, f"missing file: {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"unreadable JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "JSON root must be an object"
    return payload, None


def _resolve_embedded_or_path(
    root: dict[str, object],
    key: str,
) -> tuple[dict[str, object] | None, str | None]:
    embedded = _json_object(root.get(key))
    if embedded is not None:
        return embedded, None
    path_value = _json_string(root.get(f"{key}_path"))
    if path_value is None:
        return None, f"missing {key} document"
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return _read_json_object(candidate)


def _parse_decimal(
    value: object,
    *,
    field_name: str,
) -> tuple[Decimal | None, str | None]:
    if isinstance(value, bool) or value is None:
        return None, f"{field_name} must be a decimal string"
    try:
        return Decimal(str(value)), None
    except (InvalidOperation, ValueError):
        return None, f"{field_name} must be a decimal string"


def _sanitize_probe_evidence(value: dict[str, object]) -> dict[str, JSONValue]:
    sanitized: dict[str, JSONValue] = {}
    for key, item in value.items():
        if isinstance(item, bool) and key in SAFE_PROBE_BOOL_FIELDS:
            sanitized[key] = item
            continue
        if isinstance(item, int) and not isinstance(item, bool):
            if key.endswith(("_count", "_ms", "_seconds")) or key == "retry_after_seconds":
                sanitized[key] = item
            continue
        if isinstance(item, float) and key.endswith(("_ratio", "_score")):
            sanitized[key] = item
            continue
        if isinstance(item, str) and key.endswith("_hash") and _SAFE_HASH_RE.fullmatch(item):
            sanitized[key] = item
    return sanitized


def _sanitize_attestation_evidence(value: dict[str, object]) -> dict[str, JSONValue]:
    sanitized: dict[str, JSONValue] = {}
    for key, item in value.items():
        if isinstance(item, bool) and key in SAFE_ATTESTATION_BOOL_FIELDS:
            sanitized[key] = item
            continue
        if isinstance(item, str) and key in SAFE_ATTESTATION_STRING_FIELDS and item.strip():
            sanitized[key] = item
            continue
        if isinstance(item, str) and key.endswith("_hash") and _SAFE_HASH_RE.fullmatch(item):
            sanitized[key] = item
    return sanitized


def _offline_attestation_block(
    document: dict[str, object],
    *,
    evidence_label: str,
) -> tuple[str, dict[str, JSONValue]]:
    attestation = _json_object(document.get("provenance_attestation"))
    if attestation is None:
        return (
            (
                f"{evidence_label} is present, but offline doctor remains fail-closed until an "
                "externally verifiable provenance attestation is available."
            ),
            {
                "provenance_attestation": "missing",
                "doctor_verification": "unavailable",
            },
        )
    return (
        (
            f"{evidence_label} includes provenance attestation metadata, but offline doctor "
            "cannot verify external provenance yet."
        ),
        {
            "provenance_attestation": "present",
            "doctor_verification": "unavailable",
            **_sanitize_attestation_evidence(attestation),
        },
    )


@dataclass(frozen=True)
class GateCheck:
    check_id: str
    label: str
    status: str
    detail: str
    evidence: dict[str, JSONValue]

    def to_document(self) -> dict[str, JSONValue]:
        return {
            "check_id": self.check_id,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class GateReport:
    gate: str
    status: str
    summary: str
    checks: tuple[GateCheck, ...]
    blocking_check_ids: tuple[str, ...]
    remaining_conditions: tuple[str, ...]
    strategy_id: str
    generated_at: str
    context: dict[str, JSONValue]
    schema_version: str = GATE_REPORT_SCHEMA_VERSION

    def to_document(self) -> dict[str, JSONValue]:
        return {
            "schema_version": self.schema_version,
            "report_kind": "gate_report",
            "generated_at": self.generated_at,
            "gate": self.gate,
            "status": self.status,
            "strategy_id": self.strategy_id,
            "summary": self.summary,
            "blocking_check_ids": list(self.blocking_check_ids),
            "remaining_conditions": list(self.remaining_conditions),
            "checks": [check.to_document() for check in self.checks],
            "context": dict(self.context),
        }


def build_gate_report(
    *,
    gate: str,
    status: str,
    summary: str,
    strategy_id: str,
    checks: list[GateCheck],
    context: dict[str, JSONValue] | None = None,
    generated_at: datetime | None = None,
) -> GateReport:
    blocking = tuple(check.check_id for check in checks if check.status == CHECK_BLOCKED)
    return GateReport(
        gate=gate,
        status=status,
        summary=summary,
        checks=tuple(checks),
        blocking_check_ids=blocking,
        remaining_conditions=blocking,
        strategy_id=strategy_id,
        generated_at=utc_timestamp(generated_at),
        context={} if context is None else dict(context),
    )


def _build_explicit_user_authorization_check(
    document: dict[str, object],
) -> GateCheck:
    authorization = _json_object(document.get("authorization")) or {}
    explicit_live = _json_bool(authorization.get("explicit_live_authorization")) is True
    manual_confirmation = _json_bool(authorization.get("first_canary_manual_confirmation")) is True
    plan_id = _json_string(authorization.get("plan_id"))
    approved_by = _json_string(authorization.get("approved_by"))
    approved_at = _json_string(authorization.get("approved_at"))
    ready = (
        explicit_live
        and manual_confirmation
        and not _placeholder(plan_id)
        and not _placeholder(approved_by)
        and approved_at is not None
    )
    return GateCheck(
        check_id="explicit_user_authorization",
        label="Explicit user authorization",
        status=CHECK_READY if ready else CHECK_BLOCKED,
        detail=(
            "Reviewed live authorization and first-canary manual confirmation are both recorded."
            if ready
            else (
                "Canary requires explicit reviewed live authorization plus a "
                "first-canary manual confirmation."
            )
        ),
        evidence={
            "explicit_live_authorization": explicit_live,
            "first_canary_manual_confirmation": manual_confirmation,
            "plan_id_recorded": plan_id is not None and not _placeholder(plan_id),
            "approved_by_recorded": approved_by is not None and not _placeholder(approved_by),
            "approved_at_recorded": approved_at is not None,
        },
    )


def _build_risk_config_check(document: dict[str, object]) -> GateCheck:
    risk = _json_object(document.get("risk")) or {}
    profile_name = _json_string(document.get("profile_name"))
    decimal_fields = (
        "max_order_notional",
        "max_market_exposure",
        "max_event_exposure",
        "max_strategy_exposure",
        "max_total_exposure",
        "max_daily_loss",
        "max_drawdown",
        "max_unmatched_leg_seconds",
        "max_order_rate",
    )
    int_fields = (
        "max_open_orders",
        "max_market_data_age_ms",
        "max_user_stream_gap_seconds",
    )
    problems: list[str] = []
    evidence: dict[str, JSONValue] = {}
    parsed_decimals: dict[str, Decimal] = {}
    for field_name in decimal_fields:
        parsed, error = _parse_decimal(risk.get(field_name), field_name=field_name)
        if error is not None or parsed is None or parsed <= Decimal("0"):
            problems.append(field_name)
            evidence[field_name] = "invalid"
            continue
        parsed_decimals[field_name] = parsed
        evidence[field_name] = decimal_to_string(parsed)
    for field_name in int_fields:
        raw_value = risk.get(field_name)
        if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value <= 0:
            problems.append(field_name)
            evidence[field_name] = "invalid"
            continue
        evidence[field_name] = raw_value
    budgets_tiny = (
        parsed_decimals.get("max_order_notional", Decimal("0")) <= Decimal("5")
        and parsed_decimals.get("max_total_exposure", Decimal("0")) <= Decimal("25")
        and parsed_decimals.get("max_daily_loss", Decimal("0")) <= Decimal("10")
    )
    profile_ready = profile_name is not None and profile_name != "template-canary"
    if not budgets_tiny:
        problems.append("tiny_budget_policy")
    if not profile_ready:
        problems.append("profile_name")
    ready = not problems
    detail = (
        "Risk configuration is present and uses explicit tiny canary budgets."
        if ready
        else (
            "Risk configuration is incomplete, invalid, or exceeds the tiny canary budget envelope."
        )
    )
    evidence["tiny_budget_policy"] = budgets_tiny
    evidence["profile_name"] = profile_name
    evidence["profile_name_ready"] = profile_ready
    return GateCheck(
        check_id="risk_config",
        label="Risk config",
        status=CHECK_READY if ready else CHECK_BLOCKED,
        detail=detail,
        evidence=evidence,
    )


def _build_qualification_check(document: dict[str, object]) -> GateCheck:
    strategy = _json_object(document.get("strategy")) or {}
    qualification = _json_object(strategy.get("qualification")) or {}
    whitelist = _json_list(strategy.get("market_whitelist")) or []
    strategy_id = _json_string(strategy.get("strategy_id"))
    research_gate_passed = _json_bool(qualification.get("research_gate_passed")) is True
    shadow_gate_passed = _json_bool(qualification.get("shadow_gate_passed")) is True
    whitelist_ready = bool(whitelist) and all(
        isinstance(item, str) and not _placeholder(item) for item in whitelist
    )
    ready = (
        strategy_id is not None
        and not _placeholder(strategy_id)
        and research_gate_passed
        and shadow_gate_passed
        and whitelist_ready
    )
    return GateCheck(
        check_id="qualification",
        label="Qualification",
        status=CHECK_READY if ready else CHECK_BLOCKED,
        detail=(
            "A qualified strategy with a concrete market whitelist is registered for canary."
            if ready
            else (
                "Canary requires a non-placeholder strategy id, Research PASS, "
                "Shadow PASS, and a concrete market whitelist."
            )
        ),
        evidence={
            "strategy_id_ready": strategy_id is not None and not _placeholder(strategy_id),
            "research_gate_passed": research_gate_passed,
            "shadow_gate_passed": shadow_gate_passed,
            "market_whitelist_count": len(whitelist),
            "market_whitelist_ready": whitelist_ready,
        },
    )


def _normalize_live_probe_check(
    probe_document: dict[str, object] | None,
    check_id: str,
    label: str,
    probe_error: str | None,
) -> GateCheck:
    if probe_document is None:
        return GateCheck(
            check_id=check_id,
            label=label,
            status=CHECK_BLOCKED,
            detail=f"{label} requires a sanitized live probe result before canary can be armed.",
            evidence={"probe_document": "missing", "probe_error": probe_error},
        )
    schema_version = _json_string(probe_document.get("schema_version"))
    if schema_version != LIVE_PROBE_RESULT_SCHEMA_VERSION:
        return GateCheck(
            check_id=check_id,
            label=label,
            status=CHECK_BLOCKED,
            detail=f"{label} probe uses the wrong schema version.",
            evidence={
                "probe_document": "invalid",
                "schema_version": schema_version,
                "expected_schema_version": LIVE_PROBE_RESULT_SCHEMA_VERSION,
            },
        )
    raw_checks = _json_list(probe_document.get("checks")) or []
    for raw_check in raw_checks:
        item = _json_object(raw_check)
        if item is None or item.get("check_id") != check_id:
            continue
        status = _json_string(item.get("status"))
        ready = status == CHECK_READY
        detail = (
            f"{label} probe check passed with sanitized evidence."
            if ready
            else f"{label} probe check is present but not yet ready."
        )
        evidence = _sanitize_probe_evidence(_json_object(item.get("evidence")) or {})
        if ready:
            detail, attestation_evidence = _offline_attestation_block(
                probe_document,
                evidence_label=f"{label} probe evidence",
            )
            evidence = {**evidence, **attestation_evidence}
        return GateCheck(
            check_id=check_id,
            label=label,
            status=CHECK_BLOCKED,
            detail=detail,
            evidence=evidence,
        )
    return GateCheck(
        check_id=check_id,
        label=label,
        status=CHECK_BLOCKED,
        detail=f"{label} is missing from the live probe result.",
        evidence={"probe_document": "incomplete"},
    )


def _build_kill_restart_drills_check(
    drill_document: dict[str, object] | None,
    drill_error: str | None,
) -> GateCheck:
    if drill_document is None:
        return GateCheck(
            check_id="kill_restart_drills",
            label="Kill/restart drills",
            status=CHECK_BLOCKED,
            detail=(
                "Canary requires a passing fault-drill result with kill, restart, "
                "stream, heartbeat, and reconciliation drills."
            ),
            evidence={"drill_document": "missing", "drill_error": drill_error},
        )
    schema_version = _json_string(drill_document.get("schema_version"))
    if schema_version != FAULT_DRILL_RESULT_SCHEMA_VERSION:
        return GateCheck(
            check_id="kill_restart_drills",
            label="Kill/restart drills",
            status=CHECK_BLOCKED,
            detail="Fault-drill result uses the wrong schema version.",
            evidence={
                "drill_document": "invalid",
                "schema_version": schema_version,
                "expected_schema_version": FAULT_DRILL_RESULT_SCHEMA_VERSION,
            },
        )
    drills = _json_list(drill_document.get("drills")) or []
    seen: dict[str, str] = {}
    for raw_drill in drills:
        item = _json_object(raw_drill)
        if item is None:
            continue
        drill_id = _json_string(item.get("drill_id"))
        status = _json_string(item.get("status"))
        if drill_id is not None and status is not None:
            seen[drill_id] = status
    missing = [drill_id for drill_id in REQUIRED_FAULT_DRILLS if seen.get(drill_id) != CHECK_READY]
    required_drills_json: list[JSONValue] = [drill_id for drill_id in REQUIRED_FAULT_DRILLS]
    missing_json: list[JSONValue] = [drill_id for drill_id in missing]
    detail = (
        "Required fault drills passed."
        if not missing
        else "One or more required fault drills are missing or failed."
    )
    evidence: dict[str, JSONValue] = {
        "required_drills": required_drills_json,
        "missing_or_failed_drills": missing_json,
    }
    status = CHECK_READY if not missing else CHECK_BLOCKED
    if not missing:
        detail, attestation_evidence = _offline_attestation_block(
            drill_document,
            evidence_label="Fault-drill evidence",
        )
        evidence = {**evidence, **attestation_evidence}
        status = CHECK_BLOCKED
    return GateCheck(
        check_id="kill_restart_drills",
        label="Kill/restart drills",
        status=status,
        detail=detail,
        evidence=evidence,
    )


def evaluate_doctor(config_path: str | Path | None = None) -> GateReport:
    resolved_path = DEFAULT_CANARY_CONFIG_PATH if config_path is None else Path(config_path)
    config_document, config_error = _read_json_object(resolved_path)
    if config_document is None:
        config_document = {}
    config_schema = _json_string(config_document.get("schema_version"))

    live_probe_document, live_probe_error = _resolve_embedded_or_path(
        config_document,
        "live_probe_result",
    )
    drill_document, drill_error = _resolve_embedded_or_path(
        config_document,
        "fault_drill_result",
    )

    probe_labels = {
        "geoblock": "Geoblock",
        "credentials_signing": "Credentials/signing",
        "balance_allowance": "Balance/allowance",
        "market_constraints": "Market constraints",
        "user_stream": "User stream",
        "heartbeat": "Heartbeat",
        "reconciliation": "Reconciliation",
    }
    checks = [
        _build_explicit_user_authorization_check(config_document),
        *[
            _normalize_live_probe_check(
                live_probe_document,
                check_id,
                probe_labels[check_id],
                live_probe_error,
            )
            for check_id in REQUIRED_LIVE_PROBE_CHECKS
        ],
        _build_risk_config_check(config_document),
        _build_qualification_check(config_document),
        _build_kill_restart_drills_check(drill_document, drill_error),
    ]
    status = (
        STATUS_LIVE_CANARY_READY
        if all(check.status == CHECK_READY for check in checks)
        else STATUS_LIVE_BLOCKED
    )
    strategy = _json_object(config_document.get("strategy")) or {}
    strategy_id = _json_string(strategy.get("strategy_id")) or "unqualified-template"
    ready_checks = sum(1 for check in checks if check.status == CHECK_READY)
    total_checks = len(checks)
    summary = (
        f"{ready_checks}/{total_checks} canary checks are ready."
        if status == STATUS_LIVE_CANARY_READY
        else (
            f"{ready_checks}/{total_checks} canary checks are ready; live mutation remains blocked "
            "until every remaining condition is evidenced."
        )
    )
    return build_gate_report(
        gate=GATE_CANARY,
        status=status,
        summary=summary,
        strategy_id=strategy_id,
        checks=checks,
        context={
            "doctor_mode": "offline_safe",
            "config_path": str(resolved_path),
            "config_loaded": config_error is None,
            "config_error": config_error,
            "config_schema_version": config_schema,
            "expected_config_schema_version": CANARY_CONFIG_SCHEMA_VERSION,
            "live_evidence_policy": (
                "unsigned local probe and drill JSON are advisory only until external attestation "
                "verification exists"
            ),
            "secret_policy": "doctor never reads or prints credential values",
        },
    )


def evaluate_research_gate(
    *,
    strategy_id: str,
    candidate_count: int,
    tradable_candidate_count: int,
    unknown_cost_count: int,
    generated_at: datetime | None = None,
) -> GateReport:
    checks = [
        GateCheck(
            check_id="profitable_candidates_found",
            label="Profitable candidates found",
            status=CHECK_READY if candidate_count > 0 else CHECK_BLOCKED,
            detail="At least one candidate was produced."
            if candidate_count > 0
            else "No research candidates were produced.",
            evidence={"candidate_count": candidate_count},
        ),
        GateCheck(
            check_id="tradable_edge_positive",
            label="Tradable edge positive",
            status=CHECK_READY if tradable_candidate_count > 0 else CHECK_BLOCKED,
            detail=(
                "At least one candidate retains positive tradable edge after costs."
                if tradable_candidate_count > 0
                else "All candidates fail the tradable-edge requirement."
            ),
            evidence={"tradable_candidate_count": tradable_candidate_count},
        ),
        GateCheck(
            check_id="unknown_costs_resolved",
            label="Unknown costs resolved",
            status=CHECK_READY if unknown_cost_count == 0 else CHECK_BLOCKED,
            detail=(
                "All cost components are known."
                if unknown_cost_count == 0
                else "Unknown costs remain, so research must fail closed."
            ),
            evidence={"unknown_cost_count": unknown_cost_count},
        ),
    ]
    status = STATUS_PASS if all(check.status == CHECK_READY for check in checks) else STATUS_NO_GO
    return build_gate_report(
        gate=GATE_RESEARCH,
        status=status,
        summary="Research gate is machine-reproducible from fixed acceptance inputs.",
        strategy_id=strategy_id,
        checks=checks,
        context={"gate_contract": "research"},
        generated_at=generated_at,
    )


def evaluate_shadow_gate(
    *,
    strategy_id: str,
    same_strategy_logic: bool,
    shadow_fill_count: int,
    required_shadow_fill_count: int,
    realized_net_pnl: Decimal,
    coverage_contiguous: bool,
    generated_at: datetime | None = None,
) -> GateReport:
    checks = [
        GateCheck(
            check_id="same_strategy_logic",
            label="Same strategy logic",
            status=CHECK_READY if same_strategy_logic else CHECK_BLOCKED,
            detail=(
                "Replay, paper, and shadow share the same strategy logic."
                if same_strategy_logic
                else "Shadow is not using the same strategy logic."
            ),
            evidence={"same_strategy_logic": same_strategy_logic},
        ),
        GateCheck(
            check_id="shadow_fill_coverage",
            label="Shadow fill coverage",
            status=CHECK_READY
            if shadow_fill_count >= required_shadow_fill_count
            else CHECK_BLOCKED,
            detail=(
                "Shadow captured enough independent fills."
                if shadow_fill_count >= required_shadow_fill_count
                else "Shadow fill coverage is below the acceptance threshold."
            ),
            evidence={
                "shadow_fill_count": shadow_fill_count,
                "required_shadow_fill_count": required_shadow_fill_count,
            },
        ),
        GateCheck(
            check_id="realized_net_pnl_positive",
            label="Realized net PnL positive",
            status=CHECK_READY if realized_net_pnl > Decimal("0") else CHECK_BLOCKED,
            detail=(
                "Shadow realized net PnL is positive."
                if realized_net_pnl > Decimal("0")
                else "Shadow realized net PnL is not positive."
            ),
            evidence={"realized_net_pnl": decimal_to_string(realized_net_pnl)},
        ),
        GateCheck(
            check_id="coverage_contiguous",
            label="Coverage contiguous",
            status=CHECK_READY if coverage_contiguous else CHECK_BLOCKED,
            detail=(
                "Shadow evidence is contiguous and restart-safe."
                if coverage_contiguous
                else "Shadow coverage has gaps."
            ),
            evidence={"coverage_contiguous": coverage_contiguous},
        ),
    ]
    status = STATUS_PASS if all(check.status == CHECK_READY for check in checks) else STATUS_NO_GO
    return build_gate_report(
        gate=GATE_SHADOW,
        status=status,
        summary="Shadow gate is based on fixed acceptance replay evidence.",
        strategy_id=strategy_id,
        checks=checks,
        context={"gate_contract": "shadow"},
        generated_at=generated_at,
    )


def evaluate_limited_live_gate(
    *,
    strategy_id: str,
    live_days: int,
    independent_fills: int,
    realized_net_pnl: Decimal,
    profit_factor: Decimal,
    edge_realization_ratio: Decimal,
    max_drawdown_within_budget: bool,
    positive_windows: int,
    unresolved_reconciliation_count: int,
    unexplained_cash_difference: bool,
    generated_at: datetime | None = None,
) -> GateReport:
    checks = [
        GateCheck(
            check_id="live_days_minimum",
            label="30-day live window",
            status=CHECK_READY if live_days >= 30 else CHECK_BLOCKED,
            detail="Live observation window meets the minimum."
            if live_days >= 30
            else "Need at least 30 natural live days.",
            evidence={"live_days": live_days, "required_live_days": 30},
        ),
        GateCheck(
            check_id="independent_fills_minimum",
            label="100 independent fills",
            status=CHECK_READY if independent_fills >= 100 else CHECK_BLOCKED,
            detail="Independent fill count meets the minimum."
            if independent_fills >= 100
            else "Need at least 100 independent fills.",
            evidence={"independent_fills": independent_fills, "required_independent_fills": 100},
        ),
        GateCheck(
            check_id="realized_net_pnl_positive",
            label="Realized net PnL positive",
            status=CHECK_READY if realized_net_pnl > Decimal("0") else CHECK_BLOCKED,
            detail="Realized net PnL is positive."
            if realized_net_pnl > Decimal("0")
            else "Realized net PnL is not positive.",
            evidence={"realized_net_pnl": decimal_to_string(realized_net_pnl)},
        ),
        GateCheck(
            check_id="profit_factor_minimum",
            label="Profit factor >= 1.15",
            status=CHECK_READY if profit_factor >= Decimal("1.15") else CHECK_BLOCKED,
            detail="Profit factor passes the minimum."
            if profit_factor >= Decimal("1.15")
            else "Profit factor is below 1.15.",
            evidence={"profit_factor": decimal_to_string(profit_factor)},
        ),
        GateCheck(
            check_id="edge_realization_ratio_minimum",
            label="Edge realization ratio >= 0.70",
            status=CHECK_READY if edge_realization_ratio >= Decimal("0.70") else CHECK_BLOCKED,
            detail=(
                "Edge realization ratio passes the minimum."
                if edge_realization_ratio >= Decimal("0.70")
                else "Edge realization ratio is below 0.70."
            ),
            evidence={"edge_realization_ratio": decimal_to_string(edge_realization_ratio)},
        ),
        GateCheck(
            check_id="drawdown_within_budget",
            label="Drawdown within budget",
            status=CHECK_READY if max_drawdown_within_budget else CHECK_BLOCKED,
            detail="Drawdown stayed inside the registered budget."
            if max_drawdown_within_budget
            else "Drawdown exceeded the registered budget.",
            evidence={"max_drawdown_within_budget": max_drawdown_within_budget},
        ),
        GateCheck(
            check_id="positive_windows",
            label="Two positive evaluation windows",
            status=CHECK_READY if positive_windows >= 2 else CHECK_BLOCKED,
            detail="Two consecutive evaluation windows are positive."
            if positive_windows >= 2
            else "Need two consecutive positive evaluation windows.",
            evidence={"positive_windows": positive_windows},
        ),
        GateCheck(
            check_id="reconciliation_clear",
            label="Reconciliation clear",
            status=CHECK_READY
            if unresolved_reconciliation_count == 0 and not unexplained_cash_difference
            else CHECK_BLOCKED,
            detail=(
                "Reconciliation is clean with no unexplained cash differences."
                if unresolved_reconciliation_count == 0 and not unexplained_cash_difference
                else "Reconciliation remains unresolved or cash differences are unexplained."
            ),
            evidence={
                "unresolved_reconciliation_count": unresolved_reconciliation_count,
                "unexplained_cash_difference": unexplained_cash_difference,
            },
        ),
    ]
    status = (
        STATUS_LIVE_LIMITED_READY
        if all(check.status == CHECK_READY for check in checks)
        else STATUS_NO_GO
    )
    return build_gate_report(
        gate=GATE_LIMITED_LIVE,
        status=status,
        summary="Limited-live gate requires profitability evidence, not engineering completion.",
        strategy_id=strategy_id,
        checks=checks,
        context={"gate_contract": "limited_live"},
        generated_at=generated_at,
    )


__all__ = [
    "CANARY_CONFIG_SCHEMA_VERSION",
    "CHECK_BLOCKED",
    "CHECK_READY",
    "DEFAULT_CANARY_CONFIG_PATH",
    "FAULT_DRILL_RESULT_SCHEMA_VERSION",
    "GATE_CANARY",
    "GATE_LIMITED_LIVE",
    "GATE_RESEARCH",
    "GATE_REPORT_SCHEMA_VERSION",
    "GATE_SHADOW",
    "GateCheck",
    "GateReport",
    "JSONValue",
    "LIVE_PROBE_RESULT_SCHEMA_VERSION",
    "REQUIRED_CANARY_CHECKS",
    "REQUIRED_FAULT_DRILLS",
    "REQUIRED_LIVE_PROBE_CHECKS",
    "STATUS_LIVE_BLOCKED",
    "STATUS_LIVE_CANARY_READY",
    "STATUS_LIVE_LIMITED_READY",
    "STATUS_NO_GO",
    "STATUS_PASS",
    "build_gate_report",
    "decimal_to_string",
    "dumps_json",
    "evaluate_doctor",
    "evaluate_limited_live_gate",
    "evaluate_research_gate",
    "evaluate_shadow_gate",
    "to_jsonable",
    "utc_timestamp",
]
