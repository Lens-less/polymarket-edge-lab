from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from src.edge_lab.btc_twap_pair_pricing import PairExecutionMode, PairPricingPolicy
from src.edge_lab.btc_twap_relative_value import (
    OrderBookSnapshot,
    PairAction,
    PairSettlementState,
    SameExpiryPair,
)
from src.edge_lab.btc_twap_relative_value_readiness import (
    AUTHENTICATED_READ_ARTIFACT_SCHEMA,
    CAPTURE_CAPACITY_ARTIFACT_SCHEMA,
    FAULT_DRILLS_ARTIFACT_SCHEMA,
    FILL_STREAM_ARTIFACT_SCHEMA,
    PROBE_ADAPTER_ARTIFACT_SCHEMA,
    REQUIRED_FAULT_DRILLS,
    SERVICE_HEALTH_ARTIFACT_SCHEMA,
    STRICT_GATE_0_REPORT_SCHEMA,
    CohortCoverageResult,
    ExecutionProbeEvidenceBundle,
    ExecutionProbePrerequisites,
    VerifiedJsonArtifact,
    _strict_gate_0_parameters,
    evaluate_execution_probe_readiness,
    validate_structural_floor,
)
from src.edge_lab.btc_twap_structural_shadow import (
    LockedZeroCohort,
    SingleLegDisposition,
    StructuralCaptureVerification,
    StructuralDirection,
    StructuralDoubleMakerPlan,
    StructuralShadowAttemptReport,
    StructuralShadowResult,
    _bind_rolling_audit,
    _verified_finalized_capture_bundle,
    build_structural_double_maker_plan,
    build_structural_shadow_report,
    evaluate_shadow_robustness,
    replay_structural_double_maker,
)
from src.edge_lab.data_store import canonical_json_bytes
from src.edge_lab.execution import DepthBook, OrderSide, QueueScenario, TradeEvent
from tests.test_btc_twap_relative_value_v07_replay import _v07_pair

D = Decimal
PREREGISTRATION_SHA256 = "f" * 64


def _capture_verification(attempt_id: str) -> StructuralCaptureVerification:
    return StructuralCaptureVerification(
        attempt_id=attempt_id,
        capture_attempt_id=f"capture-{attempt_id}",
        capture_root=f"/captures/{attempt_id}",
        capture_tree_sha256=hashlib.sha256(
            f"capture-tree:{attempt_id}".encode()
        ).hexdigest(),
        manifest_count=1,
        record_count=1,
        book_event_count=0,
        trade_event_count=0,
        complete_window=True,
    )


def _capture_verifications(
    plans: list[StructuralDoubleMakerPlan] | tuple[StructuralDoubleMakerPlan, ...],
) -> dict[str, StructuralCaptureVerification]:
    return {plan.attempt_id: _capture_verification(plan.attempt_id) for plan in plans}


def _book(
    token_id: str,
    *,
    bid: str,
    bid_size: str = "5",
    ask: str = "0.99",
    ask_size: str = "5",
    timestamp_ms: int = 0,
) -> OrderBookSnapshot:
    return OrderBookSnapshot.from_tuples(
        token_id,
        bids=((D(bid), D(bid_size)),),
        asks=((D(ask), D(ask_size)),),
        timestamp_ms=timestamp_ms,
        tick_size=D("0.01"),
        minimum_order_size=D("5"),
    )


def _books() -> dict[str, OrderBookSnapshot]:
    pair = _v07_pair()
    return {
        pair.market_15.up_token_id: _book(pair.market_15.up_token_id, bid="0.40"),
        pair.market_15.down_token_id: _book(pair.market_15.down_token_id, bid="0.60"),
        pair.market_5.up_token_id: _book(pair.market_5.up_token_id, bid="0.42"),
        pair.market_5.down_token_id: _book(pair.market_5.down_token_id, bid="0.58"),
    }


def _settlement_state(
    pair: SameExpiryPair,
    direction: StructuralDirection = StructuralDirection.FIVE_ABOVE_FIFTEEN,
) -> PairSettlementState:
    if direction is StructuralDirection.FIVE_ABOVE_FIFTEEN:
        strike_5, strike_15 = D("101"), D("100")
    elif direction is StructuralDirection.FIVE_BELOW_FIFTEEN:
        strike_5, strike_15 = D("100"), D("101")
    else:
        strike_5 = strike_15 = D("100")
    return PairSettlementState(
        market_5_rule_hash=pair.market_5.rule_hash,
        market_15_rule_hash=pair.market_15.rule_hash,
        market_5_open_timestamp_ms=pair.market_5.opens_at_ms,
        market_15_open_timestamp_ms=pair.market_15.opens_at_ms,
        strike_5=strike_5,
        strike_15=strike_15,
        opening_5_source_event_id="open-5",
        opening_15_source_event_id="open-15",
    )


def _plan() -> StructuralDoubleMakerPlan:
    pair = _v07_pair()
    return build_structural_double_maker_plan(
        attempt_id="expiry-1",
        pair=pair,
        settlement_state=_settlement_state(pair),
        books=_books(),
        action=PairAction.LONG_15_UP_LONG_5_DOWN,
        quantity=D("5"),
        submitted_at_ms=0,
        actual_5_up=True,
        actual_15_up=True,
        max_book_age_ms=1_000,
        initial_cash=D("20"),
    )


def _trade(token_id: str, *, source: str, timestamp_ms: int = 100) -> TradeEvent:
    return TradeEvent(
        token_id=token_id,
        timestamp_ms=timestamp_ms,
        price=D("0.40") if "15" in token_id else D("0.58"),
        quantity=D("10"),
        aggressor_side=OrderSide.SELL,
        source_event_id=source,
    )


