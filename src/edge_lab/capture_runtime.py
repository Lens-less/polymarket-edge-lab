"""Read-only runtime adapters for the public Edge Lab recorder.

The adapters in this module join the recorder's narrow protocols to the
immutable capture store and public HTTP sources.  They intentionally import no
authentication or trading modules and never inspect environment variables.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .data_store import (
    AppendResult,
    CaptureStore,
    RawBatchWriter,
    canonical_record_id,
)
from .network_safety import (
    is_sensitive_parameter_name,
    sanitized_response_headers,
)
from .recorder import SnapshotEnvelope
from .sources import (
    Fetched,
    PublicSourceError,
    RawResponse,
)


CAPTURE_SCHEMA_VERSION = "edge-lab-recorder.raw.v1"


class SinkClosedError(RuntimeError):
    """Raised when a closed recorder sink is asked to persist more state."""


class BackgroundPersistenceError(RuntimeError):
    """A persistence call timed out/cancelled but later failed in the writer."""


class CheckpointIntegrityError(RuntimeError):
    """A restart checkpoint is malformed or no longer matches durable raw data."""


@dataclass
class _OpenBatch:
    writer: RawBatchWriter
    source: str
    session_id: str | None
    written_records: int = 0


class CaptureStoreRecorderSink:
    """Concurrency-safe ``RecorderSink`` backed by :class:`CaptureStore`."""

    def __init__(
        self,
        store: CaptureStore,
        *,
        max_records_per_batch: int = 10_000,
    ) -> None:
        if (
            isinstance(max_records_per_batch, bool)
            or not isinstance(max_records_per_batch, int)
            or max_records_per_batch < 1
        ):
            raise ValueError("max_records_per_batch must be a positive integer")
        self.store = store
        self.max_records_per_batch = max_records_per_batch
        self._lock = threading.RLock()
        self._batches: dict[tuple[str, str | None], _OpenBatch] = {}
        self._manifests: list[dict[str, Any]] = []
        self._last_manifest_by_source: dict[str, dict[str, Any]] = {}
        self._last_recorder_checkpoint: dict[str, dict[str, Any]] = {}
        self._sources_seen: set[str] = set()
        self._startup_integrity_report = self.store.audit_integrity()
        self._closed = False
        self._submission_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="edge-lab-capture-writer",
        )
        self._accepting_submissions = True
        self._close_io_future: Future[tuple[dict[str, Any], ...]] | None = None
        self._unacknowledged_io: set[Future[Any]] = set()

    @property
    def startup_integrity_report(self) -> dict[str, Any]:
        """Return the read-only startup audit, including orphan partials."""

        return copy.deepcopy(self._startup_integrity_report)

    def load_recorder_checkpoint(
        self, source: str
    ) -> dict[str, Any] | None:
        """Load the last recorder checkpoint without recovering raw partials."""

        document = self.store.read_checkpoint(source)
        if document is None:
            return None
        if (
            document.get("schema_version") != "capture-checkpoint.v1"
            or document.get("source") != source
        ):
            raise CheckpointIntegrityError(
                "checkpoint wrapper schema/source does not match request"
            )
        payload = document.get("checkpoint")
        if not isinstance(payload, Mapping):
            raise CheckpointIntegrityError(
                "checkpoint payload must be a JSON object"
            )
        if (
            payload.get("schema_version")
            == "edge-lab-capture-runtime.checkpoint.v1"
        ):
            capture_runtime = payload.get("capture_runtime")
            if not isinstance(capture_runtime, Mapping):
                raise CheckpointIntegrityError(
                    "capture runtime checkpoint metadata is missing"
                )
            self._verify_checkpoint_raw(
                source, capture_runtime.get("last_finalized_batch")
            )
            recorder_checkpoint = payload.get("recorder_checkpoint")
            if recorder_checkpoint is None:
                return None
            if (
                not isinstance(recorder_checkpoint, Mapping)
                or recorder_checkpoint.get("schema_version")
                != "edge-lab-recorder.checkpoint.v1"
            ):
                raise CheckpointIntegrityError(
                    "inner recorder checkpoint schema is invalid"
                )
            return copy.deepcopy(dict(recorder_checkpoint))
        if (
            payload.get("schema_version")
            != "edge-lab-recorder.checkpoint.v1"
        ):
            raise CheckpointIntegrityError(
                "legacy recorder checkpoint schema is invalid"
            )
        return copy.deepcopy(dict(payload))

    def _verify_checkpoint_raw(
        self, source: str, summary: Any
    ) -> None:
        if summary is None:
            return
        if not isinstance(summary, Mapping):
            raise CheckpointIntegrityError(
                "last finalized batch reference is malformed"
            )
        raw_path_value = summary.get("raw_path")
        checksum = summary.get("checksum")
        if (
            not isinstance(raw_path_value, str)
            or not isinstance(checksum, Mapping)
            or checksum.get("algorithm") != "sha256"
            or not isinstance(checksum.get("value"), str)
        ):
            raise CheckpointIntegrityError(
                "last finalized batch checksum reference is malformed"
            )
        raw_path = (self.store.root / raw_path_value).resolve()
        expected_parent = (self.store.raw_dir / source).resolve()
        if (
            not raw_path.is_relative_to(expected_parent)
            or raw_path.suffix != ".jsonl"
            or not raw_path.is_file()
        ):
            raise CheckpointIntegrityError(
                "checkpoint raw path is missing or outside its source"
            )
        actual_hash, actual_bytes, actual_lines = _hash_file(raw_path)
        if (
            actual_hash != checksum["value"]
            or (
                checksum.get("bytes") is not None
                and checksum.get("bytes") != actual_bytes
            )
            or (
                checksum.get("lines") is not None
                and checksum.get("lines") != actual_lines
            )
        ):
            raise CheckpointIntegrityError(
                "checkpoint raw checksum no longer matches durable data"
            )
        manifest_path = raw_path.with_suffix(".manifest.json")
        if not manifest_path.is_file():
            raise CheckpointIntegrityError(
                "checkpoint raw manifest is missing"
            )
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CheckpointIntegrityError(
                "checkpoint raw manifest is unreadable"
            ) from exc
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("source") != source
            or manifest.get("batch_id") != summary.get("batch_id")
            or manifest.get("raw_path") != raw_path_value
            or manifest.get("record_count") != summary.get("record_count")
            or manifest.get("checksum") != dict(checksum)
        ):
            raise CheckpointIntegrityError(
                "checkpoint raw manifest does not match its reference"
            )

    async def emit(self, record: Mapping[str, Any]) -> AppendResult:
        """Persist without blocking the recorder event loop on filesystem I/O."""

        record_snapshot = copy.deepcopy(dict(record))
        future = self._submit_io(self._emit_sync, record_snapshot)
        return await self._await_io(future)

    def _emit_sync(self, record: Mapping[str, Any]) -> AppendResult:
        with self._lock:
            if self._closed:
                raise SinkClosedError("capture-store recorder sink is closed")
            source = record.get("source")
            received_at = record.get("received_at")
            if not isinstance(source, str) or not source:
                raise ValueError("recorder record source must be a non-empty string")
            if received_at is None:
                raise ValueError("recorder record received_at is required")
            stored_payload = dict(record)
            canonical_record_id(
                {
                    "schema_version": CAPTURE_SCHEMA_VERSION,
                    "source": source,
                    "received_at": received_at,
                    "event_at": record.get("event_at"),
                    "sequence": record.get("sequence"),
                    "payload": stored_payload,
                }
            )
            self._remember_existing_checkpoint(source)
            session_value = record.get("session_id")
            session_id = (
                None if session_value is None else str(session_value)
            )
            key = (source, session_id)
            batch = self._batches.get(key)
            if batch is None:
                batch = _OpenBatch(
                    writer=self.store.open_raw_batch(
                        source=source,
                        batch_id=self._batch_id(session_id),
                        schema_version=CAPTURE_SCHEMA_VERSION,
                    ),
                    source=source,
                    session_id=session_id,
                )
                self._batches[key] = batch
            result = batch.writer.append(
                received_at=received_at,
                event_at=record.get("event_at"),
                sequence=record.get("sequence"),
                payload=stored_payload,
            )
            if result.written:
                batch.written_records += 1
            self._sources_seen.add(source)
            if batch.written_records >= self.max_records_per_batch:
                manifest = self._finalize_batch(key)
                self._persist_checkpoint(
                    source,
                    reason="batch_rotation",
                    recorder_checkpoint=self._last_recorder_checkpoint.get(source),
                    last_manifest=manifest,
                )
            return result

    async def checkpoint(
        self, source: str, checkpoint: Mapping[str, Any]
    ) -> None:
        """Finalize this source's open batch and atomically persist restart state."""

        checkpoint_snapshot = copy.deepcopy(dict(checkpoint))
        future = self._submit_io(
            self._checkpoint_sync, source, checkpoint_snapshot
        )
        await self._await_io(future)

    def _checkpoint_sync(
        self, source: str, checkpoint: Mapping[str, Any]
    ) -> None:
        with self._lock:
            if self._closed:
                raise SinkClosedError("capture-store recorder sink is closed")
            if not isinstance(source, str) or not source:
                raise ValueError("checkpoint source must be a non-empty string")
            if not isinstance(checkpoint, Mapping):
                raise ValueError("checkpoint must be a mapping")
            recorder_checkpoint = copy.deepcopy(dict(checkpoint))
            self._last_recorder_checkpoint[source] = recorder_checkpoint
            self._sources_seen.add(source)
            finalized = self._finalize_source(source)
            last_manifest = (
                finalized[-1]
                if finalized
                else self._last_manifest_by_source.get(source)
            )
            self._persist_checkpoint(
                source,
                reason="recorder_checkpoint",
                recorder_checkpoint=recorder_checkpoint,
                last_manifest=last_manifest,
                updated_at=recorder_checkpoint.get("updated_at"),
            )

    async def close(self) -> tuple[dict[str, Any], ...]:
        """Finalize all remaining batches without blocking the event loop."""

        future = self._submit_close()
        result = await asyncio.shield(asyncio.wrap_future(future))
        with self._submission_lock:
            unacknowledged = tuple(self._unacknowledged_io)
            self._unacknowledged_io.clear()
        failures = [
            item.exception()
            for item in unacknowledged
            if item.done()
            and not item.cancelled()
            and item.exception() is not None
        ]
        if failures:
            raise BackgroundPersistenceError(
                f"{len(failures)} background persistence operation(s) failed"
            ) from failures[0]
        return result

    def _submit_io(self, function: Any, *args: Any) -> Future[Any]:
        with self._submission_lock:
            if not self._accepting_submissions:
                raise SinkClosedError(
                    "capture-store recorder sink is closing or closed"
                )
            future = self._executor.submit(function, *args)
            self._unacknowledged_io.add(future)
            return future

    async def _await_io(self, future: Future[Any]) -> Any:
        try:
            result = await asyncio.shield(asyncio.wrap_future(future))
        except asyncio.CancelledError:
            # ``wait_for`` cancellation cannot safely stop a filesystem write.
            # Keep the single-writer job queued/running and make close report a
            # late failure after it drains the queue.
            raise
        except BaseException:
            with self._submission_lock:
                self._unacknowledged_io.discard(future)
            raise
        with self._submission_lock:
            self._unacknowledged_io.discard(future)
        return result

    def _submit_close(
        self,
    ) -> Future[tuple[dict[str, Any], ...]]:
        with self._submission_lock:
            if self._close_io_future is None:
                self._accepting_submissions = False
                self._close_io_future = self._executor.submit(
                    self._close_sync
                )
                self._close_io_future.add_done_callback(
                    self._shutdown_executor
                )
            return self._close_io_future

    def _shutdown_executor(self, _future: Future[Any]) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _close_sync(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            if self._closed:
                return tuple(self._manifests)
            for key in tuple(self._batches):
                self._finalize_batch(key)
            for source in sorted(self._sources_seen):
                self._persist_checkpoint(
                    source,
                    reason="close",
                    recorder_checkpoint=self._last_recorder_checkpoint.get(source),
                    last_manifest=self._last_manifest_by_source.get(source),
                )
            self._closed = True
            return tuple(self._manifests)

    def _finalize_source(self, source: str) -> list[dict[str, Any]]:
        manifests = []
        for key in tuple(self._batches):
            if key[0] == source:
                manifests.append(self._finalize_batch(key))
        return manifests

    def _finalize_batch(
        self, key: tuple[str, str | None]
    ) -> dict[str, Any]:
        batch = self._batches[key]
        manifest = batch.writer.finalize()
        del self._batches[key]
        self._manifests.append(manifest)
        self._last_manifest_by_source[batch.source] = manifest
        return manifest

    def _persist_checkpoint(
        self,
        source: str,
        *,
        reason: str,
        recorder_checkpoint: Mapping[str, Any] | None,
        last_manifest: Mapping[str, Any] | None,
        updated_at: Any = None,
    ) -> None:
        manifest_summary = None
        if last_manifest is not None:
            checksum = last_manifest.get("checksum")
            manifest_summary = {
                "batch_id": last_manifest.get("batch_id"),
                "raw_path": last_manifest.get("raw_path"),
                "record_count": last_manifest.get("record_count"),
                "finalized_at": last_manifest.get("finalized_at"),
                "checksum": dict(checksum)
                if isinstance(checksum, Mapping)
                else checksum,
            }
        document = {
            "schema_version": "edge-lab-capture-runtime.checkpoint.v1",
            "recorder_checkpoint": (
                None
                if recorder_checkpoint is None
                else copy.deepcopy(dict(recorder_checkpoint))
            ),
            "capture_runtime": {
                "reason": reason,
                "last_finalized_batch": manifest_summary,
                "startup_integrity": self._startup_integrity_report,
            },
        }
        kwargs = {} if updated_at is None else {"updated_at": updated_at}
        self.store.write_checkpoint(source, document, **kwargs)

    def _remember_existing_checkpoint(self, source: str) -> None:
        if source in self._last_recorder_checkpoint:
            return
        checkpoint = self.load_recorder_checkpoint(source)
        if checkpoint is not None:
            self._last_recorder_checkpoint[source] = checkpoint

    @staticmethod
    def _batch_id(session_id: str | None) -> str:
        label = "unscoped"
        if session_id:
            safe = "".join(
                character
                if character.isascii() and character.isalnum()
                else "-"
                for character in session_id
            ).strip("-")
            label = safe[:32] or "session"
        return f"{label}-{uuid.uuid4().hex}"


class PublicSourcesSnapshotClient:
    """Map recorder snapshot kinds onto credential-free public source methods."""

    def __init__(
        self,
        source_client: Any,
        *,
        gamma_page_limit: int = 500,
        gamma_max_pages: int = 1,
        condition_ids: Sequence[str] = (),
        reward_page_limit: int = 500,
        reward_max_pages: int = 1,
        rule_market_ids: Sequence[str] = (),
    ) -> None:
        if (
            isinstance(gamma_page_limit, bool)
            or not isinstance(gamma_page_limit, int)
            or not 1 <= gamma_page_limit <= 500
        ):
            raise ValueError("gamma_page_limit must be between 1 and 500")
        if (
            isinstance(gamma_max_pages, bool)
            or not isinstance(gamma_max_pages, int)
            or gamma_max_pages < 1
        ):
            raise ValueError("gamma_max_pages must be at least one")
        if (
            isinstance(reward_page_limit, bool)
            or not isinstance(reward_page_limit, int)
            or not 1 <= reward_page_limit <= 500
        ):
            raise ValueError("reward_page_limit must be between 1 and 500")
        if (
            isinstance(reward_max_pages, bool)
            or not isinstance(reward_max_pages, int)
            or reward_max_pages < 1
        ):
            raise ValueError("reward_max_pages must be at least one")
        self.source_client = source_client
        self.gamma_page_limit = gamma_page_limit
        self.gamma_max_pages = gamma_max_pages
        self.condition_ids = _normalize_identifiers(
            condition_ids, label="condition_ids"
        )
        self.reward_page_limit = reward_page_limit
        self.reward_max_pages = reward_max_pages
        self.rule_market_ids = _normalize_identifiers(
            rule_market_ids, label="rule_market_ids"
        )
        self._fetch_lock = threading.RLock()

    def fetch_snapshot(
        self, kind: str, *, asset_ids: Sequence[str] = ()
    ) -> dict[str, Any] | SnapshotEnvelope:
        with self._fetch_lock:
            return self._fetch_snapshot_locked(kind, asset_ids=asset_ids)

    def _fetch_snapshot_locked(
        self, kind: str, *, asset_ids: Sequence[str]
    ) -> dict[str, Any] | SnapshotEnvelope:
        supported = {"gamma", "clob", "full_book", "rewards", "rules", "trades"}
        if kind not in supported:
            raise ValueError(f"unsupported public snapshot kind: {kind}")
        requested_asset_ids = _normalize_identifiers(
            asset_ids, label="asset_ids"
        )
        if kind in {"gamma", "rewards", "rules", "trades"} and requested_asset_ids:
            raise ValueError(f"{kind} snapshot does not accept asset_ids")
        if kind == "full_book" and not requested_asset_ids:
            raise ValueError("full_book snapshot requires asset_ids")
        if (
            kind == "clob"
            and not requested_asset_ids
            and not self.condition_ids
        ):
            raise ValueError(
                "clob snapshot requires asset_ids or condition_ids"
            )
        if kind == "rules" and not self.rule_market_ids:
            raise ValueError("rules snapshot requires rule_market_ids")
        if kind == "trades" and not self.condition_ids:
            raise ValueError("trades snapshot requires condition_ids")
        responses: list[dict[str, Any]] = []
        truncated_resources: set[str] = set()

        def capture_resource(
            resource: str,
            fetch: Callable[[], Fetched[Any]],
            *,
            page_number: int | None = None,
            request_key: str | None = None,
        ) -> Fetched[Any] | None:
            try:
                fetched = fetch()
            except PublicSourceError as exc:
                responses.append(
                    _captured_error_response(
                        resource=resource,
                        error=exc,
                        page_number=page_number,
                        request_key=request_key,
                    )
                )
                return None
            responses.append(
                _captured_response(
                    resource=resource,
                    fetched=fetched,
                    page_number=page_number,
                    request_key=request_key,
                )
            )
            return fetched

        if kind == "gamma":
            for resource in ("gamma_events", "gamma_markets"):
                method = getattr(self.source_client, resource)
                cursor: str | None = None
                seen_cursors: set[str] = set()
                for page_number in range(1, self.gamma_max_pages + 1):
                    page_limit = (
                        self.gamma_page_limit
                        if resource == "gamma_events"
                        else min(self.gamma_page_limit, 100)
                    )
                    fetched = capture_resource(
                        resource,
                        lambda: method(
                            limit=page_limit,
                            cursor=cursor,
                        ),
                        page_number=page_number,
                    )
                    if fetched is None:
                        break
                    next_cursor = fetched.value.next_cursor
                    if _is_terminal_cursor(next_cursor):
                        break
                    if page_number == self.gamma_max_pages:
                        truncated_resources.add(resource)
                        break
                    if next_cursor in seen_cursors:
                        raise PublicSourceError(
                            f"{resource} pagination repeated a cursor",
                            raw=fetched.raw,
                        )
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
        elif kind in {"clob", "full_book"}:
            if kind == "clob":
                capture_resource(
                    "clob_server_time",
                    self.source_client.server_time,
                )
                for condition_id in self.condition_ids:
                    capture_resource(
                        "clob_market",
                        lambda condition_id=condition_id: (
                            self.source_client.clob_market(condition_id)
                        ),
                        request_key=condition_id,
                    )
            for asset_id in requested_asset_ids:
                capture_resource(
                    "clob_book",
                    lambda asset_id=asset_id: self.source_client.book(
                        asset_id
                    ),
                    request_key=asset_id,
                )
        elif kind == "rewards":
            iterator_failed = False
            try:
                pages = self.source_client.current_reward_pages(
                    limit=self.reward_page_limit,
                    max_pages=self.reward_max_pages,
                )
            except PublicSourceError as exc:
                responses.append(
                    _captured_error_response(
                        resource="current_rewards",
                        error=exc,
                        page_number=1,
                    )
                )
                pages = ()
                iterator_failed = True
            page_count = 0
            last_cursor: Any = None
            bounded_pages = iter(islice(pages, self.reward_max_pages))
            for page_number in range(1, self.reward_max_pages + 1):
                try:
                    fetched = next(bounded_pages)
                except StopIteration:
                    break
                except PublicSourceError as exc:
                    responses.append(
                        _captured_error_response(
                            resource="current_rewards",
                            error=exc,
                            page_number=page_number,
                        )
                    )
                    iterator_failed = True
                    break
                page_count = page_number
                last_cursor = fetched.value.next_cursor
                responses.append(
                    _captured_response(
                        resource="current_rewards",
                        fetched=fetched,
                        page_number=page_number,
                    )
                )
                if (
                    page_number == self.reward_max_pages
                    and not _is_terminal_cursor(last_cursor)
                ):
                    truncated_resources.add("current_rewards")
            if page_count == 0 and not iterator_failed:
                raise PublicSourceError(
                    "current rewards iterator returned no page"
                )
            if (
                not iterator_failed
                and page_count < self.reward_max_pages
                and not _is_terminal_cursor(last_cursor)
            ):
                raise PublicSourceError(
                    "current rewards iterator ended before its cursor"
                )
        elif kind == "rules":
            for market_id in self.rule_market_ids:
                capture_resource(
                    "resolution_rules",
                    lambda market_id=market_id: (
                        self.source_client.resolution_rules(market_id)
                    ),
                    request_key=market_id,
                )
        elif kind == "trades":
            for condition_id in self.condition_ids:
                capture_resource(
                    "data_api_trades",
                    lambda condition_id=condition_id: self.source_client.data_trades(
                        condition_id,
                        taker_only=True,
                        limit=1_000,
                        offset=0,
                    ),
                    request_key=condition_id,
                )
        snapshot = {
            "schema_version": "edge-lab-public-snapshot.v1",
            "snapshot_kind": kind,
            "requested_asset_ids": list(requested_asset_ids),
            "responses": responses,
            "truncated_resources": sorted(truncated_resources),
        }
        return _lossless_envelope_if_single(snapshot)


def _captured_response(
    *,
    resource: str,
    fetched: Fetched[Any],
    page_number: int | None = None,
    request_key: str | None = None,
) -> dict[str, Any]:
    raw = fetched.raw
    _assert_public_raw_response(raw, resource=resource)
    try:
        raw_json = json.loads(
            raw.text,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise PublicSourceError(
            f"{resource} validated response could not be decoded for capture",
            raw=raw,
        ) from exc
    response = {
        "resource": resource,
        "raw_json": raw_json,
        "provenance": _provenance(raw),
    }
    if resource == "clob_server_time":
        response["clock_probe"] = {
            "server_time_unit": "unix_seconds",
            "server_precision_ms": 1000,
            "ordering_role": "coarse_drift_diagnostic_only",
            "cross_source_reordering_allowed": False,
        }
    if page_number is not None:
        response["page_number"] = page_number
    if request_key is not None:
        response["request_key"] = request_key
    return response


def _captured_error_response(
    *,
    resource: str,
    error: PublicSourceError,
    page_number: int | None = None,
    request_key: str | None = None,
) -> dict[str, Any]:
    error_code = (
        error.code
        if error.code in _PUBLIC_SOURCE_ERROR_CODES
        else "public_source_failed"
    )
    error_type = _safe_error_type(error.error_type)
    response = {
        "resource": resource,
        "response_status": "error",
        "error": {
            "error_type": error_type,
            "error_code": error_code,
        },
        "provenance": _error_provenance(error.raw),
    }
    if page_number is not None:
        response["page_number"] = page_number
    if request_key is not None:
        response["request_key"] = request_key
    return response


_PUBLIC_SOURCE_ERROR_CODES = frozenset(
    {
        "connection_failure",
        "http_status_failure",
        "invalid_json_response",
        "non_utf8_response",
        "proxy_unavailable",
        "public_source_failed",
        "redirect_rejected",
        "request_failure",
        "request_method_forbidden",
        "request_timeout",
        "sensitive_request_parameter",
        "tls_failure",
        "unexpected_failure",
    }
)


def _safe_error_type(value: str | None) -> str:
    if not value:
        return "PublicSourceError"
    sanitized = "".join(
        character
        if character.isascii()
        and (character.isalnum() or character == "_")
        else "_"
        for character in value
    )[:80]
    return sanitized or "PublicSourceError"


def _error_provenance(raw: RawResponse | None) -> dict[str, Any]:
    if raw is None:
        return {"raw_response_available": False}
    metadata = raw.metadata
    parsed = urlsplit(metadata.url)
    try:
        public_url = (
            parsed.scheme == "https"
            and parsed.hostname
            in {"clob.polymarket.com", "gamma-api.polymarket.com"}
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )
    except ValueError:
        public_url = False
    response_headers = sanitized_response_headers(metadata.response_headers)
    omitted_response_headers = [
        str(name)
        for name in metadata.response_headers
        if str(name) not in response_headers
    ]
    provenance = {
        "raw_response_available": True,
        "source": (
            metadata.source
            if metadata.source
            in {
                "clob",
                "clob_book",
                "clob_rewards",
                "clob_time",
                "gamma",
            }
            else "unknown"
        ),
        "method": metadata.method if metadata.method == "GET" else "unknown",
        "url": (
            f"https://{parsed.hostname}{parsed.path}" if public_url else None
        ),
        "request_parameter_names": sorted(
            str(name)
            for name in metadata.request_params
            if not is_sensitive_parameter_name(name)
        ),
        "status_code": metadata.status_code,
        "requested_at_epoch_seconds": repr(metadata.requested_at),
        "received_at_epoch_seconds": repr(metadata.received_at),
        "attempt": metadata.attempt,
        "response_headers": response_headers,
        "body_sha256": hashlib.sha256(raw.body).hexdigest(),
        "body_bytes": len(raw.body),
    }
    if omitted_response_headers:
        provenance["omitted_response_headers"] = sorted(
            omitted_response_headers
        )
    return provenance


def _assert_public_raw_response(
    raw: RawResponse, *, resource: str
) -> None:
    metadata = raw.metadata
    if metadata.method != "GET":
        raise PublicSourceError(
            "snapshot adapter accepts only public GET source responses",
            raw=raw,
        )
    parsed = urlsplit(metadata.url)
    expected_route = {
        "gamma_events": (
            "gamma-api.polymarket.com",
            "gamma",
            lambda path: path == "/events/keyset",
            frozenset({"limit", "after_cursor"}),
        ),
        "gamma_markets": (
            "gamma-api.polymarket.com",
            "gamma",
            lambda path: path == "/markets/keyset",
            frozenset({"limit", "after_cursor"}),
        ),
        "clob_server_time": (
            "clob.polymarket.com",
            "clob_time",
            lambda path: path == "/time",
            frozenset(),
        ),
        "resolution_rules": (
            "gamma-api.polymarket.com",
            "gamma",
            lambda path: path.startswith("/markets/"),
            frozenset(),
        ),
        "clob_market": (
            "clob.polymarket.com",
            "clob",
            lambda path: path.startswith("/clob-markets/"),
            frozenset(),
        ),
        "clob_book": (
            "clob.polymarket.com",
            "clob_book",
            lambda path: path == "/book",
            frozenset({"token_id"}),
        ),
        "current_rewards": (
            "clob.polymarket.com",
            "clob_rewards",
            lambda path: path == "/rewards/markets/current",
            frozenset({"limit", "next_cursor"}),
        ),
        "data_api_trades": (
            "data-api.polymarket.com",
            "data_api_trades",
            lambda path: path == "/trades",
            frozenset({"market", "takeronly", "limit", "offset"}),
        ),
    }.get(resource)
    if (
        expected_route is None
        or parsed.scheme != "https"
        or parsed.hostname != expected_route[0]
        or metadata.source != expected_route[1]
        or parsed.username is not None
        or parsed.password is not None
        or not expected_route[2](parsed.path)
        or parsed.query
        or parsed.fragment
        or not 200 <= metadata.status_code < 300
    ):
        raise PublicSourceError(
            "snapshot adapter received a response from a non-public source",
            raw=raw,
        )
    parameter_keys = {
        str(key).lower().replace("-", "_")
        for key in metadata.request_params
    }
    if not parameter_keys <= expected_route[3]:
        raise PublicSourceError(
            "snapshot adapter rejected credential-like request metadata",
            raw=raw,
        )
    try:
        reencoded = raw.text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PublicSourceError(
            "snapshot adapter requires UTF-8 response text",
            raw=raw,
        ) from exc
    if reencoded != raw.body:
        raise PublicSourceError(
            "snapshot response bytes do not match its UTF-8 text",
            raw=raw,
        )


def _provenance(raw: RawResponse) -> dict[str, Any]:
    metadata = raw.metadata
    response_headers = sanitized_response_headers(metadata.response_headers)
    omitted_response_headers = [
        str(name)
        for name in metadata.response_headers
        if str(name) not in response_headers
    ]
    provenance = {
        "source": metadata.source,
        "method": metadata.method,
        "url": metadata.url,
        "request_params": dict(metadata.request_params),
        "status_code": metadata.status_code,
        "requested_at_epoch_seconds": repr(metadata.requested_at),
        "received_at_epoch_seconds": repr(metadata.received_at),
        "attempt": metadata.attempt,
        "response_headers": response_headers,
        "body_utf8": raw.text,
        "body_sha256": hashlib.sha256(raw.body).hexdigest(),
        "body_bytes": len(raw.body),
    }
    if omitted_response_headers:
        provenance["omitted_response_headers"] = sorted(
            omitted_response_headers
        )
    return provenance


def _reject_json_constant(value: str) -> Any:
    raise PublicSourceError(
        f"public response contains invalid JSON constant: {value}"
    )


def _normalize_identifiers(
    values: Sequence[str], *, label: str
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence of identifiers")
    normalized = tuple(values)
    if any(not isinstance(value, str) or not value for value in normalized):
        raise ValueError(f"{label} must contain non-empty strings")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} cannot contain duplicates")
    return normalized


def _is_terminal_cursor(value: Any) -> bool:
    return value in (None, "", "LTE=")


def _hash_file(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    byte_count = 0
    line_count = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
            line_count += chunk.count(b"\n")
    return digest.hexdigest(), byte_count, line_count


def _lossless_envelope_if_single(
    snapshot: dict[str, Any],
) -> dict[str, Any] | SnapshotEnvelope:
    responses = snapshot["responses"]
    if (
        len(responses) != 1
        or responses[0].get("response_status") == "error"
    ):
        return snapshot
    provenance = responses[0]["provenance"]
    return SnapshotEnvelope(
        payload=snapshot,
        raw_body=provenance["body_utf8"].encode("utf-8"),
        status_code=provenance["status_code"],
        request_url=provenance["url"],
        response_headers=provenance["response_headers"],
        requested_at=provenance["requested_at_epoch_seconds"],
        received_at=provenance["received_at_epoch_seconds"],
    )
