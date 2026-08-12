"""Build a self-contained production capture freeze from finalized evidence.

The builder copies byte-identical finalized manifest/raw pairs into one closed
``raw/<source>`` inventory, writes the content-addressed freeze contract and a
promotion request, then invokes the independent verifier before returning.
It never reads partial files, follows symlinks, fetches network data, or
modifies the source capture roots.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .data_store import canonical_json_bytes, canonical_record_id
from .execution_replay_freeze import (
    CAPTURE_FREEZE_SCHEMA,
    REQUIRED_ROLE_NAMES,
    verify_production_capture_freeze,
)


REQUEST_SCHEMA = "edge-lab.execution-replay-promotion-request.v1"
RAW_SCHEMA = "edge-lab-recorder.raw.v1"
MANIFEST_SCHEMA = "capture-manifest.v1"


class CaptureFreezeBuildError(ValueError):
    """The requested source inventory cannot form a verified freeze."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class CaptureFreezeBuildResult:
    request_path: Path
    freeze_path: Path
    freeze_id: str
    capture_root: Path
    source_manifest_count: int
    source_record_count: int


@dataclass(frozen=True)
class _SourcePair:
    source: str
    batch_id: str
    manifest_bytes: bytes
    raw_bytes: bytes
    record_ids: tuple[str, ...]


def _fail(code: str, detail: str) -> None:
    raise CaptureFreezeBuildError(code, detail)