def _robust_report_inputs():
    plans = []
    events_by_attempt = {}
    hedge_books_by_attempt = {}
    pair = _v07_pair()
    for index in range(1, 201):
        expiry_ms = 1_000_000 + index
        upward_direction = index % 2 == 1
        action = (
            PairAction.LONG_15_UP_LONG_5_DOWN
            if upward_direction
            else PairAction.LONG_5_UP_LONG_15_DOWN
        )
        direction = (
            StructuralDirection.FIVE_ABOVE_FIFTEEN
            if upward_direction
            else StructuralDirection.FIVE_BELOW_FIFTEEN
        )
        attempt_pair = SameExpiryPair.from_contracts(
            replace(
                pair.market_5,
                opens_at_ms=expiry_ms - 300_000,
                closes_at_ms=expiry_ms,
            ),
            replace(pair.market_15, closes_at_ms=expiry_ms),
        )
        plan = build_structural_double_maker_plan(
            attempt_id=f"expiry-{index}",
            pair=attempt_pair,
            settlement_state=_settlement_state(
                attempt_pair,
                direction,
            ),
            books=_books(),
            action=action,
            quantity=D("5"),
            submitted_at_ms=0,
            actual_5_up=True,
            actual_15_up=upward_direction,
            max_book_age_ms=1_000,
            initial_cash=D("20"),
        )
        plans.append(plan)
        events_by_attempt[plan.attempt_id] = (
            _trade(plan.first.token_id, source=f"trade-first-{index}"),
            _trade(plan.second.token_id, source=f"trade-5-{index}", timestamp_ms=101),
        )
        hedge_books_by_attempt[plan.attempt_id] = {
            plan.second.token_id: DepthBook.from_tuples(
                plan.second.token_id,
                bids=((D("0.58"), D("20")),),
                asks=((D("0.60"), D("20")),),
                tick_size=D("0.01"),
                min_order_size=D("5"),
                timestamp_ms=100,
            )
        }
    return plans, events_by_attempt, hedge_books_by_attempt


def _rolling_audit(
    plans: list[StructuralDoubleMakerPlan] | tuple[StructuralDoubleMakerPlan, ...],
    evidence_hashes: dict[str, str],
    locked_action_hashes: dict[str, str],
    *,
    realized_net_pnl: str | None = None,
) -> dict[str, object]:
    admissions = {}
    decisions = {}
    outcomes = {}
    for plan in plans:
        market_5 = next(
            contract
            for contract in (plan.first.contract, plan.second.contract)
            if contract.horizon == "5m"
        )
        market_15 = next(
            contract
            for contract in (plan.first.contract, plan.second.contract)
            if contract.horizon == "15m"
        )
        admissions[plan.attempt_id] = {
            "common_expiry_id": plan.attempt_id,
            "capture_attempt_id": f"capture-{plan.attempt_id}",
            "expiry_ms": plan.expiry_ms,
            "market_5_id": market_5.market_id,
            "market_15_id": market_15.market_id,
            "condition_5_id": market_5.condition_id,
            "condition_15_id": market_15.condition_id,
        }
        decisions[f"{plan.attempt_id}:60"] = {
            "common_expiry_id": plan.attempt_id,
            "action": plan.to_document()["action"],
            "decision_at_ms": plan.submitted_at_ms,
            "action_payload_sha256": locked_action_hashes[plan.attempt_id],
        }
        outcomes[plan.attempt_id] = {
            "common_expiry_id": plan.attempt_id,
            "outcome": "complete",
            "clean": True,
            "realized_net_pnl": (
                realized_net_pnl
                if realized_net_pnl is not None
                else (
                    "0.10"
                    if plan.direction is StructuralDirection.FIVE_ABOVE_FIFTEEN
                    else "2.00"
                )
            ),
            "evidence_sha256": evidence_hashes[plan.attempt_id],
        }
    return {
        "schema_version": "btc-5m-15m-readiness-v08-rolling-audit.v1",
        "journal_root": "test-journal",
        "valid": True,
        "errors": [],
        "policy": {"preregistration_sha256": PREREGISTRATION_SHA256},
        "admitted_common_expiry_count": len(plans),
        "finalized_common_expiry_count": len(plans),
        "unfinished_common_expiry_count": 0,
        "admissions": admissions,
        "decisions": decisions,
        "outcomes": outcomes,
        "denominator_includes_no_trade_no_fill_and_dirty": True,
    }


def _verified_bundle(
    plans: list[StructuralDoubleMakerPlan] | tuple[StructuralDoubleMakerPlan, ...],
    *,
    locked_zero_cohorts: tuple[LockedZeroCohort, ...] = (),
    capture_verifications: dict[str, StructuralCaptureVerification] | None = None,
    evidence_hashes: dict[str, str] | None = None,
    locked_action_hashes: dict[str, str] | None = None,
    audit: dict[str, object] | None = None,
):
    capture_map = capture_verifications or _capture_verifications(plans)
    evidence_map = evidence_hashes or {
        plan.attempt_id: hashlib.sha256(plan.attempt_id.encode("utf-8")).hexdigest()
        for plan in plans
    }
    locked_map = locked_action_hashes or {
        plan.attempt_id: hashlib.sha256(
            f"locked:{plan.attempt_id}".encode()
        ).hexdigest()
        for plan in plans
    }
    rolling_audit = audit or _rolling_audit(plans, evidence_map, locked_map)
    return _verified_finalized_capture_bundle(
        capture_verification_by_attempt=capture_map,
        locked_action_payload_sha256_by_attempt=locked_map,
        source_evidence_sha256_by_attempt={
            **evidence_map,
            **{
                cohort.attempt_id: cohort.source_evidence_sha256
                for cohort in locked_zero_cohorts
            },
        },
        audit_binding=_bind_rolling_audit(
            tuple(plans),
            locked_zero_cohorts,
            capture_verification_by_attempt=capture_map,
            rolling_audit=rolling_audit,
            preregistration_sha256=PREREGISTRATION_SHA256,
            locked_action_payload_sha256_by_attempt=locked_map,
            source_evidence_sha256_by_attempt={
                **evidence_map,
                **{
                    cohort.attempt_id: cohort.source_evidence_sha256
                    for cohort in locked_zero_cohorts
                },
            },
        ),
    )


