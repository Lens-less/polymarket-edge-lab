"""Assemble real dynamic-capture evidence into a strict replay request.

This module is the offline bridge between a closed dynamic sidecar group and
the generic production-freeze builder.  It derives every role from finalized
service records, includes the complete closed group inventory, and locates the
three externally captured announcement/Chainlink records by canonical ID.
No caller-supplied PnL, fill count, classification, or role relabeling is used.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .execution_replay_freeze import REQUIRED_ROLE_NAMES
from .execution_replay_freeze_builder import (
    CaptureFreezeBuildResult,
    _source_pair,
    build_production_capture_freeze,
)


class ReplayAssemblyError(ValueError):
    """Finalized capture evidence cannot yet form one strict tracer request."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ReplayCandidate:
    slug: str
    condition_id: str
    opens_at_ms: int
    closes_at_ms: int
    group_id: str
    dynamic_run_root: Path
    required_role_ids: Mapping[str, str]
    source_manifest_paths: tuple[Path, ...]


@dataclass(frozen=True)
class _ReplayCandidateDraft:
    slug: str
    condition_id: str
    opens_at_ms: int
    closes_at_ms: int
    group_id: str
    dynamic_run_root: Path
    required_role_ids: Mapping[str, str]
    source_manifest_paths: tuple[Path, ...]
    missing_external_role_ids: frozenset[str]


def _fail(code: str, detail: str) -> None:
    raise ReplayAssemblyError(code, detail)


def _manifest_records(
    manifest_path: Path,
) -> tuple[dict[str, Any], ...]:
    pair = _source_pair(manifest_path)
    records: list[dict[str, Any]] = []
    for line in pair.raw_bytes.splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            _fail("source_record_invalid", f"record is not an object: {manifest_path}")
        records.append(value)
    return tuple(records)


def _validate_capture_root(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        _fail("capture_root_invalid", f"capture root is not a directory: {root}")


def _finalized_manifests(root: Path) -> tuple[Path, ...]:
    _validate_capture_root(root)
    return tuple(
        sorted(
            path
            for path in root.rglob("*.manifest.json")
            if path.is_file()
            and not path.is_symlink()
            and path.parent.parent.name == "raw"
        )
    )


def _record_locations_for_ids(
    manifest_paths: Iterable[Path],
    record_ids: Iterable[str],
    *,
    label: str,
) -> dict[str, Path]:
    wanted = frozenset(record_ids)
    if not wanted:
        return {}
    locations: dict[str, Path] = {}
    for manifest_path in sorted(set(manifest_paths)):
        for record in _manifest_records(manifest_path):
            if "record_id" not in record:
                _fail(
                    "source_record_invalid",
                    f"{label} record lacks record_id: {manifest_path}",
                )
            record_id = str(record["record_id"])
            if record_id not in wanted:
                continue
            previous = locations.get(record_id)
            if previous is not None:
                _fail(
                    "duplicate_record_id",
                    (
                        f"{label} duplicates required record {record_id}: "
                        f"{previous} and {manifest_path}"
                    ),
                )
            locations[record_id] = manifest_path
    return locations


def _service_payload(
    record: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any]] | None:
    if record.get("source") != "dynamic_short_crypto_service":
        return None
    inner = record.get("payload")
    if (
        not isinstance(inner, Mapping)
        or inner.get("schema_version")
        != "edge-lab-dynamic-short-crypto-service.record.v1"
        or inner.get("source") != "dynamic_short_crypto_service"
        or not isinstance(inner.get("event_type"), str)
        or not isinstance(inner.get("payload"), Mapping)
    ):
        _fail(
            "service_record_invalid",
            f"invalid finalized service record {record.get('record_id')}",
        )
    return str(inner["event_type"]), inner["payload"]


def _dynamic_index(
    run_root: Path,
) -> tuple[
    dict[str, tuple[dict[str, Any], Path]],
    dict[str, list[tuple[str, dict[str, Any], Path, str]]],
]:
    by_id: dict[str, tuple[dict[str, Any], Path]] = {}
    by_slug: dict[
        str,
        list[tuple[str, dict[str, Any], Path, str]],
    ] = {}
    for manifest_path in _finalized_manifests(run_root):
        if manifest_path.parent.name != "dynamic_short_crypto_service":
            continue
        for record in _manifest_records(manifest_path):
            record_id = str(record["record_id"])
            if record_id in by_id:
                _fail(
                    "duplicate_record_id",
                    f"record ID duplicated within dynamic run: {record_id}",
                )
            by_id[record_id] = (record, manifest_path)
            service = _service_payload(record)
            if service is None:
                continue
            event_type, payload = service
            slug = payload.get("slug")
            if isinstance(slug, str) and slug:
                by_slug.setdefault(slug, []).append(
                    (
                        event_type,
                        dict(payload),
                        manifest_path,
                        record_id,
                    )
                )
    return by_id, by_slug


