from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts.build_btc_twap_relative_value_v07_counterfactual import (
    _validate_pair_targets,
)
from scripts.build_btc_twap_structural_shadow_report import (
    INPUT_SCHEMA,
    _capture_inventory,
    _compact_top_book_events,
    _event,
    _event_sort_key,
    _locked_action_document,
    _source_evidence_document,
    _trade_events,
    _verify_capture_attempt,
    _write_report_atomic,
    build_report_from_payload,
    main,
)
from src.edge_lab.btc_twap_relative_value import PairSettlementState
from src.edge_lab.btc_twap_relative_value_v07 import (
    canonical_event_cluster_id,
    canonical_expiry_cluster_id,
)
from src.edge_lab.btc_twap_structural_shadow import (
    STRUCTURAL_SHADOW_REPORT_SCHEMA,
    StructuralCaptureVerification,
)
from src.edge_lab.data_store import CaptureStore, canonical_json_bytes
from src.edge_lab.execution import BookEvent, TradeEvent
from src.edge_lab.settlement_regime import V06_SETTLEMENT_REGIME_ID
from tests.test_btc_twap_relative_value_v07_counterfactual import (
    DECISION_MS,
    EXPIRY_MS,
    _freeze_batch,
    _target,
)
from tests.test_btc_twap_relative_value_v07_replay import _v07_pair

D = Decimal


def _capture_verification(attempt: dict[str, object]) -> StructuralCaptureVerification:
    attempt_id = str(attempt["attempt_id"])
    return StructuralCaptureVerification(
        attempt_id=attempt_id,
        capture_attempt_id=f"capture-{attempt_id}",
        capture_root=str(attempt["capture_root"]),
        capture_tree_sha256=hashlib.sha256(
            f"capture-tree:{attempt_id}".encode()
        ).hexdigest(),
        manifest_count=1,
        record_count=1,
        book_event_count=0,
        trade_event_count=2,
        complete_window=True,
    )


@pytest.fixture
def _stub_capture_verifier(monkeypatch) -> None:
    def verify(**kwargs):
        return _capture_verification(dict(kwargs["attempt"]))

    monkeypatch.setattr(
        "scripts.build_btc_twap_structural_shadow_report._verify_capture_attempt",
        verify,
    )


def _contract_payload(contract) -> dict[str, object]:
    return {
        "horizon": contract.horizon,
        "slug": contract.slug,
        "market_id": contract.market_id,
        "condition_id": contract.condition_id,
        "up_token_id": contract.up_token_id,
        "down_token_id": contract.down_token_id,
        "opens_at_ms": contract.opens_at_ms,
        "closes_at_ms": contract.closes_at_ms,
        "twap_window_seconds": contract.twap_window_seconds,
        "source_topic": contract.source_topic,
        "resolution_source": contract.resolution_source,
        "tick_size": str(contract.tick_size),
        "minimum_order_size": str(contract.minimum_order_size),
        "fee_schedule": {
            "rate": str(contract.fee_schedule.rate),
            "exponent": str(contract.fee_schedule.exponent),
            "taker_only": contract.fee_schedule.taker_only,
        },
        "taker_delay_ms": contract.taker_delay_ms,
        "accepting_orders": contract.accepting_orders,
        "rule_hash": contract.rule_hash,
        "settlement_regime": contract.settlement_regime,
    }


def _book_payload(bid: str, ask: str) -> dict[str, object]:
    return {
        "bids": [[bid, "5"]],
        "asks": [[ask, "5"]],
        "timestamp_ms": 0,
        "tick_size": "0.01",
        "minimum_order_size": "5",
    }


