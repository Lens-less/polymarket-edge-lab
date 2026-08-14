#!/usr/bin/env python3
"""Build a fail-closed report from finalized BTC TWAP pilot captures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.edge_lab.btc_twap_relative_value import (  # noqa: E402
    DataHealth,
    PairSettlementState,
    SameExpiryPair,
    StrategyConfig,
    TimedPrice,
    TwapMarketContract,
    ValidationEvidence,
    evaluate_validation,
    simulate_ewma_joint_distribution,
)
from src.edge_lab.btc_twap_relative_value_qualification_runtime import (  # noqa: E402
    QualificationInsufficientData,
    build_daily_qualification_fold,
    fit_qualification_calibrators_from_reports,
)
from src.edge_lab.btc_twap_relative_value_replay import (  # noqa: E402
    BookReplayToken,
    CausalBookReplay,
    evaluate_qualified_paper_cycle,
    evaluate_shadow_paper_cycle,
)
from src.edge_lab.data_store import (  # noqa: E402
    CaptureStore,
    canonical_json_bytes,
)
from src.edge_lab.execution import ExecutionFeeSchedule  # noqa: E402
from src.edge_lab.settlement_regime import (  # noqa: E402
    LEGACY_SETTLEMENT_REGIME_ID,
    canonicalize_resolution_source,
    regime_scope_value,
)

PriceObservation = tuple[int, int, Decimal]


def _iso_to_epoch_ms(value: Any, *, receipt_clock_offset_ms: int = 0) -> int:
    if not isinstance(value, str) or not value:
        raise ValueError("received_at must be a non-empty ISO timestamp")
    parsed = datetime.fromisoformat(
        f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("received_at must include a timezone")
    if isinstance(receipt_clock_offset_ms, bool) or not isinstance(
        receipt_clock_offset_ms, int
    ):
        raise TypeError("receipt_clock_offset_ms must be an integer")
    return (
        int(parsed.astimezone(timezone.utc).timestamp() * 1_000)
        + receipt_clock_offset_ms
    )


def _clock_sync_evidence(root: Path) -> dict[str, Any] | None:
    path = root / "capture-config.json"
    if not path.is_file():
        return None
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    clock_sync = config.get("clock_sync") if isinstance(config, dict) else None
    return dict(clock_sync) if isinstance(clock_sync, dict) else None


def _receipt_clock_offset_ms(root: Path) -> int:
    clock_sync = _clock_sync_evidence(root)
    if clock_sync is None:
        return 0
    value = clock_sync.get("causal_receipt_offset_ms")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"capture has invalid clock-sync offset: {root}")
    return value


def _records(root: Path, source: str) -> Iterable[dict[str, Any]]:
    for path in sorted((root / "raw" / source).glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"non-object JSONL at {path}:{line_number}")
                yield value


def _assert_clean_integrity(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        raise ValueError(f"capture root does not exist: {root}")
    integrity = CaptureStore(root).audit_integrity()
    if any(integrity[key] for key in integrity):
        raise ValueError(f"capture integrity is not clean for {root}: {integrity}")
    return integrity


def _message(record: dict[str, Any]) -> dict[str, Any] | None:
    envelope = record.get("payload")
    if not isinstance(envelope, dict):
        return None
    message = envelope.get("payload")
    return message if isinstance(message, dict) else None


def _series(root: Path, *, topic: str, symbol: str) -> tuple[PriceObservation, ...]:
    receipt_clock_offset_ms = _receipt_clock_offset_ms(root)
    by_timestamp: dict[int, tuple[int, Decimal]] = {}
    for record in _records(root, "rtds_ws"):
        message = _message(record)
        if not message or message.get("topic") != topic:
            continue
        payload = message.get("payload")
        if not isinstance(payload, dict) or payload.get("symbol") != symbol:
            continue
        timestamp = payload.get("timestamp")
        value = payload.get("value")
        if not isinstance(timestamp, int) or isinstance(value, bool):
            continue
        parsed = Decimal(str(value))
        received_at_ms = _iso_to_epoch_ms(
            record.get("received_at"),
            receipt_clock_offset_ms=receipt_clock_offset_ms,
        )
        previous = by_timestamp.get(timestamp)
        if previous is not None and previous[1] != parsed:
            raise ValueError(
                f"conflicting {topic} observations at timestamp {timestamp}"
            )
        if previous is None or received_at_ms < previous[0]:
            by_timestamp[timestamp] = (received_at_ms, parsed)
    return tuple(
        (timestamp, received_at_ms, value)
        for timestamp, (received_at_ms, value) in sorted(by_timestamp.items())
    )


def _combined_series(
    roots: tuple[Path, ...], *, topic: str, symbol: str
) -> tuple[PriceObservation, ...]:
    combined: dict[int, tuple[int, Decimal]] = {}
    for root in roots:
        for timestamp, received_at_ms, value in _series(
            root, topic=topic, symbol=symbol
        ):
            previous = combined.get(timestamp)
            if previous is not None and previous[1] != value:
                raise ValueError(
                    f"conflicting {topic} values across captures at {timestamp}"
                )
            if previous is None or received_at_ms < previous[0]:
                combined[timestamp] = (received_at_ms, value)
    return tuple(
        (timestamp, received_at_ms, value)
        for timestamp, (received_at_ms, value) in sorted(combined.items())
    )


def _target_source_topic(target: Mapping[str, Any]) -> str:
    topic = target.get("source_topic")
    if isinstance(topic, str) and topic:
        if topic not in {
            "crypto_prices_twap_thirty",
            "crypto_prices_twap_sixty",
        }:
            raise ValueError("capture target has an unsupported settlement topic")
        return topic
    window = target.get("twap_window_seconds")
    if window == 30:
        return "crypto_prices_twap_thirty"
    if window == 60:
        return "crypto_prices_twap_sixty"
    raise ValueError("capture target has no supported settlement source")


def _settlement_series_by_horizon(
    roots: tuple[Path, ...],
    targets: Mapping[str, Mapping[str, Any]],
) -> dict[str, tuple[PriceObservation, ...]]:
    """Load the source frozen on each target instead of assuming a horizon map."""

    topics = {
        horizon: _target_source_topic(targets[horizon])
        for horizon in ("5m", "15m")
    }
    by_topic = {
        topic: _combined_series(roots, topic=topic, symbol="btc/usd")
        for topic in set(topics.values())
    }
    return {horizon: by_topic[topics[horizon]] for horizon in ("5m", "15m")}


def _exact(
    series: tuple[PriceObservation, ...],
    timestamp_ms: int,
    *,
    available_by_ms: int | None = None,
) -> Decimal | None:
    values = {
        value
        for timestamp, received_at_ms, value in series
        if timestamp == timestamp_ms
        and (available_by_ms is None or received_at_ms <= available_by_ms)
    }
    if len(values) > 1:
        raise ValueError(f"conflicting boundary values at {timestamp_ms}")
    return next(iter(values)) if values else None


def _exact_observation(
    series: tuple[PriceObservation, ...],
    timestamp_ms: int,
    *,
    available_by_ms: int | None = None,
) -> PriceObservation | None:
    matches = tuple(
        item
        for item in series
        if item[0] == timestamp_ms
        and (available_by_ms is None or item[1] <= available_by_ms)
    )
    if len({item[2] for item in matches}) > 1:
        raise ValueError(f"conflicting boundary values at {timestamp_ms}")
    return min(matches, key=lambda item: item[1]) if matches else None


def _latest_before(
    series: tuple[PriceObservation, ...], timestamp_ms: int
) -> PriceObservation | None:
    eligible = tuple(
        item for item in series if item[0] <= timestamp_ms and item[1] <= timestamp_ms
    )
    return eligible[-1] if eligible else None


def _resample_one_second(
    series: tuple[PriceObservation, ...],
    *,
    decision_at_ms: int,
    seconds: int = 300,
) -> tuple[TimedPrice, ...]:
    if seconds <= 0:
        return ()
    end_grid_ms = decision_at_ms // 1_000 * 1_000
    start_grid_ms = end_grid_ms - (seconds - 1) * 1_000
    observable = sorted(
        (
            max(source_at_ms, received_at_ms),
            source_at_ms,
            received_at_ms,
            value,
        )
        for source_at_ms, received_at_ms, value in series
        if source_at_ms <= decision_at_ms and received_at_ms <= decision_at_ms
    )
    if not observable:
        return ()
    cursor = 0
    latest: tuple[int, int, Decimal] | None = None
    sampled: list[TimedPrice] = []
    for grid_ms in range(start_grid_ms, end_grid_ms + 1, 1_000):
        while cursor < len(observable) and observable[cursor][0] <= grid_ms:
            _, source_at_ms, received_at_ms, value = observable[cursor]
            candidate = (source_at_ms, received_at_ms, value)
            if latest is None or candidate[:2] >= latest[:2]:
                latest = candidate
            cursor += 1
        if latest is None:
            # A capture may start after the left edge of the optional
            # 300-second lookback.  Do not invent the missing prefix; retain
            # only the causally observed suffix.  Once sampling has begun,
            # the existing five-second freshness guard below still rejects
            # every internal outage fail-closed.
            continue
        source_at_ms, received_at_ms, value = latest
        if grid_ms - max(source_at_ms, received_at_ms) > 5_000:
            return ()
        sampled.append(TimedPrice(timestamp_ms=grid_ms, price=value))
    return tuple(sampled)


def _latest_rules(root: Path, market_ids: set[str]) -> dict[str, dict[str, Any]]:
    latest: dict[str, tuple[str, dict[str, Any]]] = {}
    for record in _records(root, "rules_http"):
        received_at = str(record.get("received_at") or "")
        envelope = record.get("payload")
        snapshot = envelope.get("payload") if isinstance(envelope, dict) else None
        responses = snapshot.get("responses") if isinstance(snapshot, dict) else None
        if not isinstance(responses, list):
            continue
        for response in responses:
            raw = response.get("raw_json") if isinstance(response, dict) else None
            market_id = str(raw.get("id")) if isinstance(raw, dict) else ""
            if market_id not in market_ids:
                continue
            if market_id not in latest or received_at > latest[market_id][0]:
                latest[market_id] = (received_at, raw)
    return {market_id: value for market_id, (_, value) in latest.items()}


def _event_counts(root: Path, source: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for record in _records(root, source):
        envelope = record.get("payload")
        if isinstance(envelope, dict):
            counts[str(envelope.get("event_type") or "unknown")] += 1
    return dict(sorted(counts.items()))


def _capture_runtime_health(root: Path) -> dict[str, Any] | None:
    path = root / "capture-summary.json"
    if not path.is_file():
        return None
    summary = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise TypeError("capture summary must be an object")
    if (
        summary.get("paper_only") is not True
        or summary.get("public_only") is not True
        or summary.get("new_orders_disabled") is not True
        or summary.get("authenticated_endpoints_used") != 0
        or summary.get("orders_submitted") != 0
    ):
        raise ValueError("capture summary violates the public paper-only guard")
    recorder_leg_count = summary.get("recorder_leg_count")
    recorder_leg_failures = summary.get("recorder_leg_failures")
    websocket_redundancy = summary.get("websocket_redundancy")
    if (
        isinstance(recorder_leg_count, bool)
        or not isinstance(recorder_leg_count, int)
        or recorder_leg_count < 1
        or not isinstance(recorder_leg_failures, list)
        or not isinstance(websocket_redundancy, dict)
    ):
        raise ValueError("capture summary has invalid recorder-health evidence")
    return {
        "recorder_leg_count": recorder_leg_count,
        "recorder_leg_failures": recorder_leg_failures,
        "websocket_redundancy": websocket_redundancy,
        "capture_error": summary.get("capture_error"),
    }


def _book_observations(
    root: Path,
    asset_ids: set[str],
) -> tuple[dict[str, Any], ...]:
    receipt_clock_offset_ms = _receipt_clock_offset_ms(root)
    observations: list[dict[str, Any]] = []
    for record in _records(root, "clob_market_ws"):
        envelope = record.get("payload")
        if not isinstance(envelope, dict) or envelope.get("event_type") != "book":
            continue
        payload = envelope.get("payload")
        token_id = str(payload.get("asset_id")) if isinstance(payload, dict) else ""
        if token_id not in asset_ids:
            continue
        timestamp = payload.get("timestamp")
        try:
            source_at_ms = int(timestamp)
        except (TypeError, ValueError):
            continue
        bids = payload.get("bids")
        asks = payload.get("asks")
        observations.append(
            {
                "token_id": token_id,
                "source_at_ms": source_at_ms,
                "received_at_ms": _iso_to_epoch_ms(
                    record.get("received_at"),
                    receipt_clock_offset_ms=receipt_clock_offset_ms,
                ),
                "bid_levels": len(bids) if isinstance(bids, list) else 0,
                "ask_levels": len(asks) if isinstance(asks, list) else 0,
                "depth_policy": payload.get("depth_policy"),
                "source_event_id": str(record.get("record_id") or ""),
            }
        )
    return tuple(observations)


def _book_replay_coverage(
    observations: tuple[dict[str, Any], ...],
    *,
    token_ids: tuple[str, ...],
    decision_at_ms: int,
    taker_delay_ms: int,
    max_book_age_ms: int,
) -> dict[str, Any]:
    if not token_ids or len(set(token_ids)) != len(token_ids):
        raise ValueError("token_ids must be non-empty and unique")
    if any(not isinstance(token_id, str) or not token_id for token_id in token_ids):
        raise ValueError("token_ids must contain non-empty strings")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (decision_at_ms, taker_delay_ms, max_book_age_ms)
    ):
        raise ValueError("book replay timing inputs must be non-negative integers")
    execution_threshold_ms = decision_at_ms + taker_delay_ms
    by_token: dict[str, dict[str, Any]] = {}
    for token_id in token_ids:
        ordered = sorted(
            (
                item
                for item in observations
                if item.get("token_id") == token_id
                and int(item.get("bid_levels", 0)) > 0
                and int(item.get("ask_levels", 0)) > 0
            ),
            key=lambda item: (
                int(item["received_at_ms"]),
                int(item["source_at_ms"]),
                str(item.get("source_event_id", "")),
            ),
        )
        signal_candidates = [
            item
            for item in ordered
            if int(item["source_at_ms"]) <= decision_at_ms
            and int(item["received_at_ms"]) <= decision_at_ms
            and decision_at_ms - int(item["source_at_ms"]) <= max_book_age_ms
            and decision_at_ms - int(item["received_at_ms"]) <= max_book_age_ms
        ]
        execution_candidates = [
            item
            for item in ordered
            if execution_threshold_ms
            <= int(item["source_at_ms"])
            <= execution_threshold_ms + max_book_age_ms
            and execution_threshold_ms
            <= int(item["received_at_ms"])
            <= execution_threshold_ms + max_book_age_ms
        ]
        signal = signal_candidates[-1] if signal_candidates else None
        execution = execution_candidates[0] if execution_candidates else None
        received_times = [int(item["received_at_ms"]) for item in ordered]
        gaps = [
            following - current
            for current, following in zip(received_times, received_times[1:])
        ]
        by_token[token_id] = {
            "book_observations": len(ordered),
            "maximum_received_gap_ms": max(gaps) if gaps else None,
            "signal_source_event_id": (
                signal.get("source_event_id") if signal is not None else None
            ),
            "signal_source_at_ms": (
                signal.get("source_at_ms") if signal is not None else None
            ),
            "signal_received_at_ms": (
                signal.get("received_at_ms") if signal is not None else None
            ),
            "execution_source_event_id": (
                execution.get("source_event_id") if execution is not None else None
            ),
            "execution_source_at_ms": (
                execution.get("source_at_ms") if execution is not None else None
            ),
            "execution_received_at_ms": (
                execution.get("received_at_ms") if execution is not None else None
            ),
        }
    return {
        "decision_at_ms": decision_at_ms,
        "taker_delay_ms": taker_delay_ms,
        "max_book_age_ms": max_book_age_ms,
        "complete_four_token_signal_surface": all(
            item["signal_source_event_id"] is not None for item in by_token.values()
        ),
        "complete_four_token_delayed_execution_surface": all(
            item["execution_source_event_id"] is not None for item in by_token.values()
        ),
        "tokens": by_token,
    }


def _resolved_events(root: Path, market_ids: set[str]) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    receipt_clock_offset_ms = _receipt_clock_offset_ms(root)
    for record in _records(root, "clob_market_ws"):
        envelope = record.get("payload")
        if (
            not isinstance(envelope, dict)
            or envelope.get("event_type") != "market_resolved"
        ):
            continue
        payload = envelope.get("payload")
        market_id = str(payload.get("id")) if isinstance(payload, dict) else ""
        if market_id in market_ids:
            resolved[market_id] = {
                "event_at": envelope.get("event_at"),
                "received_at_ms": _iso_to_epoch_ms(
                    record.get("received_at"),
                    receipt_clock_offset_ms=receipt_clock_offset_ms,
                ),
                "condition_id": payload.get("market"),
                "winning_asset_id": payload.get("winning_asset_id"),
                "winning_outcome": payload.get("winning_outcome"),
            }
    return resolved


def _resolution_event_validation(
    event: Mapping[str, Any] | None,
    target: Mapping[str, Any],
) -> tuple[bool, str | None]:
    if not isinstance(event, Mapping):
        return False, "resolution_event_missing"
    if (
        str(event.get("condition_id", "")).lower()
        != str(target.get("condition_id", "")).lower()
    ):
        return False, "resolution_condition_mismatch"
    winning_asset_id = str(event.get("winning_asset_id", ""))
    expected_outcome = (
        "Up"
        if winning_asset_id == str(target.get("up_token_id", ""))
        else "Down"
        if winning_asset_id == str(target.get("down_token_id", ""))
        else None
    )
    if expected_outcome is None:
        return False, "resolution_winning_token_mismatch"
    if event.get("winning_outcome") != expected_outcome:
        return False, "resolution_outcome_token_mismatch"
    return True, None


def _assert_frozen_decision_tau(
    preregistration: Mapping[str, Any], decision_tau_seconds: int
) -> None:
    frozen = preregistration.get("frozen_strategy")
    if not isinstance(frozen, Mapping):
        raise ValueError("preregistration has no frozen strategy")
    ticks = frozen.get("decision_tau_seconds")
    if (
        not isinstance(ticks, list)
        or not ticks
        or any(isinstance(item, bool) or not isinstance(item, int) for item in ticks)
    ):
        raise ValueError("preregistration has invalid frozen decision ticks")
    if (
        isinstance(decision_tau_seconds, bool)
        or not isinstance(decision_tau_seconds, int)
        or decision_tau_seconds not in ticks
    ):
        raise ValueError("decision tau must be one of the frozen decision ticks")


def _epoch_ms_to_utc_iso(timestamp_ms: int) -> str:
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
        raise TypeError("timestamp_ms must be an integer")
    return (
        datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _prospective_cutoff_ms(preregistration: Mapping[str, Any]) -> int | None:
    scope = preregistration.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("preregistration scope must be an object")
    evidence_track = scope.get("evidence_track_id")
    if evidence_track is None:
        return None
    if not isinstance(evidence_track, str) or not evidence_track:
        raise ValueError("scope.evidence_track_id must be a non-empty string")
    cutoff = scope.get("prospective_only_after")
    if not isinstance(cutoff, str) or not cutoff:
        raise ValueError("scope.prospective_only_after must be a UTC timestamp")
    return _iso_to_epoch_ms(cutoff)


def _capture_identity(root: Path) -> tuple[int, str]:
    path = root / "capture-config.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"capture identity is unreadable: {root}") from exc
    if not isinstance(document, Mapping):
        raise ValueError(f"capture identity is malformed: {root}")
    started_at_ms = document.get("capture_started_at_ms")
    track = document.get("evidence_track_id")
    if (
        isinstance(started_at_ms, bool)
        or not isinstance(started_at_ms, int)
        or started_at_ms < 0
    ):
        raise ValueError(f"capture_started_at_ms is invalid: {root}")
    if not isinstance(track, str) or not track:
        raise ValueError(f"evidence_track_id is invalid: {root}")
    return started_at_ms, track


def _validate_prospective_report_identity(
    *,
    capture_config: Mapping[str, Any],
    preregistration: Mapping[str, Any],
    capture_root: Path,
    predictor_root: Path,
    history_roots: tuple[Path, ...],
    decision_at_ms: int,
) -> dict[str, Any] | None:
    """Fail closed before any v2 evidence can cross its frozen boundary."""

    cutoff_ms = _prospective_cutoff_ms(preregistration)
    if cutoff_ms is None:
        return None
    scope = preregistration["scope"]
    assert isinstance(scope, Mapping)
    expected_track = str(scope["evidence_track_id"])
    started_at_ms = capture_config.get("capture_started_at_ms")
    track = capture_config.get("evidence_track_id")
    if (
        isinstance(started_at_ms, bool)
        or not isinstance(started_at_ms, int)
        or started_at_ms < cutoff_ms
    ):
        raise ValueError("current capture predates prospective_only_after")
    if track != expected_track:
        raise ValueError("current capture evidence_track_id mismatch")
    expected_regime = scope.get("settlement_regime")
    if expected_regime is not None:
        configured_regime = capture_config.get("settlement_regime_id")
        if (
            not isinstance(expected_regime, str)
            or not isinstance(configured_regime, str)
            or regime_scope_value(expected_regime)
            != regime_scope_value(configured_regime)
        ):
            raise ValueError("current capture settlement regime mismatch")
    configured_root = capture_config.get("data_root")
    if (
        not isinstance(configured_root, str)
        or Path(configured_root).resolve() != capture_root
    ):
        raise ValueError("capture config data_root does not match capture root")
    if decision_at_ms < cutoff_ms:
        raise ValueError("decision predates prospective_only_after")

    checked_roots: set[Path] = {capture_root}
    for label, root in (("predictor", predictor_root),):
        if root in checked_roots:
            continue
        root_started_at_ms, root_track = _capture_identity(root)
        if root_started_at_ms < cutoff_ms or root_track != expected_track:
            raise ValueError(
                f"{label} capture is outside the prospective evidence track"
            )
        if root_started_at_ms > started_at_ms:
            raise ValueError(f"{label} capture starts after the current capture")
        checked_roots.add(root)
    for root in history_roots:
        if root in checked_roots:
            raise ValueError("history roots must be distinct from active captures")
        root_started_at_ms, root_track = _capture_identity(root)
        if root_started_at_ms < cutoff_ms or root_track != expected_track:
            raise ValueError(
                "history capture is outside the prospective evidence track"
            )
        if root_started_at_ms >= started_at_ms:
            raise ValueError(
                "history capture must start strictly before current capture"
            )
        checked_roots.add(root)
    return {
        "verified": True,
        "verification_version": "v2",
        "evidence_track": expected_track,
        "prospective_only_after_ms": cutoff_ms,
        "capture_started_at_ms": started_at_ms,
        "decision_at_ms": decision_at_ms,
        "history_roots_verified": len(history_roots),
    }


def _load_prior_qualification_reports(
    report_paths: tuple[Path, ...],
    *,
    decision_tau_seconds: int,
) -> tuple[dict[str, Any], ...]:
    reports: list[dict[str, Any]] = []
    for path in sorted({item.resolve() for item in report_paths}):
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"prior qualification report is malformed: {path}")
        inputs = document.get("inputs")
        if not isinstance(inputs, Mapping):
            raise ValueError(f"prior qualification report has no inputs: {path}")
        tau = inputs.get("decision_tau_seconds")
        if isinstance(tau, bool) or not isinstance(tau, int):
            raise ValueError(f"prior qualification report has invalid tau: {path}")
        if tau == decision_tau_seconds:
            reports.append(document)
    return tuple(reports)


def _normalized_binary_ask_probability(
    signal_observations: Mapping[str, Any],
    *,
    up_token_id: str,
    down_token_id: str,
) -> Decimal | None:
    up = signal_observations.get(up_token_id)
    down = signal_observations.get(down_token_id)
    up_asks = getattr(getattr(up, "snapshot", None), "asks", ())
    down_asks = getattr(getattr(down, "snapshot", None), "asks", ())
    if not up_asks or not down_asks:
        return None
    up_ask = Decimal(str(up_asks[0].price))
    down_ask = Decimal(str(down_asks[0].price))
    denominator = up_ask + down_ask
    if up_ask < 0 or down_ask < 0 or denominator <= 0:
        return None
    return up_ask / denominator


def _normalized_decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return format(normalized, "f")


def _candidate_hypotheses(
    *,
    cycle: Mapping[str, Any],
    signal_observations: Mapping[str, Any],
    pair: Any,
    boundaries: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Record causal candidate inputs and attach labels only after settlement."""

    decision = cycle.get("decision")
    if not isinstance(decision, Mapping):
        return {
            "schema_version": "btc-relative-value-candidate-hypotheses.v1",
            "available": False,
            "candidate_inputs_are_decision_time_only": True,
            "outcome_labels_added_after_settlement": True,
            "reason": "shadow_decision_unavailable",
        }

    def decimal_field(name: str) -> Decimal | None:
        value = decision.get(name)
        try:
            parsed = Decimal(str(value))
        except (ArithmeticError, ValueError):
            return None
        return parsed if parsed.is_finite() else None

    q_5 = decimal_field("q_5_raw")
    q_15 = decimal_field("q_15_raw")
    loss_probability = decimal_field("loss_probability")
    market_5 = (
        _normalized_binary_ask_probability(
            signal_observations,
            up_token_id=pair.market_5.up_token_id,
            down_token_id=pair.market_5.down_token_id,
        )
        if pair is not None
        else None
    )
    market_15 = (
        _normalized_binary_ask_probability(
            signal_observations,
            up_token_id=pair.market_15.up_token_id,
            down_token_id=pair.market_15.down_token_id,
        )
        if pair is not None
        else None
    )

    quantity = decimal_field("quantity")
    selected_depths: list[Decimal | None] = []
    for field in ("first_token_id", "second_token_id"):
        observation = signal_observations.get(str(decision.get(field) or ""))
        asks = getattr(getattr(observation, "snapshot", None), "asks", ())
        selected_depths.append(
            sum((Decimal(str(level.size)) for level in asks), Decimal(0))
            if asks
            else None
        )
    ratios = [
        depth / quantity
        if depth is not None and quantity is not None and quantity > 0
        else None
        for depth in selected_depths
    ]
    thresholds = (Decimal("1.25"), Decimal("1.5"), Decimal("2"))
    depth_passes = {
        _normalized_decimal_text(threshold): (
            all(ratio is not None and ratio >= threshold for ratio in ratios)
        )
        for threshold in thresholds
    }
    extreme_bound = Decimal("0.05")
    return {
        "schema_version": "btc-relative-value-candidate-hypotheses.v1",
        "available": True,
        "candidate_inputs_are_decision_time_only": True,
        "outcome_labels_added_after_settlement": True,
        "canonical_action_unchanged": True,
        "requires_separate_preregistration_for_action": True,
        "probability": {
            "model_probability_up": {
                "5m": _normalized_decimal_text(q_5),
                "15m": _normalized_decimal_text(q_15),
            },
            "market_probability_up": {
                "5m": _normalized_decimal_text(market_5),
                "15m": _normalized_decimal_text(market_15),
            },
            "actual_up": {
                horizon: (
                    boundary.get("mechanical_outcome") == "Up"
                    if boundary.get("mechanical_outcome") in {"Up", "Down"}
                    else None
                )
                for horizon, boundary in boundaries.items()
            },
            "a1_extreme_probability_veto_passes": (
                q_5 is not None
                and q_15 is not None
                and extreme_bound <= q_5 <= Decimal(1) - extreme_bound
                and extreme_bound <= q_15 <= Decimal(1) - extreme_bound
            ),
            "a4_loss_probability_veto_passes": (
                loss_probability is not None
                and loss_probability <= Decimal("0.45")
            ),
        },
        "depth_buffer": {
            "quantity": _normalized_decimal_text(quantity),
            "first_leg_ask_depth": _normalized_decimal_text(selected_depths[0]),
            "second_leg_ask_depth": _normalized_decimal_text(selected_depths[1]),
            "first_leg_ratio": _normalized_decimal_text(ratios[0]),
            "second_leg_ratio": _normalized_decimal_text(ratios[1]),
            "passes": depth_passes,
        },
        "existing_canonical_controls": {
            "signal_walks_both_legs_to_full_target": True,
            "timeout_then_immediate_unwind": True,
        },
    }