def _write_verified_artifact(
    root: Path,
    name: str,
    document: dict[str, object],
) -> VerifiedJsonArtifact:
    path = root / name
    path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    return VerifiedJsonArtifact(
        path=str(path.resolve()),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        schema_version=str(document["schema_version"]),
    )


def _probe_evidence_bundle(
    root: Path,
    *,
    structural_shadow_document: dict[str, object],
) -> ExecutionProbeEvidenceBundle:
    artifact_root = Path.cwd() / ".tmp_shadow_artifacts" / root.name
    artifact_root.mkdir(parents=True, exist_ok=True)

    def write_json(name: str, document: dict[str, object]) -> VerifiedJsonArtifact:
        return _write_verified_artifact(artifact_root, name, document)

    def write_plain(name: str, content: str) -> tuple[str, str]:
        path = artifact_root / name
        path.write_text(content, encoding="utf-8")
        return str(path.resolve()), hashlib.sha256(path.read_bytes()).hexdigest()

    auth_receipt = write_json(
        "auth-receipt.json",
        {"schema_version": "shadow-test-receipt.v1", "ok": True},
    )
    fill_receipt = write_json(
        "fill-receipt.json",
        {"schema_version": "shadow-test-receipt.v1", "ok": True},
    )
    drill_receipts = {
        drill: write_json(
            f"drill-{drill}.json",
            {"schema_version": "shadow-test-receipt.v1", "drill": drill},
        )
        for drill in REQUIRED_FAULT_DRILLS
    }
    code_path, code_sha = write_plain("probe_adapter.py", "pass\n")
    test_path, test_sha = write_plain("test_probe_adapter.py", "pass\n")
    capacity_receipt = write_json(
        "capture-attempt-receipt.json",
        {
            "schema_version": "btc-twap-capture-attempt-receipt.v1",
            "attempt_id": "attempt-1",
            "status": "success",
            "window_started_at_ms": 0,
            "window_ended_at_ms": 1,
        },
    )
    strict_gate_0 = _strict_gate_0_parameters()
    strict_identity = {
        "track_id": strict_gate_0["track_id"],
        "v08_preregistration_sha256": strict_gate_0["v08_preregistration_sha256"],
        "expected_clean_attempts": strict_gate_0["expected_clean_attempts"],
        "scan_ttc_seconds_inclusive": strict_gate_0["scan_ttc_seconds_inclusive"],
        "candidate_ttc_count": strict_gate_0["candidate_ttc_count"],
        "decision_execution_mode": strict_gate_0["decision_execution_mode"],
        "minimum_average_best_total_pnl_per_expiry_usdc": strict_gate_0[
            "minimum_average_best_total_pnl_per_expiry_usdc"
        ],
    }
    gate_0_document = {
        "schema_version": STRICT_GATE_0_REPORT_SCHEMA,
        "authority": {
            **strict_identity,
            "strict_identity_sha256": hashlib.sha256(
                canonical_json_bytes(strict_identity)
            ).hexdigest(),
            "strict_parameters_match": True,
        },
        "decision": "PASS",
        "gate_0_passed": True,
        "rerun_required": False,
    }
    gate_0_document["report_sha256"] = hashlib.sha256(
        canonical_json_bytes(gate_0_document)
    ).hexdigest()
    shadow_document = dict(structural_shadow_document)
    authority_payload = {
        "schema_version": shadow_document.get("schema_version"),
        "issuer": "btc_twap_structural_shadow.finalized_capture_store_builder",
        "source_input_sha256": shadow_document.get("source_input_sha256"),
        "preregistration_sha256": shadow_document.get("preregistration_sha256"),
        "rolling_audit_sha256": shadow_document.get("rolling_audit_sha256"),
        "attempts": [
            {
                "attempt_id": item.get("attempt_id"),
                "expiry_ms": item.get("expiry_ms"),
                "capture_tree_sha256": item.get("capture_verification", {}).get(
                    "capture_tree_sha256"
                )
                if isinstance(item.get("capture_verification"), dict)
                else None,
                "locked_action_payload_sha256": item.get(
                    "locked_action_payload_sha256"
                ),
                "source_evidence_sha256": item.get("source_evidence_sha256"),
            }
            for item in shadow_document.get("attempts", [])
            if isinstance(item, dict)
        ],
        "locked_zero_cohorts": [
            {
                "attempt_id": item.get("attempt_id"),
                "expiry_ms": item.get("expiry_ms"),
                "outcome": item.get("outcome"),
                "capture_tree_sha256": item.get("capture_verification", {}).get(
                    "capture_tree_sha256"
                )
                if isinstance(item.get("capture_verification"), dict)
                else None,
                "source_evidence_sha256": item.get("source_evidence_sha256"),
                "locked_action_payload_sha256": item.get(
                    "locked_action_payload_sha256"
                ),
                "dirty_reason_code": item.get("dirty_reason_code"),
            }
            for item in shadow_document.get("locked_zero_cohorts", [])
            if isinstance(item, dict)
        ],
    }
    shadow_document["builder_authority"] = {
        "issuer": "btc_twap_structural_shadow.finalized_capture_store_builder",
        "authority_kind": "capture_store_finalized",
        "authority_sha256": hashlib.sha256(
            canonical_json_bytes(authority_payload)
        ).hexdigest(),
    }
    shadow_unsigned = dict(shadow_document)
    shadow_unsigned.pop("report_sha256", None)
    shadow_document["report_sha256"] = hashlib.sha256(
        canonical_json_bytes(shadow_unsigned)
    ).hexdigest()
    return ExecutionProbeEvidenceBundle(
        gate_0_report_artifact=_write_verified_artifact(
            artifact_root,
            "gate0.json",
            gate_0_document,
        ),
        structural_shadow_artifact=_write_verified_artifact(
            artifact_root,
            "shadow.json",
            shadow_document,
        ),
        service_health_artifact=_write_verified_artifact(
            artifact_root,
            "service.json",
            {
                "schema_version": SERVICE_HEALTH_ARTIFACT_SCHEMA,
                "service_continuously_healthy": True,
                "heartbeat_source_sha256": "1" * 64,
                "window_started_at_ms": 0,
                "window_ended_at_ms": 1,
            },
        ),
        authenticated_read_artifact=_write_verified_artifact(
            artifact_root,
            "auth.json",
            {
                "schema_version": AUTHENTICATED_READ_ARTIFACT_SCHEMA,
                "authenticated_read_verified": True,
                "receipt_path": auth_receipt.path,
                "receipt_sha256": auth_receipt.sha256,
            },
        ),
        fill_stream_artifact=_write_verified_artifact(
            artifact_root,
            "fill.json",
            {
                "schema_version": FILL_STREAM_ARTIFACT_SCHEMA,
                "fill_stream_verified": True,
                "receipt_path": fill_receipt.path,
                "receipt_sha256": fill_receipt.sha256,
            },
        ),
        fault_drills_artifact=_write_verified_artifact(
            artifact_root,
            "drills.json",
            {
                "schema_version": FAULT_DRILLS_ARTIFACT_SCHEMA,
                "passed_by_drill": {drill: True for drill in REQUIRED_FAULT_DRILLS},
                "receipts_by_drill": {
                    drill: {
                        "path": receipt.path,
                        "sha256": receipt.sha256,
                    }
                    for drill, receipt in drill_receipts.items()
                },
            },
        ),
        probe_adapter_artifact=_write_verified_artifact(
            artifact_root,
            "adapter.json",
            {
                "schema_version": PROBE_ADAPTER_ARTIFACT_SCHEMA,
                "double_maker_probe_implemented": True,
                "full_hedge_depth_verified": True,
                "code_artifact_path": code_path,
                "code_artifact_sha256": code_sha,
                "test_artifact_path": test_path,
                "test_artifact_sha256": test_sha,
            },
        ),
        capture_capacity_artifact=_write_verified_artifact(
            artifact_root,
            "capacity.json",
            {
                "schema_version": CAPTURE_CAPACITY_ARTIFACT_SCHEMA,
                "capture_attempt_count": 10,
                "capture_failure_count": 0,
                "free_disk_bytes": 20 * 1024**3,
                "projected_daily_capture_bytes": 100 * 1024**2,
                "available_memory_bytes": 4 * 1024**3,
                "burstable_cpu_credit_exhausted": False,
                "attempt_receipts": [
                    {
                        "path": capacity_receipt.path,
                        "sha256": capacity_receipt.sha256,
                    }
                ],
            },
        ),
    )