def _payload() -> dict[str, object]:
    pair = _v07_pair()
    return {
        "schema_version": INPUT_SCHEMA,
        "neutral_disposition": "passive_wait",
        "rolling_window_size": 20,
        "minimum_expiries": 200,
        "locked_zero_cohorts": [],
        "attempts": [
            {
                "attempt_id": "expiry-1",
                "capture_root": "/captures/capture-expiry-1",
                "pair": {
                    "market_5": _contract_payload(pair.market_5),
                    "market_15": _contract_payload(pair.market_15),
                },
                "settlement_state": {
                    "market_5_rule_hash": pair.market_5.rule_hash,
                    "market_15_rule_hash": pair.market_15.rule_hash,
                    "market_5_open_timestamp_ms": pair.market_5.opens_at_ms,
                    "market_15_open_timestamp_ms": pair.market_15.opens_at_ms,
                    "strike_5": "101",
                    "strike_15": "100",
                    "opening_5_source_event_id": "open-5",
                    "opening_15_source_event_id": "open-15",
                },
                "action": "long_15_up_long_5_down",
                "quantity": "5",
                "submitted_at_ms": 0,
                "max_book_age_ms": 1000,
                "initial_cash": "20",
                "actual_outcome": {
                    "actual_5_up": True,
                    "actual_15_up": True,
                },
                "books": {
                    pair.market_15.up_token_id: _book_payload("0.40", "0.41"),
                    pair.market_15.down_token_id: _book_payload("0.60", "0.61"),
                    pair.market_5.up_token_id: _book_payload("0.42", "0.43"),
                    pair.market_5.down_token_id: _book_payload("0.58", "0.59"),
                },
                "public_events": [
                    {
                        "kind": "trade",
                        "token_id": pair.market_15.up_token_id,
                        "timestamp_ms": 100,
                        "price": "0.40",
                        "quantity": "10",
                        "aggressor_side": "sell",
                        "source_event_id": "trade-15",
                    },
                    {
                        "kind": "trade",
                        "token_id": pair.market_5.down_token_id,
                        "timestamp_ms": 101,
                        "price": "0.58",
                        "quantity": "10",
                        "aggressor_side": "sell",
                        "source_event_id": "trade-5",
                    },
                ],
            }
        ],
    }


def _recorder_payload(
    source: str,
    event_type: str,
    payload: Mapping[str, Any],
    *,
    kind: str = "event",
) -> dict[str, Any]:
    return {
        "source": source,
        "schema_version": f"{source}.fixture.v1",
        "kind": kind,
        "event_type": event_type,
        "payload": dict(payload),
    }


def _http_response(
    *,
    resource: str,
    request_key: str,
    raw_json: Any,
    source: str,
    url: str,
    request_params: Mapping[str, Any],
    page_number: int = 1,
) -> dict[str, Any]:
    body_utf8 = json.dumps(raw_json, separators=(",", ":"))
    body = body_utf8.encode("utf-8")
    return {
        "resource": resource,
        "request_key": request_key,
        "page_number": page_number,
        "raw_json": raw_json,
        "provenance": {
            "source": source,
            "method": "GET",
            "url": url,
            "request_params": dict(request_params),
            "status_code": 200,
            "requested_at_epoch_seconds": "0",
            "received_at_epoch_seconds": "0",
            "attempt": 1,
            "response_headers": {"content-type": "application/json"},
            "body_utf8": body_utf8,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_bytes": len(body),
        },
    }


def _event_payload(event: BookEvent | TradeEvent) -> dict[str, object]:
    if isinstance(event, BookEvent):
        return {
            "kind": "book",
            "token_id": event.token_id,
            "timestamp_ms": event.timestamp_ms,
            "source_timestamp_ms": event.source_timestamp_ms,
            "best_bid": None if event.best_bid is None else str(event.best_bid),
            "best_ask": None if event.best_ask is None else str(event.best_ask),
            "tick_size": str(event.tick_size),
            "source_event_id": event.source_event_id,
        }
    return {
        "kind": "trade",
        "token_id": event.token_id,
        "timestamp_ms": event.timestamp_ms,
        "price": str(event.price),
        "quantity": str(event.quantity),
        "aggressor_side": event.aggressor_side.value,
        "source_event_id": event.source_event_id,
    }


def _snapshot_payload(snapshot) -> dict[str, object]:
    return {
        "bids": [[str(level.price), str(level.size)] for level in snapshot.bids],
        "asks": [[str(level.price), str(level.size)] for level in snapshot.asks],
        "timestamp_ms": snapshot.timestamp_ms,
        "tick_size": str(snapshot.tick_size),
        "minimum_order_size": str(snapshot.minimum_order_size),
    }


