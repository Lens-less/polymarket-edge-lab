from __future__ import annotations

import json
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
    _freeze_batch,
)
from tests.test_btc_twap_relative_value_v07_counterfactual import (
    _load as _load_counterfactual_case,
)
from tests.test_btc_twap_relative_value_v07_replay import (
    _settlement_state,
    _v07_pair,
)
from tests.test_btc_twap_structural_shadow_report_cli import (
    _http_response,
    _recorder_payload,
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


def _add_future_trade_snapshots(
    manifest_dir: Path,
    case: dict[str, object],
    *,
    observed_through_expiry: bool = True,
    terminal_page_size: int = 1,
    post_expiry_snapshot_count: int = 2,
    page_provenance_observed_through_expiry: bool = True,
    duplicate_identical_terminal_rows: int = 0,
    post_expiry_page_received_at_ms: int | None = None,
    terminal_trade_timestamp_seconds: int | None = None,
    five_min_raw_buy_on_up: bool = False,
) -> None:
    capture_root = manifest_dir / str(case["capture_root"])
    capture_config = json.loads(
        (manifest_dir / str(case["capture_config_path"])).read_text(encoding="utf-8")
    )
    targets = {
        str(target["horizon"]): target
        for target in capture_config["targets"]
    }
    decision_ms = 1_800_000_000_000 - 60_000

    def trade_row(
        *,
        condition_id: str,
        token_id: str,
        side: str,
        size: str,
        price: str,
        timestamp_seconds: int,
        transaction_seed: int,
        wallet_seed: int,
        outcome: str,
        outcome_index: int,
    ) -> dict[str, object]:
        return {
            "conditionId": condition_id,
            "asset": token_id,
            "side": side,
            "size": size,
            "price": price,
            "timestamp": timestamp_seconds,
            "transactionHash": "0x" + format(transaction_seed, "064x"),
            "proxyWallet": "0x" + format(wallet_seed, "040x"),
            "outcome": outcome,
            "outcomeIndex": outcome_index,
        }

    trade_15_up_page_1 = [
        trade_row(
            condition_id=targets["15m"]["condition_id"],
            token_id=targets["15m"]["up_token_id"],
            side="SELL",
            size="1",
            price="0.49",
            timestamp_seconds=decision_ms // 1_000 + 1,
            transaction_seed=index + 1,
            wallet_seed=index + 100,
            outcome="Up",
            outcome_index=0,
        )
        for index in range(1_000)
    ]
    trade_15_up_page_2 = [
        trade_row(
            condition_id=targets["15m"]["condition_id"],
            token_id=targets["15m"]["up_token_id"],
            side="SELL",
            size="6",
            price="0.49",
            timestamp_seconds=(
                decision_ms // 1_000 + 2
                if terminal_trade_timestamp_seconds is None
                else terminal_trade_timestamp_seconds
            ),
            transaction_seed=2_000 + index,
            wallet_seed=2_100 + index,
            outcome="Up",
            outcome_index=0,
        )
        for index in range(terminal_page_size)
    ]
    trade_5_down = [
        trade_row(
            condition_id=targets["5m"]["condition_id"],
            token_id=(
                targets["5m"]["up_token_id"]
                if five_min_raw_buy_on_up
                else targets["5m"]["down_token_id"]
            ),
            side=("BUY" if five_min_raw_buy_on_up else "SELL"),
            size="5",
            price=("0.45" if five_min_raw_buy_on_up else "0.55"),
            timestamp_seconds=decision_ms // 1_000 + 2,
            transaction_seed=3_000,
            wallet_seed=3_100,
            outcome=("Up" if five_min_raw_buy_on_up else "Down"),
            outcome_index=(0 if five_min_raw_buy_on_up else 1),
        )
    ]
    if duplicate_identical_terminal_rows:
        duplicate_row = trade_row(
            condition_id=targets["15m"]["condition_id"],
            token_id=targets["15m"]["up_token_id"],
            side="SELL",
            size="6",
            price="0.49",
            timestamp_seconds=decision_ms // 1_000 + 2,
            transaction_seed=9_999,
            wallet_seed=8_888,
            outcome="Up",
            outcome_index=0,
        )
        trade_15_up_page_2.extend(
            duplicate_row.copy()
            for _ in range(duplicate_identical_terminal_rows)
        )

    def _epoch_seconds_text(timestamp_ms: int) -> str:
        return format(Decimal(timestamp_ms) / Decimal(1_000), "f")

    def _snapshot_payload(
        *,
        response_received_at_ms: int,
    ) -> dict[str, object]:
        responses = [
            _http_response(
                resource="data_api_trades",
                request_key=targets["15m"]["condition_id"],
                raw_json=trade_15_up_page_1,
                source="data_api_trades",
                url="https://data-api.polymarket.com/trades",
                request_params={
                    "market": targets["15m"]["condition_id"],
                    "takerOnly": "true",
                    "limit": 1_000,
                    "offset": 0,
                },
                page_number=1,
            ),
            _http_response(
                resource="data_api_trades",
                request_key=targets["15m"]["condition_id"],
                raw_json=trade_15_up_page_2,
                source="data_api_trades",
                url="https://data-api.polymarket.com/trades",
                request_params={
                    "market": targets["15m"]["condition_id"],
                    "takerOnly": "true",
                    "limit": 1_000,
                    "offset": 1_000,
                },
                page_number=2,
            ),
            _http_response(
                resource="data_api_trades",
                request_key=targets["5m"]["condition_id"],
                raw_json=trade_5_down,
                source="data_api_trades",
                url="https://data-api.polymarket.com/trades",
                request_params={
                    "market": targets["5m"]["condition_id"],
                    "takerOnly": "true",
                    "limit": 1_000,
                    "offset": 0,
                },
                page_number=1,
            ),
        ]
        for response in responses:
            response["provenance"]["requested_at_epoch_seconds"] = (
                _epoch_seconds_text(response_received_at_ms - 250)
            )
            response["provenance"]["received_at_epoch_seconds"] = (
                _epoch_seconds_text(response_received_at_ms)
            )
        return {
            "schema_version": "edge-lab-public-snapshot.v1",
            "snapshot_kind": "trades",
            "requested_asset_ids": [],
            "responses": responses,
            "truncated_resources": [],
        }

    rows: list[dict[str, object]] = []
    if not observed_through_expiry:
        rows.append(
            {
                "received_at_ms": decision_ms + 3_000,
                "event_at_ms": decision_ms + 3_000,
                "payload": {
                    **_recorder_payload(
                        "trades_http",
                        "trades_snapshot",
                        _snapshot_payload(
                            response_received_at_ms=(
                                decision_ms + 3_000
                                if page_provenance_observed_through_expiry
                                else decision_ms + 3_000
                            )
                        ),
                        kind="snapshot",
                    ),
                    "schema_version": "trades-http.snapshot.v1",
                },
            }
        )
    else:
        rows.append(
            {
                "received_at_ms": decision_ms + 3_000,
                "event_at_ms": decision_ms + 3_000,
                "payload": {
                    **_recorder_payload(
                        "trades_http",
                        "trades_snapshot",
                        _snapshot_payload(
                            response_received_at_ms=decision_ms + 3_000
                        ),
                        kind="snapshot",
                    ),
                    "schema_version": "trades-http.snapshot.v1",
                },
            }
        )
        for snapshot_index in range(post_expiry_snapshot_count):
            response_received_at_ms = (
                (
                    post_expiry_page_received_at_ms
                    if post_expiry_page_received_at_ms is not None
                    else 1_800_000_000_000 + 1_000 + snapshot_index * 1_000
                )
                if page_provenance_observed_through_expiry
                else decision_ms + 3_000
            )
            rows.append(
                {
                    "received_at_ms": 1_800_000_000_000 + 1_000 + snapshot_index * 1_000,
                    "event_at_ms": 1_800_000_000_000 + 1_000 + snapshot_index * 1_000,
                    "payload": {
                        **_recorder_payload(
                            "trades_http",
                            "trades_snapshot",
                            _snapshot_payload(
                                response_received_at_ms=response_received_at_ms
                            ),
                            kind="snapshot",
                        ),
                        "schema_version": "trades-http.snapshot.v1",
                    },
                }
            )
    _freeze_batch(
        capture_root,
        source="trades_http",
        batch_id="future-public-trades",
        rows=rows,
    )


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


def test_counterfactual_context_defaults_future_trade_tape_to_missing_when_absent(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)

    context = _load_counterfactual_case(case, manifest_dir)[0]

    assert context.future_public_trades_by_token_id is None


def test_counterfactual_context_loads_exact_four_token_future_trade_tape(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    _add_future_trade_snapshots(manifest_dir, case)

    context = _load_counterfactual_case(case, manifest_dir)[0]

    assert context.future_public_trades_by_token_id is not None
    assert set(context.future_public_trades_by_token_id) == {
        context.pair.market_5.up_token_id,
        context.pair.market_5.down_token_id,
        context.pair.market_15.up_token_id,
        context.pair.market_15.down_token_id,
    }
    assert len(context.future_public_trades_by_token_id[context.pair.market_15.up_token_id]) == 1001
    assert len(context.future_public_trades_by_token_id[context.pair.market_5.down_token_id]) == 1
    assert context.future_public_trades_by_token_id[context.pair.market_5.up_token_id] == ()
    assert context.future_public_trades_by_token_id[context.pair.market_15.down_token_id] == ()
    assert len(
        {
            trade.source_event_id
            for trades in context.future_public_trades_by_token_id.values()
            for trade in trades
        }
    ) == 1002


def test_counterfactual_context_requires_two_stable_post_expiry_snapshots(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    _add_future_trade_snapshots(
        manifest_dir,
        case,
        post_expiry_snapshot_count=1,
    )

    context = _load_counterfactual_case(case, manifest_dir)[0]

    assert context.future_public_trades_by_token_id is None


def test_counterfactual_context_rejects_pre_expiry_page_provenance(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    _add_future_trade_snapshots(
        manifest_dir,
        case,
        page_provenance_observed_through_expiry=False,
    )

    context = _load_counterfactual_case(case, manifest_dir)[0]

    assert context.future_public_trades_by_token_id is None


def test_counterfactual_context_applies_nonzero_clock_offset_to_page_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    baseline_context = _load_counterfactual_case(case, manifest_dir)[0]
    capture_config_path = manifest_dir / str(case["capture_config_path"])
    capture_config = json.loads(capture_config_path.read_text(encoding="utf-8"))
    capture_config["clock_sync"]["causal_receipt_offset_ms"] = 1
    capture_config_path.write_bytes(canonical_json_bytes(capture_config) + b"\n")
    _add_future_trade_snapshots(
        manifest_dir,
        case,
        post_expiry_page_received_at_ms=1_799_999_999_999,
    )
    monkeypatch.setattr(builder, "_receipt_clock_offset_ms", lambda root: 1)

    future_public_trades = builder._validated_future_public_trades(
        case_alias="expiry-fixture",
        capture_root=manifest_dir / str(case["capture_root"]),
        pair=baseline_context.pair,
        decision_at_ms=baseline_context.decision_at_ms,
        expiry_ms=baseline_context.expiry_ms,
    )

    assert future_public_trades is not None
    assert len(
        future_public_trades[baseline_context.pair.market_15.up_token_id]
    ) == 1001


def test_counterfactual_context_preserves_identical_trade_row_multiplicity(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    _add_future_trade_snapshots(
        manifest_dir,
        case,
        duplicate_identical_terminal_rows=1,
    )

    context = _load_counterfactual_case(case, manifest_dir)[0]

    assert context.future_public_trades_by_token_id is not None
    duplicated = context.future_public_trades_by_token_id[
        context.pair.market_15.up_token_id
    ]
    assert len(duplicated) == 1002
    assert len({trade.source_event_id for trade in duplicated}) == 1002
    assert duplicated[-1].price == duplicated[-2].price
    assert duplicated[-1].quantity == duplicated[-2].quantity
    assert duplicated[-1].timestamp_ms == duplicated[-2].timestamp_ms


def test_counterfactual_context_normalizes_raw_buy_to_complement_sell_tape(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    _add_future_trade_snapshots(
        manifest_dir,
        case,
        five_min_raw_buy_on_up=True,
    )

    context = _load_counterfactual_case(case, manifest_dir)[0]

    assert context.future_public_trades_by_token_id is not None
    assert context.future_public_trades_by_token_id[
        context.pair.market_5.up_token_id
    ] == ()
    normalized = context.future_public_trades_by_token_id[
        context.pair.market_5.down_token_id
    ]
    assert len(normalized) == 1
    assert normalized[0].token_id == context.pair.market_5.down_token_id
    assert normalized[0].aggressor_side == "SELL"
    assert normalized[0].price == D("0.55")
    assert normalized[0].quantity == D("5")


def test_counterfactual_context_rejects_trade_after_page_receipt_even_before_outer_receipt(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    _add_future_trade_snapshots(
        manifest_dir,
        case,
        post_expiry_page_received_at_ms=1_800_000_001_500,
        terminal_trade_timestamp_seconds=1_800_000_002,
    )

    context = _load_counterfactual_case(case, manifest_dir)[0]

    assert context.future_public_trades_by_token_id is None


def test_counterfactual_context_rejects_trade_tape_not_observed_through_expiry(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    _add_future_trade_snapshots(
        manifest_dir,
        case,
        observed_through_expiry=False,
    )

    context = _load_counterfactual_case(case, manifest_dir)[0]

    assert context.future_public_trades_by_token_id is None


def test_counterfactual_context_rejects_full_terminal_trade_page(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    _add_future_trade_snapshots(
        manifest_dir,
        case,
        terminal_page_size=1_000,
    )

    context = _load_counterfactual_case(case, manifest_dir)[0]

    assert context.future_public_trades_by_token_id is None


def test_formal_gate_zero_consumes_trade_tape_loaded_from_capture_manifest(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    _add_future_trade_snapshots(manifest_dir, case)
    manifest_path = manifest_dir / "manifest-with-trades.json"
    manifest_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": builder.MANIFEST_SCHEMA_VERSION,
                "preregistration_path": str(PREREGISTRATION_PATH),
                "cases": [case],
            }
        )
        + b"\n"
    )

    report = gate0.build_upper_bound_report(
        manifest_path=manifest_path,
        decision_tau_seconds=60,
        expected_clean_attempts=1,
    )

    maker_maker = report["execution_modes"]["maker_maker"]
    assert maker_maker["evidence_complete"] is True
    assert maker_maker["decision"] == "STOP"
    assert maker_maker["attempts"][0]["fill_volume_bound_source"] == (
        "context.future_public_trades_by_token_id"
    )


def test_formal_gate_zero_counts_complement_side_buy_as_maker_sell_capacity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    _add_future_trade_snapshots(
        manifest_dir,
        case,
        five_min_raw_buy_on_up=True,
    )
    loaded = _load_counterfactual_case(case, manifest_dir)[0]
    fee_exempt = ExecutionFeeSchedule.fee_exempt(
        reason="gate0-buy-normalization",
        source_ref="fixture://gate0-buy-normalization",
    )
    pair = SameExpiryPair.from_contracts(
        replace(
            loaded.pair.market_5,
            fee_schedule=fee_exempt,
            tick_size=D("0.001"),
        ),
        replace(
            loaded.pair.market_15,
            fee_schedule=fee_exempt,
            tick_size=D("0.001"),
        ),
    )
    settlement = replace(
        loaded.settlement_state,
        market_5_rule_hash=pair.market_5.rule_hash,
        market_15_rule_hash=pair.market_15.rule_hash,
        strike_5=D("101"),
        strike_15=D("100"),
    )
    books = {
        pair.market_15.up_token_id: OrderBookSnapshot.from_tuples(
            pair.market_15.up_token_id,
            bids=((D("0.49"), D("10")),),
            asks=((D("0.491"), D("10")),),
            timestamp_ms=1_000,
            tick_size=D("0.001"),
            minimum_order_size=D("5"),
        ),
        pair.market_5.down_token_id: OrderBookSnapshot.from_tuples(
            pair.market_5.down_token_id,
            bids=((D("0.55"), D("10")),),
            asks=((D("0.551"), D("10")),),
            timestamp_ms=1_000,
            tick_size=D("0.001"),
            minimum_order_size=D("5"),
        ),
        pair.market_5.up_token_id: OrderBookSnapshot.from_tuples(
            pair.market_5.up_token_id,
            bids=((D("0.45"), D("10")),),
            asks=((D("0.451"), D("10")),),
            timestamp_ms=1_000,
            tick_size=D("0.001"),
            minimum_order_size=D("5"),
        ),
        pair.market_15.down_token_id: OrderBookSnapshot.from_tuples(
            pair.market_15.down_token_id,
            bids=((D("0.51"), D("10")),),
            asks=((D("0.511"), D("10")),),
            timestamp_ms=1_000,
            tick_size=D("0.001"),
            minimum_order_size=D("5"),
        ),
    }
    context = replace(
        loaded,
        pair=pair,
        settlement_state=settlement,
        replay=SimpleNamespace(
            signal_books=lambda **kwargs: {
                token_id: SimpleNamespace(snapshot=books[token_id])
                for token_id in kwargs["token_ids"]
            }
        ),
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
        decision_tau_seconds=60,
        expected_clean_attempts=1,
    )

    maker_maker = report["execution_modes"]["maker_maker"]
    assert maker_maker["evidence_complete"] is True
    assert maker_maker["attempts"][0]["fill_volume_bound_source"] == (
        "context.future_public_trades_by_token_id"
    )
    assert maker_maker["attempts"][0]["selected_level"]["quantity"] == "5"
    assert maker_maker["attempts"][0]["selected_level"]["maker_token_ids"] == [
        pair.market_15.up_token_id,
        pair.market_5.down_token_id,
    ]
    assert maker_maker["attempts"][0]["best_total_pnl"] == "0"
    assert maker_maker["decision"] == "STOP"


def test_gate_zero_uses_preregistered_minimum_average_threshold(
) -> None:
    pair = _v07_pair()
    fee_exempt = ExecutionFeeSchedule.fee_exempt(
        reason="gate0-prereg-threshold",
        source_ref="fixture://gate0-prereg-threshold",
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
    context = SimpleNamespace(
        decision_tau_seconds=60,
        expiry_ms=1_786_700_400_000,
        expiry_cluster_id="expiry-1",
        canonical_pair_id="pair-1",
        pair=pair,
        settlement_state=settlement,
    )
    books = {
        pair.market_15.up_token_id: OrderBookSnapshot.from_tuples(
            pair.market_15.up_token_id,
            bids=((D("0.989"), D("10")),),
            asks=((D("0.99"), D("10")),),
            timestamp_ms=1_000,
            tick_size=D("0.001"),
            minimum_order_size=D("5"),
        ),
        pair.market_5.down_token_id: OrderBookSnapshot.from_tuples(
            pair.market_5.down_token_id,
            bids=((D("0.009"), D("10")),),
            asks=((D("0.01"), D("10")),),
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
    execution_modes, _ = gate0._execution_mode_diagnostics(
        observations_by_expiry={
            "expiry-1": (
                (
                    context,
                    books,
                    {
                        pair.market_15.up_token_id: D("10"),
                        pair.market_5.down_token_id: D("10"),
                        pair.market_5.up_token_id: D("0"),
                        pair.market_15.down_token_id: D("0"),
                    },
                    "context.future_public_trades_by_token_id",
                ),
            )
        },
        required_observation_count_per_expiry=1,
        minimum_average_pnl_per_expiry=D("0.0201"),
    )

    maker_maker = execution_modes["maker_maker"]
    assert D(maker_maker["average_best_total_pnl_per_expiry"]) == D("0.020")
    assert maker_maker["minimum_average_pnl_per_expiry"] == "0.0201"
    assert maker_maker["decision"] == "STOP"
    assert maker_maker["gate_0_passed"] is False


def test_gate_zero_rejects_invalid_preregistered_minimum_average_threshold(
    tmp_path: Path,
) -> None:
    preregistration = json.loads(
        gate0.V08_PREREGISTRATION_PATH.read_text(encoding="utf-8")
    )
    preregistration["gate_0"][
        "minimum_average_best_total_pnl_per_expiry_usdc"
    ] = "NaN"
    invalid_path = tmp_path / "invalid-gate0-prereg.json"
    invalid_path.write_bytes(canonical_json_bytes(preregistration) + b"\n")

    with pytest.raises(ValueError, match="minimum average"):
        gate0._load_gate_0_contract(invalid_path)


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
        minimum_average_pnl_per_expiry=D("0.5"),
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
        future_public_trades_by_token_id=None,
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