def test_capture_adapter_uses_best_bid_price_and_visible_queue() -> None:
    pair = _v07_pair()
    plan = build_structural_double_maker_plan(
        attempt_id="capture-1",
        pair=pair,
        settlement_state=_settlement_state(pair),
        books=_books(),
        action=PairAction.LONG_15_UP_LONG_5_DOWN,
        quantity=D("5"),
        submitted_at_ms=0,
        actual_5_up=True,
        actual_15_up=True,
        max_book_age_ms=1_000,
    )

    assert plan.direction is StructuralDirection.FIVE_ABOVE_FIFTEEN
    assert plan.first.price == D("0.40")
    assert plan.first.visible_queue_ahead == D("5")
    assert plan.second.price == D("0.58")
    assert plan.second.visible_queue_ahead == D("5")
    assert plan.terminal_payouts[pair.market_15.up_token_id] == D("1")
    assert plan.terminal_payouts[pair.market_5.down_token_id] == D("0")


def test_double_maker_shadow_reuses_all_three_queue_scenarios() -> None:
    plan = _plan()
    events = (
        _trade(plan.first.token_id, source="trade-15"),
        _trade(plan.second.token_id, source="trade-5", timestamp_ms=101),
    )

    optimistic = replay_structural_double_maker(
        plan,
        public_events=events,
        scenario=QueueScenario.OPTIMISTIC,
        disposition=SingleLegDisposition.PASSIVE_WAIT,
    )
    neutral = replay_structural_double_maker(
        plan,
        public_events=events,
        scenario=QueueScenario.NEUTRAL,
        disposition=SingleLegDisposition.PASSIVE_WAIT,
    )
    pessimistic = replay_structural_double_maker(
        plan,
        public_events=events,
        scenario=QueueScenario.PESSIMISTIC,
        disposition=SingleLegDisposition.PASSIVE_WAIT,
    )

    assert optimistic.paired_quantity == D("5")
    assert neutral.paired_quantity == D("5")
    assert neutral.net_pnl == D("0.10")
    assert pessimistic.paired_quantity == D("0")
    assert pessimistic.net_pnl == D("0")


