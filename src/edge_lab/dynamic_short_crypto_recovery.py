"""Finalized-only restart recovery for dynamic short-crypto capture runs.

The replay core is deliberately pure and side-effect free.  It validates and
reduces immutable prior-run evidence, then returns an ordered recovery plan.
The optional cache writes only content-addressed proof indexes and an atomic
snapshot.  The caller must durably persist ``recovery_decision`` before it may
apply any ``worker_actions``.

Mutable ``*.jsonl.partial`` files are inventory only: their paths may be
reported, but their contents are never opened or parsed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
import zlib
from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data_store import (
    _atomic_write_bytes,
    canonical_json_bytes,
    canonical_record_id_from_json,
)
from .lifecycle_cohort_contract import (
    REMEDIATION_COHORT_SCHEMA,
    ROOT_COHORT_SCHEMA,
    LifecycleCohortContractError,
    validate_cohort_payload,
)
from .short_crypto_registry import (
    merge_short_crypto_registry_snapshots,
    validate_short_crypto_registry_snapshot,
)


RAW_SCHEMA = "edge-lab-recorder.raw.v1"
RESULT_SCHEMA = "edge-lab-dynamic-short-crypto-recovery.v1"
STATE_SCHEMA = "edge-lab-dynamic-short-crypto-recovered-state.v2"
DECISION_SCHEMA = "edge-lab-dynamic-short-crypto-recovery-decision.v1"
SNAPSHOT_SCHEMA = "edge-lab-dynamic-short-crypto-recovery-snapshot.v2"
REPLAY_CHECKPOINT_SCHEMA = (
    "edge-lab-dynamic-short-crypto-replay-checkpoint.v1"
)
RECORD_INDEX_SCHEMA = (
    "edge-lab-dynamic-short-crypto-record-index.v2"
)
RECOVERY_ANCHOR_SCHEMA = (
    "edge-lab-dynamic-short-crypto-recovery-anchor.v1"
)
RUN_INVENTORY_SCHEMA = (
    "edge-lab-dynamic-short-crypto-run-inventory.v1"
)
INPUT_COMMITMENT_SCHEMA = (
    "edge-lab-dynamic-short-crypto-recovery-input-commitment.v2"
)
INPUT_SUMMARY_SCHEMA = (
    "edge-lab-dynamic-short-crypto-recovery-input-summary.v2"
)
ROOT_CONTENT_COMMITMENT_SCHEMA = (
    "edge-lab-dynamic-short-crypto-root-content-commitment.v1"
)
ROOT_INVENTORY_SCHEMA = (
    "edge-lab-dynamic-short-crypto-root-inventory.v1"
)
VALIDATOR_CONTRACT_SCHEMA = (
    "edge-lab-dynamic-short-crypto-recovery-validator-contract.v1"
)
_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_COHORT_SCHEMA = ROOT_COHORT_SCHEMA
_REMEDIATION_COHORT_SCHEMA = REMEDIATION_COHORT_SCHEMA


class RecoveryInputError(ValueError):
    """A prior-run artifact failed immutable recovery validation."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class RecoveryResult:
    """Deterministic recovered state and side-effect-free apply plan."""

    recovered_from_run_ids: tuple[str, ...]
    replayed_record_count: int
    state_hash: str
    gaps: tuple[dict[str, Any], ...]
    exclusions: tuple[dict[str, Any], ...]
    run_classifications: tuple[dict[str, Any], ...]
    state: dict[str, Any]
    recovery_decision: dict[str, Any]
    worker_actions: tuple[dict[str, Any], ...]
    callback_generation: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESULT_SCHEMA,
            "recovered_from_run_ids": list(
                self.recovered_from_run_ids
            ),
            "replayed_record_count": self.replayed_record_count,
            "state_hash": self.state_hash,
            "gaps": deepcopy(list(self.gaps)),
            "exclusions": deepcopy(list(self.exclusions)),
            "run_classifications": deepcopy(
                list(self.run_classifications)
            ),
            "state": deepcopy(self.state),
            "recovery_decision": deepcopy(self.recovery_decision),
            "worker_actions": deepcopy(list(self.worker_actions)),
            "callback_generation": self.callback_generation,
            "new_orders_disabled": True,
            "authenticated_endpoints_used": 0,
            "orders_submitted": 0,
        }

    def canonical_bytes(self) -> bytes:
        """Return stable bytes suitable for repeat-replay comparison."""

        return canonical_json_bytes(self.to_dict()) + b"\n"

    def allows_callback(self, generation: int) -> bool:
        """Reject callbacks from every pre-recovery worker generation."""

        return (
            not isinstance(generation, bool)
            and isinstance(generation, int)
            and generation == self.callback_generation
        )


@dataclass(frozen=True)
class CachedRecoveryOutcome:
    """Recovered semantics plus the opaque proof the caller must persist."""

    result: RecoveryResult
    anchor_payload: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "anchor_payload",
            deepcopy(self.anchor_payload),
        )

    def canonical_bytes(self) -> bytes:
        """Preserve the historical comparison seam for cached callers."""

        return self.result.canonical_bytes()

    def __getattr__(self, name: str) -> Any:
        """Delegate result reads while keeping the outcome explicit to service."""

        return getattr(self.result, name)


@dataclass(frozen=True)
class _VerifiedBatch:
    run_id: str
    raw_path: Path
    relative_path: str
    source: str
    manifest: dict[str, Any]
    rows: tuple[dict[str, Any], ...]
    canonical_replay_row_zlibs: tuple[bytes | None, ...] = ()


@dataclass(frozen=True)
class _AnchorControlRoot:
    """Descriptor-anchored path from one run root to its service raw store."""

    paths: tuple[Path, ...]
    descriptors: tuple[int, ...]
    identities: tuple[tuple[int, int, int], ...]

    @property
    def control_root(self) -> Path:
        return self.paths[-1]

    @property
    def service_descriptor(self) -> int:
        return self.descriptors[-1]

    def close(self) -> None:
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)


@dataclass(frozen=True)
class _RunRootProof:
    """Immutable identity for one normalized run root."""

    run_id: str
    path: Path
    identity: tuple[int, int, int]

    def close(self) -> None:
        """Identity proofs do not retain descriptors."""


@dataclass(frozen=True)
class _StableDescriptorRead:
    """Bytes bound to one immutable entry in a held directory."""

    content: bytes
    identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class _HeldArtifact:
    """One recovery-relevant entry discovered below a held run root."""

    relative_path: str
    path: Path
    parent_relative_path: str
    name: str
    identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class _HeldDirectory:
    """One identity-bound directory in a descriptor-rooted run tree."""

    relative_path: str
    identity: tuple[int, int, int, int, int, int]


@dataclass
class _HeldRunView:
    """Bounded-FD inventory used by replay and its commitments."""

    run_id: str
    run_root: Path
    root_proof: _RunRootProof
    root_descriptor: int
    directories: dict[str, _HeldDirectory]
    artifacts: dict[str, _HeldArtifact]
    member_names: dict[str, tuple[str, ...]]
    member_identities: dict[
        str, dict[str, tuple[int, int, int, int, int, int]]
    ]
    content_stats: dict[str, tuple[str, int, int]]

    def close(self) -> None:
        if self.root_descriptor < 0:
            return
        os.close(self.root_descriptor)
        self.root_descriptor = -1


_ACTIVE_HELD_RUN_VIEW: ContextVar[_HeldRunView | None] = ContextVar(
    "_ACTIVE_HELD_RUN_VIEW",
    default=None,
)


@dataclass
class _HeldRunViewLease:
    """Idempotently release one run-local view and its context binding."""

    view: _HeldRunView
    token: Any
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            _ACTIVE_HELD_RUN_VIEW.reset(self.token)
        finally:
            self.view.close()


@dataclass(frozen=True)
class _CheckpointAudit:
    closed: bool
    startup_integrity_findings: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _CheckpointArtifact:
    path: Path
    document: dict[str, Any]


@dataclass(frozen=True)
class _RecoveryReplayCheckpoint:
    """Validated reducer inputs that can be extended without prefix raw I/O."""

    run_ids: tuple[str, ...]
    replay_records: tuple[tuple[str, str, dict[str, Any]], ...]
    record_sources: dict[str, str]
    records_by_id: dict[str, dict[str, Any]]
    record_connections: dict[str, str]
    record_event_types: dict[str, str]
    validated_record_count: int
    classifications: tuple[dict[str, Any], ...]
    gaps: tuple[dict[str, Any], ...]
    exclusions: tuple[dict[str, Any], ...]
    runs_with_orphan_partials: tuple[str, ...]
    runs_with_input_integrity_gaps: tuple[str, ...]
    latest_legacy_registry_index: int | None
    latest_legacy_registry_key: (
        tuple[str, str, int, str, str] | None
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPLAY_CHECKPOINT_SCHEMA,
            "run_ids": list(self.run_ids),
            "replay_records": [
                {
                    "run_id": run_id,
                    "relative_path": relative_path,
                    "row": deepcopy(row),
                }
                for run_id, relative_path, row in self.replay_records
            ],
            "record_sources": deepcopy(self.record_sources),
            "records_by_id": deepcopy(self.records_by_id),
            "record_connections": deepcopy(self.record_connections),
            "record_event_types": deepcopy(self.record_event_types),
            "validated_record_count": self.validated_record_count,
            "classifications": deepcopy(list(self.classifications)),
            "gaps": deepcopy(list(self.gaps)),
            "exclusions": deepcopy(list(self.exclusions)),
            "runs_with_orphan_partials": list(
                self.runs_with_orphan_partials
            ),
            "runs_with_input_integrity_gaps": list(
                self.runs_with_input_integrity_gaps
            ),
            "latest_legacy_registry_index": (
                self.latest_legacy_registry_index
            ),
            "latest_legacy_registry_key": (
                None
                if self.latest_legacy_registry_key is None
                else list(self.latest_legacy_registry_key)
            ),
        }


def _checkpoint_row_record_id(row: Mapping[str, Any]) -> str:
    """Hash one decoded checkpoint row without reopening its finalized raw."""

    envelope = {
        key: row[key]
        for key in (
            "schema_version",
            "source",
            "received_at",
            "event_at",
            "sequence",
            "payload",
        )
    }
    return hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()


