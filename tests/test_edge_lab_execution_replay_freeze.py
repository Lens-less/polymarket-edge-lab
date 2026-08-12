"""Production full-window freeze contract for captured-public replay.

These tests deliberately use finalized :class:`CaptureStore` batches.  The
contract must prove a closed filesystem inventory and role semantics without
network access or writes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.edge_lab.data_store import CaptureStore, canonical_json_bytes
from src.edge_lab.execution_replay_freeze import (
    CAPTURE_FREEZE_SCHEMA,
    REQUIRED_ROLE_NAMES,
    verify_production_capture_freeze,
)


OPEN_MS = 1_784_899_000_000
CLOSE_MS = OPEN_MS + 300_000
SLUG = f"btc-updown-5m-{OPEN_MS // 1_000}"
CONDITION_ID = "0x" + "ab" * 32
UP_TOKEN_ID = "1" * 76
DOWN_TOKEN_ID = "2" * 76


def _iso(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _inner(
    *,
    source: str,
    schema_version: str,
    event_type: str,
    received_at_ms: int,
    payload: object,
    kind: str = "data",
    connection_id: str | None = None,
    monotonic_ns: int | None = None,
    **extra: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": schema_version,
        "source": source,
        "kind": kind,
        "event_type": event_type,
        "session_id": "production-contract-test",
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


def _announcement() -> dict[str, object]:
    event = {
        "event_type": "new_market",
        "id": "3081000",
        "slug": SLUG,
        "condition_id": CONDITION_ID,
        "market": CONDITION_ID,
        "assets_ids": [UP_TOKEN_ID, DOWN_TOKEN_ID],
        "clob_token_ids": [UP_TOKEN_ID, DOWN_TOKEN_ID],
        "outcomes": ["Up", "Down"],
        "question": "Bitcoin Up or Down - contract test",
        "description": (
            'This market resolves "Up" when the ending Bitcoin price is at '
            "least the opening price. The resolution source is Chainlink, "
            "specifically the BTC/USD data stream available at "
            "https://data.chain.link/streams/btc-usd."
        ),
        "event_message": {"id": "742901", "slug": SLUG, "ticker": SLUG},
        "timestamp": str(OPEN_MS - 60_000),
        "active": False,
    }
    return _inner(
        source="clob_market_ws",
        schema_version="clob-market-ws.new_market.v1",
        event_type="new_market",
        received_at_ms=OPEN_MS - 60_000,
        payload=event,
        connection_id="clob-public-connection",
        monotonic_ns=10,
    )


def _pong(received_at_ms: int, monotonic_ns: int) -> dict[str, object]:
    return _inner(
        source="clob_market_ws",
        schema_version="edge-lab-recorder.heartbeat-ack.v1",
        event_type="heartbeat_ack",
        received_at_ms=received_at_ms,
        payload=None,
        kind="lifecycle",
        connection_id="clob-public-connection",
        monotonic_ns=monotonic_ns,
        raw_frame="PONG",
    )


def _gamma() -> dict[str, object]:
    market = {
        "id": "3081000",
        "slug": SLUG,
        "conditionId": CONDITION_ID,
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["1", "0"]',
        "clobTokenIds": f'["{UP_TOKEN_ID}", "{DOWN_TOKEN_ID}"]',
        "closed": True,
        "updatedAt": _iso(CLOSE_MS + 4_000),
    }
    return _inner(
        source="gamma_http",
        schema_version="gamma-http.target-snapshot.v1",
        event_type="gamma_market",
        received_at_ms=CLOSE_MS + 5_000,
        payload={
            "schema_version": "edge-lab-public-snapshot.v1",
            "snapshot_kind": "gamma_target",
            "responses": [
                {
                    "resource": "gamma_market",
                    "request_key": "3081000",
                    "raw_json": market,
                    "provenance": {
                        "source": "gamma",
                        "method": "GET",
                        "url": "https://gamma-api.polymarket.com/markets/3081000",
                        "request_params": {},
                        "status_code": 200,
                    },
                }
            ],
        },
        kind="snapshot",
    )


def _chainlink(
    timestamp_ms: int,
    *,
    price: str,
    received_at_ms: int,
) -> dict[str, object]:
    whole, _, fraction = price.partition(".")
    update = {
        "connection_id": "rtds-public-connection",
        "payload": {
            "full_accuracy_value": f"{whole}{fraction.ljust(18, '0')}",
            "symbol": "btc/usd",
            "timestamp": timestamp_ms,
            "value": price,
        },
        "timestamp": timestamp_ms + 500,
        "topic": "crypto_prices_chainlink",
        "type": "update",
    }
    return _inner(
        source="rtds_ws",
        schema_version="rtds.crypto_prices_chainlink.update.v1",
        event_type="crypto_prices_chainlink.update",
        received_at_ms=received_at_ms,
        payload=update,
        connection_id="rtds-public-connection",
        monotonic_ns=received_at_ms,
        server_timestamp=timestamp_ms + 500,
    )


def _role_evidence(
    role: str,
    evidence_schema_version: str,
    received_at_ms: int,
    **payload: object,
) -> dict[str, object]:
    return _inner(
        source="dynamic_short_crypto_service",
        schema_version=(
            "edge-lab-dynamic-short-crypto-service.record.v1"
        ),
        event_type=role,
        received_at_ms=received_at_ms,
        payload={
            "schema_version": evidence_schema_version,
            "evidence_role": role,
            "slug": SLUG,
            "condition_id": CONDITION_ID,
            "opens_at_ms": OPEN_MS,
            "closes_at_ms": CLOSE_MS,
            **payload,
        },
    )


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
    writer.finalize(finalized_at=_iso(CLOSE_MS + 90_000))
    return writer.manifest_path, tuple(record_ids)


@dataclass(frozen=True)
class ProductionFreeze:
    request_path: Path
    freeze_path: Path
    freeze_id: str
    role_ids: dict[str, str]
    manifest_paths: tuple[Path, ...]


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_production_freeze(root: Path) -> ProductionFreeze:
    capture_root = root / "capture"
    clob_manifest, clob_ids = _freeze_batch(
        capture_root,
        source="clob_market_ws",
        batch_id="clob-window",
        payloads=[
            _announcement(),
            _pong(CLOSE_MS - 5_000, 100),
            _pong(CLOSE_MS + 5_000, 200),
        ],
    )
    gamma_manifest, gamma_ids = _freeze_batch(
        capture_root,
        source="gamma_http",
        batch_id="gamma-window",
        payloads=[_gamma()],
    )
    chainlink_manifest, chainlink_ids = _freeze_batch(
        capture_root,
        source="rtds_ws",
        batch_id="rtds-window",
        payloads=[
            _chainlink(
                OPEN_MS,
                price="68000.123456789012345678",
                received_at_ms=OPEN_MS + 1_000,
            ),
            _chainlink(
                CLOSE_MS,
                price="68100.123456789012345678",
                received_at_ms=CLOSE_MS + 1_000,
            ),
        ],
    )
    dependency_ids = [
        clob_ids[0],
        gamma_ids[0],
        chainlink_ids[0],
        chainlink_ids[1],
        clob_ids[1],
        clob_ids[2],
    ]
    settlement = _inner(
        source="dynamic_short_crypto_service",
        schema_version=(
            "edge-lab-dynamic-short-crypto-service.record.v1"
        ),
        event_type="settlement_reconciled",
        received_at_ms=CLOSE_MS + 10_000,
        payload={
            "schema_version": "edge-lab.short-crypto-settlement-commit.v1",
            "transition": "commit_settlement",
            "action": "strict_settlement_committed",
            "status": "resolved",
            "slug": SLUG,
            "condition_id": CONDITION_ID,
            "required_record_ids": dependency_ids,
        },
    )
    close_checkpoint = _role_evidence(
        "full_window_capture_close_checkpoint",
        "edge-lab.execution-replay-close-checkpoint.v1",
        CLOSE_MS + 11_000,
        finalized_through_ms=CLOSE_MS + 10_000,
    )
    l2_closure = _role_evidence(
        "decision_to_trade_l2_closure",
        "edge-lab.execution-replay-l2-closure.v1",
        CLOSE_MS + 12_000,
        decision_count=1,
        public_trade_count=1,
        closed_through_ms=CLOSE_MS,
    )
    submit_latency = _role_evidence(
        "pessimistic_submit_latency",
        "edge-lab.execution-replay-submit-latency.v1",
        CLOSE_MS + 13_000,
        latency_ms=250,
        provenance="public_capture_pessimistic_floor",
    )
    settlement_cost = _role_evidence(
        "settlement_operation_cost",
        "edge-lab.execution-replay-settlement-cost.v1",
        CLOSE_MS + 14_000,
        amount="1",
        currency="USDC",
        provenance="public_capture_pessimistic_upper_bound",
    )
    control_manifest, control_ids = _freeze_batch(
        capture_root,
        source="dynamic_short_crypto_service",
        batch_id="control-window",
        payloads=[
            settlement,
            close_checkpoint,
            l2_closure,
            submit_latency,
            settlement_cost,
        ],
    )
    role_ids = {
        "durable_announcement_record_id": clob_ids[0],
        "durable_gamma_record_id": gamma_ids[0],
        "durable_chainlink_open_record_id": chainlink_ids[0],
        "durable_chainlink_close_record_id": chainlink_ids[1],
        "durable_close_pong_before_record_id": clob_ids[1],
        "durable_close_pong_after_record_id": clob_ids[2],
        "atomic_settlement_commit_record_id": control_ids[0],
        "full_window_capture_close_checkpoint_id": control_ids[1],
        "decision_to_trade_l2_closure_id": control_ids[2],
        "pessimistic_submit_latency_evidence_id": control_ids[3],
        "settlement_operation_cost_evidence_id": control_ids[4],
    }
    manifests = tuple(
        sorted(
            (
                clob_manifest,
                gamma_manifest,
                chainlink_manifest,
                control_manifest,
            )
        )
    )
    entries: list[dict[str, object]] = []
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_bytes())
        raw_path = capture_root / manifest["raw_path"]
        raw_records = [
            json.loads(line)
            for line in raw_path.read_text(encoding="utf-8").splitlines()
        ]
        entries.append(
            {
                "path": manifest_path.relative_to(root).as_posix(),
                "manifest_sha256": _sha256(manifest_path),
                "raw_path": raw_path.relative_to(root).as_posix(),
                "raw_sha256": _sha256(raw_path),
                "raw_bytes": raw_path.stat().st_size,
                "raw_lines": len(raw_records),
                "record_ids": [
                    record["record_id"] for record in raw_records
                ],
            }
        )
    freeze_core = {
        "schema_version": CAPTURE_FREEZE_SCHEMA,
        "status": "finalized",
        "capture_root": "capture",
        "target": {
            "slug": SLUG,
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
    freeze_id = hashlib.sha256(canonical_json_bytes(freeze_core)).hexdigest()
    freeze_path = capture_root / "CAPTURE_FREEZE.json"
    _write_json(freeze_path, {**freeze_core, "freeze_id": freeze_id})
    request_path = root / "promotion-request.json"
    _write_json(
        request_path,
        {
            "schema_version": (
                "edge-lab.execution-replay-promotion-request.v1"
            ),
            "evidence_mode": "captured_public",
            "production_freeze": {
                "path": freeze_path.relative_to(root).as_posix(),
                "sha256": _sha256(freeze_path),
                "freeze_id": freeze_id,
            },
            "source_manifests": [
                {
                    "path": entry["path"],
                    "sha256": entry["manifest_sha256"],
                }
                for entry in entries
            ],
        },
    )
    return ProductionFreeze(
        request_path=request_path,
        freeze_path=freeze_path,
        freeze_id=freeze_id,
        role_ids=role_ids,
        manifest_paths=manifests,
    )


def _content_snapshot(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.relative_to(root).as_posix(): (_sha256(path), path.stat().st_mode)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _rewrite_request(
    fixture: ProductionFreeze,
    mutate: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    request = json.loads(fixture.request_path.read_bytes())
    mutate(request)
    _write_json(fixture.request_path, request)
    return request


def _rewrite_freeze(
    fixture: ProductionFreeze,
    mutate: Callable[[dict[str, Any]], None],
    *,
    sync_request_inventory: bool = False,
) -> dict[str, Any]:
    freeze = json.loads(fixture.freeze_path.read_bytes())
    mutate(freeze)
    core = {key: value for key, value in freeze.items() if key != "freeze_id"}
    freeze["freeze_id"] = hashlib.sha256(
        canonical_json_bytes(core)
    ).hexdigest()
    _write_json(fixture.freeze_path, freeze)

    def update_request(request: dict[str, Any]) -> None:
        request["production_freeze"]["sha256"] = _sha256(
            fixture.freeze_path
        )
        request["production_freeze"]["freeze_id"] = freeze["freeze_id"]
        if sync_request_inventory:
            request["source_manifests"] = [
                {
                    "path": entry["path"],
                    "sha256": entry["manifest_sha256"],
                }
                for entry in freeze["source_manifests"]
            ]

    _rewrite_request(fixture, update_request)
    return freeze


def test_verifies_content_addressed_closed_production_freeze_read_only(
    tmp_path: Path,
) -> None:
    fixture = _build_production_freeze(tmp_path)
    before = _content_snapshot(tmp_path)

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.verified is True
    assert result.status == "verified"
    assert result.freeze_id == fixture.freeze_id
    assert result.freeze_path == fixture.freeze_path
    assert result.target is not None
    assert result.target.slug == SLUG
    assert result.target.condition_id == CONDITION_ID
    assert result.target.opens_at_ms == OPEN_MS
    assert result.target.closes_at_ms == CLOSE_MS
    assert result.source_manifest_paths == tuple(
        path.relative_to(tmp_path).as_posix()
        for path in fixture.manifest_paths
    )
    assert dict(result.required_role_ids) == fixture.role_ids
    assert set(result.required_role_ids) == set(REQUIRED_ROLE_NAMES)
    assert len(result.record_ids) == 11
    assert result.reason_codes == ()
    assert _content_snapshot(tmp_path) == before


def test_rejects_request_that_omits_one_frozen_manifest(
    tmp_path: Path,
) -> None:
    fixture = _build_production_freeze(tmp_path)
    _rewrite_request(
        fixture,
        lambda request: request["source_manifests"].pop(),
    )

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.verified is False
    assert result.reason_codes == ("request_freeze_inventory_mismatch",)


def test_rejects_freeze_that_omits_finalized_filesystem_sibling(
    tmp_path: Path,
) -> None:
    fixture = _build_production_freeze(tmp_path)
    _rewrite_freeze(
        fixture,
        lambda freeze: freeze["source_manifests"].pop(),
        sync_request_inventory=True,
    )

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("capture_inventory_mismatch",)


def test_rejects_extra_finalized_filesystem_sibling(tmp_path: Path) -> None:
    fixture = _build_production_freeze(tmp_path)
    _freeze_batch(
        tmp_path / "capture",
        source="unlisted_public",
        batch_id="extra-finalized",
        payloads=[
            _inner(
                source="unlisted_public",
                schema_version="unlisted-public.record.v1",
                event_type="observation",
                received_at_ms=OPEN_MS,
                payload={"public": True},
            )
        ],
    )

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("capture_inventory_mismatch",)


def test_rejects_any_partial_in_capture_root(tmp_path: Path) -> None:
    fixture = _build_production_freeze(tmp_path)
    partial = (
        tmp_path
        / "capture"
        / "raw"
        / "clob_market_ws"
        / "orphan.jsonl.partial"
    )
    partial.write_text('{"unfinished":true}\n', encoding="utf-8")

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("capture_partial_present",)


def test_rejects_symlink_hidden_as_extra_manifest(tmp_path: Path) -> None:
    fixture = _build_production_freeze(tmp_path)
    symlink = (
        tmp_path
        / "capture"
        / "raw"
        / "clob_market_ws"
        / "linked.manifest.json"
    )
    symlink.symlink_to(fixture.manifest_paths[0])

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("capture_symlink_forbidden",)


def test_rejects_manifest_bytes_changed_after_freeze(tmp_path: Path) -> None:
    fixture = _build_production_freeze(tmp_path)
    manifest = fixture.manifest_paths[0]
    manifest.chmod(0o644)
    manifest.write_bytes(manifest.read_bytes() + b" ")

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("source_manifest_hash_mismatch",)


def test_rejects_raw_record_inventory_changed_in_freeze(
    tmp_path: Path,
) -> None:
    fixture = _build_production_freeze(tmp_path)

    def add_unobserved_record_id(freeze: dict[str, Any]) -> None:
        freeze["source_manifests"][0]["record_ids"].append("f" * 64)

    _rewrite_freeze(fixture, add_unobserved_record_id)

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("source_record_inventory_mismatch",)


def test_rejects_each_raw_hash_size_and_line_binding_drift(
    tmp_path: Path,
) -> None:
    cases: tuple[tuple[str, object], ...] = (
        ("raw_sha256", "0" * 64),
        ("raw_bytes", -1),
        ("raw_lines", 999),
    )
    for field, replacement in cases:
        case_root = tmp_path / field
        case_root.mkdir()
        fixture = _build_production_freeze(case_root)

        def mutate(
            freeze: dict[str, Any],
            *,
            field_name: str = field,
            value: object = replacement,
        ) -> None:
            freeze["source_manifests"][0][field_name] = value

        _rewrite_freeze(fixture, mutate)

        result = verify_production_capture_freeze(fixture.request_path)

        expected = (
            "capture_freeze_inventory_invalid"
            if field == "raw_bytes"
            else "source_raw_integrity_mismatch"
        )
        assert result.reason_codes == (expected,)


def test_rejects_raw_bytes_changed_after_manifest_and_freeze(
    tmp_path: Path,
) -> None:
    fixture = _build_production_freeze(tmp_path)
    manifest = json.loads(fixture.manifest_paths[0].read_bytes())
    raw_path = tmp_path / "capture" / manifest["raw_path"]
    raw_path.chmod(0o644)
    raw_path.write_bytes(raw_path.read_bytes() + b"\n")

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("source_raw_integrity_mismatch",)


def test_rejects_non_content_addressed_freeze_id(tmp_path: Path) -> None:
    fixture = _build_production_freeze(tmp_path)
    freeze = json.loads(fixture.freeze_path.read_bytes())
    freeze["freeze_id"] = "0" * 64
    _write_json(fixture.freeze_path, freeze)

    def update_binding(request: dict[str, Any]) -> None:
        request["production_freeze"]["sha256"] = _sha256(
            fixture.freeze_path
        )
        request["production_freeze"]["freeze_id"] = "0" * 64

    _rewrite_request(fixture, update_binding)

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("capture_freeze_id_mismatch",)


def test_rejects_request_freeze_id_binding_mismatch(tmp_path: Path) -> None:
    fixture = _build_production_freeze(tmp_path)
    _rewrite_request(
        fixture,
        lambda request: request["production_freeze"].__setitem__(
            "freeze_id", "0" * 64
        ),
    )

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == (
        "capture_freeze_id_binding_mismatch",
    )


def test_rejects_unfinalized_freeze_status(tmp_path: Path) -> None:
    fixture = _build_production_freeze(tmp_path)
    _rewrite_freeze(
        fixture,
        lambda freeze: freeze.__setitem__("status", "capturing"),
    )

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("capture_freeze_not_finalized",)


def test_rejects_missing_required_production_role(tmp_path: Path) -> None:
    fixture = _build_production_freeze(tmp_path)

    def remove_role(freeze: dict[str, Any]) -> None:
        del freeze["required_role_ids"][
            "settlement_operation_cost_evidence_id"
        ]

    _rewrite_freeze(fixture, remove_role)

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("required_role_ids_invalid",)


def test_rejects_role_ids_bound_to_wrong_record_semantics(
    tmp_path: Path,
) -> None:
    fixture = _build_production_freeze(tmp_path)

    def swap_roles(freeze: dict[str, Any]) -> None:
        roles = freeze["required_role_ids"]
        gamma = roles["durable_gamma_record_id"]
        checkpoint = roles["full_window_capture_close_checkpoint_id"]
        roles["durable_gamma_record_id"] = checkpoint
        roles["full_window_capture_close_checkpoint_id"] = gamma

    _rewrite_freeze(fixture, swap_roles)

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("required_role_semantics_invalid",)


def test_rejects_target_slug_window_and_condition_conflicts(
    tmp_path: Path,
) -> None:
    cases: tuple[
        tuple[str, Callable[[dict[str, Any]], None], str], ...
    ] = (
        (
            "slug",
            lambda freeze: freeze["target"].__setitem__(
                "slug", "bitcoin-updown-5m-1784899000"
            ),
            "capture_target_invalid",
        ),
        (
            "window",
            lambda freeze: freeze["target"].__setitem__(
                "closes_at_ms", CLOSE_MS + 1
            ),
            "capture_target_window_mismatch",
        ),
        (
            "condition",
            lambda freeze: freeze["target"].__setitem__(
                "condition_id", "0x" + "cd" * 32
            ),
            "required_role_target_mismatch",
        ),
    )
    for name, mutate, reason in cases:
        case_root = tmp_path / name
        case_root.mkdir()
        fixture = _build_production_freeze(case_root)
        _rewrite_freeze(fixture, mutate)

        result = verify_production_capture_freeze(fixture.request_path)

        assert result.reason_codes == (reason,)


def test_rejects_nonzero_order_or_authenticated_endpoint_claim(
    tmp_path: Path,
) -> None:
    fixture = _build_production_freeze(tmp_path)
    _rewrite_freeze(
        fixture,
        lambda freeze: freeze["safety"].__setitem__(
            "orders_submitted", 1
        ),
    )

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("capture_safety_invalid",)


def test_rejects_credential_material_in_request(tmp_path: Path) -> None:
    fixture = _build_production_freeze(tmp_path)
    _rewrite_request(
        fixture,
        lambda request: request.__setitem__(
            "authorization", "Bearer abcdefghijklmnop"
        ),
    )

    result = verify_production_capture_freeze(fixture.request_path)

    assert result.reason_codes == ("credential_material_forbidden",)
