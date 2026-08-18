"""Prospective-only v0.7 shadow runtime built from finalized v0.6 captures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from scripts.build_btc_twap_relative_value_pilot_report import (
    _pair_from_capture_targets,
)
from scripts.build_btc_twap_relative_value_v07_counterfactual import (
    CASE_DATA_QUALITY_ERROR_CODES,
    MANIFEST_SCHEMA_VERSION,
    CaseDataQualityError,
    build_counterfactual_report,
)
from src.edge_lab.btc_twap_execution_probe import (
    MAX_ALL_IN_COST,
    MAX_BUY_NOTIONAL,
    MAX_ISOLATED_BALANCE,
)
from src.edge_lab.btc_twap_relative_value_readiness import (
    StrategyLiveInputs,
    evaluate_execution_probe_readiness,
    evaluate_strategy_live_readiness_inputs,
)
from src.edge_lab.btc_twap_relative_value_v07 import canonical_event_cluster_id
from src.edge_lab.capture_cli import (
    CONFIG_SCHEMA_VERSION as SOURCE_CAPTURE_CONFIG_SCHEMA,
)
from src.edge_lab.capture_cli import (
    load_capture_config,
)
from src.edge_lab.compatibility import LiveExecutionBlocked, assert_new_orders_disabled
from src.edge_lab.data_store import CaptureStore, canonical_json_bytes
from src.edge_lab.network_safety import (
    high_confidence_secret_findings,
    safe_error_details,
)

CONFIG_SCHEMA_VERSION = "btc-twap-relative-value-v07-shadow-service.v2"
PROJECTED_CAPTURE_CONFIG_SCHEMA = "btc-5m-15m-relative-value-capture.v1"
SOURCE_SUMMARY_SCHEMA_VERSION = "btc-twap-compact-forward-capture-summary.v1"
MODE = "prospective_actual_market_counterfactual_shadow"
STATUS_SCHEMA_VERSION = "btc-twap-relative-value-v07-shadow-status.v1"
SOURCE_MARKER_SCHEMA_VERSION = "btc-twap-relative-value-v07-shadow-source-marker.v1"
REJECTED_SOURCE_CACHE_SCHEMA_VERSION = (
    "btc-twap-relative-value-v07-shadow-rejected-source-cache.v2"
)
CASE_DATA_QUALITY_CACHE_SCHEMA_VERSION = (
    "btc-twap-relative-value-v07-shadow-case-data-quality-cache.v1"
)
ALLOWED_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "track_id",
        "source_runs_root",
        "source_status_path",
        "data_root",
        "research_root",
        "status_path",
        "performance_path",
        "history_dir",
        "preregistration_path",
        "preregistration_sha256",
        "deployment_spec_path",
        "prospective_cutoff_iso",
        "prospective_cutoff_ms",
        "decision_tau_seconds",
        "train_case_count",
        "validation_case_count",
        "maximum_cases",
        "snapshot_mode",
        "maximum_status_age_seconds",
        "paper_only",
        "public_only",
        "new_orders_disabled",
        "live",
        "prelabel_lock_journal_root",
        "qualified_pnl_possible",
        "true_edge_possible",
        "minimum_free_bytes",
    }
)
REQUIRED_CONFIG_KEYS = ALLOWED_CONFIG_KEYS
EXPECTED_TAUS = (60, 120, 180, 240)
EXPECTED_MAXIMUM_CASES = 102
EXPECTED_TRACK_ID = "btc-paper-v07-shadow-20260816"
EXPECTED_CUTOFF_ISO = "2026-08-16T15:00:00Z"
EXPECTED_CUTOFF_MS = 1_786_892_400_000
EXPECTED_MINIMUM_FREE_BYTES = 12 * 1024**3
EXPECTED_MAXIMUM_STATUS_AGE_SECONDS = 2_700
EXPECTED_SETTLEMENT_REGIME = "chainlink_twap_60s_5m_and_60s_15m.v1"
EXPECTED_SOURCE_EVIDENCE_TRACK_ID = "btc-paper-v06-20260814"
SOURCE_STATUS_SCHEMA_VERSION = "btc-twap-relative-value-service-status.v1"
HEALTHY_SOURCE_PHASES = frozenset(
    {
        "starting",
        "measuring_clock",
        "discovering",
        "capturing",
        "building_report",
        "cycle_complete",
    }
)
SOURCE_INTEGRITY_KEYS = frozenset(
    {
        "orphan_partials",
        "raw_without_manifest",
        "manifest_without_raw",
        "checksum_mismatches",
        "invalid_manifests",
    }
)
EXPECTED_RESOLUTION_SOURCE = (
    "https://data.chain.link/streams/btc-usd-twap-60s-streams"
)
EXPECTED_PREREGISTRATION_SHA256 = (
    "de79f3e4d43b513a7c71e3196877ee110f04f7d31cec4b3cd060f6184f541bfe"
)
FORBIDDEN_PROXY_KEYS = frozenset(
    {
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "proxies",
        "proxy",
        "proxy_url",
    }
)


@dataclass(frozen=True)
class V07ShadowConfig:
    mode: str
    track_id: str
    source_runs_root: Path
    source_status_path: Path
    data_root: Path
    research_root: Path
    status_path: Path
    performance_path: Path
    history_dir: Path
    preregistration_path: Path
    preregistration_sha256: str
    deployment_spec_path: Path
    prospective_cutoff_iso: str
    prospective_cutoff_ms: int
    decision_tau_seconds: tuple[int, ...]
    train_case_count: int
    validation_case_count: int
    maximum_cases: int
    snapshot_mode: str
    maximum_status_age_seconds: int
    minimum_free_bytes: int
    paper_only: bool = True
    public_only: bool = True
    new_orders_disabled: bool = True
    live: bool = False
    prelabel_lock_journal_root: None = None
    qualified_pnl_possible: bool = False
    true_edge_possible: bool = False

    def __post_init__(self) -> None:
        if self.mode != MODE:
            raise ValueError(f"mode must equal {MODE}")
        if self.track_id != EXPECTED_TRACK_ID:
            raise ValueError(f"track_id must equal {EXPECTED_TRACK_ID}")
        for name in (
            "prospective_cutoff_ms",
            "train_case_count",
            "validation_case_count",
            "maximum_cases",
            "maximum_status_age_seconds",
            "minimum_free_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.decision_tau_seconds != EXPECTED_TAUS:
            raise ValueError("decision_tau_seconds must equal the frozen v0.7 taus")
        if self.maximum_cases != EXPECTED_MAXIMUM_CASES:
            raise ValueError("maximum_cases must equal 102")
        if self.train_case_count != 1 or self.validation_case_count != 1:
            raise ValueError(
                "train_case_count and validation_case_count must both equal 1"
            )
        if self.snapshot_mode != "reflink_required":
            raise ValueError("snapshot_mode must equal reflink_required")
        if self.maximum_status_age_seconds != EXPECTED_MAXIMUM_STATUS_AGE_SECONDS:
            raise ValueError("maximum_status_age_seconds must equal 2700")
        if self.minimum_free_bytes != EXPECTED_MINIMUM_FREE_BYTES:
            raise ValueError("minimum_free_bytes must equal 12 GiB")
        if self.preregistration_sha256 != EXPECTED_PREREGISTRATION_SHA256:
            raise ValueError(
                "preregistration_sha256 must match the frozen preregistration"
            )
        if not self.prospective_cutoff_iso.endswith("Z"):
            raise ValueError("prospective_cutoff_iso must be a UTC timestamp")
        try:
            cutoff_from_iso = int(
                datetime.fromisoformat(
                    self.prospective_cutoff_iso.replace("Z", "+00:00")
                ).timestamp()
                * 1_000
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("prospective_cutoff_iso must be a UTC timestamp") from exc
        if cutoff_from_iso != self.prospective_cutoff_ms:
            raise ValueError("prospective cutoff ISO and milliseconds disagree")
        if (
            self.prospective_cutoff_iso != EXPECTED_CUTOFF_ISO
            or self.prospective_cutoff_ms != EXPECTED_CUTOFF_MS
        ):
            raise ValueError(
                "prospective cutoff must equal the frozen deployment cutoff"
            )
        for name in ("paper_only", "public_only", "new_orders_disabled"):
            if getattr(self, name) is not True:
                raise ValueError(f"{name} must be true")
        if self.live is not False:
            raise ValueError("live must be false")
        if self.prelabel_lock_journal_root is not None:
            raise ValueError("prelabel_lock_journal_root must be null")
        if self.qualified_pnl_possible is not False:
            raise ValueError("qualified_pnl_possible must be false")
        if self.true_edge_possible is not False:
            raise ValueError("true_edge_possible must be false")


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return value


def _int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _bool(value: Any, *, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be a boolean")
    return value


def _decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{label} must be exact decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    return format(_decimal(value, label="decimal field").normalize(), "f")


def _resolve_path(base: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty path")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else (base / path)).resolve()


def _forbidden_proxy_paths(value: Any, *, path: str = "$") -> tuple[str, ...]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            nested_path = f"{path}.{key}"
            if normalized in FORBIDDEN_PROXY_KEYS and nested not in (None, "", [], {}):
                findings.append(nested_path)
            findings.extend(_forbidden_proxy_paths(nested, path=nested_path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            findings.extend(_forbidden_proxy_paths(nested, path=f"{path}[{index}]"))
    return tuple(findings)


def _reject_unsafe_document(document: Mapping[str, Any], *, label: str) -> None:
    findings = high_confidence_secret_findings(document)
    if findings:
        paths = ", ".join(sorted({finding["path"] for finding in findings}))
        raise ValueError(f"{label} contains forbidden credential material at {paths}")
    proxy_paths = _forbidden_proxy_paths(document)
    if proxy_paths:
        raise ValueError(
            f"{label} contains forbidden proxy configuration at "
            f"{', '.join(sorted(set(proxy_paths)))}"
        )


def _verify_live_order_guard() -> None:
    try:
        assert_new_orders_disabled()
    except LiveExecutionBlocked:
        return
    raise RuntimeError("new-order safety guard is not active")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_reflink_available() -> None:
    if shutil.which("cp") is None:
        raise RuntimeError(
            "cp with reflink support is required for snapshot_mode=reflink_required"
        )


def _copy_regular_file(source: Path, destination: Path) -> None:
    subprocess.run(
        [
            "cp",
            "--reflink=always",
            "--preserve=mode,timestamps",
            "--",
            str(source),
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=False,
    )


def _tree_identity(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"source capture contains symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"source capture contains non-regular file: {path}")
        stat_result = path.stat()
        if stat_result.st_nlink != 1:
            raise ValueError(f"source capture contains hard-linked file: {path}")
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": stat_result.st_size,
                "sha256": _sha256(path),
            }
        )
    return {
        "schema_version": "v07-shadow-tree-identity.v1",
        "file_count": len(files),
        "tree_sha256": hashlib.sha256(canonical_json_bytes(files)).hexdigest(),
    }


def _source_inventory_token(root: Path) -> dict[str, Any]:
    """Bind a finalized tree to kernel-maintained identity and change times.

    On the Linux deployment, the unprivileged source account cannot restore
    ``ctime_ns`` after changing content or metadata.  UID/GID, mode, device,
    inode, link count, type, size, and both timestamps keep cache reuse inside
    that trust boundary; any drift forces the content-level audit again.
    """
    entries: list[dict[str, Any]] = []
    for path in (root, *sorted(root.rglob("*"))):
        stat_result = path.lstat()
        if path.is_symlink():
            raise ValueError(f"source capture contains symlink: {path}")
        elif path.is_dir():
            kind = "dir"
        elif path.is_file():
            kind = "file"
            if stat_result.st_nlink != 1:
                raise ValueError(f"source capture contains hard-linked file: {path}")
        else:
            raise ValueError(f"source capture contains non-regular file: {path}")
        entries.append(
            {
                "path": "." if path == root else path.relative_to(root).as_posix(),
                "kind": kind,
                "mtime_ns": stat_result.st_mtime_ns,
                "ctime_ns": stat_result.st_ctime_ns,
                "size": stat_result.st_size,
                "device": stat_result.st_dev,
                "inode": stat_result.st_ino,
                "nlink": stat_result.st_nlink,
                "mode": stat_result.st_mode,
                "uid": stat_result.st_uid,
                "gid": stat_result.st_gid,
            }
        )
    return {
        "schema_version": "v07-shadow-tree-inventory.v2",
        "entry_count": len(entries),
        "inventory_sha256": hashlib.sha256(canonical_json_bytes(entries)).hexdigest(),
    }


def _validated_tree_identity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    file_count = value.get("file_count")
    tree_sha256 = value.get("tree_sha256")
    if (
        value.get("schema_version") != "v07-shadow-tree-identity.v1"
        or isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or file_count < 0
        or not isinstance(tree_sha256, str)
        or not tree_sha256
    ):
        return None
    return {
        "schema_version": "v07-shadow-tree-identity.v1",
        "file_count": file_count,
        "tree_sha256": tree_sha256,
    }


def _validated_inventory_token(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    entry_count = value.get("entry_count")
    inventory_sha256 = value.get("inventory_sha256")
    if (
        value.get("schema_version") != "v07-shadow-tree-inventory.v2"
        or isinstance(entry_count, bool)
        or not isinstance(entry_count, int)
        or entry_count < 0
        or not isinstance(inventory_sha256, str)
        or not inventory_sha256
    ):
        return None
    return {
        "schema_version": "v07-shadow-tree-inventory.v2",
        "entry_count": entry_count,
        "inventory_sha256": inventory_sha256,
    }


def _rejected_source_cache_path(config: V07ShadowConfig) -> Path:
    return config.data_root / "monitor" / "rejected-source-cache.json"


def _load_rejected_source_cache(config: V07ShadowConfig) -> dict[str, dict[str, Any]]:
    cache_path = _rejected_source_cache_path(config)
    if not cache_path.is_file():
        return {}
    try:
        document = _load_json(cache_path, label="rejected source cache")
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if document.get("schema_version") != REJECTED_SOURCE_CACHE_SCHEMA_VERSION:
        return {}
    entries = document.get("entries")
    if not isinstance(entries, Mapping):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for root, entry in entries.items():
        if isinstance(root, str) and isinstance(entry, Mapping):
            normalized[root] = dict(entry)
    return normalized


def _write_rejected_source_cache(
    config: V07ShadowConfig, entries: Mapping[str, Mapping[str, Any]]
) -> None:
    _atomic_write_json(
        _rejected_source_cache_path(config),
        {
            "schema_version": REJECTED_SOURCE_CACHE_SCHEMA_VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "entries": {key: dict(entries[key]) for key in sorted(entries)},
        },
    )


def _case_data_quality_cache_path(config: V07ShadowConfig) -> Path:
    return config.data_root / "monitor" / "case-data-quality-cache.json"


def _load_case_data_quality_cache(
    config: V07ShadowConfig,
) -> dict[str, dict[str, Any]]:
    cache_path = _case_data_quality_cache_path(config)
    if not cache_path.is_file():
        return {}
    try:
        document = _load_json(cache_path, label="case data-quality cache")
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if document.get("schema_version") != CASE_DATA_QUALITY_CACHE_SCHEMA_VERSION:
        return {}
    entries = document.get("entries")
    if not isinstance(entries, Mapping):
        return {}
    return {
        key: dict(entry)
        for key, entry in entries.items()
        if isinstance(key, str) and isinstance(entry, Mapping)
    }


def _write_case_data_quality_cache(
    config: V07ShadowConfig, entries: Mapping[str, Mapping[str, Any]]
) -> None:
    _atomic_write_json(
        _case_data_quality_cache_path(config),
        {
            "schema_version": CASE_DATA_QUALITY_CACHE_SCHEMA_VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "entries": {key: dict(entries[key]) for key in sorted(entries)},
        },
    )


def _case_data_quality_commitment(case: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(case)).hexdigest()


def _cached_case_data_quality_rejection(
    *,
    case: Mapping[str, Any],
    cache_entries: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, str] | None:
    commitment = _case_data_quality_commitment(case)
    entry = cache_entries.get(commitment)
    case_alias = case.get("event_cluster_id")
    if not isinstance(entry, Mapping) or not isinstance(case_alias, str):
        return None
    error_code = entry.get("error_code")
    if (
        entry.get("case_commitment_sha256") != commitment
        or entry.get("case_alias") != case_alias
        or error_code not in CASE_DATA_QUALITY_ERROR_CODES
    ):
        return None
    return {"case_alias": case_alias, "error_code": str(error_code)}


def _rejected_source_tree_identity(
    *,
    root: Path,
    capture_config_path: Path,
    summary_path: Path,
    summary: Mapping[str, Any],
    cache_entries: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    cache_key = str(root)
    config_sha256 = _sha256(capture_config_path)
    summary_sha256 = _sha256(summary_path)
    inventory_token = _source_inventory_token(root)
    cached_entry = cache_entries.get(cache_key)
    if isinstance(cached_entry, Mapping):
        cached_tree = _validated_tree_identity(cached_entry.get("source_capture_tree"))
        cached_inventory = _validated_inventory_token(
            cached_entry.get("source_capture_inventory_token")
        )
        if (
            cached_entry.get("source_capture_root") == cache_key
            and cached_entry.get("source_capture_config_sha256") == config_sha256
            and cached_entry.get("source_capture_summary_sha256") == summary_sha256
            and cached_entry.get("source_capture_integrity_clean") is True
            and cached_inventory == inventory_token
            and cached_tree is not None
        ):
            return cached_tree, False
    if not _clean_integrity(summary, root=root):
        raise ValueError("post-cutoff source capture integrity is not clean")
    tree_identity = _tree_identity(root)
    inventory_token_after = _source_inventory_token(root)
    if inventory_token_after != inventory_token:
        raise ValueError("post-cutoff source capture changed during validation")
    cache_entries[cache_key] = {
        "source_capture_root": cache_key,
        "source_capture_config_sha256": config_sha256,
        "source_capture_summary_sha256": summary_sha256,
        "source_capture_integrity_clean": True,
        "source_capture_tree": tree_identity,
        "source_capture_inventory_token": inventory_token,
    }
    return tree_identity, True


def _validated_source_capture_error(value: Any) -> Mapping[str, str] | None:
    if value is None:
        return None
    required_keys = {"error_type", "error_code"}
    if not isinstance(value, Mapping) or set(value) != required_keys:
        raise ValueError("post-cutoff source capture_error contract is invalid")
    error_type = value.get("error_type")
    if not isinstance(error_type, str) or not error_type.strip():
        raise ValueError("post-cutoff source capture_error contract is invalid")
    if value.get("error_code") != "capture_failed":
        raise ValueError("post-cutoff source capture_error contract is invalid")
    return {"error_type": error_type, "error_code": "capture_failed"}


def _validated_recorder_leg_failures(value: Any) -> tuple[Mapping[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 2:
        raise ValueError("post-cutoff recorder_leg_failures contract is invalid")
    failures: list[Mapping[str, str]] = []
    for failure in value:
        if not isinstance(failure, Mapping) or set(failure) != {
            "error_type",
            "error_code",
        }:
            raise ValueError("post-cutoff recorder_leg_failures contract is invalid")
        error_type = failure.get("error_type")
        error_code = failure.get("error_code")
        if (
            not isinstance(error_type, str)
            or not error_type.strip()
            or not isinstance(error_code, str)
            or not error_code.strip()
        ):
            raise ValueError("post-cutoff recorder_leg_failures contract is invalid")
        failures.append({"error_type": error_type, "error_code": error_code})
    return tuple(failures)


def _atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(document) + b"\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_history_json(
    directory: Path, prefix: str, document: Mapping[str, Any]
) -> Path:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = directory / f"{prefix}-{timestamp}-{time.time_ns()}.json"
    _atomic_write_json(path, document)
    return path


def load_shadow_config(path: Path) -> V07ShadowConfig:
    raw = _load_json(path, label="shadow config")
    unexpected = sorted(set(raw) - ALLOWED_CONFIG_KEYS)
    if unexpected:
        raise ValueError(
            "shadow config contains unsupported keys: " + ", ".join(unexpected)
        )
    missing = sorted(REQUIRED_CONFIG_KEYS - set(raw))
    if missing:
        raise ValueError(
            "shadow config is missing required keys: " + ", ".join(missing)
        )
    _reject_unsafe_document(raw, label="shadow config")
    if raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError("unsupported shadow config schema_version")
    if raw.get("mode") != MODE:
        raise ValueError(f"shadow config mode must equal {MODE}")
    base = path.resolve().parent
    raw_taus = raw["decision_tau_seconds"]
    if (
        not isinstance(raw_taus, list)
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in raw_taus
        )
    ):
        raise TypeError("decision_tau_seconds must be an integer array")
    return V07ShadowConfig(
        mode=str(raw["mode"]),
        track_id=raw["track_id"] if isinstance(raw["track_id"], str) else "",
        source_runs_root=_resolve_path(
            base, raw["source_runs_root"], label="source_runs_root"
        ),
        source_status_path=_resolve_path(
            base, raw["source_status_path"], label="source_status_path"
        ),
        data_root=_resolve_path(base, raw["data_root"], label="data_root"),
        research_root=_resolve_path(base, raw["research_root"], label="research_root"),
        status_path=_resolve_path(base, raw["status_path"], label="status_path"),
        performance_path=_resolve_path(
            base, raw["performance_path"], label="performance_path"
        ),
        history_dir=_resolve_path(base, raw["history_dir"], label="history_dir"),
        preregistration_path=_resolve_path(
            base,
            raw["preregistration_path"],
            label="preregistration_path",
        ),
        preregistration_sha256=(
            raw["preregistration_sha256"]
            if isinstance(raw["preregistration_sha256"], str)
            else ""
        ),
        deployment_spec_path=_resolve_path(
            base, raw["deployment_spec_path"], label="deployment_spec_path"
        ),
        prospective_cutoff_iso=(
            raw["prospective_cutoff_iso"]
            if isinstance(raw["prospective_cutoff_iso"], str)
            else ""
        ),
        prospective_cutoff_ms=_int(
            raw["prospective_cutoff_ms"],
            label="prospective_cutoff_ms",
        ),
        decision_tau_seconds=tuple(raw_taus),
        train_case_count=_int(raw["train_case_count"], label="train_case_count"),
        validation_case_count=_int(
            raw["validation_case_count"],
            label="validation_case_count",
        ),
        maximum_cases=_int(raw["maximum_cases"], label="maximum_cases"),
        snapshot_mode=str(raw["snapshot_mode"]),
        maximum_status_age_seconds=_int(
            raw["maximum_status_age_seconds"], label="maximum_status_age_seconds"
        ),
        minimum_free_bytes=_int(raw["minimum_free_bytes"], label="minimum_free_bytes"),
        paper_only=_bool(raw["paper_only"], label="paper_only"),
        public_only=_bool(raw["public_only"], label="public_only"),
        new_orders_disabled=_bool(
            raw["new_orders_disabled"], label="new_orders_disabled"
        ),
        live=_bool(raw["live"], label="live"),
        prelabel_lock_journal_root=raw["prelabel_lock_journal_root"],
        qualified_pnl_possible=_bool(
            raw["qualified_pnl_possible"], label="qualified_pnl_possible"
        ),
        true_edge_possible=_bool(
            raw["true_edge_possible"], label="true_edge_possible"
        ),
    )


def _same_filesystem(left: Path, right: Path) -> bool:
    return left.stat().st_dev == right.stat().st_dev


def _validate_path_layout(config: V07ShadowConfig) -> Path:
    runtime_root = config.data_root.parent.parent
    if config.data_root.parent.name != "data":
        raise ValueError("data_root must be under the runtime data directory")
    if config.research_root.parent.name != "research":
        raise ValueError("research_root must be under the runtime research directory")
    if not config.research_root.is_relative_to(runtime_root):
        raise ValueError("research_root must remain under the v0.7 runtime root")
    if not config.status_path.is_relative_to(config.data_root):
        raise ValueError("status_path must remain under data_root")
    for name, path in (
        ("performance_path", config.performance_path),
        ("history_dir", config.history_dir),
    ):
        if not path.is_relative_to(runtime_root):
            raise ValueError(f"{name} must remain under the v0.7 runtime root")
    source_parent = config.source_runs_root.parent
    if not config.source_status_path.is_relative_to(source_parent):
        raise ValueError("source_status_path must remain under the v0.6 source root")
    if runtime_root.is_relative_to(source_parent) or source_parent.is_relative_to(
        runtime_root
    ):
        raise ValueError("v0.6 source and v0.7 runtime roots must be independent")
    return runtime_root


def _validate_source_status(config: V07ShadowConfig) -> str:
    if not config.source_status_path.is_file():
        raise ValueError("source_status_path must exist")
    status = _load_json(config.source_status_path, label="source service status")
    _reject_unsafe_document(status, label="source service status")
    if status.get("schema_version") != SOURCE_STATUS_SCHEMA_VERSION:
        raise ValueError("unsupported source service status schema")
    unsigned = dict(status)
    claimed_hash = unsigned.pop("status_sha256", None)
    expected_hash = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    if claimed_hash != expected_hash:
        raise ValueError("source service status hash is invalid")
    phase = status.get("phase")
    if phase not in HEALTHY_SOURCE_PHASES:
        raise ValueError("source service phase is unhealthy")
    required = {
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
    }
    for key, expected in required.items():
        if status.get(key) != expected:
            raise ValueError(f"source service safety guard failed: {key}")
    heartbeat_at = status.get("heartbeat_at")
    if not isinstance(heartbeat_at, str) or not heartbeat_at.endswith("Z"):
        raise ValueError("source service heartbeat is missing or invalid")
    try:
        heartbeat = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("source service heartbeat is missing or invalid") from exc
    age_seconds = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    if age_seconds < -60 or age_seconds > config.maximum_status_age_seconds:
        raise ValueError("source service heartbeat is stale")
    return heartbeat_at


def _validate_runtime_inputs(
    config: V07ShadowConfig,
    *,
    validate_only: bool,
    check_source_status: bool = True,
) -> dict[str, Any]:
    _verify_live_order_guard()
    _ensure_reflink_available()
    runtime_root = _validate_path_layout(config)
    if config.source_runs_root.is_symlink() or not config.source_runs_root.is_dir():
        raise ValueError("source_runs_root must exist")
    source_heartbeat_at = (
        _validate_source_status(config) if check_source_status else None
    )
    if not config.preregistration_path.is_file():
        raise ValueError("preregistration_path must exist")
    if not config.deployment_spec_path.is_file():
        raise ValueError("deployment_spec_path must exist")
    if _sha256(config.preregistration_path) != config.preregistration_sha256:
        raise ValueError("preregistration hash mismatch")
    data_parent = config.data_root.parent
    research_parent = config.research_root.parent
    if not data_parent.is_dir():
        raise ValueError("data_root parent must exist")
    if not research_parent.is_dir():
        raise ValueError("research_root parent must exist")
    if not _same_filesystem(config.source_runs_root, data_parent):
        raise ValueError("source_runs_root and data_root must share one filesystem")
    if not _same_filesystem(data_parent, research_parent):
        raise ValueError(
            "data_root and research_root parents must share one filesystem"
        )
    if not validate_only:
        free_bytes = shutil.disk_usage(data_parent).free
        if free_bytes < config.minimum_free_bytes:
            raise RuntimeError("minimum_free_bytes guard failed")
    return {
        "schema_version": "btc-twap-relative-value-v07-shadow-validation.v1",
        "valid": True,
        "mode": MODE,
        "track_id": config.track_id,
        "prospective_cutoff_ms": config.prospective_cutoff_ms,
        "decision_tau_seconds": list(config.decision_tau_seconds),
        "train_case_count": config.train_case_count,
        "validation_case_count": config.validation_case_count,
        "maximum_cases": config.maximum_cases,
        "source_runs_root": str(config.source_runs_root),
        "source_status_path": str(config.source_status_path),
        "source_heartbeat_at": source_heartbeat_at,
        "runtime_root": str(runtime_root),
        "data_root": str(config.data_root),
        "research_root": str(config.research_root),
        "preregistration_path": str(config.preregistration_path),
        "snapshot_mode": config.snapshot_mode,
    }


@dataclass(frozen=True)
class _SelectedCapture:
    expiry_ms: int
    alias: str
    root: Path
    capture_config_path: Path
    summary_path: Path
    capture_started_at_ms: int
    canonical_event_cluster_id: str
    projected_capture_config: Mapping[str, Any]
    source_marker: Mapping[str, Any]


@dataclass(frozen=True)
class _SourceScanResult:
    selected: tuple[_SelectedCapture, ...]
    summary_file_count: int
    post_cutoff_attempt_count: int
    finalized_clean_count: int
    rejected_count: int
    rejected_capture_error_count: int
    rejected_recorder_leg_failure_count: int
    latest_rejected_attempts: tuple[Mapping[str, Any], ...]


def _clean_integrity(summary: Mapping[str, Any], *, root: Path) -> bool:
    integrity = summary.get("integrity")
    if not isinstance(integrity, Mapping) or set(integrity) != SOURCE_INTEGRITY_KEYS:
        return False
    if any(
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 0
        for value in integrity.values()
    ):
        return False
    required_directories = tuple(
        root / name for name in ("raw", "derived", "checkpoints")
    )
    if any(not path.is_dir() or path.is_symlink() for path in required_directories):
        return False
    actual = CaptureStore(root).audit_integrity()
    return actual == dict(integrity) and not any(actual.values())


def _pair_from_source_targets(
    targets: Any,
) -> tuple[Any, dict[str, Mapping[str, Any]]] | None:
    if not isinstance(targets, list) or len(targets) != 2:
        return None
    if any(not isinstance(target, Mapping) for target in targets):
        return None
    by_horizon = {str(target.get("horizon")): target for target in targets}
    if set(by_horizon) != {"5m", "15m"}:
        return None
    pair = _pair_from_capture_targets(by_horizon)
    if pair is None:
        return None
    for target in by_horizon.values():
        if target.get("twap_window_seconds") != 60:
            return None
        if target.get("source_topic") != "crypto_prices_twap_sixty":
            return None
        if target.get("settlement_regime") != EXPECTED_SETTLEMENT_REGIME:
            return None
        if target.get("resolution_source") != EXPECTED_RESOLUTION_SOURCE:
            return None
        if target.get("taker_delay_ms") != 250:
            return None
    if pair.market_5.closes_at_ms != pair.market_15.closes_at_ms:
        return None
    return pair, by_horizon


def _source_expiry_matches(root: Path, expiry_ms: int) -> bool:
    """Accept either the capture's millisecond or second expiry directory form."""

    directory_name = root.parent.name
    try:
        directory_expiry = int(directory_name)
    except (TypeError, ValueError):
        return False
    return directory_expiry in {expiry_ms, expiry_ms // 1_000}


def _source_directory_expiry_ms(root: Path) -> int | None:
    try:
        value = int(root.parent.name)
    except (TypeError, ValueError):
        return None
    return value if value >= 1_000_000_000_000 else value * 1_000


def _projected_capture_config(source: Mapping[str, Any]) -> dict[str, Any]:
    projected = dict(source)
    projected["schema_version"] = PROJECTED_CAPTURE_CONFIG_SCHEMA
    projected["paper_only"] = True
    projected["public_only"] = True
    projected["new_orders_disabled"] = True
    projected["generated_fixture"] = False
    projected["safety_flags_derived_from_frozen_v06_public_service"] = True
    return projected


def _source_marker(
    *,
    root: Path,
    capture_config_path: Path,
    summary_path: Path,
    capture_started_at_ms: int,
    expiry_ms: int,
    canonical_cluster_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": SOURCE_MARKER_SCHEMA_VERSION,
        "source_capture_root": str(root),
        "source_capture_config_path": str(capture_config_path),
        "source_capture_summary_path": str(summary_path),
        "source_capture_started_at_ms": capture_started_at_ms,
        "source_capture_config_sha256": _sha256(capture_config_path),
        "source_capture_summary_sha256": _sha256(summary_path),
        "source_capture_tree": _tree_identity(root),
        "expiry_ms": expiry_ms,
        "canonical_event_cluster_id": canonical_cluster_id,
    }


def _select_source_captures(
    config: V07ShadowConfig,
) -> _SourceScanResult:
    candidates: dict[int, _SelectedCapture] = {}
    summary_file_count = 0
    post_cutoff_attempt_count = 0
    finalized_count = 0
    rejected_capture_error_count = 0
    rejected_recorder_leg_failure_count = 0
    rejected_attempts: list[Mapping[str, Any]] = []
    resolved_source_root = config.source_runs_root.resolve()
    rejected_cache_entries = _load_rejected_source_cache(config)
    rejected_cache_dirty = False
    for summary_path in sorted(
        config.source_runs_root.glob("*/*/capture-summary.json")
    ):
        summary_file_count += 1
        root = summary_path.parent
        capture_config_path = root / "capture-config.json"
        inferred_expiry_ms = _source_directory_expiry_ms(root)
        path_is_post_cutoff = (
            inferred_expiry_ms is None
            or inferred_expiry_ms >= config.prospective_cutoff_ms
        )
        if (
            root.is_symlink()
            or summary_path.is_symlink()
            or capture_config_path.is_symlink()
        ):
            if path_is_post_cutoff:
                raise ValueError("post-cutoff source capture contains a symlink")
            continue
        if not capture_config_path.is_file():
            if path_is_post_cutoff:
                raise ValueError(
                    "post-cutoff source capture is missing capture-config.json"
                )
            continue
        resolved_root = root.resolve()
        resolved_config_path = capture_config_path.resolve()
        resolved_summary_path = summary_path.resolve()
        if (
            not resolved_root.is_relative_to(resolved_source_root)
            or resolved_config_path.parent != resolved_root
            or resolved_summary_path.parent != resolved_root
        ):
            if path_is_post_cutoff:
                raise ValueError("post-cutoff source capture escapes source_runs_root")
            continue
        try:
            source_config = _load_json(
                resolved_config_path, label="source capture config"
            )
            capture_started_at_ms = _int(
                source_config.get("capture_started_at_ms"),
                label="capture_started_at_ms",
            )
        except (
            FileNotFoundError,
            OSError,
            UnicodeError,
            TypeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            if path_is_post_cutoff:
                raise ValueError(
                    "post-cutoff source capture config is invalid"
                ) from exc
            continue
        if capture_started_at_ms < config.prospective_cutoff_ms:
            continue
        try:
            summary = _load_json(resolved_summary_path, label="source capture summary")
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise ValueError("post-cutoff source capture summary is invalid") from exc
        _reject_unsafe_document(source_config, label="source capture config")
        _reject_unsafe_document(summary, label="source capture summary")
        if source_config.get("schema_version") != SOURCE_CAPTURE_CONFIG_SCHEMA:
            raise ValueError("post-cutoff source capture config schema drift")
        try:
            load_capture_config(
                resolved_config_path,
                data_root_override=resolved_root,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                "post-cutoff source capture config contract is invalid"
            ) from exc
        source_data_root = source_config.get("data_root")
        if (
            not isinstance(source_data_root, str)
            or Path(source_data_root).expanduser().resolve() != resolved_root
        ):
            raise ValueError("post-cutoff source capture data_root mismatch")
        if (
            source_config.get("evidence_track_id")
            != EXPECTED_SOURCE_EVIDENCE_TRACK_ID
        ):
            raise ValueError("post-cutoff source evidence track drift")
        if source_config.get("settlement_regime_id") != EXPECTED_SETTLEMENT_REGIME:
            raise ValueError("post-cutoff source settlement regime drift")
        if summary.get("schema_version") != SOURCE_SUMMARY_SCHEMA_VERSION:
            raise ValueError("post-cutoff source capture summary schema drift")
        if summary.get("data_root") != str(resolved_root):
            raise ValueError("post-cutoff source capture summary data_root mismatch")
        for key, expected in {
            "paper_only": True,
            "public_only": True,
            "new_orders_disabled": True,
            "authenticated_endpoints_used": 0,
            "orders_submitted": 0,
        }.items():
            if summary.get(key) != expected:
                raise ValueError(
                    f"post-cutoff source capture safety guard failed: {key}"
                )
        generated_at = summary.get("generated_at")
        if not isinstance(generated_at, str) or not generated_at.endswith("Z"):
            raise ValueError("post-cutoff source capture generated_at is invalid")
        try:
            generated_at_ms = int(
                datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
                .timestamp()
                * 1_000
            )
        except ValueError as exc:
            raise ValueError(
                "post-cutoff source capture generated_at is invalid"
            ) from exc
        if generated_at_ms < capture_started_at_ms:
            raise ValueError("post-cutoff source capture predates its start")
        parsed_targets = _pair_from_source_targets(source_config.get("targets"))
        if parsed_targets is None:
            raise ValueError("post-cutoff source target contract is invalid")
        pair, _ = parsed_targets
        if not _source_expiry_matches(root, pair.expires_at_ms):
            raise ValueError("post-cutoff source expiry directory mismatch")
        earliest_decision_at_ms = (
            pair.expires_at_ms - max(config.decision_tau_seconds) * 1_000
        )
        if capture_started_at_ms > earliest_decision_at_ms:
            raise ValueError("post-cutoff source capture misses earliest decision")
        post_cutoff_attempt_count += 1
        capture_error = _validated_source_capture_error(summary.get("capture_error"))
        recorder_leg_failures = _validated_recorder_leg_failures(
            summary.get("recorder_leg_failures")
        )
        if capture_error is not None or recorder_leg_failures:
            # A failed attempt is still untrusted source input. Reuse the prior
            # full tree identity only when the config/summary digests and a cheap
            # inventory token still prove the tree is unchanged.
            _, cache_updated = _rejected_source_tree_identity(
                root=resolved_root,
                capture_config_path=resolved_config_path,
                summary_path=resolved_summary_path,
                summary=summary,
                cache_entries=rejected_cache_entries,
            )
            rejected_cache_dirty = rejected_cache_dirty or cache_updated
            if capture_error is not None:
                rejected_capture_error_count += 1
            else:
                rejected_recorder_leg_failure_count += 1
            rejected_attempts.append(
                {
                    "capture_started_at_ms": capture_started_at_ms,
                    "expiry_ms": pair.expires_at_ms,
                    "rejection_code": (
                        "source_capture_error"
                        if capture_error is not None
                        else "source_recorder_leg_failure"
                    ),
                    "recorder_leg_failures": list(recorder_leg_failures),
                    "source_capture_root": str(resolved_root),
                    "source_capture_summary_sha256": _sha256(resolved_summary_path),
                }
            )
            continue
        if not _clean_integrity(summary, root=resolved_root):
            raise ValueError("post-cutoff source capture integrity is not clean")
        finalized_count += 1
        expiry_ms = pair.expires_at_ms
        canonical_cluster_id = canonical_event_cluster_id(pair)
        marker = _source_marker(
            root=resolved_root,
            capture_config_path=resolved_config_path,
            summary_path=resolved_summary_path,
            capture_started_at_ms=capture_started_at_ms,
            expiry_ms=expiry_ms,
            canonical_cluster_id=canonical_cluster_id,
        )
        selected = _SelectedCapture(
            expiry_ms=expiry_ms,
            alias=f"expiry-{expiry_ms}",
            root=resolved_root,
            capture_config_path=resolved_config_path,
            summary_path=resolved_summary_path,
            capture_started_at_ms=capture_started_at_ms,
            canonical_event_cluster_id=canonical_cluster_id,
            projected_capture_config=_projected_capture_config(source_config),
            source_marker=marker,
        )
        current = candidates.get(expiry_ms)
        if current is None or (
            selected.capture_started_at_ms,
            selected.root.as_posix(),
        ) > (
            current.capture_started_at_ms,
            current.root.as_posix(),
        ):
            candidates[expiry_ms] = selected
    if rejected_cache_dirty:
        _write_rejected_source_cache(config, rejected_cache_entries)
    return _SourceScanResult(
        selected=tuple(
            candidates[key]
            for key in sorted(candidates)[: config.maximum_cases + 1]
        ),
        summary_file_count=summary_file_count,
        post_cutoff_attempt_count=post_cutoff_attempt_count,
        finalized_clean_count=finalized_count,
        rejected_count=(
            rejected_capture_error_count + rejected_recorder_leg_failure_count
        ),
        rejected_capture_error_count=rejected_capture_error_count,
        rejected_recorder_leg_failure_count=(
            rejected_recorder_leg_failure_count
        ),
        latest_rejected_attempts=tuple(rejected_attempts[-5:]),
    )


def _project_capture(
    config: V07ShadowConfig,
    selection: _SelectedCapture,
) -> Path:
    destination = (
        config.data_root / "captures" / str(selection.expiry_ms) / selection.root.name
    )
    marker_path = destination / "projection-source.json"
    expected_source_tree = selection.source_marker.get("source_capture_tree")
    if _tree_identity(selection.root) != expected_source_tree:
        raise ValueError("source capture changed after selection")
    if destination.exists():
        existing = _load_json(marker_path, label="projection source marker")
        if existing != dict(selection.source_marker):
            raise ValueError(
                "projected capture root already exists with different source "
                f"marker: {destination}"
            )
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = (
        destination.parent / f".{destination.name}.tmp.{os.getpid()}.{time.time_ns()}"
    )
    try:
        temporary.mkdir(parents=False, exist_ok=False)
        for path in sorted(selection.root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"source capture contains symlink: {path}")
            relative = path.relative_to(selection.root)
            target = temporary / relative
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not path.is_file():
                raise ValueError(f"source capture contains non-regular file: {path}")
            if relative.as_posix() == "capture-config.json":
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_regular_file(path, target)
        _atomic_write_json(
            temporary / "capture-config.json", selection.projected_capture_config
        )
        if _tree_identity(selection.root) != expected_source_tree:
            raise ValueError("source capture changed during projection")
        _atomic_write_json(
            temporary / "projection-source.json", selection.source_marker
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return destination


def _build_manifest(
    config: V07ShadowConfig,
    projected: Sequence[tuple[_SelectedCapture, Path]],
    *,
    initial_history_roots: Sequence[Path] = (),
    excluded_case_aliases: Collection[str] = (),
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    prior_root = initial_history_roots[-1] if initial_history_roots else None
    for selection, projected_root in projected:
        if selection.alias in excluded_case_aliases:
            prior_root = projected_root
            continue
        history_roots = [] if prior_root is None else [str(prior_root)]
        index = len(cases)
        split = (
            "train"
            if index < config.train_case_count
            else "validation"
            if index < config.train_case_count + config.validation_case_count
            else "test"
        )
        cases.append(
            {
                "event_cluster_id": selection.alias,
                "canonical_event_cluster_id": selection.canonical_event_cluster_id,
                "split": split,
                "capture_root": str(projected_root),
                "predictor_root": str(projected_root),
                "capture_config_path": str(projected_root / "capture-config.json"),
                "history_roots": history_roots,
                "decision_tau_seconds": list(config.decision_tau_seconds),
            }
        )
        prior_root = projected_root
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "preregistration_path": str(config.preregistration_path),
        "cases": cases,
    }


def _report_summary(
    config: V07ShadowConfig,
    *,
    source_scan: _SourceScanResult,
    selected: Sequence[_SelectedCapture],
    projected: Sequence[tuple[_SelectedCapture, Path]],
    history_seed_count: int,
    data_quality_rejections: Sequence[Mapping[str, str]],
    report: Mapping[str, Any] | None,
    report_status: str,
    manifest_path: Path,
    report_path: Path | None,
) -> dict[str, Any]:
    latest_expiry_ms = selected[-1].expiry_ms if selected else None
    evaluation = report.get("evaluation") if isinstance(report, Mapping) else None
    if not isinstance(evaluation, Mapping):
        evaluation = {}
    primary = report.get("primary") if isinstance(report, Mapping) else None
    if not isinstance(primary, Mapping):
        primary = {}
    evaluation_breakdown = evaluation.get("edge_basis_breakdown")
    if not isinstance(evaluation_breakdown, Mapping):
        evaluation_breakdown = evaluation.get("edge_basis")
    if not isinstance(evaluation_breakdown, Mapping):
        evaluation_breakdown = {}
    primary_breakdown = primary.get("edge_basis_breakdown")
    if not isinstance(primary_breakdown, Mapping):
        primary_breakdown = {}

    def basis_metric(
        basis: str,
        evaluation_key: str,
        fallback_key: str,
        *,
        primary_key: str | None = None,
    ) -> Any:
        value = evaluation_breakdown.get(basis)
        if isinstance(value, Mapping) and evaluation_key in value:
            return value[evaluation_key]
        primary_value = primary_breakdown.get(basis)
        selected_primary_key = primary_key or evaluation_key
        if isinstance(primary_value, Mapping) and selected_primary_key in primary_value:
            return primary_value[selected_primary_key]
        return evaluation.get(fallback_key)

    predictive_attempts = int(
        basis_metric(
            "predictive",
            "explainable_economic_attempts",
            "predictive_explainable_economic_attempt_count",
            primary_key="action_reconciliation_count",
        )
        or 0
    )
    structural_attempts = int(
        basis_metric(
            "structural",
            "explainable_economic_attempts",
            "structural_explainable_economic_attempt_count",
            primary_key="action_reconciliation_count",
        )
        or 0
    )
    predictive_pnl = _decimal_text(
        basis_metric("predictive", "net_pnl", "predictive_net_pnl")
    )
    structural_pnl = _decimal_text(
        basis_metric("structural", "net_pnl", "structural_net_pnl")
    )
    net_pnl = _decimal_text(evaluation.get("net_pnl"))
    builder_evaluation_status = evaluation.get("status")
    if builder_evaluation_status not in {None, "counterfactual_insufficient"}:
        builder_evaluation_status = "suppressed_no_prelabel_journal"
    # No pre-label journal exists on this deployment track. Keep the user gate
    # fail-closed even if a malformed upstream report claims otherwise.
    positive_100_trade_check = False
    shadow_net_decimal = None
    for candidate in (structural_pnl, net_pnl, predictive_pnl):
        if candidate is None:
            continue
        try:
            shadow_net_decimal = _decimal(candidate, label="shadow_net_pnl")
        except (TypeError, ValueError):
            shadow_net_decimal = None
        else:
            break
    builder_verified = evaluation.get("builder_verified_evidence")
    if not isinstance(builder_verified, Mapping):
        builder_verified = {}
    try:
        structural_bootstrap = _decimal(
            basis_metric(
                "structural",
                "bootstrap_mean_lower_95",
                "structural_bootstrap_cluster_mean_lower_95",
            ),
            label="structural bootstrap",
        )
    except (TypeError, ValueError):
        structural_bootstrap = None
    strategy_live = evaluate_strategy_live_readiness_inputs(
        StrategyLiveInputs(
            builder_verified_evidence_chain=(
                builder_verified.get("verified_chain_present") is True
            ),
            auditable_prelabel_lock_evidence=(
                evaluation.get("auditable_prelabel_manifest_prediction_decision_lock")
                is True
            ),
            clean_prelabeled_common_terminal_cohort_count=0,
            structural_settled_expiry_cluster_count=int(
                basis_metric(
                    "structural",
                    "settled_expiry_clusters",
                    "structural_settled_expiry_cluster_count",
                )
                or 0
            ),
            structural_explainable_economic_attempt_count=structural_attempts,
            structural_bootstrap_cluster_mean_lower_95=structural_bootstrap,
            structural_true_edge_gate_satisfied=(
                basis_metric(
                    "structural",
                    "true_edge_gate_satisfied",
                    "structural_true_edge_gate_satisfied",
                )
                is True
            ),
            structural_qualified_net_pnl=(
                None
                if basis_metric(
                    "structural",
                    "qualified_net_pnl",
                    "structural_qualified_net_pnl",
                )
                is None
                else _decimal(
                    basis_metric(
                        "structural",
                        "qualified_net_pnl",
                        "structural_qualified_net_pnl",
                    ),
                    label="structural qualified pnl",
                )
            ),
            structural_gate_0_passed=False,
            structural_max_single_expiry_pnl_concentration=None,
            complete_real_execution_evidence=False,
            all_locked_cohorts_in_pnl_distribution=False,
            service_continuously_healthy=False,
        )
    )
    probe_readiness = evaluate_execution_probe_readiness(
        shadow_net_pnl=shadow_net_decimal,
        clean_common_terminal_cohort_count=len(projected),
        coverage_results=(),
        structural_floor=None,
    )
    heartbeat_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    train_count = min(len(projected), config.train_case_count)
    validation_count = min(
        max(0, len(projected) - config.train_case_count),
        config.validation_case_count,
    )
    test_count = max(
        0,
        len(projected) - config.train_case_count - config.validation_case_count,
    )
    payload = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "generated_at": heartbeat_at,
        "heartbeat_at": heartbeat_at,
        "mode": MODE,
        "phase": "warming_up" if report_status == "warming_up" else "ok",
        "track_id": config.track_id,
        "prospective_cutoff_iso": config.prospective_cutoff_iso,
        "prospective_cutoff_ms": config.prospective_cutoff_ms,
        "source_count": source_scan.finalized_clean_count,
        "source_summary_file_count": source_scan.summary_file_count,
        "source_post_cutoff_attempt_count": (
            source_scan.post_cutoff_attempt_count
        ),
        "source_finalized_count": source_scan.finalized_clean_count,
        "source_finalized_clean_count": source_scan.finalized_clean_count,
        "source_rejected_count": source_scan.rejected_count,
        "source_rejected_capture_error_count": (
            source_scan.rejected_capture_error_count
        ),
        "source_rejected_recorder_leg_failure_count": (
            source_scan.rejected_recorder_leg_failure_count
        ),
        "latest_rejected_attempts": [
            dict(attempt) for attempt in source_scan.latest_rejected_attempts
        ],
        "source_data_quality_rejected_count": len(data_quality_rejections),
        "latest_data_quality_rejections": [
            dict(rejection) for rejection in data_quality_rejections[-5:]
        ],
        "selection_denominator_count": source_scan.finalized_clean_count,
        "cohort_admission_count": len(projected),
        "data_quality_complete": (
            source_scan.rejected_count == 0
            and not data_quality_rejections
        ),
        "source_eligible_count": len(selected),
        "history_seed_count": history_seed_count,
        "projected_count": len(projected) + history_seed_count,
        "projected_capture_count": len(projected) + history_seed_count,
        "case_count": len(projected),
        "train_case_count": train_count,
        "validation_case_count": validation_count,
        "test_case_count": test_count,
        "latest_expiry_ms": latest_expiry_ms,
        "manifest_path": str(manifest_path),
        "report_path": None if report_path is None else str(report_path),
        "report_status": report_status,
        "evaluation_status": (
            "warming_up"
            if report_status == "warming_up"
            else "counterfactual_insufficient"
        ),
        "builder_evaluation_status": builder_evaluation_status,
        "builder_evaluation_status_diagnostic_only": True,
        "predictive_attempts": predictive_attempts,
        "predictive_explainable_economic_attempt_count": predictive_attempts,
        "structural_attempts": structural_attempts,
        "structural_explainable_economic_attempt_count": structural_attempts,
        "predictive_settled_expiry_cluster_count": int(
            basis_metric(
                "predictive",
                "settled_expiry_clusters",
                "predictive_settled_expiry_cluster_count",
            )
            or 0
        ),
        "structural_settled_expiry_cluster_count": int(
            basis_metric(
                "structural",
                "settled_expiry_clusters",
                "structural_settled_expiry_cluster_count",
            )
            or 0
        ),
        "predictive_net_pnl": predictive_pnl,
        "structural_net_pnl": structural_pnl,
        "net_pnl": net_pnl,
        "qualified_net_pnl": None,
        "true_edge": False,
        "true_edge_gate": False,
        "positive_100_trade_check": positive_100_trade_check,
        "positive_100_trade_pnl_check": positive_100_trade_check,
        "execution_probe_eligible": probe_readiness.eligible,
        "execution_probe_reason_codes": list(probe_readiness.reason_codes),
        "strategy_live_eligible": strategy_live.eligible,
        "strategy_live_reason_codes": list(strategy_live.reason_codes),
        "predictive_live_fallback_allowed": False,
        "probe_policy": {
            "isolated_balance_max_usdc": _decimal_text(MAX_ISOLATED_BALANCE),
            "cumulative_buy_notional_max_usdc": _decimal_text(MAX_BUY_NOTIONAL),
            "all_in_cost_limit_per_pair": _decimal_text(MAX_ALL_IN_COST),
            "one_maker_only": True,
            "one_fok_hedge_only": True,
            "at_most_one_emergency_unwind": True,
        },
        "orders_submitted": 0,
        "orders": 0,
        "authenticated_endpoints_used": 0,
        "live_execution": False,
        "live": False,
        "prelabel_lock_journal": False,
        "prelabel": False,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "safety_flags_derived_from_frozen_v06_public_service": True,
        "builder_authority_present": False,
    }
    return payload


@contextmanager
def _exclusive_lock(data_root: Path) -> Iterable[None]:
    lock_path = data_root / "service.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(
                    "another v0.7 shadow refresh owns this data root"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "another v0.7 shadow refresh owns this data root"
                ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n".encode())
        handle.flush()
        yield
    finally:
        handle.close()


def _write_runtime_status(config: V07ShadowConfig, document: Mapping[str, Any]) -> None:
    _atomic_write_json(config.status_path, document)
    _atomic_write_json(config.performance_path, document)
    _write_history_json(config.history_dir, "status", document)
    _write_history_json(config.history_dir, "performance", document)


def _refresh_progress_document(
    config: V07ShadowConfig,
    *,
    step: str,
) -> dict[str, Any]:
    previous: dict[str, Any] = {}
    if config.status_path.is_file():
        try:
            candidate = _load_json(config.status_path, label="shadow runtime status")
        except (OSError, TypeError, ValueError):
            candidate = {}
        if (
            candidate.get("schema_version") == STATUS_SCHEMA_VERSION
            and candidate.get("mode") == MODE
            and candidate.get("paper_only") is True
            and candidate.get("public_only") is True
            and candidate.get("new_orders_disabled") is True
            and candidate.get("live") is False
            and candidate.get("orders_submitted") == 0
            and candidate.get("authenticated_endpoints_used") == 0
        ):
            previous = candidate
    heartbeat_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    defaults: dict[str, Any] = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "mode": MODE,
        "track_id": config.track_id,
        "source_post_cutoff_attempt_count": 0,
        "source_finalized_clean_count": 0,
        "source_rejected_count": 0,
        "source_rejected_capture_error_count": 0,
        "source_rejected_recorder_leg_failure_count": 0,
        "selection_denominator_count": 0,
        "cohort_admission_count": 0,
        "case_count": 0,
        "data_quality_complete": True,
        "latest_rejected_attempts": [],
        "qualified_net_pnl": None,
        "true_edge": False,
        "true_edge_gate": False,
        "positive_100_trade_check": False,
        "positive_100_trade_pnl_check": False,
        "prelabel_lock_journal": False,
        "prelabel": False,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
        "live_execution": False,
        "live": False,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
    }
    defaults.update(previous)
    defaults.update(
        {
            "generated_at": heartbeat_at,
            "heartbeat_at": heartbeat_at,
            "phase": "refreshing",
            "refresh_step": step,
            "last_error": None,
            "qualified_net_pnl": None,
            "true_edge": False,
            "true_edge_gate": False,
            "positive_100_trade_check": False,
            "positive_100_trade_pnl_check": False,
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
            "live_execution": False,
            "live": False,
            "prelabel_lock_journal": False,
            "prelabel": False,
            "paper_only": True,
            "public_only": True,
            "new_orders_disabled": True,
        }
    )
    return defaults


class _RefreshHeartbeat:
    """Keep the status lease fresh while a synchronous refresh is running."""

    def __init__(self, config: V07ShadowConfig, *, interval_seconds: float = 60.0):
        self._config = config
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._step = "source_scan"
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="v07-shadow-status-heartbeat",
            daemon=True,
        )

    def _emit(self) -> None:
        with self._lock:
            step = self._step
        document = _refresh_progress_document(self._config, step=step)
        _atomic_write_json(self._config.status_path, document)
        _atomic_write_json(self._config.performance_path, document)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._emit()
            except BaseException as exc:  # noqa: BLE001 - surfaced on join
                self._error = exc
                self._stop.set()

    def set_step(self, step: str) -> None:
        if not isinstance(step, str) or not step:
            raise ValueError("refresh heartbeat step must be non-empty")
        with self._lock:
            self._step = step
        self._emit()
        if self._error is not None:
            raise RuntimeError("refresh status heartbeat failed") from self._error

    def __enter__(self) -> Self:
        self._emit()
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, min(5.0, self._interval_seconds)))
        if exc is None and self._error is not None:
            raise RuntimeError("refresh status heartbeat failed") from self._error


