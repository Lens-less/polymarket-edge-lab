"""Deterministic registry over explicitly pinned short-crypto JSONL batches."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .data_store import DataContractError, canonical_json_bytes
from .short_crypto_catalog import (
    CatalogRejection,
    ShortCryptoTarget,
    parse_short_crypto_announcement,
)


_SCHEMA_VERSION = "edge-lab.short-crypto-registry.v1"


class RegistryInputError(ValueError):
    """An explicitly pinned registry input isn't a finalized JSONL file."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


class RegistryConflictError(ValueError):
    """Two valid announcements assign incompatible identities to one slug."""

    def __init__(
        self,
        *,
        slug: str,
        first_provenance: dict[str, object],
        conflicting_provenance: dict[str, object],
    ) -> None:
        self.code = "slug_identity_conflict"
        self.slug = slug
        self.first_provenance = dict(first_provenance)
        self.conflicting_provenance = dict(conflicting_provenance)
        super().__init__(
            f"{self.code}: valid announcements disagree for slug {slug}"
        )


@dataclass(frozen=True)
class _FrozenInput:
    path: Path
    raw_bytes: bytes
    lines: tuple[bytes, ...]


def _validated_paths(
    raw_paths: list[str | Path] | tuple[str | Path, ...],
) -> tuple[Path, ...]:
    if not isinstance(raw_paths, (list, tuple)):
        raise RegistryInputError(
            "explicit_paths_required",
            "raw_paths must be an already-materialized list or tuple",
        )
    try:
        unresolved_paths = tuple(Path(path) for path in raw_paths)
    except TypeError as exc:
        raise RegistryInputError(
            "invalid_input_path",
            "every registry input must be a filesystem path",
        ) from exc
    for path in unresolved_paths:
        if path.suffix != ".jsonl":
            raise RegistryInputError(
                "unfinalized_input",
                f"registry input must end in .jsonl: {path}",
            )
    paths = tuple(sorted((path.resolve() for path in unresolved_paths), key=str))
    if len(set(paths)) != len(paths):
        raise RegistryInputError(
            "duplicate_input",
            "registry input paths must be unique",
        )
    for path in paths:
        if path.suffix != ".jsonl":
            raise RegistryInputError(
                "unfinalized_input",
                f"resolved registry input must end in .jsonl: {path}",
            )
        if not path.is_file():
            raise RegistryInputError(
                "missing_input",
                f"registry input does not exist as a file: {path}",
            )
    return paths


def _freeze_input(path: Path) -> _FrozenInput:
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            raw_bytes = handle.read()
            after = os.fstat(handle.fileno())
        current = path.stat()
    except OSError as exc:
        raise RegistryInputError(
            "unreadable_input",
            f"cannot freeze registry input: {path}",
        ) from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    current_identity = (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    )
    if (
        before_identity != after_identity
        or after_identity != current_identity
        or len(raw_bytes) != after.st_size
    ):
        raise RegistryInputError(
            "input_changed_during_freeze",
            f"registry input changed while being frozen: {path}",
        )
    return _FrozenInput(
        path=path,
        raw_bytes=raw_bytes,
        lines=tuple(raw_bytes.splitlines()),
    )


def _append_rejection(
    *,
    counts: Counter[str],
    rejections: list[dict[str, object]],
    code: str,
    detail: str,
    record: Mapping[str, object] | None,
    path: Path,
    line: int,
) -> None:
    record_id = None if record is None else record.get("record_id")
    counts[code] += 1
    rejections.append(
        {
            "code": code,
            "detail": detail,
            "record_id": record_id if isinstance(record_id, str) else None,
            "path": str(path),
            "line": line,
        }
    )


def _target_payload(target: ShortCryptoTarget) -> dict[str, object]:
    return {
        "slug": target.slug,
        "market_id": target.market_id,
        "condition_id": target.condition_id,
        "up_token_id": target.up_token_id,
        "down_token_id": target.down_token_id,
        "source_topic": target.source_topic,
        "source_symbol": target.source_symbol,
        "horizon": target.horizon,
        "opens_at_ms": target.opens_at_ms,
        "closes_at_ms": target.closes_at_ms,
        "announced_at": target.announced_at,
        "announcement_record_id": target.announcement_record_id,
        "rule_hash": target.rule_hash,
    }


