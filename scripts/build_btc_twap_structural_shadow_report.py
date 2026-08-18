"""Build an offline BTC TWAP structural-shadow report from one strict JSON file.

Input schema `btc_twap_structural_shadow_input.v1`

Top-level object keys, exactly:
- `schema_version`: must be `btc_twap_structural_shadow_input.v1`
- `neutral_disposition`: one of `passive_wait`, `taker_hedge`, `fak_unwind`
- `rolling_window_size`: positive integer
- `minimum_expiries`: integer no lower than the registered 200-expiry gate
- `attempts`: non-empty array

Each attempt object keys, exactly:
- `attempt_id`: journal-bound canonical common-expiry id
- `capture_root`: finalized primary pair-capture directory bound by admission
- `history_capture_roots`: optional finalized overlapping captures used only to
  prove the complete official 60s TWAP window
- `pair`: object with exact keys `market_5`, `market_15`
- `settlement_state`: rule-hash-bound opening TWAP strikes and source receipts
- `action`: one of `long_15_up_long_5_down`, `long_5_up_long_15_down`
- `quantity`: exact decimal encoded as a JSON string or integer
- `submitted_at_ms`: non-negative integer
- `max_book_age_ms`: non-negative integer
- `initial_cash`: optional exact decimal encoded as a JSON string or integer
- `actual_outcome`: object with exact keys `actual_5_up`, `actual_15_up`
- `books`: object keyed by token id, covering all four tokens
- `public_events`: array of strict `book` or `trade` event objects
- `hedge_books`: optional object keyed by token id
- `unwind_books`: optional object keyed by token id

Contract objects mirror `TwapMarketContract` exactly and require a nested
`fee_schedule` object with exact keys `rate`, `exponent`, `taker_only`, plus
optional `exemption_reason` and `exemption_source_ref`.

Order-book and depth-book decimal fields must be JSON strings or integers.
Floats are rejected to preserve exact Decimal semantics.
The builder re-derives pair identity, rules, strikes, terminal outcome,
decision books, complete public event flow, and source hashes from finalized
CaptureStore manifests. The required journal and preregistration CLI arguments
are verified directly; receipt validity, cohort completeness, and evidence
hashes are never accepted as caller-supplied booleans.

This script performs no network, auth, signing, or order-submission calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.build_btc_twap_relative_value_pilot_report import (
    _assert_clean_integrity,
    _combined_series,
    _exact_observation,
    _iso_to_epoch_ms,
    _receipt_clock_offset_ms,
    _records,
)
from scripts.build_btc_twap_relative_value_v07_counterfactual import (
    _tree_identity,
    _validate_pair_targets,
    _validate_rules_and_fees,
    _validated_resolution_evidence,
)
from src.edge_lab.btc_twap_locked_shadow_v08 import audit_rolling_shadow
from src.edge_lab.btc_twap_relative_value import (
    OrderBookSnapshot,
    PairSettlementState,
    SameExpiryPair,
    TwapMarketContract,
)
from src.edge_lab.btc_twap_relative_value_replay import (
    BookReplayToken,
    CausalBookReplay,
)
from src.edge_lab.btc_twap_relative_value_v07 import (
    canonical_event_cluster_id,
    canonical_expiry_cluster_id,
)
from src.edge_lab.btc_twap_structural_shadow import (
    STRUCTURAL_SHADOW_REPORT_SCHEMA,
    LockedZeroCohort,
    StructuralCaptureVerification,
    _bind_rolling_audit,
    _verified_finalized_capture_bundle,
    build_structural_double_maker_plan,
    build_structural_shadow_report,
)
from src.edge_lab.capture_cli import load_capture_config
from src.edge_lab.data_api_trade_identity import data_api_trade_event_key
from src.edge_lab.data_manifest import _fsync_directory, _is_link_like
from src.edge_lab.data_store import (
    canonical_json_bytes,
    canonical_record_id_from_json,
)
from src.edge_lab.execution import (
    BookEvent,
    DepthBook,
    ExecutionFeeSchedule,
    OrderSide,
    TradeEvent,
)

INPUT_SCHEMA = "btc_twap_structural_shadow_input.v1"
LOCKED_ACTION_SCHEMA = "btc_twap_structural_locked_action.v1"
SOURCE_EVIDENCE_SCHEMA = "btc_twap_structural_source_evidence.v1"


def _locked_action_document(attempt: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "attempt_id",
        "capture_root",
        "history_capture_roots",
        "pair",
        "settlement_state",
        "action",
        "quantity",
        "submitted_at_ms",
        "max_book_age_ms",
        "initial_cash",
        "books",
    )
    return {
        "schema_version": LOCKED_ACTION_SCHEMA,
        **{key: attempt[key] for key in keys if key in attempt},
    }


def _no_trade_locked_action_document(
    *,
    attempt_id: str,
    decision_at_ms: int,
) -> dict[str, Any]:
    return {
        "schema_version": LOCKED_ACTION_SCHEMA,
        "attempt_id": attempt_id,
        "action": "no_trade",
        "decision_at_ms": decision_at_ms,
    }


def _source_evidence_document(
    attempt: Mapping[str, Any],
    verification: StructuralCaptureVerification,
) -> dict[str, Any]:
    """Return the post-settlement evidence envelope bound into the journal.

    The capture verifier proves every caller-supplied market datum below against
    the finalized recorder tree before this envelope is hashed.  Hashing only
    the input JSON would let a syntactically valid, invented replay masquerade
    as source evidence.
    """

    evidence_keys = (
        "attempt_id",
        "history_capture_roots",
        "pair",
        "settlement_state",
        "actual_outcome",
        "books",
        "public_events",
        "hedge_books",
        "unwind_books",
    )
    return {
        "schema_version": SOURCE_EVIDENCE_SCHEMA,
        "capture_verification": verification.to_document(),
        **{key: attempt[key] for key in evidence_keys if key in attempt},
    }


def _zero_cohort_source_evidence_document(
    cohort: Mapping[str, Any],
    verification: StructuralCaptureVerification,
    *,
    dirty_reason_code: str | None,
) -> dict[str, Any]:
    evidence_keys = (
        "attempt_id",
        "capture_root",
        "history_capture_roots",
        "outcome",
        "decision_at_ms",
    )
    return {
        "schema_version": SOURCE_EVIDENCE_SCHEMA,
        "capture_verification": verification.to_document(),
        "dirty_reason_code": dirty_reason_code,
        **{key: cohort[key] for key in evidence_keys if key in cohort},
    }


def _mapping(value: Any, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    *,
    name: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional_keys = optional or set()
    actual = set(value)
    missing = sorted(required - actual)
    extra = sorted(actual - required - optional_keys)
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if extra:
            parts.append("unexpected " + ", ".join(extra))
        raise ValueError(f"{name} schema mismatch: {'; '.join(parts)}")


def _text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _bool(value: Any, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
    return value


def _integer(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _decimal(value: Any, *, name: str):
    from src.edge_lab.btc_twap_structural_shadow import _decimal as parse_decimal

    return parse_decimal(value, name=name)


def _fee_schedule(payload: Mapping[str, Any]) -> ExecutionFeeSchedule:
    _exact_keys(
        payload,
        name="fee_schedule",
        required={"rate", "exponent", "taker_only"},
        optional={"exemption_reason", "exemption_source_ref"},
    )
    return ExecutionFeeSchedule(
        rate=_decimal(payload["rate"], name="fee_schedule.rate"),
        exponent=_decimal(payload["exponent"], name="fee_schedule.exponent"),
        taker_only=_bool(payload["taker_only"], name="fee_schedule.taker_only"),
        exemption_reason=payload.get("exemption_reason"),
        exemption_source_ref=payload.get("exemption_source_ref"),
    )


def _contract(payload: Mapping[str, Any], *, name: str) -> TwapMarketContract:
    _exact_keys(
        payload,
        name=name,
        required={
            "horizon",
            "slug",
            "market_id",
            "condition_id",
            "up_token_id",
            "down_token_id",
            "opens_at_ms",
            "closes_at_ms",
            "twap_window_seconds",
            "source_topic",
            "resolution_source",
            "tick_size",
            "minimum_order_size",
            "fee_schedule",
            "taker_delay_ms",
            "accepting_orders",
            "rule_hash",
            "settlement_regime",
        },
    )
    return TwapMarketContract(
        horizon=_text(payload["horizon"], name=f"{name}.horizon"),
        slug=_text(payload["slug"], name=f"{name}.slug"),
        market_id=_text(payload["market_id"], name=f"{name}.market_id"),
        condition_id=_text(payload["condition_id"], name=f"{name}.condition_id"),
        up_token_id=_text(payload["up_token_id"], name=f"{name}.up_token_id"),
        down_token_id=_text(payload["down_token_id"], name=f"{name}.down_token_id"),
        opens_at_ms=_integer(payload["opens_at_ms"], name=f"{name}.opens_at_ms"),
        closes_at_ms=_integer(payload["closes_at_ms"], name=f"{name}.closes_at_ms"),
        twap_window_seconds=_integer(
            payload["twap_window_seconds"], name=f"{name}.twap_window_seconds"
        ),
        source_topic=_text(payload["source_topic"], name=f"{name}.source_topic"),
        resolution_source=_text(
            payload["resolution_source"], name=f"{name}.resolution_source"
        ),
        tick_size=_decimal(payload["tick_size"], name=f"{name}.tick_size"),
        minimum_order_size=_decimal(
            payload["minimum_order_size"], name=f"{name}.minimum_order_size"
        ),
        fee_schedule=_fee_schedule(
            _mapping(payload["fee_schedule"], name=f"{name}.fee_schedule")
        ),
        taker_delay_ms=_integer(
            payload["taker_delay_ms"], name=f"{name}.taker_delay_ms"
        ),
        accepting_orders=_bool(
            payload["accepting_orders"], name=f"{name}.accepting_orders"
        ),
        rule_hash=_text(payload["rule_hash"], name=f"{name}.rule_hash"),
        settlement_regime=_text(
            payload["settlement_regime"], name=f"{name}.settlement_regime"
        ),
    )


def _pair(payload: Mapping[str, Any]) -> SameExpiryPair:
    _exact_keys(payload, name="pair", required={"market_5", "market_15"})
    return SameExpiryPair.from_contracts(
        _contract(
            _mapping(payload["market_5"], name="pair.market_5"), name="pair.market_5"
        ),
        _contract(
            _mapping(payload["market_15"], name="pair.market_15"), name="pair.market_15"
        ),
    )


def _settlement_state(
    payload: Mapping[str, Any],
    *,
    pair: SameExpiryPair,
    name: str,
) -> PairSettlementState:
    _exact_keys(
        payload,
        name=name,
        required={
            "market_5_rule_hash",
            "market_15_rule_hash",
            "market_5_open_timestamp_ms",
            "market_15_open_timestamp_ms",
            "strike_5",
            "strike_15",
            "opening_5_source_event_id",
            "opening_15_source_event_id",
        },
    )
    state = PairSettlementState(
        market_5_rule_hash=_text(
            payload["market_5_rule_hash"], name=f"{name}.market_5_rule_hash"
        ),
        market_15_rule_hash=_text(
            payload["market_15_rule_hash"], name=f"{name}.market_15_rule_hash"
        ),
        market_5_open_timestamp_ms=_integer(
            payload["market_5_open_timestamp_ms"],
            name=f"{name}.market_5_open_timestamp_ms",
        ),
        market_15_open_timestamp_ms=_integer(
            payload["market_15_open_timestamp_ms"],
            name=f"{name}.market_15_open_timestamp_ms",
        ),
        strike_5=_decimal(payload["strike_5"], name=f"{name}.strike_5"),
        strike_15=_decimal(payload["strike_15"], name=f"{name}.strike_15"),
        opening_5_source_event_id=_text(
            payload["opening_5_source_event_id"],
            name=f"{name}.opening_5_source_event_id",
        ),
        opening_15_source_event_id=_text(
            payload["opening_15_source_event_id"],
            name=f"{name}.opening_15_source_event_id",
        ),
    )
    if (
        state.market_5_rule_hash != pair.market_5.rule_hash
        or state.market_15_rule_hash != pair.market_15.rule_hash
    ):
        raise ValueError(f"{name} rule hashes do not match the captured pair")
    return state


def _snapshot(payload: Mapping[str, Any], *, token_id: str) -> OrderBookSnapshot:
    _exact_keys(
        payload,
        name=f"books[{token_id}]",
        required={"bids", "asks", "timestamp_ms", "tick_size", "minimum_order_size"},
    )
    bids = _sequence_of_pairs(payload["bids"], name=f"books[{token_id}].bids")
    asks = _sequence_of_pairs(payload["asks"], name=f"books[{token_id}].asks")
    return OrderBookSnapshot.from_tuples(
        token_id,
        bids=bids,
        asks=asks,
        timestamp_ms=_integer(
            payload["timestamp_ms"], name=f"books[{token_id}].timestamp_ms"
        ),
        tick_size=_decimal(payload["tick_size"], name=f"books[{token_id}].tick_size"),
        minimum_order_size=_decimal(
            payload["minimum_order_size"], name=f"books[{token_id}].minimum_order_size"
        ),
    )


def _depth_book(payload: Mapping[str, Any], *, token_id: str, name: str) -> DepthBook:
    _exact_keys(
        payload,
        name=name,
        required={"bids", "asks", "tick_size"},
        optional={"min_order_size", "timestamp_ms"},
    )
    return DepthBook.from_tuples(
        token_id,
        bids=_sequence_of_pairs(payload["bids"], name=f"{name}.bids"),
        asks=_sequence_of_pairs(payload["asks"], name=f"{name}.asks"),
        tick_size=_decimal(payload["tick_size"], name=f"{name}.tick_size"),
        min_order_size=(
            None
            if "min_order_size" not in payload
            else _decimal(payload["min_order_size"], name=f"{name}.min_order_size")
        ),
        timestamp_ms=(
            None
            if "timestamp_ms" not in payload
            else _integer(payload["timestamp_ms"], name=f"{name}.timestamp_ms")
        ),
    )


def _sequence_of_pairs(value: Any, *, name: str) -> tuple[tuple[Any, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be an array of [price, size]")
    pairs: list[tuple[Any, Any]] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes, bytearray))
            or len(item) != 2
        ):
            raise TypeError(f"{name}[{index}] must be a two-item array")
        pairs.append((item[0], item[1]))
    return tuple(pairs)


def _order_side(value: Any, *, name: str) -> OrderSide:
    return value if isinstance(value, OrderSide) else OrderSide(_text(value, name=name))


def _event(payload: Mapping[str, Any], *, index: int) -> BookEvent | TradeEvent:
    kind = _text(payload.get("kind"), name=f"public_events[{index}].kind")
    if kind == "book":
        _exact_keys(
            payload,
            name=f"public_events[{index}]",
            required={
                "kind",
                "token_id",
                "timestamp_ms",
                "source_timestamp_ms",
                "tick_size",
                "source_event_id",
            },
            optional={"best_bid", "best_ask"},
        )
        best_bid = (
            None
            if "best_bid" not in payload or payload["best_bid"] is None
            else _decimal(payload["best_bid"], name=f"public_events[{index}].best_bid")
        )
        best_ask = (
            None
            if "best_ask" not in payload or payload["best_ask"] is None
            else _decimal(payload["best_ask"], name=f"public_events[{index}].best_ask")
        )
        if best_bid is None and best_ask is None:
            raise ValueError("book events must carry at least one visible side")
        return BookEvent(
            token_id=_text(
                payload["token_id"], name=f"public_events[{index}].token_id"
            ),
            timestamp_ms=_integer(
                payload["timestamp_ms"], name=f"public_events[{index}].timestamp_ms"
            ),
            source_timestamp_ms=_integer(
                payload["source_timestamp_ms"],
                name=f"public_events[{index}].source_timestamp_ms",
            ),
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=_decimal(
                payload["tick_size"], name=f"public_events[{index}].tick_size"
            ),
            source_event_id=_text(
                payload["source_event_id"],
                name=f"public_events[{index}].source_event_id",
            ),
        )
    if kind == "trade":
        _exact_keys(
            payload,
            name=f"public_events[{index}]",
            required={
                "kind",
                "token_id",
                "timestamp_ms",
                "price",
                "quantity",
                "aggressor_side",
                "source_event_id",
            },
        )
        return TradeEvent(
            token_id=_text(
                payload["token_id"], name=f"public_events[{index}].token_id"
            ),
            timestamp_ms=_integer(
                payload["timestamp_ms"], name=f"public_events[{index}].timestamp_ms"
            ),
            price=_decimal(payload["price"], name=f"public_events[{index}].price"),
            quantity=_decimal(
                payload["quantity"], name=f"public_events[{index}].quantity"
            ),
            aggressor_side=_order_side(
                payload["aggressor_side"],
                name=f"public_events[{index}].aggressor_side",
            ),
            source_event_id=_text(
                payload["source_event_id"],
                name=f"public_events[{index}].source_event_id",
            ),
        )
    raise ValueError(f"public_events[{index}].kind must be 'book' or 'trade'")


def _token_map(
    payload: Mapping[str, Any],
    *,
    name: str,
    parser,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for token_id, value in payload.items():
        if not isinstance(token_id, str) or not token_id:
            raise ValueError(f"{name} keys must be non-empty token ids")
        result[token_id] = parser(_mapping(value, name=f"{name}[{token_id}]"), token_id)
    return result


def _books(payload: Mapping[str, Any]) -> dict[str, OrderBookSnapshot]:
    return _token_map(
        payload,
        name="books",
        parser=lambda item, token_id: _snapshot(item, token_id=token_id),
    )


def _depth_books(payload: Mapping[str, Any], *, name: str) -> dict[str, DepthBook]:
    return _token_map(
        payload,
        name=name,
        parser=lambda item, token_id: _depth_book(
            item, token_id=token_id, name=f"{name}[{token_id}]"
        ),
    )


@dataclass(frozen=True)
class _CaptureInventory:
    manifest_count: int
    record_count: int


def _capture_root(value: Any) -> Path:
    raw = Path(_text(value, name="capture_root")).expanduser()
    absolute = Path(os.path.abspath(raw))
    for ancestor in (absolute, *absolute.parents):
        try:
            metadata = ancestor.lstat()
        except OSError as exc:
            raise ValueError("capture_root does not exist") from exc
        if _is_link_like(metadata):
            raise ValueError(
                "capture_root and all ancestors must reject symlinks/junctions/reparse points"
            )
    try:
        root = raw.resolve(strict=True)
    except OSError as exc:
        raise ValueError("capture_root does not exist") from exc
    if not root.is_dir():
        raise ValueError("capture_root must be a directory")
    for child in (root / "raw", root / "derived", root / "checkpoints"):
        try:
            metadata = child.lstat()
        except OSError as exc:
            raise ValueError("capture_root is not a finalized CaptureStore tree") from exc
        if _is_link_like(metadata) or not child.is_dir():
            raise ValueError("capture_root is not a finalized CaptureStore tree")
    return root


def _assert_no_link_like_tree(root: Path) -> None:
    for path in (root, *sorted(root.rglob("*"))):
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ValueError("capture tree changed while it was being inspected") from exc
        if _is_link_like(metadata):
            raise ValueError(
                "capture tree cannot contain symlinks/junctions/reparse points"
            )


def _capture_inventory(root: Path) -> _CaptureInventory:
    """Validate manifest metadata and every canonical raw-record identifier."""

    _assert_no_link_like_tree(root)
    _assert_clean_integrity(root)
    manifest_paths = tuple(sorted((root / "raw").rglob("*.manifest.json")))
    if not manifest_paths:
        raise ValueError("capture tree contains no finalized manifests")
    total_records = 0
    seen_record_ids: set[str] = set()
    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            raise TypeError("capture manifest must be an object")
        source = manifest.get("source")
        batch_id = manifest.get("batch_id")
        raw_path_value = manifest.get("raw_path")
        if (
            manifest.get("manifest_version") != "capture-manifest.v1"
            or not isinstance(source, str)
            or not source
            or not isinstance(batch_id, str)
            or not batch_id
            or not isinstance(raw_path_value, str)
            or not raw_path_value
        ):
            raise ValueError("capture manifest identity is invalid")
        raw_path = (root / raw_path_value).resolve()
        expected_raw_path = manifest_path.with_name(
            f"{manifest_path.name.removesuffix('.manifest.json')}.jsonl"
        ).resolve()
        if (
            raw_path != expected_raw_path
            or not raw_path.is_relative_to((root / "raw" / source).resolve())
            or raw_path.name != f"{batch_id}.jsonl"
            or not raw_path.is_file()
            or raw_path.is_symlink()
        ):
            raise ValueError("capture manifest raw_path is not source-confined")
        raw_bytes = raw_path.read_bytes()
        lines = raw_bytes.splitlines()
        checksum = manifest.get("checksum")
        if (
            not isinstance(checksum, Mapping)
            or checksum.get("algorithm") != "sha256"
            or checksum.get("value") != hashlib.sha256(raw_bytes).hexdigest()
            or checksum.get("bytes") != len(raw_bytes)
            or checksum.get("lines") != len(lines)
            or manifest.get("record_count") != len(lines)
        ):
            raise ValueError("capture manifest counts/checksum do not match raw data")
        for line_number, encoded in enumerate(lines, start=1):
            try:
                row = json.loads(encoded)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"invalid finalized capture JSONL at {raw_path}:{line_number}"
                ) from exc
            if not isinstance(row, Mapping):
                raise TypeError("finalized capture row must be an object")
            payload = row.get("payload")
            record_id = row.get("record_id")
            if (
                row.get("source") != source
                or not isinstance(payload, Mapping)
                or payload.get("source") != source
                or not isinstance(record_id, str)
                or canonical_record_id_from_json(row) != record_id
                or record_id in seen_record_ids
            ):
                raise ValueError("finalized capture row identity is invalid")
            seen_record_ids.add(record_id)
        total_records += len(lines)
    if total_records == 0:
        raise ValueError("capture tree contains no finalized records")
    return _CaptureInventory(
        manifest_count=len(manifest_paths),
        record_count=total_records,
    )


def _capture_summary(
    root: Path,
    inventory: _CaptureInventory,
    *,
    require_clean: bool = True,
) -> Mapping[str, Any]:
    summary_path = root / "capture-summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("capture-summary.json is missing or unreadable") from exc
    if not isinstance(summary, Mapping):
        raise TypeError("capture summary must be an object")
    integrity = summary.get("integrity")
    integrity_clean = isinstance(integrity, Mapping) and not any(
        integrity.get(key)
        for key in (
            "orphan_partials",
            "raw_without_manifest",
            "manifest_without_raw",
            "checksum_mismatches",
            "invalid_manifests",
        )
    )
    if (
        summary.get("schema_version") != "btc-twap-compact-forward-capture-summary.v1"
        or summary.get("manifest_count") != inventory.manifest_count
        or summary.get("manifest_record_count") != inventory.record_count
        or summary.get("target_count") != 2
        or summary.get("asset_count") != 4
        or summary.get("paper_only") is not True
        or summary.get("public_only") is not True
        or summary.get("new_orders_disabled") is not True
        or summary.get("authenticated_endpoints_used") != 0
        or summary.get("orders_submitted") != 0
        or not integrity_clean
    ):
        raise ValueError("capture summary is not a clean finalized compact capture")
    if require_clean and (
        summary.get("capture_error") is not None
        or summary.get("recorder_leg_failures") != []
    ):
        raise ValueError("capture summary is not a clean finalized compact capture")
    return summary


def _history_capture_roots(attempt: Mapping[str, Any]) -> tuple[Path, ...]:
    value = attempt.get("history_capture_roots", ())
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise TypeError("history_capture_roots must be an array")
    roots = tuple(_capture_root(item) for item in value)
    if len(roots) != len(set(roots)):
        raise ValueError("history_capture_roots cannot contain duplicates")
    return roots


def _boundary_event_ids(
    roots: Sequence[Path],
    *,
    timestamp_ms: int,
    expected_value: Decimal,
) -> frozenset[str]:
    matches: set[str] = set()
    for root in roots:
        for record in _records(root, "rtds_ws"):
            envelope = record.get("payload")
            message = envelope.get("payload") if isinstance(envelope, Mapping) else None
            if not isinstance(message, Mapping):
                continue
            payload = message.get("payload")
            if (
                not isinstance(payload, Mapping)
                or message.get("topic") != "crypto_prices_twap_sixty"
                or payload.get("symbol") != "btc/usd"
                or payload.get("timestamp") != timestamp_ms
            ):
                continue
            observed = _decimal(payload.get("value"), name="boundary value")
            record_id = record.get("record_id")
            if observed == expected_value and isinstance(record_id, str) and record_id:
                matches.add(record_id)
    return frozenset(matches)


def _same_pair(left: SameExpiryPair, right: SameExpiryPair) -> bool:
    return left == right


def _book_events(
    replay: CausalBookReplay,
    *,
    token_ids: set[str],
    submitted_at_ms: int,
    expiry_ms: int,
) -> tuple[BookEvent, ...]:
    events: list[BookEvent] = []
    for token_id in sorted(token_ids):
        for observation in replay.observations_by_token.get(token_id, ()):
            replay_at_ms = max(
                observation.source_at_ms,
                observation.received_at_ms,
            )
            if not submitted_at_ms < replay_at_ms < expiry_ms:
                continue
            snapshot = observation.snapshot
            events.append(
                BookEvent(
                    token_id=token_id,
                    timestamp_ms=replay_at_ms,
                    source_timestamp_ms=observation.source_at_ms,
                    best_bid=snapshot.best_bid,
                    best_ask=snapshot.best_ask,
                    tick_size=snapshot.tick_size,
                    source_event_id=observation.source_event_id,
                )
            )
    return tuple(events)


def _compact_top_book_events(
    root: Path,
    *,
    tick_by_token: Mapping[str, Decimal],
    submitted_at_ms: int,
    expiry_ms: int,
) -> tuple[BookEvent, ...]:
    """Recover compact WS top changes that do not carry reconstructable depth."""

    receipt_offset_ms = _receipt_clock_offset_ms(root)
    events: list[BookEvent] = []
    for record in _records(root, "clob_market_ws"):
        envelope = record.get("payload")
        if not isinstance(envelope, Mapping) or envelope.get("event_type") != (
            "best_bid_ask"
        ):
            continue
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            raise TypeError("compact top-of-book payload must be an object")
        raw_timestamp = payload.get("timestamp")
        if (
            isinstance(raw_timestamp, bool)
            or not isinstance(raw_timestamp, (str, int))
            or not str(raw_timestamp).isdigit()
        ):
            raise ValueError("compact top-of-book source timestamp is invalid")
        source_timestamp_ms = int(str(raw_timestamp))
        received_at_ms = _iso_to_epoch_ms(
            record.get("received_at"),
            receipt_clock_offset_ms=receipt_offset_ms,
        )
        replay_at_ms = max(source_timestamp_ms, received_at_ms)
        if not submitted_at_ms < replay_at_ms < expiry_ms:
            continue
        changes_value = payload.get("changes")
        if isinstance(changes_value, list):
            changes = changes_value
        else:
            changes = [payload]
        record_id = _text(record.get("record_id"), name="top-of-book record_id")
        for index, change in enumerate(changes):
            if not isinstance(change, Mapping):
                raise TypeError("compact top-of-book change must be an object")
            token_id = _text(
                change.get("asset_id"),
                name="compact top-of-book asset_id",
            )
            if token_id not in tick_by_token:
                continue
            best_bid = (
                None
                if change.get("best_bid") is None
                else _decimal(change.get("best_bid"), name="compact top-of-book best_bid")
            )
            best_ask = (
                None
                if change.get("best_ask") is None
                else _decimal(change.get("best_ask"), name="compact top-of-book best_ask")
            )
            if best_bid is None and best_ask is None:
                raise ValueError("compact top-of-book change must include one visible side")
            if best_bid is not None and not Decimal(0) <= best_bid <= Decimal(1):
                raise ValueError("compact top-of-book bid is invalid")
            if best_ask is not None and not Decimal(0) <= best_ask <= Decimal(1):
                raise ValueError("compact top-of-book ask is invalid")
            if (
                best_bid is not None
                and best_ask is not None
                and best_bid > best_ask
            ):
                raise ValueError("compact top-of-book prices are crossed or invalid")
            events.append(
                BookEvent(
                    token_id=token_id,
                    timestamp_ms=replay_at_ms,
                    source_timestamp_ms=source_timestamp_ms,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    tick_size=tick_by_token[token_id],
                    source_event_id=f"{record_id}:top:{index}:{token_id}",
                )
            )
    return tuple(events)


@dataclass(frozen=True)
class _ValidatedTradePage:
    page_number: int
    offset: int
    limit: int
    trades: tuple[Mapping[str, Any], ...]


def _validated_trade_page(
    response: Mapping[str, Any],
    *,
    condition_id: str,
) -> _ValidatedTradePage:
    provenance = _mapping(
        response.get("provenance"),
        name="Data API trade provenance",
    )
    request_params = _mapping(
        provenance.get("request_params"),
        name="Data API trade request_params",
    )
    url = _text(provenance.get("url"), name="Data API trade URL")
    parsed = urlsplit(url)
    body_utf8 = _text(
        provenance.get("body_utf8"),
        name="Data API trade body_utf8",
    )
    body = body_utf8.encode("utf-8")
    if (
        response.get("resource") != "data_api_trades"
        or response.get("request_key") != condition_id
        or response.get("response_status") == "error"
        or provenance.get("source") != "data_api_trades"
        or provenance.get("method") != "GET"
        or parsed.scheme != "https"
        or parsed.hostname != "data-api.polymarket.com"
        or parsed.path != "/trades"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or not isinstance(provenance.get("status_code"), int)
        or isinstance(provenance.get("status_code"), bool)
        or not 200 <= provenance["status_code"] < 300
        or provenance.get("body_sha256") != hashlib.sha256(body).hexdigest()
        or provenance.get("body_bytes") != len(body)
    ):
        raise ValueError("Data API trade response provenance is invalid")
    params = dict(request_params)
    required_params = {"market", "takerOnly", "limit", "offset"}
    if set(params) - required_params or {
        "market",
        "takerOnly",
        "limit",
        "offset",
    } - set(params):
        raise ValueError("Data API trade request_params are incomplete")
    if params.get("market") != condition_id or params.get("takerOnly") != "true":
        raise ValueError("Data API trade request scope is invalid")
    limit = params.get("limit")
    offset = params.get("offset")
    page_number = response.get("page_number", params.get("page_number"))
    if any(isinstance(item, bool) for item in (limit, offset, page_number)):
        raise ValueError("Data API trade pagination values must be integers")
    if not all(isinstance(item, int) for item in (limit, offset, page_number)):
        raise ValueError("Data API trade pagination values must be integers")
    if limit != 1_000 or offset < 0 or page_number <= 0:
        raise ValueError("Data API trade pagination values are invalid")
    try:
        decoded = json.loads(body_utf8, parse_float=str)
    except json.JSONDecodeError as exc:
        raise ValueError("Data API trade body is invalid JSON") from exc
    raw_json = response.get("raw_json")
    if not isinstance(decoded, list) or decoded != raw_json:
        raise ValueError("Data API parsed trades do not match preserved body bytes")
    if any(not isinstance(item, Mapping) for item in decoded):
        raise TypeError("Data API trade rows must be objects")
    return _ValidatedTradePage(
        page_number=page_number,
        offset=offset,
        limit=limit,
        trades=tuple(decoded),
    )


def _validated_trade_pages(
    responses: Sequence[Mapping[str, Any]],
    *,
    condition_id: str,
) -> tuple[_ValidatedTradePage, ...]:
    pages = tuple(
        sorted(
            (
                _validated_trade_page(response, condition_id=condition_id)
                for response in responses
            ),
            key=lambda item: (item.offset, item.page_number),
        )
    )
    if not pages:
        raise ValueError("trade snapshot must include at least one page per condition")
    first = pages[0]
    if first.offset != 0 or first.page_number != 1:
        raise ValueError("Data API trade pagination must start at page 1 / offset 0")
    if first.offset != (first.page_number - 1) * first.limit:
        raise ValueError("Data API trade page offset does not match page_number")
    for previous, current in pairwise(pages):
        if current.limit != first.limit:
            raise ValueError("Data API trade page size must stay constant")
        if current.offset != (current.page_number - 1) * current.limit:
            raise ValueError("Data API trade page offset does not match page_number")
        if len(previous.trades) != previous.limit:
            raise ValueError("only the terminal Data API trade page may be short")
        if current.offset != previous.offset + previous.limit:
            raise ValueError("Data API trade offsets are not contiguous")
        if current.page_number != previous.page_number + 1:
            raise ValueError("Data API trade page numbers are not contiguous")
    if len(pages[-1].trades) >= pages[-1].limit:
        raise ValueError(
            "Data API trade final page must be short to prove no truncation"
        )
    return pages


def _trade_events(
    root: Path,
    *,
    pair: SameExpiryPair,
    submitted_at_ms: int,
    expiry_ms: int,
    poll_interval_ms: int,
) -> tuple[TradeEvent, ...]:
    contracts = (pair.market_5, pair.market_15)
    contract_by_condition = {contract.condition_id: contract for contract in contracts}
    receipt_offset_ms = _receipt_clock_offset_ms(root)
    candidates: dict[str, TradeEvent] = {}
    fingerprints: dict[str, tuple[str, Decimal, Decimal, OrderSide, int]] = {}
    snapshot_receipts: list[int] = []
    for record in _records(root, "trades_http"):
        envelope = record.get("payload")
        if not isinstance(envelope, Mapping):
            raise TypeError("trades_http record payload must be an object")
        snapshot = envelope.get("payload")
        if (
            envelope.get("source") != "trades_http"
            or envelope.get("kind") != "snapshot"
            or envelope.get("event_type") != "trades_snapshot"
            or envelope.get("schema_version") != "trades-http.snapshot.v1"
            or not isinstance(snapshot, Mapping)
            or snapshot.get("schema_version") != "edge-lab-public-snapshot.v1"
            or snapshot.get("snapshot_kind") != "trades"
            or snapshot.get("requested_asset_ids") != []
            or snapshot.get("truncated_resources") != []
        ):
            raise ValueError("trades_http snapshot contract is invalid")
        responses = snapshot.get("responses")
        if not isinstance(responses, list) or not responses:
            raise ValueError("trade snapshot must cover both conditions")
        grouped: dict[str, list[Mapping[str, Any]]] = {
            condition_id: [] for condition_id in contract_by_condition
        }
        for response in responses:
            if not isinstance(response, Mapping):
                raise TypeError("trade snapshot response must be an object")
            request_key = response.get("request_key")
            if request_key not in grouped:
                raise ValueError("trade snapshot condition scope is incomplete")
            grouped[str(request_key)].append(response)
        if any(not rows for rows in grouped.values()):
            raise ValueError("trade snapshot condition scope is incomplete")
        received_at_ms = _iso_to_epoch_ms(
            record.get("received_at"),
            receipt_clock_offset_ms=receipt_offset_ms,
        )
        snapshot_receipts.append(received_at_ms)
        for condition_id, contract in contract_by_condition.items():
            pages = _validated_trade_pages(grouped[condition_id], condition_id=condition_id)
            for raw_trade in (trade for page in pages for trade in page.trades):
                if str(raw_trade.get("conditionId", "")).lower() != (
                    condition_id.lower()
                ):
                    raise ValueError("Data API trade condition identity mismatch")
                observed_token = _text(
                    raw_trade.get("asset"),
                    name="Data API trade asset",
                )
                if observed_token not in {
                    contract.up_token_id,
                    contract.down_token_id,
                }:
                    raise ValueError("Data API trade token identity mismatch")
                side = _text(raw_trade.get("side"), name="Data API trade side")
                if side not in {"BUY", "SELL"}:
                    raise ValueError("Data API trade side is invalid")
                source_timestamp_raw = raw_trade.get("timestamp")
                if (
                    isinstance(source_timestamp_raw, bool)
                    or not isinstance(source_timestamp_raw, (str, int))
                    or not str(source_timestamp_raw).isdigit()
                ):
                    raise ValueError("Data API trade timestamp is invalid")
                source_timestamp_ms = int(str(source_timestamp_raw)) * 1_000
                observed_price = _decimal(
                    raw_trade.get("price"),
                    name="Data API trade price",
                )
                quantity = _decimal(
                    raw_trade.get("size"),
                    name="Data API trade size",
                )
                if not Decimal(0) < observed_price < Decimal(1) or quantity <= 0:
                    raise ValueError("Data API trade economics are invalid")
                event_key = data_api_trade_event_key(raw_trade)
                if side == "BUY":
                    token_id = (
                        contract.down_token_id
                        if observed_token == contract.up_token_id
                        else contract.up_token_id
                    )
                    price = Decimal(1) - observed_price
                else:
                    token_id = observed_token
                    price = observed_price
                if not submitted_at_ms < source_timestamp_ms < expiry_ms:
                    continue
                fingerprint = (
                    token_id,
                    price,
                    quantity,
                    OrderSide.SELL,
                    source_timestamp_ms,
                )
                previous = fingerprints.setdefault(event_key, fingerprint)
                if previous != fingerprint:
                    raise ValueError("duplicate public trade identity conflicts")
                candidates.setdefault(
                    event_key,
                    TradeEvent(
                        token_id=token_id,
                        timestamp_ms=source_timestamp_ms,
                        price=price,
                        quantity=quantity,
                        aggressor_side=OrderSide.SELL,
                        source_event_id=event_key,
                    ),
                )
    tolerance_ms = 2_000
    relevant_receipts = tuple(
        timestamp_ms
        for timestamp_ms in sorted(set(snapshot_receipts))
        if submitted_at_ms - poll_interval_ms - tolerance_ms
        <= timestamp_ms
        <= expiry_ms + poll_interval_ms + tolerance_ms
    )
    if (
        isinstance(poll_interval_ms, bool)
        or not isinstance(poll_interval_ms, int)
        or poll_interval_ms <= 0
        or not relevant_receipts
        or relevant_receipts[0] > submitted_at_ms + poll_interval_ms + tolerance_ms
        or relevant_receipts[-1] < expiry_ms
        or any(
            right - left > poll_interval_ms + tolerance_ms
            for left, right in pairwise(relevant_receipts)
        )
    ):
        raise ValueError("public taker-trade polling is incomplete for replay window")
    return tuple(
        event
        for _event_key, event in sorted(candidates.items())
    )


def _event_sort_key(event: BookEvent | TradeEvent) -> tuple[int, int, str, str]:
    return (
        event.timestamp_ms,
        0 if isinstance(event, BookEvent) else 1,
        event.token_id,
        event.source_event_id,
    )


def _depth_book_was_captured(
    book: DepthBook,
    *,
    replay: CausalBookReplay,
    submitted_at_ms: int,
    expiry_ms: int,
) -> bool:
    for observation in replay.observations_by_token.get(book.token_id, ()):
        replay_at_ms = max(observation.source_at_ms, observation.received_at_ms)
        snapshot = observation.snapshot
        if not submitted_at_ms < replay_at_ms < expiry_ms:
            continue
        if (
            book.timestamp_ms == snapshot.timestamp_ms
            and book.tick_size == snapshot.tick_size
            and book.min_order_size == snapshot.minimum_order_size
            and tuple((level.price, level.size) for level in book.bids)
            == tuple((level.price, level.size) for level in snapshot.bids)
            and tuple((level.price, level.size) for level in book.asks)
            == tuple((level.price, level.size) for level in snapshot.asks)
            and observation.full_depth_available
        ):
            return True
    return False


_DIRTY_LIFECYCLE_EVENT_TYPES = frozenset(
    {
        "disconnected",
        "error",
        "resync_begin",
        "resync_complete",
        "resnapshot_error",
        "snapshot_error",
    }
)


def _window_capture_anomalies(
    roots: Sequence[Path],
    *,
    start_ms: int,
    expiry_ms: int,
) -> tuple[str, ...]:
    anomalies: list[str] = []
    for root in roots:
        receipt_offset_ms = _receipt_clock_offset_ms(root)
        raw_root = root / "raw"
        for source_dir in sorted(path for path in raw_root.iterdir() if path.is_dir()):
            for record in _records(root, source_dir.name):
                received_at_ms = _iso_to_epoch_ms(
                    record.get("received_at"),
                    receipt_clock_offset_ms=receipt_offset_ms,
                )
                if not start_ms <= received_at_ms < expiry_ms:
                    continue
                envelope = record.get("payload")
                if not isinstance(envelope, Mapping):
                    raise TypeError("capture record payload must be an object")
                kind = envelope.get("kind")
                event_type = envelope.get("event_type")
                if kind == "diagnostic":
                    record_id = _text(record.get("record_id"), name="capture record_id")
                    anomalies.append(f"{root}:{record_id}:diagnostic")
                elif (
                    kind == "lifecycle"
                    and event_type in _DIRTY_LIFECYCLE_EVENT_TYPES
                ):
                    record_id = _text(record.get("record_id"), name="capture record_id")
                    anomalies.append(f"{root}:{record_id}:{event_type}")
    return tuple(sorted(set(anomalies)))


def _assert_no_replay_window_capture_anomalies(
    roots: Sequence[Path],
    *,
    start_ms: int,
    expiry_ms: int,
) -> None:
    if _window_capture_anomalies(roots, start_ms=start_ms, expiry_ms=expiry_ms):
        raise ValueError("capture anomaly/disconnect intersects replay window")


def _verify_capture_attempt(
    *,
    attempt: Mapping[str, Any],
    pair: SameExpiryPair,
    settlement_state: PairSettlementState,
    books: Mapping[str, OrderBookSnapshot],
    public_events: Sequence[BookEvent | TradeEvent],
    hedge_books: Mapping[str, DepthBook],
    unwind_books: Mapping[str, DepthBook],
    admission: Mapping[str, Any],
) -> StructuralCaptureVerification:
    """Re-derive one replay row exclusively from a finalized recorder tree."""

    attempt_id = _text(attempt.get("attempt_id"), name="attempt_id")
    root = _capture_root(attempt.get("capture_root"))
    initial_tree = _tree_identity(root)
    inventory = _capture_inventory(root)
    _capture_summary(root, inventory)
    history_roots = _history_capture_roots(attempt)
    if root in history_roots:
        raise ValueError("primary capture cannot also be a history capture")
    history_identities: dict[Path, Mapping[str, Any]] = {}
    history_inventories: dict[Path, _CaptureInventory] = {}
    history_configs = {}
    for history_root in history_roots:
        history_identities[history_root] = _tree_identity(history_root)
        history_inventory = _capture_inventory(history_root)
        history_inventories[history_root] = history_inventory
        _capture_summary(history_root, history_inventory)
        history_config = load_capture_config(history_root / "capture-config.json")
        if history_config.data_root != history_root:
            raise ValueError("history capture config data_root is invalid")
        history_configs[history_root] = history_config
    config_path = root / "capture-config.json"
    config = load_capture_config(config_path)
    if config.data_root != root:
        raise ValueError("capture config data_root does not match capture_root")
    targets = tuple(config.targets)
    captured_pair, _targets_by_horizon = _validate_pair_targets(targets)
    if not _same_pair(captured_pair, pair):
        raise ValueError("input pair is not the captured pair")
    expected_tokens = {
        pair.market_5.up_token_id,
        pair.market_5.down_token_id,
        pair.market_15.up_token_id,
        pair.market_15.down_token_id,
    }
    if (
        set(config.asset_ids) != expected_tokens
        or set(config.condition_ids)
        != {pair.market_5.condition_id, pair.market_15.condition_id}
        or set(config.rule_market_ids)
        != {pair.market_5.market_id, pair.market_15.market_id}
    ):
        raise ValueError("capture config does not cover the exact four-token pair")
    if (
        config.snapshot_intervals.get("clob") != 30.0
        or config.snapshot_intervals.get("trades") != 5.0
        or config.snapshot_intervals.get("rules") != 30.0
        or config.persist_raw_clob_frames
        or config.persist_reconstructed_full_depth_frames
        or not config.persist_top_of_book_changes
    ):
        raise ValueError("capture config is not the registered compact v0.8 policy")
    capture_attempt_id = _text(
        admission.get("capture_attempt_id"),
        name="rolling admission capture_attempt_id",
    )
    if capture_attempt_id != root.name:
        raise ValueError("rolling admission is bound to a different capture attempt")
    if (
        admission.get("common_expiry_id") != attempt_id
        or attempt_id != canonical_expiry_cluster_id(pair.expires_at_ms)
        or admission.get("canonical_pair_id") != canonical_event_cluster_id(pair)
    ):
        raise ValueError("rolling admission pair/expiry identity is invalid")
    submitted_at_ms = _integer(
        attempt.get("submitted_at_ms"),
        name="submitted_at_ms",
    )
    maximum_book_age_ms = _integer(
        attempt.get("max_book_age_ms"),
        name="max_book_age_ms",
    )
    capture_starts = (
        config.capture_started_at_ms,
        *(item.capture_started_at_ms for item in history_configs.values()),
    )
    if not any(
        started_at_ms is not None and started_at_ms <= pair.market_15.opens_at_ms
        for started_at_ms in capture_starts
    ):
        raise ValueError("capture set did not start before the 15m opening boundary")
    _validate_rules_and_fees(
        capture_root=root,
        targets=targets,
        available_by_ms=submitted_at_ms,
    )
    twap_roots = (*history_roots, root)
    shared_twap = _combined_series(
        twap_roots,
        topic="crypto_prices_twap_sixty",
        symbol="btc/usd",
    )
    window = tuple(
        item
        for item in shared_twap
        if pair.market_15.opens_at_ms <= item[0] <= pair.expires_at_ms
    )
    if (
        not window
        or window[0][0] != pair.market_15.opens_at_ms
        or window[-1][0] != pair.expires_at_ms
        or any(right[0] - left[0] > 2_000 for left, right in pairwise(window))
    ):
        raise ValueError("official 60s TWAP capture has an incomplete/gapped window")
    opening_5 = _exact_observation(
        shared_twap,
        pair.market_5.opens_at_ms,
        available_by_ms=submitted_at_ms,
    )
    opening_15 = _exact_observation(
        shared_twap,
        pair.market_15.opens_at_ms,
        available_by_ms=submitted_at_ms,
    )
    closing = _exact_observation(shared_twap, pair.expires_at_ms)
    if opening_5 is None or opening_15 is None or closing is None:
        raise ValueError("exact official settlement boundaries are missing")
    opening_5_event_ids = _boundary_event_ids(
        twap_roots,
        timestamp_ms=pair.market_5.opens_at_ms,
        expected_value=opening_5[2],
    )
    opening_15_event_ids = _boundary_event_ids(
        twap_roots,
        timestamp_ms=pair.market_15.opens_at_ms,
        expected_value=opening_15[2],
    )
    if (
        settlement_state.strike_5 != opening_5[2]
        or settlement_state.strike_15 != opening_15[2]
        or settlement_state.opening_5_source_event_id not in opening_5_event_ids
        or settlement_state.opening_15_source_event_id not in opening_15_event_ids
    ):
        raise ValueError("settlement state is not derived from captured boundaries")
    actual_5_up = closing[2] >= opening_5[2]
    actual_15_up = closing[2] >= opening_15[2]
    outcome = _mapping(attempt.get("actual_outcome"), name="actual_outcome")
    if (
        outcome.get("actual_5_up") is not actual_5_up
        or outcome.get("actual_15_up") is not actual_15_up
    ):
        raise ValueError("actual outcome conflicts with the common terminal TWAP")
    _validated_resolution_evidence(
        case_alias=attempt_id,
        capture_root=root,
        targets=targets,
        expected_outcomes={
            pair.market_5.market_id: "Up" if actual_5_up else "Down",
            pair.market_15.market_id: "Up" if actual_15_up else "Down",
        },
        expiry_ms=pair.expires_at_ms,
    )
    _assert_no_replay_window_capture_anomalies(
        twap_roots,
        start_ms=pair.market_15.opens_at_ms,
        expiry_ms=pair.expires_at_ms,
    )
    contracts_by_token = {
        token_id: contract
        for contract in (pair.market_5, pair.market_15)
        for token_id in (contract.up_token_id, contract.down_token_id)
    }
    replay = CausalBookReplay.from_records(
        (
            *_records(root, "clob_market_ws"),
            *_records(root, "clob_http"),
        ),
        receipt_clock_offset_ms=_receipt_clock_offset_ms(root),
        tokens={
            token_id: BookReplayToken(
                token_id=token_id,
                tick_size=contract.tick_size,
                minimum_order_size=contract.minimum_order_size,
            )
            for token_id, contract in contracts_by_token.items()
        },
    )
    signal = replay.signal_books(
        token_ids=tuple(sorted(expected_tokens)),
        decision_at_ms=submitted_at_ms,
        maximum_age_ms=maximum_book_age_ms,
        require_full_depth=True,
    )
    if set(signal) != expected_tokens or any(
        signal[token_id].snapshot != books[token_id] for token_id in expected_tokens
    ):
        raise ValueError("decision books are not the latest causal full-depth capture")
    expected_events = tuple(
        sorted(
            (
                *_book_events(
                    replay,
                    token_ids=expected_tokens,
                    submitted_at_ms=submitted_at_ms,
                    expiry_ms=pair.expires_at_ms,
                ),
                *_compact_top_book_events(
                    root,
                    tick_by_token={
                        token_id: contract.tick_size
                        for token_id, contract in contracts_by_token.items()
                    },
                    submitted_at_ms=submitted_at_ms,
                    expiry_ms=pair.expires_at_ms,
                ),
                *_trade_events(
                    root,
                    pair=pair,
                    submitted_at_ms=submitted_at_ms,
                    expiry_ms=pair.expires_at_ms,
                    poll_interval_ms=5_000,
                ),
            ),
            key=_event_sort_key,
        )
    )
    if tuple(public_events) != expected_events:
        raise ValueError("public event stream is not the complete captured replay")
    for name, depth_books in (
        ("hedge_books", hedge_books),
        ("unwind_books", unwind_books),
    ):
        if any(
            token_id != book.token_id
            or token_id not in expected_tokens
            or not _depth_book_was_captured(
                book,
                replay=replay,
                submitted_at_ms=submitted_at_ms,
                expiry_ms=pair.expires_at_ms,
            )
            for token_id, book in depth_books.items()
        ):
            raise ValueError(f"{name} contains non-captured depth")
    final_tree = _tree_identity(root)
    if final_tree != initial_tree:
        raise ValueError("capture tree changed while it was being verified")
    final_history_identities = {
        history_root: _tree_identity(history_root) for history_root in history_roots
    }
    if final_history_identities != history_identities:
        raise ValueError("history capture tree changed while it was being verified")
    tree_sha256 = final_tree.get("tree_sha256")
    if not isinstance(tree_sha256, str):
        raise TypeError("capture tree identity omitted its SHA-256")
    book_event_count = sum(isinstance(event, BookEvent) for event in expected_events)
    return StructuralCaptureVerification(
        attempt_id=attempt_id,
        capture_attempt_id=capture_attempt_id,
        capture_root=str(root),
        capture_tree_sha256=tree_sha256,
        manifest_count=inventory.manifest_count,
        record_count=inventory.record_count,
        book_event_count=book_event_count,
        trade_event_count=len(expected_events) - book_event_count,
        complete_window=True,
        supporting_capture_roots=tuple(str(item) for item in history_roots),
        supporting_capture_tree_sha256=tuple(
            str(history_identities[item]["tree_sha256"]) for item in history_roots
        ),
        supporting_manifest_count=sum(
            history_inventories[item].manifest_count for item in history_roots
        ),
        supporting_record_count=sum(
            history_inventories[item].record_count for item in history_roots
        ),
    )


def _combined_twap_window_complete(
    roots: Sequence[Path],
    *,
    pair: SameExpiryPair,
) -> bool:
    shared_twap = _combined_series(
        roots,
        topic="crypto_prices_twap_sixty",
        symbol="btc/usd",
    )
    window = tuple(
        item
        for item in shared_twap
        if pair.market_15.opens_at_ms <= item[0] <= pair.expires_at_ms
    )
    return bool(
        window
        and window[0][0] == pair.market_15.opens_at_ms
        and window[-1][0] == pair.expires_at_ms
        and not any(right[0] - left[0] > 2_000 for left, right in pairwise(window))
    )


def _verify_zero_cohort(
    *,
    cohort: Mapping[str, Any],
    admission: Mapping[str, Any],
    outcome_receipt: Mapping[str, Any],
) -> LockedZeroCohort:
    attempt_id = _text(cohort.get("attempt_id"), name="zero cohort attempt_id")
    outcome = _text(cohort.get("outcome"), name="zero cohort outcome")
    root = _capture_root(cohort.get("capture_root"))
    initial_tree = _tree_identity(root)
    inventory = _capture_inventory(root)
    summary = _capture_summary(
        root,
        inventory,
        require_clean=outcome != "capture_dirty",
    )
    config = load_capture_config(root / "capture-config.json")
    if config.data_root != root:
        raise ValueError("zero cohort capture config data_root does not match capture_root")
    targets = tuple(config.targets)
    pair, _targets_by_horizon = _validate_pair_targets(targets)
    expected_tokens = {
        pair.market_5.up_token_id,
        pair.market_5.down_token_id,
        pair.market_15.up_token_id,
        pair.market_15.down_token_id,
    }
    if (
        set(config.asset_ids) != expected_tokens
        or set(config.condition_ids)
        != {pair.market_5.condition_id, pair.market_15.condition_id}
        or set(config.rule_market_ids)
        != {pair.market_5.market_id, pair.market_15.market_id}
    ):
        raise ValueError("zero cohort capture config does not cover the exact pair")
    history_roots = _history_capture_roots(cohort)
    if root in history_roots:
        raise ValueError("primary capture cannot also be a history capture")
    history_identities: dict[Path, Mapping[str, Any]] = {}
    history_inventories: dict[Path, _CaptureInventory] = {}
    history_configs: dict[Path, Any] = {}
    for history_root in history_roots:
        history_identities[history_root] = _tree_identity(history_root)
        history_inventory = _capture_inventory(history_root)
        history_inventories[history_root] = history_inventory
        _capture_summary(
            history_root,
            history_inventory,
            require_clean=outcome != "capture_dirty",
        )
        history_config = load_capture_config(history_root / "capture-config.json")
        if history_config.data_root != history_root:
            raise ValueError("history capture config data_root is invalid")
        history_pair, _ = _validate_pair_targets(tuple(history_config.targets))
        if history_pair != pair:
            raise ValueError("history capture root is not the same captured pair")
        history_configs[history_root] = history_config
    capture_starts = (
        config.capture_started_at_ms,
        *(item.capture_started_at_ms for item in history_configs.values()),
    )
    if not any(
        started_at_ms is not None and started_at_ms <= pair.market_15.opens_at_ms
        for started_at_ms in capture_starts
    ):
        raise ValueError("capture set did not start before the 15m opening boundary")
    all_roots = (*history_roots, root)
    anomalies = _window_capture_anomalies(
        all_roots,
        start_ms=pair.market_15.opens_at_ms,
        expiry_ms=pair.expires_at_ms,
    )
    decision_at_ms = (
        None
        if "decision_at_ms" not in cohort
        else _integer(cohort["decision_at_ms"], name="zero cohort decision_at_ms")
    )
    dirty_reason_code: str | None = None
    if outcome == "no_trade":
        if anomalies:
            raise ValueError("no_trade zero cohort cannot use a dirty capture")
        if decision_at_ms is None:
            raise ValueError("no_trade zero cohort requires decision_at_ms")
        _validate_rules_and_fees(
            capture_root=root,
            targets=targets,
            available_by_ms=decision_at_ms,
        )
        if not _combined_twap_window_complete(all_roots, pair=pair):
            raise ValueError("no_trade zero cohort cannot use an incomplete RTDS window")
    elif outcome == "capture_dirty":
        capture_error = summary.get("capture_error")
        recorder_failures = summary.get("recorder_leg_failures")
        if not anomalies and capture_error is None and recorder_failures == []:
            raise ValueError(
                "capture_dirty requires a verified capture error, recorder failure, or lifecycle anomaly"
            )
        if anomalies:
            dirty_reason_code = anomalies[0]
        elif capture_error is not None:
            dirty_reason_code = f"verified_capture_error:{capture_error}"
        else:
            dirty_reason_code = "verified_recorder_leg_failure"
    elif outcome == "rtds_gap_dirty":
        if anomalies:
            raise ValueError("rtds_gap_dirty cannot hide lifecycle anomalies")
        if _combined_twap_window_complete(all_roots, pair=pair):
            raise ValueError("rtds_gap_dirty requires a verified RTDS gap")
        dirty_reason_code = "verified_rtds_window_gap"
    elif outcome == "rule_or_fee_dirty":
        if anomalies:
            raise ValueError("rule_or_fee_dirty cannot hide lifecycle anomalies")
        try:
            _validate_rules_and_fees(
                capture_root=root,
                targets=targets,
                available_by_ms=pair.expires_at_ms,
            )
        except (TypeError, ValueError) as exc:
            dirty_reason_code = f"verified_rule_or_fee_dirty:{exc}"
        else:
            raise ValueError("rule_or_fee_dirty requires a verified rules/fees failure")
    else:
        raise ValueError("zero cohort outcome is not denominator-safe")
    final_tree = _tree_identity(root)
    if final_tree != initial_tree:
        raise ValueError("capture tree changed while it was being verified")
    final_history_identities = {
        history_root: _tree_identity(history_root) for history_root in history_roots
    }
    if final_history_identities != history_identities:
        raise ValueError("history capture tree changed while it was being verified")
    tree_sha256 = final_tree.get("tree_sha256")
    if not isinstance(tree_sha256, str):
        raise TypeError("capture tree identity omitted its SHA-256")
    verification = StructuralCaptureVerification(
        attempt_id=attempt_id,
        capture_attempt_id=_text(
            admission.get("capture_attempt_id", root.name),
            name="rolling admission capture_attempt_id",
        ),
        capture_root=str(root),
        capture_tree_sha256=tree_sha256,
        manifest_count=inventory.manifest_count,
        record_count=inventory.record_count,
        book_event_count=0,
        trade_event_count=0,
        complete_window=True,
        supporting_capture_roots=tuple(str(item) for item in history_roots),
        supporting_capture_tree_sha256=tuple(
            str(history_identities[item]["tree_sha256"]) for item in history_roots
        ),
        supporting_manifest_count=sum(
            history_inventories[item].manifest_count for item in history_roots
        ),
        supporting_record_count=sum(
            history_inventories[item].record_count for item in history_roots
        ),
    )
    source_evidence_sha256 = hashlib.sha256(
        canonical_json_bytes(
            _zero_cohort_source_evidence_document(
                cohort,
                verification,
                dirty_reason_code=dirty_reason_code,
            )
        )
    ).hexdigest()
    return LockedZeroCohort(
        attempt_id=attempt_id,
        expiry_ms=_integer(
            admission.get("expiry_ms"),
            name=f"rolling admission {attempt_id}.expiry_ms",
        ),
        outcome=outcome,
        clean=outcome == "no_trade",
        capture_verification=verification,
        source_evidence_sha256=source_evidence_sha256,
        dirty_reason_code=dirty_reason_code,
        decision_at_ms=decision_at_ms,
        locked_action_payload_sha256=None,
    )


def build_report_from_payload(
    payload: Mapping[str, Any],
    *,
    rolling_audit: Mapping[str, Any],
    preregistration_sha256: str,
) -> dict[str, Any]:
    document = _mapping(payload, name="input")
    _exact_keys(
        document,
        name="input",
        required={
            "schema_version",
            "neutral_disposition",
            "rolling_window_size",
            "minimum_expiries",
            "attempts",
            "locked_zero_cohorts",
        },
    )
    if document["schema_version"] != INPUT_SCHEMA:
        raise ValueError(f"schema_version must be {INPUT_SCHEMA}")
    attempts_value = document["attempts"]
    if not isinstance(attempts_value, Sequence) or isinstance(
        attempts_value, (str, bytes, bytearray)
    ):
        raise TypeError("attempts must be an array")
    zero_cohorts_value = document["locked_zero_cohorts"]
    if not isinstance(zero_cohorts_value, Sequence) or isinstance(
        zero_cohorts_value,
        (str, bytes, bytearray),
    ):
        raise TypeError("locked_zero_cohorts must be an array")
    if not attempts_value and not zero_cohorts_value:
        raise ValueError("at least one replayed or locked-zero cohort is required")
    audit_admissions = rolling_audit.get("admissions")
    audit_outcomes = rolling_audit.get("outcomes")
    if not isinstance(audit_admissions, Mapping) or not isinstance(
        audit_outcomes,
        Mapping,
    ):
        raise TypeError("rolling audit admissions and outcomes are required")
    plans = []
    capture_verification_by_attempt: dict[str, StructuralCaptureVerification] = {}
    public_events_by_attempt: dict[str, tuple[BookEvent | TradeEvent, ...]] = {}
    hedge_books_by_attempt: dict[str, dict[str, DepthBook]] = {}
    unwind_books_by_attempt: dict[str, dict[str, DepthBook]] = {}
    locked_action_payload_sha256_by_attempt: dict[str, str] = {}
    source_evidence_sha256_by_attempt: dict[str, str] = {}
    for index, raw_attempt in enumerate(attempts_value):
        attempt = _mapping(raw_attempt, name=f"attempts[{index}]")
        _exact_keys(
            attempt,
            name=f"attempts[{index}]",
            required={
                "attempt_id",
                "capture_root",
                "pair",
                "settlement_state",
                "action",
                "quantity",
                "submitted_at_ms",
                "max_book_age_ms",
                "actual_outcome",
                "books",
                "public_events",
            },
            optional={
                "initial_cash",
                "history_capture_roots",
                "hedge_books",
                "unwind_books",
            },
        )
        pair = _pair(_mapping(attempt["pair"], name=f"attempts[{index}].pair"))
        settlement_state = _settlement_state(
            _mapping(
                attempt["settlement_state"],
                name=f"attempts[{index}].settlement_state",
            ),
            pair=pair,
            name=f"attempts[{index}].settlement_state",
        )
        books = _books(_mapping(attempt["books"], name=f"attempts[{index}].books"))
        outcome = _mapping(
            attempt["actual_outcome"], name=f"attempts[{index}].actual_outcome"
        )
        _exact_keys(
            outcome,
            name=f"attempts[{index}].actual_outcome",
            required={"actual_5_up", "actual_15_up"},
        )
        attempt_id = _text(attempt["attempt_id"], name=f"attempts[{index}].attempt_id")
        if attempt_id in source_evidence_sha256_by_attempt:
            raise ValueError("attempt ids must be unique across all report rows")
        admission = audit_admissions.get(attempt_id)
        if not isinstance(admission, Mapping):
            raise TypeError("replayed attempt is absent from the rolling audit")
        locked_action_payload_sha256_by_attempt[attempt_id] = hashlib.sha256(
            canonical_json_bytes(_locked_action_document(attempt))
        ).hexdigest()
        plan = build_structural_double_maker_plan(
            attempt_id=attempt_id,
            pair=pair,
            settlement_state=settlement_state,
            books=books,
            action=_text(attempt["action"], name=f"attempts[{index}].action"),
            quantity=_decimal(attempt["quantity"], name=f"attempts[{index}].quantity"),
            submitted_at_ms=_integer(
                attempt["submitted_at_ms"],
                name=f"attempts[{index}].submitted_at_ms",
            ),
            actual_5_up=_bool(
                outcome["actual_5_up"],
                name=f"attempts[{index}].actual_outcome.actual_5_up",
            ),
            actual_15_up=_bool(
                outcome["actual_15_up"],
                name=f"attempts[{index}].actual_outcome.actual_15_up",
            ),
            max_book_age_ms=_integer(
                attempt["max_book_age_ms"],
                name=f"attempts[{index}].max_book_age_ms",
            ),
            initial_cash=(
                None
                if "initial_cash" not in attempt
                else _decimal(
                    attempt["initial_cash"],
                    name=f"attempts[{index}].initial_cash",
                )
            ),
        )
        plans.append(plan)
        events_value = attempt["public_events"]
        if not isinstance(events_value, Sequence) or isinstance(
            events_value, (str, bytes, bytearray)
        ):
            raise TypeError(f"attempts[{index}].public_events must be an array")
        public_events_by_attempt[attempt_id] = tuple(
            _event(
                _mapping(item, name=f"attempts[{index}].public_events[{event_index}]"),
                index=event_index,
            )
            for event_index, item in enumerate(events_value)
        )
        hedge_books_by_attempt[attempt_id] = (
            {}
            if "hedge_books" not in attempt
            else _depth_books(
                _mapping(attempt["hedge_books"], name=f"attempts[{index}].hedge_books"),
                name="hedge_books",
            )
        )
        unwind_books_by_attempt[attempt_id] = (
            {}
            if "unwind_books" not in attempt
            else _depth_books(
                _mapping(
                    attempt["unwind_books"], name=f"attempts[{index}].unwind_books"
                ),
                name="unwind_books",
            )
        )
        verification = _verify_capture_attempt(
            attempt=attempt,
            pair=pair,
            settlement_state=settlement_state,
            books=books,
            public_events=public_events_by_attempt[attempt_id],
            hedge_books=hedge_books_by_attempt[attempt_id],
            unwind_books=unwind_books_by_attempt[attempt_id],
            admission=admission,
        )
        capture_verification_by_attempt[attempt_id] = verification
        source_evidence_sha256_by_attempt[attempt_id] = hashlib.sha256(
            canonical_json_bytes(_source_evidence_document(attempt, verification))
        ).hexdigest()
    locked_zero_cohorts: list[LockedZeroCohort] = []
    for index, raw_zero_cohort in enumerate(zero_cohorts_value):
        zero_cohort = _mapping(
            raw_zero_cohort,
            name=f"locked_zero_cohorts[{index}]",
        )
        _exact_keys(
            zero_cohort,
            name=f"locked_zero_cohorts[{index}]",
            required={"attempt_id", "capture_root", "outcome"},
            optional={"decision_at_ms", "history_capture_roots"},
        )
        attempt_id = _text(
            zero_cohort["attempt_id"],
            name=f"locked_zero_cohorts[{index}].attempt_id",
        )
        if attempt_id in source_evidence_sha256_by_attempt:
            raise ValueError("attempt ids must be unique across all report rows")
        admission = audit_admissions.get(attempt_id)
        outcome_receipt = audit_outcomes.get(attempt_id)
        if not isinstance(admission, Mapping) or not isinstance(
            outcome_receipt,
            Mapping,
        ):
            raise TypeError("locked zero cohort is absent from the rolling audit")
        zero_outcome = _text(
            zero_cohort["outcome"],
            name=f"locked_zero_cohorts[{index}].outcome",
        )
        locked_zero = _verify_zero_cohort(
            cohort=zero_cohort,
            admission=admission,
            outcome_receipt=outcome_receipt,
        )
        locked_action_sha256 = None
        if zero_outcome == "no_trade":
            locked_action_sha256 = hashlib.sha256(
                canonical_json_bytes(
                    _no_trade_locked_action_document(
                        attempt_id=attempt_id,
                        decision_at_ms=locked_zero.decision_at_ms,
                    )
                )
            ).hexdigest()
            locked_action_payload_sha256_by_attempt[attempt_id] = locked_action_sha256
        source_evidence_sha256_by_attempt[attempt_id] = (
            locked_zero.source_evidence_sha256
        )
        locked_zero_cohorts.append(
            replace(
                locked_zero,
                locked_action_payload_sha256=locked_action_sha256,
            )
        )
    verified_bundle = _verified_finalized_capture_bundle(
        capture_verification_by_attempt=capture_verification_by_attempt,
        locked_action_payload_sha256_by_attempt=locked_action_payload_sha256_by_attempt,
        source_evidence_sha256_by_attempt=source_evidence_sha256_by_attempt,
        audit_binding=_bind_rolling_audit(
            tuple(plans),
            tuple(locked_zero_cohorts),
            capture_verification_by_attempt=capture_verification_by_attempt,
            rolling_audit=rolling_audit,
            preregistration_sha256=preregistration_sha256,
            locked_action_payload_sha256_by_attempt=locked_action_payload_sha256_by_attempt,
            source_evidence_sha256_by_attempt=source_evidence_sha256_by_attempt,
        ),
    )
    report = build_structural_shadow_report(
        tuple(plans),
        locked_zero_cohorts=tuple(locked_zero_cohorts),
        capture_verification_by_attempt=capture_verification_by_attempt,
        public_events_by_attempt=public_events_by_attempt,
        neutral_disposition=_text(
            document["neutral_disposition"], name="neutral_disposition"
        ),
        rolling_window_size=_integer(
            document["rolling_window_size"], name="rolling_window_size"
        ),
        minimum_expiries=_integer(
            document["minimum_expiries"], name="minimum_expiries"
        ),
        rolling_audit=rolling_audit,
        source_input_sha256=hashlib.sha256(canonical_json_bytes(document)).hexdigest(),
        preregistration_sha256=preregistration_sha256,
        locked_action_payload_sha256_by_attempt=(
            locked_action_payload_sha256_by_attempt
        ),
        source_evidence_sha256_by_attempt=source_evidence_sha256_by_attempt,
        verified_finalized_bundle=verified_bundle,
        hedge_books_by_attempt=hedge_books_by_attempt,
        unwind_books_by_attempt=unwind_books_by_attempt,
    )
    built = report.to_document()
    if built["schema_version"] != STRUCTURAL_SHADOW_REPORT_SCHEMA:
        raise RuntimeError("report schema_version changed unexpectedly")
    return built


def _write_report_atomic(output_dir: Path, report: Mapping[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    digest = str(report["report_sha256"])
    target = output_dir / f"structural-shadow-report-{digest}.json"
    encoded = canonical_json_bytes(report) + b"\n"
    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".tmp",
        prefix="structural-shadow-",
        dir=output_dir,
        delete=False,
    ) as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, target)
    _fsync_directory(output_dir)
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--journal-root", type=Path, required=True)
    parser.add_argument("--preregistration", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    rolling_audit = audit_rolling_shadow(args.journal_root)
    preregistration_sha256 = hashlib.sha256(
        args.preregistration.read_bytes()
    ).hexdigest()
    report = build_report_from_payload(
        payload,
        rolling_audit=rolling_audit,
        preregistration_sha256=preregistration_sha256,
    )
    path = _write_report_atomic(args.output_dir, report)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