def _build_real_capture_payload(
    tmp_path: Path,
) -> tuple[dict[str, object], dict[str, object], StructuralCaptureVerification]:
    root = tmp_path / "capture-attempt-1"
    root.mkdir(parents=True)
    target_5, rule_5 = _target("5m")
    target_15, rule_15 = _target("15m")
    pair, _ = _validate_pair_targets((target_5, target_15))
    config = {
        "schema_version": "edge-lab-forward-capture-config.v1",
        "data_root": str(root.resolve()),
        "capture_started_at_ms": target_15["opens_at_ms"] - 1_000,
        "evidence_track_id": "btc_5m_15m_edge_readiness_v08_2026_08_18",
        "settlement_regime_id": V06_SETTLEMENT_REGIME_ID,
        "clock_sync": {"causal_receipt_offset_ms": 0},
        "asset_ids": [
            target_15["up_token_id"],
            target_15["down_token_id"],
            target_5["up_token_id"],
            target_5["down_token_id"],
        ],
        "condition_ids": [target_15["condition_id"], target_5["condition_id"]],
        "rule_market_ids": [target_15["market_id"], target_5["market_id"]],
        "rtds_subscriptions": [
            {
                "topic": "crypto_prices_twap_sixty",
                "type": "update",
                "filters": '{"symbol":"btc/usd"}',
            }
        ],
        "snapshot_intervals": {"clob": 30, "trades": 5, "rules": 30},
        "checkpoint_every_records": 10_000,
        "max_records_per_batch": 5_000,
        "gamma_page_limit": 10,
        "gamma_max_pages": 1,
        "reward_page_limit": 1,
        "reward_max_pages": 1,
        "targets": [target_15, target_5],
        "persist_raw_clob_frames": False,
        "persist_reconstructed_full_depth_frames": False,
        "persist_top_of_book_changes": True,
    }
    (root / "capture-config.json").write_bytes(canonical_json_bytes(config) + b"\n")
    rtds_rows = []
    for timestamp_ms in range(target_15["opens_at_ms"], EXPIRY_MS + 1, 1_000):
        value = (
            "101"
            if timestamp_ms == target_5["opens_at_ms"]
            else "100.5"
            if timestamp_ms == EXPIRY_MS
            else "100"
        )
        rtds_rows.append(
            {
                "received_at_ms": timestamp_ms + 10,
                "event_at_ms": timestamp_ms,
                "payload": _recorder_payload(
                    "rtds_ws",
                    "crypto_prices.update",
                    {
                        "topic": "crypto_prices_twap_sixty",
                        "payload": {
                            "symbol": "btc/usd",
                            "timestamp": timestamp_ms,
                            "value": value,
                        },
                    },
                ),
            }
        )
    rtds_ids = _freeze_batch(
        root,
        source="rtds_ws",
        batch_id="complete-twap-window",
        rows=rtds_rows,
    )
    _freeze_batch(
        root,
        source="rules_http",
        batch_id="rules",
        rows=[
            {
                "received_at_ms": DECISION_MS - 1_000,
                "event_at_ms": DECISION_MS - 1_000,
                "payload": _recorder_payload(
                    "rules_http",
                    "rules_snapshot",
                    {"responses": [{"raw_json": rule_5}, {"raw_json": rule_15}]},
                    kind="snapshot",
                ),
            }
        ],
    )
    prices = {
        target_15["up_token_id"]: ("0.40", "0.41"),
        target_15["down_token_id"]: ("0.59", "0.60"),
        target_5["up_token_id"]: ("0.41", "0.42"),
        target_5["down_token_id"]: ("0.58", "0.59"),
    }
    book_responses = []
    for token_id, (bid, ask) in prices.items():
        book_responses.append(
            {
                "resource": "clob_book",
                "request_key": token_id,
                "raw_json": {
                    "asset_id": token_id,
                    "timestamp": str(DECISION_MS),
                    "bids": [{"price": bid, "size": "5"}],
                    "asks": [{"price": ask, "size": "5"}],
                },
            }
        )
    _freeze_batch(
        root,
        source="clob_http",
        batch_id="decision-books",
        rows=[
            {
                "received_at_ms": DECISION_MS,
                "event_at_ms": DECISION_MS,
                "payload": _recorder_payload(
                    "clob_http",
                    "clob_snapshot",
                    {
                        "schema_version": "edge-lab-public-snapshot.v1",
                        "snapshot_kind": "clob",
                        "requested_asset_ids": list(prices),
                        "responses": book_responses,
                        "truncated_resources": [],
                    },
                    kind="snapshot",
                ),
            }
        ],
    )
    trade_15 = {
        "conditionId": target_15["condition_id"],
        "asset": target_15["up_token_id"],
        "side": "SELL",
        "size": "10",
        "price": "0.40",
        "timestamp": DECISION_MS // 1_000 + 1,
        "transactionHash": "0x" + "11" * 32,
        "proxyWallet": "0x" + "12" * 20,
        "outcome": "Up",
        "outcomeIndex": 0,
    }
    trade_5 = {
        "conditionId": target_5["condition_id"],
        "asset": target_5["down_token_id"],
        "side": "SELL",
        "size": "10",
        "price": "0.58",
        "timestamp": DECISION_MS // 1_000 + 1,
        "transactionHash": "0x" + "22" * 32,
        "proxyWallet": "0x" + "23" * 20,
        "outcome": "Down",
        "outcomeIndex": 1,
    }
    trade_responses = [
        _http_response(
            resource="data_api_trades",
            request_key=target_15["condition_id"],
            raw_json=[trade_15],
            source="data_api_trades",
            url="https://data-api.polymarket.com/trades",
            request_params={
                "market": target_15["condition_id"],
                "takerOnly": "true",
                "limit": 1_000,
                "offset": 0,
            },
        ),
        _http_response(
            resource="data_api_trades",
            request_key=target_5["condition_id"],
            raw_json=[trade_5],
            source="data_api_trades",
            url="https://data-api.polymarket.com/trades",
            request_params={
                "market": target_5["condition_id"],
                "takerOnly": "true",
                "limit": 1_000,
                "offset": 0,
            },
        ),
    ]
    _freeze_batch(
        root,
        source="trades_http",
        batch_id="public-trades",
        rows=[
            {
                "received_at_ms": received_at_ms,
                "event_at_ms": received_at_ms,
                "payload": {
                    **_recorder_payload(
                        "trades_http",
                        "trades_snapshot",
                        {
                            "schema_version": "edge-lab-public-snapshot.v1",
                            "snapshot_kind": "trades",
                            "requested_asset_ids": [],
                            "responses": trade_responses,
                            "truncated_resources": [],
                        },
                        kind="snapshot",
                    ),
                    "schema_version": "trades-http.snapshot.v1",
                },
            }
            for received_at_ms in range(
                DECISION_MS + 1_010,
                EXPIRY_MS + 1_011,
                5_000,
            )
        ],
    )
    _freeze_batch(
        root,
        source="clob_market_ws",
        batch_id="resolutions",
        rows=[
            {
                "received_at_ms": DECISION_MS + 500,
                "event_at_ms": DECISION_MS + 500,
                "payload": _recorder_payload(
                    "clob_market_ws",
                    "best_bid_ask",
                    {
                        "event_type": "best_bid_ask",
                        "timestamp": str(DECISION_MS + 500),
                        "changes": [
                            {
                                "asset_id": target_15["up_token_id"],
                                "best_bid": "0.40",
                                "best_ask": "0.41",
                            }
                        ],
                    },
                ),
            },
            {
                "received_at_ms": EXPIRY_MS + 1_000,
                "event_at_ms": EXPIRY_MS + 1_000,
                "payload": _recorder_payload(
                    "clob_market_ws",
                    "market_resolved",
                    {
                        "id": target_5["market_id"],
                        "market": target_5["condition_id"],
                        "winning_asset_id": target_5["down_token_id"],
                        "winning_outcome": "Down",
                    },
                ),
            },
            {
                "received_at_ms": EXPIRY_MS + 1_001,
                "event_at_ms": EXPIRY_MS + 1_001,
                "payload": _recorder_payload(
                    "clob_market_ws",
                    "market_resolved",
                    {
                        "id": target_15["market_id"],
                        "market": target_15["condition_id"],
                        "winning_asset_id": target_15["up_token_id"],
                        "winning_outcome": "Up",
                    },
                ),
            },
        ],
    )
    store = CaptureStore(root)
    manifests = tuple((root / "raw").rglob("*.manifest.json"))
    manifest_documents = [
        json.loads(path.read_text(encoding="utf-8")) for path in manifests
    ]
    summary = {
        "schema_version": "btc-twap-compact-forward-capture-summary.v1",
        "capture_error": None,
        "recorder_leg_failures": [],
        "manifest_count": len(manifests),
        "manifest_record_count": sum(
            item["record_count"] for item in manifest_documents
        ),
        "target_count": 2,
        "asset_count": 4,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "authenticated_endpoints_used": 0,
        "orders_submitted": 0,
        "integrity": store.audit_integrity(),
    }
    (root / "capture-summary.json").write_bytes(canonical_json_bytes(summary) + b"\n")
    books = {
        token_id: _v07_pair_book(token_id, bid=bid, ask=ask)
        for token_id, (bid, ask) in prices.items()
    }
    settlement_state = PairSettlementState(
        market_5_rule_hash=pair.market_5.rule_hash,
        market_15_rule_hash=pair.market_15.rule_hash,
        market_5_open_timestamp_ms=pair.market_5.opens_at_ms,
        market_15_open_timestamp_ms=pair.market_15.opens_at_ms,
        strike_5=D("101"),
        strike_15=D("100"),
        opening_5_source_event_id=rtds_ids[600],
        opening_15_source_event_id=rtds_ids[0],
    )
    public_events = tuple(
        sorted(
            (
                *_compact_top_book_events(
                    root,
                    tick_by_token={token_id: D("0.01") for token_id in prices},
                    submitted_at_ms=DECISION_MS,
                    expiry_ms=EXPIRY_MS,
                ),
                *_trade_events(
                    root,
                    pair=pair,
                    submitted_at_ms=DECISION_MS,
                    expiry_ms=EXPIRY_MS,
                    poll_interval_ms=5_000,
                ),
            ),
            key=_event_sort_key,
        )
    )
    attempt_id = canonical_expiry_cluster_id(EXPIRY_MS)
    attempt = {
        "attempt_id": attempt_id,
        "capture_root": str(root.resolve()),
        "pair": {
            "market_5": _contract_payload(pair.market_5),
            "market_15": _contract_payload(pair.market_15),
        },
        "settlement_state": {
            "market_5_rule_hash": pair.market_5.rule_hash,
            "market_15_rule_hash": pair.market_15.rule_hash,
            "market_5_open_timestamp_ms": pair.market_5.opens_at_ms,
            "market_15_open_timestamp_ms": pair.market_15.opens_at_ms,
            "strike_5": "101",
            "strike_15": "100",
            "opening_5_source_event_id": rtds_ids[600],
            "opening_15_source_event_id": rtds_ids[0],
        },
        "action": "long_15_up_long_5_down",
        "quantity": "5",
        "submitted_at_ms": DECISION_MS,
        "max_book_age_ms": 2_000,
        "initial_cash": "20",
        "actual_outcome": {"actual_5_up": False, "actual_15_up": True},
        "books": {
            token_id: _snapshot_payload(snapshot)
            for token_id, snapshot in books.items()
        },
        "public_events": [_event_payload(event) for event in public_events],
    }
    admission = {
        "common_expiry_id": attempt_id,
        "canonical_pair_id": canonical_event_cluster_id(pair),
        "capture_attempt_id": root.name,
        "expiry_ms": EXPIRY_MS,
        "market_5_id": pair.market_5.market_id,
        "market_15_id": pair.market_15.market_id,
        "condition_5_id": pair.market_5.condition_id,
        "condition_15_id": pair.market_15.condition_id,
    }
    verification = _verify_capture_attempt(
        attempt=attempt,
        pair=pair,
        settlement_state=settlement_state,
        books=books,
        public_events=public_events,
        hedge_books={},
        unwind_books={},
        admission=admission,
    )
    evidence_sha256 = hashlib.sha256(
        canonical_json_bytes(_source_evidence_document(attempt, verification))
    ).hexdigest()
    audit = {
        "schema_version": "btc-5m-15m-readiness-v08-rolling-audit.v1",
        "journal_root": "test-journal",
        "valid": True,
        "errors": [],
        "policy": {"preregistration_sha256": "f" * 64},
        "admitted_common_expiry_count": 1,
        "finalized_common_expiry_count": 1,
        "unfinished_common_expiry_count": 0,
        "admissions": {attempt_id: admission},
        "decisions": {
            f"{attempt_id}:60": {
                "common_expiry_id": attempt_id,
                "action": attempt["action"],
                "decision_at_ms": DECISION_MS,
                "action_payload_sha256": hashlib.sha256(
                    canonical_json_bytes(_locked_action_document(attempt))
                ).hexdigest(),
            }
        },
        "outcomes": {
            attempt_id: {
                "common_expiry_id": attempt_id,
                "outcome": "complete",
                "clean": True,
                "realized_net_pnl": "5.10",
                "evidence_sha256": evidence_sha256,
            }
        },
        "denominator_includes_no_trade_no_fill_and_dirty": True,
    }
    payload = {
        "schema_version": INPUT_SCHEMA,
        "neutral_disposition": "passive_wait",
        "rolling_window_size": 20,
        "minimum_expiries": 200,
        "locked_zero_cohorts": [],
        "attempts": [attempt],
    }
    return payload, audit, verification