class _FinalizedRecordIndex:
    """Exact ID/source proof and compressed public-evidence lookup."""

    _FLUSH_RECORD_COUNT = 10_000

    def __init__(
        self,
        path: Path | None = None,
        *,
        prefix_paths: tuple[Path, ...] = (),
    ) -> None:
        self._path = path
        self._store_evidence = path is not None
        self._connection = sqlite3.connect(
            "" if path is None else path,
            check_same_thread=False,
        )
        self._connection.execute("PRAGMA journal_mode = OFF")
        self._connection.execute("PRAGMA synchronous = OFF")
        self._connection.execute("PRAGMA temp_store = FILE")
        self._connection.execute("PRAGMA cache_size = -2048")
        self._connection.execute("PRAGMA locking_mode = EXCLUSIVE")
        self._connection.execute(
            "CREATE TABLE finalized_record_sources ("
            "source_id INTEGER PRIMARY KEY, "
            "source TEXT NOT NULL UNIQUE"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE finalized_records ("
            "record_id BLOB PRIMARY KEY, "
            "source_id INTEGER NOT NULL, "
            "evidence_zlib BLOB"
            ") WITHOUT ROWID"
        )
        self._connection.execute(
            "CREATE TABLE replay_records ("
            "record_id BLOB PRIMARY KEY, "
            "run_id TEXT NOT NULL, "
            "relative_path TEXT NOT NULL, "
            "row_zlib BLOB NOT NULL"
            ") WITHOUT ROWID"
        )
        self._connection.execute(
            "CREATE TABLE recovery_run_inventory ("
            "run_id TEXT PRIMARY KEY, "
            "inventory_zlib BLOB NOT NULL"
            ") WITHOUT ROWID"
        )
        self._connection.commit()
        self._prefix_connections = tuple(
            sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro&immutable=1",
                uri=True,
                check_same_thread=False,
            )
            for path in prefix_paths
        )
        self._source_ids: dict[str, int] = {}
        self._pending: list[tuple[bytes, str, bytes | None]] = []
        self._pending_replay: list[tuple[bytes, str, str, bytes]] = []
        self._evidence_cache: dict[str, dict[str, Any] | None] = {}
        self._closed = False
        self._row_count = 0

    @staticmethod
    def _prefix_row(
        connection: sqlite3.Connection,
        record_id: bytes,
    ) -> tuple[str, bytes | None] | None:
        row = connection.execute(
            "SELECT sources.source, records.evidence_zlib "
            "FROM finalized_records AS records "
            "JOIN finalized_record_sources AS sources "
            "ON sources.source_id = records.source_id "
            "WHERE records.record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), row[1]

    def _existing_prefix_row(
        self,
        record_id: bytes,
    ) -> tuple[str, bytes | None] | None:
        for connection in self._prefix_connections:
            row = self._prefix_row(connection, record_id)
            if row is not None:
                return row
        return None

    def add(
        self,
        record_id: str,
        *,
        source: str,
        row: Mapping[str, Any],
        replay_location: tuple[str, str] | None = None,
        canonical_replay_row_zlib: bytes | None = None,
    ) -> None:
        binary_id = bytes.fromhex(record_id)
        if self._existing_prefix_row(binary_id) is not None:
            raise RecoveryInputError(
                "duplicate_record_id",
                f"canonical record ID occurs more than once: {record_id}",
            )
        evidence_zlib = (
            zlib.compress(canonical_json_bytes(row), level=1)
            if (
                self._store_evidence
                and source in {"gamma_http", "rtds_ws"}
            )
            else None
        )
        self._pending.append((binary_id, source, evidence_zlib))
        if replay_location is not None:
            replay_run_id, replay_relative_path = replay_location
            self._pending_replay.append(
                (
                    binary_id,
                    replay_run_id,
                    replay_relative_path,
                    (
                        canonical_replay_row_zlib
                        if canonical_replay_row_zlib is not None
                        else zlib.compress(
                            canonical_json_bytes(row),
                            level=1,
                        )
                    ),
                )
            )
        if len(self._pending) >= self._FLUSH_RECORD_COUNT:
            self.flush()

    def _duplicate_in_pending(self) -> bytes:
        seen: set[bytes] = set()
        for record_id, _, _ in self._pending:
            if record_id in seen:
                return record_id
            seen.add(record_id)
            row = self._connection.execute(
                "SELECT 1 FROM finalized_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            if (
                row is not None
                or self._existing_prefix_row(record_id) is not None
            ):
                return record_id
        return self._pending[0][0]

    def _source_id(self, source: str) -> int:
        cached = self._source_ids.get(source)
        if cached is not None:
            return cached
        self._connection.execute(
            "INSERT OR IGNORE INTO finalized_record_sources(source) "
            "VALUES (?)",
            (source,),
        )
        row = self._connection.execute(
            "SELECT source_id FROM finalized_record_sources "
            "WHERE source = ?",
            (source,),
        ).fetchone()
        if row is None:
            raise RecoveryInputError(
                "recovery_record_index_invalid",
                f"record source could not be indexed: {source}",
            )
        source_id = int(row[0])
        self._source_ids[source] = source_id
        return source_id

    def flush(self) -> None:
        if not self._pending:
            return
        try:
            source_ids = {
                source: self._source_id(source)
                for _, source, _ in self._pending
            }
            self._connection.executemany(
                "INSERT INTO finalized_records("
                "record_id, source_id, evidence_zlib"
                ") VALUES (?, ?, ?)",
                (
                    (
                        record_id,
                        source_ids[source],
                        evidence_zlib,
                    )
                    for record_id, source, evidence_zlib
                    in self._pending
                ),
            )
            self._connection.executemany(
                "INSERT INTO replay_records("
                "record_id, run_id, relative_path, row_zlib"
                ") VALUES (?, ?, ?, ?)",
                (
                    (
                        record_id,
                        replay_run_id,
                        replay_relative_path,
                        replay_zlib,
                    )
                    for (
                        record_id,
                        replay_run_id,
                        replay_relative_path,
                        replay_zlib,
                    ) in self._pending_replay
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as exc:
            self._connection.rollback()
            duplicate = self._duplicate_in_pending().hex()
            raise RecoveryInputError(
                "duplicate_record_id",
                f"canonical record ID occurs more than once: {duplicate}",
            ) from exc
        finally:
            self._pending.clear()
            self._pending_replay.clear()

    def source_for(self, record_id: str) -> str | None:
        self.flush()
        binary_id = bytes.fromhex(record_id)
        row = self._connection.execute(
            "SELECT sources.source "
            "FROM finalized_records AS records "
            "JOIN finalized_record_sources AS sources "
            "ON sources.source_id = records.source_id "
            "WHERE records.record_id = ?",
            (binary_id,),
        ).fetchone()
        if row is not None:
            return str(row[0])
        prefix = self._existing_prefix_row(binary_id)
        return None if prefix is None else prefix[0]

    def evidence_row(self, record_id: str) -> dict[str, Any] | None:
        if record_id in self._evidence_cache:
            cached = self._evidence_cache[record_id]
            return None if cached is None else deepcopy(cached)
        self.flush()
        binary_id = bytes.fromhex(record_id)
        row = self._connection.execute(
            "SELECT sources.source, records.evidence_zlib "
            "FROM finalized_records AS records "
            "JOIN finalized_record_sources AS sources "
            "ON sources.source_id = records.source_id "
            "WHERE records.record_id = ?",
            (binary_id,),
        ).fetchone()
        indexed_source = None if row is None else str(row[0])
        evidence_zlib = None if row is None else row[1]
        if row is None:
            prefix = self._existing_prefix_row(binary_id)
            if prefix is not None:
                indexed_source, evidence_zlib = prefix
        if evidence_zlib is None:
            self._evidence_cache[record_id] = None
            return None
        try:
            value: Any = json.loads(zlib.decompress(evidence_zlib))
            canonical_record_id = canonical_record_id_from_json(value)
        except (
            TypeError,
            ValueError,
            zlib.error,
            json.JSONDecodeError,
        ) as exc:
            raise RecoveryInputError(
                "recovery_record_index_invalid",
                f"evidence row is corrupt: {record_id}",
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != RAW_SCHEMA
            or value.get("record_id") != record_id
            or canonical_record_id != record_id
            or value.get("source") != indexed_source
        ):
            raise RecoveryInputError(
                "recovery_record_index_invalid",
                f"evidence row identity changed: {record_id}",
            )
        self._evidence_cache[record_id] = deepcopy(value)
        return deepcopy(value)

    def replay_rows(
        self,
    ) -> tuple[tuple[str, str, dict[str, Any]], ...]:
        """Return the exact replay-eligible rows committed by every shard."""

        self.flush()
        rows: list[tuple[str, str, dict[str, Any]]] = []
        seen: set[str] = set()
        for connection in (
            *self._prefix_connections,
            self._connection,
        ):
            for (
                binary_record_id,
                run_id,
                relative_path,
                indexed_source,
                row_zlib,
            ) in connection.execute(
                "SELECT replay.record_id, replay.run_id, "
                "replay.relative_path, sources.source, replay.row_zlib "
                "FROM replay_records AS replay "
                "JOIN finalized_records AS records "
                "ON records.record_id = replay.record_id "
                "JOIN finalized_record_sources AS sources "
                "ON sources.source_id = records.source_id "
                "ORDER BY replay.record_id"
            ):
                record_id = bytes(binary_record_id).hex()
                try:
                    value: Any = json.loads(zlib.decompress(row_zlib))
                    exact_record_id = _checkpoint_row_record_id(value)
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    zlib.error,
                    json.JSONDecodeError,
                ) as exc:
                    raise RecoveryInputError(
                        "recovery_record_index_invalid",
                        f"replay row is corrupt: {record_id}",
                    ) from exc
                relative = Path(str(relative_path))
                if (
                    record_id in seen
                    or not isinstance(value, dict)
                    or value.get("schema_version") != RAW_SCHEMA
                    or value.get("record_id") != record_id
                    or exact_record_id != record_id
                    or value.get("source") != str(indexed_source)
                    or not isinstance(run_id, str)
                    or _RUN_ID.fullmatch(run_id) is None
                    or not isinstance(relative_path, str)
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or relative.suffix != ".jsonl"
                ):
                    raise RecoveryInputError(
                        "recovery_record_index_invalid",
                        (
                            "replay row identity or location changed: "
                            f"{record_id}"
                        ),
                    )
                seen.add(record_id)
                rows.append((run_id, relative_path, value))
        return tuple(rows)

    def add_run_inventory(
        self,
        run_id: str,
        inventory: Mapping[str, Any],
    ) -> None:
        if (
            _RUN_ID.fullmatch(run_id) is None
            or inventory.get("schema_version") != RUN_INVENTORY_SCHEMA
            or inventory.get("run_id") != run_id
        ):
            raise RecoveryInputError(
                "recovery_run_inventory_invalid",
                f"run inventory identity is invalid: {run_id}",
            )
        try:
            self._connection.execute(
                "INSERT INTO recovery_run_inventory("
                "run_id, inventory_zlib"
                ") VALUES (?, ?)",
                (
                    run_id,
                    zlib.compress(
                        canonical_json_bytes(dict(inventory)),
                        level=1,
                    ),
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as exc:
            self._connection.rollback()
            raise RecoveryInputError(
                "recovery_run_inventory_invalid",
                f"duplicate run inventory: {run_id}",
            ) from exc

    def run_inventories(self) -> tuple[dict[str, Any], ...]:
        self.flush()
        inventories: list[dict[str, Any]] = []
        seen: set[str] = set()
        for connection in (
            *self._prefix_connections,
            self._connection,
        ):
            for run_id, inventory_zlib in connection.execute(
                "SELECT run_id, inventory_zlib "
                "FROM recovery_run_inventory ORDER BY run_id"
            ):
                try:
                    value: Any = json.loads(
                        zlib.decompress(inventory_zlib)
                    )
                except (
                    TypeError,
                    ValueError,
                    zlib.error,
                    json.JSONDecodeError,
                ) as exc:
                    raise RecoveryInputError(
                        "recovery_record_index_invalid",
                        f"run inventory is corrupt: {run_id}",
                    ) from exc
                if (
                    not isinstance(run_id, str)
                    or run_id in seen
                    or not isinstance(value, dict)
                    or value.get("schema_version")
                    != RUN_INVENTORY_SCHEMA
                    or value.get("run_id") != run_id
                ):
                    raise RecoveryInputError(
                        "recovery_record_index_invalid",
                        f"run inventory identity changed: {run_id}",
                    )
                seen.add(run_id)
                inventories.append(value)
        return tuple(inventories)

    @property
    def row_count(self) -> int:
        if self._closed:
            return self._row_count
        self.flush()
        row = self._connection.execute(
            "SELECT COUNT(*) FROM finalized_records"
        ).fetchone()
        return 0 if row is None else int(row[0])

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
            self._row_count = self.row_count
        finally:
            self._connection.close()
            for connection in self._prefix_connections:
                connection.close()
            self._closed = True

    def __del__(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None and not getattr(self, "_closed", True):
            try:
                connection.close()
            except sqlite3.Error:
                pass


class _RecoveryCpuLimiter:
    """Bound sustained recovery CPU without changing validation semantics."""

    def __init__(self, cpu_ratio: float | None) -> None:
        if cpu_ratio is not None and (
            isinstance(cpu_ratio, bool)
            or not isinstance(cpu_ratio, (int, float))
            or not 0 < float(cpu_ratio) <= 1
        ):
            raise ValueError("cpu_ratio must be None or in the interval (0, 1]")
        self._ratio = (
            None if cpu_ratio is None else float(cpu_ratio)
        )
        self._started_cpu = time.process_time()
        self._started_wall = time.monotonic()

    def checkpoint(self) -> None:
        ratio = self._ratio
        if ratio is None or ratio >= 1:
            return
        cpu_elapsed = time.process_time() - self._started_cpu
        wall_elapsed = time.monotonic() - self._started_wall
        delay = (cpu_elapsed / ratio) - wall_elapsed
        if delay > 0:
            time.sleep(delay)


def _stable_read(path: Path, *, code: str) -> bytes:
    if path.is_symlink():
        raise RecoveryInputError(code, f"symlink is not immutable input: {path}")
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            content = handle.read()
            after = os.fstat(handle.fileno())
        current = path.stat()
    except OSError as exc:
        raise RecoveryInputError(code, f"cannot read artifact: {path}") from exc
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (before, after, current)
    }
    if len(identities) != 1 or len(content) != after.st_size:
        raise RecoveryInputError(
            code,
            f"artifact changed while recovery read it: {path}",
        )
    return content


def _stable_read_at(
    directory_descriptor: int,
    name: str,
    *,
    code: str,
) -> _StableDescriptorRead:
    """Read one regular file relative to a held directory descriptor."""

    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
    ):
        raise RecoveryInputError(code, f"unsafe descriptor-relative name: {name}")
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    if (
        not isinstance(no_follow, int)
        or no_follow == 0
        or not isinstance(nonblocking, int)
        or nonblocking == 0
    ):
        raise RecoveryInputError(
            code,
            "descriptor-relative safe reads are unavailable",
        )
    flags = (
        os.O_RDONLY
        | no_follow
        | nonblocking
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor: int | None = None
    try:
        entry_before = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(entry_before.st_mode):
            raise RecoveryInputError(
                code,
                f"descriptor-relative input is not a regular file: {name}",
            )
        descriptor = os.open(
            name,
            flags,
            dir_fd=directory_descriptor,
        )
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            raise RecoveryInputError(
                code,
                f"descriptor-relative input is not a regular file: {name}",
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 8 * 1024 * 1024):
            chunks.append(chunk)
        opened_after = os.fstat(descriptor)
        entry_after = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except RecoveryInputError:
        raise
    except OSError as exc:
        raise RecoveryInputError(
            code,
            f"cannot read descriptor-relative artifact: {name}",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    content = b"".join(chunks)
    identities = {
        (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        for item in (
            entry_before,
            opened_before,
            opened_after,
            entry_after,
        )
    }
    if (
        len(identities) != 1
        or not stat.S_ISREG(entry_after.st_mode)
        or len(content) != opened_after.st_size
    ):
        raise RecoveryInputError(
            code,
            f"descriptor-relative artifact changed while read: {name}",
        )
    return _StableDescriptorRead(
        content=content,
        identity=next(iter(identities)),
    )


def _revalidate_stable_entry_at(
    directory_descriptor: int,
    name: str,
    *,
    identity: tuple[int, int, int, int, int, int],
    code: str,
) -> None:
    """Rebind a consumed pair to the same held-directory entries."""

    try:
        current = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise RecoveryInputError(
            code,
            f"descriptor-relative artifact changed after read: {name}",
        ) from exc
    current_identity = (
        current.st_dev,
        current.st_ino,
        current.st_mode,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )
    if (
        not stat.S_ISREG(current.st_mode)
        or current_identity != identity
    ):
        raise RecoveryInputError(
            code,
            f"descriptor-relative artifact changed after read: {name}",
        )


def _stable_digest(
    path: Path,
    *,
    code: str,
    count_lines: bool,
) -> tuple[str, int, int | None]:
    """Hash one stable regular file without hydrating it into Python memory."""

    if path.is_symlink():
        raise RecoveryInputError(code, f"symlink is not immutable input: {path}")
    digest = hashlib.sha256()
    byte_count = 0
    line_count = 0
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            while chunk := handle.read(8 * 1024 * 1024):
                digest.update(chunk)
                byte_count += len(chunk)
                if count_lines:
                    line_count += chunk.count(b"\n")
            after = os.fstat(handle.fileno())
        current = path.stat()
    except OSError as exc:
        raise RecoveryInputError(code, f"cannot read artifact: {path}") from exc
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (before, after, current)
    }
    if len(identities) != 1 or byte_count != after.st_size:
        raise RecoveryInputError(
            code,
            f"artifact changed while recovery hashed it: {path}",
        )
    return (
        digest.hexdigest(),
        byte_count,
        line_count if count_lines else None,
    )


def _json_object(content: bytes, *, path: Path, code: str) -> dict[str, Any]:
    try:
        value: Any = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryInputError(
            code,
            f"artifact is not valid UTF-8 JSON: {path}",
        ) from exc
    if not isinstance(value, dict):
        raise RecoveryInputError(code, f"artifact must be a JSON object: {path}")
    return value


def _capture_store_root(raw_path: Path) -> Path:
    for parent in raw_path.parents:
        if parent.name == "raw":
            return parent.parent
    raise RecoveryInputError(
        "finalized_manifest_invalid",
        f"finalized raw path is outside a capture-store raw tree: {raw_path}",
    )


def _schema_shape(value: Any) -> Any:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return {
            "object": {
                key: _schema_shape(value[key])
                for key in sorted(value)
            }
        }
    if isinstance(value, list):
        unique: dict[bytes, Any] = {}
        for item in value:
            shape = _schema_shape(item)
            unique[canonical_json_bytes(shape)] = shape
        return {"array": [unique[key] for key in sorted(unique)]}
    raise RecoveryInputError(
        "record_schema_invalid",
        f"unsupported JSON schema value: {type(value).__name__}",
    )


def _payload_schema(value: Mapping[str, Any]) -> tuple[str, Any]:
    descriptor = _schema_shape(value)
    fingerprint = hashlib.sha256(
        canonical_json_bytes(descriptor)
    ).hexdigest()
    return fingerprint, descriptor


def _serialized_schema_descriptor(descriptor: Any) -> Any:
    """Return the schema shape observable after canonical JSON encoding.

    CaptureStore preserves Decimal scale by serializing Decimal values as
    strings, while retaining ``decimal-string`` in the immutable manifest
    descriptor.  Recovery therefore cannot reconstruct that distinction from
    JSONL bytes alone.  Normalize only that lossy boundary and keep every
    other structural distinction exact.
    """

    if descriptor == "decimal-string":
        return "string"
    if isinstance(descriptor, str) and descriptor in {
        "null",
        "boolean",
        "integer",
        "float",
        "string",
    }:
        return descriptor
    if isinstance(descriptor, Mapping) and set(descriptor) == {"object"}:
        fields = descriptor.get("object")
        if not isinstance(fields, Mapping) or any(
            not isinstance(key, str) for key in fields
        ):
            raise RecoveryInputError(
                "finalized_schema_mismatch",
                "manifest contains an invalid object schema descriptor",
            )
        return {
            "object": {
                key: _serialized_schema_descriptor(fields[key])
                for key in sorted(fields)
            }
        }
    if isinstance(descriptor, Mapping) and set(descriptor) == {"array"}:
        alternatives = descriptor.get("array")
        if not isinstance(alternatives, list):
            raise RecoveryInputError(
                "finalized_schema_mismatch",
                "manifest contains an invalid array schema descriptor",
            )
        unique: dict[bytes, Any] = {}
        for alternative in alternatives:
            normalized = _serialized_schema_descriptor(alternative)
            unique[canonical_json_bytes(normalized)] = normalized
        return {"array": [unique[key] for key in sorted(unique)]}
    raise RecoveryInputError(
        "finalized_schema_mismatch",
        "manifest contains an unsupported schema descriptor",
    )


_SCHEMA_NULL = 0
_SCHEMA_BOOLEAN = 1
_SCHEMA_INTEGER = 2
_SCHEMA_FLOAT = 3
_SCHEMA_STRING = 4
_SCHEMA_OBJECT = 5
_SCHEMA_ARRAY = 6
_CompiledSchema = tuple[int, Any]


def _compile_serialized_schema(descriptor: Any) -> _CompiledSchema:
    """Compile a validated descriptor once for allocation-light row checks."""

    primitive_tags = {
        "null": _SCHEMA_NULL,
        "boolean": _SCHEMA_BOOLEAN,
        "integer": _SCHEMA_INTEGER,
        "float": _SCHEMA_FLOAT,
        "string": _SCHEMA_STRING,
    }
    if isinstance(descriptor, str) and descriptor in primitive_tags:
        return (primitive_tags[descriptor], None)
    if isinstance(descriptor, Mapping) and set(descriptor) == {"object"}:
        fields = descriptor["object"]
        if not isinstance(fields, Mapping):
            raise RecoveryInputError(
                "finalized_schema_mismatch",
                "manifest contains an invalid object schema descriptor",
            )
        compiled_fields = tuple(
            (key, _compile_serialized_schema(fields[key]))
            for key in fields
        )
        primitive_keys: list[list[str]] = [
            [] for _ in range(_SCHEMA_STRING + 1)
        ]
        nested_fields: list[tuple[str, _CompiledSchema]] = []
        for key, compiled in compiled_fields:
            if compiled[0] <= _SCHEMA_STRING:
                primitive_keys[compiled[0]].append(key)
            else:
                nested_fields.append((key, compiled))
        return (
            _SCHEMA_OBJECT,
            (
                frozenset(fields),
                tuple(tuple(keys) for keys in primitive_keys),
                tuple(nested_fields),
            ),
        )
    if isinstance(descriptor, Mapping) and set(descriptor) == {"array"}:
        alternatives = descriptor["array"]
        if not isinstance(alternatives, list):
            raise RecoveryInputError(
                "finalized_schema_mismatch",
                "manifest contains an invalid array schema descriptor",
            )
        return (
            _SCHEMA_ARRAY,
            tuple(
                _compile_serialized_schema(alternative)
                for alternative in alternatives
            ),
        )
    raise RecoveryInputError(
        "finalized_schema_mismatch",
        "manifest contains an unsupported schema descriptor",
    )


def _matches_compiled_object(value: Any, detail: Any) -> bool:
    if not isinstance(value, dict):
        return False
    expected_keys, primitive_keys, nested_fields = detail
    if len(value) != len(expected_keys) or value.keys() != expected_keys:
        return False
    null_keys, boolean_keys, integer_keys, float_keys, string_keys = (
        primitive_keys
    )
    for key in null_keys:
        if value[key] is not None:
            return False
    for key in boolean_keys:
        if not isinstance(value[key], bool):
            return False
    for key in integer_keys:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int):
            return False
    for key in float_keys:
        if not isinstance(value[key], float):
            return False
    for key in string_keys:
        if not isinstance(value[key], str):
            return False
    for key, descriptor in nested_fields:
        if not _matches_compiled_schema(value[key], descriptor):
            return False
    return True


def _matches_compiled_schema(
    value: Any,
    descriptor: _CompiledSchema,
) -> bool:
    """Match one decoded value through a precompiled structural plan."""

    tag, detail = descriptor
    if tag == _SCHEMA_NULL:
        return value is None
    if tag == _SCHEMA_BOOLEAN:
        return isinstance(value, bool)
    if tag == _SCHEMA_INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if tag == _SCHEMA_FLOAT:
        return isinstance(value, float)
    if tag == _SCHEMA_STRING:
        return isinstance(value, str)
    if tag == _SCHEMA_OBJECT:
        return _matches_compiled_object(value, detail)
    if tag != _SCHEMA_ARRAY or not isinstance(value, list):
        return False
    alternatives: tuple[_CompiledSchema, ...] = detail
    if not alternatives:
        return not value
    if not value:
        return False
    if len(alternatives) == 1:
        alternative = alternatives[0]
        if alternative[0] == _SCHEMA_OBJECT:
            object_detail = alternative[1]
            for item in value:
                if not _matches_compiled_object(item, object_detail):
                    return False
            return True
        for item in value:
            if not _matches_compiled_schema(item, alternative):
                return False
        return True
    matched = [False] * len(alternatives)
    for item in value:
        for index, alternative in enumerate(alternatives):
            if _matches_compiled_schema(item, alternative):
                matched[index] = True
                break
        else:
            return False
    return all(matched)


def _validate_row(
    row: Any,
    *,
    raw_path: Path,
    line_number: int,
    source: str,
    schema_descriptors: Mapping[str, _CompiledSchema],
) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise RecoveryInputError(
            "record_schema_invalid",
            f"{raw_path}:{line_number} must contain a JSON object",
        )
    if (
        row.get("schema_version") != RAW_SCHEMA
        or row.get("source") != source
        or not isinstance(row.get("payload"), dict)
    ):
        raise RecoveryInputError(
            "record_schema_invalid",
            f"{raw_path}:{line_number} has an incompatible raw envelope",
        )
    record_id = row.get("record_id")
    if (
        not isinstance(record_id, str)
        or _CONTENT_HASH.fullmatch(record_id) is None
    ):
        raise RecoveryInputError(
            "canonical_record_id_invalid",
            f"{raw_path}:{line_number} has no canonical record ID",
        )
    try:
        expected = canonical_record_id_from_json(row)
    except (TypeError, ValueError) as exc:
        raise RecoveryInputError(
            "record_schema_invalid",
            f"{raw_path}:{line_number} cannot be canonicalized",
        ) from exc
    if expected != record_id:
        raise RecoveryInputError(
            "canonical_record_id_mismatch",
            f"{raw_path}:{line_number} record ID does not match its envelope",
        )
    schema_fingerprint = row.get("schema_fingerprint")
    if (
        not isinstance(schema_fingerprint, str)
        or _CONTENT_HASH.fullmatch(schema_fingerprint) is None
        or schema_fingerprint not in schema_descriptors
    ):
        raise RecoveryInputError(
            "finalized_schema_mismatch",
            f"{raw_path}:{line_number} references an undeclared schema",
        )
    declared_descriptor = schema_descriptors[schema_fingerprint]
    if not _matches_compiled_schema(row["payload"], declared_descriptor):
        raise RecoveryInputError(
            "finalized_schema_mismatch",
            f"{raw_path}:{line_number} payload schema fingerprint drifted",
        )
    return row


def _is_legacy_registry_row(row: Mapping[str, Any]) -> bool:
    event = row.get("payload")
    payload = event.get("payload") if isinstance(event, Mapping) else None
    return (
        isinstance(event, Mapping)
        and event.get("schema_version")
        == "edge-lab-dynamic-short-crypto-service.record.v1"
        and event.get("source") == "dynamic_short_crypto_service"
        and event.get("event_type") == "registry_revision"
        and isinstance(payload, Mapping)
        and payload.get("schema_version")
        in {
            "edge-lab.short-crypto-registry.v1",
            "edge-lab-short-crypto-registry.v1",
        }
    )


def _compact_legacy_registry_row(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    """Discard one already-validated repeated cumulative registry body."""

    event = row["payload"]
    payload = event["payload"]
    target_values = payload.get("targets")
    target_slugs = (
        sorted(
            {
                item["slug"]
                for item in target_values
                if isinstance(item, Mapping)
                and isinstance(item.get("slug"), str)
            }
        )
        if isinstance(target_values, list)
        else []
    )
    return {
        "schema_version": row["schema_version"],
        "source": row["source"],
        "received_at": row["received_at"],
        "event_at": row.get("event_at"),
        "sequence": row.get("sequence"),
        "record_id": row["record_id"],
        "payload": {
            "schema_version": event["schema_version"],
            "source": event["source"],
            "event_type": event["event_type"],
            "received_at": event.get("received_at"),
            "monotonic_ns": event.get("monotonic_ns", 0),
            "payload": {
                "schema_version": (
                    "edge-lab-short-crypto-registry-"
                    "recovery-compacted.v1"
                ),
                "snapshot_sha256": payload.get("snapshot_sha256"),
                "target_slugs": target_slugs,
            },
        },
        "_recovery_compacted_after_validation": True,
    }


def _replay_sort_key(
    item: tuple[str, str, dict[str, Any]],
) -> tuple[str, str, int, str, str]:
    run_id, relative_path, row = item
    event = row.get("payload")
    monotonic_ns = (
        event.get("monotonic_ns", 0)
        if isinstance(event, Mapping)
        else 0
    )
    return (
        run_id,
        str(row.get("received_at")),
        int(monotonic_ns),
        str(row["record_id"]),
        relative_path,
    )


def _canonical_checkpoint_replay_rows(
    rows: Iterable[tuple[str, str, dict[str, Any]]],
) -> tuple[tuple[str, str, dict[str, Any]], ...]:
    """Apply the deterministic legacy compaction used by replay checkpoints."""

    ordered = sorted(
        (
            (run_id, relative_path, deepcopy(row))
            for run_id, relative_path, row in rows
        ),
        key=_replay_sort_key,
    )
    legacy_keys = [
        _replay_sort_key(item)
        for item in ordered
        if _is_legacy_registry_row(item[2])
    ]
    latest_legacy_key = max(legacy_keys) if legacy_keys else None
    return tuple(
        (
            run_id,
            relative_path,
            (
                row
                if (
                    latest_legacy_key is not None
                    and _replay_sort_key((run_id, relative_path, row))
                    == latest_legacy_key
                )
                else _compact_legacy_registry_row(row)
            ),
        )
        if _is_legacy_registry_row(row)
        else (run_id, relative_path, row)
        for run_id, relative_path, row in ordered
    )


def _checkpoint_reference_ids(
    replay_rows: Iterable[tuple[str, str, Mapping[str, Any]]],
) -> tuple[set[str], set[str]]:
    required_evidence_ids: set[str] = set()
    required_source_ids: set[str] = set()
    for _, _, row in replay_rows:
        if row.get("source") != "dynamic_short_crypto_service":
            continue
        event = row.get("payload")
        event_payload = (
            event.get("payload")
            if isinstance(event, Mapping)
            else None
        )
        if not isinstance(event_payload, Mapping):
            continue
        event_type = event.get("event_type")
        if event_type == "gamma_target_verified":
            evidence_record_id = event_payload.get("gamma_record_id")
        elif event_type == "chainlink_boundary_evidence_committed":
            evidence_record_id = event_payload.get("evidence_record_id")
        elif event_type == "settlement_reconciled":
            gamma_record_ids = event_payload.get("gamma_record_ids")
            if isinstance(gamma_record_ids, list):
                required_source_ids.update(
                    record_id
                    for record_id in gamma_record_ids
                    if (
                        isinstance(record_id, str)
                        and _CONTENT_HASH.fullmatch(record_id) is not None
                    )
                )
            close_liveness = event_payload.get("close_liveness")
            if isinstance(close_liveness, Mapping):
                required_source_ids.update(
                    record_id
                    for record_id in (
                        close_liveness.get("before_record_id"),
                        close_liveness.get("after_record_id"),
                    )
                    if (
                        isinstance(record_id, str)
                        and _CONTENT_HASH.fullmatch(record_id) is not None
                    )
                )
            continue
        else:
            continue
        if (
            isinstance(evidence_record_id, str)
            and _CONTENT_HASH.fullmatch(evidence_record_id) is not None
        ):
            required_evidence_ids.add(evidence_record_id)
            required_source_ids.add(evidence_record_id)
    return required_evidence_ids, required_source_ids


def _validate_finalized_pair(
    run_root: Path,
    run_id: str,
    raw_path: Path,
) -> _VerifiedBatch:
    manifest_path = raw_path.with_suffix(".manifest.json")
    view = _ACTIVE_HELD_RUN_VIEW.get()
    if view is not None and view.run_root == run_root:
        try:
            raw_relative = raw_path.relative_to(run_root).as_posix()
            manifest_relative = manifest_path.relative_to(
                run_root
            ).as_posix()
        except ValueError as exc:
            raise RecoveryInputError(
                "finalized_raw_unreadable",
                f"descriptor-held raw escapes run root: {raw_path}",
            ) from exc
        raw_artifact = view.artifacts.get(raw_relative)
        manifest_artifact = view.artifacts.get(manifest_relative)
        if (
            raw_artifact is None
            or manifest_artifact is None
            or not stat.S_ISREG(raw_artifact.identity[2])
            or not stat.S_ISREG(manifest_artifact.identity[2])
        ):
            raise RecoveryInputError(
                "finalized_raw_unreadable",
                f"descriptor-held finalized pair is not regular: {raw_path}",
            )
        raw = _stable_read_held_artifact(
            view,
            raw_artifact,
            code="finalized_raw_unreadable",
        )
        manifest_bytes = _stable_read_held_artifact(
            view,
            manifest_artifact,
            code="finalized_manifest_unreadable",
        )
    else:
        raw = _stable_read(raw_path, code="finalized_raw_unreadable")
        manifest_bytes = _stable_read(
            manifest_path,
            code="finalized_manifest_unreadable",
        )
    return _validate_finalized_pair_bytes(
        run_root,
        run_id,
        raw_path,
        raw=raw,
        manifest_bytes=manifest_bytes,
    )


def _validate_finalized_pair_bytes(
    run_root: Path,
    run_id: str,
    raw_path: Path,
    *,
    raw: bytes,
    manifest_bytes: bytes,
) -> _VerifiedBatch:
    """Validate already-stabilized pair bytes through the shared proof core."""

    manifest_path = raw_path.with_suffix(".manifest.json")
    manifest = _json_object(
        manifest_bytes,
        path=manifest_path,
        code="finalized_manifest_invalid",
    )
    store_root = _capture_store_root(raw_path)
    try:
        relative_to_store = raw_path.relative_to(store_root).as_posix()
        relative_to_run = raw_path.relative_to(run_root).as_posix()
    except ValueError as exc:
        raise RecoveryInputError(
            "finalized_manifest_invalid",
            f"raw input escapes the prior run root: {raw_path}",
        ) from exc
    checksum = manifest.get("checksum")
    if (
        manifest.get("manifest_version") != "capture-manifest.v1"
        or manifest.get("schema_version") != RAW_SCHEMA
        or manifest.get("source") != raw_path.parent.name
        or manifest.get("batch_id") != raw_path.stem
        or manifest.get("raw_path") != relative_to_store
        or not isinstance(checksum, dict)
        or checksum.get("algorithm") != "sha256"
    ):
        raise RecoveryInputError(
            "finalized_manifest_invalid",
            f"manifest identity/schema does not match raw batch: {raw_path}",
        )
    if raw and not raw.endswith(b"\n"):
        raise RecoveryInputError(
            "finalized_checksum_mismatch",
            f"finalized JSONL has no terminal newline: {raw_path}",
        )
    actual_sha = hashlib.sha256(raw).hexdigest()
    line_count = raw.count(b"\n")
    if (
        checksum.get("value") != actual_sha
        or checksum.get("bytes") != len(raw)
        or checksum.get("lines") != line_count
        or manifest.get("record_count") != line_count
    ):
        raise RecoveryInputError(
            "finalized_checksum_mismatch",
            f"bytes/lines/SHA-256 do not match manifest: {raw_path}",
        )
    manifest_schema_stats = manifest.get("schema_fingerprints")
    if not isinstance(manifest_schema_stats, list):
        raise RecoveryInputError(
            "finalized_schema_mismatch",
            f"manifest has no schema fingerprint statistics: {raw_path}",
        )
    actual_stats: dict[str, dict[str, Any]] = {}
    schema_descriptors: dict[str, _CompiledSchema] = {}
    for item in manifest_schema_stats:
        if not isinstance(item, dict):
            raise RecoveryInputError(
                "finalized_schema_mismatch",
                f"manifest has an invalid schema entry: {raw_path}",
            )
        fingerprint = item.get("fingerprint")
        count = item.get("count")
        descriptor = item.get("descriptor")
        if (
            not isinstance(fingerprint, str)
            or _CONTENT_HASH.fullmatch(fingerprint) is None
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or fingerprint in actual_stats
        ):
            raise RecoveryInputError(
                "finalized_schema_mismatch",
                f"manifest has an invalid schema entry: {raw_path}",
            )
        try:
            descriptor_fingerprint = hashlib.sha256(
                canonical_json_bytes(descriptor)
            ).hexdigest()
            serialized_descriptor = _serialized_schema_descriptor(
                descriptor
            )
            compiled_descriptor = _compile_serialized_schema(
                serialized_descriptor
            )
        except (TypeError, ValueError) as exc:
            raise RecoveryInputError(
                "finalized_schema_mismatch",
                f"manifest has an invalid schema descriptor: {raw_path}",
            ) from exc
        if descriptor_fingerprint != fingerprint:
            raise RecoveryInputError(
                "finalized_schema_mismatch",
                f"manifest schema descriptor hash drifted: {raw_path}",
            )
        actual_stats[fingerprint] = item
        schema_descriptors[fingerprint] = compiled_descriptor
    rows: list[dict[str, Any]] = []
    canonical_replay_row_zlibs: list[bytes | None] = []
    expected_schema_stats: dict[str, dict[str, Any]] = {}
    ordered_fingerprints: list[str] = []
    latest_legacy_registry_index: int | None = None
    latest_legacy_registry_raw: bytes | None = None
    for line_number, line in enumerate(raw.splitlines(), start=1):
        try:
            parsed: Any = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryInputError(
                "record_schema_invalid",
                f"{raw_path}:{line_number} is not valid UTF-8 JSON",
            ) from exc
        row = _validate_row(
            parsed,
            raw_path=raw_path,
            line_number=line_number,
            source=str(manifest["source"]),
            schema_descriptors=schema_descriptors,
        )
        fingerprint = str(row["schema_fingerprint"])
        descriptor = actual_stats[fingerprint]["descriptor"]
        ordered_fingerprints.append(fingerprint)
        stats = expected_schema_stats.setdefault(
            fingerprint,
            {
                "fingerprint": fingerprint,
                "count": 0,
                "descriptor": descriptor,
            },
        )
        stats["count"] += 1
        if _is_legacy_registry_row(row):
            if latest_legacy_registry_index is not None:
                if latest_legacy_registry_raw is None:
                    raise RecoveryInputError(
                        "recovery_replay_checkpoint_invalid",
                        "legacy registry raw row was not retained",
                    )
                canonical_replay_row_zlibs[
                    latest_legacy_registry_index
                ] = zlib.compress(
                    latest_legacy_registry_raw,
                    level=1,
                )
                rows[latest_legacy_registry_index] = (
                    _compact_legacy_registry_row(
                        rows[latest_legacy_registry_index]
                    )
                )
            latest_legacy_registry_index = len(rows)
            latest_legacy_registry_raw = line
        rows.append(row)
        canonical_replay_row_zlibs.append(None)
    first_fingerprint = (
        None if not ordered_fingerprints else ordered_fingerprints[0]
    )
    drift_count = sum(
        fingerprint != first_fingerprint
        for fingerprint in ordered_fingerprints
    )
    change_count = sum(
        current != previous
        for previous, current in zip(
            ordered_fingerprints,
            ordered_fingerprints[1:],
        )
    )
    if (
        actual_stats != expected_schema_stats
        or len(actual_stats) != len(manifest_schema_stats)
        or manifest.get("schema_drift_count") != drift_count
        or manifest.get("schema_change_count") != change_count
    ):
        raise RecoveryInputError(
            "finalized_schema_mismatch",
            f"manifest schema statistics do not match rows: {raw_path}",
        )
    return _VerifiedBatch(
        run_id=run_id,
        raw_path=raw_path,
        relative_path=relative_to_run,
        source=str(manifest["source"]),
        manifest=manifest,
        rows=tuple(rows),
        canonical_replay_row_zlibs=tuple(
            canonical_replay_row_zlibs
        ),
    )


def _run_roots(
    roots: Iterable[str | Path] | str | Path,
    *,
    _proof_sink: list[_RunRootProof] | None = None,
) -> tuple[tuple[str, Path], ...]:
    """Normalize roots and transfer held descriptors transactionally."""

    if _proof_sink is None:
        return _run_roots_unchecked(roots)
    proof_start = len(_proof_sink)
    try:
        return _run_roots_unchecked(
            roots,
            _proof_sink=_proof_sink,
        )
    except BaseException:
        created = _proof_sink[proof_start:]
        del _proof_sink[proof_start:]
        for proof in reversed(created):
            proof.close()
        raise


def _run_roots_unchecked(
    roots: Iterable[str | Path] | str | Path,
    *,
    _proof_sink: list[_RunRootProof] | None = None,
) -> tuple[tuple[str, Path], ...]:
    values = [roots] if isinstance(roots, (str, Path)) else list(roots)
    if not values:
        raise ValueError("at least one prior run root is required")
    normalized: list[tuple[str, Path]] = []
    seen_ids: set[str] = set()
    for value in values:
        root = Path(value).expanduser().resolve()
        try:
            root_metadata = root.lstat()
        except OSError as exc:
            raise ValueError(
                f"prior run root must exist: {root}"
            ) from exc
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
        ):
            raise ValueError(f"prior run root must exist: {root}")
        run_id = root.name
        if _RUN_ID.fullmatch(run_id) is None:
            raise ValueError(f"prior run ID is unsafe: {run_id!r}")
        if run_id in seen_ids:
            raise ValueError(f"duplicate prior run ID: {run_id}")
        seen_ids.add(run_id)
        if _proof_sink is not None:
            proof = _RunRootProof(
                run_id=run_id,
                path=root,
                identity=(
                    root_metadata.st_dev,
                    root_metadata.st_ino,
                    root_metadata.st_mode,
                ),
            )
            descriptor: int | None = None
            try:
                descriptor = _open_run_root_descriptor(proof)
            except RecoveryInputError:
                if descriptor is not None:
                    os.close(descriptor)
                raise
            os.close(descriptor)
            _proof_sink.append(proof)
        normalized.append((run_id, root))
    return tuple(sorted(normalized))


def _revalidate_run_root_proof(
    proof: _RunRootProof,
) -> None:
    descriptor = _open_run_root_descriptor(proof)
    os.close(descriptor)


def _revalidate_run_root_proofs(
    proofs: Mapping[str, _RunRootProof],
) -> None:
    for run_id in sorted(proofs):
        _revalidate_run_root_proof(proofs[run_id])


def _descriptor_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _is_recovery_artifact_path(relative_path: str) -> bool:
    return (
        relative_path.endswith(".jsonl")
        or relative_path.endswith(".manifest.json")
        or relative_path.endswith(".jsonl.partial")
        or relative_path
        == "control/checkpoints/dynamic_short_crypto_service.json"
    )


def _run_tree_open_flags() -> int:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if (
        not isinstance(directory_flag, int)
        or directory_flag == 0
        or not isinstance(no_follow, int)
        or no_follow == 0
    ):
        raise RecoveryInputError(
            "recovery_run_root_unprovable",
            "descriptor-safe run traversal is unavailable",
        )
    return (
        os.O_RDONLY
        | directory_flag
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_run_root_descriptor(proof: _RunRootProof) -> int:
    """Open and bind one run root for a bounded operation."""

    descriptor: int | None = None
    try:
        entry_before = proof.path.lstat()
        descriptor = os.open(proof.path, _run_tree_open_flags())
        opened = os.fstat(descriptor)
        entry_after = proof.path.lstat()
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise RecoveryInputError(
            "recovery_run_root_unprovable",
            f"cannot bind prior run root: {proof.run_id}",
        ) from exc
    identities = {
        (
            item.st_dev,
            item.st_ino,
            item.st_mode,
        )
        for item in (entry_before, opened, entry_after)
    }
    if (
        len(identities) != 1
        or next(iter(identities)) != proof.identity
        or stat.S_ISLNK(entry_after.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
    ):
        os.close(descriptor)
        raise RecoveryInputError(
            "recovery_run_root_changed",
            f"prior run root changed while bound: {proof.run_id}",
        )
    return descriptor


def _run_tree_components(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str):
        raise RecoveryInputError(
            "recovery_run_root_unreadable",
            f"unsafe run-tree directory path: {relative_path!r}",
        )
    if relative_path == "":
        return ()
    path = Path(relative_path)
    parts = tuple(path.parts)
    if (
        not relative_path
        or path.is_absolute()
        or path.as_posix() != relative_path
        or not parts
        or any(
            not component
            or component in {".", ".."}
            or Path(component).name != component
            for component in parts
        )
    ):
        raise RecoveryInputError(
            "recovery_run_root_unreadable",
            f"unsafe run-tree directory path: {relative_path!r}",
        )
    return parts


def _close_directory_chain(descriptors: Iterable[int]) -> None:
    for descriptor in reversed(tuple(descriptors)):
        os.close(descriptor)


def _open_held_directory_chain(
    proof: _RunRootProof,
    root_descriptor: int,
    relative_path: str,
    *,
    directories: Mapping[str, _HeldDirectory],
) -> tuple[int, ...]:
    """Open one proven run directory with descriptors bounded by tree depth."""

    open_flags = _run_tree_open_flags()
    descriptors: list[int] = []
    try:
        duplicated_root = os.dup(root_descriptor)
        descriptors.append(duplicated_root)
        root_metadata = os.fstat(duplicated_root)
        expected_root = directories.get("")
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or (
                root_metadata.st_dev,
                root_metadata.st_ino,
                root_metadata.st_mode,
            )
            != proof.identity
            or expected_root is None
            or _descriptor_identity(root_metadata)[:3]
            != expected_root.identity[:3]
        ):
            raise RecoveryInputError(
                "recovery_run_root_changed",
                f"held run root identity changed: {proof.run_id}",
            )
        current_relative = ""
        for component in _run_tree_components(relative_path):
            next_relative = (
                component
                if not current_relative
                else f"{current_relative}/{component}"
            )
            expected = directories.get(next_relative)
            if expected is None:
                raise RecoveryInputError(
                    "recovery_run_root_changed",
                    (
                        "run directory is absent from held inventory: "
                        f"{proof.run_id}/{next_relative}"
                    ),
                )
            parent_descriptor = descriptors[-1]
            entry_before = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            child_descriptor = os.open(
                component,
                open_flags,
                dir_fd=parent_descriptor,
            )
            descriptors.append(child_descriptor)
            opened = os.fstat(child_descriptor)
            entry_after = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            identities = {
                _descriptor_identity(item)
                for item in (entry_before, opened, entry_after)
            }
            if (
                len(identities) != 1
                or not stat.S_ISDIR(opened.st_mode)
                or next(iter(identities))[:3]
                != expected.identity[:3]
            ):
                raise RecoveryInputError(
                    "recovery_run_root_changed",
                    (
                        "run directory identity changed while reopened: "
                        f"{proof.run_id}/{next_relative}"
                    ),
                )
            current_relative = next_relative
        return tuple(descriptors)
    except RecoveryInputError:
        _close_directory_chain(descriptors)
        raise
    except OSError as exc:
        _close_directory_chain(descriptors)
        raise RecoveryInputError(
            "recovery_run_root_changed",
            (
                "run directory changed while reopened: "
                f"{proof.run_id}/{relative_path}"
            ),
        ) from exc


def _open_held_run_view(proof: _RunRootProof) -> _HeldRunView:
    """Hold one root FD while inventorying it with depth-bounded walks."""

    root_descriptor = _open_run_root_descriptor(proof)
    try:
        return _inventory_held_run_view(proof, root_descriptor)
    except BaseException:
        os.close(root_descriptor)
        raise


def _inventory_held_run_view(
    proof: _RunRootProof,
    root_descriptor: int,
) -> _HeldRunView:
    open_flags = _run_tree_open_flags()
    root_metadata = os.fstat(root_descriptor)
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or (
            root_metadata.st_dev,
            root_metadata.st_ino,
            root_metadata.st_mode,
        )
        != proof.identity
    ):
        raise RecoveryInputError(
            "recovery_run_root_changed",
            f"held run root identity changed: {proof.run_id}",
        )
    root_directory = _HeldDirectory(
        relative_path="",
        identity=_descriptor_identity(root_metadata),
    )
    directories: dict[str, _HeldDirectory] = {"": root_directory}
    artifacts: dict[str, _HeldArtifact] = {}
    member_names: dict[str, tuple[str, ...]] = {}
    member_identities: dict[
        str, dict[str, tuple[int, int, int, int, int, int]]
    ] = {}
    pending = [root_directory]
    while pending:
        directory = pending.pop()
        descriptors = _open_held_directory_chain(
            proof,
            root_descriptor,
            directory.relative_path,
            directories=directories,
        )
        directory_descriptor = descriptors[-1]
        try:
            try:
                names = sorted(os.listdir(directory_descriptor))
            except OSError as exc:
                raise RecoveryInputError(
                    "recovery_run_root_unreadable",
                    (
                        "cannot enumerate descriptor-held run directory: "
                        f"{proof.run_id}/{directory.relative_path}"
                    ),
                ) from exc
            member_names[directory.relative_path] = tuple(names)
            members: dict[
                str, tuple[int, int, int, int, int, int]
            ] = {}
            member_identities[directory.relative_path] = members
            for name in names:
                if (
                    not isinstance(name, str)
                    or not name
                    or Path(name).name != name
                    or name in {".", ".."}
                ):
                    raise RecoveryInputError(
                        "recovery_run_root_unreadable",
                        f"unsafe run-tree entry: {proof.run_id}/{name}",
                    )
                relative_path = (
                    name
                    if not directory.relative_path
                    else f"{directory.relative_path}/{name}"
                )
                try:
                    entry_before = os.stat(
                        name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise RecoveryInputError(
                        "recovery_run_root_changed",
                        (
                            "run-tree entry changed during discovery: "
                            f"{proof.run_id}/{relative_path}"
                        ),
                    ) from exc
                entry_identity = _descriptor_identity(entry_before)
                members[name] = entry_identity
                if stat.S_ISDIR(entry_before.st_mode):
                    child_descriptor: int | None = None
                    try:
                        child_descriptor = os.open(
                            name,
                            open_flags,
                            dir_fd=directory_descriptor,
                        )
                        opened = os.fstat(child_descriptor)
                        entry_after = os.stat(
                            name,
                            dir_fd=directory_descriptor,
                            follow_symlinks=False,
                        )
                    except OSError as exc:
                        if child_descriptor is not None:
                            os.close(child_descriptor)
                        raise RecoveryInputError(
                            "recovery_run_root_changed",
                            (
                                "run directory changed while held: "
                                f"{proof.run_id}/{relative_path}"
                            ),
                        ) from exc
                    identities = {
                        _descriptor_identity(item)
                        for item in (entry_before, opened, entry_after)
                    }
                    if (
                        len(identities) != 1
                        or not stat.S_ISDIR(opened.st_mode)
                    ):
                        os.close(child_descriptor)
                        raise RecoveryInputError(
                            "recovery_run_root_changed",
                            (
                                "run directory identity changed while held: "
                                f"{proof.run_id}/{relative_path}"
                            ),
                        )
                    child = _HeldDirectory(
                        relative_path=relative_path,
                        identity=next(iter(identities)),
                    )
                    os.close(child_descriptor)
                    directories[relative_path] = child
                    pending.append(child)
                    continue
                if not _is_recovery_artifact_path(relative_path):
                    continue
                if relative_path in artifacts:
                    raise RecoveryInputError(
                        "recovery_run_root_unreadable",
                        (
                            "duplicate descriptor-relative artifact: "
                            f"{proof.run_id}/{relative_path}"
                        ),
                    )
                artifacts[relative_path] = _HeldArtifact(
                    relative_path=relative_path,
                    path=proof.path / relative_path,
                    parent_relative_path=directory.relative_path,
                    name=name,
                    identity=entry_identity,
                )
            try:
                names_after = tuple(
                    sorted(os.listdir(directory_descriptor))
                )
                current_directory = os.fstat(directory_descriptor)
            except OSError as exc:
                raise RecoveryInputError(
                    "recovery_run_root_changed",
                    (
                        "run directory changed during discovery: "
                        f"{proof.run_id}/{directory.relative_path}"
                    ),
                ) from exc
            if (
                names_after != tuple(names)
                or _descriptor_identity(current_directory)[:3]
                != directory.identity[:3]
            ):
                raise RecoveryInputError(
                    "recovery_run_root_changed",
                    (
                        "run directory inventory changed during discovery: "
                        f"{proof.run_id}/{directory.relative_path}"
                    ),
                )
            for name, expected_member in members.items():
                try:
                    current_member = _descriptor_identity(
                        os.stat(
                            name,
                            dir_fd=directory_descriptor,
                            follow_symlinks=False,
                        )
                    )
                except OSError as exc:
                    raise RecoveryInputError(
                        "recovery_run_root_changed",
                        (
                            "run-tree entry changed during discovery: "
                            f"{proof.run_id}/{directory.relative_path}/{name}"
                        ),
                    ) from exc
                unchanged = (
                    current_member[:3] == expected_member[:3]
                    if stat.S_ISDIR(expected_member[2])
                    else current_member == expected_member
                )
                if not unchanged:
                    raise RecoveryInputError(
                        "recovery_run_root_changed",
                        (
                            "run-tree entry identity changed during discovery: "
                            f"{proof.run_id}/{directory.relative_path}/{name}"
                        ),
                    )
        finally:
            _close_directory_chain(descriptors)
    return _HeldRunView(
        run_id=proof.run_id,
        run_root=proof.path,
        root_proof=proof,
        root_descriptor=root_descriptor,
        directories=directories,
        artifacts=artifacts,
        member_names=member_names,
        member_identities=member_identities,
        content_stats={},
    )


def _revalidate_held_run_view(view: _HeldRunView) -> None:
    """Reopen and prove every directory without retaining the whole tree."""

    for relative_path in sorted(view.directories):
        directory = view.directories[relative_path]
        descriptors = _open_held_directory_chain(
            view.root_proof,
            view.root_descriptor,
            relative_path,
            directories=view.directories,
        )
        directory_descriptor = descriptors[-1]
        try:
            try:
                opened = os.fstat(directory_descriptor)
                current_member_names = tuple(
                    sorted(os.listdir(directory_descriptor))
                )
            except OSError as exc:
                raise RecoveryInputError(
                    "recovery_run_root_changed",
                    (
                        "descriptor-held run directory changed: "
                        f"{view.run_id}/{directory.relative_path}"
                    ),
                ) from exc
            expected_directory_identity = directory.identity[:3]
            if (
                current_member_names
                != view.member_names[relative_path]
                or _descriptor_identity(opened)[:3]
                != expected_directory_identity
                or not stat.S_ISDIR(opened.st_mode)
            ):
                raise RecoveryInputError(
                    "recovery_run_root_changed",
                    (
                        "descriptor-held run directory identity changed: "
                        f"{view.run_id}/{directory.relative_path}"
                    ),
                )
            for name, expected_member in view.member_identities[
                relative_path
            ].items():
                try:
                    current_member = _descriptor_identity(
                        os.stat(
                            name,
                            dir_fd=directory_descriptor,
                            follow_symlinks=False,
                        )
                    )
                except OSError as exc:
                    raise RecoveryInputError(
                        "recovery_run_root_changed",
                        (
                            "descriptor-held run member changed: "
                            f"{view.run_id}/{directory.relative_path}/{name}"
                        ),
                    ) from exc
                if stat.S_ISDIR(expected_member[2]):
                    unchanged = (
                        current_member[:3] == expected_member[:3]
                    )
                else:
                    unchanged = current_member == expected_member
                if not unchanged:
                    raise RecoveryInputError(
                        "recovery_run_root_changed",
                        (
                            "descriptor-held run member identity changed: "
                            f"{view.run_id}/{directory.relative_path}/{name}"
                        ),
                    )
        finally:
            _close_directory_chain(descriptors)
    for artifact in view.artifacts.values():
        if (
            view.member_identities.get(
                artifact.parent_relative_path, {}
            ).get(artifact.name)
            != artifact.identity
        ):
            raise RecoveryInputError(
                "recovery_run_root_changed",
                (
                    "held run artifact is absent from its inventory: "
                    f"{view.run_id}/{artifact.relative_path}"
                ),
            )


def _stable_read_held_artifact(
    view: _HeldRunView,
    artifact: _HeldArtifact,
    *,
    code: str,
) -> bytes:
    if (
        view.artifacts.get(artifact.relative_path) != artifact
        or artifact.parent_relative_path not in view.directories
    ):
        raise RecoveryInputError(
            code,
            (
                "artifact is absent from descriptor-rooted inventory: "
                f"{view.run_id}/{artifact.relative_path}"
            ),
        )
    descriptors = _open_held_directory_chain(
        view.root_proof,
        view.root_descriptor,
        artifact.parent_relative_path,
        directories=view.directories,
    )
    parent_descriptor = descriptors[-1]
    try:
        stable = _stable_read_at(
            parent_descriptor,
            artifact.name,
            code=code,
        )
        if stable.identity != artifact.identity:
            raise RecoveryInputError(
                code,
                (
                    "descriptor-rooted artifact changed before read: "
                    f"{view.run_id}/{artifact.relative_path}"
                ),
            )
        _revalidate_stable_entry_at(
            parent_descriptor,
            artifact.name,
            identity=artifact.identity,
            code=code,
        )
    finally:
        _close_directory_chain(descriptors)
    content = stable.content
    view.content_stats[artifact.relative_path] = (
        hashlib.sha256(content).hexdigest(),
        len(content),
        content.count(b"\n"),
    )
    return content


def _stable_digest_held_artifact(
    view: _HeldRunView,
    artifact: _HeldArtifact,
    *,
    code: str,
    count_lines: bool,
) -> tuple[str, int, int | None]:
    """Hash one descriptor-held artifact without hydrating or parsing it."""

    if (
        view.artifacts.get(artifact.relative_path) != artifact
        or artifact.parent_relative_path not in view.directories
    ):
        raise RecoveryInputError(
            code,
            (
                "artifact is absent from descriptor-rooted inventory: "
                f"{view.run_id}/{artifact.relative_path}"
            ),
        )
    descriptors = _open_held_directory_chain(
        view.root_proof,
        view.root_descriptor,
        artifact.parent_relative_path,
        directories=view.directories,
    )
    parent_descriptor = descriptors[-1]
    descriptor: int | None = None
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    if (
        not isinstance(no_follow, int)
        or no_follow == 0
        or not isinstance(nonblocking, int)
        or nonblocking == 0
    ):
        _close_directory_chain(descriptors)
        raise RecoveryInputError(
            code,
            "descriptor-relative safe reads are unavailable",
        )
    flags = (
        os.O_RDONLY
        | no_follow
        | nonblocking
        | getattr(os, "O_CLOEXEC", 0)
    )
    digest = hashlib.sha256()
    byte_count = 0
    line_count = 0
    try:
        entry_before = os.stat(
            artifact.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(entry_before.st_mode):
            raise RecoveryInputError(
                code,
                (
                    "descriptor-relative input is not a regular file: "
                    f"{artifact.name}"
                ),
            )
        descriptor = os.open(
            artifact.name,
            flags,
            dir_fd=parent_descriptor,
        )
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            raise RecoveryInputError(
                code,
                (
                    "descriptor-relative input is not a regular file: "
                    f"{artifact.name}"
                ),
            )
        while chunk := os.read(descriptor, 8 * 1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
            if count_lines:
                line_count += chunk.count(b"\n")
        opened_after = os.fstat(descriptor)
        entry_after = os.stat(
            artifact.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except RecoveryInputError:
        raise
    except OSError as exc:
        raise RecoveryInputError(
            code,
            (
                "cannot hash descriptor-relative artifact: "
                f"{artifact.name}"
            ),
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _close_directory_chain(descriptors)
    identities = {
        _descriptor_identity(item)
        for item in (
            entry_before,
            opened_before,
            opened_after,
            entry_after,
        )
    }
    if (
        len(identities) != 1
        or next(iter(identities)) != artifact.identity
        or byte_count != opened_after.st_size
    ):
        raise RecoveryInputError(
            code,
            (
                "descriptor-rooted artifact changed while hashed: "
                f"{view.run_id}/{artifact.relative_path}"
            ),
        )
    stats = (
        digest.hexdigest(),
        byte_count,
        line_count if count_lines else None,
    )
    view.content_stats[artifact.relative_path] = stats
    return stats


def _checkpoint_artifact(
    run_root: Path,
) -> _CheckpointArtifact | None:
    view = _ACTIVE_HELD_RUN_VIEW.get()
    relative_path = (
        "control/checkpoints/dynamic_short_crypto_service.json"
    )
    if view is not None and view.run_root == run_root:
        artifact = view.artifacts.get(relative_path)
        if artifact is None:
            return None
        if not stat.S_ISREG(artifact.identity[2]):
            raise RecoveryInputError(
                "checkpoint_unreadable",
                f"checkpoint is not a regular file: {artifact.path}",
            )
        return _CheckpointArtifact(
            path=artifact.path,
            document=_json_object(
                _stable_read_held_artifact(
                    view,
                    artifact,
                    code="checkpoint_unreadable",
                ),
                path=artifact.path,
                code="checkpoint_invalid",
            ),
        )
    checkpoint_path = (
        run_root
        / "control"
        / "checkpoints"
        / "dynamic_short_crypto_service.json"
    )
    if not checkpoint_path.is_file():
        return None
    return _CheckpointArtifact(
        path=checkpoint_path,
        document=_json_object(
            _stable_read(checkpoint_path, code="checkpoint_unreadable"),
            path=checkpoint_path,
            code="checkpoint_invalid",
        ),
    )


def _checkpoint_record_id_hint(
    artifact: _CheckpointArtifact | None,
) -> str | None:
    if artifact is None:
        return None
    checkpoint = artifact.document.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        return None
    recorder_checkpoint = checkpoint.get("recorder_checkpoint")
    if not isinstance(recorder_checkpoint, Mapping):
        return None
    referenced_id = recorder_checkpoint.get(
        "record_id",
        recorder_checkpoint.get("event_record_id"),
    )
    return referenced_id if isinstance(referenced_id, str) else None


def _checkpoint_audit(
    run_root: Path,
    *,
    artifact: _CheckpointArtifact | None,
    verified_batches: tuple[_VerifiedBatch, ...],
    finalized_record_ids: set[str],
) -> _CheckpointAudit:
    if artifact is None:
        return _CheckpointAudit(
            closed=False,
            startup_integrity_findings=(),
        )
    checkpoint_path = artifact.path
    document = artifact.document
    checkpoint = document.get("checkpoint")
    if (
        document.get("schema_version") != "capture-checkpoint.v1"
        or document.get("source") != "dynamic_short_crypto_service"
        or not isinstance(checkpoint, dict)
        or checkpoint.get("schema_version")
        != "edge-lab-capture-runtime.checkpoint.v1"
    ):
        raise RecoveryInputError(
            "checkpoint_invalid",
            f"capture checkpoint schema/source is invalid: {checkpoint_path}",
        )
    runtime = checkpoint.get("capture_runtime")
    if not isinstance(runtime, dict):
        raise RecoveryInputError(
            "checkpoint_invalid",
            f"capture runtime checkpoint is missing: {checkpoint_path}",
        )
    startup_integrity = runtime.get("startup_integrity")
    integrity_categories = (
        "orphan_partials",
        "raw_without_manifest",
        "manifest_without_raw",
        "checksum_mismatches",
        "invalid_manifests",
    )
    if (
        not isinstance(startup_integrity, dict)
        or any(
            not isinstance(startup_integrity.get(category), list)
            for category in integrity_categories
        )
    ):
        raise RecoveryInputError(
            "checkpoint_invalid",
            f"startup integrity report is invalid: {checkpoint_path}",
        )
    startup_findings = tuple(
        {
            "category": category,
            "finding": deepcopy(finding),
        }
        for category in integrity_categories
        for finding in startup_integrity[category]
    )
    summary = runtime.get("last_finalized_batch")
    if summary is not None:
        candidates = [
            batch
            for batch in verified_batches
            if batch.source == "dynamic_short_crypto_service"
            and batch.manifest.get("batch_id") == summary.get("batch_id")
        ] if isinstance(summary, dict) else []
        if (
            len(candidates) != 1
            or summary.get("raw_path")
            != candidates[0].manifest.get("raw_path")
            or summary.get("record_count")
            != candidates[0].manifest.get("record_count")
            or summary.get("checksum")
            != candidates[0].manifest.get("checksum")
        ):
            raise RecoveryInputError(
                "checkpoint_finalized_reference_mismatch",
                f"checkpoint does not reference verified finalized raw: "
                f"{checkpoint_path}",
            )
    recorder_checkpoint = checkpoint.get("recorder_checkpoint")
    if recorder_checkpoint is not None:
        if (
            not isinstance(recorder_checkpoint, dict)
            or not isinstance(
                recorder_checkpoint.get("schema_version"), str
            )
        ):
            raise RecoveryInputError(
                "checkpoint_invalid",
                f"inner recorder checkpoint is invalid: {checkpoint_path}",
            )
        referenced_id = recorder_checkpoint.get(
            "record_id",
            recorder_checkpoint.get("event_record_id"),
        )
        if (
            referenced_id is not None
            and (
                not isinstance(referenced_id, str)
                or referenced_id not in finalized_record_ids
            )
        ):
            raise RecoveryInputError(
                "checkpoint_record_not_finalized",
                f"checkpoint references non-finalized record: {checkpoint_path}",
            )
    return _CheckpointAudit(
        closed=runtime.get("reason") == "close",
        startup_integrity_findings=startup_findings,
    )


def _descriptor(value: Any, *, record_id: str) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("path"), str)
        or not isinstance(value.get("sha256"), str)
        or _CONTENT_HASH.fullmatch(value["sha256"]) is None
        or isinstance(value.get("byte_count"), bool)
        or not isinstance(value.get("byte_count"), int)
        or value["byte_count"] < 0
        or isinstance(value.get("line_count"), bool)
        or not isinstance(value.get("line_count"), int)
        or value["line_count"] < 0
    ):
        raise RecoveryInputError(
            "registry_descriptor_invalid",
            f"registry record {record_id} contains an invalid input descriptor",
        )
    return {
        "path": value["path"],
        "sha256": value["sha256"],
        "byte_count": value["byte_count"],
        "line_count": value["line_count"],
    }


def _validated_decision(
    value: Any,
    *,
    record_id: str,
) -> dict[str, Any]:
    required = {
        "schema_version",
        "decision_id",
        "sequence",
        "action",
        "group_id",
        "asset_ids",
        "targets",
        "emitted_at_ms",
        "reason",
        "minimum_snapshot_watermark_ns",
    }
    persisted_fields = required | {"scheduler_now_ms"}
    if (
        not isinstance(value, dict)
        or frozenset(value)
        not in {frozenset(required), frozenset(persisted_fields)}
        or value.get("schema_version")
        != "edge-lab.short-crypto-capture-decision.v1"
        or not isinstance(value.get("decision_id"), str)
        or _CONTENT_HASH.fullmatch(value["decision_id"]) is None
        or isinstance(value.get("sequence"), bool)
        or not isinstance(value.get("sequence"), int)
        or value["sequence"] < 1
        or value.get("action")
        not in {"subscribe", "unsubscribe", "resnapshot_required"}
        or not isinstance(value.get("group_id"), str)
        or not value["group_id"]
        or not isinstance(value.get("asset_ids"), list)
        or not all(isinstance(item, str) and item for item in value["asset_ids"])
        or not isinstance(value.get("targets"), list)
        or not all(
            isinstance(item, dict) and isinstance(item.get("slug"), str)
            for item in value["targets"]
        )
        or isinstance(value.get("emitted_at_ms"), bool)
        or not isinstance(value.get("emitted_at_ms"), int)
        or not isinstance(value.get("reason"), str)
        or (
            "scheduler_now_ms" in value
            and (
                isinstance(value.get("scheduler_now_ms"), bool)
                or not isinstance(value.get("scheduler_now_ms"), int)
            )
        )
        or (
            value.get("minimum_snapshot_watermark_ns") is not None
            and (
                isinstance(
                    value.get("minimum_snapshot_watermark_ns"), bool
                )
                or not isinstance(
                    value.get("minimum_snapshot_watermark_ns"), int
                )
            )
        )
    ):
        raise RecoveryInputError(
            "capture_decision_invalid",
            f"record {record_id} has an invalid capture decision",
        )
    decision = {
        key: deepcopy(item)
        for key, item in value.items()
        if key != "scheduler_now_ms"
    }
    body = {
        key: deepcopy(item)
        for key, item in decision.items()
        if key != "decision_id"
    }
    expected = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    if expected != decision["decision_id"]:
        raise RecoveryInputError(
            "capture_decision_id_mismatch",
            f"record {record_id} has a non-canonical decision ID",
        )
    return decision


def _validated_cohort_event(
    event_type: str,
    value: Any,
    *,
    outer_kind: Any,
    record_id: str,
    received_at_ms: int,
    settlement_timeout_ms: int,
) -> dict[str, Any]:
    try:
        return validate_cohort_payload(
            event_type,
            value,
            outer_kind=outer_kind,
            record_id=record_id,
            received_at_ms=received_at_ms,
            settlement_timeout_ms=settlement_timeout_ms,
        )
    except LifecycleCohortContractError as exc:
        raise RecoveryInputError(
            "lifecycle_cohort_journal_invalid",
            str(exc),
        ) from exc


@dataclass(frozen=True)
class _RecoveredCohortEvent:
    event_type: str
    record_id: str
    run_id: str
    manifest_path: Path
    received_at_ms: int
    payload: dict[str, Any]


def _cohort_received_at_ms(value: Any, *, record_id: str) -> int:
    if not isinstance(value, str) or not value:
        raise RecoveryInputError(
            "lifecycle_cohort_journal_invalid",
            f"record {record_id} has no durable received_at",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryInputError(
            "lifecycle_cohort_journal_invalid",
            f"record {record_id} has an invalid durable received_at",
        ) from exc
    if parsed.tzinfo is None:
        raise RecoveryInputError(
            "lifecycle_cohort_journal_invalid",
            f"record {record_id} has a timezone-naive durable received_at",
        )
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000)


def _validated_cohort_journal(
    events: list[_RecoveredCohortEvent],
) -> list[dict[str, Any]]:
    roots = [
        event
        for event in events
        if event.event_type == "lifecycle_cohort_committed"
    ]
    remediations = [
        event
        for event in events
        if event.event_type == "lifecycle_remediation_cohort_committed"
    ]
    record_ids = [event.record_id for event in events]
    if len(record_ids) != len(set(record_ids)):
        raise RecoveryInputError(
            "lifecycle_cohort_journal_invalid",
            "duplicate cohort commitment record ID",
        )
    remediation_by_id = {
        event.record_id: event for event in remediations
    }
    for start_record_id in remediation_by_id:
        cursor = start_record_id
        visited: set[str] = set()
        while cursor in remediation_by_id:
            if cursor in visited:
                raise RecoveryInputError(
                    "lifecycle_cohort_journal_invalid",
                    "remediation commitment predecessors are cyclic",
                )
            visited.add(cursor)
            cursor = remediation_by_id[cursor].payload[
                "predecessor_commitment_record_id"
            ]
    if not roots:
        if remediations:
            raise RecoveryInputError(
                "lifecycle_cohort_journal_invalid",
                "remediation commitment has no earlier root commitment",
            )
        return []
    def order_key(
        event: _RecoveredCohortEvent,
    ) -> tuple[int, str, str]:
        return (
            event.received_at_ms,
            str(event.manifest_path),
            event.record_id,
        )
    chain = [min(roots, key=order_key)]
    unused = {event.record_id: event for event in remediations}
    while unused:
        tip = chain[-1]
        children = sorted(
            (
                event
                for event in unused.values()
                if event.payload["predecessor_commitment_record_id"]
                == tip.record_id
            ),
            key=order_key,
        )
        if len(children) != 1:
            detail = (
                "remediation commitments branch from the same predecessor"
                if len(children) > 1
                else "remediation predecessor is not the exact chain tip"
            )
            raise RecoveryInputError(
                "lifecycle_cohort_journal_invalid",
                detail,
            )
        child = children[0]
        if child.received_at_ms <= tip.received_at_ms:
            raise RecoveryInputError(
                "lifecycle_cohort_journal_invalid",
                "remediation commitment order is non-increasing",
            )
        chain.append(child)
        del unused[child.record_id]
    return [
        {
            "event_type": event.event_type,
            "record_id": event.record_id,
            "run_id": event.run_id,
            "payload": deepcopy(event.payload),
        }
        for event in chain
    ]


def _run_inventory_proof(
    *,
    run_id: str,
    run_root: Path,
    checkpoint_audit: _CheckpointAudit,
    partial_paths: tuple[Path, ...],
    raw_without_manifest: tuple[Path, ...],
    manifest_without_raw: tuple[Path, ...],
    symlink_artifacts: tuple[Path, ...],
) -> dict[str, Any]:
    def relative_paths(paths: tuple[Path, ...]) -> list[str]:
        return [
            path.relative_to(run_root).as_posix()
            for path in paths
        ]

    return {
        "schema_version": RUN_INVENTORY_SCHEMA,
        "run_id": run_id,
        "checkpoint_closed": checkpoint_audit.closed,
        "checkpoint_startup_integrity_findings": deepcopy(
            list(checkpoint_audit.startup_integrity_findings)
        ),
        "partial_paths": relative_paths(partial_paths),
        "raw_without_manifest_paths": relative_paths(
            raw_without_manifest
        ),
        "manifest_without_raw_paths": relative_paths(
            manifest_without_raw
        ),
        "symlink_artifact_paths": relative_paths(symlink_artifacts),
    }


def _checkpoint_base_fields_from_run_inventory(
    inventories: Iterable[Mapping[str, Any]],
    *,
    expected_run_ids: tuple[str, ...],
) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "run_id",
        "checkpoint_closed",
        "checkpoint_startup_integrity_findings",
        "partial_paths",
        "raw_without_manifest_paths",
        "manifest_without_raw_paths",
        "symlink_artifact_paths",
    }
    by_run_id: dict[str, Mapping[str, Any]] = {}
    path_fields = (
        "partial_paths",
        "raw_without_manifest_paths",
        "manifest_without_raw_paths",
        "symlink_artifact_paths",
    )
    for inventory in inventories:
        run_id = inventory.get("run_id")
        if (
            set(inventory) != expected_keys
            or inventory.get("schema_version")
            != RUN_INVENTORY_SCHEMA
            or not isinstance(run_id, str)
            or run_id in by_run_id
            or type(inventory.get("checkpoint_closed")) is not bool
            or not isinstance(
                inventory.get(
                    "checkpoint_startup_integrity_findings"
                ),
                list,
            )
            or any(
                not isinstance(item, dict)
                for item in inventory[
                    "checkpoint_startup_integrity_findings"
                ]
            )
            or any(
                not isinstance(inventory.get(field), list)
                or inventory[field] != sorted(set(inventory[field]))
                or any(
                    not isinstance(path, str)
                    or Path(path).is_absolute()
                    or ".." in Path(path).parts
                    for path in inventory[field]
                )
                for field in path_fields
            )
        ):
            raise RecoveryInputError(
                "recovery_record_index_invalid",
                f"run inventory proof is invalid: {run_id}",
            )
        by_run_id[run_id] = inventory
    if tuple(by_run_id) != tuple(sorted(by_run_id)):
        by_run_id = {
            run_id: by_run_id[run_id]
            for run_id in sorted(by_run_id)
        }
    if set(by_run_id) != set(expected_run_ids):
        raise RecoveryInputError(
            "recovery_record_index_invalid",
            "run inventory proof does not cover exact checkpoint roots",
        )

    classifications: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    runs_with_orphan_partials: set[str] = set()
    runs_with_input_integrity_gaps: set[str] = set()
    for run_id in expected_run_ids:
        inventory = by_run_id[run_id]
        closed = inventory["checkpoint_closed"]
        conditions = [
            "clean_completed" if closed else "checkpoint_missing"
        ]
        if not closed:
            gaps.append(
                {
                    "code": "checkpoint_missing",
                    "run_id": run_id,
                }
            )
        partial_paths = inventory["partial_paths"]
        if partial_paths:
            runs_with_orphan_partials.add(run_id)
            conditions.append("orphan_partial")
            exclusions.extend(
                {
                    "code": "orphan_partial_ignored",
                    "run_id": run_id,
                    "path": path,
                    "recovery": (
                        "earlier_finalized_state_or_public_reacquisition"
                    ),
                }
                for path in partial_paths
            )
        integrity_groups = (
            (
                "raw_without_manifest",
                "raw_without_manifest_excluded",
                inventory["raw_without_manifest_paths"],
            ),
            (
                "manifest_without_raw",
                "manifest_without_raw_excluded",
                inventory["manifest_without_raw_paths"],
            ),
            (
                "symlink_finalized_artifact",
                "symlink_finalized_artifact_excluded",
                inventory["symlink_artifact_paths"],
            ),
        )
        for gap_code, exclusion_code, paths in integrity_groups:
            if not paths:
                continue
            runs_with_input_integrity_gaps.add(run_id)
            conditions.append(gap_code)
            for path in paths:
                gaps.append(
                    {
                        "code": gap_code,
                        "run_id": run_id,
                        "path": path,
                    }
                )
                exclusions.append(
                    {
                        "code": exclusion_code,
                        "run_id": run_id,
                        "path": path,
                        "recovery": (
                            "ignore_non_finalized_or_non_regular_artifact"
                        ),
                    }
                )
        startup_findings = inventory[
            "checkpoint_startup_integrity_findings"
        ]
        if startup_findings:
            runs_with_input_integrity_gaps.add(run_id)
            conditions.append("checkpoint_startup_integrity_dirty")
            for finding in startup_findings:
                gap = {
                    "code": "checkpoint_startup_integrity_dirty",
                    "run_id": run_id,
                    "category": finding["category"],
                    "finding": deepcopy(finding["finding"]),
                }
                gaps.append(gap)
                exclusions.append(
                    {
                        **gap,
                        "code": (
                            "checkpoint_startup_integrity_finding_excluded"
                        ),
                        "recovery": (
                            "earlier_clean_finalized_state_or_reacquisition"
                        ),
                    }
                )
        classification = (
            "crash_with_orphan_partial"
            if partial_paths
            else (
                "finalized_input_integrity_gap"
                if run_id in runs_with_input_integrity_gaps
                else conditions[0]
            )
        )
        classifications.append(
            {
                "run_id": run_id,
                "classification": classification,
                "conditions": conditions,
            }
        )
    return {
        "classifications": tuple(classifications),
        "gaps": tuple(gaps),
        "exclusions": tuple(exclusions),
        "runs_with_orphan_partials": tuple(
            sorted(runs_with_orphan_partials)
        ),
        "runs_with_input_integrity_gaps": tuple(
            sorted(runs_with_input_integrity_gaps)
        ),
    }


def _recover_dynamic_short_crypto_runs(
    prior_run_roots: Iterable[str | Path] | str | Path,
    *,
    settlement_timeout_ms: int,
    cpu_ratio: float | None = None,
    _prefix_checkpoint: _RecoveryReplayCheckpoint | None = None,
    _record_index: _FinalizedRecordIndex | None = None,
    _checkpoint_sink: list[_RecoveryReplayCheckpoint] | None = None,
    _checkpoint_only_reduction: bool = False,
    _root_proofs: Mapping[str, _RunRootProof] | None = None,
    _input_proof_sink: dict[
        str, tuple[dict[str, Any], dict[str, Any]]
    ]
    | None = None,
) -> RecoveryResult:
    """Own descriptor resources for one deterministic replay."""

    with ExitStack() as resources:
        root_proofs = (
            None if _root_proofs is None else dict(_root_proofs)
        )
        replay_roots = prior_run_roots
        if root_proofs is None and not _checkpoint_only_reduction:
            held_proofs: list[_RunRootProof] = []
            roots = _run_roots(
                prior_run_roots,
                _proof_sink=held_proofs,
            )
            for proof in reversed(held_proofs):
                resources.callback(proof.close)
            root_proofs = {
                proof.run_id: proof
                for proof in held_proofs
            }
            replay_roots = tuple(path for _, path in roots)
        return _recover_dynamic_short_crypto_runs_impl(
            replay_roots,
            settlement_timeout_ms=settlement_timeout_ms,
            cpu_ratio=cpu_ratio,
            _prefix_checkpoint=_prefix_checkpoint,
            _record_index=_record_index,
            _checkpoint_sink=_checkpoint_sink,
            _checkpoint_only_reduction=_checkpoint_only_reduction,
            _root_proofs=root_proofs,
            _input_proof_sink=_input_proof_sink,
            _resources=resources,
        )


def _recover_dynamic_short_crypto_runs_impl(
    prior_run_roots: Iterable[str | Path] | str | Path,
    *,
    settlement_timeout_ms: int,
    cpu_ratio: float | None = None,
    _prefix_checkpoint: _RecoveryReplayCheckpoint | None = None,
    _record_index: _FinalizedRecordIndex | None = None,
    _checkpoint_sink: list[_RecoveryReplayCheckpoint] | None = None,
    _checkpoint_only_reduction: bool = False,
    _root_proofs: Mapping[str, _RunRootProof] | None = None,
    _input_proof_sink: dict[
        str, tuple[dict[str, Any], dict[str, Any]]
    ]
    | None = None,
    _resources: ExitStack,
) -> RecoveryResult:
    """Validate and replay prior immutable runs without starting workers.

    The returned recovery decision is deterministic and has no timestamp.
    Callers must persist it durably before applying a worker action.
    """

    if (
        isinstance(settlement_timeout_ms, bool)
        or not isinstance(settlement_timeout_ms, int)
        or settlement_timeout_ms < 0
    ):
        raise ValueError(
            "settlement_timeout_ms must be a non-negative integer"
    )
    root_proofs = (
        {}
        if _root_proofs is None
        else dict(_root_proofs)
    )
    if root_proofs:
        values = (
            [prior_run_roots]
            if isinstance(prior_run_roots, (str, Path))
            else list(prior_run_roots)
        )
        supplied_paths = {Path(value).expanduser() for value in values}
        roots = tuple(
            sorted(
                (run_id, proof.path)
                for run_id, proof in root_proofs.items()
            )
        )
        if (
            supplied_paths != {run_root for _, run_root in roots}
            or len(values) != len(roots)
        ):
            raise RecoveryInputError(
                "recovery_run_root_unprovable",
                "held run-root proofs do not match replay roots",
            )
        _revalidate_run_root_proofs(root_proofs)
    else:
        roots = _run_roots(prior_run_roots)
    root_by_run_id = dict(roots)
    run_ids = tuple(run_id for run_id, _ in roots)
    if _prefix_checkpoint is None:
        if _checkpoint_only_reduction:
            raise RecoveryInputError(
                "recovery_replay_checkpoint_invalid",
                "checkpoint-only reduction requires a checkpoint",
            )
        prefix_run_count = 0
    else:
        prefix_run_count = len(_prefix_checkpoint.run_ids)
        if (
            prefix_run_count > len(roots)
            or (
                prefix_run_count == len(roots)
                and not _checkpoint_only_reduction
            )
            or (
                _checkpoint_only_reduction
                and prefix_run_count != len(roots)
            )
            or run_ids[:prefix_run_count]
            != _prefix_checkpoint.run_ids
        ):
            raise RecoveryInputError(
                "recovery_replay_checkpoint_invalid",
                "checkpoint run IDs are not an allowed exact prefix",
            )
    roots_to_validate = roots[prefix_run_count:]
    cpu_limiter = _RecoveryCpuLimiter(cpu_ratio)
    finalized_record_index = (
        _FinalizedRecordIndex()
        if _record_index is None
        else _record_index
    )
    replay_records: list[tuple[str, str, dict[str, Any]]] = (
        []
        if _prefix_checkpoint is None
        else [
            (run_id, relative_path, deepcopy(row))
            for run_id, relative_path, row
            in _prefix_checkpoint.replay_records
        ]
    )
    record_sources: dict[str, str] = (
        {}
        if _prefix_checkpoint is None
        else deepcopy(_prefix_checkpoint.record_sources)
    )
    evidence_locations: dict[str, tuple[str, Path]] = {}
    validated_input_proofs: dict[
        str, tuple[dict[str, Any], dict[str, Any]]
    ] = {}
    required_evidence_ids: set[str] = set()
    required_source_ids: set[str] = set()
    records_by_id: dict[str, dict[str, Any]] = (
        {}
        if _prefix_checkpoint is None
        else deepcopy(_prefix_checkpoint.records_by_id)
    )
    record_connections: dict[str, str] = (
        {}
        if _prefix_checkpoint is None
        else deepcopy(_prefix_checkpoint.record_connections)
    )
    record_event_types: dict[str, str] = (
        {}
        if _prefix_checkpoint is None
        else deepcopy(_prefix_checkpoint.record_event_types)
    )
    validated_record_count = (
        0
        if _prefix_checkpoint is None
        else _prefix_checkpoint.validated_record_count
    )
    latest_legacy_registry_index = (
        None
        if _prefix_checkpoint is None
        else _prefix_checkpoint.latest_legacy_registry_index
    )
    latest_legacy_registry_key = (
        None
        if _prefix_checkpoint is None
        else _prefix_checkpoint.latest_legacy_registry_key
    )
    classifications: list[dict[str, Any]] = (
        []
        if _prefix_checkpoint is None
        else deepcopy(list(_prefix_checkpoint.classifications))
    )
    gaps: list[dict[str, Any]] = (
        []
        if _prefix_checkpoint is None
        else deepcopy(list(_prefix_checkpoint.gaps))
    )
    exclusions: list[dict[str, Any]] = (
        []
        if _prefix_checkpoint is None
        else deepcopy(list(_prefix_checkpoint.exclusions))
    )
    runs_with_orphan_partials: set[str] = (
        set()
        if _prefix_checkpoint is None
        else set(_prefix_checkpoint.runs_with_orphan_partials)
    )
    runs_with_input_integrity_gaps: set[str] = (
        set()
        if _prefix_checkpoint is None
        else set(
            _prefix_checkpoint.runs_with_input_integrity_gaps
        )
    )

    for run_id, run_root in roots_to_validate:
        run_root_proof = root_proofs.get(run_id)
        held_view_lease: _HeldRunViewLease | None = None
        if run_root_proof is not None:
            _revalidate_run_root_proof(run_root_proof)
            held_view = _open_held_run_view(run_root_proof)
            active_token = _ACTIVE_HELD_RUN_VIEW.set(held_view)
            held_view_lease = _HeldRunViewLease(
                view=held_view,
                token=active_token,
            )
            _resources.callback(held_view_lease.close)
        else:
            held_view = None
        checkpoint_artifact = _checkpoint_artifact(run_root)
        checkpoint_record_id_hint = _checkpoint_record_id_hint(
            checkpoint_artifact
        )
        if held_view is None:
            partial_paths = tuple(
                sorted(run_root.rglob("*.jsonl.partial"))
            )
            all_raw_paths = tuple(sorted(run_root.rglob("*.jsonl")))
            all_manifest_paths = tuple(
                sorted(run_root.rglob("*.manifest.json"))
            )
            symlink_artifacts = tuple(
                path
                for path in (*all_raw_paths, *all_manifest_paths)
                if path.is_symlink()
            )
            regular_raw_paths = {
                path
                for path in all_raw_paths
                if not path.is_symlink() and path.is_file()
            }
            regular_manifest_paths = {
                path
                for path in all_manifest_paths
                if not path.is_symlink() and path.is_file()
            }
        else:
            partial_paths = tuple(
                artifact.path
                for artifact in sorted(
                    held_view.artifacts.values(),
                    key=lambda item: item.relative_path,
                )
                if artifact.relative_path.endswith(
                    ".jsonl.partial"
                )
            )
            all_raw_artifacts = tuple(
                artifact
                for artifact in held_view.artifacts.values()
                if artifact.relative_path.endswith(".jsonl")
            )
            all_manifest_artifacts = tuple(
                artifact
                for artifact in held_view.artifacts.values()
                if artifact.relative_path.endswith(".manifest.json")
            )
            all_raw_paths = tuple(
                artifact.path for artifact in all_raw_artifacts
            )
            all_manifest_paths = tuple(
                artifact.path for artifact in all_manifest_artifacts
            )
            symlink_artifacts = tuple(
                artifact.path
                for artifact in (
                    *all_raw_artifacts,
                    *all_manifest_artifacts,
                )
                if stat.S_ISLNK(artifact.identity[2])
            )
            regular_raw_paths = {
                artifact.path
                for artifact in all_raw_artifacts
                if stat.S_ISREG(artifact.identity[2])
            }
            regular_manifest_paths = {
                artifact.path
                for artifact in all_manifest_artifacts
                if stat.S_ISREG(artifact.identity[2])
            }
        raw_without_manifest = tuple(
            sorted(
                path
                for path in regular_raw_paths
                if path.with_suffix(".manifest.json")
                not in regular_manifest_paths
            )
        )
        manifest_without_raw = tuple(
            sorted(
                path
                for path in regular_manifest_paths
                if path.with_name(
                    path.name.removesuffix(".manifest.json")
                    + ".jsonl"
                )
                not in regular_raw_paths
            )
        )
        raw_paths = tuple(
            sorted(
                path
                for path in regular_raw_paths
                if path.with_suffix(".manifest.json")
                in regular_manifest_paths
            )
        )
        verified_batch_references: list[_VerifiedBatch] = []
        finalized_record_ids: set[str] = set()
        for raw_path in raw_paths:
            batch = _validate_finalized_pair(
                run_root,
                run_id,
                raw_path,
            )
            verified_batch_references.append(
                _VerifiedBatch(
                    run_id=batch.run_id,
                    raw_path=batch.raw_path,
                    relative_path=batch.relative_path,
                    source=batch.source,
                    manifest=batch.manifest,
                    rows=(),
                )
            )
            for row_index, row in enumerate(batch.rows):
                record_id = str(row["record_id"])
                source = batch.source
                event = row["payload"]
                event_type = event.get("event_type")
                connection_id = event.get("connection_id")
                is_lifecycle = event.get("kind") == "lifecycle"
                is_replay = (
                    source == "dynamic_short_crypto_service"
                    or is_lifecycle
                )
                finalized_record_index.add(
                    record_id,
                    source=source,
                    row=row,
                    replay_location=(
                        (run_id, batch.relative_path)
                        if is_replay
                        else None
                    ),
                    canonical_replay_row_zlib=(
                        batch.canonical_replay_row_zlibs[row_index]
                        if (
                            row_index
                            < len(batch.canonical_replay_row_zlibs)
                        )
                        else None
                    ),
                )
                if record_id == checkpoint_record_id_hint:
                    finalized_record_ids.add(record_id)
                validated_record_count += 1
                if source in {"gamma_http", "rtds_ws"} or is_lifecycle:
                    record_sources[record_id] = source
                if is_lifecycle:
                    if isinstance(connection_id, str) and connection_id:
                        record_connections[record_id] = connection_id
                    if isinstance(event_type, str):
                        record_event_types[record_id] = event_type
                if source in {"gamma_http", "rtds_ws"}:
                    evidence_locations[record_id] = (
                        run_id,
                        raw_path,
                    )
                    if record_id in required_evidence_ids:
                        records_by_id[record_id] = row
                if not is_replay:
                    continue
                replay_item = (run_id, batch.relative_path, row)
                if source == "dynamic_short_crypto_service":
                    event_payload = event.get("payload")
                    if isinstance(event_payload, Mapping):
                        if event_type == "gamma_target_verified":
                            evidence_record_id = event_payload.get(
                                "gamma_record_id"
                            )
                        elif (
                            event_type
                            == "chainlink_boundary_evidence_committed"
                        ):
                            evidence_record_id = event_payload.get(
                                "evidence_record_id"
                            )
                        else:
                            evidence_record_id = None
                        if (
                            isinstance(evidence_record_id, str)
                            and _CONTENT_HASH.fullmatch(
                                evidence_record_id
                            )
                            is not None
                        ):
                            required_evidence_ids.add(evidence_record_id)
                if not _is_legacy_registry_row(row):
                    replay_records.append(replay_item)
                    continue
                replay_key = _replay_sort_key(replay_item)
                if (
                    latest_legacy_registry_key is None
                    or replay_key > latest_legacy_registry_key
                ):
                    if latest_legacy_registry_index is not None:
                        previous = replay_records[
                            latest_legacy_registry_index
                        ]
                        replay_records[latest_legacy_registry_index] = (
                            previous[0],
                            previous[1],
                            _compact_legacy_registry_row(previous[2]),
                        )
                    latest_legacy_registry_index = len(replay_records)
                    latest_legacy_registry_key = replay_key
                else:
                    replay_item = (
                        run_id,
                        batch.relative_path,
                        _compact_legacy_registry_row(row),
                    )
                replay_records.append(replay_item)
            del batch
            cpu_limiter.checkpoint()
        finalized_record_index.flush()
        checkpoint_audit = _checkpoint_audit(
            run_root,
            artifact=checkpoint_artifact,
            verified_batches=tuple(verified_batch_references),
            finalized_record_ids=finalized_record_ids,
        )
        if held_view is not None:
            root_content_proof = (
                _root_content_commitment_from_view(held_view)
            )
            root_inventory_proof = (
                _root_inventory_descriptor_from_view(held_view)
            )
            _revalidate_held_run_view(held_view)
            validated_input_proofs[run_id] = (
                root_content_proof,
                root_inventory_proof,
            )
            if _input_proof_sink is not None:
                _input_proof_sink[run_id] = (
                    root_content_proof,
                    root_inventory_proof,
                )
        run_inventory = _run_inventory_proof(
            run_id=run_id,
            run_root=run_root,
            checkpoint_audit=checkpoint_audit,
            partial_paths=partial_paths,
            raw_without_manifest=raw_without_manifest,
            manifest_without_raw=manifest_without_raw,
            symlink_artifacts=symlink_artifacts,
        )
        finalized_record_index.add_run_inventory(
            run_id,
            run_inventory,
        )
        base_fields = _checkpoint_base_fields_from_run_inventory(
            (run_inventory,),
            expected_run_ids=(run_id,),
        )
        classifications.extend(base_fields["classifications"])
        gaps.extend(base_fields["gaps"])
        exclusions.extend(base_fields["exclusions"])
        runs_with_orphan_partials.update(
            base_fields["runs_with_orphan_partials"]
        )
        runs_with_input_integrity_gaps.update(
            base_fields["runs_with_input_integrity_gaps"]
        )
        if run_root_proof is not None:
            if held_view is not None:
                _revalidate_held_run_view(held_view)
            _revalidate_run_root_proof(run_root_proof)
        if held_view_lease is not None:
            held_view_lease.close()

    finalized_record_index.flush()
    if root_proofs:
        _revalidate_run_root_proofs(root_proofs)
    replay_records.sort(key=_replay_sort_key)
    recovery_anchor_run_ids: set[str] = set()
    for anchor_run_id, relative_path, row in replay_records:
        anchor = _validated_recovery_anchor_event(
            row,
            run_id=anchor_run_id,
            relative_path=relative_path,
        )
        if anchor is None:
            continue
        if anchor_run_id in recovery_anchor_run_ids:
            raise RecoveryInputError(
                "recovery_anchor_invalid",
                (
                    "a recovery run contains duplicate or branched "
                    f"anchors: {anchor_run_id}"
                ),
            )
        recovery_anchor_run_ids.add(anchor_run_id)
    if latest_legacy_registry_key is not None:
        latest_legacy_registry_index = next(
            (
                index
                for index, item in enumerate(replay_records)
                if _replay_sort_key(item)
                == latest_legacy_registry_key
            ),
            None,
        )
    for _, _, row in replay_records:
        if row.get("source") != "dynamic_short_crypto_service":
            continue
        event = row.get("payload")
        if not isinstance(event, Mapping):
            continue
        event_payload = event.get("payload")
        if not isinstance(event_payload, Mapping):
            continue
        event_type = event.get("event_type")
        if event_type == "gamma_target_verified":
            evidence_record_id = event_payload.get("gamma_record_id")
        elif event_type == "chainlink_boundary_evidence_committed":
            evidence_record_id = event_payload.get("evidence_record_id")
        elif event_type == "settlement_reconciled":
            gamma_record_ids = event_payload.get("gamma_record_ids")
            if isinstance(gamma_record_ids, list):
                required_source_ids.update(
                    record_id
                    for record_id in gamma_record_ids
                    if (
                        isinstance(record_id, str)
                        and _CONTENT_HASH.fullmatch(record_id) is not None
                    )
                )
            close_liveness = event_payload.get("close_liveness")
            if isinstance(close_liveness, Mapping):
                required_source_ids.update(
                    record_id
                    for record_id in (
                        close_liveness.get("before_record_id"),
                        close_liveness.get("after_record_id"),
                    )
                    if (
                        isinstance(record_id, str)
                        and _CONTENT_HASH.fullmatch(record_id) is not None
                    )
                )
            continue
        else:
            continue
        if (
            isinstance(evidence_record_id, str)
            and _CONTENT_HASH.fullmatch(evidence_record_id) is not None
        ):
            required_evidence_ids.add(evidence_record_id)
            required_source_ids.add(evidence_record_id)
    wanted_by_batch: dict[tuple[str, Path], set[str]] = {}
    for record_id in required_evidence_ids:
        if record_id in records_by_id:
            continue
        location = evidence_locations.get(record_id)
        if location is not None:
            wanted_by_batch.setdefault(location, set()).add(record_id)
    for location in sorted(
        wanted_by_batch,
        key=lambda item: (item[0], item[1].as_posix()),
    ):
        run_id, raw_path = location
        run_root = root_by_run_id[run_id]
        wanted_ids = wanted_by_batch[location]
        run_root_proof = root_proofs.get(run_id)
        view: _HeldRunView | None = None
        lease: _HeldRunViewLease | None = None
        if run_root_proof is not None:
            _revalidate_run_root_proof(run_root_proof)
            view = _open_held_run_view(run_root_proof)
            lease = _HeldRunViewLease(
                view=view,
                token=_ACTIVE_HELD_RUN_VIEW.set(view),
            )
        try:
            if view is not None:
                expected_proof = validated_input_proofs.get(run_id)
                if (
                    expected_proof is None
                    or _root_inventory_descriptor_from_view(view)
                    != expected_proof[1]
                ):
                    raise RecoveryInputError(
                        "recovery_snapshot_input_drift",
                        (
                            "evidence root inventory changed after "
                            f"replay: {run_id}"
                        ),
                    )
            batch = _validate_finalized_pair(
                run_root,
                run_id,
                raw_path,
            )
            if view is not None:
                _revalidate_held_run_view(view)
                if (
                    _root_inventory_descriptor_from_view(view)
                    != validated_input_proofs[run_id][1]
                ):
                    raise RecoveryInputError(
                        "recovery_snapshot_input_drift",
                        (
                            "evidence root inventory changed while "
                            f"reread: {run_id}"
                        ),
                    )
        finally:
            if lease is not None:
                lease.close()
        for row in batch.rows:
            record_id = str(row["record_id"])
            if record_id in wanted_ids:
                records_by_id[record_id] = row
        del batch
        cpu_limiter.checkpoint()
    for record_id in sorted(required_source_ids):
        if record_id in record_sources:
            continue
        source = finalized_record_index.source_for(record_id)
        if source is not None:
            record_sources[record_id] = source
    for record_id in sorted(required_evidence_ids):
        if record_id in records_by_id:
            continue
        evidence_row = finalized_record_index.evidence_row(record_id)
        if evidence_row is not None:
            records_by_id[record_id] = evidence_row
    checkpoint_source_ids = (
        required_source_ids
        | set(record_connections)
        | set(record_event_types)
    )
    checkpoint_record_sources = {
        record_id: record_sources[record_id]
        for record_id in sorted(checkpoint_source_ids)
        if record_id in record_sources
    }
    replay_checkpoint = (
        None
        if _checkpoint_sink is None
        else _RecoveryReplayCheckpoint(
            run_ids=run_ids,
            replay_records=tuple(
                (run_id, relative_path, deepcopy(row))
                for run_id, relative_path, row in replay_records
            ),
            record_sources=checkpoint_record_sources,
            records_by_id=deepcopy(records_by_id),
            record_connections=deepcopy(record_connections),
            record_event_types=deepcopy(record_event_types),
            validated_record_count=validated_record_count,
            classifications=tuple(deepcopy(classifications)),
            gaps=tuple(deepcopy(gaps)),
            exclusions=tuple(deepcopy(exclusions)),
            runs_with_orphan_partials=tuple(
                sorted(runs_with_orphan_partials)
            ),
            runs_with_input_integrity_gaps=tuple(
                sorted(runs_with_input_integrity_gaps)
            ),
            latest_legacy_registry_index=(
                latest_legacy_registry_index
            ),
            latest_legacy_registry_key=latest_legacy_registry_key,
        )
    )
    finalized_record_index.close()
    if replay_checkpoint is not None:
        _checkpoint_sink.append(replay_checkpoint)
    descriptors: dict[str, dict[str, Any]] = {}
    registry_hash: str | None = None
    registry_snapshot: dict[str, Any] | None = None
    targets: dict[str, dict[str, Any]] = {}
    target_run_ids: dict[str, set[str]] = {}
    revision_record_ids: list[str] = []
    decisions: dict[str, dict[str, Any]] = {}
    decision_run_ids: dict[str, str] = {}
    start_receipts: dict[str, dict[str, Any]] = {}
    stopped_group_ids: set[str] = set()
    stop_failed_groups: dict[str, str] = {}
    interruptions: dict[str, dict[str, Any]] = {}
    interrupted_run_ids: set[str] = set()
    gamma_evidence: dict[str, list[dict[str, Any]]] = {}
    latest_gamma_records: dict[str, dict[str, Any]] = {}
    chainlink_evidence: dict[
        str, dict[str, list[dict[str, Any]]]
    ] = {}
    settlements: dict[str, dict[str, Any]] = {}
    worker_liveness: dict[str, dict[str, Any]] = {}
    recovered_cohort_events: list[_RecoveredCohortEvent] = []
    max_decision_sequence = 0
    for run_id, relative_path, row in replay_records:
        event = row["payload"]
        event_type = event.get("event_type")
        event_payload = event.get("payload")
        connection_id = event.get("connection_id")
        relative_parts = Path(relative_path).parts
        group_id_from_path: str | None = None
        if "groups" in relative_parts:
            group_index = relative_parts.index("groups")
            if group_index + 1 < len(relative_parts):
                group_id_from_path = relative_parts[group_index + 1]
        if (
            group_id_from_path is not None
            and event.get("kind") == "lifecycle"
            and isinstance(event_type, str)
        ):
            liveness = worker_liveness.setdefault(
                group_id_from_path,
                {
                    "connection_ids": set(),
                    "disconnect_record_ids": [],
                    "lifecycle_record_ids": [],
                    "latest_event_type": None,
                },
            )
            if isinstance(connection_id, str) and connection_id:
                liveness["connection_ids"].add(connection_id)
            liveness["lifecycle_record_ids"].append(
                str(row["record_id"])
            )
            liveness["latest_event_type"] = event_type
            if event_type == "disconnected":
                liveness["disconnect_record_ids"].append(
                    str(row["record_id"])
                )
        if row.get("source") != "dynamic_short_crypto_service":
            continue
        if (
            event.get("schema_version")
            != "edge-lab-dynamic-short-crypto-service.record.v1"
            or event.get("source") != "dynamic_short_crypto_service"
        ):
            raise RecoveryInputError(
                "service_record_schema_invalid",
                f"record {row['record_id']} has an incompatible service schema",
            )
        if event_type in {
            "lifecycle_cohort_committed",
            "lifecycle_remediation_cohort_committed",
        }:
            cohort_raw_path = Path(relative_path)
            source_path = cohort_raw_path.parent
            if (
                group_id_from_path is not None
                or source_path.name != "dynamic_short_crypto_service"
                or source_path.parent.name != "raw"
                or source_path.parent.parent.name != "control"
            ):
                raise RecoveryInputError(
                    "lifecycle_cohort_journal_invalid",
                    "cohort commitments must be finalized in control",
                )
            record_id = str(row["record_id"])
            received_at_ms = _cohort_received_at_ms(
                row.get("received_at"),
                record_id=record_id,
            )
            cohort_payload = _validated_cohort_event(
                str(event_type),
                event_payload,
                outer_kind=event.get("kind"),
                record_id=record_id,
                received_at_ms=received_at_ms,
                settlement_timeout_ms=settlement_timeout_ms,
            )
            recovered_cohort_events.append(
                _RecoveredCohortEvent(
                    event_type=str(event_type),
                    record_id=record_id,
                    run_id=run_id,
                    manifest_path=(
                        root_by_run_id[run_id]
                        / cohort_raw_path.with_suffix(".manifest.json")
                    ),
                    received_at_ms=received_at_ms,
                    payload=cohort_payload,
                )
            )
        elif event_type == "capture_decision":
            decision = _validated_decision(
                event_payload,
                record_id=str(row["record_id"]),
            )
            decision_id = decision["decision_id"]
            existing = decisions.get(decision_id)
            if existing is not None and existing != decision:
                raise RecoveryInputError(
                    "capture_decision_conflict",
                    f"decision {decision_id} changed across finalized records",
                )
            decisions[decision_id] = decision
            decision_run_ids[decision_id] = run_id
            max_decision_sequence = max(
                max_decision_sequence,
                decision["sequence"],
            )
        elif event_type == "worker_start_applied":
            if (
                not isinstance(event_payload, dict)
                or not isinstance(event_payload.get("decision_id"), str)
                or _CONTENT_HASH.fullmatch(
                    event_payload["decision_id"]
                )
                is None
                or not isinstance(event_payload.get("group_id"), str)
                or not event_payload["group_id"]
            ):
                raise RecoveryInputError(
                    "worker_receipt_invalid",
                    f"record {row['record_id']} has an invalid start receipt",
                )
            start_receipts[event_payload["decision_id"]] = {
                "start_record_id": str(row["record_id"]),
                "run_id": run_id,
                "group_id": event_payload.get("group_id"),
                "status": "start_applied",
            }
        elif event_type == "worker_stopped":
            if not isinstance(event_payload, dict) or not isinstance(
                event_payload.get("group_id"), str
            ):
                raise RecoveryInputError(
                    "worker_receipt_invalid",
                    f"record {row['record_id']} has an invalid stop receipt",
                )
            stopped_group_ids.add(event_payload["group_id"])
        elif event_type == "worker_stop_failed":
            if not isinstance(event_payload, dict) or not isinstance(
                event_payload.get("group_id"), str
            ):
                raise RecoveryInputError(
                    "worker_receipt_invalid",
                    f"record {row['record_id']} has invalid stop failure data",
                )
            stop_failed_groups[event_payload["group_id"]] = run_id
        elif event_type == "target_capture_interrupted":
            if (
                not isinstance(event_payload, dict)
                or not isinstance(event_payload.get("slug"), str)
                or not isinstance(event_payload.get("reason"), str)
            ):
                raise RecoveryInputError(
                    "target_interruption_invalid",
                    f"record {row['record_id']} has invalid interruption data",
                )
            interruptions[event_payload["slug"]] = {
                "reason": event_payload["reason"],
                "record_id": str(row["record_id"]),
                "run_id": run_id,
                "observed_at_ms": event_payload.get("scheduler_now_ms"),
            }
            interrupted_run_ids.add(run_id)
        elif event_type == "gamma_target_verified":
            if (
                not isinstance(event_payload, dict)
                or not isinstance(event_payload.get("slug"), str)
                or not isinstance(
                    event_payload.get("gamma_record_id"), str
                )
                or _CONTENT_HASH.fullmatch(
                    event_payload["gamma_record_id"]
                )
                is None
            ):
                raise RecoveryInputError(
                    "gamma_evidence_invalid",
                    f"record {row['record_id']} has invalid Gamma evidence",
                )
            gamma_record_id = event_payload["gamma_record_id"]
            if record_sources.get(gamma_record_id) != "gamma_http":
                raise RecoveryInputError(
                    "gamma_evidence_not_finalized",
                    f"Gamma evidence {gamma_record_id} is not finalized",
                )
            gamma_evidence.setdefault(
                event_payload["slug"], []
            ).append(
                {
                    "control_record_id": str(row["record_id"]),
                    "gamma_record_id": gamma_record_id,
                    "market_id": event_payload.get("market_id"),
                    "status": "verified",
                }
            )
            latest_gamma_records[event_payload["slug"]] = deepcopy(
                records_by_id[gamma_record_id]
            )
        elif event_type == "chainlink_boundary_evidence_committed":
            if (
                not isinstance(event_payload, dict)
                or not isinstance(event_payload.get("slug"), str)
                or event_payload.get("boundary_role")
                not in {"open", "close"}
                or not isinstance(
                    event_payload.get("evidence_record_id"), str
                )
                or _CONTENT_HASH.fullmatch(
                    event_payload["evidence_record_id"]
                )
                is None
            ):
                raise RecoveryInputError(
                    "chainlink_evidence_invalid",
                    f"record {row['record_id']} has invalid boundary evidence",
                )
            evidence_record_id = event_payload["evidence_record_id"]
            if record_sources.get(evidence_record_id) != "rtds_ws":
                raise RecoveryInputError(
                    "chainlink_evidence_not_finalized",
                    f"Chainlink evidence {evidence_record_id} is not finalized",
                )
            by_role = chainlink_evidence.setdefault(
                event_payload["slug"],
                {"open": [], "close": []},
            )
            by_role[event_payload["boundary_role"]].append(
                {
                    "control_record_id": str(row["record_id"]),
                    "evidence_record_id": evidence_record_id,
                    "evidence_record": deepcopy(
                        records_by_id[evidence_record_id]
                    ),
                }
            )
        elif event_type == "settlement_reconciled":
            if (
                not isinstance(event_payload, dict)
                or not isinstance(event_payload.get("slug"), str)
                or not isinstance(event_payload.get("status"), str)
            ):
                raise RecoveryInputError(
                    "settlement_record_invalid",
                    f"record {row['record_id']} has invalid settlement evidence",
                )
            settlements[event_payload["slug"]] = {
                "record_id": str(row["record_id"]),
                "run_id": run_id,
                "payload": deepcopy(event_payload),
            }
        if (
            event.get("schema_version")
            != "edge-lab-dynamic-short-crypto-service.record.v1"
            or event_type != "registry_revision"
        ):
            continue
        payload = event_payload
        if not isinstance(payload, dict):
            raise RecoveryInputError(
                "service_record_schema_invalid",
                f"registry record {row['record_id']} has no payload",
            )
        if (
            row.get("_recovery_compacted_after_validation") is True
            and payload.get("schema_version")
            == (
                "edge-lab-short-crypto-registry-"
                "recovery-compacted.v1"
            )
        ):
            snapshot_sha = payload.get("snapshot_sha256")
            target_slugs = payload.get("target_slugs")
            if (
                not isinstance(snapshot_sha, str)
                or _CONTENT_HASH.fullmatch(snapshot_sha) is None
                or not isinstance(target_slugs, list)
                or not all(
                    isinstance(slug, str) and slug
                    for slug in target_slugs
                )
            ):
                raise RecoveryInputError(
                    "registry_revision_invalid",
                    f"registry record {row['record_id']} compact state is invalid",
                )
            for slug in target_slugs:
                target_run_ids.setdefault(slug, set()).add(run_id)
            revision_record_ids.append(str(row["record_id"]))
            continue
        if (
            payload.get("schema_version")
            == "edge-lab-short-crypto-registry-revision.v2"
        ):
            allowed = {
                "schema_version",
                "delta_snapshot",
                "cumulative_snapshot_sha256",
                "cumulative_input_file_count",
                "cumulative_target_count",
                "scheduler_now_ms",
            }
            if set(payload) - allowed:
                raise RecoveryInputError(
                    "registry_revision_invalid",
                    f"registry record {row['record_id']} has unknown fields",
                )
            delta_value = payload.get("delta_snapshot")
            if not isinstance(delta_value, Mapping):
                raise RecoveryInputError(
                    "registry_revision_invalid",
                    f"registry record {row['record_id']} has no delta",
                )
            try:
                delta = validate_short_crypto_registry_snapshot(
                    delta_value
                )
                cumulative = (
                    delta
                    if registry_snapshot is None
                    else merge_short_crypto_registry_snapshots(
                        registry_snapshot,
                        delta,
                    )
                )
            except ValueError as exc:
                raise RecoveryInputError(
                    "registry_revision_invalid",
                    f"registry record {row['record_id']} has invalid delta",
                ) from exc
            snapshot_sha = payload.get(
                "cumulative_snapshot_sha256"
            )
            if (
                snapshot_sha != cumulative["snapshot_sha256"]
                or payload.get("cumulative_input_file_count")
                != len(cumulative["inputs"])
                or payload.get("cumulative_target_count")
                != len(cumulative["targets"])
            ):
                raise RecoveryInputError(
                    "registry_revision_cumulative_mismatch",
                    f"registry record {row['record_id']} cumulative proof failed",
                )
            inputs = delta["inputs"]
            target_values = delta["targets"]
            registry_snapshot = cumulative
        else:
            snapshot_sha = payload.get("snapshot_sha256")
            inputs = payload.get("inputs")
            target_values = payload.get("targets")
            registry_snapshot = {
                key: deepcopy(payload[key])
                for key in (
                    "schema_version",
                    "inputs",
                    "targets",
                    "rejections",
                    "completeness",
                    "snapshot_sha256",
                )
                if key in payload
            }
        if (
            not isinstance(snapshot_sha, str)
            or _CONTENT_HASH.fullmatch(snapshot_sha) is None
        ):
            raise RecoveryInputError(
                "registry_revision_invalid",
                f"registry record {row['record_id']} has no snapshot hash",
            )
        if not isinstance(inputs, list) or not isinstance(target_values, list):
            raise RecoveryInputError(
                "registry_revision_invalid",
                f"registry record {row['record_id']} has invalid collections",
            )
        for value in inputs:
            item = _descriptor(value, record_id=str(row["record_id"]))
            existing = descriptors.get(item["path"])
            if existing is not None and existing != item:
                raise RecoveryInputError(
                    "registry_descriptor_conflict",
                    f"discovery descriptor changed for {item['path']}",
                )
            descriptors[item["path"]] = item
        for target in target_values:
            if not isinstance(target, dict) or not isinstance(
                target.get("slug"), str
            ):
                raise RecoveryInputError(
                    "registry_target_invalid",
                    f"registry record {row['record_id']} has an invalid target",
                )
            targets[target["slug"]] = deepcopy(target)
            target_run_ids.setdefault(target["slug"], set()).add(run_id)
        if registry_snapshot is not None:
            targets = {
                target["slug"]: deepcopy(target)
                for target in registry_snapshot["targets"]
                if isinstance(target, dict)
                and isinstance(target.get("slug"), str)
            }
        registry_hash = snapshot_sha
        revision_record_ids.append(str(row["record_id"]))

    missing_receipts: list[dict[str, Any]] = []
    for decision_id in sorted(decisions):
        if decision_id in start_receipts:
            continue
        decision = decisions[decision_id]
        if decision["action"] != "subscribe":
            continue
        run_id = decision_run_ids[decision_id]
        gap = {
            "code": "decision_without_receipt",
            "run_id": run_id,
            "decision_id": decision_id,
            "group_id": decision["group_id"],
        }
        gaps.append(gap)
        missing_receipts.append(gap)
        for classification in classifications:
            if classification["run_id"] != run_id:
                continue
            classification["conditions"].append(
                "incomplete_decision_without_receipt"
            )
            if (
                classification["classification"]
                != "crash_with_orphan_partial"
            ):
                classification["classification"] = (
                    "incomplete_decision_without_receipt"
                )

    unrecoverable_receipts: list[dict[str, Any]] = []
    for decision_id in sorted(start_receipts):
        receipt = start_receipts[decision_id]
        group_id = receipt["group_id"]
        if decision_id not in decisions:
            run_id = receipt["run_id"]
            receipt["status"] = "decision_missing"
            gap = {
                "code": "receipt_without_decision",
                "run_id": run_id,
                "decision_id": decision_id,
                "group_id": group_id,
            }
            gaps.append(gap)
            exclusions.append(
                {
                    "code": "receipt_without_decision",
                    "run_id": run_id,
                    "group_id": group_id,
                    "recovery": (
                        "scope_not_recoverable_from_finalized_decision"
                    ),
                }
            )
            for classification in classifications:
                if classification["run_id"] != run_id:
                    continue
                classification["conditions"].append(
                    "receipt_without_decision"
                )
                if (
                    classification["classification"]
                    != "crash_with_orphan_partial"
                ):
                    classification["classification"] = (
                        "receipt_without_recoverable_worker"
                    )
            continue
        if group_id in stopped_group_ids:
            receipt["status"] = "stopped"
            continue
        run_id = receipt["run_id"]
        if group_id in stop_failed_groups:
            receipt["status"] = "stop_failed"
            gap = {
                "code": "stubborn_worker_shutdown_unconfirmed",
                "run_id": run_id,
                "decision_id": decision_id,
                "group_id": group_id,
            }
            gaps.append(gap)
            exclusions.append(
                {
                    "code": "stubborn_worker_shutdown_unconfirmed",
                    "run_id": run_id,
                    "group_id": group_id,
                    "recovery": (
                        "manual_process_ownership_revalidation_required"
                    ),
                }
            )
            for classification in classifications:
                if classification["run_id"] != run_id:
                    continue
                classification["conditions"].append(
                    "stubborn_worker_shutdown_unconfirmed"
                )
                if (
                    classification["classification"]
                    != "crash_with_orphan_partial"
                ):
                    classification["classification"] = (
                        "stubborn_worker_shutdown_unconfirmed"
                    )
            continue
        gap = {
            "code": "receipt_without_recoverable_worker",
            "run_id": run_id,
            "decision_id": decision_id,
            "group_id": group_id,
        }
        gaps.append(gap)
        unrecoverable_receipts.append(gap)
        for classification in classifications:
            if classification["run_id"] != run_id:
                continue
            classification["conditions"].append(
                "receipt_without_recoverable_worker"
            )
            if (
                classification["classification"]
                != "crash_with_orphan_partial"
            ):
                classification["classification"] = (
                    "receipt_without_recoverable_worker"
                )

    for classification in classifications:
        if classification["run_id"] not in interrupted_run_ids:
            continue
        classification["conditions"].append("clean_interrupted")
        if classification["classification"] == "clean_completed":
            classification["classification"] = "clean_interrupted"

    recovered_targets: dict[str, dict[str, Any]] = {}
    for slug in sorted(targets):
        target_state = {
            "identity": targets[slug],
            "state": "announced",
            "pending_settlement_deadline_ms": (
                targets[slug].get("closes_at_ms", 0)
                + settlement_timeout_ms
            ),
        }
        interruption = interruptions.get(slug)
        if interruption is not None:
            target_state["state"] = "excluded"
            target_state["exclusion_reason"] = interruption["reason"]
            target_state["exclusion_record_id"] = interruption["record_id"]
            if (
                isinstance(interruption.get("observed_at_ms"), int)
                and not isinstance(
                    interruption.get("observed_at_ms"), bool
                )
            ):
                target_state["exclusion_observed_at_ms"] = interruption[
                    "observed_at_ms"
                ]
        elif target_run_ids.get(slug, set()) & runs_with_orphan_partials:
            target_state["state"] = "reacquire_required"
            target_state["exclusion_reason"] = (
                "orphan_partial_not_recovered"
            )
        elif (
            target_run_ids.get(slug, set())
            & runs_with_input_integrity_gaps
        ):
            target_state["state"] = "reacquire_required"
            target_state["exclusion_reason"] = (
                "prior_run_finalized_input_integrity_gap"
            )
        elif slug in settlements:
            settlement = settlements[slug]
            payload = settlement["payload"]
            close_liveness = payload.get("close_liveness")
            before_id = (
                close_liveness.get("before_record_id")
                if isinstance(close_liveness, dict)
                else None
            )
            after_id = (
                close_liveness.get("after_record_id")
                if isinstance(close_liveness, dict)
                else None
            )
            expected_connection = (
                close_liveness.get("connection_id")
                if isinstance(close_liveness, dict)
                else None
            )
            liveness_valid = (
                isinstance(before_id, str)
                and isinstance(after_id, str)
                and isinstance(expected_connection, str)
                and record_sources.get(before_id) == "clob_market_ws"
                and record_sources.get(after_id) == "clob_market_ws"
                and record_event_types.get(before_id) == "heartbeat_ack"
                and record_event_types.get(after_id) == "heartbeat_ack"
                and record_connections.get(before_id)
                == expected_connection
                and record_connections.get(after_id)
                == expected_connection
            )
            boundaries = chainlink_evidence.get(
                slug, {"open": [], "close": []}
            )
            gamma_ids = payload.get("gamma_record_ids")
            gamma_valid = (
                isinstance(gamma_ids, list)
                and bool(gamma_ids)
                and all(
                    record_sources.get(item) == "gamma_http"
                    for item in gamma_ids
                )
            )
            settlement_valid = (
                payload.get("status") == "resolved"
                and payload.get("exclusion_reason") is None
                and payload.get("chainlink_boundary_status")
                in {"verified", "committed", "resolved"}
                and bool(boundaries["open"])
                and bool(boundaries["close"])
                and gamma_valid
                and liveness_valid
            )
            if settlement_valid:
                target_state["state"] = "settled"
                target_state["pending_settlement_deadline_ms"] = None
                target_state["settlement_record_id"] = settlement[
                    "record_id"
                ]
                target_state["settlement"] = {
                    "status": payload["status"],
                    "outcome": payload.get("outcome"),
                    "available_at_ms": payload.get("available_at_ms"),
                    "record_id": settlement["record_id"],
                }
            else:
                target_state["state"] = "excluded"
                target_state["exclusion_reason"] = (
                    "settlement_not_recoverable_from_finalized_evidence"
                )
                target_state["exclusion_record_id"] = settlement[
                    "record_id"
                ]
                gaps.append(
                    {
                        "code": "settlement_evidence_gap",
                        "run_id": settlement["run_id"],
                        "slug": slug,
                        "settlement_record_id": settlement["record_id"],
                    }
                )
        elif slug in gamma_evidence:
            target_state["state"] = "gamma_verified"
        recovered_targets[slug] = target_state

    serialized_liveness = {
        group_id: {
            "connection_ids": sorted(value["connection_ids"]),
            "disconnect_record_ids": value["disconnect_record_ids"],
            "lifecycle_record_ids": value["lifecycle_record_ids"],
            "latest_event_type": value["latest_event_type"],
        }
        for group_id, value in sorted(worker_liveness.items())
    }

    lifecycle_cohort_journal = _validated_cohort_journal(
        recovered_cohort_events
    )
    state = {
        "schema_version": STATE_SCHEMA,
        "processed_finalized_discovery_descriptors": [
            descriptors[path] for path in sorted(descriptors)
        ],
        "registry": {
            "snapshot_sha256": registry_hash,
            "revision_record_ids": revision_record_ids,
            "snapshot": registry_snapshot,
        },
        "targets": recovered_targets,
        "gamma_evidence": {
            slug: gamma_evidence[slug] for slug in sorted(gamma_evidence)
        },
        "latest_gamma_records": {
            slug: latest_gamma_records[slug]
            for slug in sorted(latest_gamma_records)
        },
        "chainlink_evidence": {
            slug: chainlink_evidence[slug]
            for slug in sorted(chainlink_evidence)
        },
        "subscription_decisions": {
            decision_id: decisions[decision_id]
            for decision_id in sorted(decisions)
        },
        "worker_receipts": {
            decision_id: start_receipts[decision_id]
            for decision_id in sorted(start_receipts)
        },
        "worker_liveness": serialized_liveness,
        "lifecycle_cohort_journal": lifecycle_cohort_journal,
    }
    state_hash = hashlib.sha256(canonical_json_bytes(state)).hexdigest()
    callback_generation = max_decision_sequence + 1
    decision_body = {
        "schema_version": DECISION_SCHEMA,
        "recovered_from_run_ids": [run_id for run_id, _ in roots],
        "replayed_record_count": validated_record_count,
        "state_hash": state_hash,
        "gap_codes": sorted({item["code"] for item in gaps}),
        "exclusion_codes": sorted(
            {item["code"] for item in exclusions}
        ),
        "callback_generation": callback_generation,
        "must_be_durable_before_worker_actions": True,
    }
    decision = {
        **decision_body,
        "decision_id": hashlib.sha256(
            canonical_json_bytes(decision_body)
        ).hexdigest(),
    }
    worker_recovery_gaps = [
        *missing_receipts,
        *unrecoverable_receipts,
    ]
    worker_actions = tuple(
        {
            "action": "start_public_worker_after_revalidation",
            "reason": gap["code"],
            "decision_id": gap["decision_id"],
            "group_id": gap["group_id"],
            "asset_ids": deepcopy(
                decisions[gap["decision_id"]]["asset_ids"]
            ),
            "target_slugs": [
                target["slug"]
                for target in decisions[gap["decision_id"]]["targets"]
            ],
            "generation": callback_generation,
            "blocked_until_recovery_decision_id": decision["decision_id"],
        }
        for gap in worker_recovery_gaps
    )
    return RecoveryResult(
        recovered_from_run_ids=tuple(run_id for run_id, _ in roots),
        replayed_record_count=validated_record_count,
        state_hash=state_hash,
        gaps=tuple(gaps),
        exclusions=tuple(exclusions),
        run_classifications=tuple(classifications),
        state=state,
        recovery_decision=decision,
        worker_actions=worker_actions,
        callback_generation=callback_generation,
    )


def recover_dynamic_short_crypto_runs(
    prior_run_roots: Iterable[str | Path] | str | Path,
    *,
    settlement_timeout_ms: int,
    cpu_ratio: float | None = None,
) -> RecoveryResult:
    """Validate and replay prior immutable runs without starting workers."""

    roots = _run_roots(prior_run_roots)
    checkpoint_sink: list[_RecoveryReplayCheckpoint] = []
    result = _recover_dynamic_short_crypto_runs(
        tuple(path for _, path in roots),
        settlement_timeout_ms=settlement_timeout_ms,
        cpu_ratio=cpu_ratio,
        _checkpoint_sink=checkpoint_sink,
    )
    if len(checkpoint_sink) != 1:
        raise RecoveryInputError(
            "recovery_replay_checkpoint_invalid",
            "full replay did not produce one checkpoint",
        )
    return _validate_recovery_result_deep(
        result,
        checkpoint_sink[0],
        roots=roots,
        settlement_timeout_ms=settlement_timeout_ms,
    )


def _recovery_validator_contract() -> str:
    files = (
        Path(__file__).resolve(),
        Path(__file__).with_name("data_store.py").resolve(),
        Path(__file__).with_name(
            "lifecycle_cohort_contract.py"
        ).resolve(),
        Path(__file__).with_name("short_crypto_registry.py").resolve(),
    )
    descriptors = []
    for path in files:
        digest, byte_count, _ = _stable_digest(
            path,
            code="recovery_validator_unreadable",
            count_lines=False,
        )
        descriptors.append(
            {
                "path": path.name,
                "sha256": digest,
                "bytes": byte_count,
            }
        )
    body = {
        "schema_version": VALIDATOR_CONTRACT_SCHEMA,
        "files": descriptors,
    }
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def _relative_artifact(path: Path, *, run_root: Path) -> str:
    try:
        return path.relative_to(run_root).as_posix()
    except ValueError as exc:
        raise RecoveryInputError(
            "recovery_input_inventory_invalid",
            f"artifact escapes prior run root: {path}",
        ) from exc


def _root_inventory_descriptor_from_view(
    view: _HeldRunView,
) -> dict[str, Any]:
    selected = [
        artifact
        for artifact in view.artifacts.values()
        if (
            artifact.relative_path.endswith(".jsonl")
            or artifact.relative_path.endswith(".manifest.json")
            or artifact.relative_path.endswith(".jsonl.partial")
            or artifact.relative_path
            == "control/checkpoints/dynamic_short_crypto_service.json"
        )
    ]
    artifacts: list[dict[str, Any]] = []
    for artifact in sorted(
        selected,
        key=lambda item: item.relative_path,
    ):
        relative = artifact.relative_path
        if relative.endswith(".jsonl.partial"):
            artifacts.append(
                {
                    "kind": "mutable_partial_path",
                    "path": relative,
                }
            )
            continue
        identity = artifact.identity
        artifacts.append(
            {
                "kind": (
                    "finalized_raw"
                    if relative.endswith(".jsonl")
                    else (
                        "finalized_manifest"
                        if relative.endswith(".manifest.json")
                        else "checkpoint"
                    )
                ),
                "path": relative,
                "mode": stat.S_IMODE(identity[2]),
                "size": identity[3],
                "mtime_ns": identity[4],
                "ctime_ns": identity[5],
                "device": identity[0],
                "inode": identity[1],
                "symlink": stat.S_ISLNK(identity[2]),
            }
        )
    body = {
        "schema_version": ROOT_INVENTORY_SCHEMA,
        "run_id": view.run_id,
        "artifacts": artifacts,
    }
    return {
        "schema_version": ROOT_INVENTORY_SCHEMA,
        "run_id": view.run_id,
        "commitment": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
        "artifact_count": len(artifacts),
    }


def _root_content_commitment_from_view(
    view: _HeldRunView,
) -> dict[str, Any]:
    raw_artifacts = {
        relative: artifact
        for relative, artifact in view.artifacts.items()
        if relative.endswith(".jsonl")
    }
    manifest_artifacts = {
        relative: artifact
        for relative, artifact in view.artifacts.items()
        if relative.endswith(".manifest.json")
    }
    regular_raw_paths = {
        relative
        for relative, artifact in raw_artifacts.items()
        if stat.S_ISREG(artifact.identity[2])
    }
    regular_manifest_paths = {
        relative
        for relative, artifact in manifest_artifacts.items()
        if stat.S_ISREG(artifact.identity[2])
    }
    paired_raw_paths = tuple(
        sorted(
            relative
            for relative in regular_raw_paths
            if Path(relative).with_suffix(
                ".manifest.json"
            ).as_posix()
            in regular_manifest_paths
        )
    )
    raw_without_manifest = tuple(
        sorted(
            relative
            for relative in regular_raw_paths
            if Path(relative).with_suffix(
                ".manifest.json"
            ).as_posix()
            not in regular_manifest_paths
        )
    )
    manifest_without_raw = tuple(
        sorted(
            relative
            for relative in regular_manifest_paths
            if (
                Path(relative).with_name(
                    Path(relative).name.removesuffix(
                        ".manifest.json"
                    )
                    + ".jsonl"
                ).as_posix()
                not in regular_raw_paths
            )
        )
    )
    symlink_artifacts = tuple(
        sorted(
            relative
            for relative, artifact in {
                **raw_artifacts,
                **manifest_artifacts,
            }.items()
            if stat.S_ISLNK(artifact.identity[2])
        )
    )
    partial_paths = tuple(
        sorted(
            relative
            for relative in view.artifacts
            if relative.endswith(".jsonl.partial")
        )
    )
    artifacts: list[dict[str, Any]] = []
    finalized_raw_bytes = 0
    finalized_raw_lines = 0
    for raw_relative in paired_raw_paths:
        manifest_relative = Path(raw_relative).with_suffix(
            ".manifest.json"
        ).as_posix()
        raw_stats = view.content_stats.get(raw_relative)
        manifest_stats = view.content_stats.get(manifest_relative)
        if raw_stats is None or manifest_stats is None:
            raise RecoveryInputError(
                "recovery_snapshot_input_drift",
                (
                    "replayed pair has no descriptor-bound content proof: "
                    f"{view.run_id}/{raw_relative}"
                ),
            )
        artifacts.append(
            {
                "kind": "finalized_pair",
                "raw_path": raw_relative,
                "raw_sha256": raw_stats[0],
                "raw_bytes": raw_stats[1],
                "raw_lines": raw_stats[2],
                "manifest_path": manifest_relative,
                "manifest_sha256": manifest_stats[0],
                "manifest_bytes": manifest_stats[1],
            }
        )
        finalized_raw_bytes += raw_stats[1]
        finalized_raw_lines += raw_stats[2]
    for kind, paths in (
        ("raw_without_manifest", raw_without_manifest),
        ("manifest_without_raw", manifest_without_raw),
        ("symlink_artifact", symlink_artifacts),
        ("orphan_partial_path", partial_paths),
    ):
        artifacts.extend(
            {"kind": kind, "path": relative}
            for relative in paths
        )
    checkpoint_relative = (
        "control/checkpoints/dynamic_short_crypto_service.json"
    )
    checkpoint = view.artifacts.get(checkpoint_relative)
    if checkpoint is not None:
        if not stat.S_ISREG(checkpoint.identity[2]):
            raise RecoveryInputError(
                "recovery_snapshot_checkpoint_unreadable",
                (
                    "checkpoint is not a regular descriptor-bound file: "
                    f"{view.run_id}/{checkpoint_relative}"
                ),
            )
        checkpoint_stats = view.content_stats.get(checkpoint_relative)
        if checkpoint_stats is None:
            raise RecoveryInputError(
                "recovery_snapshot_checkpoint_unreadable",
                (
                    "checkpoint has no descriptor-bound content proof: "
                    f"{view.run_id}/{checkpoint_relative}"
                ),
            )
        artifacts.append(
            {
                "kind": "checkpoint",
                "path": checkpoint_relative,
                "sha256": checkpoint_stats[0],
                "bytes": checkpoint_stats[1],
            }
        )
    artifacts.sort(
        key=lambda item: (
            str(item["kind"]),
            str(item.get("path", item.get("raw_path", ""))),
        )
    )
    body = {
        "schema_version": ROOT_CONTENT_COMMITMENT_SCHEMA,
        "run_id": view.run_id,
        "artifacts": artifacts,
    }
    return {
        "schema_version": ROOT_CONTENT_COMMITMENT_SCHEMA,
        "run_id": view.run_id,
        "commitment": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
        "artifact_count": len(artifacts),
        "finalized_pair_count": len(paired_raw_paths),
        "finalized_raw_bytes": finalized_raw_bytes,
        "finalized_raw_lines": finalized_raw_lines,
    }


def _root_inventory_descriptor(
    run_id: str,
    run_root: Path,
    *,
    root_proof: _RunRootProof | None = None,
) -> dict[str, Any]:
    active_view = _ACTIVE_HELD_RUN_VIEW.get()
    if active_view is not None and active_view.run_id == run_id:
        return _root_inventory_descriptor_from_view(active_view)
    if root_proof is not None:
        view = _open_held_run_view(root_proof)
        try:
            descriptor = _root_inventory_descriptor_from_view(view)
            _revalidate_held_run_view(view)
            return descriptor
        finally:
            view.close()
    paths = {
        *run_root.rglob("*.jsonl"),
        *run_root.rglob("*.manifest.json"),
        *run_root.rglob("*.jsonl.partial"),
    }
    checkpoint_path = (
        run_root
        / "control"
        / "checkpoints"
        / "dynamic_short_crypto_service.json"
    )
    if os.path.lexists(checkpoint_path):
        paths.add(checkpoint_path)
    artifacts: list[dict[str, Any]] = []
    for path in sorted(paths):
        relative = _relative_artifact(path, run_root=run_root)
        if path.name.endswith(".jsonl.partial"):
            artifacts.append(
                {
                    "kind": "mutable_partial_path",
                    "path": relative,
                }
            )
            continue
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RecoveryInputError(
                "recovery_input_inventory_invalid",
                f"recovery artifact cannot be inventoried: {path}",
            ) from exc
        artifacts.append(
            {
                "kind": (
                    "finalized_raw"
                    if path.suffix == ".jsonl"
                    else (
                        "finalized_manifest"
                        if path.name.endswith(".manifest.json")
                        else "checkpoint"
                    )
                ),
                "path": relative,
                "mode": stat.S_IMODE(metadata.st_mode),
                "size": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "ctime_ns": metadata.st_ctime_ns,
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "symlink": path.is_symlink(),
            }
        )
    body = {
        "schema_version": ROOT_INVENTORY_SCHEMA,
        "run_id": run_id,
        "artifacts": artifacts,
    }
    return {
        "schema_version": ROOT_INVENTORY_SCHEMA,
        "run_id": run_id,
        "commitment": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
        "artifact_count": len(artifacts),
    }


def _root_content_commitment(
    run_id: str,
    run_root: Path,
    *,
    root_proof: _RunRootProof | None = None,
    _digest_only: bool = False,
) -> dict[str, Any]:
    active_view = _ACTIVE_HELD_RUN_VIEW.get()
    if active_view is not None and active_view.run_id == run_id:
        return _root_content_commitment_from_view(active_view)
    if root_proof is not None:
        view = _open_held_run_view(root_proof)
        token = _ACTIVE_HELD_RUN_VIEW.set(view)
        try:
            checkpoint_relative = (
                "control/checkpoints/"
                "dynamic_short_crypto_service.json"
            )
            checkpoint_artifact = view.artifacts.get(
                checkpoint_relative
            )
            if _digest_only and checkpoint_artifact is not None:
                if not stat.S_ISREG(checkpoint_artifact.identity[2]):
                    raise RecoveryInputError(
                        "recovery_snapshot_checkpoint_unreadable",
                        (
                            "checkpoint is not a regular "
                            "descriptor-bound file: "
                            f"{run_id}/{checkpoint_relative}"
                        ),
                    )
                _stable_digest_held_artifact(
                    view,
                    checkpoint_artifact,
                    code="recovery_snapshot_checkpoint_unreadable",
                    count_lines=False,
                )
            elif not _digest_only:
                _checkpoint_artifact(run_root)
            raw_artifacts = tuple(
                artifact.path
                for artifact in sorted(
                    view.artifacts.values(),
                    key=lambda item: item.relative_path,
                )
                if (
                    artifact.relative_path.endswith(".jsonl")
                    and stat.S_ISREG(artifact.identity[2])
                    and Path(artifact.relative_path).with_suffix(
                        ".manifest.json"
                    ).as_posix()
                    in view.artifacts
                    and stat.S_ISREG(
                        view.artifacts[
                            Path(artifact.relative_path).with_suffix(
                                ".manifest.json"
                            ).as_posix()
                        ].identity[2]
                    )
                )
            )
            for raw_path in raw_artifacts:
                if not _digest_only:
                    _validate_finalized_pair(
                        run_root,
                        run_id,
                        raw_path,
                    )
                    continue
                raw_relative = raw_path.relative_to(
                    run_root
                ).as_posix()
                manifest_relative = Path(raw_relative).with_suffix(
                    ".manifest.json"
                ).as_posix()
                _stable_digest_held_artifact(
                    view,
                    view.artifacts[raw_relative],
                    code="recovery_snapshot_raw_unreadable",
                    count_lines=True,
                )
                _stable_digest_held_artifact(
                    view,
                    view.artifacts[manifest_relative],
                    code="recovery_snapshot_manifest_unreadable",
                    count_lines=False,
                )
            commitment = _root_content_commitment_from_view(view)
            _revalidate_held_run_view(view)
            return commitment
        finally:
            _ACTIVE_HELD_RUN_VIEW.reset(token)
            view.close()
    artifacts: list[dict[str, Any]] = []
    finalized_raw_bytes = 0
    finalized_raw_lines = 0
    finalized_pair_count = 0
    partial_paths = tuple(sorted(run_root.rglob("*.jsonl.partial")))
    all_raw_paths = tuple(sorted(run_root.rglob("*.jsonl")))
    all_manifest_paths = tuple(
        sorted(run_root.rglob("*.manifest.json"))
    )
    symlink_artifacts = tuple(
        path
        for path in (*all_raw_paths, *all_manifest_paths)
        if path.is_symlink()
    )
    regular_raw_paths = {
        path
        for path in all_raw_paths
        if not path.is_symlink() and path.is_file()
    }
    regular_manifest_paths = {
        path
        for path in all_manifest_paths
        if not path.is_symlink() and path.is_file()
    }
    raw_without_manifest = tuple(
        sorted(
            path
            for path in regular_raw_paths
            if path.with_suffix(".manifest.json")
            not in regular_manifest_paths
        )
    )
    manifest_without_raw = tuple(
        sorted(
            path
            for path in regular_manifest_paths
            if path.with_name(
                path.name.removesuffix(".manifest.json") + ".jsonl"
            )
            not in regular_raw_paths
        )
    )
    paired_raw_paths = tuple(
        sorted(
            path
            for path in regular_raw_paths
            if path.with_suffix(".manifest.json")
            in regular_manifest_paths
        )
    )
    for raw_path in paired_raw_paths:
        manifest_path = raw_path.with_suffix(".manifest.json")
        raw_sha, raw_bytes, raw_lines = _stable_digest(
            raw_path,
            code="recovery_snapshot_raw_unreadable",
            count_lines=True,
        )
        manifest_sha, manifest_bytes, _ = _stable_digest(
            manifest_path,
            code="recovery_snapshot_manifest_unreadable",
            count_lines=False,
        )
        artifacts.append(
            {
                "kind": "finalized_pair",
                "raw_path": _relative_artifact(
                    raw_path,
                    run_root=run_root,
                ),
                "raw_sha256": raw_sha,
                "raw_bytes": raw_bytes,
                "raw_lines": raw_lines,
                "manifest_path": _relative_artifact(
                    manifest_path,
                    run_root=run_root,
                ),
                "manifest_sha256": manifest_sha,
                "manifest_bytes": manifest_bytes,
            }
        )
        finalized_pair_count += 1
        finalized_raw_bytes += raw_bytes
        finalized_raw_lines += int(raw_lines or 0)
    for kind, paths in (
        ("raw_without_manifest", raw_without_manifest),
        ("manifest_without_raw", manifest_without_raw),
        ("symlink_artifact", symlink_artifacts),
        ("orphan_partial_path", partial_paths),
    ):
        artifacts.extend(
            {
                "kind": kind,
                "path": _relative_artifact(path, run_root=run_root),
            }
            for path in paths
        )
    checkpoint_path = (
        run_root
        / "control"
        / "checkpoints"
        / "dynamic_short_crypto_service.json"
    )
    if checkpoint_path.is_file():
        checkpoint_sha, checkpoint_bytes, _ = _stable_digest(
            checkpoint_path,
            code="recovery_snapshot_checkpoint_unreadable",
            count_lines=False,
        )
        artifacts.append(
            {
                "kind": "checkpoint",
                "path": _relative_artifact(
                    checkpoint_path,
                    run_root=run_root,
                ),
                "sha256": checkpoint_sha,
                "bytes": checkpoint_bytes,
            }
        )
    artifacts.sort(
        key=lambda item: (
            str(item["kind"]),
            str(item.get("path", item.get("raw_path", ""))),
        )
    )
    body = {
        "schema_version": ROOT_CONTENT_COMMITMENT_SCHEMA,
        "run_id": run_id,
        "artifacts": artifacts,
    }
    return {
        "schema_version": ROOT_CONTENT_COMMITMENT_SCHEMA,
        "run_id": run_id,
        "commitment": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
        "artifact_count": len(artifacts),
        "finalized_pair_count": finalized_pair_count,
        "finalized_raw_bytes": finalized_raw_bytes,
        "finalized_raw_lines": finalized_raw_lines,
    }


def _combine_input_commitments(
    root_contents: tuple[dict[str, Any], ...],
    root_inventories: tuple[dict[str, Any], ...],
) -> tuple[str, dict[str, Any]]:
    body = {
        "schema_version": INPUT_COMMITMENT_SCHEMA,
        "root_content_commitments": list(root_contents),
    }
    commitment = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    summary = {
        "schema_version": INPUT_SUMMARY_SCHEMA,
        "run_count": len(root_contents),
        "artifact_count": sum(
            int(item["artifact_count"]) for item in root_contents
        ),
        "finalized_pair_count": sum(
            int(item["finalized_pair_count"]) for item in root_contents
        ),
        "finalized_raw_bytes": sum(
            int(item["finalized_raw_bytes"]) for item in root_contents
        ),
        "finalized_raw_lines": sum(
            int(item["finalized_raw_lines"]) for item in root_contents
        ),
        "root_content_commitments": list(root_contents),
        "root_inventories": list(root_inventories),
    }
    return commitment, summary


def _recovery_input_commitment(
    roots: tuple[tuple[str, Path], ...],
    *,
    trusted_prefix_summary: Mapping[str, Any] | None = None,
    replayed_root_proofs: Mapping[
        str, tuple[dict[str, Any], dict[str, Any]]
    ]
    | None = None,
    root_proofs: Mapping[str, _RunRootProof] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Hash new roots while reusing stat-verified prefix commitments."""

    trusted_contents: dict[str, dict[str, Any]] = {}
    trusted_inventories: dict[str, dict[str, Any]] = {}
    if trusted_prefix_summary is not None:
        trusted_contents = {
            item["run_id"]: deepcopy(item)
            for item in trusted_prefix_summary[
                "root_content_commitments"
            ]
        }
        trusted_inventories = {
            item["run_id"]: deepcopy(item)
            for item in trusted_prefix_summary["root_inventories"]
        }
    root_contents: list[dict[str, Any]] = []
    root_inventories: list[dict[str, Any]] = []
    replayed = (
        {}
        if replayed_root_proofs is None
        else dict(replayed_root_proofs)
    )
    held_roots = {} if root_proofs is None else dict(root_proofs)
    for run_id, run_root in roots:
        replayed_pair = replayed.get(run_id)
        current_inventory = (
            _root_inventory_descriptor(
                run_id,
                run_root,
                root_proof=held_roots.get(run_id),
            )
            if run_id in held_roots
            else (
                deepcopy(replayed_pair[1])
                if replayed_pair is not None
                else _root_inventory_descriptor(run_id, run_root)
            )
        )
        if (
            replayed_pair is not None
            and current_inventory != replayed_pair[1]
        ):
            raise RecoveryInputError(
                "recovery_snapshot_input_drift",
                f"replayed root inventory changed: {run_id}",
            )
        inventory = current_inventory
        trusted_inventory = trusted_inventories.get(run_id)
        if trusted_inventory is not None and inventory != trusted_inventory:
            raise RecoveryInputError(
                "recovery_snapshot_input_drift",
                f"validated prefix inventory changed: {run_id}",
            )
        trusted_content = trusted_contents.get(run_id)
        root_contents.append(
            deepcopy(trusted_content)
            if trusted_content is not None
            else (
                deepcopy(replayed_pair[0])
                if replayed_pair is not None
                else _root_content_commitment(
                    run_id,
                    run_root,
                    root_proof=held_roots.get(run_id),
                )
            )
        )
        root_inventories.append(inventory)
    return _combine_input_commitments(
        tuple(root_contents),
        tuple(root_inventories),
    )


def _validated_input_summary(
    value: Any,
    *,
    expected_run_ids: tuple[str, ...],
    expected_commitment: str,
) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "run_count",
        "artifact_count",
        "finalized_pair_count",
        "finalized_raw_bytes",
        "finalized_raw_lines",
        "root_content_commitments",
        "root_inventories",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "input summary has an incompatible schema",
        )
    root_contents = value["root_content_commitments"]
    root_inventories = value["root_inventories"]
    content_keys = {
        "schema_version",
        "run_id",
        "commitment",
        "artifact_count",
        "finalized_pair_count",
        "finalized_raw_bytes",
        "finalized_raw_lines",
    }
    inventory_keys = {
        "schema_version",
        "run_id",
        "commitment",
        "artifact_count",
    }
    counters = (
        "run_count",
        "artifact_count",
        "finalized_pair_count",
        "finalized_raw_bytes",
        "finalized_raw_lines",
    )
    if (
        value["schema_version"] != INPUT_SUMMARY_SCHEMA
        or any(
            isinstance(value[name], bool)
            or not isinstance(value[name], int)
            or value[name] < 0
            for name in counters
        )
        or type(root_contents) is not list
        or type(root_inventories) is not list
        or any(type(item) is not dict for item in root_contents)
        or any(type(item) is not dict for item in root_inventories)
        or [item.get("run_id") for item in root_contents]
        != list(expected_run_ids)
        or [item.get("run_id") for item in root_inventories]
        != list(expected_run_ids)
        or any(
            not isinstance(item, dict)
            or set(item) != content_keys
            or item["schema_version"]
            != ROOT_CONTENT_COMMITMENT_SCHEMA
            or not isinstance(item["commitment"], str)
            or _CONTENT_HASH.fullmatch(item["commitment"]) is None
            or any(
                isinstance(item[name], bool)
                or not isinstance(item[name], int)
                or item[name] < 0
                for name in (
                    "artifact_count",
                    "finalized_pair_count",
                    "finalized_raw_bytes",
                    "finalized_raw_lines",
                )
            )
            for item in root_contents
        )
        or any(
            not isinstance(item, dict)
            or set(item) != inventory_keys
            or item["schema_version"] != ROOT_INVENTORY_SCHEMA
            or not isinstance(item["commitment"], str)
            or _CONTENT_HASH.fullmatch(item["commitment"]) is None
            or isinstance(item["artifact_count"], bool)
            or not isinstance(item["artifact_count"], int)
            or item["artifact_count"] < 0
            for item in root_inventories
        )
    ):
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "input summary roots or counters are invalid",
        )
    commitment, canonical_summary = _combine_input_commitments(
        tuple(deepcopy(root_contents)),
        tuple(deepcopy(root_inventories)),
    )
    if commitment != expected_commitment or canonical_summary != value:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "input summary is not the canonical committed root inventory",
        )
    return canonical_summary


def _recovery_result_from_snapshot(
    value: Any,
    *,
    expected_run_ids: tuple[str, ...],
) -> RecoveryResult:
    expected_keys = {
        "schema_version",
        "recovered_from_run_ids",
        "replayed_record_count",
        "state_hash",
        "gaps",
        "exclusions",
        "run_classifications",
        "state",
        "recovery_decision",
        "worker_actions",
        "callback_generation",
        "new_orders_disabled",
        "authenticated_endpoints_used",
        "orders_submitted",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "snapshot result has an incompatible schema",
        )
    run_ids = value["recovered_from_run_ids"]
    replayed_record_count = value["replayed_record_count"]
    callback_generation = value["callback_generation"]
    state_hash = value["state_hash"]
    sequence_fields = {
        "gaps": value["gaps"],
        "exclusions": value["exclusions"],
        "run_classifications": value["run_classifications"],
        "worker_actions": value["worker_actions"],
    }
    if (
        value["schema_version"] != RESULT_SCHEMA
        or run_ids != list(expected_run_ids)
        or isinstance(replayed_record_count, bool)
        or not isinstance(replayed_record_count, int)
        or replayed_record_count < 0
        or isinstance(callback_generation, bool)
        or not isinstance(callback_generation, int)
        or callback_generation < 0
        or not isinstance(state_hash, str)
        or _CONTENT_HASH.fullmatch(state_hash) is None
        or any(
            not isinstance(items, list)
            or any(not isinstance(item, dict) for item in items)
            for items in sequence_fields.values()
        )
        or not isinstance(value["state"], dict)
        or not isinstance(value["recovery_decision"], dict)
        or value["new_orders_disabled"] is not True
        or value["authenticated_endpoints_used"] != 0
        or value["orders_submitted"] != 0
    ):
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "snapshot result violates recovery safety invariants",
        )
    actual_state_hash = hashlib.sha256(
        canonical_json_bytes(value["state"])
    ).hexdigest()
    if actual_state_hash != state_hash:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "snapshot recovered state hash does not match its state",
        )
    try:
        gap_codes = sorted({item["code"] for item in value["gaps"]})
        exclusion_codes = sorted(
            {item["code"] for item in value["exclusions"]}
        )
    except (KeyError, TypeError) as exc:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "snapshot gaps or exclusions have no stable code",
        ) from exc
    decision_body = {
        "schema_version": DECISION_SCHEMA,
        "recovered_from_run_ids": list(expected_run_ids),
        "replayed_record_count": replayed_record_count,
        "state_hash": state_hash,
        "gap_codes": gap_codes,
        "exclusion_codes": exclusion_codes,
        "callback_generation": callback_generation,
        "must_be_durable_before_worker_actions": True,
    }
    expected_decision = {
        **decision_body,
        "decision_id": hashlib.sha256(
            canonical_json_bytes(decision_body)
        ).hexdigest(),
    }
    if value["recovery_decision"] != expected_decision:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "snapshot recovery decision is not canonical",
        )
    for action in value["worker_actions"]:
        if (
            action.get("blocked_until_recovery_decision_id")
            != expected_decision["decision_id"]
            or action.get("generation") != callback_generation
        ):
            raise RecoveryInputError(
                "recovery_snapshot_invalid",
                "snapshot worker action bypasses its durable decision",
            )
    result = RecoveryResult(
        recovered_from_run_ids=expected_run_ids,
        replayed_record_count=replayed_record_count,
        state_hash=state_hash,
        gaps=tuple(deepcopy(value["gaps"])),
        exclusions=tuple(deepcopy(value["exclusions"])),
        run_classifications=tuple(
            deepcopy(value["run_classifications"])
        ),
        state=deepcopy(value["state"]),
        recovery_decision=deepcopy(value["recovery_decision"]),
        worker_actions=tuple(deepcopy(value["worker_actions"])),
        callback_generation=callback_generation,
    )
    if result.to_dict() != value:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "snapshot result is not a lossless RecoveryResult",
        )
    return result


def _recovery_replay_checkpoint_from_snapshot(
    value: Any,
    *,
    expected_run_ids: tuple[str, ...],
) -> _RecoveryReplayCheckpoint:
    expected_keys = {
        "schema_version",
        "run_ids",
        "replay_records",
        "record_sources",
        "records_by_id",
        "record_connections",
        "record_event_types",
        "validated_record_count",
        "classifications",
        "gaps",
        "exclusions",
        "runs_with_orphan_partials",
        "runs_with_input_integrity_gaps",
        "latest_legacy_registry_index",
        "latest_legacy_registry_key",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "replay checkpoint has an incompatible schema",
        )
    replay_values = value["replay_records"]
    string_maps = (
        "record_sources",
        "record_connections",
        "record_event_types",
    )
    object_sequences = ("classifications", "gaps", "exclusions")
    run_sets = (
        "runs_with_orphan_partials",
        "runs_with_input_integrity_gaps",
    )
    if (
        value["schema_version"] != REPLAY_CHECKPOINT_SCHEMA
        or value["run_ids"] != list(expected_run_ids)
        or not isinstance(replay_values, list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"run_id", "relative_path", "row"}
            or item["run_id"] not in expected_run_ids
            or not isinstance(item["relative_path"], str)
            or not isinstance(item["row"], dict)
            for item in replay_values
        )
        or any(
            not isinstance(value[name], dict)
            or any(
                not isinstance(key, str)
                or not isinstance(item, str)
                for key, item in value[name].items()
            )
            for name in string_maps
        )
        or not isinstance(value["records_by_id"], dict)
        or any(
            not isinstance(record_id, str)
            or _CONTENT_HASH.fullmatch(record_id) is None
            or not isinstance(row, dict)
            or row.get("record_id") != record_id
            for record_id, row in value["records_by_id"].items()
        )
        or isinstance(value["validated_record_count"], bool)
        or not isinstance(value["validated_record_count"], int)
        or value["validated_record_count"] < 0
        or any(
            not isinstance(value[name], list)
            or any(not isinstance(item, dict) for item in value[name])
            for name in object_sequences
        )
        or any(
            not isinstance(value[name], list)
            or any(not isinstance(run_id, str) for run_id in value[name])
            or value[name] != sorted(set(value[name]))
            or any(
                run_id not in expected_run_ids
                for run_id in value[name]
            )
            for name in run_sets
        )
    ):
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "replay checkpoint violates canonical shape",
        )
    replay_records = tuple(
        (
            str(item["run_id"]),
            str(item["relative_path"]),
            deepcopy(item["row"]),
        )
        for item in replay_values
    )
    if tuple(sorted(replay_records, key=_replay_sort_key)) != replay_records:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "replay checkpoint records are not canonically ordered",
        )
    legacy_index = value["latest_legacy_registry_index"]
    legacy_key_value = value["latest_legacy_registry_key"]
    if (
        legacy_index is not None
        and (
            isinstance(legacy_index, bool)
            or not isinstance(legacy_index, int)
            or legacy_index < 0
            or legacy_index >= len(replay_records)
        )
    ):
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "replay checkpoint legacy index is invalid",
        )
    if legacy_key_value is None:
        legacy_key = None
    elif (
        isinstance(legacy_key_value, list)
        and len(legacy_key_value) == 5
        and isinstance(legacy_key_value[0], str)
        and isinstance(legacy_key_value[1], str)
        and isinstance(legacy_key_value[2], int)
        and not isinstance(legacy_key_value[2], bool)
        and isinstance(legacy_key_value[3], str)
        and isinstance(legacy_key_value[4], str)
    ):
        legacy_key = (
            legacy_key_value[0],
            legacy_key_value[1],
            legacy_key_value[2],
            legacy_key_value[3],
            legacy_key_value[4],
        )
    else:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "replay checkpoint legacy key is invalid",
        )
    if (
        (legacy_index is None) != (legacy_key is None)
        or (
            legacy_index is not None
            and _replay_sort_key(replay_records[legacy_index])
            != legacy_key
        )
    ):
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "replay checkpoint legacy cursor is inconsistent",
        )
    checkpoint = _RecoveryReplayCheckpoint(
        run_ids=expected_run_ids,
        replay_records=replay_records,
        record_sources=deepcopy(value["record_sources"]),
        records_by_id=deepcopy(value["records_by_id"]),
        record_connections=deepcopy(value["record_connections"]),
        record_event_types=deepcopy(value["record_event_types"]),
        validated_record_count=value["validated_record_count"],
        classifications=tuple(deepcopy(value["classifications"])),
        gaps=tuple(deepcopy(value["gaps"])),
        exclusions=tuple(deepcopy(value["exclusions"])),
        runs_with_orphan_partials=tuple(
            value["runs_with_orphan_partials"]
        ),
        runs_with_input_integrity_gaps=tuple(
            value["runs_with_input_integrity_gaps"]
        ),
        latest_legacy_registry_index=legacy_index,
        latest_legacy_registry_key=legacy_key,
    )
    if checkpoint.to_dict() != value:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "replay checkpoint is not lossless",
        )
    return checkpoint


def _record_index_paths(
    snapshot_path: Path,
    descriptors: Any,
    *,
    expected_run_ids: tuple[str, ...],
) -> tuple[Path, ...]:
    if not isinstance(descriptors, list) or not descriptors:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "record index descriptors are missing",
        )
    covered_run_ids: list[str] = []
    paths: list[Path] = []
    expected_keys = {
        "schema_version",
        "path",
        "sha256",
        "bytes",
        "row_count",
        "run_ids",
    }
    for descriptor in descriptors:
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != expected_keys
            or descriptor["schema_version"] != RECORD_INDEX_SCHEMA
            or not isinstance(descriptor["path"], str)
            or Path(descriptor["path"]).name != descriptor["path"]
            or not isinstance(descriptor["sha256"], str)
            or _CONTENT_HASH.fullmatch(descriptor["sha256"]) is None
            or descriptor["path"]
            != (
                f"{snapshot_path.stem}.record-index-"
                f"{descriptor['sha256']}.sqlite3"
            )
            or isinstance(descriptor["bytes"], bool)
            or not isinstance(descriptor["bytes"], int)
            or descriptor["bytes"] < 0
            or isinstance(descriptor["row_count"], bool)
            or not isinstance(descriptor["row_count"], int)
            or descriptor["row_count"] < 0
            or not isinstance(descriptor["run_ids"], list)
            or not descriptor["run_ids"]
            or any(
                not isinstance(run_id, str)
                for run_id in descriptor["run_ids"]
            )
        ):
            raise RecoveryInputError(
                "recovery_snapshot_invalid",
                "record index descriptor is invalid",
            )
        path = snapshot_path.parent / descriptor["path"]
        if (
            path.is_symlink()
            or not path.is_file()
            or _sqlite_sidecar_exists(path)
        ):
            raise RecoveryInputError(
                "recovery_snapshot_invalid",
                f"record index is not immutable: {path}",
            )
        digest, byte_count, _ = _stable_digest(
            path,
            code="recovery_record_index_unreadable",
            count_lines=False,
        )
        if (
            digest != descriptor["sha256"]
            or byte_count != descriptor["bytes"]
            or stat.S_IMODE(path.stat().st_mode) != 0o444
        ):
            raise RecoveryInputError(
                "recovery_snapshot_invalid",
                f"record index commitment failed: {path}",
            )
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro&immutable=1",
                uri=True,
            )
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table'"
                )
            }
            table_sql = {
                str(row[0]): " ".join(str(row[1]).split())
                for row in connection.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'table'"
                )
            }
            count_row = connection.execute(
                "SELECT COUNT(*) FROM finalized_records"
            ).fetchone()
            source_columns = [
                (str(row[1]), str(row[2]), int(row[3]), int(row[5]))
                for row in connection.execute(
                    "PRAGMA table_info(finalized_record_sources)"
                )
            ]
            record_columns = [
                (str(row[1]), str(row[2]), int(row[3]), int(row[5]))
                for row in connection.execute(
                    "PRAGMA table_info(finalized_records)"
                )
            ]
            replay_columns = [
                (str(row[1]), str(row[2]), int(row[3]), int(row[5]))
                for row in connection.execute(
                    "PRAGMA table_info(replay_records)"
                )
            ]
            inventory_columns = [
                (str(row[1]), str(row[2]), int(row[3]), int(row[5]))
                for row in connection.execute(
                    "PRAGMA table_info(recovery_run_inventory)"
                )
            ]
            invalid_record_row = connection.execute(
                "SELECT records.record_id "
                "FROM finalized_records AS records "
                "LEFT JOIN finalized_record_sources AS sources "
                "ON sources.source_id = records.source_id "
                "WHERE typeof(records.record_id) != 'blob' "
                "OR length(records.record_id) != 32 "
                "OR sources.source_id IS NULL "
                "OR (records.evidence_zlib IS NOT NULL "
                "AND typeof(records.evidence_zlib) != 'blob') "
                "LIMIT 1"
            ).fetchone()
            invalid_replay_row = connection.execute(
                "SELECT replay.record_id "
                "FROM replay_records AS replay "
                "LEFT JOIN finalized_records AS records "
                "ON records.record_id = replay.record_id "
                "WHERE typeof(replay.record_id) != 'blob' "
                "OR length(replay.record_id) != 32 "
                "OR records.record_id IS NULL "
                "OR typeof(replay.run_id) != 'text' "
                "OR typeof(replay.relative_path) != 'text' "
                "OR typeof(replay.row_zlib) != 'blob' "
                "LIMIT 1"
            ).fetchone()
            invalid_inventory_row = connection.execute(
                "SELECT run_id FROM recovery_run_inventory "
                "WHERE typeof(run_id) != 'text' "
                "OR typeof(inventory_zlib) != 'blob' "
                "LIMIT 1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise RecoveryInputError(
                "recovery_snapshot_invalid",
                f"record index schema is unreadable: {path}",
            ) from exc
        finally:
            if connection is not None:
                connection.close()
        if (
            tables
            != {
                "finalized_record_sources",
                "finalized_records",
                "replay_records",
                "recovery_run_inventory",
            }
            or table_sql
            != {
                "finalized_record_sources": (
                    "CREATE TABLE finalized_record_sources "
                    "(source_id INTEGER PRIMARY KEY, "
                    "source TEXT NOT NULL UNIQUE)"
                ),
                "finalized_records": (
                    "CREATE TABLE finalized_records "
                    "(record_id BLOB PRIMARY KEY, "
                    "source_id INTEGER NOT NULL, "
                    "evidence_zlib BLOB) WITHOUT ROWID"
                ),
                "replay_records": (
                    "CREATE TABLE replay_records "
                    "(record_id BLOB PRIMARY KEY, "
                    "run_id TEXT NOT NULL, "
                    "relative_path TEXT NOT NULL, "
                    "row_zlib BLOB NOT NULL) WITHOUT ROWID"
                ),
                "recovery_run_inventory": (
                    "CREATE TABLE recovery_run_inventory "
                    "(run_id TEXT PRIMARY KEY, "
                    "inventory_zlib BLOB NOT NULL) WITHOUT ROWID"
                ),
            }
            or count_row is None
            or int(count_row[0]) != descriptor["row_count"]
            or source_columns
            != [
                ("source_id", "INTEGER", 0, 1),
                ("source", "TEXT", 1, 0),
            ]
            or record_columns
            != [
                ("record_id", "BLOB", 1, 1),
                ("source_id", "INTEGER", 1, 0),
                ("evidence_zlib", "BLOB", 0, 0),
            ]
            or replay_columns
            != [
                ("record_id", "BLOB", 1, 1),
                ("run_id", "TEXT", 1, 0),
                ("relative_path", "TEXT", 1, 0),
                ("row_zlib", "BLOB", 1, 0),
            ]
            or inventory_columns
            != [
                ("run_id", "TEXT", 1, 1),
                ("inventory_zlib", "BLOB", 1, 0),
            ]
            or invalid_record_row is not None
            or invalid_replay_row is not None
            or invalid_inventory_row is not None
        ):
            raise RecoveryInputError(
                "recovery_snapshot_invalid",
                f"record index schema or row count changed: {path}",
            )
        covered_run_ids.extend(descriptor["run_ids"])
        paths.append(path)
    if tuple(covered_run_ids) != expected_run_ids:
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "record index shards do not cover the snapshot prefix",
        )
    return tuple(paths)


def _new_record_index(
    snapshot_path: Path,
    *,
    prefix_paths: tuple[Path, ...] = (),
) -> tuple[_FinalizedRecordIndex, Path]:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        dir=snapshot_path.parent,
        prefix=f".{snapshot_path.name}.record-index.",
        suffix=".sqlite3",
    )
    os.close(descriptor)
    path = Path(raw_path)
    try:
        index = _FinalizedRecordIndex(
            path,
            prefix_paths=prefix_paths,
        )
    except BaseException:
        _discard_pending_sqlite_sidecars(path)
        if path.exists() and not path.is_symlink() and path.is_file():
            path.unlink()
        raise
    return index, path


def _sqlite_sidecar_exists(path: Path) -> bool:
    return any(
        os.path.lexists(str(path) + suffix)
        for suffix in ("-journal", "-shm", "-wal")
    )


def _discard_pending_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-journal", "-shm", "-wal"):
        sidecar = Path(str(path) + suffix)
        if not os.path.lexists(sidecar):
            continue
        if sidecar.is_symlink() or not sidecar.is_file():
            raise RecoveryInputError(
                "recovery_record_index_unsafe",
                f"pending index sidecar is unsafe: {sidecar}",
            )
        sidecar.unlink()


def _discard_pending_record_index(
    index: _FinalizedRecordIndex,
    pending_path: Path,
) -> None:
    try:
        index.close()
    finally:
        _discard_pending_sqlite_sidecars(pending_path)
        if pending_path.exists():
            if pending_path.is_symlink() or not pending_path.is_file():
                raise RecoveryInputError(
                    "recovery_record_index_unsafe",
                    (
                        "pending index path changed before cleanup: "
                        f"{pending_path}"
                    ),
                )
            pending_path.unlink()


def _finalize_record_index(
    snapshot_path: Path,
    *,
    index: _FinalizedRecordIndex,
    pending_path: Path,
    run_ids: tuple[str, ...],
) -> dict[str, Any]:
    index.close()
    if _sqlite_sidecar_exists(pending_path):
        raise RecoveryInputError(
            "recovery_record_index_unsafe",
            f"pending index has an uncommitted sidecar: {pending_path}",
        )
    descriptor = os.open(pending_path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    pending_path.chmod(0o444)
    digest, byte_count, _ = _stable_digest(
        pending_path,
        code="recovery_record_index_unreadable",
        count_lines=False,
    )
    final_path = snapshot_path.with_name(
        f"{snapshot_path.stem}.record-index-{digest}.sqlite3"
    )
    if final_path.exists():
        existing_digest, existing_bytes, _ = _stable_digest(
            final_path,
            code="recovery_record_index_unreadable",
            count_lines=False,
        )
        if (
            final_path.is_symlink()
            or not final_path.is_file()
            or _sqlite_sidecar_exists(final_path)
            or stat.S_IMODE(final_path.stat().st_mode) != 0o444
            or existing_digest != digest
            or existing_bytes != byte_count
        ):
            raise RecoveryInputError(
                "recovery_record_index_unsafe",
                f"content-addressed index path conflicts: {final_path}",
            )
        pending_path.unlink()
    else:
        os.replace(pending_path, final_path)
        directory_fd = os.open(final_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return {
        "schema_version": RECORD_INDEX_SCHEMA,
        "path": final_path.name,
        "sha256": digest,
        "bytes": byte_count,
        "row_count": index.row_count,
        "run_ids": list(run_ids),
    }


def _cohort_journal_from_replay_checkpoint(
    checkpoint: _RecoveryReplayCheckpoint,
    *,
    roots: tuple[tuple[str, Path], ...],
    settlement_timeout_ms: int,
) -> list[dict[str, Any]]:
    root_by_run_id = dict(roots)
    events: list[_RecoveredCohortEvent] = []
    for run_id, relative_path, row in checkpoint.replay_records:
        if row.get("source") != "dynamic_short_crypto_service":
            continue
        inner = row.get("payload")
        if not isinstance(inner, Mapping):
            continue
        event_type = inner.get("event_type")
        if event_type not in {
            "lifecycle_cohort_committed",
            "lifecycle_remediation_cohort_committed",
        }:
            continue
        if (
            inner.get("schema_version")
            != "edge-lab-dynamic-short-crypto-service.record.v1"
            or inner.get("source") != "dynamic_short_crypto_service"
        ):
            raise RecoveryInputError(
                "lifecycle_cohort_journal_invalid",
                "cohort service envelope has an incompatible schema",
            )
        relative = Path(relative_path)
        source_path = relative.parent
        if (
            "groups" in relative.parts
            or source_path.name != "dynamic_short_crypto_service"
            or source_path.parent.name != "raw"
            or source_path.parent.parent.name != "control"
            or run_id not in root_by_run_id
        ):
            raise RecoveryInputError(
                "lifecycle_cohort_journal_invalid",
                "cohort commitments must be finalized in control",
            )
        record_id = row.get("record_id")
        if not isinstance(record_id, str):
            raise RecoveryInputError(
                "lifecycle_cohort_journal_invalid",
                "cohort commitment has no record ID",
            )
        received_at_ms = _cohort_received_at_ms(
            row.get("received_at"),
            record_id=record_id,
        )
        payload = _validated_cohort_event(
            str(event_type),
            inner.get("payload"),
            outer_kind=inner.get("kind"),
            record_id=record_id,
            received_at_ms=received_at_ms,
            settlement_timeout_ms=settlement_timeout_ms,
        )
        events.append(
            _RecoveredCohortEvent(
                event_type=str(event_type),
                record_id=record_id,
                run_id=run_id,
                manifest_path=(
                    root_by_run_id[run_id]
                    / relative.with_suffix(".manifest.json")
                ),
                received_at_ms=received_at_ms,
                payload=payload,
            )
        )
    return _validated_cohort_journal(events)


def _validate_recovered_state_v2(
    state: Any,
    *,
    expected_journal: list[dict[str, Any]],
) -> None:
    expected_keys = {
        "schema_version",
        "processed_finalized_discovery_descriptors",
        "registry",
        "targets",
        "gamma_evidence",
        "latest_gamma_records",
        "chainlink_evidence",
        "subscription_decisions",
        "worker_receipts",
        "worker_liveness",
        "lifecycle_cohort_journal",
    }
    mapping_fields = {
        "targets",
        "gamma_evidence",
        "latest_gamma_records",
        "chainlink_evidence",
        "subscription_decisions",
        "worker_receipts",
        "worker_liveness",
    }
    if (
        not isinstance(state, dict)
        or set(state) != expected_keys
        or state.get("schema_version") != STATE_SCHEMA
        or not isinstance(
            state.get("processed_finalized_discovery_descriptors"),
            list,
        )
        or any(
            not isinstance(item, dict)
            for item in state[
                "processed_finalized_discovery_descriptors"
            ]
        )
        or not isinstance(state.get("registry"), dict)
        or set(state["registry"])
        != {"snapshot_sha256", "revision_record_ids", "snapshot"}
        or not isinstance(
            state["registry"].get("revision_record_ids"), list
        )
        or any(
            not isinstance(record_id, str)
            or _CONTENT_HASH.fullmatch(record_id) is None
            for record_id in state["registry"]["revision_record_ids"]
        )
        or any(not isinstance(state.get(field), dict) for field in mapping_fields)
        or not isinstance(state.get("lifecycle_cohort_journal"), list)
        or state["lifecycle_cohort_journal"] != expected_journal
    ):
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "recovered-state.v2 or exact cohort journal is invalid",
        )


def _validate_checkpoint_index_membership(
    checkpoint: _RecoveryReplayCheckpoint,
    *,
    record_index_paths: tuple[Path, ...],
) -> None:
    index = _FinalizedRecordIndex(prefix_paths=record_index_paths)
    try:
        indexed_replay_rows = _canonical_checkpoint_replay_rows(
            index.replay_rows()
        )
        if checkpoint.replay_records != indexed_replay_rows:
            raise RecoveryInputError(
                "recovery_snapshot_invalid",
                (
                    "checkpoint replay rows are not a bijection with "
                    "the exact index rows and locations"
                ),
            )
        expected_connections: dict[str, str] = {}
        expected_event_types: dict[str, str] = {}
        expected_sources: dict[str, str] = {}
        for _, _, row in indexed_replay_rows:
            record_id = str(row["record_id"])
            event = row.get("payload")
            if (
                not isinstance(event, Mapping)
                or event.get("kind") != "lifecycle"
            ):
                continue
            expected_sources[record_id] = str(row["source"])
            connection_id = event.get("connection_id")
            event_type = event.get("event_type")
            if isinstance(connection_id, str) and connection_id:
                expected_connections[record_id] = connection_id
            if isinstance(event_type, str):
                expected_event_types[record_id] = event_type
        if (
            checkpoint.record_connections != expected_connections
            or checkpoint.record_event_types != expected_event_types
        ):
            raise RecoveryInputError(
                "recovery_snapshot_invalid",
                (
                    "checkpoint lifecycle maps are not deterministic "
                    "derivations of exact index rows"
                ),
            )
        base_fields = _checkpoint_base_fields_from_run_inventory(
            index.run_inventories(),
            expected_run_ids=checkpoint.run_ids,
        )
        if any(
            actual != base_fields[name]
            for name, actual in (
                ("classifications", checkpoint.classifications),
                ("gaps", checkpoint.gaps),
                ("exclusions", checkpoint.exclusions),
                (
                    "runs_with_orphan_partials",
                    checkpoint.runs_with_orphan_partials,
                ),
                (
                    "runs_with_input_integrity_gaps",
                    checkpoint.runs_with_input_integrity_gaps,
                ),
            )
        ):
            raise RecoveryInputError(
                "recovery_snapshot_invalid",
                (
                    "checkpoint integrity fields are not deterministic "
                    "derivations of exact index run inventory"
                ),
            )
        (
            required_evidence_ids,
            required_source_ids,
        ) = _checkpoint_reference_ids(indexed_replay_rows)
        required_source_ids.update(expected_connections)
        required_source_ids.update(expected_event_types)
        for record_id in sorted(required_source_ids):
            if record_id in expected_sources:
                continue
            source = index.source_for(record_id)
            if source is not None:
                expected_sources[record_id] = source
        expected_evidence = {
            record_id: row
            for record_id in sorted(required_evidence_ids)
            if (row := index.evidence_row(record_id)) is not None
        }
        if (
            checkpoint.record_sources != expected_sources
            or checkpoint.records_by_id != expected_evidence
        ):
            raise RecoveryInputError(
                "recovery_snapshot_invalid",
                (
                    "checkpoint source/evidence maps are not exact "
                    "deterministic index projections"
                ),
            )
        legacy_rows = [
            (position, item)
            for position, item in enumerate(indexed_replay_rows)
            if _is_legacy_registry_row(item[2])
        ]
        expected_legacy_index = (
            None if not legacy_rows else legacy_rows[-1][0]
        )
        expected_legacy_key = (
            None
            if expected_legacy_index is None
            else _replay_sort_key(
                indexed_replay_rows[expected_legacy_index]
            )
        )
        if (
            checkpoint.latest_legacy_registry_index
            != expected_legacy_index
            or checkpoint.latest_legacy_registry_key
            != expected_legacy_key
        ):
            raise RecoveryInputError(
                "recovery_snapshot_invalid",
                "checkpoint legacy cursor is not derived from index rows",
            )
    finally:
        index.close()


def _validate_recovery_result_deep(
    result: RecoveryResult,
    checkpoint: _RecoveryReplayCheckpoint,
    *,
    roots: tuple[tuple[str, Path], ...],
    settlement_timeout_ms: int,
    record_index_paths: tuple[Path, ...] = (),
) -> RecoveryResult:
    expected_run_ids = tuple(run_id for run_id, _ in roots)
    if (
        result.recovered_from_run_ids != expected_run_ids
        or checkpoint.run_ids != expected_run_ids
        or result.replayed_record_count
        != checkpoint.validated_record_count
    ):
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            "result and replay checkpoint do not prove the same roots",
        )
    validated = _recovery_result_from_snapshot(
        result.to_dict(),
        expected_run_ids=expected_run_ids,
    )
    expected_journal = _cohort_journal_from_replay_checkpoint(
        checkpoint,
        roots=roots,
        settlement_timeout_ms=settlement_timeout_ms,
    )
    _validate_recovered_state_v2(
        validated.state,
        expected_journal=expected_journal,
    )
    if record_index_paths:
        _validate_checkpoint_index_membership(
            checkpoint,
            record_index_paths=record_index_paths,
        )
    expected_result = _recover_dynamic_short_crypto_runs(
        tuple(path for _, path in roots),
        settlement_timeout_ms=settlement_timeout_ms,
        _prefix_checkpoint=checkpoint,
        _checkpoint_only_reduction=True,
    )
    if expected_result.canonical_bytes() != validated.canonical_bytes():
        raise RecoveryInputError(
            "recovery_snapshot_invalid",
            (
                "recovered-state.v2 does not equal deterministic "
                "checkpoint reduction"
            ),
        )
    return validated


def _recovery_anchor_payload_from_snapshot(
    document: Mapping[str, Any],
) -> dict[str, Any]:
    result = document["result"]
    replay_checkpoint = document["replay_checkpoint"]
    body = {
        "schema_version": RECOVERY_ANCHOR_SCHEMA,
        "snapshot_schema": document["schema_version"],
        "validator_contract": document["validator_contract"],
        "settlement_timeout_ms": document["settlement_timeout_ms"],
        "snapshot_id": document["snapshot_id"],
        "recovered_from_run_ids": deepcopy(
            document["recovered_from_run_ids"]
        ),
        "input_commitment": document["input_commitment"],
        "input_summary": deepcopy(document["input_summary"]),
        "record_indexes": deepcopy(document["record_indexes"]),
        "replay_checkpoint_sha256": hashlib.sha256(
            canonical_json_bytes(replay_checkpoint)
        ).hexdigest(),
        "result_sha256": hashlib.sha256(
            canonical_json_bytes(result)
        ).hexdigest(),
        "state_hash": result["state_hash"],
        "replayed_record_count": result["replayed_record_count"],
    }
    return {
        **body,
        "anchor_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }


def _validated_recovery_anchor_payload(
    value: Any,
) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "snapshot_schema",
        "validator_contract",
        "settlement_timeout_ms",
        "snapshot_id",
        "recovered_from_run_ids",
        "input_commitment",
        "input_summary",
        "record_indexes",
        "replay_checkpoint_sha256",
        "result_sha256",
        "state_hash",
        "replayed_record_count",
        "anchor_id",
    }
    if type(value) is not dict or set(value) != expected_keys:
        raise RecoveryInputError(
            "recovery_anchor_invalid",
            "recovery anchor has an incompatible schema",
        )
    run_ids = value["recovered_from_run_ids"]
    index_descriptors = value["record_indexes"]
    hash_fields = (
        "validator_contract",
        "snapshot_id",
        "input_commitment",
        "replay_checkpoint_sha256",
        "result_sha256",
        "state_hash",
        "anchor_id",
    )
    if (
        value["schema_version"] != RECOVERY_ANCHOR_SCHEMA
        or value["snapshot_schema"] != SNAPSHOT_SCHEMA
        or any(
            not isinstance(value[field], str)
            or _CONTENT_HASH.fullmatch(value[field]) is None
            for field in hash_fields
        )
        or isinstance(value["settlement_timeout_ms"], bool)
        or not isinstance(value["settlement_timeout_ms"], int)
        or value["settlement_timeout_ms"] < 0
        or isinstance(value["replayed_record_count"], bool)
        or not isinstance(value["replayed_record_count"], int)
        or value["replayed_record_count"] < 0
        or type(run_ids) is not list
        or not run_ids
        or any(
            not isinstance(run_id, str)
            or _RUN_ID.fullmatch(run_id) is None
            for run_id in run_ids
        )
        or len(set(run_ids)) != len(run_ids)
        or type(index_descriptors) is not list
        or not index_descriptors
    ):
        raise RecoveryInputError(
            "recovery_anchor_invalid",
            "recovery anchor fields are invalid",
        )
    input_summary = _validated_input_summary(
        value["input_summary"],
        expected_run_ids=tuple(run_ids),
        expected_commitment=value["input_commitment"],
    )
    actual_commitment, actual_summary = _combine_input_commitments(
        tuple(input_summary["root_content_commitments"]),
        tuple(input_summary["root_inventories"]),
    )
    descriptor_keys = {
        "schema_version",
        "path",
        "sha256",
        "bytes",
        "row_count",
        "run_ids",
    }
    covered_run_ids: list[str] = []
    indexed_record_count = 0
    for descriptor in index_descriptors:
        descriptor_hash = (
            descriptor.get("sha256")
            if isinstance(descriptor, dict)
            else None
        )
        expected_path_suffix = (
            ".record-index-"
            f"{descriptor_hash}.sqlite3"
        )
        if (
            type(descriptor) is not dict
            or set(descriptor) != descriptor_keys
            or descriptor["schema_version"] != RECORD_INDEX_SCHEMA
            or not isinstance(descriptor["path"], str)
            or Path(descriptor["path"]).name != descriptor["path"]
            or not isinstance(descriptor_hash, str)
            or _CONTENT_HASH.fullmatch(descriptor_hash) is None
            or not descriptor["path"].endswith(expected_path_suffix)
            or len(descriptor["path"]) == len(expected_path_suffix)
            or isinstance(descriptor["bytes"], bool)
            or not isinstance(descriptor["bytes"], int)
            or descriptor["bytes"] < 0
            or isinstance(descriptor["row_count"], bool)
            or not isinstance(descriptor["row_count"], int)
            or descriptor["row_count"] < 0
            or type(descriptor["run_ids"]) is not list
            or not descriptor["run_ids"]
            or any(
                not isinstance(run_id, str)
                for run_id in descriptor["run_ids"]
            )
        ):
            raise RecoveryInputError(
                "recovery_anchor_invalid",
                "recovery anchor index descriptor is invalid",
            )
        covered_run_ids.extend(descriptor["run_ids"])
        indexed_record_count += descriptor["row_count"]
    body = {
        key: deepcopy(item)
        for key, item in value.items()
        if key != "anchor_id"
    }
    if (
        actual_commitment != value["input_commitment"]
        or actual_summary != input_summary
        or tuple(covered_run_ids) != tuple(run_ids)
        or indexed_record_count != value["replayed_record_count"]
        or hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        != value["anchor_id"]
    ):
        raise RecoveryInputError(
            "recovery_anchor_invalid",
            "recovery anchor commitments are inconsistent",
        )
    return deepcopy(value)


def _validated_recovery_anchor_event(
    row: Mapping[str, Any],
    *,
    run_id: str,
    relative_path: str,
) -> dict[str, Any] | None:
    if row.get("source") != "dynamic_short_crypto_service":
        return None
    event = row.get("payload")
    if (
        not isinstance(event, Mapping)
        or event.get("event_type") != "restart_recovery_decision"
    ):
        return None
    event_payload = event.get("payload")
    if not isinstance(event_payload, Mapping):
        return None
    anchor_value = event_payload.get("recovery_anchor")
    if anchor_value is None:
        return None
    relative = Path(relative_path)
    source_path = relative.parent
    if (
        event.get("schema_version")
        != "edge-lab-dynamic-short-crypto-service.record.v1"
        or event.get("source") != "dynamic_short_crypto_service"
        or type(event.get("kind")) is not str
        or event.get("kind") != "command"
        or "groups" in relative.parts
        or source_path.name != "dynamic_short_crypto_service"
        or source_path.parent.name != "raw"
        or source_path.parent.parent.name != "control"
    ):
        raise RecoveryInputError(
            "recovery_anchor_invalid",
            f"recovery anchor envelope/path is invalid in {run_id}",
        )
    anchor = _validated_recovery_anchor_payload(anchor_value)
    decision_keys = {
        "schema_version",
        "recovered_from_run_ids",
        "replayed_record_count",
        "state_hash",
        "gap_codes",
        "exclusion_codes",
        "callback_generation",
        "must_be_durable_before_worker_actions",
    }
    decision_body = {
        key: deepcopy(event_payload.get(key))
        for key in decision_keys
    }
    if (
        event_payload.get("schema_version") != DECISION_SCHEMA
        or event_payload.get("recovered_from_run_ids")
        != anchor["recovered_from_run_ids"]
        or event_payload.get("replayed_record_count")
        != anchor["replayed_record_count"]
        or event_payload.get("state_hash") != anchor["state_hash"]
        or not isinstance(event_payload.get("decision_id"), str)
        or _CONTENT_HASH.fullmatch(event_payload["decision_id"]) is None
        or hashlib.sha256(
            canonical_json_bytes(decision_body)
        ).hexdigest()
        != event_payload["decision_id"]
    ):
        raise RecoveryInputError(
            "recovery_anchor_invalid",
            f"recovery decision does not bind its anchor in {run_id}",
        )
    return anchor


def _validated_anchor_control_root(
    run_root: Path,
    *,
    root_proof: _RunRootProof | None = None,
) -> _AnchorControlRoot:
    if root_proof is not None:
        if (
            root_proof.run_id != run_root.name
            or root_proof.path != run_root
        ):
            raise RecoveryInputError(
                "recovery_run_root_changed",
                f"anchor proof does not identify run root: {run_root}",
            )
        _revalidate_run_root_proof(root_proof)
    try:
        resolved_run_root = run_root.resolve(strict=True)
    except OSError as exc:
        raise RecoveryInputError(
            "recovery_anchor_invalid",
            f"direct successor root is unreadable: {run_root}",
        ) from exc
    if resolved_run_root != run_root:
        raise RecoveryInputError(
            "recovery_anchor_invalid",
            f"direct successor root is not resolved: {run_root}",
        )
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if (
        not isinstance(directory_flag, int)
        or directory_flag == 0
        or not isinstance(no_follow, int)
        or no_follow == 0
    ):
        raise RecoveryInputError(
            "recovery_anchor_invalid",
            "descriptor-anchored directory traversal is unavailable",
        )
    open_flags = (
        os.O_RDONLY
        | directory_flag
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
    )
    paths: list[Path] = []
    descriptors: list[int] = []
    identities: list[tuple[int, int, int]] = []
    try:
        descriptor = (
            _open_run_root_descriptor(root_proof)
            if root_proof is not None
            else os.open(run_root, open_flags)
        )
        paths.append(run_root)
        descriptors.append(descriptor)
        for component in (
            "control",
            "raw",
            "dynamic_short_crypto_service",
        ):
            descriptor = os.open(
                component,
                open_flags,
                dir_fd=descriptors[-1],
            )
            paths.append(paths[-1] / component)
            descriptors.append(descriptor)
        for path, descriptor in zip(paths, descriptors):
            metadata = os.fstat(descriptor)
            lexical_metadata = path.lstat()
            resolved = path.resolve(strict=True)
            try:
                resolved.relative_to(resolved_run_root)
            except ValueError as exc:
                raise RecoveryInputError(
                    "recovery_anchor_invalid",
                    f"anchor control directory escapes run root: {path}",
                ) from exc
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(lexical_metadata.st_mode)
                or not stat.S_ISDIR(lexical_metadata.st_mode)
                or resolved != path
                or (
                    lexical_metadata.st_dev,
                    lexical_metadata.st_ino,
                )
                != (metadata.st_dev, metadata.st_ino)
            ):
                raise RecoveryInputError(
                    "recovery_anchor_invalid",
                    f"anchor control directory is not strict-local: {path}",
                )
            identities.append(
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                )
            )
        if root_proof is not None:
            _revalidate_run_root_proof(root_proof)
    except RecoveryInputError:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    except OSError as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise RecoveryInputError(
            "recovery_anchor_invalid",
            "anchor control directory chain is unreadable",
        ) from exc
    return _AnchorControlRoot(
        paths=tuple(paths),
        descriptors=tuple(descriptors),
        identities=tuple(identities),
    )


def _revalidate_anchor_control_root(
    proof: _AnchorControlRoot,
) -> None:
    for index, (
        path,
        descriptor,
        (device, inode, mode),
    ) in enumerate(
        zip(
            proof.paths,
            proof.descriptors,
            proof.identities,
        )
    ):
        try:
            metadata = os.fstat(descriptor)
            if index == 0:
                lexical = path.lstat()
            else:
                lexical = os.stat(
                    path.name,
                    dir_fd=proof.descriptors[index - 1],
                    follow_symlinks=False,
                )
        except OSError as exc:
            raise RecoveryInputError(
                "recovery_anchor_invalid",
                f"anchor control directory changed: {path}",
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(lexical.st_mode)
            or not stat.S_ISDIR(lexical.st_mode)
            or (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
            )
            != (device, inode, mode)
            or (
                lexical.st_dev,
                lexical.st_ino,
            )
            != (device, inode)
        ):
            raise RecoveryInputError(
                "recovery_anchor_invalid",
                f"anchor control directory changed: {path}",
            )


def _validate_direct_successor_anchor(
    *,
    successor: tuple[str, Path],
    expected_anchor: Mapping[str, Any],
    root_proof: _RunRootProof | None = None,
) -> None:
    run_id, run_root = successor
    anchors: list[dict[str, Any]] = []
    restart_decision_count = 0
    directory_proof = _validated_anchor_control_root(
        run_root,
        root_proof=root_proof,
    )
    try:
        try:
            names = {
                name
                for name in os.listdir(
                    directory_proof.service_descriptor
                )
                if isinstance(name, str) and Path(name).name == name
            }
        except OSError as exc:
            raise RecoveryInputError(
                "recovery_anchor_invalid",
                f"cannot enumerate direct successor anchor pairs: {run_id}",
            ) from exc
        raw_names = tuple(
            sorted(
                name
                for name in names
                if (
                    name.endswith(".jsonl")
                    and Path(name)
                    .with_suffix(".manifest.json")
                    .name
                    in names
                )
            )
        )
        for raw_name in raw_names:
            manifest_name = (
                Path(raw_name).with_suffix(".manifest.json").name
            )
            raw_read = _stable_read_at(
                directory_proof.service_descriptor,
                raw_name,
                code="finalized_raw_unreadable",
            )
            manifest_read = _stable_read_at(
                directory_proof.service_descriptor,
                manifest_name,
                code="finalized_manifest_unreadable",
            )
            batch = _validate_finalized_pair_bytes(
                run_root,
                run_id,
                directory_proof.control_root / raw_name,
                raw=raw_read.content,
                manifest_bytes=manifest_read.content,
            )
            for row in batch.rows:
                event = row.get("payload")
                if (
                    row.get("source")
                    == "dynamic_short_crypto_service"
                    and isinstance(event, Mapping)
                    and event.get("event_type")
                    == "restart_recovery_decision"
                ):
                    restart_decision_count += 1
                anchor = _validated_recovery_anchor_event(
                    row,
                    run_id=run_id,
                    relative_path=batch.relative_path,
                )
                if anchor is not None:
                    anchors.append(anchor)
            _revalidate_stable_entry_at(
                directory_proof.service_descriptor,
                raw_name,
                identity=raw_read.identity,
                code="finalized_raw_unreadable",
            )
            _revalidate_stable_entry_at(
                directory_proof.service_descriptor,
                manifest_name,
                identity=manifest_read.identity,
                code="finalized_manifest_unreadable",
            )
        _revalidate_anchor_control_root(directory_proof)
        if root_proof is not None:
            _revalidate_run_root_proof(root_proof)
    finally:
        directory_proof.close()
    if restart_decision_count != 1 or len(anchors) != 1:
        raise RecoveryInputError(
            "recovery_anchor_invalid",
            (
                "snapshot prefix requires exactly one anchored recovery "
                "decision "
                f"in direct successor {run_id}"
            ),
        )
    if canonical_json_bytes(anchors[0]) != canonical_json_bytes(
        expected_anchor
    ):
        raise RecoveryInputError(
            "recovery_anchor_mismatch",
            f"direct successor anchor does not bind the snapshot: {run_id}",
        )


def _snapshot_candidate(
    snapshot_path: Path,
    *,
    roots: tuple[tuple[str, Path], ...],
    settlement_timeout_ms: int,
    validator_contract: str,
    root_proofs: Mapping[str, _RunRootProof] | None = None,
) -> tuple[
    RecoveryResult,
    _RecoveryReplayCheckpoint,
    tuple[dict[str, Any], ...],
    tuple[Path, ...],
    str,
    dict[str, Any],
    tuple[tuple[str, Path], ...],
] | None:
    if root_proofs is not None:
        _revalidate_run_root_proofs(root_proofs)
    if not snapshot_path.exists():
        return None
    if snapshot_path.is_symlink() or not snapshot_path.is_file():
        raise RecoveryInputError(
            "recovery_snapshot_unsafe",
            f"snapshot path is not a regular file: {snapshot_path}",
        )
    try:
        if snapshot_path.stat().st_mode & 0o222:
            return None
        raw = _stable_read(
            snapshot_path,
            code="recovery_snapshot_unreadable",
        )
        document = _json_object(
            raw,
            path=snapshot_path,
            code="recovery_snapshot_invalid",
        )
        if canonical_json_bytes(document) + b"\n" != raw:
            return None
    except (OSError, TypeError, ValueError):
        return None
    snapshot_id = document.get("snapshot_id")
    body = {
        key: value
        for key, value in document.items()
        if key != "snapshot_id"
    }
    expected_body_keys = {
        "schema_version",
        "validator_contract",
        "settlement_timeout_ms",
        "input_commitment",
        "input_summary",
        "recovered_from_run_ids",
        "result",
        "replay_checkpoint",
        "record_indexes",
    }
    run_ids = tuple(run_id for run_id, _ in roots)
    snapshot_run_ids = document.get("recovered_from_run_ids")
    if (
        set(body) != expected_body_keys
        or document.get("schema_version") != SNAPSHOT_SCHEMA
        or document.get("validator_contract") != validator_contract
        or document.get("settlement_timeout_ms")
        != settlement_timeout_ms
        or not isinstance(snapshot_run_ids, list)
        or not snapshot_run_ids
        or any(not isinstance(run_id, str) for run_id in snapshot_run_ids)
        or len(snapshot_run_ids) > len(run_ids)
        or list(run_ids[: len(snapshot_run_ids)]) != snapshot_run_ids
        or not isinstance(document.get("input_summary"), dict)
        or not isinstance(document.get("input_commitment"), str)
        or _CONTENT_HASH.fullmatch(
            str(document.get("input_commitment"))
        )
        is None
        or not isinstance(snapshot_id, str)
        or _CONTENT_HASH.fullmatch(snapshot_id) is None
        or hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        != snapshot_id
    ):
        return None
    try:
        input_summary = _validated_input_summary(
            document["input_summary"],
            expected_run_ids=tuple(snapshot_run_ids),
            expected_commitment=str(document["input_commitment"]),
        )
        snapshot_roots = roots[: len(snapshot_run_ids)]
        current_root_inventories = [
            _root_inventory_descriptor(
                run_id,
                run_root,
                root_proof=(
                    None
                    if root_proofs is None
                    else root_proofs.get(run_id)
                ),
            )
            for run_id, run_root in snapshot_roots
        ]
        trusted_input_commitment = str(document["input_commitment"])
        trusted_input_summary = input_summary
        if (
            current_root_inventories
            != input_summary["root_inventories"]
        ):
            current_root_content_commitments = [
                _root_content_commitment(
                    run_id,
                    run_root,
                    root_proof=(
                        None
                        if root_proofs is None
                        else root_proofs.get(run_id)
                    ),
                    _digest_only=True,
                )
                for run_id, run_root in snapshot_roots
            ]
            if (
                current_root_content_commitments
                != input_summary["root_content_commitments"]
            ):
                raise RecoveryInputError(
                    "recovery_snapshot_input_drift",
                    "validated prefix content changed",
                )
            (
                trusted_input_commitment,
                trusted_input_summary,
            ) = _combine_input_commitments(
                tuple(current_root_content_commitments),
                tuple(current_root_inventories),
            )
        result = _recovery_result_from_snapshot(
            document["result"],
            expected_run_ids=tuple(snapshot_run_ids),
        )
        replay_checkpoint = (
            _recovery_replay_checkpoint_from_snapshot(
                document["replay_checkpoint"],
                expected_run_ids=tuple(snapshot_run_ids),
            )
        )
        index_descriptors = document["record_indexes"]
        index_paths = _record_index_paths(
            snapshot_path,
            index_descriptors,
            expected_run_ids=tuple(snapshot_run_ids),
        )
        if (
            sum(
                int(descriptor["row_count"])
                for descriptor in index_descriptors
            )
            != result.replayed_record_count
            or replay_checkpoint.validated_record_count
            != result.replayed_record_count
        ):
            raise RecoveryInputError(
                "recovery_snapshot_invalid",
                "record index count does not cover replayed records",
            )
        result = _validate_recovery_result_deep(
            result,
            replay_checkpoint,
            roots=roots[: len(snapshot_run_ids)],
            settlement_timeout_ms=settlement_timeout_ms,
            record_index_paths=index_paths,
        )
        if len(snapshot_run_ids) >= len(roots):
            raise RecoveryInputError(
                "recovery_anchor_missing",
                "snapshot has no direct successor recovery anchor",
            )
        successor = roots[len(snapshot_run_ids)]
        successor_proof = (
            None
            if root_proofs is None
            else root_proofs.get(successor[0])
        )
        if (
            root_proofs is not None
            and successor_proof is None
        ):
            raise RecoveryInputError(
                "recovery_run_root_unprovable",
                "direct successor has no held root proof",
            )
        _validate_direct_successor_anchor(
            successor=successor,
            expected_anchor=_recovery_anchor_payload_from_snapshot(
                document
            ),
            root_proof=successor_proof,
        )
        if root_proofs is not None:
            _revalidate_run_root_proofs(root_proofs)
    except RecoveryInputError:
        return None
    return (
        result,
        replay_checkpoint,
        tuple(deepcopy(index_descriptors)),
        index_paths,
        trusted_input_commitment,
        trusted_input_summary,
        snapshot_roots,
    )


def _write_recovery_snapshot(
    snapshot_path: Path,
    *,
    result: RecoveryResult,
    replay_checkpoint: _RecoveryReplayCheckpoint,
    record_indexes: tuple[dict[str, Any], ...],
    settlement_timeout_ms: int,
    validator_contract: str,
    input_commitment: str,
    input_summary: Mapping[str, Any],
) -> dict[str, Any]:
    if snapshot_path.is_symlink() or (
        snapshot_path.exists() and not snapshot_path.is_file()
    ):
        raise RecoveryInputError(
            "recovery_snapshot_unsafe",
            f"refusing to replace unsafe snapshot path: {snapshot_path}",
        )
    body = {
        "schema_version": SNAPSHOT_SCHEMA,
        "validator_contract": validator_contract,
        "settlement_timeout_ms": settlement_timeout_ms,
        "input_commitment": input_commitment,
        "input_summary": dict(input_summary),
        "recovered_from_run_ids": list(
            result.recovered_from_run_ids
        ),
        "result": result.to_dict(),
        "replay_checkpoint": replay_checkpoint.to_dict(),
        "record_indexes": deepcopy(list(record_indexes)),
    }
    document = {
        **body,
        "snapshot_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }
    _atomic_write_bytes(
        snapshot_path,
        canonical_json_bytes(document) + b"\n",
    )
    snapshot_path.chmod(0o444)
    return _recovery_anchor_payload_from_snapshot(document)


_LOADED_VALIDATOR_CONTRACT = _recovery_validator_contract()


def recover_dynamic_short_crypto_runs_cached(
    prior_run_roots: Iterable[str | Path] | str | Path,
    *,
    settlement_timeout_ms: int,
    snapshot_path: str | Path,
    cpu_ratio: float | None = None,
) -> CachedRecoveryOutcome:
    """Reuse a byte-identical proof, or fully replay and atomically refresh it."""

    held_proofs: list[_RunRootProof] = []
    try:
        roots = _run_roots(
            prior_run_roots,
            _proof_sink=held_proofs,
        )
        root_proofs = {
            proof.run_id: proof
            for proof in held_proofs
        }
        return _recover_dynamic_short_crypto_runs_cached_held(
            roots,
            root_proofs=root_proofs,
            settlement_timeout_ms=settlement_timeout_ms,
            snapshot_path=snapshot_path,
            cpu_ratio=cpu_ratio,
        )
    finally:
        for proof in reversed(held_proofs):
            proof.close()


def _recover_dynamic_short_crypto_runs_cached_held(
    roots: tuple[tuple[str, Path], ...],
    *,
    root_proofs: Mapping[str, _RunRootProof],
    settlement_timeout_ms: int,
    snapshot_path: str | Path,
    cpu_ratio: float | None,
) -> CachedRecoveryOutcome:
    """Run cached recovery while every normalized run root remains held."""

    _revalidate_run_root_proofs(root_proofs)
    target = Path(
        os.path.abspath(Path(snapshot_path).expanduser())
    )
    current_contract = _recovery_validator_contract()
    if current_contract != _LOADED_VALIDATOR_CONTRACT:
        raise RecoveryInputError(
            "recovery_validator_changed",
            "recovery validator source changed after this process imported it",
        )
    candidate = _snapshot_candidate(
        target,
        roots=roots,
        settlement_timeout_ms=settlement_timeout_ms,
        validator_contract=current_contract,
        root_proofs=root_proofs,
    )
    if candidate is not None:
        (
            result,
            replay_checkpoint,
            record_indexes,
            prefix_index_paths,
            expected_commitment,
            expected_summary,
            snapshot_roots,
        ) = candidate
        actual_commitment, actual_summary = _combine_input_commitments(
            tuple(expected_summary["root_content_commitments"]),
            tuple(expected_summary["root_inventories"]),
        )
        if (
            actual_commitment == expected_commitment
            and actual_summary == expected_summary
        ):
            suffix_roots = roots[len(snapshot_roots) :]
            record_index, pending_index_path = _new_record_index(
                target,
                prefix_paths=prefix_index_paths,
            )
            checkpoint_sink: list[_RecoveryReplayCheckpoint] = []
            replayed_input_proofs: dict[
                str, tuple[dict[str, Any], dict[str, Any]]
            ] = {}
            try:
                result = _recover_dynamic_short_crypto_runs(
                    tuple(path for _, path in roots),
                    settlement_timeout_ms=settlement_timeout_ms,
                    cpu_ratio=cpu_ratio,
                    _prefix_checkpoint=replay_checkpoint,
                    _record_index=record_index,
                    _checkpoint_sink=checkpoint_sink,
                    _root_proofs=root_proofs,
                    _input_proof_sink=replayed_input_proofs,
                )
                if len(checkpoint_sink) != 1:
                    raise RecoveryInputError(
                        "recovery_replay_checkpoint_invalid",
                        "suffix replay did not produce one checkpoint",
                    )
                result = _validate_recovery_result_deep(
                    result,
                    checkpoint_sink[0],
                    roots=roots,
                    settlement_timeout_ms=settlement_timeout_ms,
                )
                if _recovery_validator_contract() != current_contract:
                    raise RecoveryInputError(
                        "recovery_validator_changed",
                        (
                            "recovery validator source changed during "
                            "suffix validation"
                        ),
                    )
                suffix_index = _finalize_record_index(
                    target,
                    index=record_index,
                    pending_path=pending_index_path,
                    run_ids=tuple(
                        run_id for run_id, _ in suffix_roots
                    ),
                )
                result = _validate_recovery_result_deep(
                    result,
                    checkpoint_sink[0],
                    roots=roots,
                    settlement_timeout_ms=settlement_timeout_ms,
                    record_index_paths=(
                        *prefix_index_paths,
                        target.parent / suffix_index["path"],
                    ),
                )
            except BaseException:
                _discard_pending_record_index(
                    record_index,
                    pending_index_path,
                )
                raise
            _revalidate_run_root_proofs(root_proofs)
            input_commitment, input_summary = (
                _recovery_input_commitment(
                    roots,
                    trusted_prefix_summary=expected_summary,
                    replayed_root_proofs=replayed_input_proofs,
                    root_proofs=root_proofs,
                )
            )
            _revalidate_run_root_proofs(root_proofs)
            if _recovery_validator_contract() != current_contract:
                raise RecoveryInputError(
                    "recovery_validator_changed",
                    (
                        "recovery validator source changed before "
                        "suffix snapshot commit"
                    ),
                )
            anchor_payload = _write_recovery_snapshot(
                target,
                result=result,
                replay_checkpoint=checkpoint_sink[0],
                record_indexes=(*record_indexes, suffix_index),
                settlement_timeout_ms=settlement_timeout_ms,
                validator_contract=current_contract,
                input_commitment=input_commitment,
                input_summary=input_summary,
            )
            committed_input, committed_summary = (
                _recovery_input_commitment(
                    roots,
                    trusted_prefix_summary=expected_summary,
                    replayed_root_proofs=replayed_input_proofs,
                    root_proofs=root_proofs,
                )
            )
            if (
                committed_input != input_commitment
                or committed_summary != input_summary
            ):
                raise RecoveryInputError(
                    "recovery_snapshot_input_drift",
                    "recovery inputs changed while snapshot committed",
                )
            _revalidate_run_root_proofs(root_proofs)
            return CachedRecoveryOutcome(
                result=result,
                anchor_payload=anchor_payload,
            )
    record_index, pending_index_path = _new_record_index(target)
    checkpoint_sink = []
    replayed_input_proofs = {}
    try:
        result = _recover_dynamic_short_crypto_runs(
            tuple(path for _, path in roots),
            settlement_timeout_ms=settlement_timeout_ms,
            cpu_ratio=cpu_ratio,
            _record_index=record_index,
            _checkpoint_sink=checkpoint_sink,
            _root_proofs=root_proofs,
            _input_proof_sink=replayed_input_proofs,
        )
        if len(checkpoint_sink) != 1:
            raise RecoveryInputError(
                "recovery_replay_checkpoint_invalid",
                "full replay did not produce one checkpoint",
            )
        result = _validate_recovery_result_deep(
            result,
            checkpoint_sink[0],
            roots=roots,
            settlement_timeout_ms=settlement_timeout_ms,
        )
        if _recovery_validator_contract() != current_contract:
            raise RecoveryInputError(
                "recovery_validator_changed",
                "recovery validator source changed during full replay",
            )
        record_index_descriptor = _finalize_record_index(
            target,
            index=record_index,
            pending_path=pending_index_path,
            run_ids=tuple(run_id for run_id, _ in roots),
        )
        result = _validate_recovery_result_deep(
            result,
            checkpoint_sink[0],
            roots=roots,
            settlement_timeout_ms=settlement_timeout_ms,
            record_index_paths=(
                target.parent / record_index_descriptor["path"],
            ),
        )
    except BaseException:
        _discard_pending_record_index(
            record_index,
            pending_index_path,
        )
        raise
    _revalidate_run_root_proofs(root_proofs)
    input_commitment, input_summary = _recovery_input_commitment(
        roots,
        replayed_root_proofs=replayed_input_proofs,
        root_proofs=root_proofs,
    )
    _revalidate_run_root_proofs(root_proofs)
    if _recovery_validator_contract() != current_contract:
        raise RecoveryInputError(
            "recovery_validator_changed",
            (
                "recovery validator source changed before full "
                "snapshot commit"
            ),
        )
    anchor_payload = _write_recovery_snapshot(
        target,
        result=result,
        replay_checkpoint=checkpoint_sink[0],
        record_indexes=(record_index_descriptor,),
        settlement_timeout_ms=settlement_timeout_ms,
        validator_contract=current_contract,
        input_commitment=input_commitment,
        input_summary=input_summary,
    )
    committed_input, committed_summary = _recovery_input_commitment(
        roots,
        replayed_root_proofs=replayed_input_proofs,
        root_proofs=root_proofs,
    )
    if (
        committed_input != input_commitment
        or committed_summary != input_summary
    ):
        raise RecoveryInputError(
            "recovery_snapshot_input_drift",
            "recovery inputs changed while snapshot committed",
        )
    _revalidate_run_root_proofs(root_proofs)
    return CachedRecoveryOutcome(
        result=result,
        anchor_payload=anchor_payload,
    )
