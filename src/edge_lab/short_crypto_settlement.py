"""Evaluation-only settlement labels for short Chainlink crypto markets.

This module consumes immutable public Gamma snapshots after capture.  It does
not produce trading features.  A label is emitted only when a Gamma market is
closed, its outcome prices are exactly one-hot, and every market identity
field still matches the catalog target.  Optional Chainlink boundary values
provide an independent contract-rule cross-check.

The raw Gamma shape supported here mirrors the recorder's finalized
``gamma_http`` records: ``payload.payload.responses[*].raw_json.markets``.
Direct market responses and list responses are also accepted for target-
specific capture workers, but their enclosing recorder record remains
content-addressed and mandatory.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from .chainlink_ptb import FinalizedChainlinkBoundaries
from .data_store import canonical_record_id
from .short_crypto_catalog import ShortCryptoTarget


SettlementStatus = Literal["resolved", "pending", "missing", "conflict"]
StrictSettlementState = Literal[
    "PENDING",
    "SETTLED",
    "EXCLUDED",
    "settlement_conflict",
]
_RECORD_ID = re.compile(r"^[0-9a-f]{64}$")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class SettlementRejection(ValueError):
    """A raw settlement input violated an immutable capture invariant."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ChainlinkBoundaryObservation:
    """One exact Chainlink value at a target's opening or closing boundary."""

    record_id: str
    symbol: str
    event_at_ms: int
    received_at_ms: int
    price: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.record_id, str) or _RECORD_ID.fullmatch(
            self.record_id
        ) is None:
            raise ValueError("record_id must be a lowercase 64-byte hex digest")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be non-empty")
        object.__setattr__(self, "symbol", self.symbol.strip().lower())
        for name in ("event_at_ms", "received_at_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.received_at_ms < self.event_at_ms:
            raise ValueError("received_at_ms cannot precede event_at_ms")
        if isinstance(self.price, float) or isinstance(self.price, bool):
            raise ValueError("price must be a Decimal, integer, or decimal string")
        try:
            price = (
                self.price
                if isinstance(self.price, Decimal)
                else Decimal(str(self.price))
            )
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("price must be a finite decimal") from exc
        if not price.is_finite() or price <= _ZERO:
            raise ValueError("price must be a positive finite decimal")
        object.__setattr__(self, "price", price)


@dataclass(frozen=True)
class ClosePongObservation:
    """One captured CLOB PONG used to bracket a market close."""

    record_id: str
    session_id: str
    connection_id: str
    received_at_ms: int
    raw_frame: str = "PONG"


@dataclass(frozen=True)
class CloseLivenessProof:
    """Before/after PONG observations offered to the strict settlement gate."""

    before: ClosePongObservation
    after: ClosePongObservation
    tolerance_ms: int


@dataclass(frozen=True)
class SettlementCommitReceipt:
    """Persistence boundary receipt for one finalized settlement commit."""

    record_id: str
    durable_finalized_record_ids: frozenset[str]


@dataclass(frozen=True)
class ShortCryptoSettlementLabel:
    """A post-close label, intentionally separate from decision features."""

    slug: str
    condition_id: str
    outcome: Literal["Up", "Down"]
    winning_token_id: str
    gamma_updated_at_ms: int
    gamma_received_at_ms: int
    gamma_record_id: str
    chainlink_open_record_id: str | None
    chainlink_close_record_id: str | None
    chainlink_open_price: Decimal | None
    chainlink_close_price: Decimal | None
    available_at_ms: int


@dataclass(frozen=True)
class SettlementReconciliation:
    """Fail-closed reconciliation state for one immutable catalog target."""

    status: SettlementStatus
    target_slug: str
    target_condition_id: str
    target_rule_hash: str
    label: ShortCryptoSettlementLabel | None
    reason_codes: tuple[str, ...]
    gamma_record_ids: tuple[str, ...]

    def evaluation_label_as_of(
        self,
        as_of_ms: int,
    ) -> Literal["Up", "Down"] | None:
        """Return the label only after it was observable, for evaluation.

        This is deliberately named for evaluation rather than features.  A
        replay decision must use data captured before its decision timestamp,
        never this post-close object.
        """

        if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int):
            raise ValueError("as_of_ms must be an integer")
        if as_of_ms < 0:
            raise ValueError("as_of_ms cannot be negative")
        if (
            self.status != "resolved"
            or self.label is None
            or as_of_ms < self.label.available_at_ms
        ):
            return None
        return self.label.outcome

    def decision_safe_identity(self) -> dict[str, str]:
        """Return target identity only; settlement fields are excluded."""

        return {
            "slug": self.target_slug,
            "condition_id": self.target_condition_id,
            "rule_hash": self.target_rule_hash,
        }