def _v07_pair_book(token_id: str, *, bid: str, ask: str):
    from src.edge_lab.btc_twap_relative_value import OrderBookSnapshot

    return OrderBookSnapshot.from_tuples(
        token_id,
        bids=((D(bid), D("5")),),
        asks=((D(ask), D("5")),),
        timestamp_ms=DECISION_MS,
        tick_size=D("0.01"),
        minimum_order_size=D("5"),
    )


def _rolling_audit(
    payload: dict[str, object],
    *,
    preregistration_sha256: str,
) -> dict[str, object]:
    attempt = payload["attempts"][0]  # type: ignore[index]
    pair = attempt["pair"]  # type: ignore[index]
    market_5 = pair["market_5"]  # type: ignore[index]
    market_15 = pair["market_15"]  # type: ignore[index]
    attempt_id = attempt["attempt_id"]  # type: ignore[index]
    evidence_sha256 = hashlib.sha256(
        canonical_json_bytes(
            _source_evidence_document(attempt, _capture_verification(attempt))
        )
    ).hexdigest()
    return {
        "schema_version": "btc-5m-15m-readiness-v08-rolling-audit.v1",
        "journal_root": "test-journal",
        "valid": True,
        "errors": [],
        "policy": {"preregistration_sha256": preregistration_sha256},
        "admitted_common_expiry_count": 1,
        "finalized_common_expiry_count": 1,
        "unfinished_common_expiry_count": 0,
        "admissions": {
            attempt_id: {
                "common_expiry_id": attempt_id,
                "capture_attempt_id": f"capture-{attempt_id}",
                "expiry_ms": market_5["closes_at_ms"],
                "market_5_id": market_5["market_id"],
                "market_15_id": market_15["market_id"],
                "condition_5_id": market_5["condition_id"],
                "condition_15_id": market_15["condition_id"],
            }
        },
        "decisions": {
            f"{attempt_id}:60": {
                "common_expiry_id": attempt_id,
                "action": attempt["action"],  # type: ignore[index]
                "decision_at_ms": attempt["submitted_at_ms"],  # type: ignore[index]
                "action_payload_sha256": hashlib.sha256(
                    canonical_json_bytes(_locked_action_document(attempt))
                ).hexdigest(),
            }
        },
        "outcomes": {
            attempt_id: {
                "common_expiry_id": attempt_id,
                "outcome": "complete",
                "clean": True,
                "realized_net_pnl": "0.10",
                "evidence_sha256": evidence_sha256,
            }
        },
        "denominator_includes_no_trade_no_fill_and_dirty": True,
    }