def test_single_leg_dispositions_are_accounted_separately() -> None:
    plan = _plan()
    first_only = (_trade(plan.first.token_id, source="trade-15"),)
    hedge_book = DepthBook.from_tuples(
        plan.second.token_id,
        bids=((D("0.58"), D("20")),),
        asks=((D("0.60"), D("20")),),
        tick_size=D("0.01"),
        min_order_size=D("5"),
        timestamp_ms=100,
    )
    unwind_book = DepthBook.from_tuples(
        plan.first.token_id,
        bids=((D("0.39"), D("20")),),
        asks=((D("0.41"), D("20")),),
        tick_size=D("0.01"),
        min_order_size=D("5"),
        timestamp_ms=100,
    )

    passive = replay_structural_double_maker(
        plan,
        public_events=first_only,
        scenario=QueueScenario.NEUTRAL,
        disposition=SingleLegDisposition.PASSIVE_WAIT,
    )
    hedged = replay_structural_double_maker(
        plan,
        public_events=first_only,
        scenario=QueueScenario.NEUTRAL,
        disposition=SingleLegDisposition.TAKER_HEDGE,
        hedge_books={plan.second.token_id: hedge_book},
    )
    unwound = replay_structural_double_maker(
        plan,
        public_events=first_only,
        scenario=QueueScenario.NEUTRAL,
        disposition=SingleLegDisposition.FAK_UNWIND,
        unwind_books={plan.first.token_id: unwind_book},
    )

    assert passive.unpaired_quantity == D("5")
    assert passive.taker_hedge_quantity == D("0")
    assert passive.fak_unwind_quantity == D("0")
    assert hedged.unpaired_quantity == D("0")
    assert hedged.taker_hedge_quantity == D("5")
    assert hedged.fees_paid > D("0")
    assert unwound.unpaired_quantity == D("0")
    assert unwound.fak_unwind_quantity == D("5")
    assert unwound.net_pnl < D("0")


def test_report_builder_emits_complete_3x3_matrix_and_neutral_evidence(
    tmp_path: Path,
) -> None:
    plans, events_by_attempt, hedge_books_by_attempt = _robust_report_inputs()

    report = build_structural_shadow_report(
        tuple(plans),
        locked_zero_cohorts=(),
        capture_verification_by_attempt=_capture_verifications(plans),
        public_events_by_attempt=events_by_attempt,
        neutral_disposition=SingleLegDisposition.PASSIVE_WAIT,
        rolling_window_size=20,
        minimum_expiries=200,
        rolling_audit=_rolling_audit(
            plans,
            {
                plan.attempt_id: hashlib.sha256(
                    plan.attempt_id.encode("utf-8")
                ).hexdigest()
                for plan in plans
            },
            {
                plan.attempt_id: hashlib.sha256(
                    f"locked:{plan.attempt_id}".encode()
                ).hexdigest()
                for plan in plans
            },
        ),
        source_input_sha256="e" * 64,
        preregistration_sha256=PREREGISTRATION_SHA256,
        locked_action_payload_sha256_by_attempt={
            plan.attempt_id: hashlib.sha256(
                f"locked:{plan.attempt_id}".encode()
            ).hexdigest()
            for plan in plans
        },
        source_evidence_sha256_by_attempt={
            plan.attempt_id: hashlib.sha256(plan.attempt_id.encode("utf-8")).hexdigest()
            for plan in plans
        },
        verified_finalized_bundle=_verified_bundle(plans),
        hedge_books_by_attempt=hedge_books_by_attempt,
    )

    assert len(report.attempts) == 200
    first_attempt = report.attempts[0]
    assert isinstance(first_attempt, StructuralShadowAttemptReport)
    assert len(first_attempt.results) == 9
    assert first_attempt.result_for(
        scenario=QueueScenario.NEUTRAL,
        disposition=SingleLegDisposition.PASSIVE_WAIT,
    ).net_pnl == D("0.10")
    assert report.neutral_robustness.passed is True
    assert report.neutral_shadow_evidence.expiry_count == 200
    assert (
        report.neutral_shadow_evidence.realized_net_pnl
        == report.neutral_robustness.total_net_pnl
    )
    assert report.neutral_shadow_evidence.realized_net_pnl > D("0")
    assert report.neutral_shadow_evidence.receipt_chain_verified is True


def test_direct_low_level_build_stays_unverified_and_probe_readiness_rejects(
    tmp_path: Path,
) -> None:
    plans, events_by_attempt, hedge_books_by_attempt = _robust_report_inputs()
    evidence_hashes = {
        plan.attempt_id: hashlib.sha256(plan.attempt_id.encode("utf-8")).hexdigest()
        for plan in plans
    }
    locked_hashes = {
        plan.attempt_id: hashlib.sha256(
            f"locked:{plan.attempt_id}".encode()
        ).hexdigest()
        for plan in plans
    }
    report = build_structural_shadow_report(
        tuple(plans),
        locked_zero_cohorts=(),
        capture_verification_by_attempt=_capture_verifications(plans),
        public_events_by_attempt=events_by_attempt,
        neutral_disposition=SingleLegDisposition.PASSIVE_WAIT,
        rolling_window_size=20,
        minimum_expiries=200,
        rolling_audit=_rolling_audit(plans, evidence_hashes, locked_hashes),
        source_input_sha256="e" * 64,
        preregistration_sha256=PREREGISTRATION_SHA256,
        locked_action_payload_sha256_by_attempt=locked_hashes,
        source_evidence_sha256_by_attempt=evidence_hashes,
        hedge_books_by_attempt=hedge_books_by_attempt,
    )

    assert report.receipt_chain_verified is False
    assert report.neutral_shadow_evidence.receipt_chain_verified is False
    floor = validate_structural_floor(
        pair=SameExpiryPair.from_contracts(
            plans[0]._contract_for_horizon("5m"),
            plans[0]._contract_for_horizon("15m"),
        ),
        settlement_state=plans[0].settlement_state,
        books=_books(),
        pricing_policy=PairPricingPolicy(
            pair_risk_usdc=D("5"),
            structural_only=True,
        ),
        execution_mode=PairExecutionMode.MAKER_MAKER,
    )
    readiness = evaluate_execution_probe_readiness(
        structural_shadow_report=report,
        clean_common_terminal_cohort_count=4,
        coverage_results=tuple(
            CohortCoverageResult(
                cohort_id=f"probe-{index}",
                complete=True,
                reason_codes=(),
            )
            for index in range(4)
        ),
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
            verified_evidence_bundle=_probe_evidence_bundle(
                tmp_path,
                structural_shadow_document=report.to_document(),
            ),
        ),
    )

    assert readiness.eligible is False
    assert {
        "neutral_shadow_builder_authority_hash_invalid",
        "gate_0_not_passed",
    } & set(readiness.reason_codes)


