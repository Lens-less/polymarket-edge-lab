from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import src.edge_lab.btc_twap_relative_value_readiness as readiness
from src.edge_lab.btc_twap_pair_pricing import PairExecutionMode, PairPricingPolicy
from src.edge_lab.btc_twap_relative_value import (
    OrderBookSnapshot,
    PairAction,
    SameExpiryPair,
)
from src.edge_lab.btc_twap_relative_value_readiness import (
    GATE_0_PAIR_PRICING_POLICY,
    CaptureCapacityEvidence,
    CohortCoverageInput,
    CohortCoverageResult,
    ExecutionProbeEvidenceBundle,
    ExecutionProbePrerequisites,
    PerfectInformationAttempt,
    StrategyLiveEvidenceBundle,
    StrategyLiveInputs,
    evaluate_capture_capacity,
    evaluate_execution_probe_readiness,
    evaluate_perfect_information_upper_bound,
    evaluate_strategy_live_readiness_inputs,
    load_verified_json_artifact,
    validate_cohort_data_coverage,
    validate_structural_floor,
)
from src.edge_lab.execution import ExecutionFeeSchedule
from tests.test_btc_twap_relative_value_v07_replay import (
    _settlement_state,
    _v07_pair,
)

D = Decimal
UTC = timezone.utc
STRICT_TRACK_ID = readiness._strict_gate_0_parameters()["track_id"]
PROBE_EVALUATION_NOW = datetime(2026, 8, 18, 12, 0, 30, tzinfo=UTC)
LIVE_EVALUATION_NOW = datetime(2026, 8, 14, 12, 0, 30, tzinfo=UTC)
SERVICE_HEALTH_MAX_AGE_SECONDS = 90
SERVICE_HEALTH_MIN_WINDOW_MS = 15_000
DAILY_LEDGER_MAX_AGE_DAYS = 1


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _epoch_ms(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1000)


def _write_artifact(
    tmp_path: Path,
    name: str,
    payload: dict[str, object],
):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    document = dict(payload)
    if "artifact_sha256" in document:
        unsigned = {key: value for key, value in document.items() if key != "artifact_sha256"}
        document["artifact_sha256"] = readiness.sha256(
            json.dumps(unsigned, sort_keys=True).encode("utf-8")
        ).hexdigest()
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return load_verified_json_artifact(path)


def _write_support_file(tmp_path: Path, name: str, payload: dict[str, object]) -> dict[str, str]:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return {
        "path": str(path),
        "sha256": readiness.sha256(path.read_bytes()).hexdigest(),
    }


def _file_ref(path: Path) -> dict[str, str]:
    return {
        "path": str(path.resolve()),
        "sha256": readiness.sha256(path.read_bytes()).hexdigest(),
    }


def _gate_0_artifact(tmp_path: Path, *, passed: bool = True):
    strict = readiness._strict_gate_0_parameters()
    strict_identity = {
        "track_id": strict["track_id"],
        "v08_preregistration_sha256": strict["v08_preregistration_sha256"],
        "expected_clean_attempts": strict["expected_clean_attempts"],
        "scan_ttc_seconds_inclusive": strict["scan_ttc_seconds_inclusive"],
        "candidate_ttc_count": strict["candidate_ttc_count"],
        "decision_execution_mode": strict["decision_execution_mode"],
        "minimum_average_best_total_pnl_per_expiry_usdc": strict[
            "minimum_average_best_total_pnl_per_expiry_usdc"
        ],
    }
    payload: dict[str, object] = {
        "schema_version": readiness.STRICT_GATE_0_REPORT_SCHEMA,
        "authority": {
            "track_id": strict["track_id"],
            "v08_preregistration_path": strict["v08_preregistration_path"],
            "v08_preregistration_sha256": strict["v08_preregistration_sha256"],
            "expected_clean_attempts": strict["expected_clean_attempts"],
            "scan_ttc_seconds_inclusive": strict["scan_ttc_seconds_inclusive"],
            "candidate_ttc_count": strict["candidate_ttc_count"],
            "decision_execution_mode": strict["decision_execution_mode"],
            "minimum_average_best_total_pnl_per_expiry_usdc": strict[
                "minimum_average_best_total_pnl_per_expiry_usdc"
            ],
            "strict_identity_sha256": readiness.sha256(
                readiness.canonical_json_bytes(strict_identity)
            ).hexdigest(),
            "requested_expected_clean_attempts": strict["expected_clean_attempts"],
            "requested_decision_tau_seconds": None,
            "strict_parameters_match": True,
            "strict_fill_volume_bound_complete": True,
        },
        "observed_unique_common_expiry_attempts": strict["expected_clean_attempts"],
        "scan": {
            "ttc_seconds_start": strict["scan_ttc_seconds_inclusive"][0],
            "ttc_seconds_end": strict["scan_ttc_seconds_inclusive"][1],
            "candidate_ttc_count": strict["candidate_ttc_count"],
            "all_expiries_complete": True,
        },
        "execution_modes": {
            "maker_maker": {
                "attempt_count": 1,
                "evidence_complete": True,
                "economic_gate_passed": passed,
                "missing_fill_volume_bound_expiry_ids": [],
                "aggregate_best_total_pnl": "5.0" if passed else "0.0",
                "average_best_total_pnl_per_expiry": "5.0" if passed else "0.0",
                "minimum_average_pnl_per_expiry": strict[
                    "minimum_average_best_total_pnl_per_expiry_usdc"
                ],
                "gate_0_passed": passed,
                "stop_recommended": not passed,
                "rerun_required": False,
                "decision": "PASS" if passed else "STOP",
                "attempts": [
                    {
                        "attempt_id": "expiry-1",
                        "best_total_pnl": "5.0" if passed else "0.0",
                        "selected_level": {"quantity": "5"},
                        "fill_volume_bound_source": (
                            "context.future_public_trades_by_token_id"
                        ),
                    }
                ],
            }
        },
        "decision": "PASS" if passed else "STOP",
        "gate_0_passed": passed,
        "rerun_required": False,
        "stop_recommended": not passed,
    }
    payload["report_sha256"] = readiness.sha256(
        readiness.canonical_json_bytes(payload)
    ).hexdigest()
    return _write_artifact(tmp_path, "gate0.json", payload)