def test_cli_builder_roundtrip_writes_hashed_report(
    tmp_path,
    capsys,
    monkeypatch,
    _stub_capture_verifier,
) -> None:
    payload = _payload()
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    preregistration_path = tmp_path / "PREREGISTRATION.json"
    preregistration_path.write_text("{}\n", encoding="utf-8")
    preregistration_sha256 = hashlib.sha256(
        preregistration_path.read_bytes()
    ).hexdigest()
    monkeypatch.setattr(
        "scripts.build_btc_twap_structural_shadow_report.audit_rolling_shadow",
        lambda _path: _rolling_audit(
            payload,
            preregistration_sha256=preregistration_sha256,
        ),
    )

    code = main(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(tmp_path),
            "--journal-root",
            str(tmp_path / "journal"),
            "--preregistration",
            str(preregistration_path),
        ]
    )

    assert code == 0
    written_path = capsys.readouterr().out.strip()
    report_path = tmp_path / written_path.split("\\")[-1].split("/")[-1]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == STRUCTURAL_SHADOW_REPORT_SCHEMA
    assert report["report_sha256"] in report_path.name
    assert (
        report["source_input_sha256"]
        == hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    )
    assert report["neutral_shadow_evidence"]["realized_net_pnl"] == "0.10"


def test_cli_builder_rejects_missing_evidence(_stub_capture_verifier) -> None:
    payload = _payload()
    payload["attempts"][0].pop("books")  # type: ignore[index]
    preregistration_sha256 = "f" * 64

    with pytest.raises(ValueError, match="schema mismatch"):
        build_report_from_payload(
            payload,
            rolling_audit=_rolling_audit(
                payload,
                preregistration_sha256=preregistration_sha256,
            ),
            preregistration_sha256=preregistration_sha256,
        )


