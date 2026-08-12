"""Immutable public-reward capture archives and zero-network replay.

The live experiment intentionally has no authenticated or trading capability.
This module adds an evidence boundary around that experiment: every public
response byte is persisted before a manifest is finalized, and replay consumes
only those verified bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from .reward_experiment import (
    RewardExperimentConfig,
    _json_ready,
    _preselection_reward_risk_score,
    _reward_asset_ids,
    _sort_daily_reward,
    run_reward_experiment,
)
from .sources import (
    CompactCLOBMarket,
    FetchMetadata,
    Fetched,
    PublicSourcesClient,
    RawResponse,
    RewardPage,
    SourceBook,
)


MANIFEST_SCHEMA = "edge-lab-reward-run-manifest-v2"
RUN_DIRECTORY = "reward_runs"
SOURCE_KINDS = frozenset(
    {"reward_page_raw", "compact_market_raw", "book_raw"}
)
CODE_PROVENANCE_PATHS = (
    "scripts/run_reward_experiment.py",
    "src/edge_lab/compatibility.py",
    "src/edge_lab/execution.py",
    "src/edge_lab/models.py",
    "src/edge_lab/network_safety.py",
    "src/edge_lab/public_api.py",
    "src/edge_lab/reward_archive.py",
    "src/edge_lab/reward_experiment.py",
    "src/edge_lab/reward_experiment_cli.py",
    "src/edge_lab/rewards.py",
    "src/edge_lab/sources.py",
)
LOCK_PROVENANCE_PATHS = (
    "pyproject.toml",
    "requirements.in",
    "requirements.lock",
    "uv.lock",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _json_ready(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _json_line(value: Any) -> bytes:
    return (
        json.dumps(
            _json_ready(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)


def _file_record(path: str, data: bytes, *, kind: str) -> dict[str, Any]:
    return {
        "path": path,
        "kind": kind,
        "bytes": len(data),
        "sha256": _sha256(data),
    }


def _metadata_dict(raw: RawResponse) -> dict[str, Any]:
    metadata = raw.metadata
    return {
        "source": metadata.source,
        "method": metadata.method,
        "url": metadata.url,
        "request_params": _json_ready(metadata.request_params),
        "status_code": metadata.status_code,
        "requested_at_epoch_seconds": metadata.requested_at,
        "received_at_epoch_seconds": metadata.received_at,
        "attempt": metadata.attempt,
        "response_headers": dict(metadata.response_headers),
    }


@dataclass(frozen=True)
class CapturedRewardPage:
    index: int
    cursor_in: str | None
    cursor_out: str | None
    fetched: Fetched[RewardPage]


@dataclass(frozen=True)
class CapturedMarket:
    index: int
    condition_id: str
    fetched: Fetched[CompactCLOBMarket]


@dataclass(frozen=True)
class CapturedBook:
    index: int
    token_id: str
    fetched: Fetched[SourceBook]


@dataclass(frozen=True)
class CapturedSourceFailure:
    operation: str
    error_type: str
    error_code: str | None
    raw_response_available: bool


class RecordingRewardSources:
    """Record the exact public fetch objects used by one experiment."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.reward_pages: list[CapturedRewardPage] = []
        self.compact_markets: list[CapturedMarket] = []
        self.books: list[CapturedBook] = []
        self.source_events: list[
            CapturedRewardPage | CapturedMarket | CapturedBook
        ] = []
        self.failures: list[CapturedSourceFailure] = []

    def _record_failure(self, operation: str, exc: Exception) -> None:
        raw = getattr(exc, "raw", None)
        code = getattr(exc, "code", None)
        self.failures.append(
            CapturedSourceFailure(
                operation=operation,
                error_type=type(exc).__name__,
                error_code=None if code is None else str(code),
                raw_response_available=isinstance(raw, RawResponse),
            )
        )

    def current_reward_pages(
        self,
        *,
        limit: int = 500,
        cursor: str | None = None,
        max_pages: int | None = None,
    ) -> Iterable[Fetched[RewardPage]]:
        cursor_in = cursor
        try:
            for fetched in self.delegate.current_reward_pages(
                limit=limit,
                cursor=cursor,
                max_pages=max_pages,
            ):
                captured = CapturedRewardPage(
                    index=len(self.reward_pages) + 1,
                    cursor_in=cursor_in,
                    cursor_out=fetched.value.next_cursor,
                    fetched=fetched,
                )
                self.reward_pages.append(captured)
                self.source_events.append(captured)
                cursor_in = fetched.value.next_cursor
                yield fetched
        except Exception as exc:
            self._record_failure("current_reward_pages", exc)
            raise

    def clob_market(
        self,
        condition_id: str,
    ) -> Fetched[CompactCLOBMarket]:
        try:
            fetched = self.delegate.clob_market(condition_id)
        except Exception as exc:
            self._record_failure("clob_market", exc)
            raise
        captured = CapturedMarket(
            index=len(self.compact_markets) + 1,
            condition_id=condition_id,
            fetched=fetched,
        )
        self.compact_markets.append(captured)
        self.source_events.append(captured)
        return fetched

    def book(self, token_id: str) -> Fetched[SourceBook]:
        try:
            fetched = self.delegate.book(token_id)
        except Exception as exc:
            self._record_failure("book", exc)
            raise
        captured = CapturedBook(
            index=len(self.books) + 1,
            token_id=token_id,
            fetched=fetched,
        )
        self.books.append(captured)
        self.source_events.append(captured)
        return fetched