def _strategy_shadow_artifact(
    tmp_path: Path,
    *,
    trusted: bool = False,
    self_hash_receipt: bool = False,
):
    attempt = {
        "attempt_id": "attempt-1",
        "expiry_ms": 1,
        "capture_verification": {
            "capture_attempt_id": "capture-1",
            "capture_root": "/captures/1",
            "capture_tree_sha256": "c" * 64,
        },
        "locked_action_payload_sha256": "d" * 64,
        "source_evidence_sha256": "e" * 64,
    }
    zero = {
        "attempt_id": "attempt-2",
        "expiry_ms": 2,
        "outcome": "no_trade",
        "capture_verification": {
            "capture_attempt_id": "capture-2",
            "capture_root": "/captures/2",
            "capture_tree_sha256": "f" * 64,
        },
        "source_evidence_sha256": "1" * 64,
        "locked_action_payload_sha256": "2" * 64,
        "dirty_reason_code": None,
    }
    authority_payload: dict[str, object] = {
        "schema_version": "btc_twap_structural_shadow_report.v1",
        "issuer": "btc_twap_structural_shadow.finalized_capture_store_builder",
        "trust_model": "local_hash_only_not_formal_readiness_authority",
        "required_trust_primitives": [
            "trusted_build_receipt",
            "external_anchor",
        ],
        "track_id": STRICT_TRACK_ID,
        "source_input_sha256": "1" * 64,
        "preregistration_sha256": "2" * 64,
        "rolling_audit_sha256": "3" * 64,
        "attempts": [
            {
                "attempt_id": attempt["attempt_id"],
                "expiry_ms": attempt["expiry_ms"],
                "capture_verification_sha256": readiness.sha256(
                    readiness.canonical_json_bytes(attempt["capture_verification"])
                ).hexdigest(),
                "locked_action_payload_sha256": attempt["locked_action_payload_sha256"],
                "source_evidence_sha256": attempt["source_evidence_sha256"],
            }
        ],
        "locked_zero_cohorts": [
            {
                "attempt_id": zero["attempt_id"],
                "expiry_ms": zero["expiry_ms"],
                "outcome": zero["outcome"],
                "capture_verification_sha256": readiness.sha256(
                    readiness.canonical_json_bytes(zero["capture_verification"])
                ).hexdigest(),
                "source_evidence_sha256": zero["source_evidence_sha256"],
                "locked_action_payload_sha256": zero["locked_action_payload_sha256"],
                "dirty_reason_code": zero["dirty_reason_code"],
            }
        ],
    }
    authority_sha256 = readiness.sha256(
        readiness.canonical_json_bytes(authority_payload)
    ).hexdigest()
    payload: dict[str, object] = {
        "schema_version": "btc_twap_structural_shadow_report.v1",
        "track_id": STRICT_TRACK_ID,
        "source_input_sha256": "1" * 64,
        "preregistration_sha256": "2" * 64,
        "rolling_audit_sha256": "3" * 64,
        "builder_authority": {
            "issuer": authority_payload["issuer"],
            "authority_kind": "capture_store_finalized",
            "authority_sha256": authority_sha256,
            "trust_model": "local_hash_only_not_formal_readiness_authority",
            "required_trust_primitives": [
                "trusted_build_receipt",
                "external_anchor",
            ],
            "missing_trust_primitives": [
                "trusted_build_receipt",
                "external_anchor",
            ],
            "trust_evidence_refs": {
                "trusted_build_receipt": {"status": "missing"},
                "external_anchor": {"status": "missing"},
            },
        },
        "attempts": [attempt],
        "locked_zero_cohorts": [zero],
        "neutral_robustness": {"passed": True},
        "neutral_shadow_evidence": {
            "scenario": "neutral",
            "realized_net_pnl": "10",
            "receipt_chain_verified": True,
            "all_admitted_cohorts_included": True,
            "expiry_count": 200,
            "without_best_expiry_net_pnl": "5",
            "without_best_direction_net_pnl": "5",
            "minimum_rolling_window_net_pnl": "1",
            "max_single_expiry_pnl_concentration": "0.10",
        },
    }
    payload["report_sha256"] = readiness.sha256(
        readiness.canonical_json_bytes(payload)
    ).hexdigest()
    if trusted:
        report_path = tmp_path / "shadow.json"
        trust_bound_digest = readiness.sha256(
            readiness.canonical_json_bytes(
                {
                    **{
                        key: value
                        for key, value in payload.items()
                        if key != "report_sha256"
                    },
                    "builder_authority": {
                        key: value
                        for key, value in payload["builder_authority"].items()
                        if key
                        not in {"trust_evidence_refs", "missing_trust_primitives"}
                    },
                }
            )
        ).hexdigest()
        trusted_receipt_payload = {
            "schema_version": "btc-twap-structural-shadow-build-receipt.v1",
            "track_id": STRICT_TRACK_ID,
            "report_path": str(report_path),
            "report_digest_sha256": trust_bound_digest,
            "authority_sha256": authority_sha256,
        }
        trusted_receipt_ref = (
            {
                "path": str(report_path),
                "sha256": readiness.sha256(
                    json.dumps(payload, sort_keys=True).encode("utf-8")
                ).hexdigest(),
            }
            if self_hash_receipt
            else _write_support_file(
                tmp_path,
                "authority/trusted-build-receipt.json",
                trusted_receipt_payload,
            )
        )
        external_anchor_ref = _write_support_file(
            tmp_path,
            "authority/external-anchor.json",
            {
                "schema_version": "btc-twap-structural-shadow-external-anchor.v1",
                "track_id": STRICT_TRACK_ID,
                "report_digest_sha256": trust_bound_digest,
                "trusted_build_receipt_sha256": trusted_receipt_ref["sha256"],
                "authority_sha256": authority_sha256,
            },
        )
        payload["builder_authority"]["missing_trust_primitives"] = []
        payload["builder_authority"]["trust_evidence_refs"] = {
            "trusted_build_receipt": {
                "status": "bound",
                **trusted_receipt_ref,
            },
            "external_anchor": {
                "status": "bound",
                "reference": external_anchor_ref["path"],
                "sha256": external_anchor_ref["sha256"],
            },
        }
        payload["report_sha256"] = readiness.sha256(
            readiness.canonical_json_bytes(
                {key: value for key, value in payload.items() if key != "report_sha256"}
            )
        ).hexdigest()
    return _write_artifact(tmp_path, "shadow.json", payload)


