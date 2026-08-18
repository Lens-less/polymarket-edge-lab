from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_btc_twap_executable_upper_bound as gate0
from scripts import build_btc_twap_relative_value_v07_counterfactual as builder
from src.edge_lab.btc_twap_relative_value import OrderBookSnapshot, SameExpiryPair
from src.edge_lab.data_store import canonical_json_bytes
from src.edge_lab.execution import ExecutionFeeSchedule
from tests.test_btc_twap_relative_value_v07_counterfactual import (
    PREREGISTRATION_PATH,
    _build_case,
)
from tests.test_btc_twap_relative_value_v07_replay import (
    _settlement_state,
    _v07_pair,
)

D = Decimal


def _manifest(tmp_path: Path) -> Path:
    case, _manifest_dir = _build_case(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": builder.MANIFEST_SCHEMA_VERSION,
                "preregistration_path": str(PREREGISTRATION_PATH),
                "cases": [case],
            }
        )
        + b"\n"
    )
    return path


def test_gate_zero_builder_emits_one_depth_ladder_per_common_expiry(
    tmp_path: Path,
) -> None:
    report = gate0.build_upper_bound_report(
        manifest_path=_manifest(tmp_path),
        decision_tau_seconds=60,
        expected_clean_attempts=1,
    )

    assert report["schema_version"] == gate0.REPORT_SCHEMA
    assert report["observed_unique_common_expiry_attempts"] == 1
    assert report["diagnostic"]["attempt_count"] == 1
    assert report["diagnostic"]["attempts"][0]["attempt_id"]
    assert report["diagnostic"]["counts_as_locked_oos_evidence"] is False
    assert report["diagnostic"]["gate_0_route"] == "structural_floor_only"
    assert report["policy"]["quantity_risk_cap_usdc"] is None
    assert report["policy"]["quantity_scope"] == (
        "all_captured_joint_depth_breakpoints"
    )
    assert report["authority"]["strict_parameters_match"] is False
    assert report["policy"]["can_authorize_live"] is False
    assert report["safety"]["orders_submitted"] == 0
    assert report["report_sha256"]


def test_gate_zero_builder_refuses_to_silently_misstate_41_attempts(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="expected 41.*observed 1"):
        gate0.build_upper_bound_report(
            manifest_path=_manifest(tmp_path),
            decision_tau_seconds=60,
            expected_clean_attempts=41,
        )


def test_gate_zero_cli_defaults_to_the_full_five_minute_scan() -> None:
    args = gate0.build_parser().parse_args(
        [
            "--manifest",
            "manifest.json",
            "--output",
            "report.json",
            "--expected-clean-attempts",
            "41",
        ]
    )

    assert args.decision_tau_seconds is None
    assert args.expected_clean_attempts == 41


def test_gate_zero_cli_rejects_formal_tau_override_and_count_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _manifest(tmp_path)
    monkeypatch.setattr(
        gate0,
        "_load_gate_0_contract",
        lambda preregistration_path: {
            "track_id": "btc_5m_15m_edge_readiness_v08_2026_08_18",
            "preregistration_path": str(preregistration_path),
            "preregistration_sha256": "b" * 64,
            "expected_clean_attempts": 41,
            "scan_ttc_seconds_inclusive": [300, 0],
            "candidate_ttc_count": 301,
            "decision_execution_mode": "maker_maker",
            "minimum_average_best_total_pnl_per_expiry_usdc": "0.5",
            "incomplete_existing_41_evidence_action": "rerun_required_not_stop",
        },
    )

    with pytest.raises(ValueError, match="full preregistered 300→0 window"):
        gate0.main(
            [
                "--manifest",
                str(manifest_path),
                "--output",
                str(tmp_path / "report.json"),
                "--expected-clean-attempts",
                "41",
                "--decision-tau-seconds",
                "60",
            ]
        )

    with pytest.raises(ValueError, match="must use the preregistered clean-attempt count"):
        gate0.main(
            [
                "--manifest",
                str(manifest_path),
                "--output",
                str(tmp_path / "report.json"),
                "--expected-clean-attempts",
                "1",
            ]
        )