def discover_replay_candidates(
    dynamic_run_roots: Iterable[str | Path],
    *,
    main_capture_root: str | Path,
) -> tuple[ReplayCandidate, ...]:
    """Return every finalized strict-settlement candidate in close-time order."""

    main_root = Path(main_capture_root).expanduser().resolve()
    _validate_capture_root(main_root)
    drafts: list[_ReplayCandidateDraft] = []
    for raw_run_root in dynamic_run_roots:
        run_root = Path(raw_run_root).expanduser().resolve()
        dynamic_by_id, by_slug = _dynamic_index(run_root)
        for slug, rows in sorted(by_slug.items()):
            closures = [
                (payload, manifest, record_id)
                for event_type, payload, manifest, record_id in rows
                if event_type == "decision_to_trade_l2_closure"
            ]
            settlements = [
                (payload, manifest, record_id)
                for event_type, payload, manifest, record_id in rows
                if event_type == "settlement_reconciled"
                and payload.get("action") == "strict_settlement_committed"
                and payload.get("status") == "resolved"
            ]
            if not closures or not settlements:
                continue
            if len(closures) != 1 or len(settlements) != 1:
                _fail(
                    "candidate_terminal_conflict",
                    f"{slug} has duplicate closure/settlement records",
                )
            closure, _, _ = closures[0]
            settlement, _, settlement_record_id = settlements[0]
            required = settlement.get("required_record_ids")
            if (
                not isinstance(required, list)
                or len(required) != 6
                or any(not isinstance(item, str) for item in required)
            ):
                _fail(
                    "settlement_dependency_invalid",
                    f"{slug} strict settlement lacks six record IDs",
                )
            group_id = closure.get("group_id")
            condition_id = closure.get("condition_id")
            opens_at_ms = closure.get("opens_at_ms")
            closes_at_ms = closure.get("closes_at_ms")
            if (
                not isinstance(group_id, str)
                or not group_id
                or not isinstance(condition_id, str)
                or isinstance(opens_at_ms, bool)
                or not isinstance(opens_at_ms, int)
                or isinstance(closes_at_ms, bool)
                or not isinstance(closes_at_ms, int)
            ):
                _fail(
                    "candidate_identity_invalid",
                    f"{slug} closure identity/window is malformed",
                )
            decision_count = closure.get("decision_count")
            decision_record_ids = closure.get("decision_record_ids")
            if decision_count == 0 and decision_record_ids == []:
                continue
            if (
                decision_count != 1
                or not isinstance(decision_record_ids, list)
                or len(decision_record_ids) != 1
                or not isinstance(decision_record_ids[0], str)
                or not decision_record_ids[0]
            ):
                _fail(
                    "candidate_decision_inventory_invalid",
                    (
                        f"{slug} closure must contain either no decisions "
                        "or one immutable decision record"
                    ),
                )
            role_events = {
                "full_window_capture_close_checkpoint_id": (
                    "full_window_capture_close_checkpoint"
                ),
                "decision_to_trade_l2_closure_id": (
                    "decision_to_trade_l2_closure"
                ),
                "pessimistic_submit_latency_evidence_id": (
                    "pessimistic_submit_latency"
                ),
                "settlement_operation_cost_evidence_id": (
                    "settlement_operation_cost"
                ),
            }
            derived_role_ids: dict[str, str] = {}
            for role_name, event_name in role_events.items():
                matches: list[str] = []
                for record_id, (record, _) in dynamic_by_id.items():
                    service = _service_payload(record)
                    if service is None:
                        continue
                    current_event, payload = service
                    if (
                        current_event == event_name
                        and payload.get("slug") == slug
                    ):
                        matches.append(record_id)
                if len(matches) != 1:
                    _fail(
                        "candidate_role_count_invalid",
                        f"{slug} requires one {event_name}, found {len(matches)}",
                    )
                derived_role_ids[role_name] = matches[0]
            role_ids = {
                "durable_announcement_record_id": required[0],
                "durable_gamma_record_id": required[1],
                "durable_chainlink_open_record_id": required[2],
                "durable_chainlink_close_record_id": required[3],
                "durable_close_pong_before_record_id": required[4],
                "durable_close_pong_after_record_id": required[5],
                "atomic_settlement_commit_record_id": settlement_record_id,
                **derived_role_ids,
            }
            if set(role_ids) != set(REQUIRED_ROLE_NAMES):
                _fail("required_role_ids_invalid", f"{slug} role set is incomplete")
            group_root = run_root / "groups" / group_id
            partials = tuple(group_root.rglob("*.partial")) if group_root.exists() else ()
            if partials:
                continue
            group_manifests = _finalized_manifests(group_root)
            if not group_manifests:
                _fail(
                    "candidate_group_missing",
                    f"{slug} has no finalized group inventory",
                )
            selected: set[Path] = set(group_manifests)
            missing_role_ids = {
                record_id
                for record_id in role_ids.values()
                if record_id not in dynamic_by_id
            }
            for record_id in set(role_ids.values()) - missing_role_ids:
                selected.add(dynamic_by_id[record_id][1])
            control_root = run_root / "control"
            control_manifests = (
                _finalized_manifests(control_root)
                if control_root.exists() or control_root.is_symlink()
                else ()
            )
            dynamic_locations = _record_locations_for_ids(
                (*group_manifests, *control_manifests),
                missing_role_ids,
                label=f"{slug} candidate capture",
            )
            selected.update(dynamic_locations.values())
            drafts.append(
                _ReplayCandidateDraft(
                    slug=slug,
                    condition_id=condition_id,
                    opens_at_ms=opens_at_ms,
                    closes_at_ms=closes_at_ms,
                    group_id=group_id,
                    dynamic_run_root=run_root,
                    required_role_ids=role_ids,
                    source_manifest_paths=tuple(sorted(selected)),
                    missing_external_role_ids=frozenset(
                        missing_role_ids - set(dynamic_locations)
                    ),
                )
            )
    external_role_ids = frozenset(
        record_id
        for draft in drafts
        for record_id in draft.missing_external_role_ids
    )
    main_locations = (
        _record_locations_for_ids(
            _finalized_manifests(main_root),
            external_role_ids,
            label="main capture",
        )
        if external_role_ids
        else {}
    )
    candidates: list[ReplayCandidate] = []
    for draft in drafts:
        missing = draft.missing_external_role_ids - set(main_locations)
        if missing:
            _fail(
                "external_role_record_missing",
                (
                    f"{draft.slug} external role IDs not found: "
                    f"{sorted(missing)}"
                ),
            )
        selected = set(draft.source_manifest_paths)
        selected.update(
            main_locations[record_id]
            for record_id in draft.missing_external_role_ids
        )
        candidates.append(
            ReplayCandidate(
                slug=draft.slug,
                condition_id=draft.condition_id,
                opens_at_ms=draft.opens_at_ms,
                closes_at_ms=draft.closes_at_ms,
                group_id=draft.group_id,
                dynamic_run_root=draft.dynamic_run_root,
                required_role_ids=draft.required_role_ids,
                source_manifest_paths=tuple(sorted(selected)),
            )
        )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.closes_at_ms,
                item.opens_at_ms,
                item.slug,
                str(item.dynamic_run_root),
            ),
        )
    )


def build_replay_request_for_candidate(
    candidate: ReplayCandidate,
    *,
    request_base: str | Path,
) -> CaptureFreezeBuildResult:
    """Seal one discovered candidate without changing its evidence set."""

    return build_production_capture_freeze(
        request_base=request_base,
        capture_relative_root=(
            f"phase2_capture_freezes/{candidate.slug}"
        ),
        target={
            "slug": candidate.slug,
            "condition_id": candidate.condition_id,
            "opens_at_ms": candidate.opens_at_ms,
            "closes_at_ms": candidate.closes_at_ms,
        },
        source_manifest_paths=candidate.source_manifest_paths,
        required_role_ids=candidate.required_role_ids,
        request_filename=(
            f"PHASE2_EXECUTION_REPLAY_REQUEST_{candidate.slug}.json"
        ),
    )


__all__ = [
    "ReplayAssemblyError",
    "ReplayCandidate",
    "build_replay_request_for_candidate",
    "discover_replay_candidates",
]