def _probe_bundle(
    tmp_path: Path,
    *,
    include_shadow_artifact: bool = False,
) -> ExecutionProbeEvidenceBundle:
    service_window_end = PROBE_EVALUATION_NOW - timedelta(seconds=30)
    service_window_start = service_window_end - timedelta(seconds=30)
    return ExecutionProbeEvidenceBundle(
        gate_0_report_artifact=_gate_0_artifact(tmp_path),
        structural_shadow_artifact=(
            _strategy_shadow_artifact(tmp_path) if include_shadow_artifact else None
        ),
        capture_capacity_artifact=_write_artifact(
            tmp_path,
            "capacity.json",
            {
                "schema_version": readiness.CAPTURE_CAPACITY_ARTIFACT_SCHEMA,
                "generated_at": _utc_text(PROBE_EVALUATION_NOW),
                "track": STRICT_TRACK_ID,
                "capture_attempt_count": 1,
                "capture_failure_count": 0,
                "pending_attempt_count": 0,
                "free_disk_bytes": 11 * 1024**3,
                "projected_daily_capture_bytes": 1024**3,
                "available_memory_bytes": 3 * 1024**3,
                "burstable_cpu_credit_exhausted": False,
                "attempt_receipts": [
                    _write_support_file(
                        tmp_path,
                        "receipts/attempt-1.json",
                        {
                            "schema_version": "btc-twap-capture-attempt-receipt.v1",
                            "attempt_id": "attempt-1",
                            "track_id": STRICT_TRACK_ID,
                            "created_at": "2026-08-18T11:00:00Z",
                            "started_at": "2026-08-18T11:00:00Z",
                            "terminal_at": "2026-08-18T11:10:00Z",
                            "status": "succeeded",
                        },
                    )
                ],
                "pending_attempt_receipts": [],
                "host_metrics": {
                    "mem_available_bytes": 3 * 1024**3,
                    "cpu_credit_balance": "50.0",
                    "cpu_credit_telemetry_unavailable": None,
                },
                "artifact_sha256": "",
            },
        ),
        service_health_artifact=_write_artifact(
            tmp_path,
            "service.json",
            {
                "schema_version": readiness.SERVICE_HEALTH_ARTIFACT_SCHEMA,
                "service_continuously_healthy": True,
                "generated_at": _utc_text(PROBE_EVALUATION_NOW - timedelta(seconds=5)),
                **{
                    f"heartbeat_source_{key}": value
                    for key, value in _write_support_file(
                        tmp_path,
                        "sources/service-heartbeat.json",
                        {"schema_version": "btc-twap-heartbeat-source.v1"},
                    ).items()
                },
                "window_started_at_ms": _epoch_ms(service_window_start),
                "window_ended_at_ms": _epoch_ms(service_window_end),
            },
        ),
        authenticated_read_artifact=_write_artifact(
            tmp_path,
            "auth.json",
            {
                "schema_version": readiness.AUTHENTICATED_READ_ARTIFACT_SCHEMA,
                "authenticated_read_verified": True,
                **{
                    f"receipt_{key}": value
                    for key, value in _write_support_file(
                        tmp_path,
                        "receipts/authenticated-read.json",
                        {"schema_version": "btc-twap-authenticated-read-receipt.v1"},
                    ).items()
                },
            },
        ),
        fill_stream_artifact=_write_artifact(
            tmp_path,
            "fills.json",
            {
                "schema_version": readiness.FILL_STREAM_ARTIFACT_SCHEMA,
                "fill_stream_verified": True,
                **{
                    f"receipt_{key}": value
                    for key, value in _write_support_file(
                        tmp_path,
                        "receipts/fill-stream.json",
                        {"schema_version": "btc-twap-fill-stream-receipt.v1"},
                    ).items()
                },
            },
        ),
        fault_drills_artifact=_write_artifact(
            tmp_path,
            "drills.json",
            {
                "schema_version": readiness.FAULT_DRILLS_ARTIFACT_SCHEMA,
                "passed_by_drill": {
                    drill: True for drill in readiness.REQUIRED_FAULT_DRILLS
                },
                "receipts_by_drill": {
                    drill: _write_support_file(
                        tmp_path,
                        f"receipts/{drill}.json",
                        {"schema_version": "btc-twap-fault-drill-receipt.v1"},
                    )
                    for drill in readiness.REQUIRED_FAULT_DRILLS
                },
            },
        ),
        probe_adapter_artifact=_write_artifact(
            tmp_path,
            "adapter.json",
            {
                "schema_version": readiness.PROBE_ADAPTER_ARTIFACT_SCHEMA,
                "double_maker_probe_implemented": True,
                "full_hedge_depth_verified": True,
                **{
                    f"code_artifact_{key}": value
                    for key, value in _file_ref(
                        Path("src/edge_lab/btc_twap_relative_value_readiness.py")
                    ).items()
                },
                **{
                    f"test_artifact_{key}": value
                    for key, value in _file_ref(
                        Path("tests/test_btc_twap_relative_value_readiness.py")
                    ).items()
                },
            },
        ),
    )


def _strategy_bundle(tmp_path: Path) -> StrategyLiveEvidenceBundle:
    service_window_end = LIVE_EVALUATION_NOW - timedelta(seconds=30)
    service_window_start = service_window_end - timedelta(seconds=30)
    return StrategyLiveEvidenceBundle(
        gate_0_report_artifact=_gate_0_artifact(tmp_path),
        structural_shadow_artifact=_strategy_shadow_artifact(tmp_path),
        service_health_artifact=_write_artifact(
            tmp_path,
            "service-live.json",
            {
                "schema_version": readiness.SERVICE_HEALTH_ARTIFACT_SCHEMA,
                "service_continuously_healthy": True,
                "generated_at": _utc_text(LIVE_EVALUATION_NOW - timedelta(seconds=5)),
                **{
                    f"heartbeat_source_{key}": value
                    for key, value in _write_support_file(
                        tmp_path,
                        "sources/service-heartbeat-live.json",
                        {"schema_version": "btc-twap-heartbeat-source.v1"},
                    ).items()
                },
                "window_started_at_ms": _epoch_ms(service_window_start),
                "window_ended_at_ms": _epoch_ms(service_window_end),
            },
        ),
        daily_ledger_artifact=_write_artifact(
            tmp_path,
            "daily.json",
            {
                "schema_version": readiness.DAILY_LEDGER_ARTIFACT_SCHEMA,
                "generated_at": _utc_text(LIVE_EVALUATION_NOW - timedelta(minutes=5)),
                "rows": [
                    {"utc_date": f"2026-08-{day:02d}", "net_pnl": "20"}
                    for day in range(1, 15)
                ],
            },
        ),
        capital_ledger_artifact=_write_artifact(
            tmp_path,
            "capital.json",
            {
                "schema_version": readiness.CAPITAL_LEDGER_ARTIFACT_SCHEMA,
                "rows": [{"capital_deployed": "2000"}],
            },
        ),
        execution_reconciliation_artifact=_write_artifact(
            tmp_path,
            "recon.json",
            {
                "schema_version": readiness.EXECUTION_RECONCILIATION_ARTIFACT_SCHEMA,
                "terminal_rows": [
                    {
                        "cohort_id": "attempt-1",
                        "execution_complete": True,
                        "included_in_pnl_distribution": True,
                    },
                    {
                        "cohort_id": "attempt-2",
                        "execution_complete": True,
                        "included_in_pnl_distribution": True,
                    }
                ],
            },
        ),
    )


def _probe_prerequisites(
    tmp_path: Path,
    *,
    include_shadow_artifact: bool = False,
    structural_authority_trust_policy: readiness.StructuralAuthorityTrustPolicy | None = None,
) -> ExecutionProbePrerequisites:
    return ExecutionProbePrerequisites(
        service_continuously_healthy=True,
        authenticated_read_verified=True,
        fill_stream_verified=True,
        failure_drills_complete=True,
        immutable_probe_preregistration_present=True,
        full_hedge_depth_verified=True,
        gate_0_passed=True,
        double_maker_probe_implemented=True,
        capture_capacity_gate_passed=True,
        verified_evidence_bundle=_probe_bundle(
            tmp_path,
            include_shadow_artifact=include_shadow_artifact,
        ),
        structural_authority_trust_policy=structural_authority_trust_policy,
        service_health_evaluation_utc_now=PROBE_EVALUATION_NOW,
        service_health_max_age_seconds=SERVICE_HEALTH_MAX_AGE_SECONDS,
        service_health_min_window_ms=SERVICE_HEALTH_MIN_WINDOW_MS,
    )


def _trusted_shadow_policy(tmp_path: Path) -> readiness.StructuralAuthorityTrustPolicy:
    trusted_receipt_path = tmp_path / "authority" / "trusted-build-receipt.json"
    external_anchor_path = tmp_path / "authority" / "external-anchor.json"
    return readiness.StructuralAuthorityTrustPolicy(
        trusted_build_receipt_path=str(trusted_receipt_path),
        trusted_build_receipt_sha256=readiness.sha256(
            trusted_receipt_path.read_bytes()
        ).hexdigest(),
        external_anchor_reference=str(external_anchor_path),
        external_anchor_sha256=readiness.sha256(
            external_anchor_path.read_bytes()
        ).hexdigest(),
    )