def build_short_crypto_registry(
    raw_paths: list[str | Path] | tuple[str | Path, ...],
) -> dict[str, Any]:
    """Build a JSON-serializable snapshot from exact finalized JSONL paths."""

    paths = _validated_paths(raw_paths)
    inputs: list[dict[str, object]] = []
    targets_by_slug: dict[str, dict[str, object]] = {}
    input_line_count = 0
    parsed_object_count = 0
    target_announcement_count = 0
    unrelated_record_count = 0
    rejection_counts: Counter[str] = Counter()
    rejections: list[dict[str, object]] = []

    for path in paths:
        frozen = _freeze_input(path)
        path = frozen.path
        inputs.append(
            {
                "path": str(path),
                "sha256": hashlib.sha256(frozen.raw_bytes).hexdigest(),
                "byte_count": len(frozen.raw_bytes),
                "line_count": len(frozen.lines),
            }
        )
        input_line_count += len(frozen.lines)
        for line_number, raw_line in enumerate(frozen.lines, start=1):
            try:
                text = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                _append_rejection(
                    counts=rejection_counts,
                    rejections=rejections,
                    code="invalid_utf8",
                    detail="line is not valid UTF-8",
                    record=None,
                    path=path,
                    line=line_number,
                )
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                _append_rejection(
                    counts=rejection_counts,
                    rejections=rejections,
                    code="invalid_json",
                    detail="line is not valid JSON",
                    record=None,
                    path=path,
                    line=line_number,
                )
                continue
            if not isinstance(record, Mapping):
                _append_rejection(
                    counts=rejection_counts,
                    rejections=rejections,
                    code="non_object_record",
                    detail="JSONL record must be an object",
                    record=None,
                    path=path,
                    line=line_number,
                )
                continue
            parsed_object_count += 1
            try:
                target = parse_short_crypto_announcement(record)
            except CatalogRejection as exc:
                _append_rejection(
                    counts=rejection_counts,
                    rejections=rejections,
                    code=exc.code,
                    detail=str(exc),
                    record=record,
                    path=path,
                    line=line_number,
                )
                continue
            except DataContractError:
                _append_rejection(
                    counts=rejection_counts,
                    rejections=rejections,
                    code="catalog_contract_error",
                    detail=(
                        "target announcement violates the raw data contract"
                    ),
                    record=record,
                    path=path,
                    line=line_number,
                )
                continue
            if target is None:
                unrelated_record_count += 1
                continue
            target_announcement_count += 1
            target_payload = _target_payload(target)
            provenance = {
                "record_id": target.announcement_record_id,
                "path": str(path),
                "line": line_number,
            }
            existing = targets_by_slug.get(target.slug)
            if existing is None:
                target_payload["provenance"] = [provenance]
                targets_by_slug[target.slug] = target_payload
            else:
                existing_provenance = existing["provenance"]
                assert isinstance(existing_provenance, list)
                existing_identity = {
                    key: value
                    for key, value in existing.items()
                    if key
                    not in {
                        "announced_at",
                        "announcement_record_id",
                        "provenance",
                    }
                }
                target_identity = {
                    key: value
                    for key, value in target_payload.items()
                    if key
                    not in {"announced_at", "announcement_record_id"}
                }
                if existing_identity != target_identity:
                    first_provenance = existing_provenance[0]
                    assert isinstance(first_provenance, dict)
                    raise RegistryConflictError(
                        slug=target.slug,
                        first_provenance=first_provenance,
                        conflicting_provenance=provenance,
                    )
                existing_provenance.append(provenance)
                existing_observation_key = (
                    str(existing["announced_at"]),
                    str(existing["announcement_record_id"]),
                )
                target_observation_key = (
                    target.announced_at,
                    target.announcement_record_id,
                )
                if target_observation_key < existing_observation_key:
                    existing["announced_at"] = target.announced_at
                    existing["announcement_record_id"] = (
                        target.announcement_record_id
                    )

    targets = [targets_by_slug[slug] for slug in sorted(targets_by_slug)]
    snapshot: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "inputs": inputs,
        "targets": targets,
        "rejections": rejections,
        "completeness": {
            "status": "incomplete" if rejections else "complete",
            "input_file_count": len(paths),
            "input_line_count": input_line_count,
            "parsed_object_count": parsed_object_count,
            "target_announcement_count": target_announcement_count,
            "unique_target_count": len(targets),
            "duplicate_target_announcement_count": (
                target_announcement_count - len(targets)
            ),
            "unrelated_record_count": unrelated_record_count,
            "rejected_record_count": len(rejections),
            "rejection_counts": dict(sorted(rejection_counts.items())),
        },
    }
    snapshot["snapshot_sha256"] = hashlib.sha256(
        canonical_json_bytes(snapshot)
    ).hexdigest()
    return snapshot


