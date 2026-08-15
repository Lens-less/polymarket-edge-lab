from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.maintain_btc_rawcap import MaintenanceConfig, maintain_rawcap


def _set_mtime(path: Path, value: datetime) -> None:
    timestamp = value.timestamp()
    os.utime(path, (timestamp, timestamp))


def _completed_attempt(
    root: Path,
    *,
    expiry: str,
    attempt: str,
    completed_at: datetime,
) -> Path:
    capture = root / "runs" / expiry / attempt
    capture.mkdir(parents=True)
    summary = capture / "capture-summary.json"
    summary.write_text("{}\n", encoding="utf-8")
    _set_mtime(summary, completed_at)
    return capture


def test_maintenance_only_compresses_completed_inactive_captures_and_retains_30_days(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, 18, 0, tzinfo=UTC)
    completed = _completed_attempt(
        tmp_path,
        expiry="1786665600",
        attempt="complete",
        completed_at=now - timedelta(hours=2),
    )
    completed_jsonl = completed / "raw" / "events.jsonl"
    completed_jsonl.parent.mkdir()
    completed_jsonl.write_text('{"record":1}\n', encoding="utf-8")
    active = _completed_attempt(
        tmp_path,
        expiry="1786666500",
        attempt="active",
        completed_at=now - timedelta(minutes=10),
    )
    active_jsonl = active / "events.jsonl"
    active_jsonl.write_text('{"record":2}\n', encoding="utf-8")
    old = _completed_attempt(
        tmp_path,
        expiry="1783900800",
        attempt="expired",
        completed_at=now - timedelta(days=31),
    )
    (old / "events.jsonl").write_text('{"record":3}\n', encoding="utf-8")
    incomplete = tmp_path / "runs" / "1786667400" / "no-summary"
    incomplete.mkdir(parents=True)
    incomplete_jsonl = incomplete / "events.jsonl"
    incomplete_jsonl.write_text('{"record":4}\n', encoding="utf-8")

    def fake_compress(path: Path) -> Path:
        output = path.with_name(f"{path.name}.zst")
        output.write_bytes(b"compressed:" + path.read_bytes())
        path.unlink()
        return output

    result = maintain_rawcap(
        MaintenanceConfig(
            data_root=tmp_path,
            compress_after_seconds=1800,
            retention_days=30,
        ),
        now=now,
        compressor=fake_compress,
    )

    assert result["compressed_file_count"] == 1
    assert result["deleted_capture_count"] == 1
    assert completed_jsonl.with_name("events.jsonl.zst").is_file()
    assert not completed_jsonl.exists()
    assert active_jsonl.is_file()
    assert incomplete_jsonl.is_file()
    assert not old.exists()


def test_maintenance_rejects_a_filesystem_anchor_as_data_root() -> None:
    with pytest.raises(ValueError, match="broad filesystem root"):
        MaintenanceConfig(data_root=Path(Path.cwd().anchor))