def _strategy_live_inputs(
    tmp_path: Path,
    *,
    trusted_shadow: bool = False,
    structural_authority_trust_policy: readiness.StructuralAuthorityTrustPolicy | None = None,
) -> StrategyLiveInputs:
    base_bundle = _strategy_bundle(tmp_path)
    shadow_artifact = _strategy_shadow_artifact(
        tmp_path,
        trusted=trusted_shadow,
    )
    bundle = replace(
        base_bundle,
        structural_shadow_artifact=shadow_artifact,
    )
    return StrategyLiveInputs(
        builder_verified_evidence_chain=True,
        auditable_prelabel_lock_evidence=True,
        clean_prelabeled_common_terminal_cohort_count=200,
        structural_settled_expiry_cluster_count=200,
        structural_explainable_economic_attempt_count=200,
        structural_bootstrap_cluster_mean_lower_95=D("0.01"),
        structural_true_edge_gate_satisfied=True,
        structural_qualified_net_pnl=D("10"),
        structural_gate_0_passed=True,
        structural_max_single_expiry_pnl_concentration=D("0.10"),
        complete_real_execution_evidence=True,
        all_locked_cohorts_in_pnl_distribution=True,
        service_continuously_healthy=True,
        consecutive_profitable_utc_days=14,
        minimum_daily_net_pnl=D("20"),
        peak_capital_deployed=D("2000"),
        verified_evidence_bundle=bundle,
        structural_authority_trust_policy=structural_authority_trust_policy,
        service_health_evaluation_utc_now=LIVE_EVALUATION_NOW,
        service_health_max_age_seconds=SERVICE_HEALTH_MAX_AGE_SECONDS,
        service_health_min_window_ms=SERVICE_HEALTH_MIN_WINDOW_MS,
        daily_ledger_evaluation_utc_now=LIVE_EVALUATION_NOW,
        daily_ledger_max_age_days=DAILY_LEDGER_MAX_AGE_DAYS,
    )


def _book(
    token_id: str,
    *,
    ask: str,
    bid: str | None = None,
    size: str = "10",
    tick_size: str = "0.01",
    minimum_order_size: str = "5",
) -> OrderBookSnapshot:
    best_bid = D(ask) - D("0.01") if bid is None else D(bid)
    return OrderBookSnapshot.from_tuples(
        token_id,
        bids=((best_bid, D(size)),),
        asks=((D(ask), D(size)),),
        timestamp_ms=1_000,
        tick_size=D(tick_size),
        minimum_order_size=D(minimum_order_size),
    )


def _books() -> dict[str, OrderBookSnapshot]:
    pair = _v07_pair()
    return {
        pair.market_5.up_token_id: _book(pair.market_5.up_token_id, ask="0.40"),
        pair.market_5.down_token_id: _book(pair.market_5.down_token_id, ask="0.20"),
        pair.market_15.up_token_id: _book(pair.market_15.up_token_id, ask="0.30"),
        pair.market_15.down_token_id: _book(pair.market_15.down_token_id, ask="0.25"),
    }


def test_structural_floor_uses_strike_order_to_select_the_only_valid_pair() -> None:
    pair = _v07_pair()
    books = _books()

    below = validate_structural_floor(
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        books=books,
        pricing_policy=PairPricingPolicy(pair_risk_usdc=D("5")),
    )
    above = validate_structural_floor(
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("101"),
            strike_15=D("100"),
        ),
        books=books,
        pricing_policy=PairPricingPolicy(pair_risk_usdc=D("5")),
    )

    assert below.selected_action is PairAction.LONG_5_UP_LONG_15_DOWN
    assert below.selected_first_token_id == pair.market_5.up_token_id
    assert below.selected_second_token_id == pair.market_15.down_token_id
    assert above.selected_action is PairAction.LONG_15_UP_LONG_5_DOWN
    assert above.selected_first_token_id == pair.market_15.up_token_id
    assert above.selected_second_token_id == pair.market_5.down_token_id


def test_structural_floor_equal_strikes_compares_both_routes_and_keeps_probe_buffer() -> (
    None
):
    pair = _v07_pair()
    books = _books()

    verdict = validate_structural_floor(
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("100"),
        ),
        books=books,
        pricing_policy=PairPricingPolicy(pair_risk_usdc=D("5")),
    )

    assert {level.action for level in verdict.depth_ladder} == {
        PairAction.LONG_15_UP_LONG_5_DOWN,
        PairAction.LONG_5_UP_LONG_15_DOWN,
    }
    assert verdict.selected_action is PairAction.LONG_15_UP_LONG_5_DOWN
    assert verdict.deterministic_floor_exists is True
    assert verdict.probe_buffer_ok is True
    assert verdict.split_probability_live_fallback_allowed is False


def test_structural_floor_distinguishes_c_equal_1_from_point_99_probe_buffer() -> None:
    original = _v07_pair()
    fee_exempt = ExecutionFeeSchedule.fee_exempt(
        reason="boundary-test",
        source_ref="unit-fixture",
    )
    pair = SameExpiryPair.from_contracts(
        replace(
            original.market_5,
            fee_schedule=fee_exempt,
            rule_hash="1" * 64,
        ),
        replace(
            original.market_15,
            fee_schedule=fee_exempt,
            rule_hash="2" * 64,
        ),
    )
    state = replace(
        _settlement_state(original),
        market_5_rule_hash=pair.market_5.rule_hash,
        market_15_rule_hash=pair.market_15.rule_hash,
        strike_5=D("100"),
        strike_15=D("101"),
    )

    at_one = validate_structural_floor(
        pair=pair,
        settlement_state=state,
        books={
            pair.market_5.up_token_id: _book(pair.market_5.up_token_id, ask="0.50"),
            pair.market_15.down_token_id: _book(
                pair.market_15.down_token_id, ask="0.50"
            ),
        },
        pricing_policy=PairPricingPolicy(pair_risk_usdc=D("10")),
    )
    at_point_99 = validate_structural_floor(
        pair=pair,
        settlement_state=state,
        books={
            pair.market_5.up_token_id: _book(pair.market_5.up_token_id, ask="0.49"),
            pair.market_15.down_token_id: _book(
                pair.market_15.down_token_id, ask="0.50"
            ),
        },
        pricing_policy=PairPricingPolicy(pair_risk_usdc=D("10")),
    )

    assert at_one.selected_level is not None
    assert at_one.selected_level.all_in_cost_per_pair == D("1")
    assert at_one.deterministic_floor_exists is True
    assert at_one.positive_edge_after_cost is False
    assert at_one.probe_buffer_ok is False
    assert at_point_99.selected_level is not None
    assert at_point_99.selected_level.all_in_cost_per_pair == D("0.99")
    assert at_point_99.positive_edge_after_cost is True
    assert at_point_99.probe_buffer_ok is True