def _stable_regular_bytes(path: Path, *, label: str) -> bytes:
    if path.name.endswith(".partial"):
        _fail("partial_source_forbidden", f"{label} is mutable partial evidence")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _fail("source_read_failed", f"{label} cannot be opened: {exc}")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _fail("source_not_regular", f"{label} is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            _fail("source_changed_during_read", f"{label} changed during read")
        value = b"".join(chunks)
        if len(value) != after.st_size:
            _fail("source_changed_during_read", f"{label} size changed")
        return value
    finally:
        os.close(descriptor)


def _source_pair(manifest_path: Path) -> _SourcePair:
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
        raise CaptureFreezeBuildError(
            "source_manifest_invalid",
            f"manifest is not valid JSON: {manifest_path}",
        ) from exc
    if not isinstance(manifest, Mapping):
        _fail("source_manifest_invalid", f"manifest is not an object: {manifest_path}")
    source = manifest_path.parent.name
    batch_id = manifest_path.name.removesuffix(".manifest.json")
    raw_path = manifest_path.with_name(f"{batch_id}.jsonl")
    if (
        manifest.get("manifest_version") != MANIFEST_SCHEMA
        or manifest.get("schema_version") != RAW_SCHEMA
        or manifest.get("source") != source
        or manifest.get("batch_id") != batch_id
        or PurePosixPath(str(manifest.get("raw_path", ""))).as_posix()
        != f"raw/{source}/{batch_id}.jsonl"
    ):
        _fail(
            "source_manifest_identity_invalid",
            f"manifest identity/schema drift: {manifest_path}",
        )
    raw_bytes = _stable_regular_bytes(raw_path, label=f"raw {raw_path}")
    checksum = manifest.get("checksum")
    lines = raw_bytes.splitlines()
    if (
        not isinstance(checksum, Mapping)
        or checksum.get("algorithm") != "sha256"
        or checksum.get("value") != hashlib.sha256(raw_bytes).hexdigest()
        or checksum.get("bytes") != len(raw_bytes)
        or checksum.get("lines") != len(lines)
        or manifest.get("record_count") != len(lines)
        or (raw_bytes and not raw_bytes.endswith(b"\n"))
    ):
        _fail(
            "source_raw_integrity_invalid",
            f"manifest/raw checksum mismatch: {manifest_path}",
        )
    record_ids: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            record: Any = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CaptureFreezeBuildError(
                "source_raw_invalid",
                f"invalid JSON at {raw_path}:{line_number}",
            ) from exc
        if (
            not isinstance(record, Mapping)
            or record.get("schema_version") != RAW_SCHEMA
            or record.get("source") != source
            or record.get("record_id") != canonical_record_id(record)
        ):
            _fail(
                "source_record_invalid",
                f"record schema/ID drift at {raw_path}:{line_number}",
            )
        record_ids.append(str(record["record_id"]))
    if len(record_ids) != len(set(record_ids)):
        _fail("duplicate_record_id", f"duplicate record in {raw_path}")
    return _SourcePair(
        source=source,
        batch_id=batch_id,
        manifest_bytes=manifest_bytes,
        raw_bytes=raw_bytes,
        record_ids=tuple(record_ids),
    )


def _relative_capture_root(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        _fail(
            "capture_root_invalid",
            "capture_relative_root must be a canonical confined path",
        )
    return value


def _write_new_or_identical(path: Path, value: bytes, *, label: str) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != value:
            _fail(
                "existing_output_conflict",
                f"existing {label} differs: {path}",
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_production_capture_freeze(
    *,
    request_base: str | Path,
    capture_relative_root: str,
    target: Mapping[str, Any],
    source_manifest_paths: Iterable[str | Path],
    required_role_ids: Mapping[str, str],
    request_filename: str,
) -> CaptureFreezeBuildResult:
    """Copy, seal, and independently verify one production capture freeze."""

    base = Path(request_base).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink() or not base.is_dir():
        _fail("request_base_invalid", "request_base must be a regular directory")
    capture_text = _relative_capture_root(capture_relative_root)
    if (
        PurePosixPath(request_filename).name != request_filename
        or not request_filename.endswith(".json")
        or request_filename.endswith(".partial")
    ):
        _fail("request_filename_invalid", "request filename must be one JSON basename")
    if set(required_role_ids) != set(REQUIRED_ROLE_NAMES):
        _fail("required_role_ids_invalid", "required role set is not exact")
    pairs = tuple(
        _source_pair(Path(path).expanduser().resolve())
        for path in source_manifest_paths
    )
    if not pairs:
        _fail("source_manifest_missing", "at least one source manifest is required")
    pair_keys = [(pair.source, pair.batch_id) for pair in pairs]
    if len(pair_keys) != len(set(pair_keys)):
        _fail(
            "source_batch_collision",
            "source batches collide after copying into the freeze",
        )
    all_record_ids = [
        record_id for pair in pairs for record_id in pair.record_ids
    ]
    if len(all_record_ids) != len(set(all_record_ids)):
        _fail("duplicate_record_id", "record ID occurs in multiple source batches")
    if not set(required_role_ids.values()).issubset(set(all_record_ids)):
        _fail(
            "required_role_record_missing",
            "one or more required roles are absent from source manifests",
        )

    final_capture_root = base.joinpath(*PurePosixPath(capture_text).parts)
    temporary_root = Path(
        tempfile.mkdtemp(prefix=".capture-freeze-", dir=base)
    )
    temporary_capture = temporary_root.joinpath(
        *PurePosixPath(capture_text).parts
    )
    entries: list[dict[str, Any]] = []
    try:
        for pair in sorted(pairs, key=lambda item: (item.source, item.batch_id)):
            destination = (
                temporary_capture / "raw" / pair.source
            )
            destination.mkdir(parents=True, exist_ok=True)
            manifest_path = destination / f"{pair.batch_id}.manifest.json"
            raw_path = destination / f"{pair.batch_id}.jsonl"
            manifest_path.write_bytes(pair.manifest_bytes)
            raw_path.write_bytes(pair.raw_bytes)
            entries.append(
                {
                    "path": (
                        f"{capture_text}/raw/{pair.source}/"
                        f"{pair.batch_id}.manifest.json"
                    ),
                    "manifest_sha256": hashlib.sha256(
                        pair.manifest_bytes
                    ).hexdigest(),
                    "raw_path": (
                        f"{capture_text}/raw/{pair.source}/"
                        f"{pair.batch_id}.jsonl"
                    ),
                    "raw_sha256": hashlib.sha256(
                        pair.raw_bytes
                    ).hexdigest(),
                    "raw_bytes": len(pair.raw_bytes),
                    "raw_lines": len(pair.record_ids),
                    "record_ids": list(pair.record_ids),
                }
            )
        freeze_core = {
            "schema_version": CAPTURE_FREEZE_SCHEMA,
            "status": "finalized",
            "capture_root": capture_text,
            "target": dict(target),
            "source_manifests": entries,
            "required_role_ids": dict(required_role_ids),
            "safety": {
                "orders_submitted": 0,
                "authenticated_endpoints_used": 0,
            },
        }
        freeze_id = hashlib.sha256(
            canonical_json_bytes(freeze_core)
        ).hexdigest()
        freeze_bytes = (
            canonical_json_bytes(
                {**freeze_core, "freeze_id": freeze_id}
            )
            + b"\n"
        )
        (temporary_capture / "CAPTURE_FREEZE.json").write_bytes(
            freeze_bytes
        )
        if final_capture_root.exists():
            expected_files = {
                path.relative_to(temporary_capture)
                for path in temporary_capture.rglob("*")
                if path.is_file()
            }
            actual_files = {
                path.relative_to(final_capture_root)
                for path in final_capture_root.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            if expected_files != actual_files or any(
                (final_capture_root / relative).read_bytes()
                != (temporary_capture / relative).read_bytes()
                for relative in expected_files
            ):
                _fail(
                    "existing_output_conflict",
                    f"capture root already differs: {final_capture_root}",
                )
        else:
            final_capture_root.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_capture, final_capture_root)
        freeze_path = final_capture_root / "CAPTURE_FREEZE.json"
        request = {
            "schema_version": REQUEST_SCHEMA,
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
                "path": f"{capture_text}/CAPTURE_FREEZE.json",
                "sha256": hashlib.sha256(freeze_bytes).hexdigest(),
                "freeze_id": freeze_id,
            },
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
            "actual_fill": False,
            "authenticated_fill": False,
        }
        request_path = base / request_filename
        _write_new_or_identical(
            request_path,
            canonical_json_bytes(request) + b"\n",
            label="promotion request",
        )
        verification = verify_production_capture_freeze(request_path)
        if not verification.verified:
            _fail(
                verification.reason_codes[0],
                verification.rejection_detail
                or "independent freeze verification failed",
            )
        return CaptureFreezeBuildResult(
            request_path=request_path,
            freeze_path=freeze_path,
            freeze_id=freeze_id,
            capture_root=final_capture_root,
            source_manifest_count=len(entries),
            source_record_count=len(all_record_ids),
        )
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)


__all__ = [
    "CaptureFreezeBuildError",
    "CaptureFreezeBuildResult",
    "build_production_capture_freeze",
]