class RecordingClock:
    """Record only the decision-clock calls made by the experiment."""

    def __init__(self, delegate: Callable[[], float]) -> None:
        self.delegate = delegate
        self.values: list[float] = []

    def __call__(self) -> float:
        value = float(self.delegate())
        self.values.append(value)
        return value


class _ReplayClock:
    def __init__(self, values: Iterable[Any]) -> None:
        self._values = [float(value) for value in values]
        self._index = 0

    def __call__(self) -> float:
        if self._index >= len(self._values):
            raise ValueError("replay decision clock exhausted")
        value = self._values[self._index]
        self._index += 1
        return value

    def assert_exhausted(self) -> None:
        if self._index != len(self._values):
            raise ValueError(
                "replay decision clock has unused values: "
                f"{len(self._values) - self._index}"
            )


def _config_from_dict(raw: Mapping[str, Any]) -> RewardExperimentConfig:
    reward_asset_id = raw.get("reward_asset_id")
    if not isinstance(reward_asset_id, str) or not reward_asset_id.strip():
        raise ValueError(
            "archived reward config requires a non-empty reward_asset_id"
        )
    return RewardExperimentConfig(
        max_markets=int(raw["max_markets"]),
        max_reward_pages=int(raw["max_reward_pages"]),
        max_book_age_ms=int(raw["max_book_age_ms"]),
        max_book_skew_ms=int(raw["max_book_skew_ms"]),
        quote_distance_fraction=Decimal(str(raw["quote_distance_fraction"])),
        competition_multipliers=tuple(
            Decimal(str(value)) for value in raw["competition_multipliers"]
        ),
        paired_fills_per_day=int(raw["paired_fills_per_day"]),
        adverse_one_leg_fills_per_day=int(
            raw["adverse_one_leg_fills_per_day"]
        ),
        reward_asset_id=reward_asset_id,
    )