def _run_validated_shadow_refresh(
    config: V07ShadowConfig,
    *,
    heartbeat: _RefreshHeartbeat,
) -> dict[str, Any]:
    with _exclusive_lock(config.data_root):
        source_scan = _select_source_captures(config)
        heartbeat.set_step("capture_projection")
        projected_candidates = [
            (selection, _project_capture(config, selection))
            for selection in source_scan.selected
        ]
        history_seed: tuple[tuple[_SelectedCapture, Path], ...] = ()
        projected = projected_candidates
        if projected_candidates:
            first_selection, first_root = projected_candidates[0]
            first_15m_open_ms = first_selection.expiry_ms - 15 * 60 * 1_000
            if first_selection.capture_started_at_ms > first_15m_open_ms:
                history_seed = ((first_selection, first_root),)
                projected = projected_candidates[1 : config.maximum_cases + 1]
            else:
                projected = projected_candidates[: config.maximum_cases]
        heartbeat.set_step("manifest_and_report")
        manifest_root = config.research_root / "manifests"
        manifest_path = manifest_root / "manifest-latest.json"
        minimum_cases = config.train_case_count + config.validation_case_count + 1
        report: Mapping[str, Any] | None = None
        report_path: Path | None = None
        report_status = "warming_up"
        projected_pool = list(projected)
        excluded_case_aliases: set[str] = set()
        data_quality_rejections: list[Mapping[str, str]] = []
        data_quality_cache = _load_case_data_quality_cache(config)
        data_quality_cache_dirty = False
        while True:
            projected = [
                item
                for item in projected_pool
                if item[0].alias not in excluded_case_aliases
            ]
            manifest = _build_manifest(
                config,
                projected_pool,
                initial_history_roots=[root for _, root in history_seed],
                excluded_case_aliases=excluded_case_aliases,
            )
            _atomic_write_json(manifest_path, manifest)
            cached_rejection = next(
                (
                    rejection
                    for case in manifest["cases"]
                    if (
                        rejection := _cached_case_data_quality_rejection(
                            case=case,
                            cache_entries=data_quality_cache,
                        )
                    )
                    is not None
                ),
                None,
            )
            if cached_rejection is not None:
                excluded_case_aliases.add(cached_rejection["case_alias"])
                data_quality_rejections.append(cached_rejection)
                continue
            if len(projected) < minimum_cases:
                break
            try:
                report = build_counterfactual_report(manifest_path=manifest_path)
            except CaseDataQualityError as exc:
                admitted_aliases = {selection.alias for selection, _ in projected}
                if (
                    exc.case_alias not in admitted_aliases
                    or exc.case_alias in excluded_case_aliases
                ):
                    raise RuntimeError(
                        "counterfactual builder rejected an unknown case"
                    ) from exc
                excluded_case_aliases.add(exc.case_alias)
                data_quality_rejections.append(
                    {
                        "case_alias": exc.case_alias,
                        "error_code": exc.error_code,
                    }
                )
                rejected_case = next(
                    case
                    for case in manifest["cases"]
                    if case["event_cluster_id"] == exc.case_alias
                )
                commitment = _case_data_quality_commitment(rejected_case)
                data_quality_cache[commitment] = {
                    "case_alias": exc.case_alias,
                    "case_commitment_sha256": commitment,
                    "error_code": exc.error_code,
                }
                data_quality_cache_dirty = True
                continue
            report_root = config.research_root / "reports"
            report_path = report_root / "report-latest.json"
            _atomic_write_json(report_path, report)
            _write_history_json(report_root / "history", "report", report)
            report_status = "report_built"
            break
        if data_quality_cache_dirty:
            _write_case_data_quality_cache(config, data_quality_cache)
        _write_history_json(manifest_root / "history", "manifest", manifest)
        selected = [selection for selection, _ in projected]
        document = _report_summary(
            config,
            source_scan=source_scan,
            selected=selected,
            projected=projected,
            history_seed_count=len(history_seed),
            data_quality_rejections=data_quality_rejections,
            report=report,
            report_status=report_status,
            manifest_path=manifest_path,
            report_path=report_path,
        )
        return document