def test_split_probability_curve_keeps_one_sided_extreme_observations() -> None:
    pair = _v07_pair()
    settlement = replace(
        _settlement_state(pair),
        strike_5=D("101"),
        strike_15=D("100"),
    )
    context = SimpleNamespace(pair=pair, settlement_state=settlement)
    books = {
        pair.market_15.up_token_id: OrderBookSnapshot.from_tuples(
            pair.market_15.up_token_id,
            bids=((D("0.999"), D("10")),),
            asks=(),
            timestamp_ms=1_000,
            tick_size=D("0.001"),
            minimum_order_size=D("5"),
        ),
        pair.market_5.down_token_id: OrderBookSnapshot.from_tuples(
            pair.market_5.down_token_id,
            bids=((D("0.001"), D("10")),),
            asks=(),
            timestamp_ms=1_000,
            tick_size=D("0.001"),
            minimum_order_size=D("5"),
        ),
    }

    point = gate0._implicit_split_probability_diagnostic(context, books)

    assert point == {
        "b": None,
        "bid_side_sum_minus_one": D("0.000"),
        "ask_side_sum_minus_one": None,
        "unavailable_reason": "two_sided_midpoint_unavailable",
    }


def test_gate_zero_builder_scans_all_taus_and_keeps_best_attempt_per_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pair = _v07_pair()
    fee_exempt = ExecutionFeeSchedule.fee_exempt(
        reason="gate0-best-tau-test",
        source_ref="fixture://gate0-best-tau",
    )
    pair = SameExpiryPair.from_contracts(
        replace(
            pair.market_5,
            fee_schedule=fee_exempt,
            tick_size=D("0.001"),
        ),
        replace(
            pair.market_15,
            fee_schedule=fee_exempt,
            tick_size=D("0.001"),
        ),
    )
    settlement = replace(
        _settlement_state(pair),
        market_5_rule_hash=pair.market_5.rule_hash,
        market_15_rule_hash=pair.market_15.rule_hash,
        strike_5=D("101"),
        strike_15=D("100"),
    )

    def _context(tau: int, ask_15_up: str, ask_5_down: str) -> SimpleNamespace:
        local_decision_at_ms = 1_786_700_400_000 - tau * 1_000
        books = {
            pair.market_15.up_token_id: OrderBookSnapshot.from_tuples(
                pair.market_15.up_token_id,
                bids=((D(ask_15_up) - D("0.001"), D("10")),),
                asks=((D(ask_15_up), D("10")),),
                timestamp_ms=1_000,
                tick_size=D("0.001"),
                minimum_order_size=D("5"),
            ),
            pair.market_5.down_token_id: OrderBookSnapshot.from_tuples(
                pair.market_5.down_token_id,
                bids=((D(ask_5_down) - D("0.001"), D("10")),),
                asks=((D(ask_5_down), D("10")),),
                timestamp_ms=1_000,
                tick_size=D("0.001"),
                minimum_order_size=D("5"),
            ),
            pair.market_5.up_token_id: OrderBookSnapshot.from_tuples(
                pair.market_5.up_token_id,
                bids=((D("0.50"), D("10")),),
                asks=((D("0.501"), D("10")),),
                timestamp_ms=1_000,
                tick_size=D("0.001"),
                minimum_order_size=D("5"),
            ),
            pair.market_15.down_token_id: OrderBookSnapshot.from_tuples(
                pair.market_15.down_token_id,
                bids=((D("0.50"), D("10")),),
                asks=((D("0.501"), D("10")),),
                timestamp_ms=1_000,
                tick_size=D("0.001"),
                minimum_order_size=D("5"),
            ),
        }
        requested_decision_times: list[int] = []

        def signal_books(
            *,
            token_ids,
            decision_at_ms,
            maximum_age_ms,
            require_full_depth,
        ):
            requested_decision_times.append(decision_at_ms)
            return {
                token_id: SimpleNamespace(snapshot=books[token_id])
                for token_id in token_ids
            }

        replay = SimpleNamespace(signal_books=signal_books)
        return SimpleNamespace(
            decision_tau_seconds=tau,
            expiry_ms=1_786_700_400_000,
            expiry_cluster_id="expiry-1",
            canonical_pair_id="pair-1",
            pair=pair,
            settlement_state=settlement,
            replay=replay,
            decision_at_ms=local_decision_at_ms,
            actual_5_up=True,
            actual_15_up=True,
            requested_decision_times=requested_decision_times,
            future_public_trades_by_token_id={
                pair.market_15.up_token_id: (
                    {
                        "token_id": pair.market_15.up_token_id,
                        "source_event_id": f"trade-{tau}-15up",
                        "timestamp_ms": local_decision_at_ms + 1,
                        "aggressor_side": "SELL",
                        "price": str(D(ask_15_up) - D("0.001")),
                        "quantity": "10",
                    },
                ),
                pair.market_5.down_token_id: (
                    {
                        "token_id": pair.market_5.down_token_id,
                        "source_event_id": f"trade-{tau}-5down",
                        "timestamp_ms": local_decision_at_ms + 2,
                        "aggressor_side": "SELL",
                        "price": str(D(ask_5_down) - D("0.001")),
                        "quantity": "10",
                    },
                ),
                pair.market_5.up_token_id: (),
                pair.market_15.down_token_id: (),
            },
        )

    strategy = SimpleNamespace(
        maximum_spread_each_leg=D("0.03"),
        minimum_market_price=D("0.05"),
        maximum_market_price=D("0.95"),
        maximum_book_staleness_ms=1_000,
    )
    contexts = (
        _context(240, "0.90", "0.15"),
        _context(60, "0.99", "0.01"),
    )
    monkeypatch.setattr(
        gate0,
        "_load_contexts",
        lambda manifest_path: (
            contexts,
            strategy,
            tmp_path / "prereg.json",
        ),
    )
    monkeypatch.setattr(
        gate0,
        "_load_gate_0_contract",
        lambda preregistration_path: {
            "track_id": "btc_5m_15m_edge_readiness_v08_2026_08_18",
            "preregistration_path": str(preregistration_path),
            "preregistration_sha256": "b" * 64,
            "expected_clean_attempts": 41,
            "scan_ttc_seconds_inclusive": [300, 0],
            "candidate_ttc_count": 301,
            "decision_execution_mode": "maker_maker",
            "minimum_average_best_total_pnl_per_expiry_usdc": "0.5",
            "incomplete_existing_41_evidence_action": "rerun_required_not_stop",
        },
    )
    monkeypatch.setattr(gate0, "_sha256", lambda path: "a" * 64)

    report = gate0.build_upper_bound_report(
        manifest_path=tmp_path / "manifest.json",
        decision_tau_seconds=None,
        expected_clean_attempts=1,
    )

    assert report["observed_unique_common_expiry_attempts"] == 1
    assert report["decision_tau_seconds"] is None
    assert report["selected_decision_tau_seconds_by_expiry"] == {"expiry-1": 60}
    assert report["diagnostic"]["attempt_count"] == 1
    assert D(report["diagnostic"]["attempts"][0]["best_total_pnl"]) == D("0.020")
    assert set(report["execution_modes"]) == {
        "taker_taker",
        "maker_taker",
        "maker_maker",
    }
    maker_maker = report["execution_modes"]["maker_maker"]
    assert maker_maker["attempts"][0]["best_ttc_seconds"] == 60
    assert D(maker_maker["attempts"][0]["best_net_floor_per_pair"]) > D("0")
    assert D(maker_maker["average_best_total_pnl_per_expiry"]) == D("0.020")
    assert maker_maker["gate_0_passed"] is False
    assert maker_maker["attempts"][0]["fill_volume_bound_source"] == (
        "context.future_public_trades_by_token_id"
    )
    assert report["split_probability_diagnostics"]["negative_b_observation_count"] >= 1
    requested = {
        timestamp
        for context in contexts
        for timestamp in context.requested_decision_times
    }
    assert contexts[0].expiry_ms - 300_000 in requested
    assert contexts[0].expiry_ms in requested
    assert report["scan"]["ttc_seconds_start"] == 300
    assert report["scan"]["ttc_seconds_end"] == 0
    assert report["scan"]["candidate_ttc_count"] == 301
    assert report["scan"]["all_expiries_complete"] is True
    assert maker_maker["incomplete_scan_expiry_ids"] == []
    assert maker_maker["required_observation_count_per_expiry"] == 301
    assert report["authority"]["strict_parameters_match"] is False

    token_ids = (
        pair.market_5.up_token_id,
        pair.market_5.down_token_id,
        pair.market_15.up_token_id,
        pair.market_15.down_token_id,
    )
    captured = contexts[0].replay.signal_books(
        token_ids=token_ids,
        decision_at_ms=contexts[0].decision_at_ms,
        maximum_age_ms=1_000,
        require_full_depth=True,
    )
    incomplete_modes, _diagnostics = gate0._execution_mode_diagnostics(
        observations_by_expiry={
            "expiry-1": (
                (
                    contexts[0],
                    {token_id: captured[token_id].snapshot for token_id in token_ids},
                    {
                        pair.market_15.up_token_id: D("10"),
                        pair.market_5.down_token_id: D("10"),
                    },
                    "context.future_public_trades_by_token_id",
                ),
            )
        },
        required_observation_count_per_expiry=2,
    )
    incomplete = incomplete_modes["maker_maker"]
    assert incomplete["evidence_complete"] is False
    assert incomplete["decision"] == "RERUN_REQUIRED"
    assert incomplete["stop_recommended"] is False