def _universe_bytes(
    pages: Iterable[CapturedRewardPage],
    config: RewardExperimentConfig,
) -> tuple[bytes, dict[str, int]]:
    page_rows = tuple(pages)
    first_by_condition: dict[str, Mapping[str, Any]] = {}
    first_location: dict[str, tuple[int, int]] = {}
    occurrences: dict[str, int] = {}
    raw_rows: list[tuple[CapturedRewardPage, int, Mapping[str, Any]]] = []
    for page in page_rows:
        for row_index, row in enumerate(page.fetched.value.items, start=1):
            condition_id = str(row.get("condition_id") or "")
            raw_rows.append((page, row_index, row))
            occurrences[condition_id] = occurrences.get(condition_id, 0) + 1
            if condition_id not in first_by_condition:
                first_by_condition[condition_id] = row
                first_location[condition_id] = (page.index, row_index)
    scoped_rows = tuple(
        row
        for row in first_by_condition.values()
        if _reward_asset_ids(row) == (config.reward_asset_id,)
    )
    ordered = sorted(
        scoped_rows,
        key=lambda row: (
            -_preselection_reward_risk_score(row),
            -_sort_daily_reward(row),
            str(row.get("condition_id") or ""),
        ),
    )
    rank_by_condition = {
        str(row.get("condition_id") or ""): rank
        for rank, row in enumerate(ordered, start=1)
    }
    selected = {
        str(row.get("condition_id") or "")
        for row in ordered[: config.max_markets]
    }
    output = bytearray()
    for page, row_index, row in raw_rows:
        condition_id = str(row.get("condition_id") or "")
        first = first_location.get(condition_id) == (page.index, row_index)
        rank = rank_by_condition.get(condition_id) if first else None
        in_asset_scope = (
            _reward_asset_ids(row) == (config.reward_asset_id,)
        )
        output.extend(
            _json_line(
                {
                    "schema": "edge-lab-reward-universe-row-v2",
                    "page_index": page.index,
                    "row_index": row_index,
                    "page_sha256": _sha256(page.fetched.raw.body),
                    "condition_id": condition_id,
                    "reward_asset_ids": list(_reward_asset_ids(row)),
                    "duplicate_condition_id": occurrences[condition_id] > 1,
                    "included_in_unique_universe": first,
                    "preselection_inputs": {
                        "total_daily_rate": row.get("total_daily_rate"),
                        "rewards_max_spread": row.get(
                            "rewards_max_spread"
                        ),
                        "rewards_min_size": row.get("rewards_min_size"),
                    },
                    "preselection_score": (
                        str(_preselection_reward_risk_score(row))
                        if first
                        else None
                    ),
                    "preselection_rank": rank,
                    "selected_for_book_evaluation": (
                        first and condition_id in selected
                    ),
                    "selection_reason": (
                        "selected_by_deterministic_cap"
                        if first and condition_id in selected
                        else (
                            "duplicate_condition_id"
                            if not first
                            else (
                                "outside_reward_asset_scope"
                                if not in_asset_scope
                                else "outside_deterministic_cap"
                            )
                        )
                    ),
                    "raw_row": row,
                }
            )
        )
    return bytes(output), {
        "raw_universe_row_count": len(raw_rows),
        "unique_condition_count": len(first_by_condition),
        "asset_scoped_condition_count": len(scoped_rows),
        "excluded_by_reward_asset_scope": (
            len(first_by_condition) - len(scoped_rows)
        ),
        "duplicate_condition_count": sum(
            count > 1 for count in occurrences.values()
        ),
    }