@dataclass(frozen=True)
class StrictSettlementSnapshot:
    """Externally observable state of the atomic strict settlement gate."""

    state: StrictSettlementState
    reason_codes: tuple[str, ...]
    label: ShortCryptoSettlementLabel | None
    required_record_ids: tuple[str, ...] = ()
    commit_record_id: str | None = None


class AtomicSettlementGate:
    """Keep a target non-settled until every proof is durably committed."""

    def __init__(self, target: ShortCryptoTarget) -> None:
        self._target = target
        self._lock = asyncio.Lock()
        self._snapshot = StrictSettlementSnapshot(
            state="PENDING",
            reason_codes=("settlement_not_evaluated",),
            label=None,
        )

    @property
    def snapshot(self) -> StrictSettlementSnapshot:
        return self._snapshot

    async def reconcile_and_commit(
        self,
        *,
        reconciliation: SettlementReconciliation,
        boundaries: FinalizedChainlinkBoundaries | None,
        close_liveness: CloseLivenessProof | None,
        now_ms: int,
        settlement_deadline_ms: int,
        persist: Callable[
            [dict[str, object]],
            Awaitable[SettlementCommitReceipt],
        ],
    ) -> StrictSettlementSnapshot:
        """Assess and durably commit one settlement without transient SETTLED."""

        async with self._lock:
            if self._snapshot.state in {
                "SETTLED",
                "EXCLUDED",
                "settlement_conflict",
            }:
                return self._snapshot
            if (
                isinstance(now_ms, bool)
                or not isinstance(now_ms, int)
                or isinstance(settlement_deadline_ms, bool)
                or not isinstance(settlement_deadline_ms, int)
                or settlement_deadline_ms < self._target.closes_at_ms
            ):
                raise ValueError(
                    "settlement times must be integer milliseconds and "
                    "deadline cannot precede target close"
                )
            if reconciliation.status == "conflict":
                self._snapshot = StrictSettlementSnapshot(
                    state="settlement_conflict",
                    reason_codes=reconciliation.reason_codes,
                    label=None,
                )
                return self._snapshot
            if boundaries is None:
                state: StrictSettlementState = (
                    "EXCLUDED"
                    if now_ms >= settlement_deadline_ms
                    else "PENDING"
                )
                reasons = ("chainlink_boundary_evidence_missing",)
                if state == "EXCLUDED":
                    reasons = (*reasons, "settlement_deadline_elapsed")
                self._snapshot = StrictSettlementSnapshot(
                    state=state,
                    reason_codes=reasons,
                    label=None,
                )
                return self._snapshot
            if reconciliation.status != "resolved" or reconciliation.label is None:
                state = (
                    "EXCLUDED"
                    if now_ms >= settlement_deadline_ms
                    else "PENDING"
                )
                reasons = reconciliation.reason_codes
                if state == "EXCLUDED":
                    reasons = (*reasons, "settlement_deadline_elapsed")
                self._snapshot = StrictSettlementSnapshot(
                    state=state,
                    reason_codes=reasons,
                    label=None,
                )
                return self._snapshot
            label = reconciliation.label
            expected_winning_token_id = {
                "Up": self._target.up_token_id,
                "Down": self._target.down_token_id,
            }.get(label.outcome)
            if (
                reconciliation.target_slug != self._target.slug
                or reconciliation.target_condition_id
                != self._target.condition_id
                or reconciliation.target_rule_hash != self._target.rule_hash
                or label.slug != self._target.slug
                or label.condition_id != self._target.condition_id
                or label.winning_token_id != expected_winning_token_id
                or _RECORD_ID.fullmatch(label.gamma_record_id) is None
                or label.gamma_record_id
                not in reconciliation.gamma_record_ids
                or any(
                    _RECORD_ID.fullmatch(record_id) is None
                    for record_id in reconciliation.gamma_record_ids
                )
                or _RECORD_ID.fullmatch(
                    self._target.announcement_record_id
                )
                is None
            ):
                self._snapshot = StrictSettlementSnapshot(
                    state="settlement_conflict",
                    reason_codes=("settlement_target_identity_conflict",),
                    label=None,
                )
                return self._snapshot
            expected_rule_url = (
                "https://data.chain.link/streams/"
                f"{self._target.source_symbol.split('/', 1)[0]}-usd"
            )
            open_boundary = boundaries.open_boundary
            close_boundary = boundaries.close_boundary
            open_boundary_received_at_ms = _timestamp_ms(
                open_boundary.received_at,
                code="chainlink_boundary_time_invalid",
                label="Chainlink open received_at",
            )
            close_boundary_received_at_ms = _timestamp_ms(
                close_boundary.received_at,
                code="chainlink_boundary_time_invalid",
                label="Chainlink close received_at",
            )
            boundary_identity_conflict = (
                open_boundary.boundary_role != "open"
                or close_boundary.boundary_role != "close"
                or open_boundary.source_topic != self._target.source_topic
                or close_boundary.source_topic != self._target.source_topic
                or open_boundary.source_symbol != self._target.source_symbol
                or close_boundary.source_symbol != self._target.source_symbol
                or open_boundary.rule_url != expected_rule_url
                or close_boundary.rule_url != expected_rule_url
                or open_boundary.rule_hash != self._target.rule_hash
                or close_boundary.rule_hash != self._target.rule_hash
                or open_boundary.inner_timestamp_ms
                != self._target.opens_at_ms
                or close_boundary.inner_timestamp_ms
                != self._target.closes_at_ms
                or open_boundary_received_at_ms
                < open_boundary.inner_timestamp_ms
                or close_boundary_received_at_ms
                < close_boundary.inner_timestamp_ms
                or _RECORD_ID.fullmatch(open_boundary.record_id) is None
                or _RECORD_ID.fullmatch(close_boundary.record_id) is None
                or open_boundary.record_id == close_boundary.record_id
                or label.chainlink_open_record_id
                not in {None, open_boundary.record_id}
                or label.chainlink_close_record_id
                not in {None, close_boundary.record_id}
                or label.chainlink_open_price
                not in {None, open_boundary.price}
                or label.chainlink_close_price
                not in {None, close_boundary.price}
                or any(
                    not isinstance(boundary.session_id, str)
                    or not boundary.session_id.strip()
                    or not isinstance(boundary.connection_id, str)
                    or not boundary.connection_id.strip()
                    or not isinstance(boundary.line_number, int)
                    or isinstance(boundary.line_number, bool)
                    or boundary.line_number <= 0
                    or _RECORD_ID.fullmatch(
                        boundary.source_manifest_sha256
                    )
                    is None
                    or _RECORD_ID.fullmatch(boundary.raw_sha256) is None
                    or not boundary.source_manifest_path.endswith(
                        ".manifest.json"
                    )
                    or not boundary.raw_path.endswith(".jsonl")
                    or boundary.raw_path.endswith(".jsonl.partial")
                    for boundary in (open_boundary, close_boundary)
                )
            )
            if boundary_identity_conflict:
                self._snapshot = StrictSettlementSnapshot(
                    state="settlement_conflict",
                    reason_codes=("chainlink_boundary_identity_conflict",),
                    label=None,
                )
                return self._snapshot
            if (
                open_boundary.retrospective
                or not open_boundary.available_at_first_decision
            ):
                state = (
                    "EXCLUDED"
                    if now_ms >= settlement_deadline_ms
                    else "PENDING"
                )
                reasons = ("chainlink_open_retrospective",)
                if state == "EXCLUDED":
                    reasons = (*reasons, "settlement_deadline_elapsed")
                self._snapshot = StrictSettlementSnapshot(
                    state=state,
                    reason_codes=reasons,
                    label=None,
                )
                return self._snapshot
            if close_liveness is None:
                state = (
                    "EXCLUDED"
                    if now_ms >= settlement_deadline_ms
                    else "PENDING"
                )
                reasons = ("clob_close_liveness_missing",)
                if state == "EXCLUDED":
                    reasons = (*reasons, "settlement_deadline_elapsed")
                self._snapshot = StrictSettlementSnapshot(
                    state=state,
                    reason_codes=reasons,
                    label=None,
                )
                return self._snapshot

            if (
                close_liveness.before.raw_frame != "PONG"
                or close_liveness.after.raw_frame != "PONG"
                or isinstance(close_liveness.tolerance_ms, bool)
                or not isinstance(close_liveness.tolerance_ms, int)
                or close_liveness.tolerance_ms <= 0
                or isinstance(
                    close_liveness.before.received_at_ms,
                    bool,
                )
                or not isinstance(
                    close_liveness.before.received_at_ms,
                    int,
                )
                or isinstance(
                    close_liveness.after.received_at_ms,
                    bool,
                )
                or not isinstance(
                    close_liveness.after.received_at_ms,
                    int,
                )
                or close_liveness.before.received_at_ms
                > self._target.closes_at_ms
                or close_liveness.after.received_at_ms
                < self._target.closes_at_ms
                or (
                    self._target.closes_at_ms
                    - close_liveness.before.received_at_ms
                    > close_liveness.tolerance_ms
                )
                or (
                    close_liveness.after.received_at_ms
                    - self._target.closes_at_ms
                    > close_liveness.tolerance_ms
                )
                or _RECORD_ID.fullmatch(
                    close_liveness.before.record_id
                )
                is None
                or _RECORD_ID.fullmatch(
                    close_liveness.after.record_id
                )
                is None
                or close_liveness.before.record_id
                == close_liveness.after.record_id
            ):
                state = (
                    "EXCLUDED"
                    if now_ms >= settlement_deadline_ms
                    else "PENDING"
                )
                reasons = ("clob_close_liveness_missing",)
                if state == "EXCLUDED":
                    reasons = (*reasons, "settlement_deadline_elapsed")
                self._snapshot = StrictSettlementSnapshot(
                    state=state,
                    reason_codes=reasons,
                    label=None,
                )
                return self._snapshot

            if (
                not isinstance(close_liveness.before.connection_id, str)
                or not close_liveness.before.connection_id.strip()
                or not isinstance(close_liveness.after.connection_id, str)
                or not close_liveness.after.connection_id.strip()
                or not isinstance(close_liveness.before.session_id, str)
                or not close_liveness.before.session_id.strip()
                or not isinstance(close_liveness.after.session_id, str)
                or not close_liveness.after.session_id.strip()
                or close_liveness.before.connection_id
                != close_liveness.after.connection_id
                or close_liveness.before.session_id
                != close_liveness.after.session_id
            ):
                self._snapshot = StrictSettlementSnapshot(
                    state="settlement_conflict",
                    reason_codes=("clob_close_pong_connection_conflict",),
                    label=None,
                )
                return self._snapshot

            evidence_available_at_ms = max(
                self._target.closes_at_ms,
                label.available_at_ms,
                open_boundary_received_at_ms,
                close_boundary_received_at_ms,
                close_liveness.before.received_at_ms,
                close_liveness.after.received_at_ms,
            )
            if now_ms < evidence_available_at_ms:
                state = (
                    "EXCLUDED"
                    if now_ms >= settlement_deadline_ms
                    else "PENDING"
                )
                reasons = ("settlement_evidence_not_yet_available",)
                if state == "EXCLUDED":
                    reasons = (*reasons, "settlement_deadline_elapsed")
                self._snapshot = StrictSettlementSnapshot(
                    state=state,
                    reason_codes=reasons,
                    label=None,
                )
                return self._snapshot
            chainlink_outcome: Literal["Up", "Down"] = (
                "Up"
                if boundaries.close_boundary.price
                >= boundaries.open_boundary.price
                else "Down"
            )
            if chainlink_outcome != label.outcome:
                self._snapshot = StrictSettlementSnapshot(
                    state="settlement_conflict",
                    reason_codes=("gamma_chainlink_label_conflict",),
                    label=None,
                )
                return self._snapshot
            required_record_ids = (
                self._target.announcement_record_id,
                label.gamma_record_id,
                boundaries.open_boundary.record_id,
                boundaries.close_boundary.record_id,
                close_liveness.before.record_id,
                close_liveness.after.record_id,
            )
            payload: dict[str, object] = {
                "schema_version": "edge-lab.short-crypto-settlement-commit.v1",
                "transition": "commit_settlement",
                "slug": self._target.slug,
                "condition_id": self._target.condition_id,
                "rule_hash": self._target.rule_hash,
                "outcome": label.outcome,
                "winning_token_id": label.winning_token_id,
                "chainlink_open_price": str(
                    boundaries.open_boundary.price
                ),
                "chainlink_close_price": str(
                    boundaries.close_boundary.price
                ),
                "required_record_ids": list(required_record_ids),
            }
            try:
                receipt = await persist(payload)
            except asyncio.CancelledError:
                self._snapshot = StrictSettlementSnapshot(
                    state="PENDING",
                    reason_codes=("settlement_persistence_cancelled",),
                    label=None,
                    required_record_ids=required_record_ids,
                )
                raise
            except Exception:
                self._snapshot = StrictSettlementSnapshot(
                    state="PENDING",
                    reason_codes=("settlement_persistence_failed",),
                    label=None,
                    required_record_ids=required_record_ids,
                )
                raise
            if (
                not isinstance(receipt, SettlementCommitReceipt)
                or _RECORD_ID.fullmatch(receipt.record_id) is None
                or receipt.record_id
                not in receipt.durable_finalized_record_ids
                or not set(required_record_ids).issubset(
                    receipt.durable_finalized_record_ids
                )
            ):
                self._snapshot = StrictSettlementSnapshot(
                    state="PENDING",
                    reason_codes=("settlement_commit_not_finalized",),
                    label=None,
                    required_record_ids=required_record_ids,
                )
                raise SettlementRejection(
                    "settlement_commit_not_finalized",
                    "persistence receipt does not finalize every required record",
                )
            self._snapshot = StrictSettlementSnapshot(
                state="SETTLED",
                reason_codes=(),
                label=label,
                required_record_ids=required_record_ids,
                commit_record_id=receipt.record_id,
            )
            return self._snapshot