def _qualification_event_identity(
    *, evidence_track: str, expiry_ms: int, decision_tau_seconds: int
) -> tuple[str, str]:
    cluster_id = f"{evidence_track}:{expiry_ms}"
    event_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "evidence_track": evidence_track,
                "expiry_ms": expiry_ms,
                "decision_tau_seconds": decision_tau_seconds,
            }
        )
    ).hexdigest()
    return event_id, cluster_id


def _bind_verified_oos_label(
    forecast: Mapping[str, Any],
    *,
    label_available: bool,
    label_available_at_ms: int | None,
) -> dict[str, Any]:
    """Attach a test label only after the local mechanical evidence is final."""

    document = dict(forecast)
    if document.get("available") is not True:
        return document
    if not label_available or label_available_at_ms is None:
        return {
            "available": False,
            "split": "test",
            "reason_codes": ["verified_mechanical_label_not_available"],
        }
    document["local_label_available_at_ms"] = label_available_at_ms
    return document


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_string_list(value: Any) -> tuple[str, ...] | None:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, list) or any(
        not isinstance(item, str) for item in decoded
    ):
        return None
    return tuple(decoded)


def _pair_from_capture_targets(
    by_horizon: Mapping[str, Mapping[str, Any]],
) -> SameExpiryPair | None:
    contracts: dict[str, TwapMarketContract] = {}
    for horizon in ("5m", "15m"):
        target = by_horizon.get(horizon)
        if not isinstance(target, Mapping):
            return None
        fee = target.get("fee_schedule")
        if not isinstance(fee, Mapping):
            return None
        required = (
            "slug",
            "market_id",
            "condition_id",
            "up_token_id",
            "down_token_id",
            "opens_at_ms",
            "closes_at_ms",
            "twap_window_seconds",
            "resolution_source",
            "tick_size",
            "minimum_order_size",
            "taker_delay_ms",
            "accepting_orders",
            "rule_hash",
        )
        if any(key not in target for key in required):
            return None
        string_fields = (
            "slug",
            "market_id",
            "condition_id",
            "up_token_id",
            "down_token_id",
            "resolution_source",
            "rule_hash",
        )
        if any(
            not isinstance(target[key], str) or not target[key] for key in string_fields
        ):
            return None
        if (
            target["up_token_id"] == target["down_token_id"]
            or not isinstance(target["accepting_orders"], bool)
            or not isinstance(fee.get("taker_only"), bool)
        ):
            return None
        try:
            fee_schedule = ExecutionFeeSchedule(
                rate=Decimal(str(fee["rate"])),
                exponent=Decimal(str(fee["exponent"])),
                taker_only=fee["taker_only"],
            )
            window_seconds = int(target["twap_window_seconds"])
            contract = TwapMarketContract(
                horizon=horizon,
                slug=str(target["slug"]),
                market_id=str(target["market_id"]),
                condition_id=str(target["condition_id"]),
                up_token_id=str(target["up_token_id"]),
                down_token_id=str(target["down_token_id"]),
                opens_at_ms=int(target["opens_at_ms"]),
                closes_at_ms=int(target["closes_at_ms"]),
                twap_window_seconds=window_seconds,
                source_topic=(
                    str(target["source_topic"])
                    if isinstance(target.get("source_topic"), str)
                    and target["source_topic"]
                    else "crypto_prices_twap_thirty"
                    if window_seconds == 30
                    else "crypto_prices_twap_sixty"
                ),
                resolution_source=str(target["resolution_source"]),
                tick_size=Decimal(str(target["tick_size"])),
                minimum_order_size=Decimal(str(target["minimum_order_size"])),
                fee_schedule=fee_schedule,
                taker_delay_ms=int(target["taker_delay_ms"]),
                accepting_orders=target["accepting_orders"],
                rule_hash=str(target["rule_hash"]),
                settlement_regime=str(
                    target.get("settlement_regime", LEGACY_SETTLEMENT_REGIME_ID)
                ),
            )
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None
        contracts[horizon] = contract
    try:
        return SameExpiryPair.from_contracts(contracts["5m"], contracts["15m"])
    except (KeyError, ValueError):
        return None