def test_gate_zero_maker_modes_treat_precomputed_fill_caps_as_diagnostic_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pair = _v07_pair()
    context = SimpleNamespace(
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("101"),
            strike_15=D("100"),
        ),
        decision_tau_seconds=60,
        expiry_ms=1_786_700_400_000,
        expiry_cluster_id="expiry-1",
        canonical_pair_id="pair-1",
        decision_at_ms=1_000,
        actual_5_up=True,
        actual_15_up=True,
        replay=SimpleNamespace(
            signal_books=lambda **kwargs: {
                pair.market_15.up_token_id: SimpleNamespace(
                    snapshot=OrderBookSnapshot.from_tuples(
                        pair.market_15.up_token_id,
                        bids=((D("0.99"), D("10")),),
                        asks=((D("0.991"), D("10")),),
                        timestamp_ms=1_000,
                        tick_size=D("0.001"),
                        minimum_order_size=D("5"),
                    )
                ),
                pair.market_5.down_token_id: SimpleNamespace(
                    snapshot=OrderBookSnapshot.from_tuples(
                        pair.market_5.down_token_id,
                        bids=((D("0.01"), D("10")),),
                        asks=((D("0.011"), D("10")),),
                        timestamp_ms=1_000,
                        tick_size=D("0.001"),
                        minimum_order_size=D("5"),
                    )
                ),
                pair.market_5.up_token_id: SimpleNamespace(
                    snapshot=OrderBookSnapshot.from_tuples(
                        pair.market_5.up_token_id,
                        bids=((D("0.50"), D("10")),),
                        asks=((D("0.501"), D("10")),),
                        timestamp_ms=1_000,
                        tick_size=D("0.001"),
                        minimum_order_size=D("5"),
                    )
                ),
                pair.market_15.down_token_id: SimpleNamespace(
                    snapshot=OrderBookSnapshot.from_tuples(
                        pair.market_15.down_token_id,
                        bids=((D("0.50"), D("10")),),
                        asks=((D("0.501"), D("10")),),
                        timestamp_ms=1_000,
                        tick_size=D("0.001"),
                        minimum_order_size=D("5"),
                    )
                ),
            }
        ),
        future_public_trade_caps_by_token_id={
            pair.market_15.up_token_id: D("10"),
            pair.market_5.down_token_id: D("10"),
        },
    )
    monkeypatch.setattr(
        gate0,
        "_load_contexts",
        lambda manifest_path: (
            (context,),
            SimpleNamespace(
                maximum_spread_each_leg=D("0.03"),
                minimum_market_price=D("0.05"),
                maximum_market_price=D("0.95"),
                maximum_book_staleness_ms=1_000,
            ),
            tmp_path / "counterfactual-prereg.json",
        ),
    )
    monkeypatch.setattr(gate0, "_sha256", lambda path: "a" * 64)

    report = gate0.build_upper_bound_report(
        manifest_path=tmp_path / "manifest.json",
        decision_tau_seconds=None,
        expected_clean_attempts=1,
    )

    assert report["decision"] == "RERUN_REQUIRED"
    assert report["authority"]["strict_parameters_match"] is False