def test_structural_floor_keeps_extreme_prices_when_split_probability_collapses() -> None:
    original = _v07_pair()
    fee_exempt = ExecutionFeeSchedule.fee_exempt(
        reason="structural-boundary-test",
        source_ref="unit-fixture",
    )
    pair = SameExpiryPair.from_contracts(
        replace(
            original.market_5,
            fee_schedule=fee_exempt,
            tick_size=D("0.001"),
            rule_hash="3" * 64,
        ),
        replace(
            original.market_15,
            fee_schedule=fee_exempt,
            tick_size=D("0.001"),
            rule_hash="4" * 64,
        ),
    )
    state = replace(
        _settlement_state(original),
        market_5_rule_hash=pair.market_5.rule_hash,
        market_15_rule_hash=pair.market_15.rule_hash,
        strike_5=D("100"),
        strike_15=D("101"),
    )

    verdict = validate_structural_floor(
        pair=pair,
        settlement_state=state,
        books={
            pair.market_5.up_token_id: _book(
                pair.market_5.up_token_id,
                bid="0.009",
                ask="0.010",
                tick_size="0.001",
            ),
            pair.market_15.down_token_id: _book(
                pair.market_15.down_token_id,
                bid="0.979",
                ask="0.980",
                tick_size="0.001",
            ),
        },
        pricing_policy=PairPricingPolicy(
            structural_only=True,
            pair_risk_usdc=D("10"),
        ),
    )

    assert verdict.selected_level is not None
    assert verdict.selected_level.all_in_cost_per_pair == D("0.99")
    assert verdict.selected_level.positive_edge_after_cost is True


def test_structural_floor_rejects_book_rules_that_do_not_match_the_contract() -> None:
    pair = _v07_pair()
    state = replace(
        _settlement_state(pair),
        strike_5=D("100"),
        strike_15=D("101"),
    )

    verdict = validate_structural_floor(
        pair=pair,
        settlement_state=state,
        books={
            pair.market_5.up_token_id: _book(
                pair.market_5.up_token_id,
                ask="0.40",
                tick_size="0.001",
            ),
            pair.market_15.down_token_id: _book(
                pair.market_15.down_token_id,
                ask="0.50",
            ),
        },
    )

    assert verdict.selected_level is None


def test_perfect_information_upper_bound_is_gate_zero_and_stops_non_positive_route() -> (
    None
):
    pair = _v07_pair()
    books = _books()
    positive = PerfectInformationAttempt(
        attempt_id="positive",
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        books=books,
        actual_5_up=True,
        actual_15_up=False,
    )
    negative_books = {
        pair.market_5.up_token_id: _book(pair.market_5.up_token_id, ask="0.80"),
        pair.market_5.down_token_id: _book(pair.market_5.down_token_id, ask="0.80"),
        pair.market_15.up_token_id: _book(pair.market_15.up_token_id, ask="0.80"),
        pair.market_15.down_token_id: _book(pair.market_15.down_token_id, ask="0.80"),
    }
    negative = PerfectInformationAttempt(
        attempt_id="negative",
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        books=negative_books,
        actual_5_up=True,
        actual_15_up=True,
    )

    positive_report = evaluate_perfect_information_upper_bound((positive,))
    negative_report = evaluate_perfect_information_upper_bound((negative,))

    assert positive_report.gate_0_passed is True
    assert positive_report.stop_recommended is False
    assert positive_report.attempts[0].best_action is PairAction.LONG_5_UP_LONG_15_DOWN
    assert negative_report.gate_0_passed is False
    assert negative_report.stop_recommended is True
    assert set(positive_report.attempts[0].per_action_best_total_pnl) == {
        PairAction.LONG_15_UP_LONG_5_DOWN.value,
        PairAction.LONG_5_UP_LONG_15_DOWN.value,
    }
    assert all(
        value >= D("0")
        for value in negative_report.attempts[0].per_action_best_total_pnl.values()
    )


def test_gate_zero_enumerates_non_floor_direction_as_a_true_hindsight_bound() -> None:
    pair = _v07_pair()
    books = {
        pair.market_5.up_token_id: _book(pair.market_5.up_token_id, ask="0.80"),
        pair.market_5.down_token_id: _book(pair.market_5.down_token_id, ask="0.10"),
        pair.market_15.up_token_id: _book(pair.market_15.up_token_id, ask="0.10"),
        pair.market_15.down_token_id: _book(pair.market_15.down_token_id, ask="0.80"),
    }
    attempt = PerfectInformationAttempt(
        attempt_id="hindsight-direction",
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        books=books,
        actual_5_up=False,
        actual_15_up=False,
    )

    report = evaluate_perfect_information_upper_bound((attempt,))

    assert (
        report.attempts[0].unrestricted_best_action
        is PairAction.LONG_15_UP_LONG_5_DOWN
    )
    assert report.attempts[0].unrestricted_best_total_pnl > D("0")
    assert report.attempts[0].best_action is None
    assert report.attempts[0].best_total_pnl == D("0")
    assert report.gate_0_passed is False
    assert report.unrestricted_hindsight_aggregate_best_total_pnl > D("0")


def test_gate_zero_upper_bound_is_not_truncated_by_v07_pair_risk_budget() -> None:
    pair = _v07_pair()
    books = {
        pair.market_5.up_token_id: _book(
            pair.market_5.up_token_id, ask="0.40", size="100"
        ),
        pair.market_5.down_token_id: _book(
            pair.market_5.down_token_id, ask="0.20", size="100"
        ),
        pair.market_15.up_token_id: _book(
            pair.market_15.up_token_id, ask="0.30", size="100"
        ),
        pair.market_15.down_token_id: _book(
            pair.market_15.down_token_id, ask="0.25", size="100"
        ),
    }
    attempt = PerfectInformationAttempt(
        attempt_id="full-depth-upper-bound",
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        books=books,
        actual_5_up=True,
        actual_15_up=False,
    )

    report = evaluate_perfect_information_upper_bound((attempt,))

    structural_breakpoints = [
        item
        for item in report.attempts[0].breakpoints
        if item.action is PairAction.LONG_5_UP_LONG_15_DOWN
    ]
    assert max(item.quantity for item in structural_breakpoints) == D("100")


def test_gate_zero_keeps_extreme_structural_books_near_zero_split_probability() -> (
    None
):
    original = _v07_pair()
    pair = SameExpiryPair.from_contracts(
        replace(original.market_5, tick_size=D("0.001")),
        replace(original.market_15, tick_size=D("0.001")),
    )
    attempt = PerfectInformationAttempt(
        attempt_id="extreme-structural-books",
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("101"),
            strike_15=D("100"),
        ),
        books={
            pair.market_15.up_token_id: _book(
                pair.market_15.up_token_id,
                ask="0.99",
                bid="0.989",
                tick_size="0.001",
            ),
            pair.market_5.down_token_id: _book(
                pair.market_5.down_token_id,
                ask="0.01",
                bid="0.001",
                tick_size="0.001",
            ),
        },
        actual_5_up=True,
        actual_15_up=True,
        pricing_policy=GATE_0_PAIR_PRICING_POLICY,
    )

    report = evaluate_perfect_information_upper_bound((attempt,))

    assert report.attempts[0].breakpoints
    assert {
        breakpoint.action for breakpoint in report.attempts[0].breakpoints
    } == {PairAction.LONG_15_UP_LONG_5_DOWN}
    assert (
        report.attempts[0].breakpoints[0].all_in_cost_per_pair
        == D("1.001388")
    )