def test_shadow_robustness_removes_best_expiry_direction_and_rolling_window() -> None:
    records = tuple(
        StructuralShadowResult.for_robustness(
            attempt_id=f"expiry-{index}",
            expiry_ms=index,
            direction=(
                StructuralDirection.FIVE_ABOVE_FIFTEEN
                if index % 2
                else StructuralDirection.FIVE_BELOW_FIFTEEN
            ),
            net_pnl=pnl,
        )
        for index, pnl in enumerate(
            (D("1"), D("1"), D("1"), D("1"), D("1")),
            start=1,
        )
    )

    verdict = evaluate_shadow_robustness(
        records,
        rolling_window_size=2,
        minimum_expiries=5,
    )

    assert verdict.total_net_pnl == D("5")
    assert verdict.without_best_expiry_net_pnl == D("4")
    assert verdict.without_best_direction_net_pnl == D("2")
    assert verdict.minimum_rolling_window_net_pnl == D("2")
    assert verdict.max_single_expiry_concentration == D("0.2")
    assert verdict.passed is True


def test_double_maker_plan_rejects_a_direction_label_that_conflicts_with_legs() -> None:
    pair = _v07_pair()
    with pytest.raises(ValueError, match="structural direction conflicts"):
        build_structural_double_maker_plan(
            attempt_id="conflicting-direction",
            pair=pair,
            settlement_state=_settlement_state(
                pair,
                StructuralDirection.FIVE_BELOW_FIFTEEN,
            ),
            books=_books(),
            action=PairAction.LONG_15_UP_LONG_5_DOWN,
            quantity=D("5"),
            submitted_at_ms=0,
            actual_5_up=True,
            actual_15_up=True,
            max_book_age_ms=1_000,
            initial_cash=D("20"),
        )


def test_taker_disposition_does_not_mutate_captured_depth_evidence() -> None:
    plan = _plan()
    first_only = (_trade(plan.first.token_id, source="trade-15"),)
    hedge_book = DepthBook.from_tuples(
        plan.second.token_id,
        bids=((D("0.58"), D("20")),),
        asks=((D("0.60"), D("20")),),
        tick_size=D("0.01"),
        min_order_size=D("5"),
        timestamp_ms=100,
    )

    first = replay_structural_double_maker(
        plan,
        public_events=first_only,
        disposition=SingleLegDisposition.TAKER_HEDGE,
        hedge_books={plan.second.token_id: hedge_book},
    )
    second = replay_structural_double_maker(
        plan,
        public_events=first_only,
        disposition=SingleLegDisposition.TAKER_HEDGE,
        hedge_books={plan.second.token_id: hedge_book},
    )

    assert first == second
    assert hedge_book.asks[0].size == D("20")


def test_robustness_requires_unique_common_expiries_and_typed_results() -> None:
    duplicate_expiry = (
        StructuralShadowResult.for_robustness(
            attempt_id="a",
            expiry_ms=1,
            direction="five_above_fifteen",
            net_pnl=D("1"),
        ),
        StructuralShadowResult.for_robustness(
            attempt_id="b",
            expiry_ms=1,
            direction="five_below_fifteen",
            net_pnl=D("1"),
        ),
    )
    with pytest.raises(ValueError, match="unique common expiries"):
        evaluate_shadow_robustness(
            duplicate_expiry,
            rolling_window_size=1,
            minimum_expiries=2,
        )
    with pytest.raises(TypeError, match="StructuralShadowResult"):
        evaluate_shadow_robustness(
            (object(),),  # type: ignore[arg-type]
            rolling_window_size=1,
            minimum_expiries=1,
        )


def test_report_builder_fails_closed_on_duplicate_expiry() -> None:
    plan = _plan()
    duplicate = replace(plan, attempt_id="expiry-2")
    plans = (plan, duplicate)
    hashes = {
        item.attempt_id: hashlib.sha256(item.attempt_id.encode("utf-8")).hexdigest()
        for item in plans
    }
    with pytest.raises(ValueError, match="unique common expiries"):
        build_structural_shadow_report(
            plans,
            locked_zero_cohorts=(),
            capture_verification_by_attempt=_capture_verifications(plans),
            public_events_by_attempt={
                plan.attempt_id: (),
                duplicate.attempt_id: (),
            },
            neutral_disposition=SingleLegDisposition.PASSIVE_WAIT,
            rolling_window_size=1,
            minimum_expiries=2,
            rolling_audit=_rolling_audit(
                plans,
                hashes,
                {
                    item.attempt_id: hashlib.sha256(
                        f"locked:{item.attempt_id}".encode()
                    ).hexdigest()
                    for item in plans
                },
                realized_net_pnl="0",
            ),
            source_input_sha256="e" * 64,
            preregistration_sha256=PREREGISTRATION_SHA256,
            locked_action_payload_sha256_by_attempt={
                item.attempt_id: hashlib.sha256(
                    f"locked:{item.attempt_id}".encode()
                ).hexdigest()
                for item in plans
            },
            source_evidence_sha256_by_attempt=hashes,
        )


