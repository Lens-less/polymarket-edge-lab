from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.edge_lab import btc_twap_continuous_rtds as continuous_rtds
from src.edge_lab.data_store import CaptureStore


class QueueWebSocket:
    def __init__(self) -> None:
        import asyncio

        self.incoming: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


class QueueWebSocketFactory:
    def __init__(self, connections: Mapping[str, Sequence[QueueWebSocket]]) -> None:
        import asyncio

        self.connections = {url: asyncio.Queue[QueueWebSocket]() for url in connections}
        for url, sockets in connections.items():
            for socket in sockets:
                self.connections[url].put_nowait(socket)

    async def connect(self, url: str) -> QueueWebSocket:
        return await self.connections[url].get()


class IdleSnapshotClient:
    async def fetch_snapshot(self, kind: str, *, asset_ids: Sequence[str] = ()) -> Any:
        return {"kind": kind, "asset_ids": list(asset_ids)}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_build_recorder_config_is_rtds_only_btc_twap_sixty() -> None:
    config = continuous_rtds.build_btc_twap_recorder_config()

    assert config.clob_enabled is False
    assert config.clob_asset_ids == ()
    assert config.snapshot_intervals == {}
    assert config.rtds_subscriptions == (
        {
            "topic": "crypto_prices_twap_sixty",
            "type": "update",
            "filters": '{"symbol":"btc/usd"}',
        },
    )


def test_official_observation_uses_chainlink_time_not_publisher_time() -> None:
    message = {
        "topic": "crypto_prices_twap_sixty",
        "type": "update",
        "timestamp": 1_785_178_800_123,
        "payload": {
            "symbol": "btc/usd",
            "timestamp": 1_785_178_800_000,
            "value": 65_000.5,
            "full_accuracy_value": "65000500000000000000000",
            "window_s": 60,
        },
    }

    assert continuous_rtds._official_observation(message) == (
        1_785_178_800_000,
        "65000500000000000000000",
    )
    message["payload"]["window_s"] = 30
    assert continuous_rtds._official_observation(message) is None


