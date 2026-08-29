"""Strict, read-only completeness audit for Phase 2 short-crypto targets.

The auditor selects the chronologically first mature targets from cumulative
registry revisions and reconstructs eleven lifecycle stages exclusively from
finalized manifest/raw pairs.  Mutable partial files are counted and ignored;
they can never improve a target's result.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

from .data_store import canonical_json_bytes
from .execution_replay_freeze_builder import (
    _source_pair,
    _stable_regular_bytes,
)
from .lifecycle_cohort_contract import (
    REMEDIATION_COHORT_SCHEMA,
    REMEDIATION_EVENT_TYPE,
    ROOT_COHORT_SCHEMA,
    ROOT_EVENT_TYPE,
    LifecycleCohortContractError,
    validate_cohort_payload,
)
from .short_crypto_registry import (
    merge_short_crypto_registry_snapshots,
    validate_short_crypto_registry_snapshot,
)


REPORT_SCHEMA = "edge-lab.phase2-lifecycle-completeness.v3"
SERVICE_SCHEMA = "edge-lab-dynamic-short-crypto-service.record.v1"
COHORT_SCHEMA = ROOT_COHORT_SCHEMA
RAW_SCHEMA = "edge-lab-recorder.raw.v1"
DEFAULT_SAMPLE_SIZE = 20
DEFAULT_THRESHOLD = Decimal("0.8")
DEFAULT_SETTLEMENT_TIMEOUT_MS = 3_600_000
DEFAULT_CLOSE_LIVENESS_TOLERANCE_MS = 30_000
REGISTRY_SNAPSHOT_SCHEMA = "edge-lab.short-crypto-registry.v1"
REGISTRY_REVISION_V2_SCHEMA = (
    "edge-lab-short-crypto-registry-revision.v2"
)
COMPACTED_REGISTRY_SCHEMA = (
    "edge-lab.phase2-lifecycle-registry-compacted.v1"
)
REGISTRY_SNAPSHOT_FIELDS = (
    "schema_version",
    "inputs",
    "targets",
    "rejections",
    "completeness",
    "snapshot_sha256",
)
REGISTRY_REVISION_V2_FIELDS = frozenset(
    {
        "schema_version",
        "delta_snapshot",
        "cumulative_snapshot_sha256",
        "cumulative_input_file_count",
        "cumulative_target_count",
        "scheduler_now_ms",
    }
)

STAGES = (
    "announcement",
    "gamma_identity",
    "preopen_subscription_decision_receipt",
    "rest_resnapshot_watermark",
    "clob_increment_continuity",
    "chainlink_ptb",
    "chainlink_close_boundary",
    "same_connection_liveness_bracket",
    "gamma_winner",
    "settlement_reconciliation",
    "finalization_manifest",
)


class LifecycleAuditError(ValueError):
    """Finalized inputs are malformed, conflicting, or unsafe to audit."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class _LocatedRecord:
    record: Mapping[str, Any]
    manifest_path: Path
    group_id: str | None
    manifest_finalized_at_ms: int


@dataclass(frozen=True, slots=True)
class _ServiceEvent:
    event_type: str
    payload: Mapping[str, Any]
    record_id: str
    manifest_path: Path
    received_at_ms: int
    kind: str = "command"
    manifest_finalized_at_ms: int = 0
    compressed_payload: bytes | None = None
    registry_v1_validated: bool = False


@dataclass(frozen=True, slots=True)
class _Decision:
    decision_id: str
    group_id: str
    emitted_at_ms: int
    record_id: str


@dataclass(frozen=True, slots=True)
class _CohortCommitment:
    record_id: str
    manifest_path: Path
    committed_at_ms: int
    scheduler_now_ms: int
    eligibility_start_ms: int
    role: str = "root"
    predecessor_commitment_record_id: str | None = None
    remediation_reason_code: str | None = None
    remediation_plan_id: str | None = None


class _RecordInventory:
    """Exact all-record index with only audit-relevant rows in Python."""

    _FLUSH_RECORD_COUNT = 100_000

    def __init__(self) -> None:
        self._connection = sqlite3.connect("")
        self._connection.execute("PRAGMA journal_mode = OFF")
        self._connection.execute("PRAGMA synchronous = OFF")
        self._connection.execute("PRAGMA temp_store = FILE")
        self._connection.execute("PRAGMA cache_size = -2048")
        self._connection.execute("PRAGMA locking_mode = EXCLUSIVE")
        self._connection.execute(
            "CREATE TABLE record_ids ("
            "record_id BLOB PRIMARY KEY"
            ") WITHOUT ROWID"
        )
        self._pending: list[tuple[str, bytes]] = []
        self._pending_encoded: set[bytes] = set()
        self._hydrated: dict[str, _LocatedRecord] = {}
        self._indexed_record_count = 0
        self._closed = False

    @staticmethod
    def _encoded(record_id: str) -> bytes:
        if len(record_id) == 64:
            try:
                return bytes.fromhex(record_id)
            except ValueError:
                pass
        return b"\xff" + record_id.encode("utf-8")

    @property
    def indexed_record_count(self) -> int:
        return self._indexed_record_count

    @property
    def hydrated_record_count(self) -> int:
        return len(self._hydrated)

    def add(self, record_id: str) -> None:
        encoded = self._encoded(record_id)
        if encoded in self._pending_encoded:
            _fail(
                "duplicate_record_id",
                f"{record_id} occurs more than once",
            )
        self._pending.append((record_id, encoded))
        self._pending_encoded.add(encoded)
        self._indexed_record_count += 1
        if len(self._pending) >= self._FLUSH_RECORD_COUNT:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        try:
            self._connection.executemany(
                "INSERT INTO record_ids(record_id) VALUES (?)",
                ((encoded,) for _, encoded in self._pending),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as exc:
            self._connection.rollback()
            duplicate = next(
                (
                    record_id
                    for record_id, encoded in self._pending
                    if self._connection.execute(
                        "SELECT 1 FROM record_ids WHERE record_id = ?",
                        (encoded,),
                    ).fetchone()
                    is not None
                ),
                "unknown",
            )
            raise LifecycleAuditError(
                "duplicate_record_id",
                f"{duplicate} occurs more than once",
            ) from exc
        finally:
            self._pending.clear()
            self._pending_encoded.clear()

    def hydrate(
        self,
        record_id: str,
        located: _LocatedRecord,
    ) -> None:
        if record_id not in self:
            _fail(
                "source_changed_during_audit",
                f"{record_id} was not present in the verified inventory",
            )
        self._hydrated[record_id] = located

    def __contains__(self, record_id: object) -> bool:
        if not isinstance(record_id, str):
            return False
        encoded = self._encoded(record_id)
        if encoded in self._pending_encoded:
            return True
        return (
            self._connection.execute(
                "SELECT 1 FROM record_ids WHERE record_id = ?",
                (encoded,),
            ).fetchone()
            is not None
        )

    def __getitem__(self, record_id: str) -> _LocatedRecord | None:
        located = self._hydrated.get(record_id)
        if located is not None:
            return located
        if record_id in self:
            return None
        raise KeyError(record_id)

    def get(
        self,
        record_id: str,
        default: _LocatedRecord | None = None,
    ) -> _LocatedRecord | None:
        located = self._hydrated.get(record_id)
        if located is not None:
            return located
        return None if record_id in self else default

    def values(self) -> Iterable[_LocatedRecord]:
        return self._hydrated.values()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._connection.close()
            self._closed = True

    def __del__(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None and not getattr(self, "_closed", True):
            self.close()


class _RecordCutoffView:
    """Expose only rows durably finalized by one immutable cutoff."""

    def __init__(
        self,
        inventory: _RecordInventory,
        *,
        cutoff_ms: int,
    ) -> None:
        self._inventory = inventory
        self._cutoff_ms = cutoff_ms

    def _eligible(
        self,
        located: _LocatedRecord | None,
    ) -> _LocatedRecord | None:
        if located is None:
            return None
        if (
            located.manifest_finalized_at_ms > self._cutoff_ms
            or _record_received_at_ms(located) > self._cutoff_ms
        ):
            return None
        return located

    def __contains__(self, record_id: object) -> bool:
        if not isinstance(record_id, str):
            return False
        return self.get(record_id) is not None

    def __getitem__(self, record_id: str) -> _LocatedRecord | None:
        return self._eligible(self._inventory[record_id])

    def get(
        self,
        record_id: str,
        default: _LocatedRecord | None = None,
    ) -> _LocatedRecord | None:
        try:
            located = self._inventory[record_id]
        except KeyError:
            return default
        return self._eligible(located)

    def values(self) -> Iterable[_LocatedRecord]:
        return (
            located
            for located in self._inventory.values()
            if self._eligible(located) is not None
        )


def _fail(code: str, detail: str) -> None:
    raise LifecycleAuditError(code, detail)


def _epoch_ms(value: Any, *, label: str) -> int:
    if not isinstance(value, str) or not value:
        _fail("timestamp_invalid", f"{label} is not a non-empty string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LifecycleAuditError(
            "timestamp_invalid",
            f"{label} is not ISO-8601",
        ) from exc
    if parsed.tzinfo is None:
        _fail("timestamp_invalid", f"{label} is timezone-naive")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000)


def _manifest_finalized_at_ms(
    manifest_bytes: bytes,
    *,
    manifest_path: Path,
) -> int:
    try:
        manifest: Any = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleAuditError(
            "source_manifest_invalid",
            f"manifest is not valid JSON: {manifest_path}",
        ) from exc
    if not isinstance(manifest, Mapping):
        _fail(
            "source_manifest_invalid",
            f"manifest is not an object: {manifest_path}",
        )
    return _epoch_ms(
        manifest.get("finalized_at"),
        label=f"{manifest_path}.finalized_at",
    )


def _manifest_header_before_raw(
    manifest_path: Path,
) -> tuple[bytes, int]:
    """Validate a content-addressing manifest without opening its raw."""

    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or not manifest_path.name.endswith(".manifest.json")
        or manifest_path.parent.parent.name != "raw"
    ):
        _fail(
            "source_manifest_path_invalid",
            f"not a finalized raw/<source> manifest: {manifest_path}",
        )
    manifest_bytes = _stable_regular_bytes(
        manifest_path,
        label=f"manifest {manifest_path}",
    )
    try:
        manifest: Any = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleAuditError(
            "source_manifest_invalid",
            f"manifest is not valid JSON: {manifest_path}",
        ) from exc
    source = manifest_path.parent.name
    batch_id = manifest_path.name.removesuffix(".manifest.json")
    raw_path = manifest_path.with_name(f"{batch_id}.jsonl")
    checksum = (
        manifest.get("checksum")
        if isinstance(manifest, Mapping)
        else None
    )
    checksum_value = (
        checksum.get("value")
        if isinstance(checksum, Mapping)
        else None
    )
    checksum_bytes = (
        checksum.get("bytes")
        if isinstance(checksum, Mapping)
        else None
    )
    checksum_lines = (
        checksum.get("lines")
        if isinstance(checksum, Mapping)
        else None
    )
    record_count = (
        manifest.get("record_count")
        if isinstance(manifest, Mapping)
        else None
    )
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("manifest_version") != "capture-manifest.v1"
        or manifest.get("schema_version") != RAW_SCHEMA
        or manifest.get("source") != source
        or manifest.get("batch_id") != batch_id
        or PurePosixPath(
            str(manifest.get("raw_path", ""))
        ).as_posix()
        != f"raw/{source}/{batch_id}.jsonl"
        or not isinstance(checksum, Mapping)
        or checksum.get("algorithm") != "sha256"
        or not isinstance(checksum_value, str)
        or len(checksum_value) != 64
        or any(
            character not in "0123456789abcdef"
            for character in checksum_value
        )
        or isinstance(checksum_bytes, bool)
        or not isinstance(checksum_bytes, int)
        or checksum_bytes < 0
        or isinstance(checksum_lines, bool)
        or not isinstance(checksum_lines, int)
        or checksum_lines < 0
        or isinstance(record_count, bool)
        or not isinstance(record_count, int)
        or record_count != checksum_lines
        or raw_path.is_symlink()
        or not raw_path.is_file()
    ):
        _fail(
            "source_manifest_identity_invalid",
            f"manifest identity/content address drift: {manifest_path}",
        )
    return (
        manifest_bytes,
        _manifest_finalized_at_ms(
            manifest_bytes,
            manifest_path=manifest_path,
        ),
    )


