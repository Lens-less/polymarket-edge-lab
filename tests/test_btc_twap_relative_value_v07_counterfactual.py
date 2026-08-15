from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts import build_btc_twap_relative_value_v07_counterfactual as builder
from src.edge_lab.btc_twap_relative_value_v07 import (
    CANONICAL_EVENT_CLUSTER_PREFIX,
    SharedTerminalModelConfig,
    V07EdgeBasis,
    V07ModelRejection,
    V07StrategyConfig,
    canonical_event_cluster_id,
)
from src.edge_lab.data_store import CaptureStore, canonical_json_bytes
from src.edge_lab.settlement_regime import V06_SETTLEMENT_REGIME_ID
from tests.test_btc_twap_relative_value_v07_replay import _replay as _structural_replay
from tests.test_edge_lab_btc_twap_relative_value import (
    _replay_observation,
    _ReplayStub,
)

D = Decimal
EXPIRY_MS = 1_800_000_000_000
DECISION_MS = EXPIRY_MS - 60_000
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREREGISTRATION_PATH = (
    PROJECT_ROOT
    / "research"
    / "btc_5m_15m_relative_value_counterfactual_v07_2026-08-15"
    / "PREREGISTRATION.json"
)


def _iso(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _freeze_batch(
    root: Path,
    *,
    source: str,
    batch_id: str,
    rows: list[dict[str, Any]],
) -> tuple[str, ...]:
    writer = CaptureStore(root).open_raw_batch(
        source=source,
        batch_id=batch_id,
        schema_version="edge-lab-recorder.raw.v1",
    )
    record_ids: list[str] = []
    for index, row in enumerate(rows, start=1):
        result = writer.append(
            received_at=_iso(int(row["received_at_ms"])),
            event_at=(
                None
                if row.get("event_at_ms") is None
                else _iso(int(row["event_at_ms"]))
            ),
            sequence=index,
            payload=row["payload"],
        )
        record_ids.append(result.record_id)
    writer.finalize(finalized_at=_iso(EXPIRY_MS + 20_000))
    return tuple(record_ids)


def _rule_identity(target: dict[str, Any], description: str) -> dict[str, Any]:
    fee = target["fee_schedule"]
    return {
        "schema_version": "btc-twap-market-contract.v1",
        "horizon": target["horizon"],
        "slug": target["slug"],
        "market_id": target["market_id"],
        "condition_id": target["condition_id"].lower(),
        "token_ids": [target["up_token_id"], target["down_token_id"]],
        "opens_at_ms": target["opens_at_ms"],
        "closes_at_ms": target["closes_at_ms"],
        "twap_window_seconds": 60,
        "source_topic": "crypto_prices_twap_sixty",
        "resolution_source": target["resolution_source"],
        "settlement_regime": V06_SETTLEMENT_REGIME_ID,
        "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
        "tick_size": "0.01",
        "minimum_order_size": "5",
        "fee_rate": str(fee["rate"]),
        "fee_exponent": str(fee["exponent"]),
        "fee_taker_only": fee["taker_only"],
        "taker_delay_ms": 250,
    }


def _target(horizon: str) -> tuple[dict[str, Any], dict[str, Any]]:
    opens_at_ms = EXPIRY_MS - (300_000 if horizon == "5m" else 900_000)
    suffix = "5" if horizon == "5m" else "15"
    description = (
        f"BTC {horizon} resolves Up when the terminal Chainlink BTC/USD "
        "60-second TWAP is at least its opening 60-second TWAP."
    )
    target: dict[str, Any] = {
        "horizon": horizon,
        "slug": f"btc-updown-{horizon}-fixture",
        "market_id": f"market-{suffix}",
        "condition_id": f"0xcondition{suffix}",
        "up_token_id": f"token-{suffix}-up",
        "down_token_id": f"token-{suffix}-down",
        "opens_at_ms": opens_at_ms,
        "closes_at_ms": EXPIRY_MS,
        "twap_window_seconds": 60,
        "source_topic": "crypto_prices_twap_sixty",
        "resolution_source": "https://data.chain.link/streams/btc-usd",
        "settlement_regime": V06_SETTLEMENT_REGIME_ID,
        "tick_size": "0.01",
        "minimum_order_size": "5",
        "fee_schedule": {
            "rate": "0.07",
            "exponent": "1",
            "taker_only": True,
        },
        "taker_delay_ms": 250,
        "accepting_orders": True,
        "rules_text_sha256": hashlib.sha256(description.encode()).hexdigest(),
    }
    target["rule_hash"] = hashlib.sha256(
        canonical_json_bytes(_rule_identity(target, description))
    ).hexdigest()
    rule = {
        "id": target["market_id"],
        "slug": target["slug"],
        "conditionId": target["condition_id"],
        "outcomes": ["Up", "Down"],
        "clobTokenIds": [target["up_token_id"], target["down_token_id"]],
        "description": description,
        "resolutionSource": target["resolution_source"],
        "feeSchedule": {
            "rate": "0.07",
            "exponent": "1",
            "takerOnly": True,
        },
    }
    return target, rule


def _rtds_payload(
    *, topic: str, symbol: str, timestamp_ms: int, value: str
) -> dict[str, Any]:
    return {
        "event_type": "crypto_prices.update",
        "payload": {
            "topic": topic,
            "payload": {
                "symbol": symbol,
                "timestamp": timestamp_ms,
                "value": value,
            },
        },
    }


def _book_payload(
    token_id: str,
    *,
    ask: str,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    if timestamp_ms is None:
        timestamp_ms = DECISION_MS
    bid = D(ask) - D("0.01")
    return {
        "event_type": "book",
        "payload": {
            "event_type": "book",
            "asset_id": token_id,
            "market": "fixture-condition",
            "timestamp": str(timestamp_ms),
            "bids": [{"price": bid, "size": D("100")}],
            "asks": [{"price": D(ask), "size": D("100")}],
        },
    }


def _build_case(
    tmp_path: Path,
    *,
    rule_received_at_ms: int = DECISION_MS - 10_000,
    resolution_5_received_at_ms: int = EXPIRY_MS + 5_000,
    resolution_15_received_at_ms: int = EXPIRY_MS + 7_000,
    tamper_rule_hash: bool = False,
    generated_fixture: bool = True,
) -> tuple[dict[str, Any], Path]:
    capture_root = tmp_path / "capture"
    predictor_root = tmp_path / "predictor"
    target_5, rule_5 = _target("5m")
    target_15, rule_15 = _target("15m")
    pair = builder._pair_from_capture_targets({"5m": target_5, "15m": target_15})
    assert pair is not None
    canonical_cluster_id = canonical_event_cluster_id(pair)
    if tamper_rule_hash:
        target_5["rule_hash"] = "f" * 64

    _freeze_batch(
        capture_root,
        source="rtds_ws",
        batch_id="shared-terminal",
        rows=[
            {
                "received_at_ms": target_15["opens_at_ms"] + 25,
                "event_at_ms": target_15["opens_at_ms"],
                "payload": _rtds_payload(
                    topic="crypto_prices_twap_sixty",
                    symbol="btc/usd",
                    timestamp_ms=target_15["opens_at_ms"],
                    value="100",
                ),
            },
            {
                "received_at_ms": target_5["opens_at_ms"] + 25,
                "event_at_ms": target_5["opens_at_ms"],
                "payload": _rtds_payload(
                    topic="crypto_prices_twap_sixty",
                    symbol="btc/usd",
                    timestamp_ms=target_5["opens_at_ms"],
                    value="101",
                ),
            },
            {
                "received_at_ms": DECISION_MS,
                "event_at_ms": DECISION_MS,
                "payload": _rtds_payload(
                    topic="crypto_prices_twap_sixty",
                    symbol="btc/usd",
                    timestamp_ms=DECISION_MS,
                    value="100.2",
                ),
            },
            {
                "received_at_ms": EXPIRY_MS + 1_000,
                "event_at_ms": EXPIRY_MS,
                "payload": _rtds_payload(
                    topic="crypto_prices_twap_sixty",
                    symbol="btc/usd",
                    timestamp_ms=EXPIRY_MS,
                    value="100.5",
                ),
            },
        ],
    )

    predictor_rows: list[dict[str, Any]] = []
    for index in range(300):
        timestamp_ms = DECISION_MS - (299 - index) * 1_000
        value = D("100") + D(index % 11) / D("1000")
        predictor_rows.append(
            {
                "received_at_ms": timestamp_ms,
                "event_at_ms": timestamp_ms,
                "payload": _rtds_payload(
                    topic="crypto_prices",
                    symbol="btcusdt",
                    timestamp_ms=timestamp_ms,
                    value=str(value),
                ),
            }
        )
    _freeze_batch(
        predictor_root,
        source="rtds_ws",
        batch_id="predictor",
        rows=predictor_rows,
    )

    _freeze_batch(
        capture_root,
        source="rules_http",
        batch_id="rules",
        rows=[
            {
                "received_at_ms": rule_received_at_ms,
                "event_at_ms": rule_received_at_ms,
                "payload": {
                    "event_type": "rules_snapshot",
                    "payload": {
                        "responses": [
                            {"raw_json": rule_5},
                            {"raw_json": rule_15},
                        ]
                    },
                },
            }
        ],
    )

    resolution_5_event_ms = max(EXPIRY_MS, resolution_5_received_at_ms)
    resolution_15_event_ms = max(EXPIRY_MS, resolution_15_received_at_ms)
    clob_rows = [
        {
            "received_at_ms": DECISION_MS,
            "event_at_ms": DECISION_MS,
            "payload": _book_payload(target_5["up_token_id"], ask="0.45"),
        },
        {
            "received_at_ms": DECISION_MS,
            "event_at_ms": DECISION_MS,
            "payload": _book_payload(target_5["down_token_id"], ask="0.56"),
        },
        {
            "received_at_ms": DECISION_MS,
            "event_at_ms": DECISION_MS,
            "payload": _book_payload(target_15["up_token_id"], ask="0.50"),
        },
        {
            "received_at_ms": DECISION_MS,
            "event_at_ms": DECISION_MS,
            "payload": _book_payload(target_15["down_token_id"], ask="0.51"),
        },
        *[
            {
                "received_at_ms": timestamp_ms,
                "event_at_ms": timestamp_ms,
                "payload": _book_payload(
                    token_id,
                    ask=ask,
                    timestamp_ms=timestamp_ms,
                ),
            }
            for timestamp_ms in (
                DECISION_MS + 250,
                DECISION_MS + 500,
                DECISION_MS + 1_250,
                DECISION_MS + 5_250,
                DECISION_MS + 5_500,
                DECISION_MS + 6_250,
            )
            for token_id, ask in (
                (target_5["up_token_id"], "0.45"),
                (target_5["down_token_id"], "0.56"),
                (target_15["up_token_id"], "0.50"),
                (target_15["down_token_id"], "0.51"),
            )
        ],
        {
            "received_at_ms": resolution_5_received_at_ms,
            "event_at_ms": resolution_5_event_ms,
            "payload": {
                "event_type": "market_resolved",
                "payload": {
                    "id": target_5["market_id"],
                    "market": target_5["condition_id"],
                    "winning_asset_id": target_5["down_token_id"],
                    "winning_outcome": "Down",
                },
            },
        },
        {
            "received_at_ms": resolution_15_received_at_ms,
            "event_at_ms": resolution_15_event_ms,
            "payload": {
                "event_type": "market_resolved",
                "payload": {
                    "id": target_15["market_id"],
                    "market": target_15["condition_id"],
                    "winning_asset_id": target_15["up_token_id"],
                    "winning_outcome": "Up",
                },
            },
        },
    ]
    _freeze_batch(
        capture_root,
        source="clob_market_ws",
        batch_id="books-and-resolution",
        rows=clob_rows,
    )

    capture_config = {
        "schema_version": "btc-5m-15m-relative-value-capture.v1",
        "capture_started_at_ms": target_15["opens_at_ms"] - 1_000,
        "generated_fixture": generated_fixture,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "clock_sync": {
            "schema_version": "btc-twap-clock-sync.v1",
            "causal_receipt_offset_ms": 0,
            "uncertainty_ms": 50,
            "measured_at_raw_ms": DECISION_MS - 1_000,
            "system_clock_mutated": False,
        },
        "targets": [target_5, target_15],
    }
    capture_config_path = capture_root / "capture-config.json"
    capture_config_path.write_bytes(canonical_json_bytes(capture_config) + b"\n")
    case = {
        "event_cluster_id": "expiry-fixture",
        "canonical_event_cluster_id": canonical_cluster_id,
        "split": "test",
        "capture_root": capture_root.name,
        "predictor_root": predictor_root.name,
        "capture_config_path": f"{capture_root.name}/capture-config.json",
        "history_roots": [],
        "decision_tau_seconds": [60],
    }
    return case, tmp_path


def _load(case: dict[str, Any], manifest_dir: Path) -> tuple[Any, ...]:
    return builder._load_case_contexts(
        case,
        manifest_case_index=0,
        manifest_dir=manifest_dir,
        model=SharedTerminalModelConfig(n_paths=100),
        strategy=V07StrategyConfig(),
    )


def test_builder_structural_path_skips_model_simulation_and_predictor_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    context = _load(case, manifest_dir)[0]
    structural_context = replace(
        context,
        settlement_state=replace(
            context.settlement_state,
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        predictor_prices=(),
        actual_5_up=True,
        actual_15_up=False,
        replay=_structural_replay(
            context.pair,
            context.decision_at_ms,
            forecast_available_at_ms=context.decision_at_ms + 5_000,
            include_second=True,
        ),
    )

    def reject_simulation(**_kwargs: Any) -> None:
        raise AssertionError("structural path unexpectedly invoked Monte Carlo")

    monkeypatch.setattr(
        builder,
        "simulate_shared_terminal_twap_60_distribution",
        reject_simulation,
    )
    result = builder._run_setting(
        setting_id="primary",
        contexts=(structural_context,),
        model=SharedTerminalModelConfig(n_paths=100),
        strategy=V07StrategyConfig(),
        candidate_weights=(D("0"), D("1")),
        lock_index=None,
    )

    assert result.simulations == {}
    assert result.shrinkage is None
    assert result.validation_veto is None
    assert len(result.cycles) == 1
    assert result.cycles[0].decision.edge_basis is V07EdgeBasis.STRUCTURAL
    assert result.forecasts[0].forecast_q_5_up is None
    assert result.forecasts[0].forecast_q_15_up is None
    assert result.forecasts[0].probability_diagnostics_applicable is False


def test_predictive_training_rejection_preserves_structural_and_fails_nonstructural(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    context = _load(case, manifest_dir)[0]
    structural_context = replace(
        context,
        settlement_state=replace(
            context.settlement_state,
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        predictor_prices=(),
        actual_5_up=True,
        actual_15_up=False,
        replay=_structural_replay(
            context.pair,
            context.decision_at_ms,
            forecast_available_at_ms=context.decision_at_ms + 5_000,
            include_second=True,
        ),
    )
    signal_books = {
        token_id: _replay_observation(
            token_id,
            bid="0.59",
            ask="0.60",
            timestamp_ms=context.decision_at_ms - 100,
            received_at_ms=context.decision_at_ms - 100,
            source_event_id=f"nonstructural-signal-{token_id}",
        )
        for token_id in (
            context.pair.market_5.up_token_id,
            context.pair.market_5.down_token_id,
            context.pair.market_15.up_token_id,
            context.pair.market_15.down_token_id,
        )
    }
    nonstructural_context = replace(
        structural_context,
        decision_tau_seconds=61,
        predictor_prices=(),
        replay=_ReplayStub(signal_books=signal_books, executable_books={}),
    )
    train_context = replace(
        structural_context,
        split="train",
        decision_tau_seconds=101,
        predictor_prices=(),
    )
    validation_context = replace(
        structural_context,
        split="validation",
        decision_tau_seconds=102,
        predictor_prices=(),
    )

    def reject_simulation(**_kwargs: Any) -> None:
        raise V07ModelRejection("forced_training_rejection", "generated regression")

    monkeypatch.setattr(
        builder,
        "simulate_shared_terminal_twap_60_distribution",
        reject_simulation,
    )
    result = builder._run_setting(
        setting_id="primary",
        contexts=(
            train_context,
            validation_context,
            structural_context,
            nonstructural_context,
        ),
        model=SharedTerminalModelConfig(n_paths=100),
        strategy=V07StrategyConfig(),
        candidate_weights=(D("0"), D("1")),
        lock_index=None,
    )

    cycles_by_tau = {cycle.decision_tau_seconds: cycle for cycle in result.cycles}
    assert cycles_by_tau[60].decision.edge_basis is V07EdgeBasis.STRUCTURAL
    assert cycles_by_tau[61].decision.edge_basis is V07EdgeBasis.NONE
    assert (
        "predictive_training_model_rejected_after_structural_scan:"
        "forced_training_rejection"
    ) in cycles_by_tau[61].decision.reason_codes
    assert result.shrinkage is None
    assert result.validation_veto is None
    assert result.simulations == {}


def test_raw_case_uses_official_resolution_receipts_for_label_availability(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)

    contexts = _load(case, manifest_dir)

    assert len(contexts) == 1
    context = contexts[0]
    assert context.actual_5_up is False
    assert context.actual_15_up is True
    assert context.label_available_at_ms == EXPIRY_MS + 7_000
    assert context.resolution_received_at_ms == {
        "5m": EXPIRY_MS + 5_000,
        "15m": EXPIRY_MS + 7_000,
    }
    assert all(context.resolution_source_event_ids.values())
    assert all(
        item["available_by_earliest_decision"] is True
        for item in context.rules_and_fees.values()
    )
    assert context.immutable_public_capture_evidence is False
    with pytest.raises(TypeError):
        context.resolution_received_at_ms["5m"] = 0  # type: ignore[index]
    with pytest.raises(TypeError):
        context.capture_identity["capture_root"]["integrity"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        context.rules_and_fees["market-5"]["rule_hash"] = "tampered"  # type: ignore[index]


def test_raw_case_rejects_rule_snapshot_received_after_earliest_decision(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(
        tmp_path,
        rule_received_at_ms=DECISION_MS + 1,
    )

    with pytest.raises(
        ValueError, match="causal captured public rule metadata missing"
    ):
        _load(case, manifest_dir)


def test_raw_case_reconstructs_and_rejects_tampered_rule_hash(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path, tamper_rule_hash=True)

    with pytest.raises(ValueError, match="reconstructed frozen rule hash mismatch"):
        _load(case, manifest_dir)


def test_raw_case_rejects_official_resolution_received_before_expiry(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(
        tmp_path,
        resolution_5_received_at_ms=EXPIRY_MS - 1,
    )

    with pytest.raises(ValueError, match="official resolution receipt predates expiry"):
        _load(case, manifest_dir)


def test_raw_case_rejects_unsupported_manifest_fields(tmp_path: Path) -> None:
    case, manifest_dir = _build_case(tmp_path)
    case["proxy_url"] = "http://127.0.0.1:8080"

    with pytest.raises(ValueError, match="unsupported keys: proxy_url"):
        _load(case, manifest_dir)


def test_raw_case_rejects_proxy_or_credential_capture_configuration(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    capture_config_path = manifest_dir / str(case["capture_config_path"])
    capture_config = json.loads(capture_config_path.read_text(encoding="utf-8"))
    capture_config["proxy_url"] = "http://127.0.0.1:8080"
    capture_config_path.write_bytes(canonical_json_bytes(capture_config) + b"\n")

    with pytest.raises(ValueError, match="forbidden proxy configuration"):
        _load(case, manifest_dir)

    capture_config.pop("proxy_url")
    capture_config["api_key"] = "not-a-real-key"
    capture_config_path.write_bytes(canonical_json_bytes(capture_config) + b"\n")

    with pytest.raises(ValueError, match="forbidden credential material"):
        _load(case, manifest_dir)


@pytest.mark.parametrize(
    "document",
    (
        {},
        {"schema_version": "unknown"},
        {"generated_fixture": False},
        {
            "paper_only": True,
            "public_only": True,
            "new_orders_disabled": True,
        },
    ),
)
def test_capture_config_minimal_fail_open_counterexamples_are_rejected(
    document: dict[str, Any],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        builder._confirm_public_capture_config(document)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", 1),
        ("paper_only", False),
        ("public_only", 1),
        ("new_orders_disabled", False),
        ("generated_fixture", "false"),
    ),
)
def test_capture_config_rejects_wrong_types_or_false_safety_flags(
    field: str,
    value: object,
) -> None:
    document: dict[str, Any] = {
        "schema_version": builder.CAPTURE_CONFIG_SCHEMA_VERSION,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "generated_fixture": False,
    }
    document[field] = value

    with pytest.raises((TypeError, ValueError)):
        builder._confirm_public_capture_config(document)


def test_capture_config_accepts_explicit_qualified_contract() -> None:
    builder._confirm_public_capture_config(
        {
            "schema_version": builder.CAPTURE_CONFIG_SCHEMA_VERSION,
            "paper_only": True,
            "public_only": True,
            "new_orders_disabled": True,
            "generated_fixture": False,
        }
    )


def test_raw_case_rejects_symlinks_inside_immutable_capture_tree(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    capture_root = manifest_dir / str(case["capture_root"])
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (capture_root / "outside-link").symlink_to(outside)

    with pytest.raises(ValueError, match="immutable capture tree contains symlink"):
        _load(case, manifest_dir)


def test_full_raw_counterfactual_builder_is_deterministic_and_nonpromotional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases: list[dict[str, Any]] = []
    base_expiry_ms = 1_800_000_000_000
    for index, split in enumerate(("train", "validation", "test")):
        expiry_ms = base_expiry_ms + index * 3_600_000
        monkeypatch.setitem(globals(), "EXPIRY_MS", expiry_ms)
        monkeypatch.setitem(globals(), "DECISION_MS", expiry_ms - 60_000)
        case_dir = tmp_path / split
        case_dir.mkdir()
        case, _ = _build_case(
            case_dir,
            rule_received_at_ms=expiry_ms - 70_000,
            resolution_5_received_at_ms=expiry_ms + 5_000,
            resolution_15_received_at_ms=expiry_ms + 7_000,
        )
        case.update(
            {
                "event_cluster_id": f"{split}-expiry-fixture",
                "split": split,
                "capture_root": f"{split}/{case['capture_root']}",
                "predictor_root": f"{split}/{case['predictor_root']}",
                "capture_config_path": (f"{split}/{case['capture_config_path']}"),
            }
        )
        cases.append(case)

    manifest = {
        "schema_version": builder.MANIFEST_SCHEMA_VERSION,
        "preregistration_path": str(PREREGISTRATION_PATH),
        "cases": cases,
    }
    manifest_path = tmp_path / "counterfactual-manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")

    first = builder.build_counterfactual_report(manifest_path=manifest_path)
    second = builder.build_counterfactual_report(manifest_path=manifest_path)

    assert first == second
    assert first["report_sha256"] == second["report_sha256"]
    assert first["evaluation"]["status"] == "counterfactual_insufficient"
    assert first["evaluation"]["qualified_net_pnl"] is None
    assert first["evaluation"]["true_edge_gate_satisfied"] is False
    assert (
        first["evaluation"]["builder_verified_evidence"]["verified_chain_present"]
        is False
    )
    assert first["prelabel_lock"]["builder_verified_evidence"] is None
    assert all(
        context["immutable_public_capture_evidence"] is False
        for context in first["contexts"]
    )
    assert first["safety"]["orders_submitted"] == 0
    assert first["timing_policy"] == {
        "forecast_available_at_ms_verified_source": (
            "immutable_prediction_receipt_received_at_ms"
        ),
        "verified_receipt_strictly_before_common_expiry": True,
        "first_execution_eligibility": (
            "forecast_available_at_ms_plus_captured_taker_delay_ms"
        ),
        "counterfactual_computation_availability_delay_ms": 5000,
        "counterfactual_delay_is_measured_production_runtime": False,
        "counterfactual_timing_role": "paper_replay_timing_diagnostic_only",
        "effective_signal_to_execution_latency_serialized": True,
    }
    timing = first["primary"]["timing"]
    assert (
        timing["forecast_availability_basis_counts"][
            "verified_immutable_prediction_receipt"
        ]
        == 0
    )
    assert (
        timing["forecast_availability_basis_counts"][
            "preregistered_counterfactual_computation_delay"
        ]
        > 0
    )
    assert timing["counterfactual_delay_is_production_runtime_validation"] is False
    assert timing["paper_replay_only"] is True


def test_builder_without_lock_journal_stays_counterfactual_on_strict_captures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases: list[dict[str, Any]] = []
    base_expiry_ms = 1_820_000_000_000
    for index, split in enumerate(("train", "validation", "test")):
        expiry_ms = base_expiry_ms + index * 3_600_000
        monkeypatch.setitem(globals(), "EXPIRY_MS", expiry_ms)
        monkeypatch.setitem(globals(), "DECISION_MS", expiry_ms - 60_000)
        case_dir = tmp_path / split
        case_dir.mkdir()
        case, _ = _build_case(
            case_dir,
            rule_received_at_ms=expiry_ms - 70_000,
            resolution_5_received_at_ms=expiry_ms + 5_000,
            resolution_15_received_at_ms=expiry_ms + 7_000,
            generated_fixture=False,
        )
        case.update(
            {
                "event_cluster_id": f"{split}-strict-capture",
                "split": split,
                "capture_root": f"{split}/{case['capture_root']}",
                "predictor_root": f"{split}/{case['predictor_root']}",
                "capture_config_path": (f"{split}/{case['capture_config_path']}"),
            }
        )
        cases.append(case)

    manifest_path = tmp_path / "counterfactual-manifest.json"
    manifest_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": builder.MANIFEST_SCHEMA_VERSION,
                "preregistration_path": str(PREREGISTRATION_PATH),
                "cases": cases,
            }
        )
        + b"\n"
    )

    report = builder.build_counterfactual_report(manifest_path=manifest_path)

    assert all(
        context["immutable_public_capture_evidence"] is True
        for context in report["contexts"]
    )
    assert report["prelabel_lock"]["journal_supplied"] is False
    assert report["prelabel_lock"]["builder_verified_evidence"] is None
    assert report["evaluation"]["status"] == "counterfactual_insufficient"
    assert report["evaluation"]["true_edge_gate_satisfied"] is False
    assert report["evaluation"]["qualified_net_pnl"] is None


def test_generated_fixture_cannot_receive_builder_capability(tmp_path: Path) -> None:
    case, manifest_dir = _build_case(tmp_path, generated_fixture=True)
    context = _load(case, manifest_dir)[0]

    result = builder._builder_verified_evidence(
        contexts=(context,),
        primary=object(),  # type: ignore[arg-type]
        lock_index=object(),  # type: ignore[arg-type]
        preregistration_sha256="a" * 64,
        parameter_neighborhood=object(),  # type: ignore[arg-type]
    )

    assert context.capture_config_evidence["generated_fixture"] is True
    assert result is None


def test_split_policy_rejects_canonical_cluster_renamed_as_new_case(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    original = _load(case, manifest_dir)[0]
    renamed = replace(
        original,
        event_cluster_alias="test-renamed-001",
        manifest_case_index=1,
    )

    with pytest.raises(
        ValueError,
        match="one common expiry appears in multiple manifest cases",
    ):
        builder._assert_split_policy((original, renamed))


def test_split_policy_rejects_one_expiry_renamed_into_100_pairs() -> None:
    pair_ids = tuple(
        CANONICAL_EVENT_CLUSTER_PREFIX
        + hashlib.sha256(f"pair-{index}".encode()).hexdigest()
        for index in range(100)
    )
    original = builder._RawDecisionContext(
        event_cluster_id=pair_ids[0],
        event_cluster_alias="test-renamed-000",
        manifest_case_index=0,
        split="test",
        decision_tau_seconds=60,
        decision_at_ms=2_940_000,
        expiry_ms=3_000_000,
        label_available_at_ms=3_001_000,
        pair=None,  # type: ignore[arg-type]
        settlement_state=None,  # type: ignore[arg-type]
        predictor_prices=(),
        current_terminal_twap_60=D("1"),
        terminal_state_observed_at_ms=2_940_000,
        terminal_state_received_at_ms=2_940_000,
        actual_5_up=True,
        actual_15_up=True,
        replay=None,  # type: ignore[arg-type]
        market_q_5_up=D("0.5"),
        market_q_15_up=D("0.5"),
        raw_top_ask_q_5_up=D("0.5"),
        raw_top_ask_q_15_up=D("0.5"),
        clock_uncertainty_ms=0,
        capture_identity={},
        rules_and_fees={},
        opening_source_event_ids={},
        closing_source_event_id="closing",
        resolution_source_event_ids={},
        resolution_received_at_ms={},
        immutable_public_capture_evidence=False,
        capture_config_evidence={},
    )
    contexts = tuple(
        replace(
            original,
            event_cluster_id=pair_id,
            event_cluster_alias=f"test-renamed-{index:03d}",
            manifest_case_index=index,
            decision_tau_seconds=60 + index,
            decision_at_ms=original.expiry_ms - (60 + index) * 1_000,
        )
        for index, pair_id in enumerate(pair_ids)
    )

    with pytest.raises(
        ValueError,
        match="one common expiry maps to multiple 5m/15m pairs",
    ):
        builder._assert_split_policy(contexts)


def test_split_policy_rejects_same_expiry_across_splits(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path)
    original = _load(case, manifest_dir)[0]
    train = replace(original, split="train")
    test = replace(
        original,
        split="test",
        event_cluster_alias="same-expiry-test",
        manifest_case_index=1,
    )

    with pytest.raises(
        ValueError,
        match="one common expiry appears in more than one split",
    ):
        builder._assert_split_policy((train, test))


def test_split_policy_accepts_distinct_chronological_common_expiries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[Any] = []
    base_expiry_ms = 1_810_000_000_000
    for index, split in enumerate(("train", "validation", "test")):
        expiry_ms = base_expiry_ms + index * 3_600_000
        monkeypatch.setitem(globals(), "EXPIRY_MS", expiry_ms)
        monkeypatch.setitem(globals(), "DECISION_MS", expiry_ms - 60_000)
        case_dir = tmp_path / split
        case_dir.mkdir()
        case, manifest_dir = _build_case(
            case_dir,
            rule_received_at_ms=expiry_ms - 70_000,
            resolution_5_received_at_ms=expiry_ms + 5_000,
            resolution_15_received_at_ms=expiry_ms + 7_000,
        )
        case["split"] = split
        case["event_cluster_id"] = f"{split}-expiry"
        contexts.extend(
            builder._load_case_contexts(
                case,
                manifest_case_index=index,
                manifest_dir=manifest_dir,
                model=SharedTerminalModelConfig(n_paths=100),
                strategy=V07StrategyConfig(),
            )
        )

    builder._assert_split_policy(tuple(contexts))
    assert len({context.expiry_cluster_id for context in contexts}) == 3


def test_prelabel_lock_rejects_receipt_after_expiry_even_before_official_label(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path / "case")
    context = _load(case, manifest_dir)[0]
    assert context.expiry_ms < context.label_available_at_ms
    journal_root = tmp_path / "lock-journal"
    preregistration_sha256 = hashlib.sha256(
        PREREGISTRATION_PATH.read_bytes()
    ).hexdigest()
    universe_document = builder._test_universe_document(
        (context,),
        preregistration_sha256=preregistration_sha256,
    )
    universe_sha256 = hashlib.sha256(
        canonical_json_bytes(universe_document)
    ).hexdigest()
    universe_receipt_ms = context.decision_at_ms - 1_000
    late_prediction_receipt_ms = context.expiry_ms + 1_000
    assert late_prediction_receipt_ms < context.label_available_at_ms
    _freeze_batch(
        journal_root,
        source=builder.LOCK_JOURNAL_SOURCE,
        batch_id="post-expiry-prediction-receipt",
        rows=[
            {
                "received_at_ms": universe_receipt_ms,
                "event_at_ms": universe_receipt_ms,
                "payload": {
                    "schema_version": builder.LOCK_JOURNAL_SCHEMA_VERSION,
                    "kind": "test_universe_lock",
                    "test_universe_sha256": universe_sha256,
                    "locked_at_ms": universe_receipt_ms,
                },
            },
            {
                "received_at_ms": late_prediction_receipt_ms,
                "event_at_ms": context.decision_at_ms,
                "payload": {
                    "schema_version": builder.LOCK_JOURNAL_SCHEMA_VERSION,
                    "kind": "forecast_decision_lock",
                    "test_universe_sha256": universe_sha256,
                    "canonical_pair_id": context.canonical_pair_id,
                    "expiry_ms": context.expiry_ms,
                    "expiry_cluster_id": context.expiry_cluster_id,
                    "decision_tau_seconds": context.decision_tau_seconds,
                    "decision_at_ms": context.decision_at_ms,
                    "prediction_locked_at_ms": context.decision_at_ms,
                    "forecast_payload_sha256": "a" * 64,
                    "decision_payload_sha256": "b" * 64,
                },
            },
        ],
    )

    with pytest.raises(
        ValueError,
        match="forecast/decision lock receipt is not strictly pre-expiry",
    ):
        builder._load_prelabel_lock_index(
            str(journal_root),
            manifest_dir=tmp_path,
            contexts=(context,),
            preregistration_sha256=preregistration_sha256,
        )


def test_prelabel_lock_rejects_retrospectively_selected_test_universe(
    tmp_path: Path,
) -> None:
    case, manifest_dir = _build_case(tmp_path / "case")
    context = _load(case, manifest_dir)[0]
    journal_root = tmp_path / "lock-journal"
    preregistration_sha256 = hashlib.sha256(
        PREREGISTRATION_PATH.read_bytes()
    ).hexdigest()
    universe_document = builder._test_universe_document(
        (context,),
        preregistration_sha256=preregistration_sha256,
    )
    universe_sha256 = hashlib.sha256(
        canonical_json_bytes(universe_document)
    ).hexdigest()
    late_receipt_ms = context.decision_at_ms + 1
    _freeze_batch(
        journal_root,
        source=builder.LOCK_JOURNAL_SOURCE,
        batch_id="late-universe-lock",
        rows=[
            {
                "received_at_ms": late_receipt_ms,
                "event_at_ms": late_receipt_ms,
                "payload": {
                    "schema_version": builder.LOCK_JOURNAL_SCHEMA_VERSION,
                    "kind": "test_universe_lock",
                    "test_universe_sha256": universe_sha256,
                    "locked_at_ms": late_receipt_ms,
                },
            }
        ],
    )

    with pytest.raises(
        ValueError,
        match="test universe was selected retrospectively after the test began",
    ):
        builder._load_prelabel_lock_index(
            str(journal_root),
            manifest_dir=tmp_path,
            contexts=(context,),
            preregistration_sha256=preregistration_sha256,
        )
