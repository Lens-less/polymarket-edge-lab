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
    assert report["diagnostic"]["attempts"][0]["breakpoints"]
    assert report["diagnostic"]["counts_as_locked_oos_evidence"] is False
    assert report["diagnostic"]["gate_0_route"] == "structural_floor_only"
    assert report["policy"]["quantity_risk_cap_usdc"] is None
    assert report["policy"]["quantity_scope"] == (
        "all_captured_joint_depth_breakpoints"
    )
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
            decision_at_ms=1_000,
            actual_5_up=True,
            actual_15_up=True,
            requested_decision_times=requested_decision_times,
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
    assert report["diagnostic"]["attempts"][0]["best_total_pnl"] == "0E-8"
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