def _int(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail("integer_invalid", f"{label} is not an integer >= {minimum}")
    return value


def _root(value: str | Path, *, label: str) -> Path:
    root = Path(value).expanduser().resolve()
    if root.is_symlink() or not root.is_dir():
        _fail("capture_root_invalid", f"{label} is not a regular directory: {root}")
    return root


def _dynamic_run_set_complete(dynamic_roots: tuple[Path, ...]) -> bool:
    parents = {root.parent for root in dynamic_roots}
    if len(parents) != 1:
        return False
    runs_root = next(iter(parents))
    if runs_root.name != "runs":
        return False
    sibling_roots: set[Path] = set()
    for path in runs_root.iterdir():
        if path.is_symlink():
            _fail(
                "dynamic_runs_root_unsafe",
                f"run root contains a symlink: {path}",
            )
        if path.is_dir():
            sibling_roots.add(path.resolve())
    supplied = set(dynamic_roots)
    if supplied != sibling_roots:
        missing = sorted(str(path) for path in sibling_roots - supplied)
        unexpected = sorted(str(path) for path in supplied - sibling_roots)
        _fail(
            "dynamic_roots_incomplete",
            f"missing={missing}, unexpected={unexpected}",
        )
    return True


def _finalized_manifests(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in root.rglob("*.manifest.json")
            if path.is_file()
            and not path.is_symlink()
            and path.parent.parent.name == "raw"
        )
    )


def _group_id_for_manifest(
    manifest_path: Path,
    *,
    dynamic_root: Path,
) -> str | None:
    try:
        relative = manifest_path.relative_to(dynamic_root)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) >= 3 and parts[0] == "groups":
        group_id = parts[1]
        if not group_id:
            _fail("group_path_invalid", f"empty group path: {manifest_path}")
        return group_id
    return None