def test_locked_no_trade_stays_in_denominator_and_dirty_pnl_is_rejected() -> None:
    plan = _plan()
    plan_hash = hashlib.sha256(b"plan-evidence").hexdigest()
    plan_action_hash = hashlib.sha256(b"plan-action").hexdigest()
    zero_hash = hashlib.sha256(b"zero-evidence").hexdigest()
    zero_action_hash = hashlib.sha256(b"zero-action").hexdigest()
    no_trade = LockedZeroCohort(
        attempt_id="expiry-zero",
        expiry_ms=plan.expiry_ms + 1,
        outcome="no_trade",
        clean=True,
        capture_verification=_capture_verification("expiry-zero"),
        source_evidence_sha256=zero_hash,
        decision_at_ms=0,
        locked_action_payload_sha256=zero_action_hash,
    )
    audit = _rolling_audit(
        (plan,),
        {plan.attempt_id: plan_hash},
        {plan.attempt_id: plan_action_hash},
        realized_net_pnl="0",
    )
    audit["admissions"][no_trade.attempt_id] = {  # type: ignore[index]
        "common_expiry_id": no_trade.attempt_id,
        "expiry_ms": no_trade.expiry_ms,
    }
    audit["decisions"][f"{no_trade.attempt_id}:60"] = {  # type: ignore[index]
        "common_expiry_id": no_trade.attempt_id,
        "action": "no_trade",
        "decision_at_ms": 0,
        "action_payload_sha256": zero_action_hash,
    }
    audit["outcomes"][no_trade.attempt_id] = {  # type: ignore[index]
        "common_expiry_id": no_trade.attempt_id,
        "outcome": "no_trade",
        "clean": True,
        "realized_net_pnl": "0",
        "evidence_sha256": zero_hash,
    }
    audit["admitted_common_expiry_count"] = 2
    audit["finalized_common_expiry_count"] = 2

    report = build_structural_shadow_report(
        (plan,),
        locked_zero_cohorts=(no_trade,),
        capture_verification_by_attempt=_capture_verifications((plan,)),
        public_events_by_attempt={plan.attempt_id: ()},
        neutral_disposition=SingleLegDisposition.PASSIVE_WAIT,
        rolling_window_size=20,
        minimum_expiries=200,
        rolling_audit=audit,
        source_input_sha256="e" * 64,
        preregistration_sha256=PREREGISTRATION_SHA256,
        locked_action_payload_sha256_by_attempt={
            plan.attempt_id: plan_action_hash,
            no_trade.attempt_id: zero_action_hash,
        },
        source_evidence_sha256_by_attempt={
            plan.attempt_id: plan_hash,
            no_trade.attempt_id: zero_hash,
        },
        verified_finalized_bundle=_verified_bundle(
            (plan,),
            locked_zero_cohorts=(no_trade,),
            capture_verifications=_capture_verifications((plan,)),
            evidence_hashes={plan.attempt_id: plan_hash},
            locked_action_hashes={
                plan.attempt_id: plan_action_hash,
                no_trade.attempt_id: zero_action_hash,
            },
            audit=audit,
        ),
    )

    assert report.neutral_shadow_evidence.expiry_count == 2
    assert report.locked_zero_cohorts == (no_trade,)
    assert report.to_document()["locked_zero_cohort_count"] == 1

    dirty = LockedZeroCohort(
        attempt_id="expiry-dirty",
        expiry_ms=plan.expiry_ms + 2,
        outcome="capture_dirty",
        clean=False,
        capture_verification=_capture_verification("expiry-dirty"),
        source_evidence_sha256="d" * 64,
        dirty_reason_code="verified_capture_anomaly",
    )
    dirty_audit = {
        "schema_version": "btc-5m-15m-readiness-v08-rolling-audit.v1",
        "journal_root": "test-journal",
        "valid": True,
        "errors": [],
        "policy": {"preregistration_sha256": PREREGISTRATION_SHA256},
        "admitted_common_expiry_count": 1,
        "finalized_common_expiry_count": 1,
        "unfinished_common_expiry_count": 0,
        "admissions": {
            dirty.attempt_id: {
                "common_expiry_id": dirty.attempt_id,
                "expiry_ms": dirty.expiry_ms,
            }
        },
        "decisions": {},
        "outcomes": {
            dirty.attempt_id: {
                "common_expiry_id": dirty.attempt_id,
                "outcome": dirty.outcome,
                "clean": False,
                "realized_net_pnl": "0.10",
                "evidence_sha256": dirty.source_evidence_sha256,
            }
        },
        "denominator_includes_no_trade_no_fill_and_dirty": True,
    }
    with pytest.raises(ValueError, match="cannot contribute PnL"):
        build_structural_shadow_report(
            (),
            locked_zero_cohorts=(dirty,),
            capture_verification_by_attempt={},
            public_events_by_attempt={},
            neutral_disposition=SingleLegDisposition.PASSIVE_WAIT,
            rolling_window_size=20,
            minimum_expiries=200,
            rolling_audit=dirty_audit,
            source_input_sha256="e" * 64,
            preregistration_sha256=PREREGISTRATION_SHA256,
            locked_action_payload_sha256_by_attempt={},
            source_evidence_sha256_by_attempt={dirty.attempt_id: "d" * 64},
        )