def _strategy_config(preregistration: Mapping[str, Any]) -> StrategyConfig:
    frozen = preregistration["frozen_strategy"]
    scope = preregistration.get("scope")
    if isinstance(scope, Mapping) and isinstance(scope.get("settlement_regime"), str):
        settlement_regime = str(scope["settlement_regime"])
        if not settlement_regime.endswith(".v1"):
            settlement_regime = f"{settlement_regime}.v1"
    else:
        settlement_regime = LEGACY_SETTLEMENT_REGIME_ID
    return StrategyConfig(
        tau_min_seconds=int(frozen["tau_min_seconds"]),
        tau_max_seconds=int(frozen["tau_max_seconds"]),
        maximum_spread_each_leg=Decimal(str(frozen["max_spread_each_leg"])),
        maximum_chainlink_staleness_ms=int(frozen["max_chainlink_staleness_ms"]),
        maximum_book_staleness_ms=int(frozen["max_book_staleness_ms"]),
        maximum_clock_drift_ms=int(frozen["max_clock_drift_ms"]),
        pair_risk_usdc=Decimal(str(frozen["pair_risk_usdc"])),
        minimum_net_expected_pnl_per_pair=Decimal(
            str(frozen["minimum_net_expected_pnl_per_pair"])
        ),
        uncertainty_multiplier=Decimal(str(frozen["uncertainty_multiplier"])),
        settlement_regime=settlement_regime,
    )


