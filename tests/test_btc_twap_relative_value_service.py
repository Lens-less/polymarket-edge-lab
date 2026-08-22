"""Public-contract tests for the continuous BTC TWAP paper service."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

from src.edge_lab.btc_twap_relative_value_service import (
    LEGACY_EVIDENCE_TRACK_ID,
    CapturePlan,
    ClockSyncMeasurement,
    ContinuousServiceConfig,
    DiskCapacityError,
    ServiceStatus,
    build_capture_plan,
    evaluate_service_health,
    fetch_gamma_market_by_slug,
    load_service_config,
    measure_sntp_clock_sync,
    parse_sntp_output,
    public_session,
    require_disk_capacity,
    validate_preregistration_runtime_identity,
    write_service_status,
)
from src.edge_lab.capture_runtime import CaptureStoreRecorderSink
from src.edge_lab.data_store import CaptureStore


def _market_fixture(
    *,
    horizon: str,
    opens_at: int,
    market_id: str,
    condition_id: str,
    up_token: str,
    down_token: str,
) -> tuple[dict[str, object], dict[str, object]]:
    duration = {"5m": 300, "15m": 900}[horizon]
    window = {"5m": 30, "15m": 60}[horizon]
    source = f"https://data.chain.link/streams/btc-usd-twap-{window}s-streams"
    end_date = (
        datetime.fromtimestamp(
            opens_at + duration,
            tz=timezone.utc,
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    gamma = {
        "id": market_id,
        "conditionId": condition_id,
        "slug": f"btc-updown-{horizon}-{opens_at}",
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": f'["{up_token}", "{down_token}"]',
        "endDate": end_date,
        "resolutionSource": source,
        "description": (
            "This market resolves Up when the closing TWAP is greater than or "
            "equal to the opening TWAP, using the BTC/USD TWAP data stream "
            f"available at {source}."
        ),
    }
    clob = {
        "c": condition_id,
        "t": [
            {"t": up_token, "o": "Up"},
            {"t": down_token, "o": "Down"},
        ],
        "mos": 5,
        "mts": 0.01,
        "ao": True,
        "itode": True,
        "r": {},
        "fd": {"r": 0.07, "e": 1, "to": True},
    }
    return gamma, clob


def _config(tmp_path: Path) -> ContinuousServiceConfig:
    preregistration = tmp_path / "PREREGISTRATION.json"
    preregistration.write_text("{}", encoding="utf-8")
    return ContinuousServiceConfig(
        data_root=tmp_path / "data",
        research_root=tmp_path / "research",
        preregistration_path=preregistration,
        settlement_grace_seconds=360,
        retry_seconds=30,
        status_interval_seconds=15,
        minimum_free_disk_bytes=12 * 1024**3,
        max_history_roots=2,
        decision_tau_seconds=60,
        evidence_track_id="paper-v05",
    )


def _write_strategy_fixture(tmp_path: Path) -> tuple[Path, str]:
    research_dir = tmp_path / "research" / "paper"
    research_dir.mkdir(parents=True, exist_ok=True)
    strategy_path = research_dir / "STRATEGY_SPEC.md"
    strategy_path.write_text("# frozen strategy\n", encoding="utf-8")
    return strategy_path, hashlib.sha256(strategy_path.read_bytes()).hexdigest()


def _preregistration_document(
    tmp_path: Path,
    *,
    evidence_track_id: str = "paper-v05",
) -> dict[str, object]:
    strategy_path, strategy_hash = _write_strategy_fixture(tmp_path)
    return {
        "scope": {
            "paper_only": True,
            "live_orders_disabled": True,
            "prospective_only_after": "2026-08-13T06:00:00Z",
            "evidence_track_id": evidence_track_id,
        },
        "frozen_strategy": {
            "decision_tau_seconds": [60],
            "clock_sync": {"source": "SNTP time.apple.com"},
        },
        "strategy_spec": {
            "path": str(strategy_path.relative_to(tmp_path)).replace("\\", "/"),
            "sha256": strategy_hash,
        },
    }


def test_preregistration_runtime_identity_rejects_mismatched_deployment_marker(
    tmp_path: Path,
) -> None:
    preregistration_path = tmp_path / "research" / "paper" / "PREREGISTRATION.json"
    preregistration_path.parent.mkdir(parents=True, exist_ok=True)
    config = ContinuousServiceConfig(
        **{
            **_config(tmp_path).__dict__,
            "preregistration_path": preregistration_path,
        }
    )
    preregistration = _preregistration_document(tmp_path)
    preregistration["repository_head"] = "a" * 40
    (tmp_path / ".implementation-revision").write_text(
        "b" * 40 + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="implementation revision"):
        validate_preregistration_runtime_identity(
            preregistration,
            config=config,
        )


def test_capture_plan_targets_the_next_same_expiry_pair(tmp_path: Path) -> None:
    now_seconds = 1_786_531_810
    expiry_seconds = 1_786_532_400
    gamma_15, clob_15 = _market_fixture(
        horizon="15m",
        opens_at=expiry_seconds - 900,
        market_id="15-market",
        condition_id="0x" + "15" * 32,
        up_token="15-up",
        down_token="15-down",
    )
    gamma_5, clob_5 = _market_fixture(
        horizon="5m",
        opens_at=expiry_seconds - 300,
        market_id="5-market",
        condition_id="0x" + "05" * 32,
        up_token="5-up",
        down_token="5-down",
    )

    plan = build_capture_plan(
        config=_config(tmp_path),
        now_ms=now_seconds * 1_000,
        attempt_id="attempt-001",
        clock_sync=ClockSyncMeasurement(
            measured_at_raw_ms=now_seconds * 1_000 - 1_000,
            offset_seconds="0.291699461",
            uncertainty_seconds="0.050614",
            source="SNTP time.apple.com",
        ),
        gamma_5=gamma_5,
        clob_5=clob_5,
        gamma_15=gamma_15,
        clob_15=clob_15,
    )

    assert isinstance(plan, CapturePlan)
    assert plan.expiry_seconds == expiry_seconds
    assert plan.duration_seconds == 950
    assert plan.capture_config["asset_ids"] == [
        "15-up",
        "15-down",
        "5-up",
        "5-down",
    ]
    assert all(
        len(target["rule_hash"]) == 64 and len(target["rules_text_sha256"]) == 64
        for target in plan.capture_config["targets"]
    )
    assert all(
        target["tick_size"] == "0.01"
        and target["minimum_order_size"] == "5"
        and target["taker_delay_ms"] == 250
        and target["accepting_orders"] is True
        and target["fee_schedule"]
        == {"rate": "0.07", "exponent": "1", "taker_only": True}
        for target in plan.capture_config["targets"]
    )
    assert [item["topic"] for item in plan.capture_config["rtds_subscriptions"]] == [
        "crypto_prices",
        "crypto_prices_twap_thirty",
        "crypto_prices_twap_sixty",
    ]
    assert plan.capture_config["checkpoint_every_records"] == 10_000
    assert plan.capture_config["snapshot_intervals"] == {
        "clob": 5,
        "trades": 30,
        "rules": 30,
    }
    assert plan.capture_config["persist_raw_clob_frames"] is False
    assert plan.capture_config["persist_reconstructed_full_depth_frames"] is True
    assert plan.capture_config["persist_top_of_book_changes"] is False
    assert plan.capture_config["capture_started_at_ms"] == now_seconds * 1_000
    assert plan.capture_config["evidence_track_id"] == "paper-v05"
    assert plan.capture_config["clock_sync"] == {
        "schema_version": "btc-twap-clock-sync.v1",
        "source": "SNTP time.apple.com",
        "measured_at_raw_ms": now_seconds * 1_000 - 1_000,
        "offset_seconds": "0.291699461",
        "uncertainty_seconds": "0.050614",
        "causal_receipt_offset_ms": 343,
        "uncertainty_ms": 51,
        "system_clock_mutated": False,
    }
    assert plan.capture_root.name == "attempt-001"


def test_capture_plan_can_bootstrap_a_bounded_future_expiry(tmp_path: Path) -> None:
    now_seconds = 1_786_531_810
    expiry_seconds = 1_786_533_300
    gamma_15, clob_15 = _market_fixture(
        horizon="15m",
        opens_at=expiry_seconds - 900,
        market_id="future-15-market",
        condition_id="0x" + "15" * 32,
        up_token="future-15-up",
        down_token="future-15-down",
    )
    gamma_5, clob_5 = _market_fixture(
        horizon="5m",
        opens_at=expiry_seconds - 300,
        market_id="future-5-market",
        condition_id="0x" + "05" * 32,
        up_token="future-5-up",
        down_token="future-5-down",
    )

    plan = build_capture_plan(
        config=_config(tmp_path),
        now_ms=now_seconds * 1_000,
        attempt_id="bootstrap-attempt",
        clock_sync=ClockSyncMeasurement(
            measured_at_raw_ms=now_seconds * 1_000,
            offset_seconds="0.126401",
            uncertainty_seconds="0.052",
        ),
        gamma_5=gamma_5,
        clob_5=clob_5,
        gamma_15=gamma_15,
        clob_15=clob_15,
        expiry_seconds=expiry_seconds,
    )

    assert plan.expiry_seconds == expiry_seconds
    assert plan.duration_seconds == 1_850


def test_sntp_parser_freezes_conservative_causal_receipt_offset() -> None:
    output = """