def test_report_rejects_forged_receipt_hash_or_weakened_sample_gate(
    _stub_capture_verifier,
) -> None:
    payload = _payload()
    preregistration_sha256 = "f" * 64
    audit = _rolling_audit(
        payload,
        preregistration_sha256=preregistration_sha256,
    )
    audit["outcomes"]["expiry-1"]["evidence_sha256"] = "0" * 64  # type: ignore[index]

    with pytest.raises(ValueError, match="source replay evidence"):
        build_report_from_payload(
            payload,
            rolling_audit=audit,
            preregistration_sha256=preregistration_sha256,
        )

    payload = _payload()
    payload["minimum_expiries"] = 1
    with pytest.raises(ValueError, match="registered 200-expiry gate"):
        build_report_from_payload(
            payload,
            rolling_audit=_rolling_audit(
                payload,
                preregistration_sha256=preregistration_sha256,
            ),
            preregistration_sha256=preregistration_sha256,
        )

    payload = _payload()
    payload["neutral_disposition"] = "taker_hedge"
    with pytest.raises(ValueError, match="registered passive_wait"):
        build_report_from_payload(
            payload,
            rolling_audit=_rolling_audit(
                payload,
                preregistration_sha256=preregistration_sha256,
            ),
            preregistration_sha256=preregistration_sha256,
        )