def test_dirty_zero_cohort_rejects_any_decision_receipt() -> None:
    plan = _plan()
    dirty = LockedZeroCohort(
        attempt_id="expiry-dirty-decision",
        expiry_ms=plan.expiry_ms + 3,
        outcome="capture_dirty",
        clean=False,
        capture_verification=_capture_verification("expiry-dirty-decision"),
        source_evidence_sha256="c" * 64,
        dirty_reason_code="verified_capture_anomaly",
    )
    audit = {
        "schema_version": "btc-5m-15m-readiness-v08-rolling-audit.v1",
        "journal_root": "test-journal",
        "valid": True,
        "errors": [],
        "policy": {"preregistration_sha256": PREREGISTRATION_SHA256},
        "admitted_common_expiry_count": 1,
        "finalized_common_expiry_count": 1,
        "unfinished_common_expiry_count": 0,
        "admissions": {
            dirty.attempt_id: {
                "common_expiry_id": dirty.attempt_id,
                "expiry_ms": dirty.expiry_ms,
            }
        },
        "decisions": {
            f"{dirty.attempt_id}:decision": {
                "common_expiry_id": dirty.attempt_id,
                "action": "no_trade",
                "decision_at_ms": 0,
                "action_payload_sha256": "d" * 64,
            }
        },
        "outcomes": {
            dirty.attempt_id: {
                "common_expiry_id": dirty.attempt_id,
                "outcome": dirty.outcome,
                "clean": False,
                "realized_net_pnl": "0",
                "evidence_sha256": dirty.source_evidence_sha256,
            }
        },
        "denominator_includes_no_trade_no_fill_and_dirty": True,
    }

    with pytest.raises(ValueError, match="decision/action receipts"):
        build_structural_shadow_report(
            (),
            locked_zero_cohorts=(dirty,),
            capture_verification_by_attempt={},
            public_events_by_attempt={},
            neutral_disposition=SingleLegDisposition.PASSIVE_WAIT,
            rolling_window_size=20,
            minimum_expiries=200,
            rolling_audit=audit,
            source_input_sha256="e" * 64,
            preregistration_sha256=PREREGISTRATION_SHA256,
            locked_action_payload_sha256_by_attempt={},
            source_evidence_sha256_by_attempt={dirty.attempt_id: "c" * 64},
        )


def test_supporting_capture_root_cannot_serve_as_another_primary() -> None:
    plan = _plan()
    shared_root = "/captures/shared-history"
    capture_map = {
        plan.attempt_id: replace(
            _capture_verification(plan.attempt_id),
            supporting_capture_roots=(shared_root,),
            supporting_capture_tree_sha256=("a" * 64,),
            supporting_manifest_count=1,
            supporting_record_count=1,
        )
    }
    dirty = LockedZeroCohort(
        attempt_id="expiry-overlap",
        expiry_ms=plan.expiry_ms + 4,
        outcome="capture_dirty",
        clean=False,
        capture_verification=replace(
            _capture_verification("expiry-overlap"),
            capture_root=shared_root,
        ),
        source_evidence_sha256="d" * 64,
        dirty_reason_code="verified_capture_anomaly",
    )
    audit = {
        "schema_version": "btc-5m-15m-readiness-v08-rolling-audit.v1",
        "journal_root": "test-journal",
        "valid": True,
        "errors": [],
        "policy": {"preregistration_sha256": PREREGISTRATION_SHA256},
        "admitted_common_expiry_count": 2,
        "finalized_common_expiry_count": 2,
        "unfinished_common_expiry_count": 0,
        "admissions": {
            plan.attempt_id: {
                "common_expiry_id": plan.attempt_id,
                "capture_attempt_id": capture_map[plan.attempt_id].capture_attempt_id,
                "expiry_ms": plan.expiry_ms,
                "market_5_id": plan._contract_for_horizon("5m").market_id,
                "market_15_id": plan._contract_for_horizon("15m").market_id,
                "condition_5_id": plan._contract_for_horizon("5m").condition_id,
                "condition_15_id": plan._contract_for_horizon("15m").condition_id,
            },
            dirty.attempt_id: {
                "common_expiry_id": dirty.attempt_id,
                "expiry_ms": dirty.expiry_ms,
            },
        },
        "decisions": {
            f"{plan.attempt_id}:60": {
                "common_expiry_id": plan.attempt_id,
                "action": plan.to_document()["action"],
                "decision_at_ms": plan.submitted_at_ms,
                "action_payload_sha256": "b" * 64,
            }
        },
        "outcomes": {
            plan.attempt_id: {
                "common_expiry_id": plan.attempt_id,
                "outcome": "complete",
                "clean": True,
                "realized_net_pnl": "0",
                "evidence_sha256": "c" * 64,
            },
            dirty.attempt_id: {
                "common_expiry_id": dirty.attempt_id,
                "outcome": dirty.outcome,
                "clean": False,
                "realized_net_pnl": "0",
                "evidence_sha256": dirty.source_evidence_sha256,
            },
        },
        "denominator_includes_no_trade_no_fill_and_dirty": True,
    }

    with pytest.raises(ValueError, match="supporting capture roots cannot also serve"):
        build_structural_shadow_report(
            (plan,),
            locked_zero_cohorts=(dirty,),
            capture_verification_by_attempt=capture_map,
            public_events_by_attempt={plan.attempt_id: ()},
            neutral_disposition=SingleLegDisposition.PASSIVE_WAIT,
            rolling_window_size=20,
            minimum_expiries=200,
            rolling_audit=audit,
            source_input_sha256="e" * 64,
            preregistration_sha256=PREREGISTRATION_SHA256,
            locked_action_payload_sha256_by_attempt={plan.attempt_id: "b" * 64},
            source_evidence_sha256_by_attempt={
                plan.attempt_id: "c" * 64,
                dirty.attempt_id: dirty.source_evidence_sha256,
            },
        )
