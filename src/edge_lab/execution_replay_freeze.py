"""Verify a finalized production capture freeze without side effects.

The promotion request's individual manifest bindings are not a proof that a
capture window is complete: a caller could omit another finalized batch, leave
an active partial, or relabel arbitrary records as the evidence needed by the
replay.  This module closes that gap with one content-addressed
``CAPTURE_FREEZE.json`` contract.

``verify_production_capture_freeze`` performs only local, no-follow reads.  It
validates the request and freeze hashes, enumerates the complete raw tree,
recomputes every manifest/raw/record binding, and verifies the semantics of all
production-only evidence roles.  Evidence failures are returned as a
structured rejection; no artifact is created or modified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal

from .data_store import canonical_json_bytes, canonical_record_id
from .network_safety import high_confidence_secret_findings
from .short_crypto_catalog import (
    CatalogRejection,
    ShortCryptoTarget,
    parse_short_crypto_announcement,
)


PROMOTION_REQUEST_SCHEMA = "edge-lab.execution-replay-promotion-request.v1"
CAPTURE_FREEZE_SCHEMA = "edge-lab.execution-replay-capture-freeze.v1"
CAPTURE_MANIFEST_SCHEMA = "capture-manifest.v1"
RAW_SCHEMA = "edge-lab-recorder.raw.v1"
SERVICE_RECORD_SCHEMA = (
    "edge-lab-dynamic-short-crypto-service.record.v1"
)
SETTLEMENT_COMMIT_SCHEMA = (
    "edge-lab.short-crypto-settlement-commit.v1"
)
PONG_TOLERANCE_MS = 30_000
PESSIMISTIC_SUBMIT_LATENCY_FLOOR_MS = 250

REQUIRED_ROLE_NAMES = (
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
)

_SETTLEMENT_DEPENDENCY_ROLES = REQUIRED_ROLE_NAMES[:6]
_FREEZE_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "freeze_id",
        "capture_root",
        "target",
        "source_manifests",
        "required_role_ids",
        "safety",
    }
)
_TARGET_KEYS = frozenset(
    {"slug", "condition_id", "opens_at_ms", "closes_at_ms"}
)
_ENTRY_KEYS = frozenset(
    {
        "path",
        "manifest_sha256",
        "raw_path",
        "raw_sha256",
        "raw_bytes",
        "raw_lines",
        "record_ids",
    }
)
_SAFETY_KEYS = frozenset(
    {"orders_submitted", "authenticated_endpoints_used"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONDITION_RE = re.compile(r"^0x[0-9a-f]{64}$")
_TARGET_RE = re.compile(r"^(btc|eth)-updown-(5m|15m)-([0-9]{10})$")
_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DURATION_MS = {"5m": 300_000, "15m": 900_000}
_ROLE_SCHEMAS = MappingProxyType(
    {
        "full_window_capture_close_checkpoint": (
            "edge-lab.execution-replay-close-checkpoint.v1"
        ),
        "decision_to_trade_l2_closure": (
            "edge-lab.execution-replay-l2-closure.v1"
        ),
        "pessimistic_submit_latency": (
            "edge-lab.execution-replay-submit-latency.v1"
        ),
        "settlement_operation_cost": (
            "edge-lab.execution-replay-settlement-cost.v1"
        ),
    }
)
_SUBMIT_LATENCY_PROVENANCE = frozenset(
    {
        "public_capture_pessimistic_floor",
        "measured_public_capture_upper_bound",
    }
)
_SETTLEMENT_COST_PROVENANCE = frozenset(
    {
        "public_capture_pessimistic_upper_bound",
        "measured_public_operation_upper_bound",
    }
)


class CaptureFreezeRejection(ValueError):
    """One production freeze invariant failed."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class CaptureFreezeTarget:
    """Target identity bound by a verified production freeze."""

    slug: str
    condition_id: str
    opens_at_ms: int
    closes_at_ms: int
    asset: Literal["btc", "eth"]
    horizon: Literal["5m", "15m"]


@dataclass(frozen=True)
class CaptureFreezeVerification:
    """Structured result of production freeze verification."""

    verified: bool
    status: Literal["verified", "rejected"]
    freeze_id: str | None
    freeze_path: Path | None
    target: CaptureFreezeTarget | None
    source_manifest_paths: tuple[str, ...]
    source_manifest_hashes: Mapping[str, str]
    raw_hashes: Mapping[str, str]
    record_ids: tuple[str, ...]
    required_role_ids: Mapping[str, str]
    reason_codes: tuple[str, ...] = ()
    rejection_detail: str | None = None


@dataclass(frozen=True)
class _FreezeEntry:
    manifest_path_text: str
    manifest_path: Path
    manifest_sha256: str
    raw_path_text: str
    raw_path: Path
    raw_sha256: str
    raw_bytes: int
    raw_lines: int
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class _VerifiedBatch:
    entry: _FreezeEntry
    manifest_bytes: bytes
    raw_bytes: bytes
    records: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class _Inventory:
    manifests: frozenset[Path]
    raws: frozenset[Path]


def _reject(code: str, detail: str) -> None:
    raise CaptureFreezeRejection(code, detail)