def test_real_capture_verifier_binds_finalized_manifests_and_complete_events(
    tmp_path: Path,
) -> None:
    payload, audit, verification = _build_real_capture_payload(tmp_path)

    report = build_report_from_payload(
        payload,
        rolling_audit=audit,
        preregistration_sha256="f" * 64,
    )

    captured = report["attempts"][0]["capture_verification"]
    assert captured["capture_tree_sha256"] == verification.capture_tree_sha256
    assert captured["manifest_count"] == 5
    assert captured["record_count"] == 919
    assert captured["book_event_count"] == 1
    assert captured["trade_event_count"] == 2
    assert captured["verified_from_finalized_recorder_manifests"] is True


def test_real_capture_verifier_rejects_omitted_flow_or_tampered_raw_data(
    tmp_path: Path,
) -> None:
    payload, audit, _verification = _build_real_capture_payload(tmp_path)
    payload["attempts"][0]["public_events"].pop()  # type: ignore[index]

    with pytest.raises(ValueError, match="complete captured replay"):
        build_report_from_payload(
            payload,
            rolling_audit=audit,
            preregistration_sha256="f" * 64,
        )

    payload, audit, _verification = _build_real_capture_payload(tmp_path / "tampered")
    capture_root = Path(payload["attempts"][0]["capture_root"])  # type: ignore[index]
    raw_path = next((capture_root / "raw" / "trades_http").glob("*.jsonl"))
    raw_path.chmod(0o644)
    raw_path.write_bytes(raw_path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="integrity|counts/checksum"):
        build_report_from_payload(
            payload,
            rolling_audit=audit,
            preregistration_sha256="f" * 64,
        )