def test_coverage_validator_requires_continuous_official_rtds_and_complete_inputs() -> (
    None
):
    complete = validate_cohort_data_coverage(
        CohortCoverageInput(
            cohort_id="cohort-ok",
            market_15_open_ms=0,
            common_expiry_ms=3_000,
            rtds_observed_at_ms=(0, 1_000, 2_000, 3_000),
            rtds_received_at_ms=(10, 1_010, 2_010, 3_010),
            market_5_l2_complete=True,
            market_15_l2_complete=True,
            market_5_trades_complete=True,
            market_15_trades_complete=True,
            fee_complete=True,
            rule_complete=True,
            source_timestamps_complete=True,
            receipt_timestamps_complete=True,
            disconnect_count=0,
            error_count=0,
            clock_sync_valid=True,
        )
    )
    incomplete = validate_cohort_data_coverage(
        CohortCoverageInput(
            cohort_id="cohort-gap",
            market_15_open_ms=0,
            common_expiry_ms=3_000,
            rtds_observed_at_ms=(1_000, 3_500),
            rtds_received_at_ms=(10, 20),
            market_5_l2_complete=False,
            market_15_l2_complete=True,
            market_5_trades_complete=True,
            market_15_trades_complete=False,
            fee_complete=False,
            rule_complete=True,
            source_timestamps_complete=False,
            receipt_timestamps_complete=True,
            disconnect_count=1,
            error_count=1,
            clock_sync_valid=False,
        )
    )

    assert complete.complete is True
    assert incomplete.complete is False
    assert "official_rtds_starts_after_15m_open" in incomplete.reason_codes
    assert "official_rtds_gap_detected" in incomplete.reason_codes
    assert "market_5_l2_incomplete" in incomplete.reason_codes
    assert "fee_metadata_incomplete" in incomplete.reason_codes
    assert "official_rtds_disconnect_observed" in incomplete.reason_codes
    assert "official_rtds_error_observed" in incomplete.reason_codes
    assert "clock_sync_invalid" in incomplete.reason_codes


def test_probe_and_strategy_live_readiness_are_separated_and_fail_closed(
    tmp_path: Path,
) -> None:
    pair = _v07_pair()
    books = _books()
    floor = validate_structural_floor(
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        books=books,
        pricing_policy=PairPricingPolicy(
            pair_risk_usdc=D("5"),
            maker_fill_cap_by_token_id={
                pair.market_5.up_token_id: D("5"),
                pair.market_15.down_token_id: D("5"),
            },
        ),
        execution_mode=PairExecutionMode.MAKER_MAKER,
    )
    coverage = tuple(
        validate_cohort_data_coverage(
            CohortCoverageInput(
                cohort_id=f"cohort-{index}",
                market_15_open_ms=0,
                common_expiry_ms=3_000,
                rtds_observed_at_ms=(0, 1_000, 2_000, 3_000),
                rtds_received_at_ms=(10, 1_010, 2_010, 3_010),
                market_5_l2_complete=True,
                market_15_l2_complete=True,
                market_5_trades_complete=True,
                market_15_trades_complete=True,
                fee_complete=True,
                rule_complete=True,
                source_timestamps_complete=True,
                receipt_timestamps_complete=True,
            )
        )
        for index in range(4)
    )
    probe = evaluate_execution_probe_readiness(
        structural_shadow_report=None,
        clean_common_terminal_cohort_count=4,
        coverage_results=coverage,
        structural_floor=floor,
        prerequisites=_probe_prerequisites(tmp_path),
    )
    insufficient_unique_probe = evaluate_execution_probe_readiness(
        structural_shadow_report=None,
        clean_common_terminal_cohort_count=4,
        coverage_results=(coverage[0], coverage[0], coverage[0], coverage[0]),
        structural_floor=floor,
        prerequisites=_probe_prerequisites(tmp_path / "duplicate"),
    )

    common_live_inputs = _strategy_live_inputs(tmp_path / "live")
    live = evaluate_strategy_live_readiness_inputs(common_live_inputs)
    predictive_only = evaluate_strategy_live_readiness_inputs(
        replace(
            common_live_inputs,
            structural_true_edge_gate_satisfied=False,
        )
    )

    assert probe.eligible is False
    assert "neutral_shadow_artifact_missing" in probe.reason_codes
    assert insufficient_unique_probe.eligible is False
    assert "fewer_than_4_unique_complete_common_terminal_cohorts" in (
        insufficient_unique_probe.reason_codes
    )
    assert live.eligible is False
    assert "neutral_shadow_builder_authority_untrusted" in live.reason_codes
    assert predictive_only.eligible is False
    assert "structural_true_edge_gate_not_satisfied" in predictive_only.reason_codes


def test_probe_rejects_object_fallback_and_self_report_only_inputs(
    tmp_path: Path,
) -> None:
    pair = _v07_pair()
    floor = validate_structural_floor(
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        books=_books(),
        pricing_policy=PairPricingPolicy(
            pair_risk_usdc=D("5"),
            maker_fill_cap_by_token_id={
                pair.market_5.up_token_id: D("5"),
                pair.market_15.down_token_id: D("5"),
            },
        ),
        execution_mode=PairExecutionMode.MAKER_MAKER,
    )
    coverage = tuple(
        CohortCoverageResult(
            cohort_id=f"cohort-{index}",
            complete=True,
            reason_codes=(),
        )
        for index in range(4)
    )
    result = evaluate_execution_probe_readiness(
        structural_shadow_report=_strategy_shadow_artifact(tmp_path / "shadow-object"),
        clean_common_terminal_cohort_count=4,
        coverage_results=coverage,
        structural_floor=floor,
        prerequisites=ExecutionProbePrerequisites(
            service_continuously_healthy=True,
            authenticated_read_verified=True,
            fill_stream_verified=True,
            failure_drills_complete=True,
            immutable_probe_preregistration_present=True,
            full_hedge_depth_verified=True,
            gate_0_passed=True,
            double_maker_probe_implemented=True,
            capture_capacity_gate_passed=True,
            service_health_evaluation_utc_now=PROBE_EVALUATION_NOW,
            service_health_max_age_seconds=SERVICE_HEALTH_MAX_AGE_SECONDS,
            service_health_min_window_ms=SERVICE_HEALTH_MIN_WINDOW_MS,
        ),
    )

    assert result.eligible is False
    assert "probe_verified_evidence_bundle_missing" in result.reason_codes
    assert "neutral_shadow_artifact_missing" in result.reason_codes
    assert "neutral_shadow_object_fallback_rejected" in result.reason_codes


def test_live_requires_verified_bundle_even_when_all_self_reports_are_positive() -> None:
    result = evaluate_strategy_live_readiness_inputs(
        StrategyLiveInputs(
            builder_verified_evidence_chain=True,
            auditable_prelabel_lock_evidence=True,
            clean_prelabeled_common_terminal_cohort_count=200,
            structural_settled_expiry_cluster_count=200,
            structural_explainable_economic_attempt_count=200,
            structural_bootstrap_cluster_mean_lower_95=D("0.01"),
            structural_true_edge_gate_satisfied=True,
            structural_qualified_net_pnl=D("10"),
            structural_gate_0_passed=True,
            structural_max_single_expiry_pnl_concentration=D("0.10"),
            complete_real_execution_evidence=True,
            all_locked_cohorts_in_pnl_distribution=True,
            service_continuously_healthy=True,
            consecutive_profitable_utc_days=14,
            minimum_daily_net_pnl=D("20"),
            peak_capital_deployed=D("2000"),
            service_health_evaluation_utc_now=LIVE_EVALUATION_NOW,
            service_health_max_age_seconds=SERVICE_HEALTH_MAX_AGE_SECONDS,
            service_health_min_window_ms=SERVICE_HEALTH_MIN_WINDOW_MS,
            daily_ledger_evaluation_utc_now=LIVE_EVALUATION_NOW,
            daily_ledger_max_age_days=DAILY_LEDGER_MAX_AGE_DAYS,
        )
    )

    assert result.eligible is False
    assert "strategy_live_verified_evidence_bundle_missing" in result.reason_codes


