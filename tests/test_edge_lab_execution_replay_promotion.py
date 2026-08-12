"""Behavioral tests for the strict execution-replay promotion seam.

The fixture mirrors finalized recorder envelopes, but is deliberately marked
``fixture``.  It validates the offline evidence pipeline and must never be
reported as a completed real-market tracer.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import src.edge_lab.execution_replay_assembly as replay_assembly
from src.edge_lab.data_store import CaptureStore, canonical_json_bytes
from src.edge_lab.execution_replay_promotion import (
    PromotionRejection,
    _load_frozen_evidence,
    _public_trades,
    _read_request,
    _strict_target,
    verify_and_promote_execution_replay,
)
from src.edge_lab.execution_replay_freeze import CAPTURE_FREEZE_SCHEMA
from src.edge_lab.execution_replay_freeze_builder import (
    build_production_capture_freeze,
)
from src.edge_lab.execution_replay_assembly import (
    ReplayAssemblyError,
    build_replay_request_for_candidate,
    discover_replay_candidates,
)
from src.edge_lab.short_crypto_catalog import parse_short_crypto_announcement


OPEN_MS = 1_784_899_000_000
CLOSE_MS = OPEN_MS + 300_000
CONDITION_ID = "0x" + "ab" * 32
UP_TOKEN_ID = "1" * 76
DOWN_TOKEN_ID = "2" * 76
TRANSACTION_HASH = "0x" + "cd" * 32


def _iso(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _recorder_payload(
    *,
    source: str,
    schema_version: str,
    event_type: str,
    received_at_ms: int,
    payload: object,
    connection_id: str = "fixture-connection",
    kind: str = "data",
    monotonic_ns: int | None = None,
    **extra: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": schema_version,
        "source": source,
        "kind": kind,
        "event_type": event_type,
        "session_id": "fixture-session",
        "connection_id": connection_id,
        "received_at": _iso(received_at_ms),
        "event_at": _iso(received_at_ms),
        "sequence": None,
        "payload": payload,
    }
    if monotonic_ns is not None:
        value["monotonic_ns"] = monotonic_ns
    value.update(extra)
    return value


def _freeze_batch(
    root: Path,
    *,
    source: str,
    batch_id: str,
    payloads: list[dict[str, object]],
) -> tuple[Path, tuple[str, ...]]:
    writer = CaptureStore(root).open_raw_batch(
        source=source,
        batch_id=batch_id,
        schema_version="edge-lab-recorder.raw.v1",
    )
    record_ids: list[str] = []
    for payload in payloads:
        result = writer.append(
            received_at=payload["received_at"],
            event_at=payload["event_at"],
            sequence=payload["sequence"],
            payload=payload,
        )
        record_ids.append(result.record_id)
    writer.finalize(finalized_at=_iso(CLOSE_MS + 60_000))
    return writer.manifest_path, tuple(record_ids)


def _announcement() -> dict[str, object]:
    slug = f"btc-updown-5m-{OPEN_MS // 1_000}"
    event = {
        "event_type": "new_market",
        "id": "3081000",
        "slug": slug,
        "condition_id": CONDITION_ID,
        "market": CONDITION_ID,
        "assets_ids": [UP_TOKEN_ID, DOWN_TOKEN_ID],
        "clob_token_ids": [UP_TOKEN_ID, DOWN_TOKEN_ID],
        "outcomes": ["Up", "Down"],
        "question": "Bitcoin Up or Down - fixture only",
        "description": (
            'This market resolves "Up" when the ending Bitcoin price is at '
            "least the opening price. The resolution source is Chainlink, "
            "specifically the BTC/USD data stream available at "
            "https://data.chain.link/streams/btc-usd."
        ),
        "event_message": {"id": "742901", "slug": slug, "ticker": slug},
        "timestamp": str(OPEN_MS - 60_000),
        "active": False,
    }
    return _recorder_payload(
        source="clob_market_ws",
        schema_version="clob-market-ws.new_market.v1",
        event_type="new_market",
        received_at_ms=OPEN_MS - 60_000,
        payload=event,
        monotonic_ns=10,
    )


def _resync(
    *,
    connection_id: str = "fixture-connection",
    received_at_ms: int = OPEN_MS + 1_000,
    monotonic_ns: int = 100,
) -> dict[str, object]:
    return _recorder_payload(
        source="clob_market_ws",
        schema_version="edge-lab-recorder.lifecycle.v1",
        event_type="resync_complete",
        received_at_ms=received_at_ms,
        payload=None,
        connection_id=connection_id,
        kind="lifecycle",
        monotonic_ns=monotonic_ns,
        detail={
            "watermark_ns": 90,
            "asset_watermarks_ns": {
                UP_TOKEN_ID: 80,
                DOWN_TOKEN_ID: 90,
            },
        },
    )


def _book() -> dict[str, object]:
    received_at_ms = OPEN_MS + 2_000
    book = {
        "event_type": "book",
        "market": CONDITION_ID,
        "asset_id": UP_TOKEN_ID,
        "bids": [
            {"price": "0.39", "size": "5"},
            {"price": "0.40", "size": "1"},
        ],
        "asks": [{"price": "0.41", "size": "7"}],
        "hash": "fixture-book-state-hash",
        "timestamp": str(received_at_ms),
    }
    return _recorder_payload(
        source="clob_market_ws",
        schema_version="clob-market-ws.book.v1",
        event_type="book",
        received_at_ms=received_at_ms,
        payload=book,
        monotonic_ns=110,
        server_timestamp=received_at_ms,
        server_hash="fixture-book-state-hash",
    )


def _chainlink(timestamp_ms: int, *, price: str, received_at_ms: int) -> dict[str, object]:
    full_accuracy = f"{price.split('.')[0]}{price.partition('.')[2].ljust(18, '0')}"
    update = {
        "connection_id": "fixture-rtds-upstream",
        "payload": {
            "full_accuracy_value": full_accuracy,
            "symbol": "btc/usd",
            "timestamp": timestamp_ms,
            "value": price,
        },
        "timestamp": timestamp_ms + 500,
        "topic": "crypto_prices_chainlink",
        "type": "update",
    }
    return _recorder_payload(
        source="rtds_ws",
        schema_version="rtds.crypto_prices_chainlink.update.v1",
        event_type="crypto_prices_chainlink.update",
        received_at_ms=received_at_ms,
        payload=update,
        connection_id="fixture-rtds-capture",
        monotonic_ns=received_at_ms,
        server_timestamp=timestamp_ms + 500,
    )


def _fee_snapshot(
    *,
    schema_version: str = "clob-http.snapshot.v1",
) -> dict[str, object]:
    market = {
        "c": CONDITION_ID,
        "fd": {"r": "0.02", "e": 1, "to": False},
        "mts": "0.01",
        "mos": 1,
        "t": [
            {"o": "Up", "t": UP_TOKEN_ID},
            {"o": "Down", "t": DOWN_TOKEN_ID},
        ],
    }
    return _recorder_payload(
        source="clob_http",
        schema_version=schema_version,
        event_type="clob_snapshot",
        received_at_ms=OPEN_MS + 2_500,
        kind="snapshot",
        payload={
            "snapshot_kind": "periodic",
            "responses": [
                {
                    "resource": "clob_market",
                    "raw_json": market,
                    "provenance": {"status_code": 200},
                }
            ],
        },
    )


def _decision(
    *,
    announcement_id: str,
    book_id: str,
    resync_id: str,
    received_at_ms: int = OPEN_MS + 3_000,
    quantity: str = "2",
) -> dict[str, object]:
    strategy_config = {
        "strategy_id": "fixture-pessimistic-maker-buy",
        "initial_cash": "100",
        "queue_scenario": "pessimistic",
        "trade_volume_haircut": "0.5",
        "settlement_operation_cost": "1",
        "settlement_operation_latency_ms": 120_000,
        "operation_cost_source": "public_capture_pessimistic_upper_bound",
    }
    config_hash = hashlib.sha256(canonical_json_bytes(strategy_config)).hexdigest()
    return _recorder_payload(
        source="edge_lab_ghost_decision",
        schema_version="edge-lab.ghost-decision.v1",
        event_type="ghost_decision",
        received_at_ms=received_at_ms,
        payload={
            "experiment_id": "fixture-experiment",
            "strategy_config": strategy_config,
            "config_hash": config_hash,
            "announcement_record_id": announcement_id,
            "condition_id": CONDITION_ID,
            "market_id": CONDITION_ID,
            "token_id": UP_TOKEN_ID,
            "side": "BUY",
            "price": "0.40",
            "quantity": quantity,
            "decision_receive_time": _iso(received_at_ms),
            "l2_record_ids": [book_id],
            "last_book_record_id": book_id,
            "book_state_hash": "fixture-book-state-hash",
            "resnapshot_record_id": resync_id,
            "visible_queue": "1",
            "tick_size": "0.01",
            "minimum_order_size": "1",
            "submit_latency_ms": 0,
            "cancel_latency_ms": 100,
            "max_book_age_ms": 5_000,
            "split": "test",
            "public_trade_side_contract": (
                "data_api_taker_only_required"
            ),
            # Hostile self-reported results are intentionally ignored.
            "fill_count": 99_999,
            "pnl": "999999",
            "classification": "validated_profitable",
            "reconciled": True,
            "fee": "0",
        },
    )


def _public_trade(
    *,
    received_at_ms: int | None = None,
    size: str = "10",
    connection_id: str = "fixture-connection",
) -> dict[str, object]:
    timestamp_ms = OPEN_MS + 4_000
    actual_received_at_ms = received_at_ms or timestamp_ms
    trade = {
        "event_type": "last_trade_price",
        "market": CONDITION_ID,
        "asset_id": UP_TOKEN_ID,
        "price": "0.40",
        "size": size,
        "side": "SELL",
        "timestamp": str(timestamp_ms),
        "transaction_hash": TRANSACTION_HASH,
    }
    return _recorder_payload(
        source="clob_market_ws",
        schema_version="clob-market-ws.last_trade_price.v1",
        event_type="last_trade_price",
        received_at_ms=actual_received_at_ms,
        payload=trade,
        connection_id=connection_id,
        monotonic_ns=120,
        server_timestamp=timestamp_ms,
    )


def _data_api_trade_snapshot(
    *,
    taker_only: bool = True,
    side: str = "SELL",
    token_id: str = UP_TOKEN_ID,
    price: str = "0.40",
) -> dict[str, object]:
    trade = {
        "conditionId": CONDITION_ID,
        "asset": token_id,
        "side": side,
        "size": "10",
        "price": price,
        "timestamp": OPEN_MS // 1_000 + 4,
        "transactionHash": TRANSACTION_HASH,
        "proxyWallet": "0x" + "12" * 20,
        "outcome": "Up",
        "outcomeIndex": 0,
    }
    raw_body = json.dumps([trade], separators=(",", ":")).encode()
    return _recorder_payload(
        source="data_api_trades",
        schema_version="data-api.trades.taker-only.snapshot.v1",
        event_type="data_api_trades",
        received_at_ms=OPEN_MS + 4_010,
        kind="snapshot",
        payload={
            "schema_version": "edge-lab-data-api-trades-snapshot.v1",
            "condition_id": CONDITION_ID,
            "taker_only": taker_only,
            "raw_json": [trade],
            "raw_body_base64": base64.b64encode(raw_body).decode("ascii"),
            "provenance": {
                "source": "data_api_trades",
                "method": "GET",
                "url": "https://data-api.polymarket.com/trades",
                "request_params": {
                    "market": CONDITION_ID,
                    "takerOnly": "true" if taker_only else "false",
                    "limit": 1_000,
                    "offset": 0,
                },
                "status_code": 200,
                "requested_at_epoch_seconds": repr(OPEN_MS / 1_000 + 4),
                "received_at_epoch_seconds": repr(
                    OPEN_MS / 1_000 + 4.01
                ),
                "attempt": 1,
                "response_headers": {"content-type": "application/json"},
                "body_sha256": hashlib.sha256(raw_body).hexdigest(),
                "body_bytes": len(raw_body),
            },
        },
    )


def _price_change(
    *,
    best_ask: str,
    received_at_ms: int = OPEN_MS + 3_500,
    connection_id: str = "fixture-connection",
) -> dict[str, object]:
    return _recorder_payload(
        source="clob_market_ws",
        schema_version="clob-market-ws.price_change.v1",
        event_type="price_change",
        received_at_ms=received_at_ms,
        connection_id=connection_id,
        payload={
            "event_type": "price_change",
            "market": CONDITION_ID,
            "timestamp": str(received_at_ms),
            "price_changes": [
                {
                    "asset_id": UP_TOKEN_ID,
                    "price": best_ask,
                    "size": "1",
                    "side": "SELL",
                    "hash": "fixture-price-change-hash",
                    "best_bid": "0.39",
                    "best_ask": best_ask,
                }
            ],
        },
        monotonic_ns=115,
        server_timestamp=received_at_ms,
    )


def _heartbeat(
    received_at_ms: int,
    monotonic_ns: int,
    *,
    connection_id: str = "fixture-connection",
) -> dict[str, object]:
    return _recorder_payload(
        source="clob_market_ws",
        schema_version="edge-lab-recorder.heartbeat-ack.v1",
        event_type="heartbeat_ack",
        received_at_ms=received_at_ms,
        payload=None,
        connection_id=connection_id,
        kind="lifecycle",
        monotonic_ns=monotonic_ns,
        raw_frame="PONG",
    )


def _gamma() -> dict[str, object]:
    slug = f"btc-updown-5m-{OPEN_MS // 1_000}"
    market = {
        "id": "3081000",
        "slug": slug,
        "conditionId": CONDITION_ID,
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["1", "0"]',
        "clobTokenIds": f'["{UP_TOKEN_ID}", "{DOWN_TOKEN_ID}"]',
        "closed": True,
        "updatedAt": _iso(CLOSE_MS + 4_000),
    }
    return _recorder_payload(
        source="gamma_http",
        schema_version="gamma-http.snapshot.v1",
        event_type="snapshot",
        received_at_ms=CLOSE_MS + 5_000,
        payload={
            "snapshot_kind": "periodic",
            "responses": [
                {
                    "resource": "gamma_markets",
                    "raw_json": {"markets": [market], "next_cursor": "fixture"},
                    "provenance": {"status_code": 200},
                }
            ],
        },
    )


def _manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def complete_fixture(
    root: Path,
    *,
    fee_schema_version: str = "clob-http.snapshot.v1",
    include_fee: bool = True,
    decision_received_at_ms: int = OPEN_MS + 3_000,
    duplicate_trade: bool = False,
    conflicting_duplicate_trade: bool = False,
    second_decision: bool = False,
    include_data_api_trade: bool = False,
    data_api_taker_only: bool = True,
    crossing_book_update: bool = False,
    crossing_book_update_received_at_ms: int | None = None,
    liveness_connection_id: str = "fixture-connection",
    trade_received_at_ms: int | None = None,
    batch_prefix: str = "fixture",
) -> Path:
    pre_manifest, pre_ids = _freeze_batch(
        root,
        source="clob_market_ws",
        batch_id=f"{batch_prefix}-pre-decision",
        payloads=[_announcement(), _resync(), _book()],
    )
    announcement_id, resync_id, book_id = pre_ids
    rtds_manifest, _ = _freeze_batch(
        root,
        source="rtds_ws",
        batch_id=f"{batch_prefix}-chainlink",
        payloads=[
            _chainlink(
                OPEN_MS,
                price="100.000000000000000000",
                received_at_ms=OPEN_MS + 1_500,
            ),
            _chainlink(
                CLOSE_MS,
                price="101.000000000000000000",
                received_at_ms=CLOSE_MS + 1_500,
            ),
        ],
    )
    fee_manifest: Path | None = None
    if include_fee:
        fee_manifest, _ = _freeze_batch(
            root,
            source="clob_http",
            batch_id=f"{batch_prefix}-fee",
            payloads=[_fee_snapshot(schema_version=fee_schema_version)],
        )
    decision_payloads = [
        _decision(
            announcement_id=announcement_id,
            book_id=book_id,
            resync_id=resync_id,
            received_at_ms=decision_received_at_ms,
        )
    ]
    if second_decision:
        decision_payloads.append(
            _decision(
                announcement_id=announcement_id,
                book_id=book_id,
                resync_id=resync_id,
                received_at_ms=decision_received_at_ms + 100,
            )
        )
    decision_manifest, _ = _freeze_batch(
        root,
        source="edge_lab_ghost_decision",
        batch_id=f"{batch_prefix}-decision",
        payloads=decision_payloads,
    )
    trade_payloads = [
        _public_trade(received_at_ms=trade_received_at_ms)
    ]
    if duplicate_trade or conflicting_duplicate_trade:
        trade_payloads.append(
            _public_trade(
                received_at_ms=OPEN_MS + 4_100,
                size="11" if conflicting_duplicate_trade else "10",
                connection_id="fixture-reconnected",
            )
        )
    post_manifest, _ = _freeze_batch(
        root,
        source="clob_market_ws",
        batch_id=f"{batch_prefix}-post-decision",
        payloads=[
            *(
                [
                    _resync(
                        connection_id=liveness_connection_id,
                        received_at_ms=OPEN_MS + 3_250,
                        monotonic_ns=150,
                    )
                ]
                if liveness_connection_id != "fixture-connection"
                else []
            ),
            *(
                [
                    _price_change(
                        best_ask="0.40",
                        received_at_ms=(
                            crossing_book_update_received_at_ms
                            if crossing_book_update_received_at_ms is not None
                            else OPEN_MS + 3_500
                        ),
                    )
                ]
                if crossing_book_update
                else []
            ),
            *trade_payloads,
            _heartbeat(
                CLOSE_MS - 10_000,
                200,
                connection_id=liveness_connection_id,
            ),
            _heartbeat(
                CLOSE_MS + 10_000,
                300,
                connection_id=liveness_connection_id,
            ),
        ],
    )
    gamma_manifest, _ = _freeze_batch(
        root,
        source="gamma_http",
        batch_id=f"{batch_prefix}-gamma",
        payloads=[_gamma()],
    )
    manifests = [
        pre_manifest,
        rtds_manifest,
        decision_manifest,
        post_manifest,
        gamma_manifest,
    ]
    if include_data_api_trade:
        data_api_manifest, _ = _freeze_batch(
            root,
            source="data_api_trades",
            batch_id=f"{batch_prefix}-data-api-trades",
            payloads=[
                _data_api_trade_snapshot(
                    taker_only=data_api_taker_only
                )
            ],
        )
        manifests.append(data_api_manifest)
    if fee_manifest is not None:
        manifests.insert(2, fee_manifest)
    request = {
        "schema_version": "edge-lab.execution-replay-promotion-request.v1",
        "evidence_mode": "fixture",
        "output_root": "phase2_execution_runs",
        "source_manifests": [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _manifest_sha256(path),
            }
            for path in manifests
        ],
        # These values must neither override recomputed results nor weaken
        # the safety boundary.
        "pnl": "123456",
        "fill_count": 123456,
        "classification": "validated_profitable",
        "reconciled": True,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
    }
    path = root / "EXECUTION_REPLAY_REQUEST.json"
    path.write_bytes(canonical_json_bytes(request) + b"\n")
    return path


def complete_captured_public_fixture(
    root: Path,
    *,
    settlement_outcome: str = "Up",
    settlement_received_at_ms: int = CLOSE_MS + 10_000,
) -> Path:
    capture_root = root / "capture"
    fixture_request_path = complete_fixture(
        capture_root,
        include_data_api_trade=True,
        batch_prefix="captured-public",
    )
    fixture_request = json.loads(
        fixture_request_path.read_text(encoding="utf-8")
    )
    records = [
        json.loads(line)
        for raw_path in sorted(
            (capture_root / "raw").rglob("*.jsonl")
        )
        for line in raw_path.read_text(encoding="utf-8").splitlines()
    ]

    def role_record(
        predicate: object,
    ) -> dict[str, object]:
        matches = [
            row
            for row in records
            if predicate(row)
        ]
        assert len(matches) == 1
        return matches[0]

    announcement = role_record(
        lambda row: row["source"] == "clob_market_ws"
        and row["payload"]["event_type"] == "new_market"
    )
    gamma = role_record(
        lambda row: row["source"] == "gamma_http"
    )
    chainlink = sorted(
        (
            row
            for row in records
            if row["source"] == "rtds_ws"
        ),
        key=lambda row: row["payload"]["payload"]["payload"][
            "timestamp"
        ],
    )
    pongs = sorted(
        (
            row
            for row in records
            if row["source"] == "clob_market_ws"
            and row["payload"]["event_type"] == "heartbeat_ack"
        ),
        key=lambda row: row["received_at"],
    )
    decision = role_record(
        lambda row: row["source"] == "edge_lab_ghost_decision"
    )
    data_api = role_record(
        lambda row: row["source"] == "data_api_trades"
    )
    parsed_target = parse_short_crypto_announcement(announcement)
    assert parsed_target is not None
    dependencies = [
        announcement["record_id"],
        gamma["record_id"],
        chainlink[0]["record_id"],
        chainlink[1]["record_id"],
        pongs[0]["record_id"],
        pongs[1]["record_id"],
    ]

    def service_role(
        event_type: str,
        schema_version: str,
        received_at_ms: int,
        **extra: object,
    ) -> dict[str, object]:
        return _recorder_payload(
            source="dynamic_short_crypto_service",
            schema_version=(
                "edge-lab-dynamic-short-crypto-service.record.v1"
            ),
            event_type=event_type,
            received_at_ms=received_at_ms,
            payload={
                "schema_version": schema_version,
                "evidence_role": event_type,
                "slug": f"btc-updown-5m-{OPEN_MS // 1_000}",
                "condition_id": CONDITION_ID,
                "opens_at_ms": OPEN_MS,
                "closes_at_ms": CLOSE_MS,
                **extra,
            },
        )

    settlement = _recorder_payload(
        source="dynamic_short_crypto_service",
        schema_version=(
            "edge-lab-dynamic-short-crypto-service.record.v1"
        ),
        event_type="settlement_reconciled",
        received_at_ms=settlement_received_at_ms,
        payload={
            "schema_version": (
                "edge-lab.short-crypto-settlement-commit.v1"
            ),
            "transition": "commit_settlement",
            "action": "strict_settlement_committed",
            "status": "resolved",
            "slug": f"btc-updown-5m-{OPEN_MS // 1_000}",
            "condition_id": CONDITION_ID,
            "rule_hash": parsed_target.rule_hash,
            "outcome": settlement_outcome,
            "winning_token_id": (
                UP_TOKEN_ID
                if settlement_outcome == "Up"
                else DOWN_TOKEN_ID
            ),
            "chainlink_open_price": "100",
            "chainlink_close_price": "101",
            "required_record_ids": dependencies,
            "gamma_record_ids": [gamma["record_id"]],
            "available_at_ms": CLOSE_MS + 5_000,
            "chainlink_boundary_status": "committed",
            "close_liveness": {
                "status": "verified",
                "session_id": "fixture-session",
                "connection_id": "fixture-connection",
                "before_record_id": pongs[0]["record_id"],
                "before_received_at_ms": CLOSE_MS - 10_000,
                "after_record_id": pongs[1]["record_id"],
                "after_received_at_ms": CLOSE_MS + 10_000,
            },
        },
    )
    control_manifest, control_ids = _freeze_batch(
        capture_root,
        source="dynamic_short_crypto_service",
        batch_id="captured-public-production-roles",
        payloads=[
            settlement,
            service_role(
                "full_window_capture_close_checkpoint",
                "edge-lab.execution-replay-close-checkpoint.v1",
                CLOSE_MS + 11_000,
                finalized_through_ms=CLOSE_MS + 10_000,
                group_id="captured-public-fixture-group",
            ),
            service_role(
                "decision_to_trade_l2_closure",
                "edge-lab.execution-replay-l2-closure.v1",
                CLOSE_MS + 12_000,
                decision_count=1,
                public_trade_count=1,
                closed_through_ms=CLOSE_MS,
                decision_record_ids=[decision["record_id"]],
                public_trade_snapshot_record_ids=[
                    data_api["record_id"]
                ],
                group_id="captured-public-fixture-group",
            ),
            service_role(
                "pessimistic_submit_latency",
                "edge-lab.execution-replay-submit-latency.v1",
                CLOSE_MS + 13_000,
                latency_ms=250,
                provenance="public_capture_pessimistic_floor",
                decision_record_id=decision["record_id"],
            ),
            service_role(
                "settlement_operation_cost",
                "edge-lab.execution-replay-settlement-cost.v1",
                CLOSE_MS + 14_000,
                amount="1",
                currency="USDC",
                provenance="public_capture_pessimistic_upper_bound",
                decision_record_id=decision["record_id"],
            ),
        ],
    )
    del control_manifest
    role_ids = {
        "durable_announcement_record_id": announcement["record_id"],
        "durable_gamma_record_id": gamma["record_id"],
        "durable_chainlink_open_record_id": chainlink[0]["record_id"],
        "durable_chainlink_close_record_id": chainlink[1]["record_id"],
        "durable_close_pong_before_record_id": pongs[0]["record_id"],
        "durable_close_pong_after_record_id": pongs[1]["record_id"],
        "atomic_settlement_commit_record_id": control_ids[0],
        "full_window_capture_close_checkpoint_id": control_ids[1],
        "decision_to_trade_l2_closure_id": control_ids[2],
        "pessimistic_submit_latency_evidence_id": control_ids[3],
        "settlement_operation_cost_evidence_id": control_ids[4],
    }
    entries: list[dict[str, object]] = []
    for manifest_path in sorted(
        (capture_root / "raw").rglob("*.manifest.json")
    ):
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
        raw_path = capture_root / manifest["raw_path"]
        raw_records = [
            json.loads(line)
            for line in raw_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        entries.append(
            {
                "path": manifest_path.relative_to(root).as_posix(),
                "manifest_sha256": _manifest_sha256(manifest_path),
                "raw_path": raw_path.relative_to(root).as_posix(),
                "raw_sha256": hashlib.sha256(
                    raw_path.read_bytes()
                ).hexdigest(),
                "raw_bytes": raw_path.stat().st_size,
                "raw_lines": len(raw_records),
                "record_ids": [
                    row["record_id"] for row in raw_records
                ],
            }
        )
    freeze_core = {
        "schema_version": CAPTURE_FREEZE_SCHEMA,
        "status": "finalized",
        "capture_root": "capture",
        "target": {
            "slug": f"btc-updown-5m-{OPEN_MS // 1_000}",
            "condition_id": CONDITION_ID,
            "opens_at_ms": OPEN_MS,
            "closes_at_ms": CLOSE_MS,
        },
        "source_manifests": entries,
        "required_role_ids": role_ids,
        "safety": {
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
        },
    }
    freeze_id = hashlib.sha256(
        canonical_json_bytes(freeze_core)
    ).hexdigest()
    freeze_path = capture_root / "CAPTURE_FREEZE.json"
    freeze_path.write_bytes(
        canonical_json_bytes(
            {**freeze_core, "freeze_id": freeze_id}
        )
        + b"\n"
    )
    request = {
        **fixture_request,
        "evidence_mode": "captured_public",
        "output_root": "phase2_execution_runs",
        "source_manifests": [
            {
                "path": entry["path"],
                "sha256": entry["manifest_sha256"],
            }
            for entry in entries
        ],
        "production_freeze": {
            "path": "capture/CAPTURE_FREEZE.json",
            "sha256": hashlib.sha256(
                freeze_path.read_bytes()
            ).hexdigest(),
            "freeze_id": freeze_id,
        },
    }
    request_path = root / "EXECUTION_REPLAY_REQUEST.json"
    request_path.write_bytes(canonical_json_bytes(request) + b"\n")
    return request_path


def test_fixture_validates_full_replay_without_claiming_real_market_completion(
    tmp_path: Path,
) -> None:
    request_path = complete_fixture(tmp_path)

    result = verify_and_promote_execution_replay(request_path)

    assert result.verified is True
    assert result.promoted is False
    assert result.status == "fixture_validated"
    assert result.production_evidence is False
    assert result.classification == "insufficient_data"
    assert result.run_id is not None
    assert result.output_dir == tmp_path / "phase2_execution_runs" / result.run_id
    assert set(result.artifacts) == {
        "RUN_MANIFEST.json",
        "EXECUTION_REPLAY.json",
        "TRADES.csv",
        "DATA_QUALITY.json",
        "REPRODUCIBILITY.json",
    }

    replay = json.loads(
        result.artifacts["EXECUTION_REPLAY.json"].read_text(encoding="utf-8")
    )
    assert replay["counts"] == {
        "event_count": 1,
        "decision_count": 1,
        "explainable_fill_count": 1,
        "actual_fill_count": 0,
        "authenticated_fill_count": 0,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
    }
    assert replay["metrics"] == {
        "initial_capital": "100",
        "final_equity": "100.1904",
        "fees": "0.0096",
        "operation_costs": "1",
        "settled_pnl": "0.1904",
        "return": "0.001904",
        "turnover": "0.008",
        "rewards": "0",
    }
    fill = replay["fills"][0]
    assert fill["counterfactual"] is True
    assert fill["actual_fill"] is False
    assert fill["authenticated_fill"] is False
    assert fill["evidence_kind"] == "execution_replay_fill"
    assert fill["queue_before"] == "2"
    assert fill["queue_consumed"] == "2"
    assert fill["queue_after"] == "0"
    assert fill["public_trade_volume"] == "10"
    assert fill["trade_volume_haircut"] == "0.5"
    assert fill["attributable_trade_capacity"] == "5"
    assert fill["quantity"] == "2"
    assert fill["fee"] == "0.0096"
    assert replay["classification"] == "insufficient_data"
    assert "event_count_below_100" in replay["classification_reasons"]
    assert "explainable_fill_count_below_100" in replay["classification_reasons"]
    assert replay["source_claims_ignored"] == [
        "classification",
        "fill_count",
        "pnl",
        "reconciled",
    ]
    assert replay["trade_evidence"][0]["counterfactual"] is True
    assert replay["trade_evidence"][0]["actual_fill"] is False
    assert replay["trade_evidence"][0]["other_costs"] == "1"
    assert replay["trade_evidence"][0]["claimed_net_pnl"] == "0.1904"


def test_rejects_decision_time_fee_schema_drift_without_partial_output(
    tmp_path: Path,
) -> None:
    request_path = complete_fixture(
        tmp_path,
        fee_schema_version="clob-http.snapshot.v2",
    )

    result = verify_and_promote_execution_replay(request_path)

    assert result.verified is False
    assert result.reason_codes == ("fee_schema_mismatch",)
    assert result.output_dir is None
    assert not (tmp_path / "phase2_execution_runs").exists()


def test_rejects_future_l2_and_missing_fee_without_zero_fallback(
    tmp_path: Path,
) -> None:
    future_root = tmp_path / "future-l2"
    future_result = verify_and_promote_execution_replay(
        complete_fixture(
            future_root,
            decision_received_at_ms=OPEN_MS + 1_500,
        )
    )
    assert future_result.reason_codes == ("future_l2_reference",)
    assert not (future_root / "phase2_execution_runs").exists()

    missing_fee_root = tmp_path / "missing-fee"
    missing_fee_result = verify_and_promote_execution_replay(
        complete_fixture(missing_fee_root, include_fee=False)
    )
    assert missing_fee_result.reason_codes == ("decision_fee_missing",)
    assert not (missing_fee_root / "phase2_execution_runs").exists()


def test_reconnect_duplicate_trade_is_consumed_once_and_conflict_is_rejected(
    tmp_path: Path,
) -> None:
    duplicate_result = verify_and_promote_execution_replay(
        complete_fixture(tmp_path / "duplicate", duplicate_trade=True)
    )
    assert duplicate_result.verified is True
    duplicate_replay = json.loads(
        duplicate_result.artifacts["EXECUTION_REPLAY.json"].read_text(
            encoding="utf-8"
        )
    )
    assert duplicate_replay["counts"]["explainable_fill_count"] == 1
    assert sum(
        Decimal(row["quantity"]) for row in duplicate_replay["fills"]
    ) == Decimal("2")

    conflict_root = tmp_path / "conflict"
    conflict_result = verify_and_promote_execution_replay(
        complete_fixture(
            conflict_root,
            conflicting_duplicate_trade=True,
        )
    )
    assert conflict_result.reason_codes == (
        "public_trade_duplicate_conflict",
    )
    assert not (conflict_root / "phase2_execution_runs").exists()


def test_multiple_ghost_orders_share_one_public_trade_capacity(
    tmp_path: Path,
) -> None:
    result = verify_and_promote_execution_replay(
        complete_fixture(tmp_path, second_decision=True)
    )

    assert result.verified is True
    replay = json.loads(
        result.artifacts["EXECUTION_REPLAY.json"].read_text(encoding="utf-8")
    )
    assert replay["counts"]["decision_count"] == 2
    assert replay["counts"]["explainable_fill_count"] == 1
    assert sum(Decimal(row["quantity"]) for row in replay["fills"]) == Decimal("2")
    assert sum(
        Decimal(row["queue_consumed"])
        for row in replay["fills"]
    ) + sum(
        Decimal(row["quantity"]) for row in replay["fills"]
    ) <= Decimal("10") * Decimal("0.5")


def test_inter_decision_l2_update_cannot_be_skipped(
    tmp_path: Path,
) -> None:
    result = verify_and_promote_execution_replay(
        complete_fixture(
            tmp_path,
            second_decision=True,
            crossing_book_update=True,
            crossing_book_update_received_at_ms=OPEN_MS + 3_050,
        )
    )

    assert result.reason_codes == ("decision_l2_prefix_incomplete",)
    assert result.output_dir is None


def test_close_liveness_must_use_decision_book_connection(
    tmp_path: Path,
) -> None:
    result = verify_and_promote_execution_replay(
        complete_fixture(
            tmp_path,
            liveness_connection_id="unrelated-healthy-connection",
        )
    )

    assert result.reason_codes == ("close_liveness_connection_mismatch",)
    assert result.output_dir is None


def test_replay_is_byte_identical_and_reuses_the_content_address(
    tmp_path: Path,
) -> None:
    request_path = complete_fixture(tmp_path)

    first = verify_and_promote_execution_replay(request_path)
    first_bytes = {
        name: path.read_bytes() for name, path in first.artifacts.items()
    }
    second = verify_and_promote_execution_replay(request_path)

    assert second.run_id == first.run_id
    assert second.output_dir == first.output_dir
    assert {
        name: path.read_bytes() for name, path in second.artifacts.items()
    } == first_bytes


def test_tampered_raw_partial_reference_and_nonzero_activity_fail_closed(
    tmp_path: Path,
) -> None:
    tamper_root = tmp_path / "tamper"
    tamper_request = complete_fixture(tamper_root)
    request = json.loads(tamper_request.read_text(encoding="utf-8"))
    first_manifest = tamper_root / request["source_manifests"][0]["path"]
    first_raw = first_manifest.with_name(
        first_manifest.name.removesuffix(".manifest.json") + ".jsonl"
    )
    first_raw.chmod(0o644)
    with first_raw.open("ab") as handle:
        handle.write(b"{}\n")

    tampered = verify_and_promote_execution_replay(tamper_request)

    assert tampered.reason_codes == ("source_raw_integrity_mismatch",)
    assert not (tamper_root / "phase2_execution_runs").exists()

    partial_root = tmp_path / "partial"
    partial_request = complete_fixture(partial_root)
    partial_payload = json.loads(partial_request.read_text(encoding="utf-8"))
    partial_payload["source_manifests"][0]["path"] += ".partial"
    partial_request.write_bytes(canonical_json_bytes(partial_payload) + b"\n")
    partial = verify_and_promote_execution_replay(partial_request)
    assert partial.reason_codes == ("partial_evidence_forbidden",)
    assert not (partial_root / "phase2_execution_runs").exists()

    unsafe_root = tmp_path / "unsafe"
    unsafe_request = complete_fixture(unsafe_root)
    unsafe_payload = json.loads(unsafe_request.read_text(encoding="utf-8"))
    unsafe_payload["authenticated_endpoints_used"] = 1
    unsafe_request.write_bytes(canonical_json_bytes(unsafe_payload) + b"\n")
    unsafe = verify_and_promote_execution_replay(unsafe_request)
    assert unsafe.reason_codes == ("unsafe_activity_count",)
    assert not (unsafe_root / "phase2_execution_runs").exists()


def test_request_cannot_self_promote_fixture_shaped_bytes_as_public_capture(
    tmp_path: Path,
) -> None:
    request_path = complete_fixture(tmp_path, batch_prefix="capture")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["evidence_mode"] = "captured_public"
    request_path.write_bytes(canonical_json_bytes(request) + b"\n")

    result = verify_and_promote_execution_replay(request_path)

    assert result.promoted is False
    assert result.production_evidence is False
    assert result.reason_codes == ("captured_public_freeze_contract_missing",)
    assert {
        "finalized_full_window_freeze_manifest_id",
        "atomic_settlement_commit_record_id",
        "pessimistic_submit_latency_evidence_id",
        "settlement_operation_cost_evidence_id",
    }.issubset(result.missing_frozen_roles)
    assert not (tmp_path / "phase2_execution_runs").exists()


def test_verified_capture_freeze_promotes_counterfactual_public_replay(
    tmp_path: Path,
) -> None:
    request_path = complete_captured_public_fixture(tmp_path)

    first = verify_and_promote_execution_replay(request_path)
    second = verify_and_promote_execution_replay(request_path)

    assert first.verified is True
    assert first.promoted is True
    assert first.production_evidence is True
    assert first.status == "promoted_counterfactual"
    assert first.classification == "insufficient_data"
    assert first.missing_frozen_roles == ()
    assert second.run_id == first.run_id
    assert {
        name: path.read_bytes()
        for name, path in second.artifacts.items()
    } == {
        name: path.read_bytes()
        for name, path in first.artifacts.items()
    }
    replay = json.loads(
        first.artifacts["EXECUTION_REPLAY.json"].read_text(
            encoding="utf-8"
        )
    )
    assert replay["counts"]["explainable_fill_count"] == 1
    assert replay["counts"]["actual_fill_count"] == 0
    assert replay["counts"]["authenticated_fill_count"] == 0
    assert replay["counts"]["orders_submitted"] == 0
    assert replay["counts"]["authenticated_endpoints_used"] == 0
    assert replay["settlement"]["strict_terminal_status"] == (
        "strict_settlement_committed"
    )
    quality = json.loads(
        first.artifacts["DATA_QUALITY.json"].read_text(
            encoding="utf-8"
        )
    )
    assert quality["checks"]["full_window_capture_inventory"] == "passed"
    assert quality["checks"]["settlement_operation_cost"] == "passed"
    assert quality["production_missing_frozen_roles"] == []


def test_captured_replay_uses_atomic_commit_as_settlement_source(
    tmp_path: Path,
) -> None:
    request_path = complete_captured_public_fixture(tmp_path)
    freeze = json.loads(
        (tmp_path / "capture" / "CAPTURE_FREEZE.json").read_text(
            encoding="utf-8"
        )
    )
    atomic_commit_id = freeze["required_role_ids"][
        "atomic_settlement_commit_record_id"
    ]

    result = verify_and_promote_execution_replay(request_path)

    assert result.promoted is True
    replay = json.loads(
        result.artifacts["EXECUTION_REPLAY.json"].read_text(
            encoding="utf-8"
        )
    )
    assert replay["settlement"]["atomic_settlement_commit_record_id"] == (
        atomic_commit_id
    )
    resolve_entries = [
        row for row in replay["ledger"]["trace"] if row["action"] == "resolve"
    ]
    assert len(resolve_entries) == 1
    assert resolve_entries[0]["source_event_id"] == atomic_commit_id
    assert {
        row["settlement_source_event_id"]
        for row in replay["trade_evidence"]
    } == {atomic_commit_id}


def test_atomic_settlement_commit_must_match_recomputed_outcome(
    tmp_path: Path,
) -> None:
    result = verify_and_promote_execution_replay(
        complete_captured_public_fixture(
            tmp_path,
            settlement_outcome="Down",
        )
    )

    assert result.reason_codes == ("atomic_settlement_commit_mismatch",)
    assert result.output_dir is None


def test_atomic_settlement_commit_cannot_predate_its_dependencies(
    tmp_path: Path,
) -> None:
    result = verify_and_promote_execution_replay(
        complete_captured_public_fixture(
            tmp_path,
            settlement_received_at_ms=CLOSE_MS + 500,
        )
    )

    assert result.reason_codes == ("atomic_settlement_commit_not_causal",)
    assert result.output_dir is None


def test_freeze_builder_copies_finalized_pairs_and_emits_verified_request(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    source_request_path = complete_captured_public_fixture(source_root)
    source_request = json.loads(
        source_request_path.read_text(encoding="utf-8")
    )
    source_freeze = json.loads(
        (
            source_root
            / source_request["production_freeze"]["path"]
        ).read_text(encoding="utf-8")
    )
    sealed_root = tmp_path / "sealed"

    built = build_production_capture_freeze(
        request_base=sealed_root,
        capture_relative_root=(
            "phase2_capture_freezes/"
            f"btc-updown-5m-{OPEN_MS // 1_000}"
        ),
        target=source_freeze["target"],
        source_manifest_paths=[
            source_root / row["path"]
            for row in source_request["source_manifests"]
        ],
        required_role_ids=source_freeze["required_role_ids"],
        request_filename="PHASE2_EXECUTION_REPLAY_REQUEST.json",
    )

    assert built.source_manifest_count == len(
        source_request["source_manifests"]
    )
    assert built.source_record_count > 0
    promoted = verify_and_promote_execution_replay(
        built.request_path
    )
    assert promoted.status == "promoted_counterfactual"
    assert promoted.promoted is True


def test_assembly_ignores_zero_decision_terminal_pair_and_discovers_complete_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    source_request_path = complete_captured_public_fixture(source_root)
    source_request = json.loads(
        source_request_path.read_text(encoding="utf-8")
    )
    run_root = tmp_path / "dynamic-run"
    main_root = tmp_path / "main-capture"
    group_root = (
        run_root
        / "groups"
        / "captured-public-fixture-group"
    )
    for row in source_request["source_manifests"]:
        source_manifest = source_root / row["path"]
        manifest = json.loads(
            source_manifest.read_text(encoding="utf-8")
        )
        source_raw = (
            source_manifest.parent
            / (
                source_manifest.name.removesuffix(
                    ".manifest.json"
                )
                + ".jsonl"
            )
        )
        source_name = manifest["source"]
        if source_name == "rtds_ws":
            destination_root = main_root
        elif source_name in {
            "dynamic_short_crypto_service",
            "gamma_http",
        }:
            destination_root = run_root / "control"
        else:
            destination_root = group_root
        destination = destination_root / "raw" / source_name
        destination.mkdir(parents=True, exist_ok=True)
        (destination / source_manifest.name).write_bytes(
            source_manifest.read_bytes()
        )
        (destination / source_raw.name).write_bytes(
            source_raw.read_bytes()
        )
    zero_decision_slug = f"eth-updown-5m-{OPEN_MS // 1_000}"
    zero_decision_group = "zero-decision-group"
    zero_decision_identity = {
        "slug": zero_decision_slug,
        "condition_id": "0x" + "ef" * 32,
        "opens_at_ms": OPEN_MS,
        "closes_at_ms": CLOSE_MS,
    }
    _freeze_batch(
        run_root / "control",
        source="dynamic_short_crypto_service",
        batch_id="zero-decision-terminal-pair",
        payloads=[
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="full_window_capture_close_checkpoint",
                received_at_ms=CLOSE_MS + 20_000,
                payload={
                    **zero_decision_identity,
                    "schema_version": (
                        "edge-lab.execution-replay-close-checkpoint.v1"
                    ),
                    "evidence_role": (
                        "full_window_capture_close_checkpoint"
                    ),
                    "group_id": zero_decision_group,
                    "finalized_through_ms": CLOSE_MS + 20_000,
                },
            ),
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="decision_to_trade_l2_closure",
                received_at_ms=CLOSE_MS + 21_000,
                payload={
                    **zero_decision_identity,
                    "schema_version": (
                        "edge-lab.execution-replay-l2-closure.v1"
                    ),
                    "evidence_role": "decision_to_trade_l2_closure",
                    "group_id": zero_decision_group,
                    "decision_count": 0,
                    "decision_record_ids": [],
                    "public_trade_count": 0,
                    "public_trade_snapshot_record_ids": [],
                    "closed_through_ms": CLOSE_MS,
                },
            ),
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="settlement_reconciled",
                received_at_ms=CLOSE_MS + 22_000,
                payload={
                    **zero_decision_identity,
                    "schema_version": (
                        "edge-lab.short-crypto-settlement-commit.v1"
                    ),
                    "action": "strict_settlement_committed",
                    "status": "resolved",
                    "required_record_ids": [
                        f"{value:064x}" for value in range(1, 7)
                    ],
                },
            ),
        ],
    )
    _freeze_batch(
        run_root / "groups" / zero_decision_group,
        source="clob_market_ws",
        batch_id="zero-decision-group-capture",
        payloads=[
            _recorder_payload(
                source="clob_market_ws",
                schema_version="clob-market-ws.book.v1",
                event_type="book",
                received_at_ms=OPEN_MS,
                payload={"asset_id": "zero-decision-token"},
            )
        ],
    )
    active_slug = f"eth-updown-5m-{(OPEN_MS + 300_000) // 1_000}"
    active_group = "active-terminal-group"
    active_identity = {
        "slug": active_slug,
        "condition_id": "0x" + "ab" * 32,
        "opens_at_ms": OPEN_MS + 300_000,
        "closes_at_ms": CLOSE_MS + 300_000,
    }
    _freeze_batch(
        run_root / "control",
        source="dynamic_short_crypto_service",
        batch_id="active-terminal-pair",
        payloads=[
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="full_window_capture_close_checkpoint",
                received_at_ms=CLOSE_MS + 320_000,
                payload={
                    **active_identity,
                    "schema_version": (
                        "edge-lab.execution-replay-close-checkpoint.v1"
                    ),
                    "evidence_role": (
                        "full_window_capture_close_checkpoint"
                    ),
                    "group_id": active_group,
                    "finalized_through_ms": CLOSE_MS + 320_000,
                },
            ),
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="decision_to_trade_l2_closure",
                received_at_ms=CLOSE_MS + 321_000,
                payload={
                    **active_identity,
                    "schema_version": (
                        "edge-lab.execution-replay-l2-closure.v1"
                    ),
                    "evidence_role": "decision_to_trade_l2_closure",
                    "group_id": active_group,
                    "decision_count": 1,
                    "decision_record_ids": ["7" * 64],
                    "public_trade_count": 0,
                    "public_trade_snapshot_record_ids": [],
                    "closed_through_ms": CLOSE_MS + 300_000,
                },
            ),
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="pessimistic_submit_latency",
                received_at_ms=CLOSE_MS + 322_000,
                payload={
                    **active_identity,
                    "schema_version": (
                        "edge-lab.execution-replay-submit-latency.v1"
                    ),
                    "evidence_role": "pessimistic_submit_latency",
                    "latency_ms": 250,
                    "decision_record_id": "7" * 64,
                },
            ),
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="settlement_operation_cost",
                received_at_ms=CLOSE_MS + 323_000,
                payload={
                    **active_identity,
                    "schema_version": (
                        "edge-lab.execution-replay-settlement-cost.v1"
                    ),
                    "evidence_role": "settlement_operation_cost",
                    "amount": "1",
                    "currency": "USDC",
                    "decision_record_id": "7" * 64,
                },
            ),
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="settlement_reconciled",
                received_at_ms=CLOSE_MS + 324_000,
                payload={
                    **active_identity,
                    "schema_version": (
                        "edge-lab.short-crypto-settlement-commit.v1"
                    ),
                    "action": "strict_settlement_committed",
                    "status": "resolved",
                    "required_record_ids": [
                        f"{value:064x}" for value in range(1, 7)
                    ],
                },
            ),
        ],
    )
    active_partial = (
        run_root
        / "groups"
        / active_group
        / "raw"
        / "clob_market_ws"
        / "active.jsonl.partial"
    )
    active_partial.parent.mkdir(parents=True, exist_ok=True)
    active_partial.write_bytes(b"")
    _freeze_batch(
        run_root / "groups" / "unrelated-group",
        source="clob_market_ws",
        batch_id="unrelated-l2",
        payloads=[
            _recorder_payload(
                source="clob_market_ws",
                schema_version="clob-market-ws.book.v1",
                event_type="book",
                received_at_ms=OPEN_MS,
                payload={"asset_id": "unrelated-token"},
            )
        ],
    )
    original_manifest_records = replay_assembly._manifest_records

    def candidate_scope_records(
        manifest_path: Path,
    ) -> tuple[dict[str, object], ...]:
        if "unrelated-group" in manifest_path.parts:
            raise AssertionError(
                f"unrelated group payload was read: {manifest_path}"
            )
        return original_manifest_records(manifest_path)

    monkeypatch.setattr(
        replay_assembly,
        "_manifest_records",
        candidate_scope_records,
    )

    candidates = discover_replay_candidates(
        (run_root,),
        main_capture_root=main_root,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.slug == f"btc-updown-5m-{OPEN_MS // 1_000}"
    assert candidate.group_id == "captured-public-fixture-group"
    assert set(candidate.required_role_ids) == {
        "durable_announcement_record_id",
        "durable_gamma_record_id",
        "durable_chainlink_open_record_id",
        "durable_chainlink_close_record_id",
        "durable_close_pong_before_record_id",
        "durable_close_pong_after_record_id",
        "atomic_settlement_commit_record_id",
        "full_window_capture_close_checkpoint_id",
        "decision_to_trade_l2_closure_id",
        "pessimistic_submit_latency_evidence_id",
        "settlement_operation_cost_evidence_id",
    }
    built = build_replay_request_for_candidate(
        candidate,
        request_base=tmp_path / "research",
    )
    promoted = verify_and_promote_execution_replay(
        built.request_path
    )
    assert promoted.status == "promoted_counterfactual"


def test_assembly_rejects_invalid_zero_decision_terminal_settlement(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "dynamic-run"
    main_root = tmp_path / "main-capture"
    main_root.mkdir()
    slug = f"eth-updown-5m-{OPEN_MS // 1_000}"
    identity = {
        "slug": slug,
        "condition_id": "0x" + "ef" * 32,
        "opens_at_ms": OPEN_MS,
        "closes_at_ms": CLOSE_MS,
    }
    _freeze_batch(
        run_root / "control",
        source="dynamic_short_crypto_service",
        batch_id="invalid-zero-decision-settlement",
        payloads=[
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="decision_to_trade_l2_closure",
                received_at_ms=CLOSE_MS + 20_000,
                payload={
                    **identity,
                    "schema_version": (
                        "edge-lab.execution-replay-l2-closure.v1"
                    ),
                    "evidence_role": "decision_to_trade_l2_closure",
                    "group_id": "zero-decision-group",
                    "decision_count": 0,
                    "decision_record_ids": [],
                },
            ),
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="settlement_reconciled",
                received_at_ms=CLOSE_MS + 21_000,
                payload={
                    **identity,
                    "schema_version": (
                        "edge-lab.short-crypto-settlement-commit.v1"
                    ),
                    "action": "strict_settlement_committed",
                    "status": "resolved",
                    "required_record_ids": [],
                },
            ),
        ],
    )

    with pytest.raises(ReplayAssemblyError) as raised:
        discover_replay_candidates(
            (run_root,),
            main_capture_root=main_root,
        )

    assert raised.value.code == "settlement_dependency_invalid"


def test_assembly_rejects_invalid_zero_decision_terminal_identity(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "dynamic-run"
    main_root = tmp_path / "main-capture"
    main_root.mkdir()
    slug = f"eth-updown-5m-{OPEN_MS // 1_000}"
    identity = {
        "slug": slug,
        "condition_id": "0x" + "ef" * 32,
        "opens_at_ms": OPEN_MS,
        "closes_at_ms": CLOSE_MS,
    }
    _freeze_batch(
        run_root / "control",
        source="dynamic_short_crypto_service",
        batch_id="invalid-zero-decision-identity",
        payloads=[
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="decision_to_trade_l2_closure",
                received_at_ms=CLOSE_MS + 20_000,
                payload={
                    **identity,
                    "schema_version": (
                        "edge-lab.execution-replay-l2-closure.v1"
                    ),
                    "evidence_role": "decision_to_trade_l2_closure",
                    "group_id": "",
                    "decision_count": 0,
                    "decision_record_ids": [],
                },
            ),
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="settlement_reconciled",
                received_at_ms=CLOSE_MS + 21_000,
                payload={
                    **identity,
                    "schema_version": (
                        "edge-lab.short-crypto-settlement-commit.v1"
                    ),
                    "action": "strict_settlement_committed",
                    "status": "resolved",
                    "required_record_ids": [
                        f"{value:064x}" for value in range(1, 7)
                    ],
                },
            ),
        ],
    )

    with pytest.raises(ReplayAssemblyError) as raised:
        discover_replay_candidates(
            (run_root,),
            main_capture_root=main_root,
        )

    assert raised.value.code == "candidate_identity_invalid"


def test_assembly_skips_group_payloads_without_terminal_service_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "dynamic-run"
    main_root = tmp_path / "main-capture"
    main_root.mkdir()
    _freeze_batch(
        main_root,
        source="rtds_ws",
        batch_id="irrelevant-main-record",
        payloads=[
            _recorder_payload(
                source="rtds_ws",
                schema_version="rtds-ws.crypto-prices.v1",
                event_type="crypto_prices",
                received_at_ms=OPEN_MS,
                payload={"symbol": "btc/usd", "value": "100"},
            )
        ],
    )
    slug = f"btc-updown-5m-{OPEN_MS // 1_000}"
    _freeze_batch(
        run_root / "control",
        source="dynamic_short_crypto_service",
        batch_id="non-terminal-service-record",
        payloads=[
            _recorder_payload(
                source="dynamic_short_crypto_service",
                schema_version=(
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                event_type="capture_decision",
                received_at_ms=OPEN_MS - 60_000,
                payload={"slug": slug},
            )
        ],
    )
    _freeze_batch(
        run_root / "groups" / "irrelevant-group",
        source="clob_market_ws",
        batch_id="irrelevant-l2",
        payloads=[_announcement()],
    )
    original_manifest_records = replay_assembly._manifest_records

    def service_records_only(
        manifest_path: Path,
    ) -> tuple[dict[str, object], ...]:
        if manifest_path.parent.name != "dynamic_short_crypto_service":
            raise AssertionError(
                f"non-service payload was read: {manifest_path}"
            )
        return original_manifest_records(manifest_path)

    monkeypatch.setattr(
        replay_assembly,
        "_manifest_records",
        service_records_only,
    )

    assert (
        discover_replay_candidates(
            (run_root,),
            main_capture_root=main_root,
        )
        == ()
    )


def test_captured_public_trade_direction_requires_taker_only_data_api(
    tmp_path: Path,
) -> None:
    request_path = complete_fixture(
        tmp_path / "valid",
        include_data_api_trade=True,
    )
    request, _, base = _read_request(request_path)
    frozen = _load_frozen_evidence(request, base=base)
    target = _strict_target(frozen.records)

    trades = _public_trades(
        frozen,
        target=target,
        evidence_mode="captured_public",
    )

    assert len(trades) == 1
    assert trades[0].transaction_hash == TRANSACTION_HASH
    assert trades[0].aggressor_side.value == "sell"
    assert trades[0].source_timestamp_ms == OPEN_MS + 4_000
    assert trades[0].match_path == "same_token_sell"
    assert frozen.records_by_id[trades[0].evidence_record_id]["source"] == (
        "data_api_trades"
    )

    unsafe_path = complete_fixture(
        tmp_path / "unsafe",
        include_data_api_trade=True,
        data_api_taker_only=False,
    )
    unsafe_request, _, unsafe_base = _read_request(unsafe_path)
    unsafe_frozen = _load_frozen_evidence(
        unsafe_request,
        base=unsafe_base,
    )
    with pytest.raises(
        PromotionRejection,
        match="public_trade_taker_contract_invalid",
    ):
        _public_trades(
            unsafe_frozen,
            target=_strict_target(unsafe_frozen.records),
            evidence_mode="captured_public",
        )


def test_taker_buy_on_complement_maps_to_mint_capacity_for_maker_buy(
    tmp_path: Path,
) -> None:
    request_path = complete_fixture(tmp_path)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    manifest, _ = _freeze_batch(
        tmp_path,
        source="data_api_trades",
        batch_id="complementary-buy",
        payloads=[
            _data_api_trade_snapshot(
                side="BUY",
                token_id=DOWN_TOKEN_ID,
                price="0.60",
            )
        ],
    )
    request["source_manifests"].append(
        {
            "path": manifest.relative_to(tmp_path).as_posix(),
            "sha256": _manifest_sha256(manifest),
        }
    )
    request_path.write_bytes(canonical_json_bytes(request) + b"\n")
    parsed_request, _, base = _read_request(request_path)
    frozen = _load_frozen_evidence(parsed_request, base=base)

    trades = _public_trades(
        frozen,
        target=_strict_target(frozen.records),
        evidence_mode="captured_public",
    )

    assert len(trades) == 1
    assert trades[0].observed_token_id == DOWN_TOKEN_ID
    assert trades[0].observed_taker_side.value == "buy"
    assert trades[0].observed_price == Decimal("0.60")
    assert trades[0].token_id == UP_TOKEN_ID
    assert trades[0].aggressor_side.value == "sell"
    assert trades[0].price == Decimal("0.40")
    assert trades[0].match_path == "complementary_buy_mint"


def test_distinct_data_api_matches_in_one_transaction_keep_separate_capacity(
    tmp_path: Path,
) -> None:
    request_path = complete_fixture(tmp_path)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    manifest, _ = _freeze_batch(
        tmp_path,
        source="data_api_trades",
        batch_id="same-transaction-distinct-matches",
        payloads=[
            _data_api_trade_snapshot(
                side="SELL",
                token_id=UP_TOKEN_ID,
                price="0.40",
            ),
            _data_api_trade_snapshot(
                side="BUY",
                token_id=DOWN_TOKEN_ID,
                price="0.60",
            ),
        ],
    )
    request["source_manifests"].append(
        {
            "path": manifest.relative_to(tmp_path).as_posix(),
            "sha256": _manifest_sha256(manifest),
        }
    )
    request_path.write_bytes(canonical_json_bytes(request) + b"\n")
    parsed_request, _, base = _read_request(request_path)
    frozen = _load_frozen_evidence(parsed_request, base=base)

    trades = _public_trades(
        frozen,
        target=_strict_target(frozen.records),
        evidence_mode="captured_public",
    )

    assert len(trades) == 2
    assert {trade.transaction_hash for trade in trades} == {
        TRANSACTION_HASH
    }
    assert len({trade.event_key for trade in trades}) == 2
    assert len({trade.record_id for trade in trades}) == 2


def test_post_decision_crossing_book_update_invalidates_maker_fill(
    tmp_path: Path,
) -> None:
    result = verify_and_promote_execution_replay(
        complete_fixture(tmp_path, crossing_book_update=True)
    )

    assert result.reason_codes == ("explainable_fill_missing",)
    assert result.output_dir is None


def test_trade_that_occurred_before_decision_cannot_fill_when_seen_later(
    tmp_path: Path,
) -> None:
    result = verify_and_promote_execution_replay(
        complete_fixture(
            tmp_path,
            decision_received_at_ms=OPEN_MS + 5_000,
            trade_received_at_ms=OPEN_MS + 6_000,
        )
    )

    assert result.reason_codes == ("public_trade_missing",)
    assert result.output_dir is None