def _opening_event_id(
    roots: tuple[Path, ...],
    *,
    topic: str,
    timestamp_ms: int,
    expected_value: Decimal,
) -> str | None:
    matches: set[str] = set()
    for root in roots:
        for record in _records(root, "rtds_ws"):
            message = _message(record)
            if not message or message.get("topic") != topic:
                continue
            payload = message.get("payload")
            if not isinstance(payload, Mapping):
                continue
            try:
                observed_value = Decimal(str(payload.get("value")))
            except (ArithmeticError, ValueError):
                continue
            if (
                payload.get("symbol") != "btc/usd"
                or payload.get("timestamp") != timestamp_ms
                or not observed_value.is_finite()
                or observed_value != expected_value
            ):
                continue
            record_id = record.get("record_id")
            if isinstance(record_id, str) and record_id:
                matches.add(record_id)
    if len(matches) > 1:
        raise ValueError(f"conflicting source event IDs at {timestamp_ms}")
    return next(iter(matches)) if matches else None


def _replay_observation_rows(replay: CausalBookReplay) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "token_id": token_id,
            "source_at_ms": item.source_at_ms,
            "received_at_ms": item.received_at_ms,
            "bid_levels": len(item.snapshot.bids),
            "ask_levels": len(item.snapshot.asks),
            "depth_policy": "causal_anchor_plus_reconciled_deltas",
            "source_event_id": item.source_event_id,
        }
        for token_id, observations in replay.observations_by_token.items()
        for item in observations
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(payload) + b"\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
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