def test_gate_zero_empty_but_complete_trade_tape_yields_formal_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pair = _v07_pair()
    fee_exempt = ExecutionFeeSchedule.fee_exempt(
        reason="gate0-empty-tape",
        source_ref="fixture://gate0-empty-tape",
    )
    pair = SameExpiryPair.from_contracts(
        replace(pair.market_5, fee_schedule=fee_exempt, tick_size=D("0.001")),
        replace(pair.market_15, fee_schedule=fee_exempt, tick_size=D("0.001")),
    )
    settlement = replace(
        _settlement_state(pair),
        market_5_rule_hash=pair.market_5.rule_hash,
        market_15_rule_hash=pair.market_15.rule_hash,
        strike_5=D("101"),
        strike_15=D("100"),
    )
    expiry_ms = 1_786_700_400_000
    decision_at_ms = expiry_ms - 60_000
    books = {
        pair.market_15.up_token_id: OrderBookSnapshot.from_tuples(
            pair.market_15.up_token_id,
            bids=((D("0.999"), D("10")),),
            asks=((D("1.000"), D("10")),),
            timestamp_ms=1_000,
            tick_size=D("0.001"),
            minimum_order_size=D("5"),
        ),
        pair.market_5.down_token_id: OrderBookSnapshot.from_tuples(
            pair.market_5.down_token_id,
            bids=((D("0.001"), D("10")),),
            asks=((D("0.002"), D("10")),),
            timestamp_ms=1_000,
            tick_size=D("0.001"),
            minimum_order_size=D("5"),
        ),
        pair.market_5.up_token_id: OrderBookSnapshot.from_tuples(
            pair.market_5.up_token_id,
            bids=((D("0.500"), D("10")),),
            asks=((D("0.501"), D("10")),),
            timestamp_ms=1_000,
            tick_size=D("0.001"),
            minimum_order_size=D("5"),
        ),
        pair.market_15.down_token_id: OrderBookSnapshot.from_tuples(
            pair.market_15.down_token_id,
            bids=((D("0.500"), D("10")),),
            asks=((D("0.501"), D("10")),),
            timestamp_ms=1_000,
            tick_size=D("0.001"),
            minimum_order_size=D("5"),
        ),
    }
    context = SimpleNamespace(
        decision_tau_seconds=60,
        expiry_ms=expiry_ms,
        expiry_cluster_id="expiry-1",
        canonical_pair_id="pair-1",
        pair=pair,
        settlement_state=settlement,
        replay=SimpleNamespace(
            signal_books=lambda **kwargs: {
                token_id: SimpleNamespace(snapshot=books[token_id])
                for token_id in kwargs["token_ids"]
            }
        ),
        decision_at_ms=decision_at_ms,
        actual_5_up=True,
        actual_15_up=True,
        future_public_trades_by_token_id={
            token_id: ()
            for token_id in (
                pair.market_5.up_token_id,
                pair.market_5.down_token_id,
                pair.market_15.up_token_id,
                pair.market_15.down_token_id,
            )
        },
    )
    monkeypatch.setattr(
        gate0,
        "_load_contexts",
        lambda manifest_path: (
            (context,),
            SimpleNamespace(
                maximum_spread_each_leg=D("0.03"),
                minimum_market_price=D("0.05"),
                maximum_market_price=D("0.95"),
                maximum_book_staleness_ms=1_000,
            ),
            tmp_path / "counterfactual-prereg.json",
        ),
    )
    monkeypatch.setattr(
        gate0,
        "_load_gate_0_contract",
        lambda preregistration_path: {
            "track_id": "btc_5m_15m_edge_readiness_v08_2026_08_18",
            "preregistration_path": str(preregistration_path),
            "preregistration_sha256": "b" * 64,
            "expected_clean_attempts": 1,
            "scan_ttc_seconds_inclusive": [300, 0],
            "candidate_ttc_count": 301,
            "decision_execution_mode": "maker_maker",
            "minimum_average_best_total_pnl_per_expiry_usdc": "0.5",
            "incomplete_existing_41_evidence_action": "rerun_required_not_stop",
        },
    )
    monkeypatch.setattr(gate0, "_sha256", lambda path: "a" * 64)

    report = gate0.build_upper_bound_report(
        manifest_path=tmp_path / "manifest.json",
        decision_tau_seconds=None,
        expected_clean_attempts=1,
    )

    maker_maker = report["execution_modes"]["maker_maker"]
    assert maker_maker["evidence_complete"] is True
    assert D(maker_maker["aggregate_best_total_pnl"]) == D("0")
    assert maker_maker["attempts"][0]["fill_volume_bound_source"] == (
        "context.future_public_trades_by_token_id"
    )
    assert report["decision"] == "STOP"
    assert report["rerun_required"] is False
    assert report["authority"]["strict_parameters_match"] is True