def test_validate_only_returns_public_only_guarded_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = continuous_rtds.main(
        [
            "--data-root",
            str(tmp_path),
            "--validate-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema_version": continuous_rtds.VALIDATION_SCHEMA_VERSION,
        "valid": True,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
        "topic": "crypto_prices_twap_sixty",
        "symbol": "btc/usd",
        "status_path": str(tmp_path / "service" / "status.json"),
    }


@pytest.mark.asyncio
async def test_run_continuous_capture_persists_status_and_integrity_summary(
    tmp_path: Path,
) -> None:
    rtds_one = QueueWebSocket()
    await rtds_one.incoming.put(
        json.dumps(
            {
                "topic": "crypto_prices_twap_sixty",
                "timestamp": 1_721_808_001_000,
                "type": "update",
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp": 1_721_808_001_000,
                    "value": 63_123.45,
                    "full_accuracy_value": "63123450000000000000000",
                    "window_s": 60,
                },
            }
        )
    )
    await rtds_one.incoming.put(ConnectionError("synthetic disconnect"))
    rtds_two = QueueWebSocket()
    await rtds_two.incoming.put(
        json.dumps(
            {
                "topic": "crypto_prices_twap_sixty",
                "timestamp": 1_721_808_061_000,
                "type": "update",
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp": 1_721_808_061_000,
                    "value": 63_199.99,
                    "full_accuracy_value": "63199990000000000000000",
                    "window_s": 60,
                },
            }
        )
    )

    config = continuous_rtds.ContinuousBtcTwapRtdsConfig(
        data_root=tmp_path,
        reconnect_initial_seconds=0.0,
        reconnect_max_seconds=0.01,
    )
    summary = await continuous_rtds.run_continuous_capture(
        config,
        run_for_seconds=0.1,
        websocket_factory=QueueWebSocketFactory(
            {
                continuous_rtds.build_btc_twap_recorder_config().rtds_url: [
                    rtds_one,
                    rtds_two,
                ]
            }
        ),
        snapshot_client=IdleSnapshotClient(),
    )

    assert summary["schema_version"] == continuous_rtds.SUMMARY_SCHEMA_VERSION
    assert summary["capture_error"] is None
    assert summary["integrity"] == CaptureStore(tmp_path).audit_integrity()
    assert summary["status"]["connection_epoch"] == 2
    assert summary["status"]["disconnect_count"] == 2
    assert summary["status"]["reconnect_count"] == 1
    assert summary["status"]["error_count"] == 1
    assert summary["status"]["invalid_data_record_count"] == 0
    assert summary["status"]["coverage_healthy"] is False
    assert summary["status"]["max_observation_gap_ms"] == 60_000
    assert summary["status"]["last_full_accuracy_value"] == ("63199990000000000000000")
    assert summary["status"]["last_source_observed_at"] == (
        "2024-07-24T08:01:01.000000Z"
    )
    assert summary["status"]["last_source_received_at"].endswith("Z")
    assert summary["status"]["checkpoint_reasons"]["disconnect"] == 2
    assert summary["status"]["checkpoint_reasons"]["stop"] == 1

    status = _read_json(tmp_path / "service" / "status.json")
    assert status["schema_version"] == continuous_rtds.STATUS_SCHEMA_VERSION
    assert status["phase"] == "stopped"
    assert status["status_sha256"]
    assert status["capture_integrity"] == summary["integrity"]
    assert status["heartbeat_at"].endswith("Z")

    persisted_summary = _read_json(tmp_path / "capture-summary.json")
    assert persisted_summary == summary

    data_payloads: list[dict[str, Any]] = []
    for raw_path in sorted((tmp_path / "raw" / "rtds_ws").glob("*.jsonl")):
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("payload", {}).get("kind") != "data":
                continue
            payload = record["payload"].get("payload")
            if isinstance(payload, dict):
                data_payloads.append(payload)
    data_payloads.sort(key=lambda item: int(item["timestamp"]))
    assert data_payloads == [
        {
            "topic": "crypto_prices_twap_sixty",
            "timestamp": 1721808001000,
            "type": "update",
            "payload": {
                "symbol": "btc/usd",
                "timestamp": 1721808001000,
                "value": "63123.45",
                "full_accuracy_value": "63123450000000000000000",
                "window_s": 60,
            },
        },
        {
            "topic": "crypto_prices_twap_sixty",
            "timestamp": 1721808061000,
            "type": "update",
            "payload": {
                "symbol": "btc/usd",
                "timestamp": 1721808061000,
                "value": "63199.99",
                "full_accuracy_value": "63199990000000000000000",
                "window_s": 60,
            },
        },
    ]


def test_continuous_health_rejects_gap_disconnect_invalid_or_tampered_status() -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    tracker = continuous_rtds._ContinuousStatusTracker(
        data_root=Path("C:/capture"),
        status_path=Path("C:/capture/status.json"),
    )
    tracker.phase = "capturing"
    tracker.heartbeat_at = now.isoformat().replace("+00:00", "Z")
    tracker.data_record_count = 10
    tracker.max_observation_gap_ms = 1_000
    healthy_status = tracker.to_document()

    healthy = continuous_rtds.evaluate_continuous_rtds_health(
        healthy_status,
        now=now,
    )
    unhealthy_status = dict(healthy_status)
    unhealthy_status["disconnect_count"] = 1
    unhealthy_status["max_observation_gap_ms"] = 3_000
    unhealthy_status["invalid_data_record_count"] = 1
    unhealthy_status["status_sha256"] = continuous_rtds._status_sha256(unhealthy_status)
    unhealthy = continuous_rtds.evaluate_continuous_rtds_health(
        unhealthy_status,
        now=now,
    )
    tampered = dict(healthy_status)
    tampered["data_record_count"] = 11
    tampered_result = continuous_rtds.evaluate_continuous_rtds_health(
        tampered,
        now=now,
    )

    assert healthy["healthy"] is True
    assert unhealthy["healthy"] is False
    assert "connection_gap_or_error_present" in unhealthy["failures"]
    assert "official_observation_gap_exceeded" in unhealthy["failures"]
    assert "invalid_official_observation_present" in unhealthy["failures"]
    assert tampered_result["healthy"] is False
    assert "status_hash_mismatch" in tampered_result["failures"]