def build_report(
    *,
    capture_root: Path,
    predictor_root: Path,
    capture_config_path: Path,
    preregistration_path: Path,
    history_roots: tuple[Path, ...] = (),
    decision_tau_seconds: int = 60,
    prior_report_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    capture_root = capture_root.resolve()
    predictor_root = predictor_root.resolve()
    history_roots = tuple(root.resolve() for root in history_roots)
    integrity = {
        "market_and_twap_capture": _assert_clean_integrity(capture_root),
        "predictor_capture": _assert_clean_integrity(predictor_root),
        "history_captures": [
            {
                "root": str(root),
                "integrity": _assert_clean_integrity(root),
            }
            for root in history_roots
        ],
    }
    config = json.loads(capture_config_path.read_text(encoding="utf-8"))
    preregistration = json.loads(preregistration_path.read_text(encoding="utf-8"))
    _assert_frozen_decision_tau(preregistration, decision_tau_seconds)
    frozen_strategy = preregistration["frozen_strategy"]
    pair_risk_usdc = Decimal(str(frozen_strategy["pair_risk_usdc"]))
    same_expiry_upper_bound = pair_risk_usdc * Decimal(
        len(frozen_strategy["decision_tau_seconds"])
    )
    max_same_expiry_risk = Decimal(str(frozen_strategy["max_same_expiry_risk_usdc"]))
    max_total_open_risk = Decimal(str(frozen_strategy["max_total_open_risk_usdc"]))
    if (
        same_expiry_upper_bound > max_same_expiry_risk
        or same_expiry_upper_bound > max_total_open_risk
    ):
        raise ValueError("frozen decision surface exceeds portfolio risk caps")
    targets = config["targets"]
    by_horizon = {target["horizon"]: target for target in targets}
    pair = _pair_from_capture_targets(by_horizon)
    target_by_market_id = {str(target["market_id"]): target for target in targets}
    market_ids = {str(target["market_id"]) for target in targets}

    expiry_ms = int(by_horizon["5m"]["closes_at_ms"])
    decision_at_ms = expiry_ms - decision_tau_seconds * 1_000
    verification = _validate_prospective_report_identity(
        capture_config=config,
        preregistration=preregistration,
        capture_root=capture_root,
        predictor_root=predictor_root,
        history_roots=history_roots,
        decision_at_ms=decision_at_ms,
    )

    twap_roots = (*history_roots, capture_root)
    twap_30 = _combined_series(
        twap_roots, topic="crypto_prices_twap_thirty", symbol="btc/usd"
    )
    twap_60 = _combined_series(
        twap_roots, topic="crypto_prices_twap_sixty", symbol="btc/usd"
    )
    # Predictor returns stay inside the current continuous capture.  Joining
    # an older root across a service rollover would turn the rollover outage
    # into an internal gap and reject an otherwise valid current suffix.  The
    # resampler below accepts only an unobserved leading prefix; after its
    # first sample, every gap over five seconds still fails closed.
    predictor = _series(predictor_root, topic="crypto_prices", symbol="btcusdt")
    series_by_horizon = _settlement_series_by_horizon(twap_roots, by_horizon)
    source_topic_by_horizon = {
        horizon: _target_source_topic(target)
        for horizon, target in by_horizon.items()
    }
    boundaries: dict[str, dict[str, Any]] = {}
    mechanically_labelable = 0
    for horizon, target in sorted(by_horizon.items()):
        opening = _exact(series_by_horizon[horizon], target["opens_at_ms"])
        closing = _exact(series_by_horizon[horizon], target["closes_at_ms"])
        outcome = (
            "Up"
            if opening is not None and closing is not None and closing >= opening
            else "Down"
            if opening is not None and closing is not None
            else None
        )
        if outcome is not None:
            mechanically_labelable += 1
        boundaries[horizon] = {
            "market_id": str(target["market_id"]),
            "condition_id": str(target["condition_id"]),
            "slug": str(target["slug"]),
            "opens_at_ms": target["opens_at_ms"],
            "opening_twap": str(opening) if opening is not None else None,
            "closes_at_ms": target["closes_at_ms"],
            "closing_twap": str(closing) if closing is not None else None,
            "mechanical_outcome": outcome,
            "source_topic": source_topic_by_horizon[horizon],
        }

    latest_rules = _latest_rules(capture_root, market_ids)
    resolved_events = _resolved_events(capture_root, market_ids)
    officially_resolved = 0
    rule_summary: dict[str, Any] = {}
    current_taker_cost_evidence_complete = True
    for market_id in sorted(market_ids):
        market = latest_rules.get(market_id)
        target = target_by_market_id[market_id]
        prices: list[str] | None = None
        if market is not None:
            try:
                decoded = json.loads(market.get("outcomePrices", "null"))
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, list):
                prices = [str(item) for item in decoded]
        fee_schedule = market.get("feeSchedule") if market is not None else None
        description = market.get("description") if market is not None else None
        description_sha256 = (
            hashlib.sha256(description.encode("utf-8")).hexdigest()
            if isinstance(description, str)
            else None
        )
        rule_identity_present = isinstance(target.get("rule_hash"), str) and isinstance(
            target.get("rules_text_sha256"), str
        )
        try:
            resolution_source_matches = isinstance(market, Mapping) and (
                canonicalize_resolution_source(str(market.get("resolutionSource")))
                == canonicalize_resolution_source(str(target.get("resolution_source")))
            )
        except (TypeError, ValueError):
            resolution_source_matches = False
        rules_match_capture = (
            market is not None
            and rule_identity_present
            and description_sha256 == target.get("rules_text_sha256")
            and market.get("slug") == target.get("slug")
            and str(market.get("conditionId", "")).lower()
            == str(target.get("condition_id", "")).lower()
            and resolution_source_matches
            and _json_string_list(market.get("outcomes")) == ("Up", "Down")
            and _json_string_list(market.get("clobTokenIds"))
            == (target.get("up_token_id"), target.get("down_token_id"))
        )
        resolution_event = resolved_events.get(market_id)
        resolution_event_valid, resolution_event_reason = _resolution_event_validation(
            resolution_event, target
        )
        officially_resolved += int(resolution_event_valid)
        if (
            not isinstance(fee_schedule, dict)
            or Decimal(str(fee_schedule.get("rate"))) != Decimal("0.07")
            or Decimal(str(fee_schedule.get("exponent"))) != Decimal("1")
            or fee_schedule.get("takerOnly") is not True
            or not resolution_source_matches
        ):
            current_taker_cost_evidence_complete = False
        rule_summary[market_id] = {
            "present": market is not None,
            "closed": market.get("closed") if market is not None else None,
            "accepting_orders": (
                market.get("acceptingOrders") if market is not None else None
            ),
            "outcome_prices": prices,
            "resolution_source": (
                market.get("resolutionSource") if market is not None else None
            ),
            "fee_schedule": fee_schedule,
            "captured_rule_hash": target.get("rule_hash"),
            "rules_text_sha256": description_sha256,
            "rules_match_capture": rules_match_capture,
            "resolution_event": resolution_event,
            "resolution_event_valid": resolution_event_valid,
            "resolution_event_reason": resolution_event_reason,
        }

    boundary_by_market_id = {
        str(target["market_id"]): boundaries[horizon]
        for horizon, target in by_horizon.items()
    }
    resolution_conflicts = [
        market_id
        for market_id in sorted(market_ids)
        if (
            resolved_events.get(market_id) is not None
            and rule_summary[market_id]["resolution_event_valid"] is not True
        )
        or (
            rule_summary[market_id]["resolution_event_valid"] is True
            and boundary_by_market_id[market_id]["mechanical_outcome"] is not None
            and resolved_events[market_id].get("winning_outcome")
            != boundary_by_market_id[market_id]["mechanical_outcome"]
        )
    ]

    clock_policy = preregistration["frozen_strategy"].get("clock_sync")
    capture_clock_sync = config.get("clock_sync")
    receipt_clock_offset_ms = _receipt_clock_offset_ms(capture_root)
    clock_sync_reasons: list[str] = []
    clock_uncertainty_ms = 0
    clock_sync_summary: dict[str, Any]
    if isinstance(clock_policy, Mapping):
        clock_sync_summary = {
            "required": True,
            "evidence": capture_clock_sync,
            "measurement_age_ms": None,
            "valid_for_decision": False,
        }
        if not isinstance(capture_clock_sync, Mapping):
            clock_sync_reasons.append("clock_sync_evidence_missing")
            clock_uncertainty_ms = (
                int(preregistration["frozen_strategy"]["max_clock_drift_ms"]) + 1
            )
        else:
            try:
                offset_seconds = Decimal(str(capture_clock_sync["offset_seconds"]))
                uncertainty_seconds = Decimal(
                    str(capture_clock_sync["uncertainty_seconds"])
                )
                measured_at_raw_ms = int(capture_clock_sync["measured_at_raw_ms"])
                recorded_uncertainty_ms = int(capture_clock_sync["uncertainty_ms"])
                expected_receipt_offset_ms = int(
                    ((offset_seconds + uncertainty_seconds) * 1_000).to_integral_value(
                        rounding=ROUND_CEILING
                    )
                )
                expected_uncertainty_ms = int(
                    (uncertainty_seconds * 1_000).to_integral_value(
                        rounding=ROUND_CEILING
                    )
                )
                measurement_at_corrected_ms = (
                    measured_at_raw_ms + receipt_clock_offset_ms
                )
                measurement_age_ms = decision_at_ms - measurement_at_corrected_ms
                maximum_uncertainty_ms = int(
                    clock_policy["maximum_measurement_uncertainty_ms"]
                )
                maximum_age_ms = (
                    int(clock_policy["maximum_measurement_age_seconds"]) * 1_000
                )
                valid = (
                    capture_clock_sync.get("schema_version") == "btc-twap-clock-sync.v1"
                    and capture_clock_sync.get("source") == clock_policy.get("source")
                    and capture_clock_sync.get("system_clock_mutated") is False
                    and offset_seconds.is_finite()
                    and uncertainty_seconds.is_finite()
                    and uncertainty_seconds >= 0
                    and receipt_clock_offset_ms == expected_receipt_offset_ms
                    and recorded_uncertainty_ms == expected_uncertainty_ms
                    and recorded_uncertainty_ms <= maximum_uncertainty_ms
                    and 0 <= measurement_age_ms <= maximum_age_ms
                )
                clock_uncertainty_ms = recorded_uncertainty_ms
                clock_sync_summary["measurement_age_ms"] = measurement_age_ms
                clock_sync_summary["valid_for_decision"] = valid
                if not valid:
                    clock_sync_reasons.append("clock_sync_evidence_invalid")
            except (KeyError, TypeError, ValueError, ArithmeticError):
                clock_sync_reasons.append("clock_sync_evidence_invalid")
                clock_uncertainty_ms = (
                    int(preregistration["frozen_strategy"]["max_clock_drift_ms"]) + 1
                )
    else:
        clock_sync_summary = {
            "required": False,
            "evidence": capture_clock_sync,
            "measurement_age_ms": None,
            "valid_for_decision": capture_clock_sync is None,
            "legacy_uncorrected_receipts": True,
        }
    asset_ids = tuple(str(item) for item in config.get("asset_ids", ()))
    replay: CausalBookReplay | None = None
    if pair is not None:
        token_contracts = {
            contract.up_token_id: contract
            for contract in (pair.market_5, pair.market_15)
        } | {
            contract.down_token_id: contract
            for contract in (pair.market_5, pair.market_15)
        }
        replay = CausalBookReplay.from_records(
            _records(capture_root, "clob_market_ws"),
            receipt_clock_offset_ms=receipt_clock_offset_ms,
            tokens={
                token_id: BookReplayToken(
                    token_id=token_id,
                    tick_size=contract.tick_size,
                    minimum_order_size=contract.minimum_order_size,
                )
                for token_id, contract in token_contracts.items()
            },
        )
    replay_observations = (
        _replay_observation_rows(replay)
        if replay is not None
        else _book_observations(capture_root, set(asset_ids))
    )
    book_replay_coverage = _book_replay_coverage(
        replay_observations,
        token_ids=asset_ids,
        decision_at_ms=decision_at_ms,
        taker_delay_ms=int(
            preregistration["frozen_strategy"]["taker_delay_ms_when_itode"]
        ),
        max_book_age_ms=int(
            preregistration["frozen_strategy"]["max_book_staleness_ms"]
        ),
    )
    raw_shadow: dict[str, Any]
    distribution = None
    series_5 = series_by_horizon["5m"]
    series_15 = series_by_horizon["15m"]
    strike_5 = _exact(
        series_5,
        int(by_horizon["5m"]["opens_at_ms"]),
        available_by_ms=decision_at_ms,
    )
    strike_15 = _exact(
        series_15,
        int(by_horizon["15m"]["opens_at_ms"]),
        available_by_ms=decision_at_ms,
    )
    state_5 = _latest_before(series_5, decision_at_ms)
    state_15 = _latest_before(series_15, decision_at_ms)
    predictor_before = _resample_one_second(predictor, decision_at_ms=decision_at_ms)
    raw_reasons: list[str] = []
    raw_reasons.extend(clock_sync_reasons)
    if strike_5 is None or strike_15 is None:
        raw_reasons.append("exact_chainlink_opening_boundary_missing")
    if state_5 is None or state_15 is None:
        raw_reasons.append("causal_chainlink_state_missing")
    maximum_twap_age_ms = int(
        preregistration["frozen_strategy"]["max_chainlink_staleness_ms"]
    )
    state_health: dict[str, Any] = {}
    for horizon, state in (("5m", state_5), ("15m", state_15)):
        label = f"{horizon}:{source_topic_by_horizon[horizon]}"
        if state is None:
            state_health[label] = None
            continue
        observation_age_ms = decision_at_ms - state[0]
        receipt_age_ms = decision_at_ms - state[1]
        state_health[label] = {
            "observation_at_ms": state[0],
            "received_at_ms": state[1],
            "observation_age_ms": observation_age_ms,
            "receipt_age_ms": receipt_age_ms,
        }
        if (
            observation_age_ms < 0
            or observation_age_ms > maximum_twap_age_ms
            or receipt_age_ms < 0
            or receipt_age_ms > maximum_twap_age_ms
        ):
            raw_reasons.append(f"{label}_stale_or_future")
    if len(predictor_before) < 60:
        raw_reasons.append("causal_predictor_history_missing")
    if raw_reasons:
        raw_shadow = {
            "available": False,
            "decision_at_ms": decision_at_ms,
            "decision_tau_seconds": decision_tau_seconds,
            "reason_codes": raw_reasons,
            "predictor_points_available": len(predictor_before),
            "chainlink_state_health": state_health,
        }
    else:
        distribution = simulate_ewma_joint_distribution(
            predictor_prices=tuple(predictor_before),
            current_twap_30=state_5[2],
            current_twap_60=state_15[2],
            decision_at_ms=decision_at_ms,
            expiry_ms=expiry_ms,
            strike_5=strike_5,
            strike_15=strike_15,
            n_paths=20_000,
            seed=712,
        )
        raw_shadow = {
            "available": True,
            "actionable": False,
            "decision_at_ms": decision_at_ms,
            "decision_tau_seconds": decision_tau_seconds,
            "reason": "past_only_isotonic_calibration_not_available",
            "predictor_points_available": len(predictor_before),
            "chainlink_state_health": state_health,
            "q_5_raw": str(distribution.q_5_up),
            "q_15_raw": str(distribution.q_15_up),
            "outcome_counts": dict(distribution.outcome_counts),
            "monte_carlo_paths": 20_000,
            "seed": 712,
        }

    development_shadow_cycle: dict[str, Any] = {
        "schema_version": "btc-5m-15m-relative-value-paper-cycle.v1",
        "track": "development_shadow",
        "available": False,
        "reason_codes": [],
        "paper_only": True,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
    }
    qualified_cycle: dict[str, Any] = {
        "schema_version": "btc-5m-15m-relative-value-paper-cycle.v1",
        "track": (
            verification["evidence_track"] if verification is not None else "qualified"
        ),
        "available": False,
        "reason_codes": ["prospective_qualification_not_enabled"],
        "paper_only": True,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
    }
    oos_forecast: dict[str, Any] = {
        "available": False,
        "split": None,
        "reason_codes": ["prospective_qualification_not_enabled"],
    }
    shadow_setup_reasons: list[str] = []
    if pair is None:
        shadow_setup_reasons.append("frozen_execution_contract_missing")
    if replay is None:
        shadow_setup_reasons.append("causal_book_replay_unavailable")
    if distribution is None:
        shadow_setup_reasons.extend(raw_reasons or ("raw_distribution_unavailable",))
    if strike_5 is None or strike_15 is None:
        shadow_setup_reasons.append("exact_chainlink_opening_boundary_missing")
    if state_5 is None or state_15 is None:
        shadow_setup_reasons.append("causal_chainlink_state_missing")
    if any(
        rule.get("rules_match_capture") is not True for rule in rule_summary.values()
    ):
        shadow_setup_reasons.append("exact_rule_binding_incomplete")
    opening_5_event_id = (
        None
        if strike_5 is None
        else _opening_event_id(
            twap_roots,
            topic=source_topic_by_horizon["5m"],
            timestamp_ms=int(by_horizon["5m"]["opens_at_ms"]),
            expected_value=strike_5,
        )
    )
    opening_15_event_id = (
        None
        if strike_15 is None
        else _opening_event_id(
            twap_roots,
            topic=source_topic_by_horizon["15m"],
            timestamp_ms=int(by_horizon["15m"]["opens_at_ms"]),
            expected_value=strike_15,
        )
    )
    if strike_5 is not None and opening_5_event_id is None:
        shadow_setup_reasons.append("opening_5_source_event_missing")
    if strike_15 is not None and opening_15_event_id is None:
        shadow_setup_reasons.append("opening_15_source_event_missing")
    signal_observations: Mapping[str, Any] = {}
    strategy_config: StrategyConfig | None = None
    clock_drift_ms = clock_uncertainty_ms
    settlement_state: PairSettlementState | None = None
    if not shadow_setup_reasons:
        assert pair is not None
        assert replay is not None
        assert distribution is not None
        assert strike_5 is not None and strike_15 is not None
        assert state_5 is not None and state_15 is not None
        assert opening_5_event_id is not None and opening_15_event_id is not None
        strategy_config = _strategy_config(preregistration)
        signal_observations = replay.signal_books(
            token_ids=asset_ids,
            decision_at_ms=decision_at_ms,
            maximum_age_ms=strategy_config.maximum_book_staleness_ms,
        )
        clock_drift_ms = (
            clock_uncertainty_ms
            if isinstance(clock_policy, Mapping)
            else max(
                (
                    abs(item.received_at_ms - item.source_at_ms)
                    for item in signal_observations.values()
                ),
                default=0,
            )
        )
        settlement_state = PairSettlementState(
            market_5_rule_hash=pair.market_5.rule_hash,
            market_15_rule_hash=pair.market_15.rule_hash,
            market_5_open_timestamp_ms=pair.market_5.opens_at_ms,
            market_15_open_timestamp_ms=pair.market_15.opens_at_ms,
            strike_5=strike_5,
            strike_15=strike_15,
            opening_5_source_event_id=opening_5_event_id,
            opening_15_source_event_id=opening_15_event_id,
        )
        cycle = evaluate_shadow_paper_cycle(
            pair=pair,
            settlement_state=settlement_state,
            distribution=distribution,
            replay=replay,
            health=DataHealth(
                decision_at_ms=decision_at_ms,
                # DataHealth keeps legacy field names for schema compatibility;
                # the values are the frozen 5m and 15m settlement sources.
                twap_30_observed_at_ms=state_5[0],
                twap_60_observed_at_ms=state_15[0],
                twap_30_received_at_ms=state_5[1],
                twap_60_received_at_ms=state_15[1],
                absolute_clock_drift_ms=clock_drift_ms,
                calibration_5=None,
                calibration_15=None,
            ),
            config=strategy_config,
            market_5_up=(
                boundaries["5m"]["mechanical_outcome"] == "Up"
                if boundaries["5m"]["mechanical_outcome"] is not None
                else None
            ),
            market_15_up=(
                boundaries["15m"]["mechanical_outcome"] == "Up"
                if boundaries["15m"]["mechanical_outcome"] is not None
                else None
            ),
            initial_cash=Decimal(
                str(preregistration["frozen_strategy"].get("paper_bankroll", "10000"))
            ),
            max_leg_delay_ms=int(
                preregistration["frozen_strategy"]["max_leg_delay_ms"]
            ),
        )
        development_shadow_cycle = {**cycle.to_document(), "available": True}
    else:
        development_shadow_cycle["reason_codes"] = list(
            dict.fromkeys(shadow_setup_reasons)
        )

    preregistration_sha256 = _sha256(preregistration_path)
    evidence_track = (
        str(verification["evidence_track"]) if verification is not None else "qualified"
    )
    if verification is not None and not shadow_setup_reasons:
        assert pair is not None
        assert replay is not None
        assert distribution is not None
        assert strategy_config is not None
        assert settlement_state is not None
        assert state_5 is not None and state_15 is not None
        fold = build_daily_qualification_fold(
            datetime.fromtimestamp(decision_at_ms / 1_000, tz=timezone.utc)
            .date()
            .isoformat()
        )
        try:
            fitted = fit_qualification_calibrators_from_reports(
                _load_prior_qualification_reports(
                    prior_report_paths,
                    decision_tau_seconds=decision_tau_seconds,
                ),
                fold=fold,
                preregistration_sha256=preregistration_sha256,
                evidence_track=evidence_track,
                decision_tau_seconds=decision_tau_seconds,
                minimum_unique_expiry_clusters=int(
                    preregistration["frozen_strategy"].get(
                        "development_calibration_minimum_points_per_horizon",
                        20,
                    )
                ),
                settlement_regime=strategy_config.settlement_regime,
            )
        except QualificationInsufficientData as exc:
            qualified_cycle["reason_codes"] = [
                "past_only_calibration_insufficient",
                str(exc),
            ]
            oos_forecast["reason_codes"] = ["past_only_calibration_insufficient"]
        else:
            if decision_at_ms < fold.fit_at_ms:
                raise ValueError(
                    "qualified test decision predates the frozen daily fit"
                )
            qualified_evaluation = evaluate_qualified_paper_cycle(
                pair=pair,
                settlement_state=settlement_state,
                distribution=distribution,
                replay=replay,
                health=DataHealth(
                    decision_at_ms=decision_at_ms,
                    twap_30_observed_at_ms=state_5[0],
                    twap_60_observed_at_ms=state_15[0],
                    twap_30_received_at_ms=state_5[1],
                    twap_60_received_at_ms=state_15[1],
                    absolute_clock_drift_ms=clock_drift_ms,
                    calibration_5=fitted.artifacts["5m"],
                    calibration_15=fitted.artifacts["15m"],
                ),
                config=strategy_config,
                market_5_up=(
                    boundaries["5m"]["mechanical_outcome"] == "Up"
                    if boundaries["5m"]["mechanical_outcome"] is not None
                    else None
                ),
                market_15_up=(
                    boundaries["15m"]["mechanical_outcome"] == "Up"
                    if boundaries["15m"]["mechanical_outcome"] is not None
                    else None
                ),
                initial_cash=Decimal(
                    str(
                        preregistration["frozen_strategy"].get(
                            "paper_bankroll", "10000"
                        )
                    )
                ),
                max_leg_delay_ms=int(
                    preregistration["frozen_strategy"]["max_leg_delay_ms"]
                ),
            )
            qualified_cycle = {
                **qualified_evaluation.to_document(),
                "track": evidence_track,
                "available": True,
                "calibration_provenance": fitted.provenance.to_document(),
                "calibration_provenance_sha256": fitted.provenance.artifact_hash,
            }
            event_id, expiry_cluster_id = _qualification_event_identity(
                evidence_track=evidence_track,
                expiry_ms=expiry_ms,
                decision_tau_seconds=decision_tau_seconds,
            )
            market_5_probability = _normalized_binary_ask_probability(
                signal_observations,
                up_token_id=pair.market_5.up_token_id,
                down_token_id=pair.market_5.down_token_id,
            )
            market_15_probability = _normalized_binary_ask_probability(
                signal_observations,
                up_token_id=pair.market_15.up_token_id,
                down_token_id=pair.market_15.down_token_id,
            )
            if market_5_probability is not None and market_15_probability is not None:
                oos_forecast = {
                    "available": True,
                    "split": "test",
                    "event_cluster_id": expiry_cluster_id,
                    "event_id": event_id,
                    "decision_tau_seconds": decision_tau_seconds,
                    "model_probability_up": {
                        "5m": str(
                            fitted.artifacts["5m"].transform(distribution.q_5_up)
                        ),
                        "15m": str(
                            fitted.artifacts["15m"].transform(distribution.q_15_up)
                        ),
                    },
                    "market_probability_up": {
                        "5m": str(market_5_probability),
                        "15m": str(market_15_probability),
                    },
                    "actual_up": {
                        "5m": boundaries["5m"]["mechanical_outcome"] == "Up",
                        "15m": boundaries["15m"]["mechanical_outcome"] == "Up",
                    },
                    "calibration_provenance": fitted.provenance.to_document(),
                    "calibration_provenance_sha256": fitted.provenance.artifact_hash,
                }

    decision_reasons = list(dict.fromkeys(raw_reasons))
    if any(
        rule.get("rules_match_capture") is not True for rule in rule_summary.values()
    ):
        decision_reasons.append("exact_rule_binding_incomplete")
    if not book_replay_coverage["complete_four_token_signal_surface"]:
        decision_reasons.append("complete_four_outcome_books_missing")
    if not book_replay_coverage["complete_four_token_delayed_execution_surface"]:
        decision_reasons.append("delayed_execution_book_surface_missing")
    qualified_decision_document = qualified_cycle.get("decision")
    if isinstance(qualified_decision_document, Mapping):
        decision_reasons.extend(
            str(reason)
            for reason in qualified_decision_document.get("reason_codes", ())
        )
    else:
        decision_reasons.append("past_only_isotonic_calibration_not_available")
    paper_decision = {
        "schema_version": "btc-5m-15m-relative-value-paper-decision.v1",
        "decision_id": hashlib.sha256(
            canonical_json_bytes(
                {
                    "capture_config_sha256": _sha256(capture_config_path),
                    "decision_at_ms": decision_at_ms,
                    "decision_tau_seconds": decision_tau_seconds,
                    "strategy_spec_sha256": preregistration["strategy_spec"]["sha256"],
                }
            )
        ).hexdigest(),
        "evaluated": True,
        "action": (
            qualified_decision_document.get("action", "no_trade")
            if isinstance(qualified_decision_document, Mapping)
            else "no_trade"
        ),
        "reason_codes": list(dict.fromkeys(decision_reasons)),
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
    }

    complete_market_capture = (
        mechanically_labelable
        if len(predictor_before) >= 60
        and book_replay_coverage["complete_four_token_signal_surface"]
        and book_replay_coverage["complete_four_token_delayed_execution_surface"]
        and all(
            rule.get("rules_match_capture") is True for rule in rule_summary.values()
        )
        and clock_sync_summary.get("valid_for_decision") is True
        else 0
    )
    evidence = ValidationEvidence(
        resolved_current_regime_markets=officially_resolved,
        expected_current_regime_markets=2,
        markets_with_complete_capture=complete_market_capture,
        unknown_resolution_mapping_count=sum(
            boundary["opening_twap"] is None or boundary["closing_twap"] is None
            for boundary in boundaries.values()
        )
        + len(resolution_conflicts)
        # A label cannot qualify if the exact target rules were not versioned
        # at discovery or the latest captured text/mapping changed afterward.
        + sum(
            rule.get("rules_match_capture") is not True
            for rule in rule_summary.values()
        ),
        explainable_simulated_trades=0,
        explainable_fills=0,
        explainable_net_pnls=(),
        chronological_oos_complete=False,
        # Fee metadata alone is not a complete cost model.  This report does
        # not yet replay delay, depth, partial fills, and legging end-to-end.
        complete_taker_cost_model=False,
        delay_depth_and_legging_replay_complete=False,
        bootstrap_net_pnl_lower_95=None,
        oos_brier_5=None,
        oos_brier_15=None,
        market_brier_5=None,
        market_brier_15=None,
        oos_expected_calibration_error_5=None,
        oos_expected_calibration_error_15=None,
        maximum_single_event_pnl_share=None,
        direction_exposure_below_single_leg=None,
        signal_strength_net_ev_monotonic=None,
    )
    validation = evaluate_validation(evidence)
    candidate_hypotheses = _candidate_hypotheses(
        cycle=development_shadow_cycle,
        signal_observations=signal_observations,
        pair=pair,
        boundaries=boundaries,
    )
    shadow_decision_document = development_shadow_cycle.get("decision")
    shadow_execution_document = development_shadow_cycle.get("execution")
    shadow_settlement_document = development_shadow_cycle.get("settlement")
    shadow_decision_evaluated = isinstance(shadow_decision_document, Mapping)
    shadow_execution_diagnostics = (
        shadow_execution_document.get("diagnostics")
        if isinstance(shadow_execution_document, Mapping)
        and isinstance(shadow_execution_document.get("diagnostics"), Mapping)
        else None
    )
    shadow_trade_count = int(
        isinstance(shadow_execution_diagnostics, Mapping)
        and shadow_execution_diagnostics.get("economic_attempt") is True
    )
    shadow_fill_count = 0
    if isinstance(shadow_execution_document, Mapping):
        for leg_name in ("first_leg", "second_leg", "unwind_leg"):
            leg = shadow_execution_document.get(leg_name)
            fills = leg.get("fills") if isinstance(leg, Mapping) else None
            if isinstance(fills, list):
                shadow_fill_count += len(fills)
    shadow_net_pnl = (
        shadow_settlement_document.get("net_pnl")
        if isinstance(shadow_settlement_document, Mapping)
        and shadow_settlement_document.get("explainable") is True
        else None
    )
    event_id, expiry_cluster_id = _qualification_event_identity(
        evidence_track=evidence_track,
        expiry_ms=expiry_ms,
        decision_tau_seconds=decision_tau_seconds,
    )
    closing_observations = {
        "5m": _exact_observation(series_5, int(by_horizon["5m"]["closes_at_ms"])),
        "15m": _exact_observation(series_15, int(by_horizon["15m"]["closes_at_ms"])),
    }
    resolution_received_at_ms = tuple(
        int(rule["resolution_event"]["received_at_ms"])
        for rule in rule_summary.values()
        if rule.get("resolution_event_valid") is True
        and isinstance(rule.get("resolution_event"), Mapping)
        and isinstance(rule["resolution_event"].get("received_at_ms"), int)
    )
    label_available_at_ms = (
        max(
            *(
                observation[1]
                for observation in closing_observations.values()
                if observation
            ),
            *resolution_received_at_ms,
        )
        if all(observation is not None for observation in closing_observations.values())
        and len(resolution_received_at_ms) == 2
        else None
    )
    calibration_observation_available = (
        verification is not None
        and distribution is not None
        and label_available_at_ms is not None
        and all(
            boundaries[horizon]["mechanical_outcome"] in {"Up", "Down"}
            for horizon in ("5m", "15m")
        )
        and all(
            rule.get("rules_match_capture") is True
            and rule.get("resolution_event_valid") is True
            for rule in rule_summary.values()
        )
        and not resolution_conflicts
    )
    calibration_observation: dict[str, Any] = {
        "available": calibration_observation_available,
        "reason_codes": (
            []
            if calibration_observation_available
            else ["verified_mechanical_label_not_available"]
        ),
    }
    if calibration_observation_available:
        assert distribution is not None
        assert label_available_at_ms is not None
        calibration_observation.update(
            {
                "event_id": event_id,
                "expiry_cluster_id": expiry_cluster_id,
                "raw_probabilities": {
                    "5m": str(distribution.q_5_up),
                    "15m": str(distribution.q_15_up),
                },
                "mechanical_label": {
                    "5m": boundaries["5m"]["mechanical_outcome"] == "Up",
                    "15m": boundaries["15m"]["mechanical_outcome"] == "Up",
                },
                "local_label_available_at_ms": label_available_at_ms,
            }
        )
    oos_forecast = _bind_verified_oos_label(
        oos_forecast,
        label_available=calibration_observation_available,
        label_available_at_ms=label_available_at_ms,
    )
    qualified_settlement = qualified_cycle.get("settlement")
    if isinstance(qualified_settlement, dict):
        qualified_settlement.update(
            {
                "event_id": event_id,
                "expiry_cluster_id": expiry_cluster_id,
                "mechanical_label": (
                    {
                        "5m": boundaries["5m"]["mechanical_outcome"] == "Up",
                        "15m": boundaries["15m"]["mechanical_outcome"] == "Up",
                    }
                    if calibration_observation_available
                    else None
                ),
                "local_label_available_at_ms": label_available_at_ms,
            }
        )
    qualified_execution = qualified_cycle.get("execution")
    qualified_diagnostics = (
        qualified_execution.get("diagnostics")
        if isinstance(qualified_execution, Mapping)
        and isinstance(qualified_execution.get("diagnostics"), Mapping)
        else None
    )
    qualified_economic_attempt = bool(
        isinstance(qualified_diagnostics, Mapping)
        and qualified_diagnostics.get("economic_attempt") is True
    )
    qualified_fill_count = 0
    if isinstance(qualified_execution, Mapping):
        for leg_name in ("first_leg", "second_leg", "unwind_leg"):
            leg = qualified_execution.get(leg_name)
            fills = leg.get("fills") if isinstance(leg, Mapping) else None
            if isinstance(fills, list):
                qualified_fill_count += len(fills)
    qualified_explainable_pnl = (
        qualified_settlement.get("net_pnl")
        if qualified_economic_attempt
        and isinstance(qualified_settlement, Mapping)
        and qualified_settlement.get("explainable") is True
        else None
    )
    report = {
        "schema_version": (
            "btc-5m-15m-relative-value-pilot-report.v2"
            if verification is not None
            else "btc-5m-15m-relative-value-pilot-report.v1"
        ),
        "verified_report_v2": verification is not None,
        "verification": verification,
        "capture_started_at": _epoch_ms_to_utc_iso(
            int(config.get("capture_started_at_ms", 0))
        ),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
        "classification": validation.status.value,
        "qualified_net_pnl": (
            str(validation.qualified_net_pnl)
            if validation.qualified_net_pnl is not None
            else None
        ),
        "reason_codes": list(validation.reason_codes),
        "inputs": {
            "capture_root": str(capture_root),
            "predictor_root": str(predictor_root),
            "history_roots": [str(root) for root in history_roots],
            "capture_config": str(capture_config_path.resolve()),
            "capture_config_sha256": _sha256(capture_config_path),
            "preregistration": str(preregistration_path.resolve()),
            "preregistration_sha256": _sha256(preregistration_path),
            "strategy_spec_sha256": preregistration["strategy_spec"]["sha256"],
            "decision_tau_seconds": decision_tau_seconds,
            "decision_at_ms": decision_at_ms,
            "capture_started_at_ms": config.get("capture_started_at_ms"),
            "evidence_track": config.get("evidence_track_id"),
        },
        "integrity": integrity,
        "observed": {
            "twap_30_unique_observations": len(twap_30),
            "twap_60_unique_observations": len(twap_60),
            "settlement_sources": {
                horizon: {
                    "source_topic": source_topic_by_horizon[horizon],
                    "unique_observations": len(series_by_horizon[horizon]),
                }
                for horizon in ("5m", "15m")
            },
            "binance_btc_unique_observations": len(predictor),
            "predictor_one_second_samples": len(predictor_before),
            "clob_event_counts": _event_counts(capture_root, "clob_market_ws"),
            "rtds_event_counts": _event_counts(capture_root, "rtds_ws"),
            "capture_runtime": _capture_runtime_health(capture_root),
            "clock_sync": clock_sync_summary,
            "book_replay_coverage": book_replay_coverage,
            "mechanically_labelable_markets": mechanically_labelable,
            "markets_with_complete_decision_capture": complete_market_capture,
            "officially_resolved_markets": officially_resolved,
            "resolution_conflicts": resolution_conflicts,
            "boundaries": boundaries,
            "latest_rules": rule_summary,
        },
        "raw_shadow_model": raw_shadow,
        "candidate_hypotheses": candidate_hypotheses,
        "paper_decision": paper_decision,
        "development_shadow_cycle": development_shadow_cycle,
        "qualified_cycle": qualified_cycle,
        "calibration_observation": calibration_observation,
        "oos_forecast": oos_forecast,
        "qualified_evidence": {
            "economic_attempt": qualified_economic_attempt,
            "explainable_fills": qualified_fill_count,
            "explainable_net_pnl": qualified_explainable_pnl,
            "complete_taker_cost_model": (
                qualified_economic_attempt
                and qualified_explainable_pnl is not None
                and current_taker_cost_evidence_complete
            ),
            "delay_depth_and_legging_replay_complete": (
                qualified_economic_attempt
                and qualified_explainable_pnl is not None
                and book_replay_coverage["complete_four_token_signal_surface"]
                and book_replay_coverage[
                    "complete_four_token_delayed_execution_surface"
                ]
            ),
            "event_id": event_id,
            "expiry_cluster_id": expiry_cluster_id,
            "local_label_available_at_ms": label_available_at_ms,
        },
        "risk_controls": {
            "pair_risk_usdc": str(pair_risk_usdc),
            "same_expiry_decision_count_upper_bound": len(
                frozen_strategy["decision_tau_seconds"]
            ),
            "same_expiry_risk_upper_bound_usdc": str(same_expiry_upper_bound),
            "max_same_expiry_risk_usdc": str(max_same_expiry_risk),
            "max_total_open_risk_usdc": str(max_total_open_risk),
            "within_frozen_caps": True,
        },
        "economic_evidence": {
            "explainable_simulated_trades": 0,
            "explainable_fills": 0,
            "observed_explainable_net_pnl": None,
            "current_taker_fee_metadata_complete": (
                current_taker_cost_evidence_complete
            ),
            "complete_taker_cost_model": False,
            "delay_depth_and_legging_replay_complete": False,
            "replay_input_surface_complete": (
                book_replay_coverage["complete_four_token_signal_surface"]
                and book_replay_coverage[
                    "complete_four_token_delayed_execution_surface"
                ]
            ),
            "development_shadow_decisions": int(shadow_decision_evaluated),
            "development_shadow_trades": shadow_trade_count,
            "development_shadow_fills": shadow_fill_count,
            "development_shadow_net_pnl": shadow_net_pnl,
            "development_shadow_execution_diagnostics": shadow_execution_diagnostics,
            "development_shadow_complete_taker_cost_model": (
                shadow_trade_count == 1
                and isinstance(shadow_settlement_document, Mapping)
                and shadow_settlement_document.get("explainable") is True
            ),
            "development_shadow_is_qualified_evidence": False,
            "note": "missing economic evidence is null, never zero PnL",
        },
        "promotion_progress": {
            "resolved_markets": officially_resolved,
            "resolved_markets_required": validation.minimum_resolved_markets,
            "simulated_trades": 0,
            "simulated_trades_required": validation.minimum_simulated_trades,
            "explainable_fills": 0,
            "explainable_fills_required": validation.minimum_explainable_fills,
        },
    }
    report["report_sha256"] = hashlib.sha256(canonical_json_bytes(report)).hexdigest()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-root", required=True, type=Path)
    parser.add_argument("--predictor-root", required=True, type=Path)
    parser.add_argument("--history-root", action="append", default=[], type=Path)
    parser.add_argument("--capture-config", required=True, type=Path)
    parser.add_argument("--preregistration", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--prior-report", action="append", default=[], type=Path)
    parser.add_argument("--decision-tau-seconds", type=int, default=60)
    args = parser.parse_args()
    report = build_report(
        capture_root=args.capture_root,
        predictor_root=args.predictor_root,
        capture_config_path=args.capture_config,
        preregistration_path=args.preregistration,
        history_roots=tuple(args.history_root),
        decision_tau_seconds=args.decision_tau_seconds,
        prior_report_paths=tuple(args.prior_report),
    )
    _atomic_json(args.output.resolve(), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