def test_gate_zero_missing_trade_tape_token_requires_rerun(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pair = _v07_pair()
    settlement = replace(
        _settlement_state(pair),
        strike_5=D("101"),
        strike_15=D("100"),
    )
    context = SimpleNamespace(
        pair=pair,
        settlement_state=settlement,
        decision_tau_seconds=60,
        expiry_ms=1_786_700_400_000,
        expiry_cluster_id="expiry-1",
        canonical_pair_id="pair-1",
        decision_at_ms=1_000,
        actual_5_up=True,
        actual_15_up=True,
        replay=SimpleNamespace(
            signal_books=lambda **kwargs: {
                pair.market_15.up_token_id: SimpleNamespace(
                    snapshot=OrderBookSnapshot.from_tuples(
                        pair.market_15.up_token_id,
                        bids=((D("0.99"), D("10")),),
                        asks=((D("0.991"), D("10")),),
                        timestamp_ms=1_000,
                        tick_size=D("0.001"),
                        minimum_order_size=D("5"),
                    )
                ),
                pair.market_5.down_token_id: SimpleNamespace(
                    snapshot=OrderBookSnapshot.from_tuples(
                        pair.market_5.down_token_id,
                        bids=((D("0.01"), D("10")),),
                        asks=((D("0.011"), D("10")),),
                        timestamp_ms=1_000,
                        tick_size=D("0.001"),
                        minimum_order_size=D("5"),
                    )
                ),
                pair.market_5.up_token_id: SimpleNamespace(
                    snapshot=OrderBookSnapshot.from_tuples(
                        pair.market_5.up_token_id,
                        bids=((D("0.50"), D("10")),),
                        asks=((D("0.501"), D("10")),),
                        timestamp_ms=1_000,
                        tick_size=D("0.001"),
                        minimum_order_size=D("5"),
                    )
                ),
                pair.market_15.down_token_id: SimpleNamespace(
                    snapshot=OrderBookSnapshot.from_tuples(
                        pair.market_15.down_token_id,
                        bids=((D("0.50"), D("10")),),
                        asks=((D("0.501"), D("10")),),
                        timestamp_ms=1_000,
                        tick_size=D("0.001"),
                        minimum_order_size=D("5"),
                    )
                ),
            }
        ),
        future_public_trades_by_token_id={
            pair.market_15.up_token_id: (),
            pair.market_5.down_token_id: (),
            pair.market_5.up_token_id: (),
        },
    )
    monkeypatch.setattr(
        gate0,
        "_load_contexts",
        lambda manifest_path: (
            (context,),
            SimpleNamespace(
                maximum_spread_each_leg=D("0.03"),
                minimum_market_price=D("0.05"),
                maximum_market_price=D("0.95"),
                maximum_book_staleness_ms=1_000,
            ),
            tmp_path / "counterfactual-prereg.json",
        ),
    )
    monkeypatch.setattr(gate0, "_sha256", lambda path: "a" * 64)

    report = gate0.build_upper_bound_report(
        manifest_path=tmp_path / "manifest.json",
        decision_tau_seconds=None,
        expected_clean_attempts=1,
    )

    assert report["decision"] == "RERUN_REQUIRED"
    assert report["execution_modes"]["maker_maker"][
        "missing_fill_volume_bound_expiry_ids"
    ] == ["expiry-1"]