selected:
+0.291699461 +/- 0.050614 time.apple.com 17.253.114.35
"""

    measurement = parse_sntp_output(output, measured_at_raw_ms=123_000)

    assert measurement.offset_seconds == "0.291699461"
    assert measurement.uncertainty_seconds == "0.050614"
    assert measurement.causal_receipt_offset_ms == 343
    assert measurement.uncertainty_ms == 51
    assert measurement.to_document()["system_clock_mutated"] is False


def test_sntp_measurement_uses_best_uncertainty_across_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = iter(
        (
            (0, "+0.126386 +/- 0.102 time.apple.com 17.253.114.35"),
            (1, "temporary network failure"),
            (0, "+0.126401 +/- 0.052 time.apple.com 17.253.114.35"),
        )
    )
    calls: list[tuple[object, ...]] = []

    class Completed:
        def __init__(self, returncode: int, output: str) -> None:
            self.returncode = returncode
            self.stdout = output if returncode == 0 else ""
            self.stderr = output if returncode != 0 else ""

    def fake_run(*args: object, **kwargs: object) -> Completed:
        calls.append(args)
        return Completed(*next(outputs))

    monkeypatch.setattr(
        "src.edge_lab.btc_twap_relative_value_service.subprocess.run",
        fake_run,
    )

    measurement = measure_sntp_clock_sync()

    assert len(calls) == 3
    assert measurement.offset_seconds == "0.126401"
    assert measurement.uncertainty_seconds == "0.052"
    assert measurement.uncertainty_ms == 52


def test_sntp_measurement_fails_after_three_unsuccessful_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class Completed:
        returncode = 1
        stdout = ""
        stderr = "temporary network failure"

    def fake_run(*args: object, **kwargs: object) -> Completed:
        nonlocal calls
        calls += 1
        return Completed()

    monkeypatch.setattr(
        "src.edge_lab.btc_twap_relative_value_service.subprocess.run",
        fake_run,
    )

    with pytest.raises(RuntimeError, match="public SNTP clock measurement failed"):
        measure_sntp_clock_sync()

    assert calls == 3


def test_disk_guard_stops_before_the_reserved_free_space_is_consumed(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    require_disk_capacity(config, free_bytes=13 * 1024**3)
    with pytest.raises(DiskCapacityError, match="disk_capacity_below_reserved_floor"):
        require_disk_capacity(config, free_bytes=11 * 1024**3)


def test_service_config_freezes_multiple_prospective_decision_ticks(
    tmp_path: Path,
) -> None:
    preregistration = tmp_path / "PREREGISTRATION.json"
    preregistration.write_text("{}", encoding="utf-8")
    path = tmp_path / "SERVICE_CONFIG.json"
    path.write_text(
        __import__("json").dumps(
            {
                "schema_version": "btc-twap-relative-value-continuous-service.v1",
                "data_root": str(tmp_path / "data"),
                "research_root": str(tmp_path / "research"),
                "preregistration_path": str(preregistration),
                "settlement_grace_seconds": 360,
                "retry_seconds": 30,
                "status_interval_seconds": 15,
                "minimum_free_disk_bytes": 12 * 1024**3,
                "max_history_roots": 2,
                "decision_tau_seconds": [240, 180, 120, 60],
            }
        ),
        encoding="utf-8",
    )

    config = load_service_config(path)

    assert config.decision_tau_seconds == (240, 180, 120, 60)
    assert config.evidence_track_id == LEGACY_EVIDENCE_TRACK_ID


def test_service_config_loads_explicit_evidence_track_id(tmp_path: Path) -> None:
    preregistration = tmp_path / "PREREGISTRATION.json"
    preregistration.write_text("{}", encoding="utf-8")
    path = tmp_path / "SERVICE_CONFIG.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "btc-twap-relative-value-continuous-service.v1",
                "data_root": str(tmp_path / "data"),
                "research_root": str(tmp_path / "research"),
                "preregistration_path": str(preregistration),
                "settlement_grace_seconds": 360,
                "retry_seconds": 30,
                "status_interval_seconds": 15,
                "minimum_free_disk_bytes": 12 * 1024**3,
                "max_history_roots": 2,
                "decision_tau_seconds": [240, 180, 120, 60],
                "evidence_track_id": "paper-v05",
            }
        ),
        encoding="utf-8",
    )

    config = load_service_config(path)

    assert config.evidence_track_id == "paper-v05"


def test_validate_preregistration_runtime_identity_accepts_matching_runtime(
    tmp_path: Path,
) -> None:
    preregistration = tmp_path / "research" / "paper" / "PREREGISTRATION.json"
    preregistration.parent.mkdir(parents=True, exist_ok=True)
    prereg_document = _preregistration_document(tmp_path)
    preregistration.write_text(json.dumps(prereg_document), encoding="utf-8")
    config = _config(tmp_path)
    config = ContinuousServiceConfig(
        **{**config.__dict__, "preregistration_path": preregistration}
    )

    validate_preregistration_runtime_identity(prereg_document, config=config)


@pytest.mark.parametrize(
    ("mutator", "pattern"),
    [
        (
            lambda doc: doc["scope"].__setitem__(
                "prospective_only_after", "2026-08-13"
            ),
            "UTC timestamp",
        ),
        (
            lambda doc: doc["scope"].__setitem__("paper_only", False),
            "paper_only=true",
        ),
        (
            lambda doc: doc["scope"].__setitem__("evidence_track_id", "other-track"),
            "does not match",
        ),
        (
            lambda doc: doc["frozen_strategy"].__setitem__(
                "decision_tau_seconds", [240]
            ),
            "decision_tau_seconds",
        ),
        (
            lambda doc: doc["frozen_strategy"]["clock_sync"].__setitem__(
                "source",
                "Chrony Amazon Time Sync Service 169.254.169.123",
            ),
            "clock_sync_source",
        ),
        (
            lambda doc: doc["strategy_spec"].__setitem__("sha256", "0" * 64),
            "sha256",
        ),
    ],
)
def test_validate_preregistration_runtime_identity_fails_closed(
    tmp_path: Path,
    mutator: Callable[[dict[str, object]], None],
    pattern: str,
) -> None:
    preregistration = tmp_path / "research" / "paper" / "PREREGISTRATION.json"
    preregistration.parent.mkdir(parents=True, exist_ok=True)
    prereg_document = _preregistration_document(tmp_path)
    preregistration.write_text(json.dumps(prereg_document), encoding="utf-8")
    config = _config(tmp_path)
    config = ContinuousServiceConfig(
        **{**config.__dict__, "preregistration_path": preregistration}
    )
    mutator(prereg_document)

    with pytest.raises(ValueError, match=pattern):
        validate_preregistration_runtime_identity(prereg_document, config=config)


def test_service_status_is_hashed_and_explicitly_paper_only(tmp_path: Path) -> None:
    path = tmp_path / "service" / "status.json"
    status = ServiceStatus(
        phase="capturing",
        heartbeat_at="2026-08-12T11:00:00Z",
        pid=123,
        child_pid=456,
        current_expiry_seconds=1_786_532_400,
        current_capture_root="/tmp/capture",
        completed_report_count=7,
        latest_summary_path="/tmp/summary.json",
        latest_classification="insufficient_data",
        last_error=None,
    )

    write_service_status(path, status)

    document = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert document["paper_only"] is True
    assert document["new_orders_disabled"] is True
    assert document["authenticated_endpoints_used"] == 0
    assert document["phase"] == "capturing"
    assert len(document["status_sha256"]) == 64


def test_service_health_fails_closed_for_stale_or_tampered_status() -> None:
    status = ServiceStatus(
        phase="capturing",
        heartbeat_at="2026-08-12T11:00:00Z",
        pid=123,
        child_pid=None,
        current_expiry_seconds=1_786_532_400,
        current_capture_root="/tmp/capture",
        completed_report_count=7,
        latest_summary_path="/tmp/summary.json",
        latest_classification="insufficient_data",
        last_error=None,
    ).to_document()

    healthy = evaluate_service_health(
        status,
        now=datetime(2026, 8, 12, 11, 0, 30, tzinfo=timezone.utc),
        process_alive=True,
        free_disk_bytes=13 * 1024**3,
        minimum_free_disk_bytes=12 * 1024**3,
        maximum_heartbeat_age_seconds=90,
    )
    assert healthy["healthy"] is True
    assert healthy["failures"] == []

    stale = evaluate_service_health(
        status,
        now=datetime(2026, 8, 12, 11, 2, tzinfo=timezone.utc),
        process_alive=True,
        free_disk_bytes=13 * 1024**3,
        minimum_free_disk_bytes=12 * 1024**3,
        maximum_heartbeat_age_seconds=90,
    )
    assert stale["healthy"] is False
    assert "heartbeat_stale" in stale["failures"]

    tampered = dict(status)
    tampered["orders_submitted"] = 1
    unsafe = evaluate_service_health(
        tampered,
        now=datetime(2026, 8, 12, 11, 0, 30, tzinfo=timezone.utc),
        process_alive=True,
        free_disk_bytes=13 * 1024**3,
        minimum_free_disk_bytes=12 * 1024**3,
        maximum_heartbeat_age_seconds=90,
    )
    assert unsafe["healthy"] is False
    assert "status_hash_invalid" in unsafe["failures"]
    assert "paper_only_guard_invalid" in unsafe["failures"]


def test_gamma_slug_lookup_discards_response_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = public_session(None)

    class Response:
        status_code = 200

        @staticmethod
        def json() -> list[dict[str, str]]:
            return [{"slug": "btc-updown-5m-123", "id": "market"}]

    def fake_get(*args: object, **kwargs: object) -> Response:
        session.cookies.set("public-edge-cookie", "discard-me")
        return Response()

    monkeypatch.setattr(session, "get", fake_get)
    try:
        market = fetch_gamma_market_by_slug(session, "btc-updown-5m-123")
        assert market["id"] == "market"
        assert not session.cookies
    finally:
        session.close()


@pytest.mark.asyncio
async def test_compact_sink_keeps_target_full_depth_book_without_duplicate_frame(
    tmp_path: Path,
) -> None:
    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(backing, allowed_asset_ids=("token",))
    raw_frame = '{"event_type":"book","timestamp":"1000"}'
    record = {
        "schema_version": "clob-market-ws.book.v1",
        "source": "clob_market_ws",
        "received_at": "2026-08-12T11:00:00Z",
        "event_at": "1970-01-01T00:00:01Z",
        "event_type": "book",
        "kind": "data",
        "sequence": None,
        "session_id": "session-1",
        "connection_id": "connection-1",
        "raw_frame": raw_frame,
        "frame_hash": "a" * 64,
        "payload_hash": "b" * 64,
        "payload": {
            "event_type": "book",
            "timestamp": "1000",
            "market": "condition",
            "asset_id": "token",
            "hash": "c" * 40,
            "bids": [
                {"price": str(index / 100), "size": "1"} for index in range(1, 12)
            ],
            "asks": [
                {"price": str(index / 100), "size": "1"} for index in range(12, 23)
            ],
        },
    }

    await sink.emit(record)
    await sink.checkpoint(
        "clob_market_ws",
        {
            "schema_version": "edge-lab-recorder.checkpoint.v1",
            "source": "clob_market_ws",
            "updated_at": "2026-08-12T11:00:01Z",
            "last_event_at": None,
            "last_sequence": None,
            "last_server_timestamp": None,
            "last_frame_hash": None,
            "last_server_hash": None,
        },
    )
    await sink.close()

    raw_path = next((tmp_path / "capture" / "raw" / "clob_market_ws").glob("*.jsonl"))
    stored = __import__("json").loads(raw_path.read_text(encoding="utf-8"))
    inner = stored["payload"]
    assert "raw_frame" not in inner
    assert inner["frame_hash"] == "a" * 64
    assert len(inner["payload"]["bids"]) == 11
    assert [level["price"] for level in inner["payload"]["bids"]] == [
        "0.11",
        "0.1",
        "0.09",
        "0.08",
        "0.07",
        "0.06",
        "0.05",
        "0.04",
        "0.03",
        "0.02",
        "0.01",
    ]
    assert [level["price"] for level in inner["payload"]["asks"]] == [
        "0.12",
        "0.13",
        "0.14",
        "0.15",
        "0.16",
        "0.17",
        "0.18",
        "0.19",
        "0.2",
        "0.21",
        "0.22",
    ]


@pytest.mark.asyncio
async def test_compact_sink_keeps_causal_clob_snapshots_and_drops_unrelated_records(
    tmp_path: Path,
) -> None:
    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(backing, allowed_asset_ids=("token",))
    base = {
        "schema_version": "source.v1",
        "received_at": "2026-08-12T11:00:00Z",
        "event_at": "2026-08-12T11:00:00Z",
        "kind": "data",
        "sequence": None,
        "session_id": "session-1",
        "connection_id": "connection-1",
        "monotonic_ns": 123_456_789,
        "frame_hash": "a" * 64,
        "payload_hash": "b" * 64,
    }
    assert await sink.emit({**base, "source": "gamma_http"}) is None
    assert (
        await sink.emit(
            {
                **base,
                "source": "clob_http",
                "event_type": "snapshot",
                "payload": {
                    "snapshot_kind": "clob",
                    "responses": [
                        {
                            "resource": "clob_server_time",
                            "raw_json": 1_786_896_000,
                        },
                        {
                            "resource": "clob_book",
                            "request_key": "token",
                            "raw_json": {
                                "asset_id": "token",
                                "timestamp": "1786896000000",
                                "bids": [{"price": "0.49", "size": "50"}],
                                "asks": [{"price": "0.50", "size": "50"}],
                            },
                        },
                        {
                            "resource": "clob_market",
                            "raw_json": {
                                "condition_id": "condition",
                                "fd": {"r": "0.07", "e": 1, "to": True},
                            },
                        },
                    ],
                },
            }
        )
        is not None
    )
    assert (
        await sink.emit(
            {
                **base,
                "source": "clob_market_ws",
                "event_type": "book",
                "payload": {
                    "event_type": "book",
                    "asset_id": "other-token",
                    "bids": [],
                    "asks": [],
                },
            }
        )
        is None
    )
    await sink.close()
    raw_files = tuple((tmp_path / "capture" / "raw" / "clob_http").glob("*.jsonl"))
    assert len(raw_files) == 1
    stored = json.loads(raw_files[0].read_text(encoding="utf-8"))
    assert stored["payload"]["monotonic_ns"] == 123_456_789
    assert stored["payload"]["payload"]["responses"][1]["raw_json"]["asks"] == [
        {"price": "0.50", "size": "50"}
    ]


@pytest.mark.asyncio
async def test_compact_sink_preserves_public_websocket_error_diagnostics(
    tmp_path: Path,
) -> None:
    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(backing, allowed_asset_ids=("token",))
    await sink.emit(
        {
            "schema_version": "edge-lab-recorder.lifecycle.v1",
            "source": "clob_market_ws",
            "received_at": "2026-08-12T11:00:00Z",
            "event_at": None,
            "event_type": "error",
            "kind": "lifecycle",
            "sequence": None,
            "session_id": "session-1",
            "connection_id": "connection-1",
            "detail": {
                "error_code": "websocket_connection_failed",
                "error_type": "ConnectionClosedError",
            },
        }
    )
    await sink.close()

    raw_path = next((tmp_path / "capture" / "raw" / "clob_market_ws").glob("*.jsonl"))
    stored = __import__("json").loads(raw_path.read_text(encoding="utf-8"))
    lifecycle = stored["payload"]
    assert lifecycle["detail"] == {
        "error_code": "websocket_connection_failed",
        "error_type": "ConnectionClosedError",
    }


@pytest.mark.asyncio
async def test_compact_sink_coalesces_target_price_changes_into_causal_books(
    tmp_path: Path,
) -> None:
    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(backing, allowed_asset_ids=("target-token",))
    anchor = {
        "schema_version": "clob-market-ws.book.v1",
        "source": "clob_market_ws",
        "received_at": "1970-01-01T00:00:01Z",
        "event_at": "1970-01-01T00:00:01Z",
        "event_type": "book",
        "kind": "data",
        "sequence": None,
        "session_id": "session-1",
        "connection_id": "connection-1",
        "frame_hash": "0" * 64,
        "payload_hash": "1" * 64,
        "payload": {
            "event_type": "book",
            "timestamp": "1000",
            "market": "condition",
            "asset_id": "target-token",
            "bids": [{"price": "0.49", "size": "20"}],
            "asks": [
                {"price": "0.50", "size": "10"},
                {"price": "0.51", "size": "12"},
            ],
        },
    }
    early_change = {
        "schema_version": "clob-market-ws.price_change.v1",
        "source": "clob_market_ws",
        "received_at": "1970-01-01T00:00:01.100000Z",
        "event_at": "1970-01-01T00:00:01.100000Z",
        "event_type": "price_change",
        "kind": "data",
        "sequence": None,
        "session_id": "session-1",
        "connection_id": "connection-1",
        "frame_hash": "a" * 64,
        "payload_hash": "b" * 64,
        "payload": {
            "event_type": "price_change",
            "market": "condition",
            "timestamp": "1100",
            "price_changes": [
                {
                    "asset_id": "target-token",
                    "price": "0.50",
                    "size": "0",
                    "side": "SELL",
                    "best_bid": "0.49",
                    "best_ask": "0.51",
                    "hash": "c" * 40,
                },
                {
                    "asset_id": "other-token",
                    "price": "0.52",
                    "size": "99",
                    "side": "SELL",
                    "best_bid": "0.48",
                    "best_ask": "0.52",
                    "hash": "d" * 40,
                },
            ],
        },
    }
    due_change = {
        **early_change,
        "received_at": "1970-01-01T00:00:01.250000Z",
        "event_at": "1970-01-01T00:00:01.250000Z",
        "frame_hash": "e" * 64,
        "payload_hash": "f" * 64,
        "payload": {
            "event_type": "price_change",
            "market": "condition",
            "timestamp": "1250",
            "price_changes": [
                {
                    "asset_id": "target-token",
                    "price": "0.51",
                    "size": "11",
                    "side": "SELL",
                    "best_bid": "0.49",
                    "best_ask": "0.51",
                    "hash": "f" * 40,
                }
            ],
        },
    }

    await sink.emit(anchor)
    assert await sink.emit(early_change) is None
    await sink.emit(due_change)
    await sink.close()

    stored = [
        __import__("json").loads(line)
        for raw_path in (tmp_path / "capture" / "raw" / "clob_market_ws").glob(
            "*.jsonl"
        )
        for line in raw_path.read_text(encoding="utf-8").splitlines()
    ]
    data = [item["payload"] for item in stored if item["payload"]["kind"] == "data"]
    assert [item["event_type"] for item in data] == ["book", "book"]
    reconstructed = data[-1]["payload"]
    assert reconstructed["asset_id"] == "target-token"
    assert reconstructed["asks"][0] == {"price": "0.51", "size": "11"}
    assert reconstructed["depth_policy"] == (
        "full_depth_reconstructed_from_price_change"
    )
    assert reconstructed["source_event_type"] == "price_change"


@pytest.mark.asyncio
async def test_compact_sink_can_keep_top_changes_without_reconstructed_depth(
    tmp_path: Path,
) -> None:
    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(
        backing,
        allowed_asset_ids=("target-token",),
        persist_reconstructed_full_depth_frames=False,
        persist_top_of_book_changes=True,
    )
    await sink.emit(
        {
            "schema_version": "clob-market-ws.price_change.v1",
            "source": "clob_market_ws",
            "received_at": "1970-01-01T00:00:01.100000Z",
            "event_at": "1970-01-01T00:00:01.100000Z",
            "event_type": "price_change",
            "kind": "data",
            "connection_id": "connection-1",
            "frame_hash": "a" * 64,
            "payload_hash": "b" * 64,
            "payload": {
                "event_type": "price_change",
                "market": "condition",
                "timestamp": "1100",
                "price_changes": [
                    {
                        "asset_id": "target-token",
                        "price": "0.50",
                        "size": "0",
                        "side": "SELL",
                        "best_bid": "0.49",
                        "best_ask": "0.51",
                    },
                    {
                        "asset_id": "other-token",
                        "price": "0.60",
                        "size": "9",
                        "side": "SELL",
                        "best_bid": "0.59",
                        "best_ask": "0.60",
                    },
                ],
            },
        }
    )
    await sink.close()

    raw_path = next(
        (tmp_path / "capture" / "raw" / "clob_market_ws").glob("*.jsonl")
    )
    stored = json.loads(raw_path.read_text(encoding="utf-8"))["payload"]
    assert stored["event_type"] == "best_bid_ask"
    assert stored["schema_version"] == "clob-market-ws.compact-top-of-book.v1"
    assert stored["payload"] == {
        "event_type": "best_bid_ask",
        "market": "condition",
        "timestamp": "1100",
        "changes": [
            {
                "asset_id": "target-token",
                "best_bid": "0.49",
                "best_ask": "0.51",
            }
        ],
        "source_event_type": "price_change",
    }


@pytest.mark.asyncio
async def test_top_of_book_changes_dedup_repeated_best_bid_ask(
    tmp_path: Path,
) -> None:
    """A price_change that leaves the touch level unmoved must not be re-persisted.

    The upstream stream reports best_bid/best_ask on every message that
    touches an allowed token, including changes deeper in the book. Without
    dedup this turns "top-of-book changes" into "every relevant message".
    """

    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    def price_change_record(*, size: str, best_bid: str, best_ask: str) -> dict:
        return {
            "schema_version": "clob-market-ws.price_change.v1",
            "source": "clob_market_ws",
            "received_at": "1970-01-01T00:00:01.100000Z",
            "event_at": "1970-01-01T00:00:01.100000Z",
            "event_type": "price_change",
            "kind": "data",
            "connection_id": "connection-1",
            "frame_hash": "a" * 64,
            "payload_hash": "b" * 64,
            "payload": {
                "event_type": "price_change",
                "market": "condition",
                "timestamp": "1100",
                "price_changes": [
                    {
                        "asset_id": "target-token",
                        "price": "0.30",
                        "size": size,
                        "side": "SELL",
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                    },
                ],
            },
        }

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(
        backing,
        allowed_asset_ids=("target-token",),
        persist_reconstructed_full_depth_frames=False,
        persist_top_of_book_changes=True,
    )
    # First observation: always persisted.
    await sink.emit(
        price_change_record(size="0", best_bid="0.49", best_ask="0.51")
    )
    # A deeper-book change reporting the same touch level: must be skipped.
    await sink.emit(
        price_change_record(size="5", best_bid="0.49", best_ask="0.51")
    )
    await sink.emit(
        price_change_record(size="7", best_bid="0.49", best_ask="0.51")
    )
    # A genuine touch-level move: must be persisted again.
    await sink.emit(
        price_change_record(size="3", best_bid="0.48", best_ask="0.51")
    )
    await sink.close()

    raw_paths = sorted(
        (tmp_path / "capture" / "raw" / "clob_market_ws").glob("*.jsonl")
    )
    stored = [
        json.loads(line)["payload"]["payload"]
        for path in raw_paths
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(stored) == 2
    assert stored[0]["changes"] == [
        {"asset_id": "target-token", "best_bid": "0.49", "best_ask": "0.51"}
    ]
    assert stored[1]["changes"] == [
        {"asset_id": "target-token", "best_bid": "0.48", "best_ask": "0.51"}
    ]