def test_tampered_artifact_invalidates_preverified_live_bundle(tmp_path: Path) -> None:
    bundle = _strategy_bundle(tmp_path)
    daily_path = Path(bundle.daily_ledger_artifact.path)
    daily_path.write_text("{}", encoding="utf-8")

    result = evaluate_strategy_live_readiness_inputs(
        replace(_strategy_live_inputs(tmp_path / "tampered"), verified_evidence_bundle=bundle)
    )

    assert result.eligible is False
    assert "daily_ledger_artifact_invalid" in result.reason_codes


def test_live_recomputes_daily_and_capital_thresholds_from_rows_not_summaries(
    tmp_path: Path,
) -> None:
    bundle = _strategy_bundle(tmp_path)
    capital_path = Path(bundle.capital_ledger_artifact.path)
    capital_path.write_text(
        json.dumps(
            {
                "schema_version": readiness.CAPITAL_LEDGER_ARTIFACT_SCHEMA,
                "peak_capital_deployed": "100",
                "rows": [{"capital_deployed": "2500"}],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = evaluate_strategy_live_readiness_inputs(
        replace(_strategy_live_inputs(tmp_path / "capital"), verified_evidence_bundle=bundle)
    )

    assert result.eligible is False
    assert "capital_ledger_artifact_invalid" in result.reason_codes or (
        "peak_capital_deployed_outside_0_to_2000" in result.reason_codes
    )


def test_gate_zero_is_mandatory_for_probe_and_live(tmp_path: Path) -> None:
    live = evaluate_strategy_live_readiness_inputs(
        replace(
            _strategy_live_inputs(tmp_path),
            structural_gate_0_passed=False,
        )
    )

    assert live.eligible is False
    assert "structural_gate_0_not_passed" in live.reason_codes


def test_live_gate_requires_200_expiries_and_the_14_day_commercial_floor(
    tmp_path: Path,
) -> None:
    inputs = replace(
        _strategy_live_inputs(tmp_path),
        clean_prelabeled_common_terminal_cohort_count=199,
        structural_settled_expiry_cluster_count=199,
        structural_explainable_economic_attempt_count=199,
        structural_qualified_net_pnl=D("100"),
        structural_max_single_expiry_pnl_concentration=D("0.20"),
        consecutive_profitable_utc_days=13,
        minimum_daily_net_pnl=D("19.99"),
        peak_capital_deployed=D("2000.01"),
    )

    result = evaluate_strategy_live_readiness_inputs(inputs)

    assert result.eligible is False
    assert {
        "fewer_than_200_clean_prelabeled_common_terminal_cohorts",
        "structural_fewer_than_200_distinct_settled_expiry_clusters",
        "structural_fewer_than_200_explainable_locked_oos_economic_attempts",
        "fewer_than_14_consecutive_profitable_utc_days",
        "minimum_daily_net_pnl_below_20",
        "peak_capital_deployed_outside_0_to_2000",
    }.issubset(result.reason_codes)


def test_capture_capacity_gate_enforces_failure_storage_memory_and_cpu_limits() -> None:
    passing = evaluate_capture_capacity(
        CaptureCapacityEvidence(
            capture_attempt_count=100,
            capture_failure_count=4,
            free_disk_bytes=10 * 1024**3,
            projected_daily_capture_bytes=1024**3,
            available_memory_bytes=2 * 1024**3,
            burstable_cpu_credit_exhausted=False,
        )
    )
    boundary = evaluate_capture_capacity(
        CaptureCapacityEvidence(
            capture_attempt_count=100,
            capture_failure_count=5,
            free_disk_bytes=10 * 1024**3,
            projected_daily_capture_bytes=1024**3,
            available_memory_bytes=2 * 1024**3,
            burstable_cpu_credit_exhausted=False,
        )
    )
    failing = evaluate_capture_capacity(
        CaptureCapacityEvidence(
            capture_attempt_count=100,
            capture_failure_count=6,
            free_disk_bytes=9 * 1024**3,
            projected_daily_capture_bytes=1024**3 + 1,
            available_memory_bytes=2 * 1024**3 - 1,
            burstable_cpu_credit_exhausted=True,
        )
    )

    assert passing.passed is True
    assert passing.failure_rate == D("0.04")
    assert boundary.passed is False
    assert boundary.reason_codes == ("capture_failure_rate_not_below_5pct",)
    assert failing.passed is False
    assert set(failing.reason_codes) == {
        "capture_failure_rate_not_below_5pct",
        "capture_free_disk_below_10gib",
        "projected_daily_capture_above_1gib",
        "capture_memory_below_2gib",
        "burstable_cpu_credit_exhausted",
    }


def test_gate_zero_report_rejects_non_mapping_maker_attempts_fail_closed(
    tmp_path: Path,
) -> None:
    base = json.loads(Path(_gate_0_artifact(tmp_path).path).read_text(encoding="utf-8"))
    base["execution_modes"]["maker_maker"]["attempts"] = ["not-a-mapping"]
    base["report_sha256"] = readiness.sha256(
        readiness.canonical_json_bytes(
            {key: value for key, value in base.items() if key != "report_sha256"}
        )
    ).hexdigest()
    artifact = _write_artifact(tmp_path, "gate0-invalid-attempts.json", base)

    report, reasons = readiness._validated_gate_0_report_document(artifact)

    assert report is None
    assert reasons == ("gate_0_report_maker_maker_attempts_invalid",)


def test_gate_zero_report_recomputes_maker_maker_economic_gate(
    tmp_path: Path,
) -> None:
    base = json.loads(Path(_gate_0_artifact(tmp_path).path).read_text(encoding="utf-8"))
    maker_maker = base["execution_modes"]["maker_maker"]
    maker_maker["aggregate_best_total_pnl"] = "0.10"
    maker_maker["average_best_total_pnl_per_expiry"] = "0.10"
    maker_maker["economic_gate_passed"] = True
    base["gate_0_passed"] = True
    base["decision"] = "PASS"
    base["stop_recommended"] = False
    base["report_sha256"] = readiness.sha256(
        readiness.canonical_json_bytes(
            {key: value for key, value in base.items() if key != "report_sha256"}
        )
    ).hexdigest()
    artifact = _write_artifact(tmp_path, "gate0-economic-mismatch.json", base)

    report, reasons = readiness._validated_gate_0_report_document(artifact)

    assert report is None
    assert reasons == ("gate_0_report_maker_maker_economic_invalid",)


def test_strategy_live_requires_explicit_evaluation_times_and_freshness(
    tmp_path: Path,
) -> None:
    result = evaluate_strategy_live_readiness_inputs(
        replace(
            _strategy_live_inputs(tmp_path),
            service_health_evaluation_utc_now=None,
            daily_ledger_evaluation_utc_now=None,
        )
    )

    assert result.eligible is False
    assert "service_health_evaluation_now_missing" in result.reason_codes
    assert "daily_ledger_evaluation_now_missing" in result.reason_codes


def test_strategy_live_rejects_invalid_daily_generated_at_and_stale_service_health(
    tmp_path: Path,
) -> None:
    inputs = _strategy_live_inputs(tmp_path)
    daily_path = Path(inputs.verified_evidence_bundle.daily_ledger_artifact.path)
    daily = json.loads(daily_path.read_text(encoding="utf-8"))
    daily["generated_at"] = "not-a-timestamp"
    daily_path.write_text(json.dumps(daily, sort_keys=True), encoding="utf-8")
    service_path = Path(inputs.verified_evidence_bundle.service_health_artifact.path)
    service = json.loads(service_path.read_text(encoding="utf-8"))
    stale_end = LIVE_EVALUATION_NOW - timedelta(hours=1)
    service["generated_at"] = _utc_text(stale_end)
    service["window_started_at_ms"] = _epoch_ms(stale_end - timedelta(seconds=30))
    service["window_ended_at_ms"] = _epoch_ms(stale_end)
    service_path.write_text(json.dumps(service, sort_keys=True), encoding="utf-8")
    reloaded_bundle = replace(
        inputs.verified_evidence_bundle,
        daily_ledger_artifact=load_verified_json_artifact(daily_path),
        service_health_artifact=load_verified_json_artifact(service_path),
    )

    result = evaluate_strategy_live_readiness_inputs(
        replace(inputs, verified_evidence_bundle=reloaded_bundle)
    )

    assert result.eligible is False
    assert "daily_ledger_generated_at_invalid" in result.reason_codes
    assert "service_health_artifact_stale" in result.reason_codes


def test_service_health_fresh_generated_at_cannot_extend_old_window(
    tmp_path: Path,
) -> None:
    inputs = _strategy_live_inputs(tmp_path)
    service_path = Path(inputs.verified_evidence_bundle.service_health_artifact.path)
    service = json.loads(service_path.read_text(encoding="utf-8"))
    stale_end = LIVE_EVALUATION_NOW - timedelta(hours=1)
    service["generated_at"] = _utc_text(LIVE_EVALUATION_NOW - timedelta(seconds=5))
    service["window_started_at_ms"] = _epoch_ms(stale_end - timedelta(seconds=30))
    service["window_ended_at_ms"] = _epoch_ms(stale_end)
    service_path.write_text(json.dumps(service, sort_keys=True), encoding="utf-8")
    reloaded_bundle = replace(
        inputs.verified_evidence_bundle,
        service_health_artifact=load_verified_json_artifact(service_path),
    )

    result = evaluate_strategy_live_readiness_inputs(
        replace(inputs, verified_evidence_bundle=reloaded_bundle)
    )

    assert result.eligible is False
    assert "service_health_artifact_stale" in result.reason_codes


def test_probe_capture_capacity_requires_expected_v08_track_binding(
    tmp_path: Path,
) -> None:
    prerequisites = _probe_prerequisites(tmp_path)
    capacity_path = Path(prerequisites.verified_evidence_bundle.capture_capacity_artifact.path)
    capacity = json.loads(capacity_path.read_text(encoding="utf-8"))
    capacity["track"] = "other-track"
    capacity["artifact_sha256"] = readiness.sha256(
        readiness.canonical_json_bytes(
            {key: value for key, value in capacity.items() if key != "artifact_sha256"}
        )
    ).hexdigest()
    capacity_path.write_text(json.dumps(capacity, sort_keys=True), encoding="utf-8")

    result = evaluate_execution_probe_readiness(
        structural_shadow_report=None,
        clean_common_terminal_cohort_count=4,
        coverage_results=tuple(
            CohortCoverageResult(
                cohort_id=f"cohort-{index}",
                complete=True,
                reason_codes=(),
            )
            for index in range(4)
        ),
        structural_floor=validate_structural_floor(
            pair=_v07_pair(),
            settlement_state=replace(
                _settlement_state(_v07_pair()),
                strike_5=D("100"),
                strike_15=D("101"),
            ),
            books=_books(),
            pricing_policy=PairPricingPolicy(
                pair_risk_usdc=D("5"),
                maker_fill_cap_by_token_id={
                    _v07_pair().market_5.up_token_id: D("5"),
                    _v07_pair().market_15.down_token_id: D("5"),
                },
            ),
            execution_mode=PairExecutionMode.MAKER_MAKER,
        ),
        prerequisites=replace(
            prerequisites,
            verified_evidence_bundle=replace(
                prerequisites.verified_evidence_bundle,
                capture_capacity_artifact=load_verified_json_artifact(capacity_path),
            ),
        ),
    )

    assert result.eligible is False
    assert "capture_capacity_track_mismatch" in result.reason_codes


def test_live_rejects_self_hash_authority_and_accepts_bound_synthetic_positive_path(
    tmp_path: Path,
) -> None:
    no_policy = evaluate_strategy_live_readiness_inputs(
        _strategy_live_inputs(tmp_path / "no-policy", trusted_shadow=True)
    )
    wrong_pin_root = tmp_path / "wrong-pin"
    wrong_pin = evaluate_strategy_live_readiness_inputs(
        _strategy_live_inputs(
            wrong_pin_root,
            trusted_shadow=True,
            structural_authority_trust_policy=readiness.StructuralAuthorityTrustPolicy(
                trusted_build_receipt_path=str(
                    wrong_pin_root / "authority" / "trusted-build-receipt.json"
                ),
                trusted_build_receipt_sha256="0" * 64,
                external_anchor_reference=str(
                    wrong_pin_root / "authority" / "external-anchor.json"
                ),
                external_anchor_sha256="1" * 64,
            ),
        )
    )
    trusted_root = tmp_path / "trusted"
    trusted_inputs = _strategy_live_inputs(
        trusted_root,
        trusted_shadow=True,
    )
    bound = evaluate_strategy_live_readiness_inputs(
        replace(
            trusted_inputs,
            structural_authority_trust_policy=_trusted_shadow_policy(trusted_root),
        )
    )
    self_ref_root = tmp_path / "self-ref"
    self_hash_inputs = _strategy_live_inputs(self_ref_root)
    self_hash_inputs = replace(
        self_hash_inputs,
        verified_evidence_bundle=replace(
            self_hash_inputs.verified_evidence_bundle,
            structural_shadow_artifact=_strategy_shadow_artifact(
                self_ref_root,
                trusted=True,
                self_hash_receipt=True,
            ),
        ),
        structural_authority_trust_policy=readiness.StructuralAuthorityTrustPolicy(
            trusted_build_receipt_path=str(self_ref_root / "shadow.json"),
            trusted_build_receipt_sha256=readiness.sha256(
                (self_ref_root / "shadow.json").read_bytes()
            ).hexdigest(),
            external_anchor_reference=str(
                self_ref_root / "authority" / "external-anchor.json"
            ),
            external_anchor_sha256=readiness.sha256(
                (self_ref_root / "authority" / "external-anchor.json").read_bytes()
            ).hexdigest(),
        ),
    )
    fake_trust = evaluate_strategy_live_readiness_inputs(self_hash_inputs)

    assert no_policy.eligible is False
    assert "neutral_shadow_builder_authority_untrusted" in no_policy.reason_codes
    assert wrong_pin.eligible is False
    assert "neutral_shadow_builder_authority_policy_mismatch" in wrong_pin.reason_codes
    assert fake_trust.eligible is False
    assert {
        "neutral_shadow_builder_authority_policy_mismatch",
        "neutral_shadow_builder_authority_trusted_build_receipt_invalid",
    } & set(fake_trust.reason_codes)
    assert bound.eligible is True