@dataclass(frozen=True)
class _GammaObservation:
    record_id: str
    received_at_ms: int
    market: Mapping[str, Any]


def _timestamp_ms(value: Any, *, code: str, label: str) -> int:
    if not isinstance(value, str) or not value.strip():
        raise SettlementRejection(code, f"{label} must be an ISO-8601 timestamp")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(
            f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError as exc:
        raise SettlementRejection(
            code,
            f"{label} must be an ISO-8601 timestamp",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SettlementRejection(code, f"{label} must include a timezone")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000)


def _sequence(value: Any) -> Sequence[Any] | None:
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return value
    return None


def _json_array(value: Any) -> list[Any] | None:
    candidate = value
    if isinstance(value, str):
        try:
            candidate = json.loads(value)
        except json.JSONDecodeError:
            return None
    items = _sequence(candidate)
    return None if items is None else list(items)


def _string_array(value: Any) -> tuple[str, ...] | None:
    items = _json_array(value)
    if items is None or any(not isinstance(item, str) for item in items):
        return None
    return tuple(items)


def _outcome_prices(value: Any) -> tuple[Decimal, Decimal] | None:
    items = _json_array(value)
    if items is None or len(items) != 2:
        return None
    prices: list[Decimal] = []
    for item in items:
        if isinstance(item, (float, bool)) or not isinstance(
            item, (str, int, Decimal)
        ):
            return None
        try:
            price = item if isinstance(item, Decimal) else Decimal(str(item))
        except (InvalidOperation, ValueError):
            return None
        if not price.is_finite():
            return None
        prices.append(price)
    return prices[0], prices[1]


def _markets_from_raw_json(raw_json: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(raw_json, Mapping):
        if "markets" in raw_json:
            candidates = _sequence(raw_json.get("markets"))
            if candidates is None:
                raise SettlementRejection(
                    "gamma_schema_mismatch",
                    "raw_json.markets must be an array",
                )
        elif {"id", "slug", "conditionId"} <= set(raw_json):
            candidates = (raw_json,)
        else:
            return ()
    else:
        candidates = _sequence(raw_json)
        if candidates is None:
            raise SettlementRejection(
                "gamma_schema_mismatch",
                "Gamma raw_json must be a market, market array, or markets object",
            )
    markets: list[Mapping[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise SettlementRejection(
                "gamma_schema_mismatch",
                "every Gamma market must be an object",
            )
        markets.append(candidate)
    return tuple(markets)


def _gamma_observations(
    records: Iterable[Mapping[str, Any]],
) -> tuple[_GammaObservation, ...]:
    observations: list[_GammaObservation] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise SettlementRejection(
                "gamma_schema_mismatch",
                "Gamma record must be an object",
            )
        record_id = record.get("record_id")
        if (
            not isinstance(record_id, str)
            or _RECORD_ID.fullmatch(record_id) is None
            or record_id != canonical_record_id(record)
        ):
            raise SettlementRejection(
                "record_id_mismatch",
                "Gamma record_id does not match its canonical envelope",
            )
        if record.get("source") != "gamma_http":
            raise SettlementRejection(
                "gamma_source_mismatch",
                "settlement record must come from gamma_http",
            )
        received_at_ms = _timestamp_ms(
            record.get("received_at"),
            code="gamma_time_invalid",
            label="Gamma received_at",
        )
        recorder_payload = record.get("payload")
        if not isinstance(recorder_payload, Mapping):
            raise SettlementRejection(
                "gamma_schema_mismatch",
                "Gamma recorder payload must be an object",
            )
        if recorder_payload.get("source") != "gamma_http":
            raise SettlementRejection(
                "gamma_source_mismatch",
                "inner Gamma source must equal gamma_http",
            )
        inner_received = recorder_payload.get("received_at")
        if inner_received is not None and _timestamp_ms(
            inner_received,
            code="gamma_time_invalid",
            label="inner Gamma received_at",
        ) != received_at_ms:
            raise SettlementRejection(
                "gamma_time_invalid",
                "inner and outer Gamma received_at must match",
            )
        if recorder_payload.get("kind") in {"diagnostic", "lifecycle"}:
            continue
        payload = recorder_payload.get("payload")
        if not isinstance(payload, Mapping):
            raise SettlementRejection(
                "gamma_schema_mismatch",
                "Gamma response payload must be an object",
            )
        responses = _sequence(payload.get("responses"))
        if responses is None:
            raise SettlementRejection(
                "gamma_schema_mismatch",
                "Gamma response payload must contain responses",
            )
        for response in responses:
            if not isinstance(response, Mapping):
                raise SettlementRejection(
                    "gamma_schema_mismatch",
                    "Gamma response entry must be an object",
                )
            if response.get("resource") not in {"gamma_market", "gamma_markets"}:
                continue
            for market in _markets_from_raw_json(response.get("raw_json")):
                observations.append(
                    _GammaObservation(
                        record_id=record_id,
                        received_at_ms=received_at_ms,
                        market=market,
                    )
                )
    return tuple(observations)


def _identity_candidate(
    target: ShortCryptoTarget,
    market: Mapping[str, Any],
) -> bool:
    token_ids = _string_array(market.get("clobTokenIds"))
    return (
        market.get("slug") == target.slug
        or str(market.get("id", "")) == target.market_id
        or (
            isinstance(market.get("conditionId"), str)
            and market["conditionId"].lower() == target.condition_id.lower()
        )
        or (
            token_ids is not None
            and (
                target.up_token_id in token_ids
                or target.down_token_id in token_ids
            )
        )
    )


def _identity_matches(
    target: ShortCryptoTarget,
    market: Mapping[str, Any],
) -> bool:
    outcomes = _string_array(market.get("outcomes"))
    token_ids = _string_array(market.get("clobTokenIds"))
    condition_id = market.get("conditionId")
    return (
        market.get("slug") == target.slug
        and str(market.get("id", "")) == target.market_id
        and isinstance(condition_id, str)
        and condition_id.lower() == target.condition_id.lower()
        and outcomes == ("Up", "Down")
        and token_ids == (target.up_token_id, target.down_token_id)
    )


def _result(
    target: ShortCryptoTarget,
    *,
    status: SettlementStatus,
    label: ShortCryptoSettlementLabel | None = None,
    reasons: tuple[str, ...] = (),
    record_ids: tuple[str, ...] = (),
) -> SettlementReconciliation:
    return SettlementReconciliation(
        status=status,
        target_slug=target.slug,
        target_condition_id=target.condition_id,
        target_rule_hash=target.rule_hash,
        label=label,
        reason_codes=reasons,
        gamma_record_ids=record_ids,
    )


def _unique_record_ids(
    observations: Iterable[_GammaObservation],
) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.record_id for item in observations))


def _chainlink_conflict(
    target: ShortCryptoTarget,
    matching: tuple[_GammaObservation, ...],
    code: str,
) -> SettlementReconciliation:
    return _result(
        target,
        status="conflict",
        reasons=(code,),
        record_ids=_unique_record_ids(matching),
    )


def reconcile_short_crypto_settlement(
    target: ShortCryptoTarget,
    gamma_records: Iterable[Mapping[str, Any]],
    *,
    chainlink_open: ChainlinkBoundaryObservation | None = None,
    chainlink_close: ChainlinkBoundaryObservation | None = None,
) -> SettlementReconciliation:
    """Reconcile one target's public post-close label, without look-ahead.

    ``missing`` means no Gamma market shared any target identity. ``pending``
    means the bound market was observed but wasn't closed. ``conflict`` means
    identity, time, terminal prices, or independent Chainlink evidence
    disagreed.  Every non-resolved state carries no label.
    """

    observations = _gamma_observations(gamma_records)
    matching = tuple(
        observation
        for observation in observations
        if _identity_candidate(target, observation.market)
    )
    if not matching:
        return _result(
            target,
            status="missing",
            reasons=("gamma_not_found",),
        )
    record_ids = _unique_record_ids(matching)
    if any(
        not _identity_matches(target, observation.market)
        for observation in matching
    ):
        return _result(
            target,
            status="conflict",
            reasons=("gamma_identity_conflict",),
            record_ids=record_ids,
        )

    ordered = tuple(
        sorted(
            matching,
            key=lambda observation: (
                observation.received_at_ms,
                observation.record_id,
            ),
        )
    )
    terminal: list[
        tuple[
            Literal["Up", "Down"],
            _GammaObservation,
            int,
        ]
    ] = []
    terminal_seen_at: int | None = None
    for observation in ordered:
        closed = observation.market.get("closed")
        if not isinstance(closed, bool):
            return _result(
                target,
                status="conflict",
                reasons=("gamma_closed_flag_invalid",),
                record_ids=record_ids,
            )
        if not closed:
            if (
                terminal_seen_at is not None
                and observation.received_at_ms >= terminal_seen_at
            ):
                return _result(
                    target,
                    status="conflict",
                    reasons=("gamma_terminal_state_regression",),
                    record_ids=record_ids,
                )
            continue
        prices = _outcome_prices(observation.market.get("outcomePrices"))
        if prices == (_ONE, _ZERO):
            outcome: Literal["Up", "Down"] = "Up"
        elif prices == (_ZERO, _ONE):
            outcome = "Down"
        else:
            return _result(
                target,
                status="conflict",
                reasons=("gamma_terminal_prices_not_one_hot",),
                record_ids=record_ids,
            )
        updated_at_ms = _timestamp_ms(
            observation.market.get("updatedAt"),
            code="gamma_terminal_time_invalid",
            label="Gamma updatedAt",
        )
        if (
            observation.received_at_ms < target.closes_at_ms
            or updated_at_ms < target.closes_at_ms
        ):
            return _result(
                target,
                status="conflict",
                reasons=("gamma_terminal_time_conflict",),
                record_ids=record_ids,
            )
        terminal.append((outcome, observation, updated_at_ms))
        terminal_seen_at = observation.received_at_ms

    if not terminal:
        return _result(
            target,
            status="pending",
            reasons=("gamma_not_closed",),
            record_ids=record_ids,
        )
    labels = {outcome for outcome, _, _ in terminal}
    if len(labels) != 1:
        return _result(
            target,
            status="conflict",
            reasons=("gamma_terminal_label_conflict",),
            record_ids=record_ids,
        )
    gamma_outcome, gamma_observation, gamma_updated_at_ms = terminal[0]

    if (chainlink_open is None) != (chainlink_close is None):
        return _chainlink_conflict(
            target,
            matching,
            "chainlink_pair_incomplete",
        )
    chainlink_outcome: Literal["Up", "Down"] | None = None
    if chainlink_open is not None and chainlink_close is not None:
        expected_symbol = target.source_symbol.lower()
        if (
            chainlink_open.symbol != expected_symbol
            or chainlink_close.symbol != expected_symbol
        ):
            return _chainlink_conflict(
                target,
                matching,
                "chainlink_symbol_conflict",
            )
        if chainlink_open.event_at_ms != target.opens_at_ms:
            return _chainlink_conflict(
                target,
                matching,
                "chainlink_open_time_conflict",
            )
        if chainlink_close.event_at_ms != target.closes_at_ms:
            return _chainlink_conflict(
                target,
                matching,
                "chainlink_close_time_conflict",
            )
        chainlink_outcome = (
            "Up"
            if chainlink_close.price >= chainlink_open.price
            else "Down"
        )
        if chainlink_outcome != gamma_outcome:
            return _result(
                target,
                status="conflict",
                reasons=("gamma_chainlink_label_conflict",),
                record_ids=record_ids,
            )

    winning_token_id = (
        target.up_token_id
        if gamma_outcome == "Up"
        else target.down_token_id
    )
    available_at_ms = gamma_observation.received_at_ms
    if chainlink_open is not None and chainlink_close is not None:
        available_at_ms = max(
            available_at_ms,
            chainlink_open.received_at_ms,
            chainlink_close.received_at_ms,
        )
    label = ShortCryptoSettlementLabel(
        slug=target.slug,
        condition_id=target.condition_id,
        outcome=gamma_outcome,
        winning_token_id=winning_token_id,
        gamma_updated_at_ms=gamma_updated_at_ms,
        gamma_received_at_ms=gamma_observation.received_at_ms,
        gamma_record_id=gamma_observation.record_id,
        chainlink_open_record_id=(
            None if chainlink_open is None else chainlink_open.record_id
        ),
        chainlink_close_record_id=(
            None if chainlink_close is None else chainlink_close.record_id
        ),
        chainlink_open_price=(
            None if chainlink_open is None else chainlink_open.price
        ),
        chainlink_close_price=(
            None if chainlink_close is None else chainlink_close.price
        ),
        available_at_ms=available_at_ms,
    )
    return _result(
        target,
        status="resolved",
        label=label,
        record_ids=record_ids,
    )
