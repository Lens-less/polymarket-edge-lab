#!/usr/bin/env python3
"""Read-only health check for the BTC TWAP paper-validation service."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.edge_lab.btc_twap_relative_value_service import (  # noqa: E402
    evaluate_service_health,
    load_service_config,
)
from src.edge_lab.data_store import canonical_json_bytes  # noqa: E402


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    return value


def _process_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _free_disk_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _summary_snapshot(path_value: Any) -> Mapping[str, Any] | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    path = Path(path_value).expanduser().resolve()
    summary = _read_object(path)
    unsigned = dict(summary)
    claimed = unsigned.pop("summary_sha256", None)
    actual = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    if claimed != actual:
        raise ValueError("validation summary hash mismatch")
    if (
        summary.get("paper_only") is not True
        or summary.get("new_orders_disabled") is not True
    ):
        raise ValueError("validation summary is not paper-only")
    evidence = summary.get("evidence")
    gaps = summary.get("promotion_gaps")
    return {
        "path": str(path),
        "summary_sha256": claimed,
        "classification": summary.get("classification"),
        "qualified_net_pnl": summary.get("qualified_net_pnl"),
        "evidence": evidence if isinstance(evidence, Mapping) else None,
        "promotion_gaps": gaps if isinstance(gaps, Mapping) else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--maximum-heartbeat-age-seconds", type=int, default=90)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        config = load_service_config(args.config)
        status_path = config.data_root / "service" / "status.json"
        status = _read_object(status_path)
        health = evaluate_service_health(
            status,
            now=now,
            process_alive=_process_alive(status.get("pid")),
            free_disk_bytes=_free_disk_bytes(config.data_root),
            minimum_free_disk_bytes=config.minimum_free_disk_bytes,
            maximum_heartbeat_age_seconds=args.maximum_heartbeat_age_seconds,
        )
        try:
            summary = _summary_snapshot(status.get("latest_summary_path"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            summary = None
            health["healthy"] = False
            health["failures"].append("validation_summary_invalid")
            health["summary_error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
        health["summary"] = summary
        health["checked_at"] = now.isoformat().replace("+00:00", "Z")
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        health = {
            "schema_version": "btc-twap-relative-value-service-health.v1",
            "healthy": False,
            "failures": ["health_check_input_invalid"],
            "checked_at": now.isoformat().replace("+00:00", "Z"),
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    print(json.dumps(health, indent=2, sort_keys=True))
    return 0 if health["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