def _provenance_records(
    repo_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def records(paths: Iterable[str]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for relative in paths:
            path = repo_root / relative
            if not path.is_file():
                raise ValueError(
                    f"required provenance file is missing: {relative}"
                )
            data = path.read_bytes()
            output.append(
                {
                    "path": relative,
                    "bytes": len(data),
                    "sha256": _sha256(data),
                }
            )
        return output

    return records(CODE_PROVENANCE_PATHS), records(LOCK_PROVENANCE_PATHS)


def save_reward_run(
    result: Mapping[str, Any],
    recording: RecordingRewardSources,
    *,
    clock_values: Iterable[float],
    output_dir: str | Path,
) -> Path:
    """Persist one immutable reward experiment plus every source byte."""

    if recording.failures:
        operations = sorted(
            {failure.operation for failure in recording.failures}
        )
        raise ValueError(
            "cannot archive run with uncaptured source failure(s): "
            + ",".join(operations)
        )
    if result.get("universe", {}).get("pagination_complete") is not True:
        raise ValueError(
            "cannot archive run because reward pagination is incomplete"
        )
    if result.get("schema_version") != (
        "edge-lab-liquidity-reward-experiment-v2"
    ):
        raise ValueError("only reward experiment schema v2 can be archived")
    if int(result["universe"]["reward_pages_fetched"]) != len(
        recording.reward_pages
    ):
        raise ValueError("result and recording reward-page counts mismatch")
    experiment_id = str(result.get("experiment_id") or "").strip()
    if not experiment_id:
        raise ValueError("result must contain an experiment_id")
    safe_id = "".join(
        character
        for character in experiment_id
        if character.isalnum() or character in {"-", "_", "."}
    )
    if safe_id != experiment_id:
        raise ValueError("experiment_id contains unsafe path characters")

    captured_by_type = [
        *recording.reward_pages,
        *recording.compact_markets,
        *recording.books,
    ]
    if (
        len(recording.source_events) != len(captured_by_type)
        or {id(captured) for captured in recording.source_events}
        != {id(captured) for captured in captured_by_type}
    ):
        raise ValueError("recording source event index is incomplete")
    source_sequence = {
        id(captured): index
        for index, captured in enumerate(recording.source_events, start=1)
    }
    experiment_bytes = _canonical_json_bytes(result)
    config = _config_from_dict(result["config"])
    universe_bytes, universe_counts = _universe_bytes(
        recording.reward_pages,
        config,
    )
    repo_root = Path(__file__).resolve().parents[2]
    code_records, lock_records = _provenance_records(repo_root)
    recorded_clock_values = list(clock_values)

    root = Path(output_dir).resolve()
    run_parent = root / RUN_DIRECTORY
    run_parent.mkdir(parents=True, exist_ok=True)
    run_dir = run_parent / safe_id
    run_dir.mkdir(exist_ok=False)

    files: list[dict[str, Any]] = []
    experiment_path = "EXPERIMENT.json"
    _write_exclusive(run_dir / experiment_path, experiment_bytes)
    files.append(
        _file_record(
            experiment_path,
            experiment_bytes,
            kind="experiment",
        )
    )

    universe_path = "REWARD_UNIVERSE.jsonl"
    _write_exclusive(run_dir / universe_path, universe_bytes)
    files.append(
        _file_record(
            universe_path,
            universe_bytes,
            kind="reward_universe",
        )
    )

    source_entries: list[dict[str, Any]] = []
    for captured in recording.reward_pages:
        relative = f"raw/reward_pages/{captured.index:04d}.json"
        body = captured.fetched.raw.body
        _write_exclusive(run_dir / relative, body)
        record = _file_record(relative, body, kind="reward_page_raw")
        record.update(
            {
                "page_index": captured.index,
                "source_sequence": source_sequence[id(captured)],
                "cursor_in": captured.cursor_in,
                "cursor_out": captured.cursor_out,
                "response_limit": captured.fetched.value.response_limit,
                "response_count": captured.fetched.value.count,
                "parsed_item_count": len(captured.fetched.value.items),
                "source_metadata": _metadata_dict(captured.fetched.raw),
            }
        )
        files.append(record)
        source_entries.append(record)
    for captured in recording.compact_markets:
        relative = f"raw/compact_markets/{captured.index:04d}.json"
        body = captured.fetched.raw.body
        _write_exclusive(run_dir / relative, body)
        record = _file_record(relative, body, kind="compact_market_raw")
        record.update(
            {
                "condition_id": captured.condition_id,
                "compact_index": captured.index,
                "source_sequence": source_sequence[id(captured)],
                "source_metadata": _metadata_dict(captured.fetched.raw),
            }
        )
        files.append(record)
        source_entries.append(record)
    for captured in recording.books:
        relative = f"raw/books/{captured.index:04d}.json"
        body = captured.fetched.raw.body
        _write_exclusive(run_dir / relative, body)
        record = _file_record(relative, body, kind="book_raw")
        record.update(
            {
                "token_id": captured.token_id,
                "book_index": captured.index,
                "source_sequence": source_sequence[id(captured)],
                "source_metadata": _metadata_dict(captured.fetched.raw),
            }
        )
        files.append(record)
        source_entries.append(record)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "experiment_id": experiment_id,
        "canonical_artifact": experiment_path,
        "reward_universe": universe_path,
        "config": result["config"],
        "experiment_clock_values": recorded_clock_values,
        "capture": {
            "raw_reward_page_count": len(recording.reward_pages),
            "raw_compact_market_count": len(recording.compact_markets),
            "raw_book_count": len(recording.books),
            **universe_counts,
            "terminal_cursor": (
                recording.reward_pages[-1].cursor_out
                if recording.reward_pages
                else None
            ),
        },
        "files": files,
        "source_entry_paths": [
            entry["path"]
            for entry in sorted(
                source_entries,
                key=lambda entry: int(entry["source_sequence"]),
            )
        ],
        "provenance": {
            "repo_root": str(repo_root),
            "code": code_records,
            "locks": lock_records,
        },
        "safety": {
            "network_required_for_replay": False,
            "credentials_read": False,
            "orders_submitted": False,
            "transactions_signed": False,
        },
    }
    _write_exclusive(
        run_dir / "RUN_MANIFEST.json",
        _canonical_json_bytes(manifest),
    )
    return run_dir


def _safe_member(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ValueError(f"unsafe manifest path: {relative}")
    path = root.joinpath(*pure.parts)
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise ValueError(f"manifest path escapes run directory: {relative}")
    return path


def _verify_record(root: Path, record: Mapping[str, Any]) -> bytes:
    relative = str(record.get("path") or "")
    path = _safe_member(root, relative)
    if not path.is_file():
        raise ValueError(f"manifest file missing: {relative}")
    data = path.read_bytes()
    if len(data) != int(record["bytes"]):
        raise ValueError(f"byte length mismatch: {relative}")
    observed = _sha256(data)
    if observed != record["sha256"]:
        raise ValueError(
            f"sha256 mismatch: {relative}: "
            f"expected {record['sha256']}, observed {observed}"
        )
    return data


def _raw_response(record: Mapping[str, Any], data: bytes) -> RawResponse:
    metadata = record["source_metadata"]
    return RawResponse(
        body=data,
        text=data.decode("utf-8"),
        metadata=FetchMetadata(
            source=str(metadata["source"]),
            method=str(metadata["method"]),
            url=str(metadata["url"]),
            request_params=MappingProxyType(
                dict(metadata.get("request_params") or {})
            ),
            status_code=int(metadata["status_code"]),
            requested_at=float(metadata["requested_at_epoch_seconds"]),
            received_at=float(metadata["received_at_epoch_seconds"]),
            attempt=int(metadata["attempt"]),
            response_headers=MappingProxyType(
                dict(metadata.get("response_headers") or {})
            ),
        ),
    )


class _ArchivedRewardSources(PublicSourcesClient):
    """Reuse the strict production parsers without constructing a session."""

    def __init__(
        self,
        entries: Iterable[tuple[Mapping[str, Any], bytes]],
    ) -> None:
        self._entries = [
            (record, _raw_response(record, data))
            for record, data in entries
        ]
        self._entry_index = 0

    def _next(
        self,
        *,
        kind: str,
        url: str,
        params: Mapping[str, Any] | None,
    ) -> tuple[Mapping[str, Any], RawResponse]:
        if self._entry_index >= len(self._entries):
            raise ValueError("replay requested an uncaptured source response")
        record, raw = self._entries[self._entry_index]
        expected_sequence = self._entry_index + 1
        if int(record["source_sequence"]) != expected_sequence:
            raise ValueError("replay source sequence mismatch")
        if record.get("kind") != kind:
            raise ValueError(
                "replay source kind mismatch at sequence "
                f"{expected_sequence}"
            )
        if raw.metadata.url != url:
            raise ValueError(
                "replay source URL mismatch at sequence "
                f"{expected_sequence}"
            )
        if dict(raw.metadata.request_params) != dict(params or {}):
            raise ValueError(
                "replay source request params mismatch at sequence "
                f"{expected_sequence}"
            )
        self._entry_index += 1
        return record, raw

    def _get(
        self,
        url: str,
        *,
        source: str,
        params: Mapping[str, Any] | None = None,
    ) -> RawResponse:
        if source == "clob_rewards":
            _, raw = self._next(
                kind="reward_page_raw",
                url=url,
                params=params,
            )
            return raw
        if source == "clob":
            _, raw = self._next(
                kind="compact_market_raw",
                url=url,
                params=params,
            )
            return raw
        if source == "clob_book":
            _, raw = self._next(
                kind="book_raw",
                url=url,
                params=params,
            )
            return raw
        raise ValueError(f"replay attempted unsupported source: {source}")

    def assert_exhausted(self) -> None:
        if self._entry_index != len(self._entries):
            raise ValueError(
                "replay left source responses unused: "
                f"{len(self._entries) - self._entry_index}"
            )


def _ordered_source_records(
    manifest: Mapping[str, Any],
    file_by_path: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    paths = manifest.get("source_entry_paths")
    if not isinstance(paths, list) or any(
        not isinstance(path, str) or not path for path in paths
    ):
        raise ValueError("source_entry_paths must be an array of paths")
    if len(set(paths)) != len(paths):
        raise ValueError("source_entry_paths contains duplicates")
    declared = [
        record
        for record in file_by_path.values()
        if record.get("kind") in SOURCE_KINDS
    ]
    try:
        ordered = sorted(
            declared,
            key=lambda record: int(record["source_sequence"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "source records require integer source_sequence values"
        ) from exc
    if [
        int(record["source_sequence"]) for record in ordered
    ] != list(range(1, len(ordered) + 1)):
        raise ValueError("source_sequence values must be contiguous")
    ordered_paths = [str(record["path"]) for record in ordered]
    if paths != ordered_paths:
        raise ValueError(
            "source_entry_paths does not match ordered source records"
        )

    indexed_fields = (
        ("reward_page_raw", "page_index"),
        ("compact_market_raw", "compact_index"),
        ("book_raw", "book_index"),
    )
    for kind, field in indexed_fields:
        records = [record for record in ordered if record["kind"] == kind]
        try:
            indexes = [int(record[field]) for record in records]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"{kind} records require integer {field} values"
            ) from exc
        if indexes != list(range(1, len(records) + 1)):
            raise ValueError(f"{field} values must be contiguous")

    capture = manifest.get("capture")
    if not isinstance(capture, Mapping):
        raise ValueError("reward run manifest capture must be an object")
    expected_counts = {
        "raw_reward_page_count": sum(
            record["kind"] == "reward_page_raw" for record in ordered
        ),
        "raw_compact_market_count": sum(
            record["kind"] == "compact_market_raw" for record in ordered
        ),
        "raw_book_count": sum(
            record["kind"] == "book_raw" for record in ordered
        ),
    }
    for key, expected in expected_counts.items():
        if int(capture.get(key, -1)) != expected:
            raise ValueError(f"manifest capture {key} mismatch")
    return ordered


def _assert_replayed_capture_matches_manifest(
    recording: RecordingRewardSources,
    source_records: Iterable[Mapping[str, Any]],
    capture: Mapping[str, Any],
    universe_counts: Mapping[str, int],
) -> None:
    records = list(source_records)
    if len(recording.source_events) != len(records):
        raise ValueError("replayed source event count mismatch")
    for captured, record in zip(recording.source_events, records):
        if isinstance(captured, CapturedRewardPage):
            expected_kind = "reward_page_raw"
            comparisons = {
                "page_index": captured.index,
                "cursor_in": captured.cursor_in,
                "cursor_out": captured.cursor_out,
                "response_limit": captured.fetched.value.response_limit,
                "response_count": captured.fetched.value.count,
                "parsed_item_count": len(captured.fetched.value.items),
            }
        elif isinstance(captured, CapturedMarket):
            expected_kind = "compact_market_raw"
            comparisons = {
                "compact_index": captured.index,
                "condition_id": captured.condition_id,
            }
        else:
            expected_kind = "book_raw"
            comparisons = {
                "book_index": captured.index,
                "token_id": captured.token_id,
            }
        if record.get("kind") != expected_kind:
            raise ValueError("replayed source kind mismatch")
        for field, observed in comparisons.items():
            if record.get(field) != observed:
                raise ValueError(f"replayed {field} mismatch")

    page_records = [
        record
        for record in records
        if record["kind"] == "reward_page_raw"
    ]
    terminal_cursor = (
        page_records[-1].get("cursor_out") if page_records else None
    )
    if capture.get("terminal_cursor") != terminal_cursor:
        raise ValueError("replayed terminal_cursor mismatch")
    replayed_counts = {
        "raw_reward_page_count": len(recording.reward_pages),
        "raw_compact_market_count": len(recording.compact_markets),
        "raw_book_count": len(recording.books),
        **universe_counts,
    }
    for field, observed in replayed_counts.items():
        if int(capture.get(field, -1)) != observed:
            raise ValueError(f"replayed capture {field} mismatch")


def _verify_provenance(manifest: Mapping[str, Any]) -> None:
    provenance = manifest.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("reward run provenance must be an object")
    repo_root = Path(__file__).resolve().parents[2]
    declared_root = provenance.get("repo_root")
    if (
        not isinstance(declared_root, str)
        or declared_root != str(repo_root)
    ):
        raise ValueError("provenance repo_root does not match trusted repo")
    expected_groups = {
        "code": list(CODE_PROVENANCE_PATHS),
        "locks": list(LOCK_PROVENANCE_PATHS),
    }
    for group, expected_paths in expected_groups.items():
        records = provenance.get(group)
        if not isinstance(records, list) or any(
            not isinstance(record, Mapping) for record in records
        ):
            raise ValueError(f"provenance {group} must be an array")
        observed_paths = [
            str(record.get("path") or "") for record in records
        ]
        if observed_paths != expected_paths:
            raise ValueError(f"provenance {group} paths mismatch")
        for record in records:
            path = _safe_member(repo_root, str(record["path"]))
            if not path.is_file():
                raise ValueError(f"provenance file missing: {record['path']}")
            data = path.read_bytes()
            if len(data) != int(record["bytes"]):
                raise ValueError(
                    f"provenance byte length mismatch: {record['path']}"
                )
            if _sha256(data) != record["sha256"]:
                raise ValueError(
                    f"provenance sha256 mismatch: {record['path']}"
                )


def verify_reward_run_manifest(
    run_dir_or_manifest_path: str | Path,
) -> dict[str, Any]:
    """Verify sibling binding, all declared bytes, and code/lock provenance.

    This function is intentionally pure and read-only.  It neither constructs
    a network-capable object nor writes replay output.
    """

    supplied = Path(run_dir_or_manifest_path).resolve()
    path = (
        supplied / "RUN_MANIFEST.json"
        if supplied.is_dir()
        else supplied
    )
    manifest_bytes = path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("unsupported reward run manifest schema")
    root = path.parent
    records = manifest.get("files")
    if not isinstance(records, list):
        raise ValueError("reward run manifest files must be an array")
    file_by_path: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("reward run manifest file record must be an object")
        relative = str(record.get("path") or "")
        if relative in file_by_path:
            raise ValueError(f"duplicate manifest path: {relative}")
        _verify_record(root, record)
        file_by_path[relative] = record
    canonical = str(manifest.get("canonical_artifact") or "")
    if canonical not in file_by_path:
        raise ValueError("canonical artifact is not bound in manifest files")
    canonical_record = file_by_path[canonical]
    if canonical_record.get("kind") != "experiment":
        raise ValueError("canonical artifact record kind must be experiment")
    if sum(record.get("kind") == "experiment" for record in records) != 1:
        raise ValueError("manifest must bind exactly one experiment artifact")
    universe = str(manifest.get("reward_universe") or "")
    if universe not in file_by_path:
        raise ValueError("reward universe is not bound in manifest files")
    if file_by_path[universe].get("kind") != "reward_universe":
        raise ValueError("reward universe record kind is invalid")
    _ordered_source_records(manifest, file_by_path)
    artifact = json.loads((root / canonical).read_text(encoding="utf-8"))
    if artifact.get("schema_version") != (
        "edge-lab-liquidity-reward-experiment-v2"
    ):
        raise ValueError("canonical reward artifact schema is not v2")
    if artifact.get("experiment_id") != manifest.get("experiment_id"):
        raise ValueError("manifest and artifact experiment_id mismatch")
    if artifact.get("config") != manifest.get("config"):
        raise ValueError("manifest and artifact config mismatch")
    _verify_provenance(manifest)
    return {
        "manifest_path": str(path),
        "manifest_sha256": _sha256(manifest_bytes),
        "schema": MANIFEST_SCHEMA,
        "experiment_id": manifest["experiment_id"],
        "canonical_artifact": canonical,
        "canonical_artifact_sha256": canonical_record["sha256"],
        "reward_universe": universe,
        "reward_universe_sha256": file_by_path[universe]["sha256"],
        "declared_file_count": len(records),
        "network_requests": 0,
        "writes_performed": 0,
    }


def replay_reward_run(manifest_path: str | Path) -> dict[str, Any]:
    """Verify and fully recompute a reward run without a network-capable object."""

    path = Path(manifest_path).resolve()
    verify_reward_run_manifest(path)
    manifest_bytes = path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    root = path.parent
    file_by_path: dict[str, tuple[Mapping[str, Any], bytes]] = {}
    for record in manifest["files"]:
        data = _verify_record(root, record)
        file_by_path[str(record["path"])] = (record, data)
    source_records = _ordered_source_records(
        manifest,
        {
            relative: record
            for relative, (record, _) in file_by_path.items()
        },
    )
    entries = [
        file_by_path[str(record["path"])]
        for record in source_records
    ]
    archived = _ArchivedRewardSources(entries)
    recording = RecordingRewardSources(archived)
    clock = _ReplayClock(manifest["experiment_clock_values"])
    config = _config_from_dict(manifest["config"])
    result = run_reward_experiment(
        recording,
        config=config,
        clock=clock,
    )
    archived.assert_exhausted()
    clock.assert_exhausted()

    expected_artifact = file_by_path[manifest["canonical_artifact"]][1]
    replayed_artifact = _canonical_json_bytes(result)
    if replayed_artifact != expected_artifact:
        raise ValueError("replayed experiment bytes do not match canonical artifact")
    expected_universe = file_by_path[manifest["reward_universe"]][1]
    replayed_universe, universe_counts = _universe_bytes(
        recording.reward_pages,
        config,
    )
    if replayed_universe != expected_universe:
        raise ValueError("replayed reward universe bytes do not match")
    _assert_replayed_capture_matches_manifest(
        recording,
        source_records,
        manifest["capture"],
        universe_counts,
    )
    return {
        "manifest_sha256": _sha256(manifest_bytes),
        "artifact_sha256": _sha256(expected_artifact),
        "artifact_bytes_match": True,
        "universe_bytes_match": True,
        "network_requests": 0,
        "raw_reward_page_count": len(recording.reward_pages),
        "raw_universe_row_count": universe_counts[
            "raw_universe_row_count"
        ],
        "unique_condition_count": universe_counts["unique_condition_count"],
    }


__all__ = [
    "MANIFEST_SCHEMA",
    "RecordingClock",
    "RecordingRewardSources",
    "replay_reward_run",
    "save_reward_run",
    "verify_reward_run_manifest",
]