def run_shadow_refresh(config: V07ShadowConfig) -> dict[str, Any]:
    _validate_runtime_inputs(config, validate_only=False)
    config.data_root.mkdir(parents=True, exist_ok=True)
    config.research_root.mkdir(parents=True, exist_ok=True)
    with _RefreshHeartbeat(config) as heartbeat:
        document = _run_validated_shadow_refresh(config, heartbeat=heartbeat)
    _write_runtime_status(config, document)
    return document


def run_once(config: V07ShadowConfig) -> dict[str, Any]:
    """Run one locked, prospective-only shadow refresh."""

    return run_shadow_refresh(config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--check-source",
        action="store_true",
        help="include the mutable source heartbeat/runtime in validation",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_shadow_config(args.config.resolve())
    if args.check_source and not args.validate_only:
        raise ValueError("--check-source requires --validate-only")
    if args.validate_only:
        print(
            json.dumps(
                _validate_runtime_inputs(
                    config,
                    validate_only=True,
                    check_source_status=args.check_source,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    try:
        run_shadow_refresh(config)
    except Exception as exc:  # noqa: BLE001
        config.data_root.mkdir(parents=True, exist_ok=True)
        heartbeat_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        document = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "generated_at": heartbeat_at,
            "heartbeat_at": heartbeat_at,
            "mode": MODE,
            "phase": "failed",
            "track_id": config.track_id,
            "source_count": 0,
            "source_summary_file_count": 0,
            "source_post_cutoff_attempt_count": 0,
            "source_finalized_count": 0,
            "source_finalized_clean_count": 0,
            "source_rejected_count": 0,
            "source_rejected_capture_error_count": 0,
            "source_rejected_recorder_leg_failure_count": 0,
            "latest_rejected_attempts": [],
            "source_data_quality_rejected_count": 0,
            "latest_data_quality_rejections": [],
            "selection_denominator_count": 0,
            "cohort_admission_count": 0,
            "case_count": 0,
            "data_quality_complete": True,
            "projected_count": 0,
            "train_case_count": 0,
            "validation_case_count": 0,
            "test_case_count": 0,
            "evaluation_status": "failed",
            "predictive_attempts": 0,
            "structural_attempts": 0,
            "predictive_settled_expiry_cluster_count": 0,
            "structural_settled_expiry_cluster_count": 0,
            "predictive_net_pnl": None,
            "structural_net_pnl": None,
            "net_pnl": None,
            "qualified_net_pnl": None,
            "true_edge": False,
            "true_edge_gate": False,
            "positive_100_trade_check": False,
            "positive_100_trade_pnl_check": False,
            "builder_evaluation_status": None,
            "builder_evaluation_status_diagnostic_only": True,
            "live_execution": False,
            "live": False,
            "prelabel_lock_journal": False,
            "prelabel": False,
            "orders_submitted": 0,
            "orders": 0,
            "authenticated_endpoints_used": 0,
            "paper_only": True,
            "public_only": True,
            "new_orders_disabled": True,
            "last_error": safe_error_details(exc, code="v07_shadow_refresh_failed"),
        }
        _write_runtime_status(config, document)
        return 1
    return 0


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "MODE",
    "V07ShadowConfig",
    "build_parser",
    "load_shadow_config",
    "main",
    "run_once",
    "run_shadow_refresh",
]
