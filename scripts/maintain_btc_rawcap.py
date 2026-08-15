#!/usr/bin/env python3
"""Compress finalized raw captures and enforce bounded local retention."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MaintenanceConfig:
    data_root: Path
    compress_after_seconds: int = 1800
    retention_days: int = 30
    status_path: Path | None = None

    def __post_init__(self) -> None:
        root = self.data_root.expanduser().resolve()
        anchor = Path(root.anchor).resolve()
        if root == anchor:
            raise ValueError("data_root cannot be a broad filesystem root")
        if (
            isinstance(self.compress_after_seconds, bool)
            or not isinstance(self.compress_after_seconds, int)
            or self.compress_after_seconds <= 0
        ):
            raise ValueError("compress_after_seconds must be positive")
        if (
            isinstance(self.retention_days, bool)
            or not isinstance(self.retention_days, int)
            or self.retention_days <= 0
        ):
            raise ValueError("retention_days must be positive")
        object.__setattr__(self, "data_root", root)
        if self.status_path is not None:
            object.__setattr__(
                self,
                "status_path",
                self.status_path.expanduser().resolve(),
            )


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _capture_attempts(runs_root: Path) -> tuple[Path, ...]:
    if not runs_root.is_dir() or runs_root.is_symlink():
        return ()
    attempts: list[Path] = []
    for expiry_root in sorted(runs_root.iterdir()):
        if (
            not expiry_root.name.isdigit()
            or not expiry_root.is_dir()
            or expiry_root.is_symlink()
        ):
            continue
        for attempt in sorted(expiry_root.iterdir()):
            if not attempt.is_dir() or attempt.is_symlink():
                continue
            resolved = attempt.resolve()
            if (
                resolved.parent != expiry_root.resolve()
                or not resolved.is_relative_to(runs_root)
            ):
                continue
            attempts.append(resolved)
    return tuple(attempts)


def _zstd_compress(path: Path) -> Path:
    executable = shutil.which("zstd")
    if executable is None:
        raise RuntimeError("zstd executable is required")
    output = path.with_name(f"{path.name}.zst")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing archive: {output}")
    subprocess.run(
        [executable, "-q", "-T1", "--rm", "--", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    if not output.is_file() or path.exists():
        raise RuntimeError(f"zstd did not finalize expected archive: {output}")
    return output


def maintain_rawcap(
    config: MaintenanceConfig,
    *,
    now: datetime,
    compressor: Callable[[Path], Path] = _zstd_compress,
) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    observed_at = now.astimezone(UTC)
    runs_root = (config.data_root / "runs").resolve()
    if not runs_root.is_relative_to(config.data_root):
        raise ValueError("runs root escaped data_root")
    compression_cutoff = observed_at - timedelta(
        seconds=config.compress_after_seconds
    )
    retention_cutoff = observed_at - timedelta(days=config.retention_days)
    compressed: list[str] = []
    deleted: list[str] = []
    skipped_incomplete = 0
    for attempt in _capture_attempts(runs_root):
        summary = attempt / "capture-summary.json"
        if not summary.is_file() or summary.is_symlink():
            skipped_incomplete += 1
            continue
        completed_at = datetime.fromtimestamp(summary.stat().st_mtime, tz=UTC)
        if completed_at < retention_cutoff:
            if (
                attempt.parent.parent.resolve() != runs_root
                or not attempt.parent.name.isdigit()
                or not attempt.is_relative_to(runs_root)
            ):
                raise ValueError("refusing to delete an unbounded capture path")
            shutil.rmtree(attempt)
            deleted.append(str(attempt))
            continue
        if completed_at > compression_cutoff:
            continue
        for source in sorted(attempt.rglob("*.jsonl")):
            if source.is_symlink() or not source.resolve().is_relative_to(attempt):
                continue
            archive = compressor(source.resolve())
            if not archive.resolve().is_relative_to(attempt):
                raise ValueError("compressor wrote outside the capture attempt")
            compressed.append(str(archive))
    document: dict[str, Any] = {
        "schema_version": "btc-rawcap-maintenance.v1",
        "checked_at": observed_at.isoformat().replace("+00:00", "Z"),
        "data_root": str(config.data_root),
        "compress_after_seconds": config.compress_after_seconds,
        "retention_days": config.retention_days,
        "compressed_file_count": len(compressed),
        "deleted_capture_count": len(deleted),
        "skipped_incomplete_capture_count": skipped_incomplete,
        "compressed_files": compressed,
        "deleted_captures": deleted,
    }
    if config.status_path is not None:
        _write_json_atomic(config.status_path, document)
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--compress-after-seconds", type=int, default=1800)
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--status-path", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = MaintenanceConfig(
        data_root=args.data_root,
        compress_after_seconds=args.compress_after_seconds,
        retention_days=args.retention_days,
        status_path=args.status_path,
    )
    if args.validate_only:
        if shutil.which("zstd") is None:
            raise SystemExit("zstd executable is required")
        result: Mapping[str, Any] = {
            "schema_version": "btc-rawcap-maintenance-validation.v1",
            "valid": True,
            "data_root": str(config.data_root),
            "retention_days": config.retention_days,
        }
    else:
        result = maintain_rawcap(config, now=datetime.now(UTC))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