def test_trade_events_use_source_timestamps_and_accept_contiguous_multipage(
    monkeypatch,
) -> None:
    pair = _v07_pair()
    decision_ms = 0
    expiry_ms = 20_000

    def trade_row(index: int) -> dict[str, object]:
        return {
            "conditionId": pair.market_15.condition_id,
            "asset": pair.market_15.up_token_id,
            "side": "SELL",
            "size": "1",
            "price": "0.40",
            "timestamp": 10,
            "transactionHash": "0x" + format(index, "064x"),
            "proxyWallet": "0x" + format(index, "040x"),
            "outcome": "Up",
            "outcomeIndex": 0,
        }

    first_page = [trade_row(index) for index in range(1_000)]
    late_trade = {
        **trade_row(1_001),
        "timestamp": 19,
    }

    def snapshot(received_at_ms: int) -> dict[str, object]:
        return {
            "received_at": f"1970-01-01T00:00:{received_at_ms // 1000:02d}Z",
            "payload": {
                "source": "trades_http",
                "kind": "snapshot",
                "event_type": "trades_snapshot",
                "schema_version": "trades-http.snapshot.v1",
                "payload": {
                    "schema_version": "edge-lab-public-snapshot.v1",
                    "snapshot_kind": "trades",
                    "requested_asset_ids": [],
                    "truncated_resources": [],
                    "responses": [
                        _http_response(
                            resource="data_api_trades",
                            request_key=pair.market_15.condition_id,
                            raw_json=first_page,
                            source="data_api_trades",
                            url="https://data-api.polymarket.com/trades",
                            request_params={
                                "market": pair.market_15.condition_id,
                                "takerOnly": "true",
                                "limit": 1_000,
                                "offset": 0,
                            },
                            page_number=1,
                        ),
                        _http_response(
                            resource="data_api_trades",
                            request_key=pair.market_15.condition_id,
                            raw_json=[late_trade],
                            source="data_api_trades",
                            url="https://data-api.polymarket.com/trades",
                            request_params={
                                "market": pair.market_15.condition_id,
                                "takerOnly": "true",
                                "limit": 1_000,
                                "offset": 1_000,
                            },
                            page_number=2,
                        ),
                        _http_response(
                            resource="data_api_trades",
                            request_key=pair.market_5.condition_id,
                            raw_json=[],
                            source="data_api_trades",
                            url="https://data-api.polymarket.com/trades",
                            request_params={
                                "market": pair.market_5.condition_id,
                                "takerOnly": "true",
                                "limit": 1_000,
                                "offset": 0,
                            },
                            page_number=1,
                        ),
                    ],
                },
            },
        }

    monkeypatch.setattr(
        "scripts.build_btc_twap_structural_shadow_report._records",
        lambda _root, source: (
            snapshot(1_000),
            snapshot(6_000),
            snapshot(11_000),
            snapshot(16_000),
            snapshot(21_000),
        )
        if source == "trades_http"
        else (),
    )
    monkeypatch.setattr(
        "scripts.build_btc_twap_structural_shadow_report._receipt_clock_offset_ms",
        lambda _root: 0,
    )

    events = _trade_events(
        Path("."),
        pair=pair,
        submitted_at_ms=decision_ms,
        expiry_ms=expiry_ms,
        poll_interval_ms=5_000,
    )

    assert len(events) == 1_001
    assert max(event.timestamp_ms for event in events) == 19_000


def test_event_accepts_single_sided_best_bid_ask_input() -> None:
    event = _event(
        {
            "kind": "book",
            "token_id": "token-1",
            "timestamp_ms": 100,
            "source_timestamp_ms": 99,
            "best_bid": "0.40",
            "tick_size": "0.01",
            "source_event_id": "book-1",
        },
        index=0,
    )

    assert isinstance(event, BookEvent)
    assert event.best_bid == D("0.40")
    assert event.best_ask is None


def test_write_report_atomic_propagates_directory_fsync_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "scripts.build_btc_twap_structural_shadow_report._fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("fsync failed")),
    )

    with pytest.raises(OSError, match="fsync failed"):
        _write_report_atomic(
            tmp_path,
            {"report_sha256": "a" * 64},
        )


def test_capture_inventory_rejects_reparse_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload, _audit, _verification = _build_real_capture_payload(tmp_path / "reparse")
    capture_root = Path(payload["attempts"][0]["capture_root"])  # type: ignore[index]
    target = next((capture_root / "raw").rglob("*.jsonl"))
    original_lstat = Path.lstat

    class _FakeStat:
        def __init__(self, metadata):
            self.st_mode = metadata.st_mode
            self.st_file_attributes = 0x400

    def fake_lstat(self: Path):
        metadata = original_lstat(self)
        if self == target:
            return _FakeStat(metadata)
        return metadata

    monkeypatch.setattr(Path, "lstat", fake_lstat)

    with pytest.raises(ValueError, match="reparse|junction|capture tree"):
        _capture_inventory(capture_root)