def _mapping(value: Any, *, code: str, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _reject(code, f"{label} must be an object")
    return value


def _sequence(value: Any, *, code: str, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        _reject(code, f"{label} must be an array")
    return value


def _string(value: Any, *, code: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _reject(code, f"{label} must be a non-empty string")
    return value.strip()


def _integer(
    value: Any,
    *,
    code: str,
    label: str,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _reject(code, f"{label} must be an integer")
    if minimum is not None and value < minimum:
        _reject(code, f"{label} must be at least {minimum}")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    expected: frozenset[str] | set[str],
    *,
    code: str,
    label: str,
) -> None:
    actual = set(value)
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        _reject(
            code,
            f"{label} keys mismatch; missing={missing}, extra={extra}",
        )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_read(path: Path, *, label: str) -> bytes:
    """Read a regular file from a no-follow descriptor and detect mutation."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _reject("capture_file_unreadable", f"{label} cannot be opened: {exc}")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _reject(
                "capture_path_not_regular",
                f"{label} must be a regular file",
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        fingerprint_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        fingerprint_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if fingerprint_before != fingerprint_after:
            _reject(
                "capture_file_changed_during_read",
                f"{label} changed while it was read",
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _json_document(raw: bytes, *, code: str, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        _reject(code, f"{label} is not valid UTF-8 JSON: {exc}")
    return _mapping(value, code=code, label=label)


def _declared_parts(value: Any, *, code: str, label: str) -> tuple[str, ...]:
    text = _string(value, code=code, label=label)
    if "\\" in text:
        _reject(code, f"{label} must use POSIX separators")
    pure = PurePosixPath(text)
    parts = pure.parts
    if (
        pure.is_absolute()
        or not parts
        or text != pure.as_posix()
        or any(
            part in {"", ".", ".."}
            or _SAFE_PATH_COMPONENT.fullmatch(part) is None
            for part in parts
        )
    ):
        _reject(code, f"{label} is not a safe canonical relative path")
    return parts


def _safe_existing_path(
    base: Path,
    value: Any,
    *,
    code: str,
    label: str,
    expect_directory: bool = False,
) -> tuple[str, Path]:
    parts = _declared_parts(value, code=code, label=label)
    candidate = base.joinpath(*parts)
    current = base
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            _reject(code, f"{label} does not exist: {exc}")
        if stat.S_ISLNK(metadata.st_mode):
            _reject(
                "capture_symlink_forbidden",
                f"{label} traverses a symlink: {current}",
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            _reject(code, f"{label} has a non-directory parent component")
    final = candidate.lstat()
    if expect_directory:
        if not stat.S_ISDIR(final.st_mode):
            _reject(code, f"{label} must be a directory")
    elif not stat.S_ISREG(final.st_mode):
        _reject(code, f"{label} must be a regular file")
    return PurePosixPath(*parts).as_posix(), candidate


def _safe_request_path(path: Path) -> tuple[Path, Path, bytes]:
    if path.name.endswith(".partial"):
        _reject(
            "request_path_invalid",
            "promotion request cannot be a partial file",
        )
    try:
        metadata = path.lstat()
    except OSError as exc:
        _reject(
            "request_path_invalid",
            f"promotion request does not exist: {exc}",
        )
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _reject(
            "request_path_invalid",
            "promotion request must be a non-symlink regular file",
        )
    raw = _stable_read(path, label="promotion request")
    return path.resolve(strict=True), path.resolve(strict=True).parent, raw


def _scan_secrets(value: Any, *, label: str) -> None:
    findings = high_confidence_secret_findings(value, path=label)
    if findings:
        summary = ", ".join(
            f"{item['kind']}@{item['path']}" for item in findings[:3]
        )
        _reject(
            "credential_material_forbidden",
            f"{label} contains credential-like material: {summary}",
        )


def _validate_target(value: Any) -> CaptureFreezeTarget:
    target = _mapping(
        value,
        code="capture_target_invalid",
        label="capture freeze target",
    )
    _exact_keys(
        target,
        _TARGET_KEYS,
        code="capture_target_invalid",
        label="capture freeze target",
    )
    slug = _string(
        target.get("slug"),
        code="capture_target_invalid",
        label="target.slug",
    )
    match = _TARGET_RE.fullmatch(slug)
    if match is None:
        _reject(
            "capture_target_invalid",
            "target.slug must be a BTC/ETH 5m/15m up/down slug",
        )
    asset_text, horizon_text, epoch_text = match.groups()
    opens_at_ms = _integer(
        target.get("opens_at_ms"),
        code="capture_target_invalid",
        label="target.opens_at_ms",
        minimum=0,
    )
    closes_at_ms = _integer(
        target.get("closes_at_ms"),
        code="capture_target_invalid",
        label="target.closes_at_ms",
        minimum=0,
    )
    if opens_at_ms != int(epoch_text) * 1_000:
        _reject(
            "capture_target_window_mismatch",
            "target.opens_at_ms does not equal the slug epoch",
        )
    if closes_at_ms != opens_at_ms + _DURATION_MS[horizon_text]:
        _reject(
            "capture_target_window_mismatch",
            "target.closes_at_ms does not equal the exact horizon",
        )
    condition_id = _string(
        target.get("condition_id"),
        code="capture_target_invalid",
        label="target.condition_id",
    )
    if _CONDITION_RE.fullmatch(condition_id) is None:
        _reject(
            "capture_target_invalid",
            "target.condition_id must be lowercase 0x plus 32 bytes",
        )
    return CaptureFreezeTarget(
        slug=slug,
        condition_id=condition_id,
        opens_at_ms=opens_at_ms,
        closes_at_ms=closes_at_ms,
        asset=asset_text,  # type: ignore[arg-type]
        horizon=horizon_text,  # type: ignore[arg-type]
    )


def _validate_safety(value: Any) -> None:
    safety = _mapping(
        value,
        code="capture_safety_invalid",
        label="capture freeze safety",
    )
    _exact_keys(
        safety,
        _SAFETY_KEYS,
        code="capture_safety_invalid",
        label="capture freeze safety",
    )
    for key in sorted(_SAFETY_KEYS):
        if (
            isinstance(safety.get(key), bool)
            or not isinstance(safety.get(key), int)
            or safety.get(key) != 0
        ):
            _reject(
                "capture_safety_invalid",
                f"safety.{key} must be the exact integer zero",
            )


def _validate_role_ids(value: Any) -> Mapping[str, str]:
    roles = _mapping(
        value,
        code="required_role_ids_invalid",
        label="required_role_ids",
    )
    _exact_keys(
        roles,
        set(REQUIRED_ROLE_NAMES),
        code="required_role_ids_invalid",
        label="required_role_ids",
    )
    normalized: dict[str, str] = {}
    for name in REQUIRED_ROLE_NAMES:
        record_id = _string(
            roles.get(name),
            code="required_role_ids_invalid",
            label=f"required_role_ids.{name}",
        )
        if _SHA256_RE.fullmatch(record_id) is None:
            _reject(
                "required_role_ids_invalid",
                f"required_role_ids.{name} is not a sha256 record ID",
            )
        normalized[name] = record_id
    if len(set(normalized.values())) != len(normalized):
        _reject(
            "required_role_ids_not_distinct",
            "every required production role must bind a distinct record",
        )
    return MappingProxyType(normalized)


def _validate_entries(
    value: Any,
    *,
    base: Path,
    capture_root_text: str,
) -> tuple[_FreezeEntry, ...]:
    rows = _sequence(
        value,
        code="capture_freeze_inventory_invalid",
        label="source_manifests",
    )
    if not rows:
        _reject(
            "capture_freeze_inventory_invalid",
            "source_manifests cannot be empty",
        )
    entries: list[_FreezeEntry] = []
    seen_manifests: set[str] = set()
    seen_raws: set[str] = set()
    for index, raw_row in enumerate(rows):
        row = _mapping(
            raw_row,
            code="capture_freeze_inventory_invalid",
            label=f"source_manifests[{index}]",
        )
        _exact_keys(
            row,
            _ENTRY_KEYS,
            code="capture_freeze_inventory_invalid",
            label=f"source_manifests[{index}]",
        )
        manifest_text, manifest_path = _safe_existing_path(
            base,
            row.get("path"),
            code="capture_freeze_inventory_invalid",
            label=f"source_manifests[{index}].path",
        )
        raw_text, raw_path = _safe_existing_path(
            base,
            row.get("raw_path"),
            code="capture_freeze_inventory_invalid",
            label=f"source_manifests[{index}].raw_path",
        )
        expected_manifest_prefix = f"{capture_root_text}/raw/"
        if (
            not manifest_text.startswith(expected_manifest_prefix)
            or not manifest_text.endswith(".manifest.json")
            or len(PurePosixPath(manifest_text).parts)
            != len(PurePosixPath(capture_root_text).parts) + 3
        ):
            _reject(
                "capture_freeze_inventory_invalid",
                f"{manifest_text} is not under capture_root/raw/<source>",
            )
        expected_raw = (
            manifest_text.removesuffix(".manifest.json") + ".jsonl"
        )
        if raw_text != expected_raw:
            _reject(
                "capture_freeze_inventory_invalid",
                f"{manifest_text} does not bind its exact raw sibling",
            )
        if manifest_text in seen_manifests or raw_text in seen_raws:
            _reject(
                "capture_freeze_inventory_invalid",
                "source manifest/raw paths must be unique",
            )
        seen_manifests.add(manifest_text)
        seen_raws.add(raw_text)
        manifest_sha = _string(
            row.get("manifest_sha256"),
            code="capture_freeze_inventory_invalid",
            label=f"source_manifests[{index}].manifest_sha256",
        )
        raw_sha = _string(
            row.get("raw_sha256"),
            code="capture_freeze_inventory_invalid",
            label=f"source_manifests[{index}].raw_sha256",
        )
        if (
            _SHA256_RE.fullmatch(manifest_sha) is None
            or _SHA256_RE.fullmatch(raw_sha) is None
        ):
            _reject(
                "capture_freeze_inventory_invalid",
                "manifest/raw hashes must be lowercase sha256",
            )
        raw_byte_count = _integer(
            row.get("raw_bytes"),
            code="capture_freeze_inventory_invalid",
            label=f"source_manifests[{index}].raw_bytes",
            minimum=0,
        )
        raw_line_count = _integer(
            row.get("raw_lines"),
            code="capture_freeze_inventory_invalid",
            label=f"source_manifests[{index}].raw_lines",
            minimum=0,
        )
        raw_record_ids = _sequence(
            row.get("record_ids"),
            code="capture_freeze_inventory_invalid",
            label=f"source_manifests[{index}].record_ids",
        )
        record_ids: list[str] = []
        for record_index, candidate in enumerate(raw_record_ids):
            record_id = _string(
                candidate,
                code="capture_freeze_inventory_invalid",
                label=(
                    f"source_manifests[{index}]."
                    f"record_ids[{record_index}]"
                ),
            )
            if _SHA256_RE.fullmatch(record_id) is None:
                _reject(
                    "capture_freeze_inventory_invalid",
                    f"invalid record ID in {manifest_text}",
                )
            record_ids.append(record_id)
        if len(set(record_ids)) != len(record_ids):
            _reject(
                "capture_freeze_inventory_invalid",
                f"duplicate record ID within {manifest_text}",
            )
        entries.append(
            _FreezeEntry(
                manifest_path_text=manifest_text,
                manifest_path=manifest_path,
                manifest_sha256=manifest_sha,
                raw_path_text=raw_text,
                raw_path=raw_path,
                raw_sha256=raw_sha,
                raw_bytes=raw_byte_count,
                raw_lines=raw_line_count,
                record_ids=tuple(record_ids),
            )
        )
    if [entry.manifest_path_text for entry in entries] != sorted(
        entry.manifest_path_text for entry in entries
    ):
        _reject(
            "capture_freeze_inventory_not_canonical",
            "source_manifests must be sorted by path",
        )
    return tuple(entries)


def _scan_inventory(raw_root: Path) -> _Inventory:
    manifests: set[Path] = set()
    raws: set[Path] = set()
    for directory_text, directory_names, file_names in os.walk(
        raw_root,
        topdown=True,
        followlinks=False,
    ):
        directory = Path(directory_text)
        relative_directory = directory.relative_to(raw_root)
        if len(relative_directory.parts) > 1:
            _reject(
                "capture_inventory_layout_invalid",
                "raw batches must be directly under raw/<source>",
            )
        for name in list(directory_names):
            child = directory / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                _reject(
                    "capture_symlink_forbidden",
                    f"capture inventory contains symlink directory {child}",
                )
            if not stat.S_ISDIR(metadata.st_mode):
                _reject(
                    "capture_inventory_layout_invalid",
                    f"capture inventory contains non-directory {child}",
                )
            if (
                _SAFE_PATH_COMPONENT.fullmatch(name) is None
                or len(relative_directory.parts) >= 1
            ):
                _reject(
                    "capture_inventory_layout_invalid",
                    f"unexpected capture directory {child}",
                )
        for name in file_names:
            child = directory / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                _reject(
                    "capture_symlink_forbidden",
                    f"capture inventory contains symlink file {child}",
                )
            if not stat.S_ISREG(metadata.st_mode):
                _reject(
                    "capture_inventory_layout_invalid",
                    f"capture inventory contains non-regular file {child}",
                )
            if name.endswith(".jsonl.partial"):
                _reject(
                    "capture_partial_present",
                    f"capture inventory contains active/orphan partial {child}",
                )
            if len(relative_directory.parts) != 1:
                _reject(
                    "capture_inventory_layout_invalid",
                    f"raw artifact is not under raw/<source>: {child}",
                )
            if name.endswith(".manifest.json"):
                manifests.add(child)
            elif name.endswith(".jsonl"):
                raws.add(child)
            else:
                _reject(
                    "capture_inventory_unexpected_file",
                    f"unexpected file in raw inventory: {child}",
                )
    paired_raws = {
        path.with_name(
            f"{path.name.removesuffix('.manifest.json')}.jsonl"
        )
        for path in manifests
    }
    paired_manifests = {
        path.with_suffix(".manifest.json")
        for path in raws
    }
    if paired_raws != raws or paired_manifests != manifests:
        _reject(
            "capture_inventory_pair_mismatch",
            "every finalized manifest must have exactly one raw sibling",
        )
    return _Inventory(
        manifests=frozenset(manifests),
        raws=frozenset(raws),
    )


def _validate_manifest_time(value: Any, *, label: str) -> None:
    text = _string(
        value,
        code="source_manifest_invalid",
        label=label,
    )
    try:
        parsed = datetime.fromisoformat(
            f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError:
        _reject("source_manifest_invalid", f"{label} is not ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _reject("source_manifest_invalid", f"{label} lacks a timezone")


def _validate_batch(
    entry: _FreezeEntry,
    *,
    capture_root_text: str,
    seen_record_ids: set[str],
) -> _VerifiedBatch:
    manifest_bytes = _stable_read(
        entry.manifest_path,
        label=f"source manifest {entry.manifest_path_text}",
    )
    if _sha256_bytes(manifest_bytes) != entry.manifest_sha256:
        _reject(
            "source_manifest_hash_mismatch",
            f"manifest hash drifted: {entry.manifest_path_text}",
        )
    manifest = _json_document(
        manifest_bytes,
        code="source_manifest_invalid",
        label=entry.manifest_path_text,
    )
    if manifest_bytes != canonical_json_bytes(manifest) + b"\n":
        _reject(
            "source_manifest_not_canonical",
            f"manifest is not canonical JSON: {entry.manifest_path_text}",
        )
    _scan_secrets(manifest, label=f"manifest[{entry.manifest_path_text}]")
    if (
        manifest.get("manifest_version") != CAPTURE_MANIFEST_SCHEMA
        or manifest.get("schema_version") != RAW_SCHEMA
    ):
        _reject(
            "source_manifest_schema_mismatch",
            f"unsupported manifest schema: {entry.manifest_path_text}",
        )
    source = _string(
        manifest.get("source"),
        code="source_manifest_invalid",
        label=f"{entry.manifest_path_text}.source",
    )
    manifest_parts = PurePosixPath(entry.manifest_path_text).parts
    if source != manifest_parts[-2]:
        _reject(
            "source_manifest_source_mismatch",
            f"manifest source does not match its parent: {entry.manifest_path_text}",
        )
    expected_batch = PurePosixPath(entry.manifest_path_text).name.removesuffix(
        ".manifest.json"
    )
    if manifest.get("batch_id") != expected_batch:
        _reject(
            "source_manifest_batch_mismatch",
            f"manifest batch_id does not match filename: {entry.manifest_path_text}",
        )
    expected_manifest_raw = PurePosixPath(
        entry.raw_path_text
    ).relative_to(PurePosixPath(capture_root_text)).as_posix()
    if manifest.get("raw_path") != expected_manifest_raw:
        _reject(
            "source_manifest_raw_path_mismatch",
            f"manifest raw_path does not bind its sibling: {entry.manifest_path_text}",
        )
    _validate_manifest_time(
        manifest.get("finalized_at"),
        label=f"{entry.manifest_path_text}.finalized_at",
    )
    checksum = _mapping(
        manifest.get("checksum"),
        code="source_manifest_invalid",
        label=f"{entry.manifest_path_text}.checksum",
    )
    raw_bytes = _stable_read(
        entry.raw_path,
        label=f"source raw {entry.raw_path_text}",
    )
    actual_raw_sha = _sha256_bytes(raw_bytes)
    lines = raw_bytes.splitlines()
    if raw_bytes and not raw_bytes.endswith(b"\n"):
        _reject(
            "source_raw_integrity_mismatch",
            f"raw JSONL lacks trailing newline: {entry.raw_path_text}",
        )
    manifest_count = _integer(
        manifest.get("record_count"),
        code="source_manifest_invalid",
        label=f"{entry.manifest_path_text}.record_count",
        minimum=0,
    )
    if (
        checksum.get("algorithm") != "sha256"
        or checksum.get("value") != actual_raw_sha
        or checksum.get("bytes") != len(raw_bytes)
        or checksum.get("lines") != len(lines)
        or manifest_count != len(lines)
        or entry.raw_sha256 != actual_raw_sha
        or entry.raw_bytes != len(raw_bytes)
        or entry.raw_lines != len(lines)
    ):
        _reject(
            "source_raw_integrity_mismatch",
            f"raw sha/bytes/lines drifted: {entry.raw_path_text}",
        )
    records: list[Mapping[str, Any]] = []
    actual_record_ids: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except (UnicodeError, json.JSONDecodeError):
            _reject(
                "source_raw_invalid",
                f"invalid JSON at {entry.raw_path_text}:{line_number}",
            )
        record = _mapping(
            value,
            code="source_raw_invalid",
            label=f"{entry.raw_path_text}:{line_number}",
        )
        if line != canonical_json_bytes(record):
            _reject(
                "source_raw_not_canonical",
                f"non-canonical JSON at {entry.raw_path_text}:{line_number}",
            )
        if record.get("schema_version") != RAW_SCHEMA:
            _reject(
                "source_record_schema_mismatch",
                f"outer schema drift at {entry.raw_path_text}:{line_number}",
            )
        if record.get("source") != source:
            _reject(
                "source_record_source_mismatch",
                f"outer source drift at {entry.raw_path_text}:{line_number}",
            )
        record_id = record.get("record_id")
        if (
            not isinstance(record_id, str)
            or _SHA256_RE.fullmatch(record_id) is None
            or record_id != canonical_record_id(record)
        ):
            _reject(
                "record_id_mismatch",
                f"non-canonical record ID at {entry.raw_path_text}:{line_number}",
            )
        if record_id in seen_record_ids:
            _reject(
                "duplicate_record_id",
                f"record ID appears in multiple raw rows: {record_id}",
            )
        seen_record_ids.add(record_id)
        _scan_secrets(record, label=f"raw[{record_id}]")
        actual_record_ids.append(record_id)
        records.append(record)
    if tuple(actual_record_ids) != entry.record_ids:
        _reject(
            "source_record_inventory_mismatch",
            f"record_ids drifted: {entry.raw_path_text}",
        )
    return _VerifiedBatch(
        entry=entry,
        manifest_bytes=manifest_bytes,
        raw_bytes=raw_bytes,
        records=tuple(records),
    )


def _inner(record: Mapping[str, Any], *, role: str) -> Mapping[str, Any]:
    value = record.get("payload")
    return _mapping(
        value,
        code="required_role_semantics_invalid",
        label=f"{role} recorder envelope",
    )


def _domain_payload(
    record: Mapping[str, Any],
    *,
    role: str,
) -> Mapping[str, Any]:
    inner = _inner(record, role=role)
    return _mapping(
        inner.get("payload"),
        code="required_role_semantics_invalid",
        label=f"{role} payload",
    )


def _timestamp_ms(value: Any, *, role: str) -> int:
    text = _string(
        value,
        code="required_role_semantics_invalid",
        label=f"{role}.received_at",
    )
    try:
        parsed = datetime.fromisoformat(
            f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError:
        _reject(
            "required_role_semantics_invalid",
            f"{role}.received_at is not ISO-8601",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _reject(
            "required_role_semantics_invalid",
            f"{role}.received_at lacks timezone",
        )
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000)


def _validate_announcement_role(
    record: Mapping[str, Any],
    *,
    target: CaptureFreezeTarget,
) -> ShortCryptoTarget:
    try:
        parsed = parse_short_crypto_announcement(record)
    except CatalogRejection as exc:
        _reject(
            "required_role_semantics_invalid",
            f"announcement role rejected: {exc.code}",
        )
    if parsed is None:
        _reject(
            "required_role_semantics_invalid",
            "announcement role is not a supported target announcement",
        )
    if (
        parsed.slug != target.slug
        or parsed.condition_id != target.condition_id
        or parsed.opens_at_ms != target.opens_at_ms
        or parsed.closes_at_ms != target.closes_at_ms
    ):
        _reject(
            "required_role_target_mismatch",
            "announcement identity/window differs from capture target",
        )
    return parsed


def _string_array(value: Any) -> tuple[str, ...] | None:
    candidate = value
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except json.JSONDecodeError:
            return None
    if not isinstance(candidate, Sequence) or isinstance(
        candidate, (str, bytes, bytearray)
    ):
        return None
    if any(not isinstance(item, str) for item in candidate):
        return None
    return tuple(candidate)


def _gamma_market_candidates(raw_json: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(raw_json, Sequence) and not isinstance(
        raw_json, (str, bytes, bytearray)
    ):
        candidates = raw_json
    elif isinstance(raw_json, Mapping):
        markets = raw_json.get("markets")
        if isinstance(markets, Sequence) and not isinstance(
            markets, (str, bytes, bytearray)
        ):
            candidates = markets
        else:
            candidates = (raw_json,)
    else:
        return ()
    return tuple(
        item for item in candidates if isinstance(item, Mapping)
    )


def _validate_gamma_role(
    record: Mapping[str, Any],
    *,
    target: CaptureFreezeTarget,
    announcement: ShortCryptoTarget,
) -> None:
    inner = _inner(record, role="Gamma")
    if (
        record.get("source") != "gamma_http"
        or inner.get("source") != "gamma_http"
        or inner.get("schema_version")
        not in {
            "gamma-http.snapshot.v1",
            "gamma-http.target-snapshot.v1",
        }
        or inner.get("kind") in {"diagnostic", "lifecycle"}
    ):
        _reject(
            "required_role_semantics_invalid",
            "Gamma role is not an allowlisted public Gamma snapshot",
        )
    payload = _domain_payload(record, role="Gamma")
    responses = _sequence(
        payload.get("responses"),
        code="required_role_semantics_invalid",
        label="Gamma responses",
    )
    matches: list[Mapping[str, Any]] = []
    for response_value in responses:
        response = _mapping(
            response_value,
            code="required_role_semantics_invalid",
            label="Gamma response",
        )
        if response.get("resource") not in {"gamma_market", "gamma_markets"}:
            continue
        provenance = _mapping(
            response.get("provenance"),
            code="required_role_semantics_invalid",
            label="Gamma provenance",
        )
        if provenance.get("status_code") != 200:
            _reject(
                "required_role_semantics_invalid",
                "Gamma role must bind a successful public response",
            )
        for market in _gamma_market_candidates(response.get("raw_json")):
            condition = market.get("conditionId")
            if (
                market.get("slug") == target.slug
                or (
                    isinstance(condition, str)
                    and condition.lower() == target.condition_id
                )
            ):
                matches.append(market)
    if len(matches) != 1:
        _reject(
            "required_role_semantics_invalid",
            "Gamma role must contain exactly one target market",
        )
    market = matches[0]
    condition = market.get("conditionId")
    if (
        market.get("slug") != target.slug
        or not isinstance(condition, str)
        or condition.lower() != target.condition_id
        or str(market.get("id", "")) != announcement.market_id
        or _string_array(market.get("outcomes")) != ("Up", "Down")
        or _string_array(market.get("clobTokenIds"))
        != (announcement.up_token_id, announcement.down_token_id)
        or market.get("closed") is not True
    ):
        _reject(
            "required_role_target_mismatch",
            "Gamma target identity/token mapping/final state differs",
        )


def _validate_chainlink_role(
    record: Mapping[str, Any],
    *,
    role: str,
    target: CaptureFreezeTarget,
    boundary_ms: int,
    expected_symbol: str,
) -> None:
    inner = _inner(record, role=role)
    update = _domain_payload(record, role=role)
    payload = _mapping(
        update.get("payload"),
        code="required_role_semantics_invalid",
        label=f"{role} Chainlink payload",
    )
    if (
        record.get("source") != "rtds_ws"
        or inner.get("source") != "rtds_ws"
        or inner.get("schema_version")
        != "rtds.crypto_prices_chainlink.update.v1"
        or inner.get("event_type")
        != "crypto_prices_chainlink.update"
        or update.get("topic") != "crypto_prices_chainlink"
        or update.get("type") != "update"
        or payload.get("symbol") != expected_symbol
        or payload.get("timestamp") != boundary_ms
    ):
        _reject(
            "required_role_semantics_invalid",
            f"{role} is not the exact target Chainlink boundary",
        )
    try:
        price = Decimal(str(payload.get("value")))
    except InvalidOperation:
        _reject(
            "required_role_semantics_invalid",
            f"{role} Chainlink value is not decimal",
        )
    if not price.is_finite() or price <= 0:
        _reject(
            "required_role_semantics_invalid",
            f"{role} Chainlink value must be finite and positive",
        )
    received_at_ms = _timestamp_ms(record.get("received_at"), role=role)
    if received_at_ms < boundary_ms:
        _reject(
            "required_role_semantics_invalid",
            f"{role} arrived before its source boundary timestamp",
        )
    if target.asset not in expected_symbol:
        _reject(
            "required_role_target_mismatch",
            f"{role} symbol does not match target asset",
        )


def _validate_pong_roles(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    target: CaptureFreezeTarget,
) -> None:
    values: list[tuple[int, int, str]] = []
    for role, record in (("PONG before", before), ("PONG after", after)):
        inner = _inner(record, role=role)
        connection = inner.get("connection_id")
        monotonic_ns = inner.get("monotonic_ns")
        if (
            record.get("source") != "clob_market_ws"
            or inner.get("source") != "clob_market_ws"
            or inner.get("schema_version")
            != "edge-lab-recorder.heartbeat-ack.v1"
            or inner.get("event_type") != "heartbeat_ack"
            or not isinstance(inner.get("raw_frame"), str)
            or inner["raw_frame"].strip().upper() != "PONG"
            or not isinstance(connection, str)
            or not connection
            or isinstance(monotonic_ns, bool)
            or not isinstance(monotonic_ns, int)
        ):
            _reject(
                "required_role_semantics_invalid",
                f"{role} is not a persisted CLOB heartbeat acknowledgement",
            )
        values.append(
            (
                _timestamp_ms(record.get("received_at"), role=role),
                monotonic_ns,
                connection,
            )
        )
    before_ms, before_monotonic, before_connection = values[0]
    after_ms, after_monotonic, after_connection = values[1]
    if (
        before_connection != after_connection
        or before_ms > target.closes_at_ms
        or after_ms < target.closes_at_ms
        or target.closes_at_ms - before_ms > PONG_TOLERANCE_MS
        or after_ms - target.closes_at_ms > PONG_TOLERANCE_MS
        or before_monotonic >= after_monotonic
    ):
        _reject(
            "required_role_semantics_invalid",
            "PONG roles do not form one same-connection close bracket",
        )


def _validate_settlement_commit(
    record: Mapping[str, Any],
    *,
    target: CaptureFreezeTarget,
    roles: Mapping[str, str],
) -> None:
    inner = _inner(record, role="atomic settlement commit")
    payload = _domain_payload(record, role="atomic settlement commit")
    required_ids = _sequence(
        payload.get("required_record_ids"),
        code="required_role_semantics_invalid",
        label="settlement required_record_ids",
    )
    expected_ids = [roles[name] for name in _SETTLEMENT_DEPENDENCY_ROLES]
    if (
        record.get("source") != "dynamic_short_crypto_service"
        or inner.get("source") != "dynamic_short_crypto_service"
        or inner.get("schema_version") != SERVICE_RECORD_SCHEMA
        or inner.get("event_type") != "settlement_reconciled"
        or payload.get("schema_version") != SETTLEMENT_COMMIT_SCHEMA
        or payload.get("transition") != "commit_settlement"
        or payload.get("action") != "strict_settlement_committed"
        or payload.get("status") != "resolved"
        or payload.get("slug") != target.slug
        or payload.get("condition_id") != target.condition_id
        or list(required_ids) != expected_ids
    ):
        _reject(
            "required_role_semantics_invalid",
            "atomic settlement commit does not bind the exact six prerequisites",
        )


def _validate_service_role(
    record: Mapping[str, Any],
    *,
    role: str,
    target: CaptureFreezeTarget,
) -> Mapping[str, Any]:
    inner = _inner(record, role=role)
    payload = _domain_payload(record, role=role)
    if (
        record.get("source") != "dynamic_short_crypto_service"
        or inner.get("source") != "dynamic_short_crypto_service"
        or inner.get("schema_version") != SERVICE_RECORD_SCHEMA
        or inner.get("event_type") != role
        or payload.get("schema_version") != _ROLE_SCHEMAS[role]
        or payload.get("evidence_role") != role
        or payload.get("slug") != target.slug
        or payload.get("condition_id") != target.condition_id
        or payload.get("opens_at_ms") != target.opens_at_ms
        or payload.get("closes_at_ms") != target.closes_at_ms
    ):
        _reject(
            "required_role_semantics_invalid",
            f"{role} record does not bind the exact target/window schema",
        )
    return payload


def _validate_production_roles(
    records: Mapping[str, Mapping[str, Any]],
    *,
    roles: Mapping[str, str],
    target: CaptureFreezeTarget,
) -> None:
    missing = sorted(set(roles.values()) - set(records))
    if missing:
        _reject(
            "required_role_record_missing",
            f"required role IDs are absent from frozen raw inventory: {missing}",
        )
    announcement = _validate_announcement_role(
        records[roles["durable_announcement_record_id"]],
        target=target,
    )
    _validate_gamma_role(
        records[roles["durable_gamma_record_id"]],
        target=target,
        announcement=announcement,
    )
    _validate_chainlink_role(
        records[roles["durable_chainlink_open_record_id"]],
        role="Chainlink open",
        target=target,
        boundary_ms=target.opens_at_ms,
        expected_symbol=announcement.source_symbol,
    )
    _validate_chainlink_role(
        records[roles["durable_chainlink_close_record_id"]],
        role="Chainlink close",
        target=target,
        boundary_ms=target.closes_at_ms,
        expected_symbol=announcement.source_symbol,
    )
    _validate_pong_roles(
        records[roles["durable_close_pong_before_record_id"]],
        records[roles["durable_close_pong_after_record_id"]],
        target=target,
    )
    _validate_settlement_commit(
        records[roles["atomic_settlement_commit_record_id"]],
        target=target,
        roles=roles,
    )
    close_checkpoint = _validate_service_role(
        records[roles["full_window_capture_close_checkpoint_id"]],
        role="full_window_capture_close_checkpoint",
        target=target,
    )
    if (
        _integer(
            close_checkpoint.get("finalized_through_ms"),
            code="required_role_semantics_invalid",
            label="close checkpoint finalized_through_ms",
            minimum=0,
        )
        < target.closes_at_ms
    ):
        _reject(
            "required_role_semantics_invalid",
            "close checkpoint does not finalize through market close",
        )
    l2_closure = _validate_service_role(
        records[roles["decision_to_trade_l2_closure_id"]],
        role="decision_to_trade_l2_closure",
        target=target,
    )
    if (
        _integer(
            l2_closure.get("decision_count"),
            code="required_role_semantics_invalid",
            label="L2 closure decision_count",
            minimum=1,
        )
        < 1
        or _integer(
            l2_closure.get("public_trade_count"),
            code="required_role_semantics_invalid",
            label="L2 closure public_trade_count",
            minimum=0,
        )
        < 0
        or _integer(
            l2_closure.get("closed_through_ms"),
            code="required_role_semantics_invalid",
            label="L2 closure closed_through_ms",
            minimum=0,
        )
        < target.closes_at_ms
    ):
        _reject(
            "required_role_semantics_invalid",
            "L2 closure does not cover a decision through market close",
        )
    submit_latency = _validate_service_role(
        records[roles["pessimistic_submit_latency_evidence_id"]],
        role="pessimistic_submit_latency",
        target=target,
    )
    _integer(
        submit_latency.get("latency_ms"),
        code="required_role_semantics_invalid",
        label="submit latency_ms",
        minimum=PESSIMISTIC_SUBMIT_LATENCY_FLOOR_MS,
    )
    if (
        submit_latency.get("provenance")
        not in _SUBMIT_LATENCY_PROVENANCE
    ):
        _reject(
            "required_role_semantics_invalid",
            "submit latency provenance is not in the production allowlist",
        )
    settlement_cost = _validate_service_role(
        records[roles["settlement_operation_cost_evidence_id"]],
        role="settlement_operation_cost",
        target=target,
    )
    try:
        amount = Decimal(str(settlement_cost.get("amount")))
    except InvalidOperation:
        _reject(
            "required_role_semantics_invalid",
            "settlement operation amount is not decimal",
        )
    if (
        not amount.is_finite()
        or amount <= 0
        or settlement_cost.get("currency") != "USDC"
        or settlement_cost.get("provenance")
        not in _SETTLEMENT_COST_PROVENANCE
    ):
        _reject(
            "required_role_semantics_invalid",
            "settlement cost is not a positive production USDC bound",
        )


def _validate_request_inventory(
    request: Mapping[str, Any],
    entries: tuple[_FreezeEntry, ...],
) -> None:
    rows = _sequence(
        request.get("source_manifests"),
        code="request_source_inventory_invalid",
        label="request.source_manifests",
    )
    actual: list[tuple[str, str]] = []
    for index, raw_row in enumerate(rows):
        row = _mapping(
            raw_row,
            code="request_source_inventory_invalid",
            label=f"request.source_manifests[{index}]",
        )
        _exact_keys(
            row,
            {"path", "sha256"},
            code="request_source_inventory_invalid",
            label=f"request.source_manifests[{index}]",
        )
        path_text = PurePosixPath(
            *_declared_parts(
                row.get("path"),
                code="request_source_inventory_invalid",
                label=f"request.source_manifests[{index}].path",
            )
        ).as_posix()
        sha256 = _string(
            row.get("sha256"),
            code="request_source_inventory_invalid",
            label=f"request.source_manifests[{index}].sha256",
        )
        if _SHA256_RE.fullmatch(sha256) is None:
            _reject(
                "request_source_inventory_invalid",
                "request source hash is not lowercase sha256",
            )
        actual.append((path_text, sha256))
    expected = [
        (entry.manifest_path_text, entry.manifest_sha256)
        for entry in entries
    ]
    if actual != expected:
        _reject(
            "request_freeze_inventory_mismatch",
            "request source_manifests must exactly equal freeze inventory",
        )


def _rejected_result(
    rejection: CaptureFreezeRejection,
) -> CaptureFreezeVerification:
    return CaptureFreezeVerification(
        verified=False,
        status="rejected",
        freeze_id=None,
        freeze_path=None,
        target=None,
        source_manifest_paths=(),
        source_manifest_hashes=MappingProxyType({}),
        raw_hashes=MappingProxyType({}),
        record_ids=(),
        required_role_ids=MappingProxyType({}),
        reason_codes=(rejection.code,),
        rejection_detail=rejection.detail,
    )


def verify_production_capture_freeze(
    request_manifest_path: str | Path,
) -> CaptureFreezeVerification:
    """Verify one finalized captured-public freeze using local reads only.

    The request must bind ``capture_root/CAPTURE_FREEZE.json`` by full-file
    sha256 and must repeat the freeze's manifest inventory exactly.  A verified
    result proves byte/record bindings and the production-role contract; it
    does not itself claim replay profitability or enable order submission.
    """

    try:
        request_path, base, request_bytes = _safe_request_path(
            Path(request_manifest_path)
        )
        request = _json_document(
            request_bytes,
            code="request_invalid",
            label="promotion request",
        )
        _scan_secrets(request, label="request")
        if request.get("schema_version") != PROMOTION_REQUEST_SCHEMA:
            _reject(
                "request_schema_mismatch",
                f"request schema must equal {PROMOTION_REQUEST_SCHEMA}",
            )
        if request.get("evidence_mode") != "captured_public":
            _reject(
                "evidence_mode_invalid",
                "production freeze verification requires captured_public",
            )
        freeze_binding = _mapping(
            request.get("production_freeze"),
            code="capture_freeze_binding_invalid",
            label="request.production_freeze",
        )
        _exact_keys(
            freeze_binding,
            {"path", "sha256", "freeze_id"},
            code="capture_freeze_binding_invalid",
            label="request.production_freeze",
        )
        freeze_path_text, freeze_path = _safe_existing_path(
            base,
            freeze_binding.get("path"),
            code="capture_freeze_binding_invalid",
            label="request.production_freeze.path",
        )
        if freeze_path.name != "CAPTURE_FREEZE.json":
            _reject(
                "capture_freeze_path_invalid",
                "capture freeze basename must be CAPTURE_FREEZE.json",
            )
        expected_freeze_sha = _string(
            freeze_binding.get("sha256"),
            code="capture_freeze_binding_invalid",
            label="request.production_freeze.sha256",
        )
        if _SHA256_RE.fullmatch(expected_freeze_sha) is None:
            _reject(
                "capture_freeze_binding_invalid",
                "capture freeze hash must be lowercase sha256",
            )
        expected_freeze_id = _string(
            freeze_binding.get("freeze_id"),
            code="capture_freeze_binding_invalid",
            label="request.production_freeze.freeze_id",
        )
        if _SHA256_RE.fullmatch(expected_freeze_id) is None:
            _reject(
                "capture_freeze_binding_invalid",
                "capture freeze ID must be lowercase sha256",
            )
        freeze_bytes = _stable_read(
            freeze_path,
            label="CAPTURE_FREEZE.json",
        )
        if _sha256_bytes(freeze_bytes) != expected_freeze_sha:
            _reject(
                "capture_freeze_hash_mismatch",
                "CAPTURE_FREEZE.json differs from the request binding",
            )
        freeze = _json_document(
            freeze_bytes,
            code="capture_freeze_invalid",
            label="CAPTURE_FREEZE.json",
        )
        if freeze_bytes != canonical_json_bytes(freeze) + b"\n":
            _reject(
                "capture_freeze_not_canonical",
                "CAPTURE_FREEZE.json must be canonical JSON plus newline",
            )
        _scan_secrets(freeze, label="capture_freeze")
        _exact_keys(
            freeze,
            _FREEZE_KEYS,
            code="capture_freeze_invalid",
            label="CAPTURE_FREEZE.json",
        )
        if freeze.get("schema_version") != CAPTURE_FREEZE_SCHEMA:
            _reject(
                "capture_freeze_schema_mismatch",
                f"freeze schema must equal {CAPTURE_FREEZE_SCHEMA}",
            )
        if freeze.get("status") != "finalized":
            _reject(
                "capture_freeze_not_finalized",
                "capture freeze status must be finalized",
            )
        freeze_id = _string(
            freeze.get("freeze_id"),
            code="capture_freeze_id_invalid",
            label="capture freeze freeze_id",
        )
        if _SHA256_RE.fullmatch(freeze_id) is None:
            _reject(
                "capture_freeze_id_invalid",
                "freeze_id must be lowercase sha256",
            )
        if freeze_id != expected_freeze_id:
            _reject(
                "capture_freeze_id_binding_mismatch",
                "request production_freeze.freeze_id differs from the freeze",
            )
        freeze_core = {
            key: value for key, value in freeze.items() if key != "freeze_id"
        }
        if _sha256_bytes(canonical_json_bytes(freeze_core)) != freeze_id:
            _reject(
                "capture_freeze_id_mismatch",
                "freeze_id is not the content hash of the freeze contract",
            )
        capture_root_text, capture_root = _safe_existing_path(
            base,
            freeze.get("capture_root"),
            code="capture_root_invalid",
            label="capture_root",
            expect_directory=True,
        )
        expected_freeze_path = f"{capture_root_text}/CAPTURE_FREEZE.json"
        if freeze_path_text != expected_freeze_path:
            _reject(
                "capture_freeze_path_invalid",
                "freeze path must be capture_root/CAPTURE_FREEZE.json",
            )
        _, raw_root = _safe_existing_path(
            base,
            f"{capture_root_text}/raw",
            code="capture_root_invalid",
            label="capture_root/raw",
            expect_directory=True,
        )
        target = _validate_target(freeze.get("target"))
        _validate_safety(freeze.get("safety"))
        roles = _validate_role_ids(freeze.get("required_role_ids"))
        entries = _validate_entries(
            freeze.get("source_manifests"),
            base=base,
            capture_root_text=capture_root_text,
        )
        _validate_request_inventory(request, entries)
        filesystem_inventory = _scan_inventory(raw_root)
        expected_manifests = frozenset(
            entry.manifest_path for entry in entries
        )
        expected_raws = frozenset(entry.raw_path for entry in entries)
        if (
            filesystem_inventory.manifests != expected_manifests
            or filesystem_inventory.raws != expected_raws
        ):
            _reject(
                "capture_inventory_mismatch",
                "filesystem finalized inventory differs from CAPTURE_FREEZE",
            )
        seen_record_ids: set[str] = set()
        batches = tuple(
            _validate_batch(
                entry,
                capture_root_text=capture_root_text,
                seen_record_ids=seen_record_ids,
            )
            for entry in entries
        )
        records_by_id: dict[str, Mapping[str, Any]] = {}
        ordered_record_ids: list[str] = []
        for batch in batches:
            for record in batch.records:
                record_id = str(record["record_id"])
                records_by_id[record_id] = record
                ordered_record_ids.append(record_id)
        _validate_production_roles(
            records_by_id,
            roles=roles,
            target=target,
        )

        # Close the verification interval: every root byte and the complete
        # directory inventory must still match after semantic validation.
        if _sha256_bytes(
            _stable_read(request_path, label="promotion request recheck")
        ) != _sha256_bytes(request_bytes):
            _reject(
                "request_changed_after_verification",
                "promotion request changed during verification",
            )
        if _sha256_bytes(
            _stable_read(freeze_path, label="capture freeze recheck")
        ) != expected_freeze_sha:
            _reject(
                "capture_freeze_changed_after_verification",
                "CAPTURE_FREEZE.json changed during verification",
            )
        for batch in batches:
            entry = batch.entry
            if (
                _stable_read(
                    entry.manifest_path,
                    label=f"manifest recheck {entry.manifest_path_text}",
                )
                != batch.manifest_bytes
                or _stable_read(
                    entry.raw_path,
                    label=f"raw recheck {entry.raw_path_text}",
                )
                != batch.raw_bytes
            ):
                _reject(
                    "capture_bytes_changed_after_verification",
                    f"capture bytes changed: {entry.manifest_path_text}",
                )
        final_inventory = _scan_inventory(raw_root)
        if final_inventory != filesystem_inventory:
            _reject(
                "capture_inventory_changed_after_verification",
                "capture inventory changed during verification",
            )
        return CaptureFreezeVerification(
            verified=True,
            status="verified",
            freeze_id=freeze_id,
            freeze_path=freeze_path,
            target=target,
            source_manifest_paths=tuple(
                entry.manifest_path_text for entry in entries
            ),
            source_manifest_hashes=MappingProxyType(
                {
                    entry.manifest_path_text: entry.manifest_sha256
                    for entry in entries
                }
            ),
            raw_hashes=MappingProxyType(
                {
                    entry.raw_path_text: entry.raw_sha256
                    for entry in entries
                }
            ),
            record_ids=tuple(ordered_record_ids),
            required_role_ids=roles,
            reason_codes=(),
            rejection_detail=None,
        )
    except CaptureFreezeRejection as rejection:
        return _rejected_result(rejection)


__all__ = [
    "CAPTURE_FREEZE_SCHEMA",
    "PROMOTION_REQUEST_SCHEMA",
    "REQUIRED_ROLE_NAMES",
    "CaptureFreezeRejection",
    "CaptureFreezeTarget",
    "CaptureFreezeVerification",
    "verify_production_capture_freeze",
]
