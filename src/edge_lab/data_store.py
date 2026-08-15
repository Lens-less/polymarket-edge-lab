"""Immutable raw-capture storage contracts for the Edge Discovery Lab.

The capture store deliberately keeps raw observations apart from derived
artifacts.  A raw batch is written to a ``.partial`` file, atomically promoted
to JSONL at finalization, and then made read-only.  Financial values must be
provided as :class:`~decimal.Decimal` (or already-lossless strings), never as
binary floats.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


class DataContractError(ValueError):
    """Base error for capture data that violates the storage contract."""


class CriticalFloatError(DataContractError):
    """Raised when a financially meaningful field contains a binary float."""


class BatchFinalizedError(RuntimeError):
    """Raised when code attempts to mutate a finalized raw batch."""


@dataclass(frozen=True)
class AppendResult:
    """Result of an append attempt.

    Duplicate records are not written, but their stable record ID is returned
    with ``written=False`` so recorders can checkpoint deterministically.
    """

    record_id: str
    written: bool
    schema_fingerprint: str
    event_fingerprint: str


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CRITICAL_FLOAT_TOKENS = frozenset(
    {
        "amount",
        "ask",
        "asks",
        "balance",
        "bid",
        "bids",
        "book",
        "capital",
        "cash",
        "cost",
        "edge",
        "fd",
        "fee",
        "feeschedule",
        "level",
        "levels",
        "liquidity",
        "loss",
        "margin",
        "mid",
        "midpoint",
        "notional",
        "payout",
        "pnl",
        "price",
        "probability",
        "profit",
        "qty",
        "quantity",
        "rate",
        "rebate",
        "shares",
        "size",
        "spread",
        "tick",
        "value",
        "volume",
    }
)
_CRITICAL_FLOAT_FRAGMENTS = (
    "amount",
    "balance",
    "capital",
    "cash",
    "cost",
    "edge",
    "fee",
    "liquidity",
    "margin",
    "notional",
    "outcomeprice",
    "payout",
    "pnl",
    "price",
    "probability",
    "profit",
    "quantity",
    "rebate",
    "reward",
    "shares",
    "size",
    "spread",
    "tick",
    "volume",
)


def _validate_component(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError(
            f"{label} must match {_SAFE_COMPONENT.pattern!r}; got {value!r}"
        )
    return value


def _critical_float_path(path: tuple[str, ...]) -> bool:
    for component in path:
        lowered = component.lower()
        tokens = set(re.findall(r"[a-z0-9]+", lowered))
        if tokens & _CRITICAL_FLOAT_TOKENS:
            return True
        if any(fragment in lowered for fragment in _CRITICAL_FLOAT_FRAGMENTS):
            return True
    return False


def _normalize_json(value: Any, *, path: tuple[str, ...] = ()) -> Any:
    """Return a lossless JSON-compatible copy of ``value``.

    ``Decimal`` values intentionally retain their textual scale.  Floats are
    allowed only for clearly non-financial telemetry; NaN and infinity are
    always rejected.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DataContractError(
                f"non-finite Decimal at {'.'.join(path) or '<root>'}"
            )
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DataContractError(
                f"non-finite float at {'.'.join(path) or '<root>'}"
            )
        if _critical_float_path(path):
            raise CriticalFloatError(
                "binary float in critical field "
                f"{'.'.join(path) or '<root>'}; use Decimal or a string"
            )
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise DataContractError(
                    f"JSON object key at {'.'.join(path) or '<root>'} "
                    f"must be a string, got {type(key).__name__}"
                )
            normalized[key] = _normalize_json(item, path=(*path, key))
        return normalized
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            _normalize_json(item, path=(*path, str(index)))
            for index, item in enumerate(value)
        ]
    raise DataContractError(
        f"unsupported JSON value at {'.'.join(path) or '<root>'}: "
        f"{type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a value as deterministic UTF-8 JSON.

    Object keys are sorted, insignificant whitespace is omitted, Unicode is
    emitted directly, and all ``Decimal`` instances become exact strings.
    """

    normalized = _normalize_json(value)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _normalize_timestamp(value: str | datetime, *, label: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise DataContractError(f"{label} cannot be empty")
        try:
            parsed = datetime.fromisoformat(
                f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
            )
        except ValueError as exc:
            raise DataContractError(
                f"{label} must be an ISO-8601 timestamp: {value!r}"
            ) from exc
    else:
        raise DataContractError(
            f"{label} must be a timezone-aware datetime or ISO-8601 string"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataContractError(f"{label} must include a timezone")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _normalized_event_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "source",
        "event_at",
        "sequence",
        "payload",
    }
    missing = required - set(record)
    if missing:
        raise DataContractError(
            f"record is missing required fields: {', '.join(sorted(missing))}"
        )

    schema_version = record["schema_version"]
    source = record["source"]
    sequence = record["sequence"]
    payload = record["payload"]
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise DataContractError("schema_version must be a non-empty string")
    _validate_component(source, label="source")
    if sequence is not None and (
        isinstance(sequence, bool) or not isinstance(sequence, int)
    ):
        raise DataContractError("sequence must be an integer or null")
    if sequence is not None and sequence < 0:
        raise DataContractError("sequence cannot be negative")
    if not isinstance(payload, Mapping):
        raise DataContractError("payload must be a JSON object")

    event_at = record["event_at"]
    return {
        "schema_version": schema_version,
        "source": source,
        "event_at": (
            None
            if event_at is None
            else _normalize_timestamp(event_at, label="event_at")
        ),
        "sequence": sequence,
        "payload": _normalize_json(payload, path=("payload",)),
    }


def _base_record(record: Mapping[str, Any]) -> dict[str, Any]:
    if "received_at" not in record:
        raise DataContractError("record is missing required fields: received_at")
    event_fields = _normalized_event_fields(record)
    return {
        "schema_version": event_fields["schema_version"],
        "source": event_fields["source"],
        "received_at": _normalize_timestamp(
            record["received_at"], label="received_at"
        ),
        "event_at": event_fields["event_at"],
        "sequence": event_fields["sequence"],
        "payload": event_fields["payload"],
    }


def canonical_record_id(record: Mapping[str, Any]) -> str:
    """Return the SHA-256 ID of the canonical six-field raw envelope."""

    return hashlib.sha256(canonical_json_bytes(_base_record(record))).hexdigest()


def canonical_record_id_from_json(record: Mapping[str, Any]) -> str:
    """Return the canonical ID for an already JSON-decoded raw envelope.

    Finalized JSONL validation has already crossed the generic Python-to-JSON
    boundary: nested values can only be JSON primitives.  Avoid normalizing
    and copying that potentially large payload a second time while retaining
    the same outer-envelope validation, timestamp normalization, key ordering,
    and non-finite-float rejection as :func:`canonical_record_id`.
    """

    required = {
        "schema_version",
        "source",
        "received_at",
        "event_at",
        "sequence",
        "payload",
    }
    missing = required - set(record)
    if missing:
        raise DataContractError(
            f"record is missing required fields: {', '.join(sorted(missing))}"
        )
    schema_version = record["schema_version"]
    source = record["source"]
    sequence = record["sequence"]
    payload = record["payload"]
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise DataContractError("schema_version must be a non-empty string")
    _validate_component(source, label="source")
    if sequence is not None and (
        isinstance(sequence, bool) or not isinstance(sequence, int)
    ):
        raise DataContractError("sequence must be an integer or null")
    if sequence is not None and sequence < 0:
        raise DataContractError("sequence cannot be negative")
    if not isinstance(payload, dict):
        raise DataContractError("payload must be a JSON object")
    event_at = record["event_at"]
    base_record = {
        "schema_version": schema_version,
        "source": source,
        "received_at": _normalize_timestamp(
            record["received_at"],
            label="received_at",
        ),
        "event_at": (
            None
            if event_at is None
            else _normalize_timestamp(event_at, label="event_at")
        ),
        "sequence": sequence,
        "payload": payload,
    }
    encoded = json.dumps(
        base_record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_fields(base_record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": base_record["schema_version"],
        "source": base_record["source"],
        "event_at": base_record["event_at"],
        "sequence": base_record["sequence"],
        "payload": base_record["payload"],
    }


def canonical_event_fingerprint(record: Mapping[str, Any]) -> str:
    """Identify a server event independently of its local receive timestamp."""

    return hashlib.sha256(
        canonical_json_bytes(_normalized_event_fields(record))
    ).hexdigest()


def _schema_shape(value: Any) -> Any:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, Decimal):
        return "decimal-string"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        fields: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise DataContractError(
                    f"schema object key must be a string, got {type(key).__name__}"
                )
            fields[key] = _schema_shape(value[key])
        return {"object": fields}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        unique: dict[bytes, Any] = {}
        for item in value:
            shape = _schema_shape(item)
            unique[canonical_json_bytes(shape)] = shape
        return {
            "array": [
                unique[key] for key in sorted(unique)
            ]
        }
    return f"unsupported:{type(value).__name__}"


def _schema_fingerprint(payload: Mapping[str, Any]) -> tuple[str, Any]:
    descriptor = _schema_shape(payload)
    fingerprint = hashlib.sha256(canonical_json_bytes(descriptor)).hexdigest()
    return fingerprint, descriptor


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Atomically replace ``path`` with durable bytes on the same filesystem."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity_pair_token(manifest_path: Path, raw_path: Path) -> bytes:
    """Return a compact identity for one manifest/raw validation pair.

    The token deliberately includes lexical and followed metadata for both
    paths.  Content validation may be reused only while inode, type, size,
    link count, ownership, and nanosecond change timestamps are unchanged.
    Access time is excluded because the validation read itself may update it.
    """

    digest = hashlib.sha256()
    for path in (manifest_path, raw_path):
        encoded_path = os.fsencode(path)
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        for metadata in (path.lstat(), path.stat()):
            for field in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_uid",
                "st_gid",
                "st_rdev",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            ):
                digest.update(
                    int(getattr(metadata, field, 0)).to_bytes(
                        16, "big", signed=True
                    )
                )
    return digest.digest()


def _can_reuse_integrity_validation() -> bool:
    """Return whether metadata-only integrity caching is safe on this platform.

    Same-size rewrites can preserve or restore the metadata this cache records on
    the Windows, WSL/DrvFS, and mixed-filesystem paths we currently support. Fail
    closed until we have an unforgeable content-addressed or kernel-versioned
    cache key.
    """

    return False


class CaptureStore:
    """Filesystem boundary for raw, derived, and checkpoint data."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.raw_dir = self.root / "raw"
        self.derived_dir = self.root / "derived"
        self.checkpoints_dir = self.root / "checkpoints"
        self._verified_integrity_pairs: set[bytes] = set()
        for directory in (
            self.raw_dir,
            self.derived_dir,
            self.checkpoints_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def open_raw_batch(
        self,
        *,
        source: str,
        batch_id: str,
        schema_version: str,
    ) -> "RawBatchWriter":
        """Create a new exclusive raw batch.

        Existing partial or finalized names are never resumed or overwritten;
        recovery code must choose a new batch ID and retain the partial artifact
        for audit.
        """

        return RawBatchWriter(
            store=self,
            source=_validate_component(source, label="source"),
            batch_id=_validate_component(batch_id, label="batch_id"),
            schema_version=schema_version,
        )

    def derived_path(self, *parts: str) -> Path:
        """Return a path confined to the derived-artifact tree."""

        if not parts:
            return self.derived_dir
        for part in parts:
            if (
                not isinstance(part, str)
                or not part
                or Path(part).is_absolute()
                or part in {".", ".."}
                or "/" in part
                or "\\" in part
            ):
                raise ValueError(f"unsafe derived path component: {part!r}")
        path = self.derived_dir.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_checkpoint(
        self,
        source: str,
        checkpoint: Mapping[str, Any],
        *,
        updated_at: str | datetime | None = None,
    ) -> Path:
        """Atomically replace a source checkpoint.

        Checkpoints are mutable restart hints, not raw evidence.  They therefore
        live outside the immutable raw tree.
        """

        safe_source = _validate_component(source, label="source")
        if not isinstance(checkpoint, Mapping):
            raise DataContractError("checkpoint must be a JSON object")
        document = {
            "schema_version": "capture-checkpoint.v1",
            "source": safe_source,
            "updated_at": _normalize_timestamp(
                updated_at or datetime.now(timezone.utc),
                label="updated_at",
            ),
            "checkpoint": _normalize_json(
                checkpoint, path=("checkpoint",)
            ),
        }
        path = self.checkpoints_dir / f"{safe_source}.json"
        _atomic_write_bytes(path, canonical_json_bytes(document) + b"\n")
        return path

    def read_checkpoint(self, source: str) -> dict[str, Any] | None:
        safe_source = _validate_component(source, label="source")
        path = self.checkpoints_dir / f"{safe_source}.json"
        if not path.exists():
            return None
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise DataContractError(f"checkpoint {path} is not a JSON object")
        return parsed

    def audit_integrity(self) -> dict[str, Any]:
        """Inspect capture artifacts without changing or recovering any file.

        Every ``.jsonl.partial`` is conservatively reported as an orphan
        candidate because a read-only filesystem audit cannot prove whether a
        recorder still owns it.  Recovery is intentionally a separate,
        explicitly authorized operation.
        """

        def relative(path: Path) -> str:
            return path.relative_to(self.root).as_posix()

        partials = sorted(self.raw_dir.rglob("*.jsonl.partial"))
        raw_files = sorted(self.raw_dir.rglob("*.jsonl"))
        manifests = sorted(self.raw_dir.rglob("*.manifest.json"))
        manifest_set = set(manifests)
        raw_set = set(raw_files)

        raw_without_manifest = [
            path
            for path in raw_files
            if path.with_suffix(".manifest.json") not in manifest_set
        ]
        manifest_without_raw = [
            path
            for path in manifests
            if path.with_name(
                f"{path.name.removesuffix('.manifest.json')}.jsonl"
            )
            not in raw_set
        ]

        checksum_mismatches: list[dict[str, str]] = []
        invalid_manifests: list[dict[str, str]] = []
        cache_validation = _can_reuse_integrity_validation()
        for manifest_path in manifests:
            raw_path = manifest_path.with_name(
                f"{manifest_path.name.removesuffix('.manifest.json')}.jsonl"
            )
            if raw_path not in raw_set:
                continue
            validation_token: bytes | None = None
            if cache_validation:
                try:
                    validation_token = _integrity_pair_token(
                        manifest_path, raw_path
                    )
                except OSError:
                    validation_token = None
            if (
                validation_token is not None
                and validation_token in self._verified_integrity_pairs
            ):
                continue
            try:
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                checksum = manifest["checksum"]
                if (
                    not isinstance(checksum, dict)
                    or checksum.get("algorithm") != "sha256"
                    or not isinstance(checksum.get("value"), str)
                ):
                    raise DataContractError(
                        "checksum must contain sha256 algorithm and value"
                    )
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                DataContractError,
            ) as exc:
                invalid_manifests.append(
                    {
                        "manifest_path": relative(manifest_path),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            actual = _sha256_path(raw_path)
            expected = checksum["value"]
            if actual != expected:
                checksum_mismatches.append(
                    {
                        "raw_path": relative(raw_path),
                        "manifest_path": relative(manifest_path),
                        "expected": expected,
                        "actual": actual,
                    }
                )
                continue
            if validation_token is not None:
                try:
                    validation_token_after = _integrity_pair_token(
                        manifest_path, raw_path
                    )
                except OSError:
                    continue
                if validation_token_after == validation_token:
                    self._verified_integrity_pairs.add(validation_token)

        return {
            "orphan_partials": [relative(path) for path in partials],
            "raw_without_manifest": [
                relative(path) for path in raw_without_manifest
            ],
            "manifest_without_raw": [
                relative(path) for path in manifest_without_raw
            ],
            "checksum_mismatches": sorted(
                checksum_mismatches, key=lambda item: item["raw_path"]
            ),
            "invalid_manifests": sorted(
                invalid_manifests, key=lambda item: item["manifest_path"]
            ),
        }


class RawBatchWriter:
    """Exclusive append handle for one immutable raw JSONL batch."""

    def __init__(
        self,
        *,
        store: CaptureStore,
        source: str,
        batch_id: str,
        schema_version: str,
    ) -> None:
        if not isinstance(schema_version, str) or not schema_version.strip():
            raise DataContractError("schema_version must be a non-empty string")
        self.store = store
        self.source = source
        self.batch_id = batch_id
        self.schema_version = schema_version

        directory = store.raw_dir / source
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{batch_id}.jsonl"
        self.partial_path = directory / f"{batch_id}.jsonl.partial"
        self.manifest_path = directory / f"{batch_id}.manifest.json"
        if self.path.exists() or self.manifest_path.exists():
            raise FileExistsError(
                f"raw batch {source}/{batch_id} is already finalized"
            )
        self._handle = self.partial_path.open(
            "x", encoding="utf-8", newline="\n"
        )
        self._finalized = False
        self._attempted_count = 0
        self._record_count = 0
        self._duplicate_count = 0
        self._seen_ids: set[str] = set()
        self._seen_event_fingerprints: set[str] = set()
        self._semantic_duplicate_count = 0
        self._last_received_at: str | None = None
        self._last_event_at: str | None = None
        self._last_sequence: int | None = None
        self._time_regressions: list[dict[str, Any]] = []
        self._sequence_gaps: list[dict[str, int]] = []
        self._sequence_regressions: list[dict[str, int]] = []
        self._received_min: str | None = None
        self._received_max: str | None = None
        self._event_min: str | None = None
        self._event_max: str | None = None
        self._schema_stats: dict[str, dict[str, Any]] = {}
        self._first_schema_fingerprint: str | None = None
        self._last_schema_fingerprint: str | None = None
        self._schema_drift_count = 0
        self._schema_change_count = 0

    @property
    def finalized(self) -> bool:
        return self._finalized

    def append(
        self,
        *,
        received_at: str | datetime,
        event_at: str | datetime | None,
        sequence: int | None,
        payload: Mapping[str, Any],
    ) -> AppendResult:
        """Append one unique canonical envelope to the active partial batch."""

        if self._finalized or self._handle.closed:
            raise BatchFinalizedError(
                f"raw batch {self.source}/{self.batch_id} is finalized"
            )
        if not isinstance(payload, Mapping):
            raise DataContractError("payload must be a JSON object")

        candidate = {
            "schema_version": self.schema_version,
            "source": self.source,
            "received_at": received_at,
            "event_at": event_at,
            "sequence": sequence,
            "payload": payload,
        }
        base = _base_record(candidate)
        record_id = hashlib.sha256(canonical_json_bytes(base)).hexdigest()
        event_fingerprint = hashlib.sha256(
            canonical_json_bytes(_event_fields(base))
        ).hexdigest()
        fingerprint, descriptor = _schema_fingerprint(payload)
        self._attempted_count += 1
        if record_id in self._seen_ids:
            self._duplicate_count += 1
            return AppendResult(
                record_id=record_id,
                written=False,
                schema_fingerprint=fingerprint,
                event_fingerprint=event_fingerprint,
            )

        row = {
            **base,
            "record_id": record_id,
            "event_fingerprint": event_fingerprint,
            "schema_fingerprint": fingerprint,
        }
        self._handle.write(canonical_json_bytes(row).decode("utf-8"))
        self._handle.write("\n")
        self._handle.flush()

        self._seen_ids.add(record_id)
        if event_fingerprint in self._seen_event_fingerprints:
            self._semantic_duplicate_count += 1
        self._seen_event_fingerprints.add(event_fingerprint)
        self._record_count += 1
        self._update_time_stats(base, record_id)
        self._update_sequence_stats(sequence)
        self._update_schema_stats(fingerprint, descriptor)
        return AppendResult(
            record_id=record_id,
            written=True,
            schema_fingerprint=fingerprint,
            event_fingerprint=event_fingerprint,
        )

    def _update_time_stats(
        self, record: Mapping[str, Any], record_id: str
    ) -> None:
        received_at = record["received_at"]
        event_at = record["event_at"]
        self._received_min = (
            received_at
            if self._received_min is None
            else min(self._received_min, received_at)
        )
        self._received_max = (
            received_at
            if self._received_max is None
            else max(self._received_max, received_at)
        )
        if event_at is not None:
            self._event_min = (
                event_at
                if self._event_min is None
                else min(self._event_min, event_at)
            )
            self._event_max = (
                event_at
                if self._event_max is None
                else max(self._event_max, event_at)
            )

        if (
            self._last_received_at is not None
            and received_at < self._last_received_at
        ):
            self._time_regressions.append(
                {
                    "clock": "received_at",
                    "previous": self._last_received_at,
                    "current": received_at,
                    "record_id": record_id,
                }
            )
        self._last_received_at = received_at
        if event_at is not None:
            if (
                self._last_event_at is not None
                and event_at < self._last_event_at
            ):
                self._time_regressions.append(
                    {
                        "clock": "event_at",
                        "previous": self._last_event_at,
                        "current": event_at,
                        "record_id": record_id,
                    }
                )
            self._last_event_at = event_at

    def _update_sequence_stats(self, sequence: int | None) -> None:
        if sequence is None:
            return
        if self._last_sequence is not None:
            if sequence > self._last_sequence + 1:
                self._sequence_gaps.append(
                    {
                        "after": self._last_sequence,
                        "before": sequence,
                        "missing_start": self._last_sequence + 1,
                        "missing_end": sequence - 1,
                        "missing_count": sequence - self._last_sequence - 1,
                    }
                )
            elif sequence <= self._last_sequence:
                self._sequence_regressions.append(
                    {
                        "previous": self._last_sequence,
                        "current": sequence,
                    }
                )
        self._last_sequence = sequence

    def _update_schema_stats(
        self, fingerprint: str, descriptor: Any
    ) -> None:
        stats = self._schema_stats.setdefault(
            fingerprint,
            {"fingerprint": fingerprint, "count": 0, "descriptor": descriptor},
        )
        stats["count"] += 1
        if self._first_schema_fingerprint is None:
            self._first_schema_fingerprint = fingerprint
        elif fingerprint != self._first_schema_fingerprint:
            self._schema_drift_count += 1
        if (
            self._last_schema_fingerprint is not None
            and fingerprint != self._last_schema_fingerprint
        ):
            self._schema_change_count += 1
        self._last_schema_fingerprint = fingerprint

    def finalize(
        self, *, finalized_at: str | datetime | None = None
    ) -> dict[str, Any]:
        """Finalize the batch, atomically promote it, and emit its manifest."""

        if self._finalized or self._handle.closed:
            raise BatchFinalizedError(
                f"raw batch {self.source}/{self.batch_id} is finalized"
            )
        finalized_timestamp = _normalize_timestamp(
            finalized_at or datetime.now(timezone.utc),
            label="finalized_at",
        )
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        os.replace(self.partial_path, self.path)

        raw_bytes = self.path.stat().st_size
        raw_sha256 = _sha256_path(self.path)
        missing_count = sum(
            gap["missing_count"] for gap in self._sequence_gaps
        )
        semantic_duplicate_rate = (
            str(
                Decimal(self._semantic_duplicate_count)
                / Decimal(self._record_count)
            )
            if self._record_count
            else "0"
        )
        manifest: dict[str, Any] = {
            "manifest_version": "capture-manifest.v1",
            "schema_version": self.schema_version,
            "source": self.source,
            "batch_id": self.batch_id,
            "raw_path": self.path.relative_to(self.store.root).as_posix(),
            "finalized_at": finalized_timestamp,
            "record_count": self._record_count,
            "attempted_count": self._attempted_count,
            "duplicate_count": self._duplicate_count,
            "unique_event_count": len(self._seen_event_fingerprints),
            "semantic_duplicate_count": self._semantic_duplicate_count,
            "semantic_duplicate_rate": semantic_duplicate_rate,
            "time_range": {
                "received_at": {
                    "min": self._received_min,
                    "max": self._received_max,
                },
                "event_at": {
                    "min": self._event_min,
                    "max": self._event_max,
                },
            },
            "time_regression_count": len(self._time_regressions),
            "time_regressions": self._time_regressions,
            "sequence_gap_count": len(self._sequence_gaps),
            "sequence_missing_count": missing_count,
            "sequence_gaps": self._sequence_gaps,
            "sequence_regression_count": len(
                self._sequence_regressions
            ),
            "sequence_regressions": self._sequence_regressions,
            "schema_fingerprints": list(self._schema_stats.values()),
            "schema_drift_count": self._schema_drift_count,
            "schema_change_count": self._schema_change_count,
            "checksum": {
                "algorithm": "sha256",
                "value": raw_sha256,
                "bytes": raw_bytes,
                "lines": self._record_count,
            },
        }
        _atomic_write_bytes(
            self.manifest_path,
            canonical_json_bytes(manifest) + b"\n",
        )
        self.path.chmod(0o444)
        self.manifest_path.chmod(0o444)
        self._finalized = True
        return manifest