def _validated_registry_snapshot(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    required = {
        "schema_version",
        "inputs",
        "targets",
        "rejections",
        "completeness",
        "snapshot_sha256",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != required
        or value.get("schema_version") != _SCHEMA_VERSION
        or not isinstance(value.get("inputs"), list)
        or not isinstance(value.get("targets"), list)
        or not isinstance(value.get("rejections"), list)
        or not isinstance(value.get("completeness"), Mapping)
        or not isinstance(value.get("snapshot_sha256"), str)
    ):
        raise RegistryInputError(
            "registry_snapshot_invalid",
            "incremental registry input has an incompatible schema",
        )
    body = {
        key: deepcopy(item)
        for key, item in value.items()
        if key != "snapshot_sha256"
    }
    expected_hash = hashlib.sha256(
        canonical_json_bytes(body)
    ).hexdigest()
    if value["snapshot_sha256"] != expected_hash:
        raise RegistryInputError(
            "registry_snapshot_hash_mismatch",
            "incremental registry input is not content-addressed correctly",
        )
    return deepcopy(dict(value))


def validate_short_crypto_registry_snapshot(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Return an isolated copy of one valid content-addressed snapshot."""

    return _validated_registry_snapshot(value)


def _registry_target_identity(
    target: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: value
        for key, value in target.items()
        if key
        not in {
            "announced_at",
            "announcement_record_id",
            "provenance",
        }
    }


def merge_short_crypto_registry_snapshots(
    base: Mapping[str, Any],
    delta: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge disjoint registry snapshots exactly like one full rebuild.

    This preserves the registry's path ordering, earliest-announcement rule,
    conflict behavior, counters, and content hash without reparsing previously
    frozen JSONL bytes.
    """

    snapshots = (
        _validated_registry_snapshot(base),
        _validated_registry_snapshot(delta),
    )
    inputs_by_path: dict[str, dict[str, Any]] = {}
    targets_by_slug: dict[str, dict[str, Any]] = {}
    rejections: list[dict[str, Any]] = []
    completeness_totals: Counter[str] = Counter()
    rejection_counts: Counter[str] = Counter()

    additive_fields = {
        "input_line_count",
        "parsed_object_count",
        "target_announcement_count",
        "unrelated_record_count",
        "rejected_record_count",
    }
    for snapshot in snapshots:
        for descriptor in snapshot["inputs"]:
            if not isinstance(descriptor, dict) or not isinstance(
                descriptor.get("path"), str
            ):
                raise RegistryInputError(
                    "registry_snapshot_invalid",
                    "registry input descriptor is invalid",
                )
            path = descriptor["path"]
            if path in inputs_by_path:
                raise RegistryInputError(
                    "overlapping_snapshot_input",
                    f"registry snapshots both contain {path}",
                )
            inputs_by_path[path] = deepcopy(descriptor)

        completeness = snapshot["completeness"]
        if not all(
            isinstance(completeness.get(field), int)
            and not isinstance(completeness.get(field), bool)
            and completeness[field] >= 0
            for field in additive_fields
        ):
            raise RegistryInputError(
                "registry_snapshot_invalid",
                "registry completeness counters are invalid",
            )
        for field in additive_fields:
            completeness_totals[field] += completeness[field]
        counts = completeness.get("rejection_counts")
        if not isinstance(counts, Mapping) or any(
            not isinstance(code, str)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            for code, count in counts.items()
        ):
            raise RegistryInputError(
                "registry_snapshot_invalid",
                "registry rejection counters are invalid",
            )
        rejection_counts.update(counts)

        for rejection in snapshot["rejections"]:
            if (
                not isinstance(rejection, dict)
                or not isinstance(rejection.get("path"), str)
                or isinstance(rejection.get("line"), bool)
                or not isinstance(rejection.get("line"), int)
            ):
                raise RegistryInputError(
                    "registry_snapshot_invalid",
                    "registry rejection entry is invalid",
                )
            rejections.append(deepcopy(rejection))

        for candidate in snapshot["targets"]:
            if (
                not isinstance(candidate, dict)
                or not isinstance(candidate.get("slug"), str)
                or not isinstance(candidate.get("announced_at"), str)
                or not isinstance(
                    candidate.get("announcement_record_id"), str
                )
                or not isinstance(candidate.get("provenance"), list)
                or not candidate["provenance"]
            ):
                raise RegistryInputError(
                    "registry_snapshot_invalid",
                    "registry target entry is invalid",
                )
            slug = candidate["slug"]
            existing = targets_by_slug.get(slug)
            if existing is None:
                targets_by_slug[slug] = deepcopy(candidate)
                continue
            if _registry_target_identity(existing) != (
                _registry_target_identity(candidate)
            ):
                raise RegistryConflictError(
                    slug=slug,
                    first_provenance=deepcopy(existing["provenance"][0]),
                    conflicting_provenance=deepcopy(
                        candidate["provenance"][0]
                    ),
                )
            existing["provenance"].extend(
                deepcopy(candidate["provenance"])
            )
            existing_observation = (
                existing["announced_at"],
                existing["announcement_record_id"],
            )
            candidate_observation = (
                candidate["announced_at"],
                candidate["announcement_record_id"],
            )
            if candidate_observation < existing_observation:
                existing["announced_at"] = candidate["announced_at"]
                existing["announcement_record_id"] = candidate[
                    "announcement_record_id"
                ]

    inputs = [
        inputs_by_path[path] for path in sorted(inputs_by_path)
    ]
    for target in targets_by_slug.values():
        target["provenance"].sort(
            key=lambda item: (
                str(item.get("path")),
                int(item.get("line", 0)),
                str(item.get("record_id")),
            )
        )
    targets = [
        targets_by_slug[slug] for slug in sorted(targets_by_slug)
    ]
    rejections.sort(
        key=lambda item: (
            str(item.get("path")),
            int(item.get("line", 0)),
            str(item.get("code")),
            str(item.get("record_id")),
        )
    )
    snapshot = {
        "schema_version": _SCHEMA_VERSION,
        "inputs": inputs,
        "targets": targets,
        "rejections": rejections,
        "completeness": {
            "status": "incomplete" if rejections else "complete",
            "input_file_count": len(inputs),
            "input_line_count": completeness_totals[
                "input_line_count"
            ],
            "parsed_object_count": completeness_totals[
                "parsed_object_count"
            ],
            "target_announcement_count": completeness_totals[
                "target_announcement_count"
            ],
            "unique_target_count": len(targets),
            "duplicate_target_announcement_count": (
                completeness_totals["target_announcement_count"]
                - len(targets)
            ),
            "unrelated_record_count": completeness_totals[
                "unrelated_record_count"
            ],
            "rejected_record_count": completeness_totals[
                "rejected_record_count"
            ],
            "rejection_counts": dict(sorted(rejection_counts.items())),
        },
    }
    snapshot["snapshot_sha256"] = hashlib.sha256(
        canonical_json_bytes(snapshot)
    ).hexdigest()
    return snapshot