def _compact_located_record(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    source = record.get("source")
    compact: dict[str, Any] = {
        "source": source,
        "received_at": record.get("received_at"),
        "record_id": record.get("record_id"),
    }
    inner = record.get("payload")
    if not isinstance(inner, Mapping):
        return compact
    event_type = inner.get("event_type")
    compact_inner: dict[str, Any] = {}
    if isinstance(event_type, str):
        compact_inner["event_type"] = event_type
    event_payload = inner.get("payload")
    if (
        event_type == "new_market"
        and isinstance(event_payload, Mapping)
    ):
        compact_inner["schema_version"] = inner.get("schema_version")
        compact_inner["payload"] = {
            key: event_payload.get(key)
            for key in ("slug", "condition_id", "clob_token_ids")
        }
    elif event_type == "heartbeat_ack":
        compact_inner.update(
            {
                key: inner.get(key)
                for key in (
                    "schema_version",
                    "session_id",
                    "connection_id",
                    "raw_frame",
                )
            }
        )
    compact["payload"] = compact_inner
    return compact


def _record_needed_for_lifecycle(
    record: Mapping[str, Any],
    *,
    group_id: str | None,
) -> bool:
    if group_id is not None:
        return False
    source = record.get("source")
    inner = record.get("payload")
    event_type = (
        inner.get("event_type")
        if isinstance(inner, Mapping)
        else None
    )
    if source in {"gamma_http", "rtds_ws"}:
        return True
    if source == "clob_market_ws" and event_type == "new_market":
        return True
    return False


def _record_needed_for_selected_group(
    record: Mapping[str, Any],
) -> bool:
    source = record.get("source")
    if source == "clob_http":
        return True
    inner = record.get("payload")
    event_type = (
        inner.get("event_type")
        if isinstance(inner, Mapping)
        else None
    )
    return source == "clob_market_ws" and event_type in {
        "resync_complete",
        "book",
        "price_change",
        "tick_size_change",
        "disconnected",
        "heartbeat_ack",
    }


def _validated_v1_registry_payload(
    payload: Mapping[str, Any],
    *,
    record_id: str,
) -> dict[str, Any]:
    snapshot_value = {
        field: payload.get(field)
        for field in REGISTRY_SNAPSHOT_FIELDS
    }
    try:
        return validate_short_crypto_registry_snapshot(snapshot_value)
    except ValueError as exc:
        raise LifecycleAuditError(
            "registry_revision_invalid",
            f"{record_id} has an invalid v1 snapshot",
        ) from exc


def _compact_registry_event(event: _ServiceEvent) -> _ServiceEvent:
    raw_targets = event.payload.get("targets")
    if not isinstance(raw_targets, list):
        _fail(
            "registry_revision_invalid",
            f"{event.record_id} targets is not a list",
        )
    targets = []
    for raw_target in raw_targets:
        if not isinstance(raw_target, Mapping):
            _fail(
                "registry_target_invalid",
                f"{event.record_id} contains a non-object target",
            )
        targets.append(_target_identity(raw_target))
    return _ServiceEvent(
        event_type=event.event_type,
        payload={
            "schema_version": COMPACTED_REGISTRY_SCHEMA,
            "targets": targets,
        },
        record_id=event.record_id,
        manifest_path=event.manifest_path,
        received_at_ms=event.received_at_ms,
        kind=event.kind,
        manifest_finalized_at_ms=event.manifest_finalized_at_ms,
    )


def _service_event_payload(event: _ServiceEvent) -> Mapping[str, Any]:
    if event.compressed_payload is None:
        return event.payload
    try:
        value: Any = json.loads(zlib.decompress(event.compressed_payload))
    except (zlib.error, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleAuditError(
            "registry_revision_invalid",
            f"{event.record_id} compressed payload is invalid",
        ) from exc
    if not isinstance(value, Mapping):
        _fail(
            "registry_revision_invalid",
            f"{event.record_id} compressed payload is not an object",
        )
    return value


def _load_inventory(
    roots: tuple[tuple[Path, bool], ...],
    *,
    manifest_entries_out: (
        list[tuple[Path, str | None, int]] | None
    ) = None,
    as_of_ms: int | None = None,
) -> tuple[
    _RecordInventory,
    tuple[_ServiceEvent, ...],
    int,
    int,
]:
    by_id = _RecordInventory()
    service_events: list[_ServiceEvent] = []
    compressed_registry_payloads: dict[
        bytes, tuple[bytes, bytes]
    ] = {}
    manifest_count = 0
    record_count = 0
    try:
        for root, is_dynamic in roots:
            for manifest_path in _finalized_manifests(root):
                (
                    manifest_bytes,
                    finalized_at_ms,
                ) = _manifest_header_before_raw(
                    manifest_path
                )
                if (
                    as_of_ms is not None
                    and finalized_at_ms > as_of_ms
                ):
                    continue
                pair = _source_pair(manifest_path)
                if pair.manifest_bytes != manifest_bytes:
                    _fail(
                        "source_changed_during_audit",
                        (
                            "manifest changed after cutoff validation: "
                            f"{manifest_path}"
                        ),
                    )
                manifest_count += 1
                group_id = (
                    _group_id_for_manifest(
                        manifest_path,
                        dynamic_root=root,
                    )
                    if is_dynamic
                    else None
                )
                if manifest_entries_out is not None:
                    manifest_entries_out.append(
                        (manifest_path, group_id, finalized_at_ms)
                    )
                for line_number, line in enumerate(
                    pair.raw_bytes.splitlines(),
                    start=1,
                ):
                    try:
                        record: Any = json.loads(line)
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        raise LifecycleAuditError(
                            "source_record_invalid",
                            (
                                f"invalid JSON at "
                                f"{manifest_path}:{line_number}"
                            ),
                        ) from exc
                    if not isinstance(record, Mapping):
                        _fail(
                            "source_record_invalid",
                            (
                                f"non-object record at "
                                f"{manifest_path}:{line_number}"
                            ),
                        )
                    record_id = record.get("record_id")
                    if not isinstance(record_id, str):
                        _fail(
                            "source_record_invalid",
                            (
                                f"missing record ID at "
                                f"{manifest_path}:{line_number}"
                            ),
                        )
                    received_at_ms = _epoch_ms(
                        record.get("received_at"),
                        label=f"{record_id}.received_at",
                    )
                    if (
                        as_of_ms is not None
                        and received_at_ms > as_of_ms
                    ):
                        continue
                    by_id.add(record_id)
                    needed_for_lifecycle = (
                        _record_needed_for_lifecycle(
                            record,
                            group_id=group_id,
                        )
                    )
                    if needed_for_lifecycle:
                        by_id.hydrate(
                            record_id,
                            _LocatedRecord(
                                record=_compact_located_record(record),
                                manifest_path=manifest_path,
                                group_id=group_id,
                                manifest_finalized_at_ms=(
                                    finalized_at_ms
                                ),
                            ),
                        )
                    record_count += 1
                    inner = record.get("payload")
                    if not isinstance(inner, Mapping):
                        continue
                    if (
                        inner.get("source")
                        != "dynamic_short_crypto_service"
                    ):
                        continue
                    if (
                        inner.get("schema_version") != SERVICE_SCHEMA
                        or not isinstance(
                            inner.get("event_type"), str
                        )
                        or not isinstance(
                            inner.get("payload"), Mapping
                        )
                    ):
                        _fail(
                            "service_record_invalid",
                            (
                                f"service schema drift at "
                                f"{manifest_path}:{line_number}"
                            ),
                        )
                    event_type = str(inner["event_type"])
                    event_payload = dict(inner["payload"])
                    compressed_payload: bytes | None = None
                    registry_v1_validated = False
                    if event_type == "registry_revision":
                        schema_version = event_payload.get(
                            "schema_version"
                        )
                        cache_identity_payload = (
                            {
                                field: event_payload.get(field)
                                for field in REGISTRY_SNAPSHOT_FIELDS
                            }
                            if schema_version == REGISTRY_SNAPSHOT_SCHEMA
                            else event_payload
                        )
                        raw_canonical_payload = canonical_json_bytes(
                            cache_identity_payload
                        )
                        payload_digest = hashlib.sha256(
                            raw_canonical_payload
                        ).digest()
                        cached_payload = compressed_registry_payloads.get(
                            payload_digest
                        )
                        if cached_payload is not None:
                            if cached_payload[0] != raw_canonical_payload:
                                _fail(
                                    "registry_payload_digest_collision",
                                    (
                                        f"{record_id} canonical registry "
                                        "payload collided with different "
                                        "finalized bytes"
                                    ),
                                )
                            compressed_payload = cached_payload[1]
                            registry_v1_validated = (
                                schema_version == REGISTRY_SNAPSHOT_SCHEMA
                            )
                            event_payload = {}
                        else:
                            if schema_version == REGISTRY_SNAPSHOT_SCHEMA:
                                event_payload = (
                                    _validated_v1_registry_payload(
                                        event_payload,
                                        record_id=record_id,
                                    )
                                )
                                registry_v1_validated = True
                            canonical_payload = canonical_json_bytes(
                                event_payload
                            )
                            compressed_payload = zlib.compress(
                                canonical_payload,
                                level=1,
                            )
                            compressed_registry_payloads[payload_digest] = (
                                raw_canonical_payload,
                                compressed_payload,
                            )
                            event_payload = {}
                    service_events.append(
                        _ServiceEvent(
                            event_type=event_type,
                            payload=event_payload,
                            record_id=record_id,
                            manifest_path=manifest_path,
                            received_at_ms=received_at_ms,
                            kind=inner.get("kind"),
                            manifest_finalized_at_ms=finalized_at_ms,
                            compressed_payload=compressed_payload,
                            registry_v1_validated=(
                                registry_v1_validated
                            ),
                        )
                    )
        by_id.flush()
    except BaseException:
        by_id.close()
        raise
    return by_id, tuple(service_events), manifest_count, record_count


def _load_selected_group_records(
    manifest_entries: Iterable[tuple[Path, str | None, int]],
    selected_group_ids: set[str],
    by_id: _RecordInventory,
    *,
    as_of_ms: int | None = None,
) -> dict[str, tuple[_LocatedRecord, ...]]:
    records_by_group: dict[str, list[_LocatedRecord]] = {
        group_id: [] for group_id in selected_group_ids
    }
    for manifest_path, group_id, finalized_at_ms in manifest_entries:
        if group_id not in selected_group_ids:
            continue
        if as_of_ms is not None and finalized_at_ms > as_of_ms:
            continue
        pair = _source_pair(manifest_path)
        for line_number, line in enumerate(
            pair.raw_bytes.splitlines(),
            start=1,
        ):
            try:
                record: Any = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise LifecycleAuditError(
                    "source_record_invalid",
                    f"invalid JSON at {manifest_path}:{line_number}",
                ) from exc
            if not isinstance(record, Mapping):
                _fail(
                    "source_record_invalid",
                    f"non-object record at {manifest_path}:{line_number}",
                )
            record_id = record.get("record_id")
            if not isinstance(record_id, str) or record_id not in by_id:
                _fail(
                    "source_changed_during_audit",
                    (
                        f"selected group record changed after inventory at "
                        f"{manifest_path}:{line_number}"
                    ),
                )
            if not _record_needed_for_selected_group(record):
                continue
            if (
                as_of_ms is not None
                and _epoch_ms(
                    record.get("received_at"),
                    label=f"{record_id}.received_at",
                )
                > as_of_ms
            ):
                continue
            if by_id[record_id] is not None:
                _fail(
                    "duplicate_record_id",
                    f"{record_id} was hydrated more than once",
                )
            located = _LocatedRecord(
                record=_compact_located_record(record),
                manifest_path=manifest_path,
                group_id=group_id,
                manifest_finalized_at_ms=finalized_at_ms,
            )
            by_id.hydrate(record_id, located)
            records_by_group[str(group_id)].append(located)
    return {
        group_id: tuple(records)
        for group_id, records in records_by_group.items()
    }


_TARGET_IDENTITY_FIELDS = (
    "slug",
    "market_id",
    "condition_id",
    "up_token_id",
    "down_token_id",
    "source_symbol",
    "source_topic",
    "horizon",
    "opens_at_ms",
    "closes_at_ms",
    "rule_hash",
)


def _target_identity(target: Mapping[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for field in _TARGET_IDENTITY_FIELDS:
        value = target.get(field)
        if field in {"opens_at_ms", "closes_at_ms"}:
            value = _int(value, label=f"target.{field}")
        elif not isinstance(value, str) or not value:
            _fail("registry_target_invalid", f"target.{field} is missing")
        identity[field] = value
    announcement_record_id = target.get("announcement_record_id")
    announced_at = target.get("announced_at")
    if (
        not isinstance(announcement_record_id, str)
        or len(announcement_record_id) != 64
        or not isinstance(announced_at, str)
    ):
        _fail(
            "registry_target_invalid",
            f"{identity['slug']} announcement provenance is invalid",
        )
    identity["announcement_record_id"] = announcement_record_id
    identity["announced_at"] = announced_at
    return identity


def _registry_targets(
    events: tuple[_ServiceEvent, ...],
) -> tuple[dict[str, Any], ...]:
    targets: dict[str, dict[str, Any]] = {}
    registry_snapshot: dict[str, Any] | None = None
    registry_events = sorted(
        (
            event
            for event in events
            if event.event_type == "registry_revision"
        ),
        key=lambda event: (
            event.received_at_ms,
            str(event.manifest_path),
            event.record_id,
        ),
    )
    decoded_registry_payloads: dict[bytes, Mapping[str, Any]] = {}
    for event in registry_events:
        if event.compressed_payload is None:
            payload = _service_event_payload(event)
        else:
            payload = decoded_registry_payloads.get(
                event.compressed_payload
            )
            if payload is None:
                payload = _service_event_payload(event)
                decoded_registry_payloads[event.compressed_payload] = payload
        schema_version = payload.get("schema_version")
        if schema_version == REGISTRY_REVISION_V2_SCHEMA:
            unknown_fields = set(payload) - REGISTRY_REVISION_V2_FIELDS
            if unknown_fields:
                _fail(
                    "registry_revision_invalid",
                    (
                        f"{event.record_id} has unknown v2 fields: "
                        f"{sorted(unknown_fields)}"
                    ),
                )
            delta_value = payload.get("delta_snapshot")
            if not isinstance(delta_value, Mapping):
                _fail(
                    "registry_revision_invalid",
                    f"{event.record_id} delta_snapshot is not an object",
                )
            try:
                delta = validate_short_crypto_registry_snapshot(delta_value)
                cumulative = (
                    delta
                    if registry_snapshot is None
                    else merge_short_crypto_registry_snapshots(
                        registry_snapshot,
                        delta,
                    )
                )
            except ValueError as exc:
                raise LifecycleAuditError(
                    "registry_revision_invalid",
                    f"{event.record_id} has an invalid v2 delta",
                ) from exc
            if (
                payload.get("cumulative_snapshot_sha256")
                != cumulative["snapshot_sha256"]
                or payload.get("cumulative_input_file_count")
                != len(cumulative["inputs"])
                or payload.get("cumulative_target_count")
                != len(cumulative["targets"])
            ):
                _fail(
                    "registry_revision_cumulative_mismatch",
                    f"{event.record_id} cumulative proof failed",
                )
            registry_snapshot = cumulative
            revision_targets = delta["targets"]
        else:
            revision_targets = payload.get("targets")
            if not isinstance(revision_targets, list):
                _fail(
                    "registry_revision_invalid",
                    f"{event.record_id} targets is not a list",
                )
            if schema_version == REGISTRY_SNAPSHOT_SCHEMA:
                registry_snapshot = (
                    dict(payload)
                    if event.registry_v1_validated
                    else _validated_v1_registry_payload(
                        payload,
                        record_id=event.record_id,
                    )
                )
            elif schema_version == COMPACTED_REGISTRY_SCHEMA:
                registry_snapshot = None
            else:
                # Preserve the historical target-only journal contract, but
                # never use an unrecognized schema as a v2 cumulative base.
                registry_snapshot = None
        for raw_target in revision_targets:
            if not isinstance(raw_target, Mapping):
                _fail(
                    "registry_target_invalid",
                    f"{event.record_id} contains a non-object target",
                )
            candidate = _target_identity(raw_target)
            slug = str(candidate["slug"])
            previous = targets.get(slug)
            if previous is None:
                targets[slug] = candidate
                continue
            semantic_previous = {
                key: previous[key] for key in _TARGET_IDENTITY_FIELDS
            }
            semantic_candidate = {
                key: candidate[key] for key in _TARGET_IDENTITY_FIELDS
            }
            if semantic_previous != semantic_candidate:
                _fail(
                    "registry_target_conflict",
                    f"{slug} changed identity across finalized revisions",
                )
            previous_observation = (
                _epoch_ms(
                    previous["announced_at"],
                    label=f"{slug}.announced_at",
                ),
                previous["announcement_record_id"],
            )
            candidate_observation = (
                _epoch_ms(
                    candidate["announced_at"],
                    label=f"{slug}.announced_at",
                ),
                candidate["announcement_record_id"],
            )
            if candidate_observation < previous_observation:
                previous["announced_at"] = candidate["announced_at"]
                previous["announcement_record_id"] = candidate[
                    "announcement_record_id"
                ]
    return tuple(
        sorted(
            targets.values(),
            key=lambda item: (
                item["opens_at_ms"],
                item["closes_at_ms"],
                item["slug"],
            ),
        )
    )


def _direct_inner(located: _LocatedRecord) -> Mapping[str, Any] | None:
    inner = located.record.get("payload")
    if not isinstance(inner, Mapping):
        return None
    if inner.get("source") == "dynamic_short_crypto_service":
        return None
    return inner


def _record_received_at_ms(located: _LocatedRecord) -> int:
    return _epoch_ms(
        located.record.get("received_at"),
        label=f"{located.record.get('record_id')}.received_at",
    )


def _announcement_valid(
    target: Mapping[str, Any],
    by_id: Mapping[str, _LocatedRecord | None],
) -> tuple[bool, list[str]]:
    record_id = str(target["announcement_record_id"])
    located = by_id.get(record_id)
    if located is None or located.record.get("source") != "clob_market_ws":
        return False, []
    inner = _direct_inner(located)
    payload = inner.get("payload") if isinstance(inner, Mapping) else None
    expected_tokens = [target["up_token_id"], target["down_token_id"]]
    valid = (
        isinstance(inner, Mapping)
        and inner.get("schema_version") == "clob-market-ws.new_market.v1"
        and inner.get("event_type") == "new_market"
        and isinstance(payload, Mapping)
        and payload.get("slug") == target["slug"]
        and payload.get("condition_id") == target["condition_id"]
        and payload.get("clob_token_ids") == expected_tokens
    )
    return valid, [record_id] if valid else []


def _events_for_slug(
    events: tuple[_ServiceEvent, ...],
    slug: str,
) -> tuple[_ServiceEvent, ...]:
    return tuple(
        event
        for event in events
        if event.payload.get("slug") == slug
    )


def _events_at_cutoff(
    events: tuple[_ServiceEvent, ...],
    cutoff_ms: int,
) -> tuple[_ServiceEvent, ...]:
    return tuple(
        event
        for event in events
        if (
            event.received_at_ms <= cutoff_ms
            and event.manifest_finalized_at_ms <= cutoff_ms
        )
    )


def _gamma_identity(
    target: Mapping[str, Any],
    events: tuple[_ServiceEvent, ...],
    by_id: Mapping[str, _LocatedRecord | None],
) -> tuple[bool, list[str]]:
    valid_ids: list[str] = []
    for event in events:
        if (
            event.event_type != "gamma_target_verified"
            or event.received_at_ms >= target["opens_at_ms"]
            or event.payload.get("market_id") != target["market_id"]
        ):
            continue
        gamma_id = event.payload.get("gamma_record_id")
        located = by_id.get(str(gamma_id))
        if (
            isinstance(gamma_id, str)
            and located is not None
            and located.record.get("source") == "gamma_http"
        ):
            valid_ids.extend((gamma_id, event.record_id))
    return bool(valid_ids), sorted(set(valid_ids))


def _subscription(
    target: Mapping[str, Any],
    events: tuple[_ServiceEvent, ...],
) -> tuple[_Decision | None, list[str]]:
    decisions: list[tuple[_Decision, str, str]] = []
    for event in events:
        if event.event_type != "capture_decision":
            continue
        payload = event.payload
        if payload.get("action") != "subscribe":
            continue
        emitted_at_ms = payload.get("emitted_at_ms")
        decision_id = payload.get("decision_id")
        group_id = payload.get("group_id")
        bindings = payload.get("targets")
        if (
            isinstance(emitted_at_ms, bool)
            or not isinstance(emitted_at_ms, int)
            or emitted_at_ms >= target["opens_at_ms"]
            or not isinstance(decision_id, str)
            or not decision_id
            or not isinstance(group_id, str)
            or not group_id
            or not isinstance(bindings, list)
        ):
            continue
        matching = [
            item
            for item in bindings
            if isinstance(item, Mapping)
            and item.get("slug") == target["slug"]
            and item.get("announcement_record_id")
            == target["announcement_record_id"]
            and item.get("rule_hash") == target["rule_hash"]
            and item.get("up_token_id") == target["up_token_id"]
            and item.get("down_token_id") == target["down_token_id"]
        ]
        if len(matching) != 1:
            continue
        receipts = [
            receipt
            for receipt in events
            if receipt.event_type == "worker_start_applied"
            and receipt.payload.get("decision_id") == decision_id
            and receipt.payload.get("group_id") == group_id
            and target["slug"]
            in (
                receipt.payload.get("target_slugs")
                if isinstance(receipt.payload.get("target_slugs"), list)
                else []
            )
            and receipt.received_at_ms < target["opens_at_ms"]
        ]
        for receipt in receipts:
            decisions.append(
                (
                    _Decision(
                        decision_id=decision_id,
                        group_id=group_id,
                        emitted_at_ms=emitted_at_ms,
                        record_id=event.record_id,
                    ),
                    receipt.record_id,
                    event.record_id,
                )
            )
    if not decisions:
        return None, []
    decisions.sort(
        key=lambda item: (
            item[0].emitted_at_ms,
            item[0].decision_id,
            item[1],
        )
    )
    selected = decisions[0]
    return selected[0], [selected[2], selected[1]]


def _group_records(
    by_id: Mapping[str, _LocatedRecord | None],
    group_id: str,
) -> tuple[_LocatedRecord, ...]:
    return tuple(
        located
        for located in by_id.values()
        if located is not None and located.group_id == group_id
    )


def _resnapshot(
    target: Mapping[str, Any],
    decision: _Decision | None,
    events: tuple[_ServiceEvent, ...],
    group_records: tuple[_LocatedRecord, ...],
) -> tuple[bool, list[str]]:
    if decision is None:
        return False, []
    ready = [
        event
        for event in events
        if event.event_type == "worker_ready"
        and event.payload.get("decision_id") == decision.decision_id
        and event.payload.get("group_id") == decision.group_id
        and event.received_at_ms < target["opens_at_ms"]
        and isinstance(event.payload.get("snapshot_watermark_ns"), int)
        and not isinstance(event.payload.get("snapshot_watermark_ns"), bool)
        and event.payload["snapshot_watermark_ns"] >= 0
    ]
    snapshots = [
        located
        for located in group_records
        if located.record.get("source") == "clob_http"
        and _record_received_at_ms(located) < target["opens_at_ms"]
    ]
    resyncs = [
        located
        for located in group_records
        if (
            (inner := _direct_inner(located)) is not None
            and located.record.get("source") == "clob_market_ws"
            and inner.get("event_type") == "resync_complete"
            and _record_received_at_ms(located) < target["opens_at_ms"]
        )
    ]
    valid = bool(ready and snapshots and resyncs)
    evidence = (
        [ready[0].record_id]
        + [str(snapshots[0].record["record_id"])]
        + [str(resyncs[0].record["record_id"])]
        if valid
        else []
    )
    return valid, evidence


def _closure(
    target: Mapping[str, Any],
    group_id: str | None,
    events: tuple[_ServiceEvent, ...],
) -> tuple[_ServiceEvent | None, list[str]]:
    if group_id is None:
        return None, []
    matches = [
        event
        for event in events
        if event.event_type == "decision_to_trade_l2_closure"
        and event.payload.get("slug") == target["slug"]
        and event.payload.get("condition_id") == target["condition_id"]
        and event.payload.get("group_id") == group_id
        and event.payload.get("opens_at_ms") == target["opens_at_ms"]
        and event.payload.get("closes_at_ms") == target["closes_at_ms"]
        and event.payload.get("closed_through_ms") == target["closes_at_ms"]
    ]
    if len(matches) != 1:
        return None, []
    return matches[0], [matches[0].record_id]


def _clob_continuity(
    target: Mapping[str, Any],
    closure: _ServiceEvent | None,
    group_records: tuple[_LocatedRecord, ...],
) -> tuple[bool, list[str]]:
    if closure is None:
        return False, []
    data_records: list[_LocatedRecord] = []
    disconnects: list[_LocatedRecord] = []
    for located in group_records:
        if located.record.get("source") != "clob_market_ws":
            continue
        inner = _direct_inner(located)
        if inner is None:
            continue
        received_at_ms = _record_received_at_ms(located)
        if not target["opens_at_ms"] <= received_at_ms <= target["closes_at_ms"]:
            continue
        event_type = inner.get("event_type")
        if event_type in {"book", "price_change", "tick_size_change"}:
            data_records.append(located)
        if event_type == "disconnected":
            disconnects.append(located)
    valid = bool(data_records) and not disconnects
    evidence = (
        [closure.record_id, str(data_records[0].record["record_id"])]
        if valid
        else []
    )
    return valid, evidence


def _boundary(
    target: Mapping[str, Any],
    events: tuple[_ServiceEvent, ...],
    by_id: Mapping[str, _LocatedRecord | None],
    *,
    role: str,
) -> tuple[bool, list[str]]:
    boundary_ms = (
        target["opens_at_ms"] if role == "open" else target["closes_at_ms"]
    )
    evidence = [
        event
        for event in events
        if event.event_type == "chainlink_boundary_evidence"
        and event.payload.get("slug") == target["slug"]
        and event.payload.get("boundary_role") == role
        and event.payload.get("inner_timestamp_ms") == boundary_ms
        and isinstance(event.payload.get("source_record_id"), str)
    ]
    committed = [
        event
        for event in events
        if event.event_type == "chainlink_boundary_evidence_committed"
        and event.payload.get("slug") == target["slug"]
        and event.payload.get("boundary_role") == role
        and isinstance(event.payload.get("source_record_id"), str)
    ]
    for evidence_event in evidence:
        source_id = str(evidence_event.payload["source_record_id"])
        source = by_id.get(source_id)
        if source is None or source.record.get("source") != "rtds_ws":
            continue
        matching_commits = [
            event
            for event in committed
            if event.payload.get("source_record_id") == source_id
        ]
        if matching_commits:
            return True, [
                source_id,
                evidence_event.record_id,
                matching_commits[0].record_id,
            ]
    return False, []


def _strict_settlement(
    target: Mapping[str, Any],
    events: tuple[_ServiceEvent, ...],
) -> _ServiceEvent | None:
    matches = [
        event
        for event in events
        if event.event_type == "settlement_reconciled"
        and event.payload.get("slug") == target["slug"]
        and event.payload.get("condition_id") == target["condition_id"]
        and event.payload.get("status") == "resolved"
        and event.payload.get("action") == "strict_settlement_committed"
        and event.payload.get("chainlink_boundary_status") == "committed"
        and event.payload.get("outcome") in {"Up", "Down"}
    ]
    return matches[0] if len(matches) == 1 else None


def _pong(
    record_id: Any,
    by_id: Mapping[str, _LocatedRecord | None],
    *,
    group_id: str,
) -> tuple[_LocatedRecord, Mapping[str, Any]] | None:
    if not isinstance(record_id, str):
        return None
    located = by_id.get(record_id)
    if located is None or located.group_id != group_id:
        return None
    inner = _direct_inner(located)
    if (
        inner is None
        or located.record.get("source") != "clob_market_ws"
        or inner.get("schema_version")
        != "edge-lab-recorder.heartbeat-ack.v1"
        or inner.get("event_type") != "heartbeat_ack"
        or str(inner.get("raw_frame", "")).strip().upper() != "PONG"
    ):
        return None
    return located, inner


def _liveness(
    target: Mapping[str, Any],
    settlement: _ServiceEvent | None,
    by_id: Mapping[str, _LocatedRecord | None],
    *,
    group_id: str | None,
    tolerance_ms: int,
) -> tuple[bool, list[str]]:
    if settlement is None or group_id is None:
        return False, []
    proof = settlement.payload.get("close_liveness")
    if not isinstance(proof, Mapping) or proof.get("status") != "verified":
        return False, []
    before = _pong(proof.get("before_record_id"), by_id, group_id=group_id)
    after = _pong(proof.get("after_record_id"), by_id, group_id=group_id)
    if before is None or after is None:
        return False, []
    before_located, before_inner = before
    after_located, after_inner = after
    before_ms = _record_received_at_ms(before_located)
    after_ms = _record_received_at_ms(after_located)
    valid = (
        before_located.record["record_id"] != after_located.record["record_id"]
        and before_inner.get("session_id") == after_inner.get("session_id")
        == proof.get("session_id")
        and before_inner.get("connection_id")
        == after_inner.get("connection_id")
        == proof.get("connection_id")
        == group_id
        and proof.get("before_received_at_ms") == before_ms
        and proof.get("after_received_at_ms") == after_ms
        and target["closes_at_ms"] - tolerance_ms
        <= before_ms
        <= target["closes_at_ms"]
        <= after_ms
        <= target["closes_at_ms"] + tolerance_ms
    )
    return (
        valid,
        [
            str(before_located.record["record_id"]),
            str(after_located.record["record_id"]),
        ]
        if valid
        else [],
    )


def _gamma_winner(
    settlement: _ServiceEvent | None,
    by_id: Mapping[str, _LocatedRecord | None],
) -> tuple[bool, list[str]]:
    if settlement is None:
        return False, []
    gamma_ids = settlement.payload.get("gamma_record_ids")
    if not isinstance(gamma_ids, list) or not gamma_ids:
        return False, []
    valid_ids = []
    for record_id in gamma_ids:
        if not isinstance(record_id, str):
            continue
        located = by_id.get(record_id)
        if located is not None and located.record.get("source") == "gamma_http":
            valid_ids.append(record_id)
    valid = len(valid_ids) == len(gamma_ids)
    return valid, valid_ids + [settlement.record_id] if valid else []


def _settlement_valid(
    target: Mapping[str, Any],
    settlement: _ServiceEvent | None,
    by_id: Mapping[str, _LocatedRecord | None],
    *,
    boundary_open_ids: list[str],
    boundary_close_ids: list[str],
    liveness_ids: list[str],
) -> tuple[bool, list[str]]:
    if settlement is None:
        return False, []
    required = settlement.payload.get("required_record_ids")
    if (
        not isinstance(required, list)
        or len(required) != 6
        or any(not isinstance(item, str) for item in required)
        or any(item not in by_id for item in required)
    ):
        return False, []
    required_set = set(required)
    role_ids = {
        str(target["announcement_record_id"]),
        boundary_open_ids[0] if boundary_open_ids else "",
        boundary_close_ids[0] if boundary_close_ids else "",
        *liveness_ids,
    }
    valid = "" not in role_ids and role_ids <= required_set
    return valid, [settlement.record_id, *required] if valid else []


def _finalization(
    target: Mapping[str, Any],
    events: tuple[_ServiceEvent, ...],
    *,
    group_id: str | None,
    dynamic_roots: tuple[Path, ...],
    closure: _ServiceEvent | None,
    settlement: _ServiceEvent | None,
    evidence_cutoff_ms: int,
) -> tuple[bool, list[str]]:
    if group_id is None or closure is None or settlement is None:
        return False, []
    checkpoints = [
        event
        for event in events
        if event.event_type == "full_window_capture_close_checkpoint"
        and event.payload.get("slug") == target["slug"]
        and event.payload.get("condition_id") == target["condition_id"]
        and event.payload.get("group_id") == group_id
        and event.payload.get("opens_at_ms") == target["opens_at_ms"]
        and event.payload.get("closes_at_ms") == target["closes_at_ms"]
        and isinstance(event.payload.get("finalized_through_ms"), int)
        and event.payload["finalized_through_ms"] >= target["closes_at_ms"]
    ]
    group_roots = [
        root / "groups" / group_id
        for root in dynamic_roots
        if (root / "groups" / group_id).is_dir()
    ]
    manifests: list[Path] = []
    for group_root in group_roots:
        for manifest in _finalized_manifests(group_root):
            (
                manifest_bytes,
                finalized_at_ms,
            ) = _manifest_header_before_raw(manifest)
            if finalized_at_ms > evidence_cutoff_ms:
                continue
            pair = _source_pair(manifest)
            if pair.manifest_bytes != manifest_bytes:
                _fail(
                    "source_changed_during_audit",
                    (
                        "manifest changed after cutoff validation: "
                        f"{manifest}"
                    ),
                )
            manifests.append(manifest)
    valid = len(checkpoints) == 1 and bool(manifests)
    return (
        valid,
        [checkpoints[0].record_id, closure.record_id, settlement.record_id]
        if valid
        else [],
    )


def _ratio(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0"
    value = Decimal(numerator) / Decimal(denominator)
    return format(value.normalize(), "f")


def _prospective_cohort_commitment(
    events: tuple[_ServiceEvent, ...],
    *,
    as_of_ms: int,
    sample_size: int,
    settlement_timeout_ms: int,
    threshold: Decimal,
) -> _CohortCommitment | None:
    candidates: list[_CohortCommitment] = []
    for event in events:
        if (
            event.event_type != "lifecycle_cohort_committed"
            or event.received_at_ms > as_of_ms
        ):
            continue
        source_path = event.manifest_path.parent
        try:
            payload = validate_cohort_payload(
                event.event_type,
                event.payload,
                outer_kind=event.kind,
                record_id=event.record_id,
                received_at_ms=event.received_at_ms,
                settlement_timeout_ms=settlement_timeout_ms,
                sample_size=sample_size,
                threshold=threshold,
            )
        except LifecycleCohortContractError as exc:
            _fail(
                "cohort_commitment_invalid",
                str(exc),
            )
        if (
            source_path.name != "dynamic_short_crypto_service"
            or source_path.parent.name != "raw"
            or source_path.parent.parent.name != "control"
        ):
            _fail(
                "cohort_commitment_invalid",
                f"{event.record_id} is not finalized in control",
            )
        scheduler_now_ms = payload["scheduler_now_ms"]
        eligibility_start_ms = payload["eligibility_start_ms"]
        candidates.append(
            _CohortCommitment(
                record_id=event.record_id,
                manifest_path=event.manifest_path,
                committed_at_ms=event.received_at_ms,
                scheduler_now_ms=scheduler_now_ms,
                eligibility_start_ms=eligibility_start_ms,
            )
        )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            item.committed_at_ms,
            str(item.manifest_path),
            item.record_id,
        ),
    )


def _cohort_commitment_chain(
    events: tuple[_ServiceEvent, ...],
    *,
    as_of_ms: int,
    sample_size: int,
    settlement_timeout_ms: int,
    threshold: Decimal,
) -> tuple[_CohortCommitment, ...]:
    root = _prospective_cohort_commitment(
        events,
        as_of_ms=as_of_ms,
        sample_size=sample_size,
        settlement_timeout_ms=settlement_timeout_ms,
        threshold=threshold,
    )
    remediations: list[_CohortCommitment] = []
    for event in events:
        if (
            event.event_type != "lifecycle_remediation_cohort_committed"
            or event.received_at_ms > as_of_ms
        ):
            continue
        source_path = event.manifest_path.parent
        try:
            payload = validate_cohort_payload(
                event.event_type,
                event.payload,
                outer_kind=event.kind,
                record_id=event.record_id,
                received_at_ms=event.received_at_ms,
                settlement_timeout_ms=settlement_timeout_ms,
                sample_size=sample_size,
                threshold=threshold,
            )
        except LifecycleCohortContractError as exc:
            _fail(
                "cohort_remediation_invalid",
                str(exc),
            )
        if (
            source_path.name != "dynamic_short_crypto_service"
            or source_path.parent.name != "raw"
            or source_path.parent.parent.name != "control"
        ):
            _fail(
                "cohort_remediation_invalid",
                f"{event.record_id} is not finalized in control",
            )
        scheduler_now_ms = payload["scheduler_now_ms"]
        eligibility_start_ms = payload["eligibility_start_ms"]
        predecessor_record_id = payload[
            "predecessor_commitment_record_id"
        ]
        remediations.append(
            _CohortCommitment(
                record_id=event.record_id,
                manifest_path=event.manifest_path,
                committed_at_ms=event.received_at_ms,
                scheduler_now_ms=scheduler_now_ms,
                eligibility_start_ms=eligibility_start_ms,
                role="remediation",
                predecessor_commitment_record_id=predecessor_record_id,
                remediation_reason_code=payload[
                    "remediation_reason_code"
                ],
                remediation_plan_id=payload["remediation_plan_id"],
            )
        )
    remediation_record_ids = [
        item.record_id for item in remediations
    ]
    if len(remediation_record_ids) != len(set(remediation_record_ids)):
        _fail(
            "cohort_remediation_duplicate",
            "remediation commitment record IDs must be unique",
        )
    remediation_by_id = {
        item.record_id: item for item in remediations
    }
    for start_record_id in remediation_by_id:
        cursor = start_record_id
        visited: set[str] = set()
        while cursor in remediation_by_id:
            if cursor in visited:
                _fail(
                    "cohort_remediation_cycle",
                    "remediation commitment predecessors are cyclic",
                )
            visited.add(cursor)
            predecessor = remediation_by_id[
                cursor
            ].predecessor_commitment_record_id
            if predecessor is None:
                break
            cursor = predecessor
    if not remediations:
        return () if root is None else (root,)
    if root is None:
        _fail(
            "cohort_remediation_predecessor_missing",
            "a remediation commitment requires an earlier root commitment",
        )
    chain = [root]
    unused = {item.record_id: item for item in remediations}
    seen = {root.record_id}
    while unused:
        tip = chain[-1]
        children = sorted(
            (
                item
                for item in unused.values()
                if item.predecessor_commitment_record_id == tip.record_id
            ),
            key=lambda item: (
                item.committed_at_ms,
                str(item.manifest_path),
                item.record_id,
            ),
        )
        if len(children) != 1:
            code = (
                "cohort_remediation_branch"
                if len(children) > 1
                else "cohort_remediation_chain_invalid"
            )
            _fail(
                code,
                "remediation commitments must form one exact linear chain",
            )
        child = children[0]
        if (
            child.record_id in seen
            or child.committed_at_ms <= tip.committed_at_ms
        ):
            _fail(
                "cohort_remediation_chain_invalid",
                "remediation commitment order is cyclic or non-increasing",
            )
        chain.append(child)
        seen.add(child.record_id)
        del unused[child.record_id]
    return tuple(chain)


def _evaluate_target_reports(
    targets: Iterable[Mapping[str, Any]],
    *,
    service_events: tuple[_ServiceEvent, ...],
    by_id: _RecordInventory,
    group_records_by_id: Mapping[
        str, tuple[_LocatedRecord, ...]
    ],
    dynamic_roots: tuple[Path, ...],
    evidence_cutoff_ms: int,
    settlement_timeout_ms: int,
    close_liveness_tolerance_ms: int,
) -> list[dict[str, Any]]:
    cutoff_by_id = _RecordCutoffView(
        by_id,
        cutoff_ms=evidence_cutoff_ms,
    )
    target_values = list(targets)
    subscriptions = {
        str(target["slug"]): _subscription(target, service_events)
        for target in target_values
    }
    reports: list[dict[str, Any]] = []
    for target in target_values:
        slug = str(target["slug"])
        slug_events = _events_for_slug(service_events, slug)
        announcement_ok, announcement_ids = _announcement_valid(
            target,
            cutoff_by_id,
        )
        gamma_ok, gamma_ids = _gamma_identity(
            target,
            slug_events,
            cutoff_by_id,
        )
        decision, subscription_ids = subscriptions[slug]
        group_id = None if decision is None else decision.group_id
        group_records = tuple(
            located
            for located in (
                ()
                if group_id is None
                else group_records_by_id.get(group_id, ())
            )
            if (
                located.manifest_finalized_at_ms <= evidence_cutoff_ms
                and _record_received_at_ms(located)
                <= evidence_cutoff_ms
            )
        )
        resnapshot_ok, resnapshot_ids = _resnapshot(
            target,
            decision,
            service_events,
            group_records,
        )
        closure, closure_ids = _closure(
            target,
            group_id,
            slug_events,
        )
        continuity_ok, continuity_ids = _clob_continuity(
            target,
            closure,
            group_records,
        )
        open_ok, open_ids = _boundary(
            target,
            slug_events,
            cutoff_by_id,
            role="open",
        )
        close_ok, close_ids = _boundary(
            target,
            slug_events,
            cutoff_by_id,
            role="close",
        )
        settlement = _strict_settlement(target, slug_events)
        liveness_ok, liveness_ids = _liveness(
            target,
            settlement,
            cutoff_by_id,
            group_id=group_id,
            tolerance_ms=close_liveness_tolerance_ms,
        )
        winner_ok, winner_ids = _gamma_winner(
            settlement,
            cutoff_by_id,
        )
        settlement_ok, settlement_ids = _settlement_valid(
            target,
            settlement,
            cutoff_by_id,
            boundary_open_ids=open_ids,
            boundary_close_ids=close_ids,
            liveness_ids=liveness_ids,
        )
        finalization_ok, finalization_ids = _finalization(
            target,
            slug_events,
            group_id=group_id,
            dynamic_roots=dynamic_roots,
            closure=closure,
            settlement=settlement,
            evidence_cutoff_ms=evidence_cutoff_ms,
        )
        checks = {
            "announcement": announcement_ok,
            "gamma_identity": gamma_ok,
            "preopen_subscription_decision_receipt": decision is not None,
            "rest_resnapshot_watermark": resnapshot_ok,
            "clob_increment_continuity": continuity_ok,
            "chainlink_ptb": open_ok,
            "chainlink_close_boundary": close_ok,
            "same_connection_liveness_bracket": liveness_ok,
            "gamma_winner": winner_ok,
            "settlement_reconciliation": settlement_ok,
            "finalization_manifest": finalization_ok,
        }
        if tuple(checks) != STAGES:
            raise AssertionError("lifecycle stage order drift")
        evidence = {
            "announcement": announcement_ids,
            "gamma_identity": gamma_ids,
            "preopen_subscription_decision_receipt": subscription_ids,
            "rest_resnapshot_watermark": resnapshot_ids,
            "clob_increment_continuity": continuity_ids or closure_ids,
            "chainlink_ptb": open_ids,
            "chainlink_close_boundary": close_ids,
            "same_connection_liveness_bracket": liveness_ids,
            "gamma_winner": winner_ids,
            "settlement_reconciliation": settlement_ids,
            "finalization_manifest": finalization_ids,
        }
        missing = [
            stage for stage, passed in checks.items() if not passed
        ]
        reports.append(
            {
                "slug": slug,
                "condition_id": target["condition_id"],
                "horizon": target["horizon"],
                "opens_at_ms": target["opens_at_ms"],
                "closes_at_ms": target["closes_at_ms"],
                "mature_at_ms": (
                    target["closes_at_ms"] + settlement_timeout_ms
                ),
                "group_id": group_id,
                "checks": checks,
                "evidence_record_ids": evidence,
                "complete": not missing,
                "missing_stages": missing,
            }
        )
    return reports


def _build_lifecycle_report(
    *,
    dynamic_roots: tuple[Path, ...],
    dynamic_run_set_complete: bool,
    main_root: Path,
    as_of_ms: int,
    sample_size: int,
    settlement_timeout_ms: int,
    threshold: Decimal,
    close_liveness_tolerance_ms: int,
    by_id: _RecordInventory,
    service_events: tuple[_ServiceEvent, ...],
    manifest_entries: list[tuple[Path, str | None, int]],
    manifest_count: int,
    record_count: int,
) -> dict[str, Any]:
    active_events = _events_at_cutoff(service_events, as_of_ms)
    registered = _registry_targets(active_events)
    historical_mature = [
        target
        for target in registered
        if target["closes_at_ms"] + settlement_timeout_ms <= as_of_ms
    ]
    historical_selected = historical_mature[:sample_size]
    cohort_chain = _cohort_commitment_chain(
        active_events,
        as_of_ms=as_of_ms,
        sample_size=sample_size,
        settlement_timeout_ms=settlement_timeout_ms,
        threshold=threshold,
    )
    cohort_commitment = cohort_chain[0] if cohort_chain else None
    cohort_selections: list[
        tuple[
            _CohortCommitment,
            int,
            tuple[_ServiceEvent, ...],
            list[dict[str, Any]],
            list[dict[str, Any]],
        ]
    ] = []
    for index, commitment in enumerate(cohort_chain):
        evidence_cutoff_ms = (
            cohort_chain[index + 1].committed_at_ms
            if index + 1 < len(cohort_chain)
            else as_of_ms
        )
        commitment_events = _events_at_cutoff(
            active_events,
            evidence_cutoff_ms,
        )
        commitment_registered = _registry_targets(commitment_events)
        commitment_mature = [
            target
            for target in commitment_registered
            if (
                target["opens_at_ms"]
                >= commitment.eligibility_start_ms
                and target["closes_at_ms"] + settlement_timeout_ms
                <= evidence_cutoff_ms
            )
        ]
        cohort_selections.append(
            (
                commitment,
                evidence_cutoff_ms,
                commitment_events,
                commitment_mature,
                commitment_mature[:sample_size],
            )
        )
    if not cohort_selections:
        mature = historical_mature
        selected = historical_selected
    else:
        _, _, _, mature, selected = cohort_selections[-1]

    evaluation_contexts = [
        (historical_selected, active_events),
        *[
            (cohort_selected, commitment_events)
            for (
                _,
                _,
                commitment_events,
                _,
                cohort_selected,
            ) in cohort_selections
        ],
    ]
    selected_group_ids = {
        decision.group_id
        for context_targets, context_events in evaluation_contexts
        for target in context_targets
        for decision, _ in (
            (_subscription(target, context_events),)
        )
        if decision is not None
    }
    group_records_by_id = _load_selected_group_records(
        manifest_entries,
        selected_group_ids,
        by_id,
        as_of_ms=as_of_ms,
    )
    historical_target_reports = _evaluate_target_reports(
        historical_selected,
        service_events=active_events,
        by_id=by_id,
        group_records_by_id=group_records_by_id,
        dynamic_roots=dynamic_roots,
        evidence_cutoff_ms=as_of_ms,
        settlement_timeout_ms=settlement_timeout_ms,
        close_liveness_tolerance_ms=close_liveness_tolerance_ms,
    )

    cohort_history: list[dict[str, Any]] = []
    base_gate_statuses: list[str] = []
    for (
        commitment,
        evidence_cutoff_ms,
        commitment_events,
        commitment_mature,
        commitment_selected,
    ) in cohort_selections:
        commitment_reports = _evaluate_target_reports(
            commitment_selected,
            service_events=commitment_events,
            by_id=by_id,
            group_records_by_id=group_records_by_id,
            dynamic_roots=dynamic_roots,
            evidence_cutoff_ms=evidence_cutoff_ms,
            settlement_timeout_ms=settlement_timeout_ms,
            close_liveness_tolerance_ms=close_liveness_tolerance_ms,
        )
        complete_count = sum(
            1 for target in commitment_reports if target["complete"]
        )
        sampled_count = len(commitment_reports)
        ratio_text = _ratio(complete_count, sampled_count)
        enough_targets = sampled_count >= sample_size
        threshold_met = (
            enough_targets and Decimal(ratio_text) >= threshold
        )
        base_gate_status = (
            "waiting_for_mature_targets"
            if not enough_targets
            else "passed"
            if threshold_met
            else "failed"
        )
        base_gate_statuses.append(base_gate_status)
        cohort_history.append(
            {
                "index": len(cohort_history),
                "role": commitment.role,
                "commitment": {
                    "record_id": commitment.record_id,
                    "manifest_path": str(commitment.manifest_path),
                    "committed_at_ms": commitment.committed_at_ms,
                    "scheduler_now_ms": commitment.scheduler_now_ms,
                    "eligibility_start_ms": (
                        commitment.eligibility_start_ms
                    ),
                },
                "predecessor_commitment_record_id": (
                    commitment.predecessor_commitment_record_id
                ),
                "remediation_reason_code": (
                    commitment.remediation_reason_code
                ),
                "remediation_plan_id": commitment.remediation_plan_id,
                "evidence_cutoff_ms": evidence_cutoff_ms,
                "mature_target_count": len(commitment_mature),
                "sampled_target_count": sampled_count,
                "complete_target_count": complete_count,
                "completeness_ratio": ratio_text,
                "threshold_met": threshold_met,
                "gate_status": base_gate_status,
                "targets": commitment_reports,
            }
        )

    for index in range(1, len(cohort_history)):
        predecessor = cohort_history[index - 1]
        commitment = cohort_chain[index]
        if (
            commitment.predecessor_commitment_record_id
            != predecessor["commitment"]["record_id"]
        ):
            _fail(
                "cohort_remediation_chain_invalid",
                "remediation predecessor is not the exact chain tip",
            )
        if (
            base_gate_statuses[index - 1]
            == "waiting_for_mature_targets"
        ):
            _fail(
                "cohort_remediation_committed_too_early",
                "remediation predates predecessor maturity",
            )
        if base_gate_statuses[index - 1] != "failed":
            _fail(
                "cohort_remediation_predecessor_not_failed",
                "remediation requires a fully mature failed predecessor",
            )
        predecessor_mature_at_ms = max(
            target["mature_at_ms"]
            for target in predecessor["targets"]
        )
        if commitment.committed_at_ms < predecessor_mature_at_ms:
            _fail(
                "cohort_remediation_committed_too_early",
                "remediation predates predecessor maturity",
            )

    if len(cohort_history) > 1:
        for predecessor in cohort_history[:-1]:
            predecessor["gate_status"] = (
                "failed_retained_for_remediation"
            )
        active = cohort_history[-1]
        active_base_status = base_gate_statuses[-1]
        active["gate_status"] = (
            "waiting_for_remediation_mature_targets"
            if active_base_status == "waiting_for_mature_targets"
            else (
                "passed_after_remediation_with_retained_failures"
                if active_base_status == "passed"
                else "remediation_failed_with_retained_failures"
            )
        )

    target_reports = (
        historical_target_reports
        if not cohort_history
        else cohort_history[-1]["targets"]
    )
    complete_count = sum(
        1 for target in target_reports if target["complete"]
    )
    sampled_count = len(target_reports)
    ratio_text = _ratio(complete_count, sampled_count)
    enough_targets = sampled_count >= sample_size
    threshold_met = enough_targets and Decimal(ratio_text) >= threshold
    gate_status = (
        "waiting_for_mature_targets"
        if not enough_targets
        else "passed"
        if threshold_met
        else "failed"
    )
    if cohort_history:
        gate_status = cohort_history[-1]["gate_status"]

    historical_complete_count = sum(
        1 for target in historical_target_reports if target["complete"]
    )
    historical_sampled_count = len(historical_target_reports)
    historical_ratio_text = _ratio(
        historical_complete_count,
        historical_sampled_count,
    )
    historical_enough_targets = historical_sampled_count >= sample_size
    historical_threshold_met = (
        historical_enough_targets
        and Decimal(historical_ratio_text) >= threshold
    )
    historical_gate_status = (
        "waiting_for_mature_targets"
        if not historical_enough_targets
        else "passed"
        if historical_threshold_met
        else "failed"
    )
    body: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "as_of_ms": as_of_ms,
        "cohort_mode": (
            "historical_global_first_mature"
            if cohort_commitment is None
            else (
                "append_only_remediation"
                if len(cohort_history) > 1
                else "prospective_committed"
            )
        ),
        "cohort_commitment": (
            None
            if cohort_commitment is None
            else {
                "record_id": cohort_commitment.record_id,
                "manifest_path": str(cohort_commitment.manifest_path),
                "committed_at_ms": cohort_commitment.committed_at_ms,
                "scheduler_now_ms": cohort_commitment.scheduler_now_ms,
                "eligibility_start_ms": (
                    cohort_commitment.eligibility_start_ms
                ),
                "earliest_valid_commitment_wins": True,
            }
        ),
        "active_cohort_commitment": (
            None
            if not cohort_history
            else cohort_history[-1]["commitment"]
        ),
        "active_cohort_index": (
            None if not cohort_history else len(cohort_history) - 1
        ),
        "prior_failed_cohort_count": sum(
            1
            for cohort in cohort_history[:-1]
            if cohort["gate_status"]
            == "failed_retained_for_remediation"
        ),
        "cohort_history": cohort_history,
        "sample_size_required": sample_size,
        "settlement_timeout_ms": settlement_timeout_ms,
        "close_liveness_tolerance_ms": close_liveness_tolerance_ms,
        "threshold": format(threshold.normalize(), "f"),
        "registered_target_count": len(registered),
        "mature_target_count": len(mature),
        "sampled_target_count": sampled_count,
        "complete_target_count": complete_count,
        "completeness_ratio": ratio_text,
        "threshold_met": threshold_met,
        "gate_status": gate_status,
        "targets": target_reports,
        "historical_global_first_mature": {
            "mature_target_count": len(historical_mature),
            "sampled_target_count": historical_sampled_count,
            "complete_target_count": historical_complete_count,
            "completeness_ratio": historical_ratio_text,
            "threshold_met": historical_threshold_met,
            "gate_status": historical_gate_status,
            "targets": historical_target_reports,
        },
        "input_inventory": {
            "main_capture_root": str(main_root),
            "dynamic_run_roots": [str(root) for root in dynamic_roots],
            "dynamic_run_set_complete": dynamic_run_set_complete,
            "evidence_cutoff_ms": as_of_ms,
            "finalized_manifest_count": manifest_count,
            "finalized_record_count": record_count,
            "partial_inventory_policy": (
                "excluded_mutable_diagnostics_from_canonical_report"
            ),
        },
        "excluded_partial_count": 0,
        "excluded_partial_paths": [],
        "public_only": True,
        "network_requests": 0,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
        "actual_fill": False,
        "authenticated_fill": False,
        "classification": "insufficient_data",
    }
    body["report_id"] = hashlib.sha256(
        canonical_json_bytes(body)
    ).hexdigest()
    return body


def audit_phase2_lifecycle(
    dynamic_run_roots: Iterable[str | Path],
    *,
    main_capture_root: str | Path,
    as_of_ms: int,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    settlement_timeout_ms: int = DEFAULT_SETTLEMENT_TIMEOUT_MS,
    threshold: Decimal = DEFAULT_THRESHOLD,
    close_liveness_tolerance_ms: int = (
        DEFAULT_CLOSE_LIVENESS_TOLERANCE_MS
    ),
) -> dict[str, Any]:
    """Reconstruct the first mature target lifecycles from finalized evidence."""

    _int(as_of_ms, label="as_of_ms")
    _int(sample_size, label="sample_size", minimum=1)
    _int(
        settlement_timeout_ms,
        label="settlement_timeout_ms",
    )
    _int(
        close_liveness_tolerance_ms,
        label="close_liveness_tolerance_ms",
        minimum=1,
    )
    if threshold <= 0 or threshold > 1:
        _fail("threshold_invalid", "threshold must be in (0, 1]")
    dynamic_roots = tuple(
        sorted(
            (
                _root(path, label="dynamic_run_root")
                for path in dynamic_run_roots
            ),
            key=str,
        )
    )
    if not dynamic_roots or len(set(dynamic_roots)) != len(dynamic_roots):
        _fail(
            "dynamic_roots_invalid",
            "at least one unique dynamic run root is required",
        )
    dynamic_run_set_complete = _dynamic_run_set_complete(dynamic_roots)
    main_root = _root(main_capture_root, label="main_capture_root")
    inventory_roots = (
        *((root, True) for root in dynamic_roots),
        (main_root, False),
    )
    manifest_entries: list[tuple[Path, str | None, int]] = []
    by_id, service_events, manifest_count, record_count = _load_inventory(
        inventory_roots,
        manifest_entries_out=manifest_entries,
        as_of_ms=as_of_ms,
    )
    try:
        return _build_lifecycle_report(
            dynamic_roots=dynamic_roots,
            dynamic_run_set_complete=dynamic_run_set_complete,
            main_root=main_root,
            as_of_ms=as_of_ms,
            sample_size=sample_size,
            settlement_timeout_ms=settlement_timeout_ms,
            threshold=threshold,
            close_liveness_tolerance_ms=close_liveness_tolerance_ms,
            by_id=by_id,
            service_events=service_events,
            manifest_entries=manifest_entries,
            manifest_count=manifest_count,
            record_count=record_count,
        )
    finally:
        by_id.close()


def write_phase2_lifecycle_report(
    report: Mapping[str, Any],
    *,
    output_base: str | Path,
) -> Path:
    """Write a content-addressed report, refusing non-identical overwrite."""

    if report.get("schema_version") != REPORT_SCHEMA:
        _fail("report_schema_invalid", "report schema is not current")
    report_id = report.get("report_id")
    if not isinstance(report_id, str) or len(report_id) != 64:
        _fail("report_id_invalid", "report ID is not a SHA-256 digest")
    unsigned = dict(report)
    unsigned.pop("report_id", None)
    expected = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    if report_id != expected:
        _fail("report_id_invalid", "report content does not match report ID")
    base = Path(output_base).expanduser().resolve()
    if base.exists() and (base.is_symlink() or not base.is_dir()):
        _fail("output_base_invalid", f"output base is unsafe: {base}")
    base.mkdir(parents=True, exist_ok=True)
    output_dir = base / report_id
    if output_dir.exists() and (
        output_dir.is_symlink() or not output_dir.is_dir()
    ):
        _fail("output_conflict", f"report directory is unsafe: {output_dir}")
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "LIFECYCLE_COMPLETENESS.json"
    value = canonical_json_bytes(dict(report)) + b"\n"
    if output_path.exists():
        if (
            output_path.is_symlink()
            or not output_path.is_file()
            or output_path.read_bytes() != value
        ):
            _fail(
                "output_conflict",
                f"existing report differs: {output_path}",
            )
        return output_path
    temporary = output_dir / f".LIFECYCLE_COMPLETENESS.tmp-{os.getpid()}"
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return output_path


__all__ = [
    "LifecycleAuditError",
    "REPORT_SCHEMA",
    "STAGES",
    "audit_phase2_lifecycle",
    "write_phase2_lifecycle_report",
]
