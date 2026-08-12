"""Causal RTDS--CLOB lead/lag and execution-friction research.

The module is intentionally offline and credential-free.  Its central rule is
that an event cannot affect a decision before ``received_at_ms`` even when the
event carries an earlier exchange/server timestamp.  Corrected server times
are useful for age and lead/lag measurement; they never grant earlier
availability.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Sequence


ZERO = Decimal("0")
ONE = Decimal("1")


def _decimal(value: object, *, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # pragma: no cover - Decimal errors vary
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _timestamp(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer millisecond timestamp")
    return value


def _median(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("at least one value is required")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


@dataclass(frozen=True)
class ClockProbe:
    """One NTP-style observation of a remote server clock."""

    trace_ref: str
    client_sent_at_ms: int
    server_time_ms: int
    client_received_at_ms: int

    def __post_init__(self) -> None:
        if not self.trace_ref.strip():
            raise ValueError("trace_ref must be non-empty")
        _timestamp(self.client_sent_at_ms, name="client_sent_at_ms")
        _timestamp(self.server_time_ms, name="server_time_ms")
        _timestamp(self.client_received_at_ms, name="client_received_at_ms")
        if self.client_received_at_ms < self.client_sent_at_ms:
            raise ValueError("client_received_at_ms cannot precede client_sent_at_ms")

    @property
    def rtt_ms(self) -> Decimal:
        return Decimal(self.client_received_at_ms - self.client_sent_at_ms)

    @property
    def offset_ms(self) -> Decimal:
        client_midpoint = (
            Decimal(self.client_sent_at_ms)
            + Decimal(self.client_received_at_ms)
        ) / Decimal("2")
        return Decimal(self.server_time_ms) - client_midpoint


@dataclass(frozen=True)
class ClockEstimate:
    """Robust remote-minus-local clock estimate and network uncertainty."""

    offset_ms: Decimal
    rtt_ms: Decimal
    uncertainty_ms: Decimal
    sample_count: int
    trace_refs: tuple[str, ...]
    observed_at_ms: int
    valid_for_ms: int = 60_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "offset_ms", _decimal(self.offset_ms, name="offset_ms"))
        object.__setattr__(self, "rtt_ms", _decimal(self.rtt_ms, name="rtt_ms"))
        object.__setattr__(
            self,
            "uncertainty_ms",
            _decimal(self.uncertainty_ms, name="uncertainty_ms"),
        )
        if self.rtt_ms < ZERO or self.uncertainty_ms < ZERO:
            raise ValueError("RTT and uncertainty cannot be negative")
        if self.sample_count <= 0 or self.sample_count != len(self.trace_refs):
            raise ValueError("sample_count must match trace_refs")
        _timestamp(self.observed_at_ms, name="observed_at_ms")
        if (
            isinstance(self.valid_for_ms, bool)
            or not isinstance(self.valid_for_ms, int)
            or self.valid_for_ms < 0
        ):
            raise ValueError("valid_for_ms must be a non-negative integer")

    def local_time(self, server_time_ms: int) -> Decimal:
        """Map a remote timestamp onto the recorder's local clock."""

        _timestamp(server_time_ms, name="server_time_ms")
        return Decimal(server_time_ms) - self.offset_ms

    def local_time_bounds(self, server_time_ms: int) -> tuple[Decimal, Decimal]:
        center = self.local_time(server_time_ms)
        return center - self.uncertainty_ms, center + self.uncertainty_ms

    def valid_at(self, timestamp_ms: int) -> bool:
        _timestamp(timestamp_ms, name="timestamp_ms")
        return (
            self.observed_at_ms <= timestamp_ms
            and timestamp_ms - self.observed_at_ms <= self.valid_for_ms
        )


def estimate_clock(
    probes: Iterable[ClockProbe],
    *,
    as_of_ms: Optional[int] = None,
    valid_for_ms: int = 60_000,
) -> ClockEstimate:
    """Estimate clock offset from probe midpoints without assuming zero RTT.

    Median offset and RTT avoid letting a single slow probe dictate the
    alignment.  ``uncertainty_ms`` includes half the median RTT plus the median
    absolute offset deviation.
    """

    all_observations = tuple(probes)
    if not all_observations:
        raise ValueError("at least one clock probe is required")
    if as_of_ms is None:
        as_of_ms = max(probe.client_received_at_ms for probe in all_observations)
    _timestamp(as_of_ms, name="as_of_ms")
    observations = tuple(
        probe
        for probe in all_observations
        if probe.client_received_at_ms <= as_of_ms
    )
    if not observations:
        raise ValueError("no clock probe was received by as_of_ms")
    offsets = tuple(probe.offset_ms for probe in observations)
    rtts = tuple(probe.rtt_ms for probe in observations)
    offset = _median(offsets)
    rtt = _median(rtts)
    deviation = _median(tuple(abs(item - offset) for item in offsets))
    return ClockEstimate(
        offset_ms=offset,
        rtt_ms=rtt,
        uncertainty_ms=(rtt / Decimal("2")) + deviation,
        sample_count=len(observations),
        trace_refs=tuple(probe.trace_ref for probe in observations),
        observed_at_ms=max(
            probe.client_received_at_ms for probe in observations
        ),
        valid_for_ms=valid_for_ms,
    )


@dataclass(frozen=True)
class SourcePriceEvent:
    """A public RTDS/reference-price observation."""

    trace_ref: str
    symbol: str
    source: str
    price: Decimal
    server_event_time_ms: int
    received_at_ms: int
    is_carried_forward: bool = False
    is_stale: bool = False
    capture_seq: Optional[int] = None
    raw_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.trace_ref.strip() or not self.symbol.strip() or not self.source.strip():
            raise ValueError("trace_ref, symbol, and source must be non-empty")
        object.__setattr__(self, "price", _decimal(self.price, name="price"))
        if self.price <= ZERO:
            raise ValueError("source price must be positive")
        _timestamp(self.server_event_time_ms, name="server_event_time_ms")
        _timestamp(self.received_at_ms, name="received_at_ms")
        if self.capture_seq is not None:
            _timestamp(self.capture_seq, name="capture_seq")


@dataclass(frozen=True)
class ClobQuoteEvent:
    """A top-of-book observation captured from the public CLOB stream."""

    trace_ref: str
    market_id: str
    token_id: str
    outcome: str
    best_bid: Decimal
    best_ask: Decimal
    server_event_time_ms: int
    received_at_ms: int
    best_bid_size: Optional[Decimal] = None
    best_ask_size: Optional[Decimal] = None
    is_stale: bool = False
    capture_seq: Optional[int] = None
    raw_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.trace_ref, self.market_id, self.token_id, self.outcome)
        ):
            raise ValueError("quote trace, market, token, and outcome must be non-empty")
        outcome = self.outcome.upper()
        if outcome not in {"YES", "NO"}:
            raise ValueError("outcome must be YES or NO")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "best_bid", _decimal(self.best_bid, name="best_bid"))
        object.__setattr__(self, "best_ask", _decimal(self.best_ask, name="best_ask"))
        if not ZERO <= self.best_bid <= self.best_ask <= ONE:
            raise ValueError("quote must satisfy 0 <= best_bid <= best_ask <= 1")
        for field_name in ("best_bid_size", "best_ask_size"):
            raw_size = getattr(self, field_name)
            if raw_size is None:
                continue
            size = _decimal(raw_size, name=field_name)
            if size < ZERO:
                raise ValueError(f"{field_name} cannot be negative")
            object.__setattr__(self, field_name, size)
        _timestamp(self.server_event_time_ms, name="server_event_time_ms")
        _timestamp(self.received_at_ms, name="received_at_ms")
        if self.capture_seq is not None:
            _timestamp(self.capture_seq, name="capture_seq")

    @property
    def midpoint(self) -> Decimal:
        return (self.best_bid + self.best_ask) / Decimal("2")


@dataclass(frozen=True)
class RuleWindow:
    """A versioned price-to-beat market rule and its entry deadline."""

    trace_ref: str
    market_id: str
    source_symbol: str
    opens_at_ms: int
    closes_at_ms: int
    entry_deadline_ms: int
    price_to_beat: Decimal
    observed_at_ms: int
    effective_at_ms: int
    resolved_outcome: Optional[str] = None
    resolution_observed_at_ms: Optional[int] = None
    yes_when: str = "above"
    raw_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.trace_ref, self.market_id, self.source_symbol)
        ):
            raise ValueError("rule trace, market, and source symbol must be non-empty")
        _timestamp(self.opens_at_ms, name="opens_at_ms")
        _timestamp(self.closes_at_ms, name="closes_at_ms")
        _timestamp(self.entry_deadline_ms, name="entry_deadline_ms")
        _timestamp(self.observed_at_ms, name="observed_at_ms")
        _timestamp(self.effective_at_ms, name="effective_at_ms")
        if not self.opens_at_ms < self.closes_at_ms:
            raise ValueError("rule window must close after it opens")
        if not self.opens_at_ms <= self.entry_deadline_ms <= self.closes_at_ms:
            raise ValueError("entry deadline must fall inside the rule window")
        object.__setattr__(
            self,
            "price_to_beat",
            _decimal(self.price_to_beat, name="price_to_beat"),
        )
        if self.price_to_beat <= ZERO:
            raise ValueError("price_to_beat must be positive")
        yes_when = self.yes_when.lower()
        if yes_when not in {"above", "below"}:
            raise ValueError("yes_when must be 'above' or 'below'")
        object.__setattr__(self, "yes_when", yes_when)
        if self.resolved_outcome is not None:
            resolved = self.resolved_outcome.upper()
            if resolved not in {"YES", "NO"}:
                raise ValueError("resolved_outcome must be YES, NO, or None")
            object.__setattr__(self, "resolved_outcome", resolved)
            if self.resolution_observed_at_ms is None:
                raise ValueError(
                    "resolved outcomes require resolution_observed_at_ms"
                )
        if self.resolution_observed_at_ms is not None:
            _timestamp(
                self.resolution_observed_at_ms,
                name="resolution_observed_at_ms",
            )
            if self.resolution_observed_at_ms < self.closes_at_ms:
                raise ValueError(
                    "resolution cannot be observed before the rule closes"
                )

    def outcome_for_price(
        self,
        price: Decimal,
        *,
        threshold: Decimal = ZERO,
    ) -> Optional[str]:
        """Return the signaled token; equality/inside-threshold has no signal."""

        observed = _decimal(price, name="price")
        minimum_move = _decimal(threshold, name="threshold")
        if minimum_move < ZERO:
            raise ValueError("threshold cannot be negative")
        difference = observed - self.price_to_beat
        if abs(difference) <= minimum_move:
            return None
        above = difference > ZERO
        yes = above if self.yes_when == "above" else not above
        return "YES" if yes else "NO"


@dataclass(frozen=True)
class ChronologicalRuleSplit:
    """Whole-market chronological train/validation/test partitions."""

    train: tuple[RuleWindow, ...]
    validation: tuple[RuleWindow, ...]
    test: tuple[RuleWindow, ...]

    def as_experiment_splits(self) -> dict[str, tuple[str, ...]]:
        return {
            "train": tuple(rule.market_id for rule in self.train),
            "validation": tuple(rule.market_id for rule in self.validation),
            "test": tuple(rule.market_id for rule in self.test),
        }


def chronological_rule_split(
    rules: Iterable[RuleWindow],
    *,
    train_fraction: Decimal = Decimal("0.6"),
    validation_fraction: Decimal = Decimal("0.2"),
    embargo_ms: int = 0,
) -> ChronologicalRuleSplit:
    """Purged split of whole, non-overlapping temporal rule groups."""

    train_ratio = _decimal(train_fraction, name="train_fraction")
    validation_ratio = _decimal(
        validation_fraction, name="validation_fraction"
    )
    if not ZERO < train_ratio < ONE:
        raise ValueError("train_fraction must be between 0 and 1")
    if validation_ratio < ZERO or train_ratio + validation_ratio >= ONE:
        raise ValueError(
            "validation_fraction must be non-negative and leave a test split"
        )
    if (
        isinstance(embargo_ms, bool)
        or not isinstance(embargo_ms, int)
        or embargo_ms < 0
    ):
        raise ValueError("embargo_ms must be a non-negative integer")
    ordered = tuple(
        sorted(
            rules,
            key=lambda rule: (
                rule.opens_at_ms,
                rule.closes_at_ms,
                rule.market_id,
                rule.trace_ref,
            ),
        )
    )
    if not ordered:
        raise ValueError("at least one rule window is required")
    market_ids = tuple(rule.market_id for rule in ordered)
    if len(set(market_ids)) != len(market_ids):
        raise ValueError("a market cannot appear in multiple chronological splits")

    groups: list[list[RuleWindow]] = []
    group_close = -1
    for rule in ordered:
        if groups and rule.opens_at_ms < group_close + embargo_ms:
            groups[-1].append(rule)
            group_close = max(group_close, rule.closes_at_ms)
        else:
            groups.append([rule])
            group_close = rule.closes_at_ms

    count = len(groups)
    train_count = int(Decimal(count) * train_ratio)
    validation_count = int(Decimal(count) * validation_ratio)
    test_count = count - train_count - validation_count
    if train_count < 1 or test_count < 1:
        raise ValueError(
            "dataset has too few non-overlapping temporal groups for train and test"
        )
    if validation_ratio > ZERO and validation_count < 1:
        raise ValueError(
            "dataset has too few non-overlapping temporal groups for validation"
        )
    validation_end = train_count + validation_count

    def flatten(selected: Sequence[list[RuleWindow]]) -> tuple[RuleWindow, ...]:
        return tuple(rule for group in selected for rule in group)

    return ChronologicalRuleSplit(
        train=flatten(groups[:train_count]),
        validation=flatten(groups[train_count:validation_end]),
        test=flatten(groups[validation_end:]),
    )


@dataclass(frozen=True)
class SignalDecision:
    """One price-to-beat decision with causal provenance."""

    market_id: str
    decision_at_ms: int
    outcome: Optional[str]
    source_price: Optional[Decimal]
    price_to_beat: Decimal
    rule_trace_ref: str
    source_trace_ref: str
    source_capture_seq: Optional[int]
    reject_reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return not self.reject_reasons and self.outcome is not None

    @property
    def trace_refs(self) -> tuple[str, str]:
        return (self.rule_trace_ref, self.source_trace_ref)


def evaluate_rule_signal(
    *,
    rule: RuleWindow,
    source_event: SourcePriceEvent,
    decision_at_ms: int,
    threshold: Decimal = ZERO,
    max_source_age_ms: int,
    source_clock: Optional[ClockEstimate] = None,
) -> SignalDecision:
    """Apply source, freshness, rule-window, and price-to-beat gates."""

    _timestamp(decision_at_ms, name="decision_at_ms")
    if max_source_age_ms < 0:
        raise ValueError("max_source_age_ms must be non-negative")
    minimum_move = _decimal(threshold, name="threshold")
    if minimum_move < ZERO:
        raise ValueError("threshold cannot be negative")

    reasons: list[str] = []
    source_visible = source_event.received_at_ms <= decision_at_ms
    if source_event.symbol != rule.source_symbol:
        reasons.append("source_symbol_mismatch")
    if not source_visible:
        reasons.append("source_not_received_by_decision")
    if decision_at_ms < rule.opens_at_ms:
        reasons.append("before_rule_window")
    if rule.observed_at_ms > decision_at_ms:
        reasons.append("rule_not_observed_by_decision")
    if rule.effective_at_ms > decision_at_ms:
        reasons.append("rule_not_effective_by_decision")
    if decision_at_ms > rule.entry_deadline_ms:
        reasons.append("past_entry_deadline")
    if decision_at_ms > rule.closes_at_ms:
        reasons.append("after_rule_window")

    if source_visible:
        age, future, ambiguous = _event_age(
            decision_at_ms=decision_at_ms,
            server_event_time_ms=source_event.server_event_time_ms,
            received_at_ms=source_event.received_at_ms,
            clock=source_clock,
        )
        if future:
            reasons.append("source_event_time_after_decision")
        if ambiguous:
            reasons.append("source_clock_ambiguous")
        reasons.extend(
            _clock_reject_reasons(
                source_clock, decision_at_ms, prefix="source"
            )
        )
        if age > Decimal(max_source_age_ms):
            reasons.append("source_too_old")
        if source_event.is_carried_forward:
            reasons.append("source_carried_forward")
        if source_event.is_stale:
            reasons.append("source_explicitly_stale")

    may_read_price = (
        source_visible
        and source_event.symbol == rule.source_symbol
        and "source_event_time_after_decision" not in reasons
    )
    outcome = (
        rule.outcome_for_price(source_event.price, threshold=minimum_move)
        if may_read_price
        else None
    )
    if may_read_price and outcome is None:
        reasons.append("signal_below_threshold")

    return SignalDecision(
        market_id=rule.market_id,
        decision_at_ms=decision_at_ms,
        outcome=outcome,
        source_price=source_event.price if may_read_price else None,
        price_to_beat=rule.price_to_beat,
        rule_trace_ref=rule.trace_ref,
        source_trace_ref=source_event.trace_ref,
        source_capture_seq=source_event.capture_seq,
        reject_reasons=tuple(dict.fromkeys(reasons)),
    )


@dataclass(frozen=True)
class AsOfAlignment:
    """One auditable causal as-of join, including every failed gate."""

    decision_at_ms: int
    source: Optional[SourcePriceEvent]
    quote: Optional[ClobQuoteEvent]
    source_age_ms: Optional[Decimal]
    quote_age_ms: Optional[Decimal]
    reject_reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return not self.reject_reasons

    @property
    def trace_refs(self) -> tuple[str, ...]:
        refs: list[str] = []
        if self.source is not None:
            refs.append(self.source.trace_ref)
        if self.quote is not None:
            refs.append(self.quote.trace_ref)
        return tuple(refs)


def _event_age(
    *,
    decision_at_ms: int,
    server_event_time_ms: int,
    received_at_ms: int,
    clock: Optional[ClockEstimate],
) -> tuple[Decimal, bool, bool]:
    if clock is None:
        lower_event_time = upper_event_time = Decimal(server_event_time_ms)
    else:
        lower_event_time, upper_event_time = clock.local_time_bounds(
            server_event_time_ms
        )
    decision = Decimal(decision_at_ms)
    event_is_future = lower_event_time > decision
    event_time_ambiguous = (
        lower_event_time <= decision < upper_event_time
    )
    # Freshness must pass both the source timestamp and local availability.
    conservative_age = max(
        decision - lower_event_time,
        decision - Decimal(received_at_ms),
    )
    return conservative_age, event_is_future, event_time_ambiguous


def _clock_reject_reasons(
    clock: Optional[ClockEstimate],
    at_ms: int,
    *,
    prefix: str,
) -> tuple[str, ...]:
    if clock is None:
        return ()
    if clock.observed_at_ms > at_ms:
        return (f"{prefix}_clock_estimate_from_future",)
    if not clock.valid_at(at_ms):
        return (f"{prefix}_clock_estimate_expired",)
    return ()


def _latest_received(
    events: Iterable[SourcePriceEvent | ClobQuoteEvent],
    decision_at_ms: int,
    decision_capture_seq: Optional[int] = None,
) -> Optional[SourcePriceEvent | ClobQuoteEvent]:
    available: list[SourcePriceEvent | ClobQuoteEvent] = []
    for event in events:
        if event.received_at_ms < decision_at_ms:
            available.append(event)
            continue
        if event.received_at_ms > decision_at_ms:
            continue
        if (
            decision_capture_seq is not None
            and event.capture_seq is not None
            and event.capture_seq < decision_capture_seq
        ):
            available.append(event)
    if not available:
        return None
    return max(
        available,
        key=lambda event: (
            event.received_at_ms,
            -1 if event.capture_seq is None else event.capture_seq,
            event.server_event_time_ms,
            event.trace_ref,
        ),
    )


def align_asof(
    *,
    decision_at_ms: int,
    source_events: Iterable[SourcePriceEvent],
    quote_events: Iterable[ClobQuoteEvent],
    max_source_age_ms: int,
    max_quote_age_ms: int,
    source_clock: Optional[ClockEstimate] = None,
    clob_clock: Optional[ClockEstimate] = None,
    decision_capture_seq: Optional[int] = None,
    symbol: Optional[str] = None,
    market_id: Optional[str] = None,
    outcome: Optional[str] = None,
    allow_carried_forward: bool = False,
    allow_explicit_stale: bool = False,
) -> AsOfAlignment:
    """Causally join the latest received source and quote at a decision time."""

    _timestamp(decision_at_ms, name="decision_at_ms")
    if max_source_age_ms < 0 or max_quote_age_ms < 0:
        raise ValueError("maximum ages must be non-negative")
    sources = (
        event for event in source_events if symbol is None or event.symbol == symbol
    )
    normalized_outcome = outcome.upper() if outcome is not None else None
    quotes = (
        event
        for event in quote_events
        if (market_id is None or event.market_id == market_id)
        and (normalized_outcome is None or event.outcome == normalized_outcome)
    )
    source = _latest_received(
        sources, decision_at_ms, decision_capture_seq
    )
    quote = _latest_received(quotes, decision_at_ms, decision_capture_seq)
    assert source is None or isinstance(source, SourcePriceEvent)
    assert quote is None or isinstance(quote, ClobQuoteEvent)

    reasons: list[str] = []
    source_age: Optional[Decimal] = None
    quote_age: Optional[Decimal] = None
    if source is None:
        reasons.append("no_source_received_before_decision")
    else:
        source_age, source_future, source_ambiguous = _event_age(
            decision_at_ms=decision_at_ms,
            server_event_time_ms=source.server_event_time_ms,
            received_at_ms=source.received_at_ms,
            clock=source_clock,
        )
        if source_future:
            reasons.append("source_event_time_after_decision")
        if source_ambiguous:
            reasons.append("source_clock_ambiguous")
        reasons.extend(
            _clock_reject_reasons(
                source_clock, decision_at_ms, prefix="source"
            )
        )
        if source_age > Decimal(max_source_age_ms):
            reasons.append("source_too_old")
        if source.is_carried_forward and not allow_carried_forward:
            reasons.append("source_carried_forward")
        if source.is_stale and not allow_explicit_stale:
            reasons.append("source_explicitly_stale")

    if quote is None:
        reasons.append("no_quote_received_before_decision")
    else:
        quote_age, quote_future, quote_ambiguous = _event_age(
            decision_at_ms=decision_at_ms,
            server_event_time_ms=quote.server_event_time_ms,
            received_at_ms=quote.received_at_ms,
            clock=clob_clock,
        )
        if quote_future:
            reasons.append("quote_event_time_after_decision")
        if quote_ambiguous:
            reasons.append("quote_clock_ambiguous")
        reasons.extend(
            _clock_reject_reasons(
                clob_clock, decision_at_ms, prefix="quote"
            )
        )
        if quote_age > Decimal(max_quote_age_ms):
            reasons.append("quote_too_old")
        if quote.is_stale and not allow_explicit_stale:
            reasons.append("quote_explicitly_stale")

    return AsOfAlignment(
        decision_at_ms=decision_at_ms,
        source=source,
        quote=quote,
        source_age_ms=source_age,
        quote_age_ms=quote_age,
        reject_reasons=tuple(dict.fromkeys(reasons)),
    )


@dataclass(frozen=True)
class LagScore:
    """One fixed-lag source-return versus later CLOB-midpoint score."""

    lag_ms: int
    samples: int
    correlation: Optional[Decimal]
    direction_hit_rate: Optional[Decimal]
    source_trace_refs: tuple[str, ...]
    quote_trace_refs: tuple[str, ...]


@dataclass(frozen=True)
class LeadLagStudy:
    """Auditable fixed-grid study result."""

    scores: tuple[LagScore, ...]
    rejection_counts: Mapping[str, int]
    series_key: tuple[str, str, str, str, str]
    source_clock: Optional[ClockEstimate] = None
    clob_clock: Optional[ClockEstimate] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rejection_counts",
            MappingProxyType(dict(sorted(self.rejection_counts.items()))),
        )

    @property
    def best_lag(self) -> Optional[LagScore]:
        eligible = [
            score for score in self.scores if score.correlation is not None
        ]
        if not eligible:
            return None
        return max(
            eligible,
            key=lambda score: (
                abs(score.correlation or ZERO),
                score.direction_hit_rate or ZERO,
                -score.lag_ms,
            ),
        )


def _pearson(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> Optional[Decimal]:
    if len(xs) != len(ys):
        raise ValueError("correlation inputs must have the same length")
    if len(xs) < 2:
        return None
    count = Decimal(len(xs))
    mean_x = sum(xs, ZERO) / count
    mean_y = sum(ys, ZERO) / count
    centered_x = tuple(value - mean_x for value in xs)
    centered_y = tuple(value - mean_y for value in ys)
    numerator = sum(
        (left * right for left, right in zip(centered_x, centered_y)),
        ZERO,
    )
    x_squares = sum((value * value for value in centered_x), ZERO)
    y_squares = sum((value * value for value in centered_y), ZERO)
    if x_squares == ZERO or y_squares == ZERO:
        return None
    return numerator / (x_squares * y_squares).sqrt()


def _sign(value: Decimal) -> int:
    if value > ZERO:
        return 1
    if value < ZERO:
        return -1
    return 0


def _usable_source_at_arrival(
    event: SourcePriceEvent,
    *,
    max_age_ms: int,
    clock: Optional[ClockEstimate],
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    age, future, ambiguous = _event_age(
        decision_at_ms=event.received_at_ms,
        server_event_time_ms=event.server_event_time_ms,
        received_at_ms=event.received_at_ms,
        clock=clock,
    )
    if future:
        reasons.append("source_event_time_after_decision")
    if ambiguous:
        reasons.append("source_clock_ambiguous")
    reasons.extend(
        _clock_reject_reasons(
            clock, event.received_at_ms, prefix="source"
        )
    )
    if age > Decimal(max_age_ms):
        reasons.append("source_too_old")
    if event.is_carried_forward:
        reasons.append("source_carried_forward")
    if event.is_stale:
        reasons.append("source_explicitly_stale")
    return not reasons, tuple(reasons)


def _usable_quote_asof(
    events: Sequence[ClobQuoteEvent],
    *,
    at_ms: int,
    max_age_ms: int,
    clock: Optional[ClockEstimate],
    at_capture_seq: Optional[int] = None,
) -> tuple[Optional[ClobQuoteEvent], tuple[str, ...]]:
    selected = _latest_received(events, at_ms, at_capture_seq)
    if selected is None:
        return None, ("no_quote_received_before_decision",)
    assert isinstance(selected, ClobQuoteEvent)
    reasons: list[str] = []
    age, future, ambiguous = _event_age(
        decision_at_ms=at_ms,
        server_event_time_ms=selected.server_event_time_ms,
        received_at_ms=selected.received_at_ms,
        clock=clock,
    )
    if future:
        reasons.append("quote_event_time_after_decision")
    if ambiguous:
        reasons.append("quote_clock_ambiguous")
    reasons.extend(_clock_reject_reasons(clock, at_ms, prefix="quote"))
    if age > Decimal(max_age_ms):
        reasons.append("quote_too_old")
    if selected.is_stale:
        reasons.append("quote_explicitly_stale")
    return selected, tuple(reasons)


def lead_lag_study(
    *,
    source_events: Iterable[SourcePriceEvent],
    quote_events: Iterable[ClobQuoteEvent],
    lag_grid_ms: Sequence[int],
    max_source_age_ms: int,
    max_quote_age_ms: int,
    symbol: str,
    source_name: str,
    market_id: str,
    token_id: str,
    outcome: str,
    source_clock: Optional[ClockEstimate] = None,
    clob_clock: Optional[ClockEstimate] = None,
    observation_end_ms: Optional[int] = None,
) -> LeadLagStudy:
    """Score a fixed lag grid using only observations available at each time.

    For a source move arriving at ``t``, the feature is frozen at ``t`` and the
    CLOB label is the as-of midpoint change from ``t`` to ``t + lag``.  This is
    an offline label, never fed back into the signal at ``t``.
    """

    if max_source_age_ms < 0 or max_quote_age_ms < 0:
        raise ValueError("maximum ages must be non-negative")
    if observation_end_ms is not None:
        _timestamp(observation_end_ms, name="observation_end_ms")
    if not all(
        value.strip()
        for value in (symbol, source_name, market_id, token_id, outcome)
    ):
        raise ValueError("the complete lead-lag series key is required")
    lags = tuple(lag_grid_ms)
    if not lags:
        raise ValueError("lag_grid_ms must be non-empty")
    if any(isinstance(lag, bool) or not isinstance(lag, int) or lag < 0 for lag in lags):
        raise ValueError("lags must be non-negative integer milliseconds")
    if len(set(lags)) != len(lags):
        raise ValueError("lag_grid_ms cannot contain duplicates")

    selected_sources = sorted(
        (
            event
            for event in source_events
            if event.symbol == symbol and event.source == source_name
        ),
        key=lambda event: (
            event.received_at_ms,
            event.capture_seq is None,
            -1 if event.capture_seq is None else event.capture_seq,
            event.server_event_time_ms,
            event.trace_ref,
        ),
    )
    normalized_outcome = outcome.upper()
    if normalized_outcome not in {"YES", "NO"}:
        raise ValueError("outcome must be YES or NO")
    selected_quotes = tuple(
        sorted(
            (
                event
                for event in quote_events
                if event.market_id == market_id
                and event.token_id == token_id
                and event.outcome == normalized_outcome
            ),
            key=lambda event: (
                event.received_at_ms,
                event.capture_seq is None,
                -1 if event.capture_seq is None else event.capture_seq,
                event.server_event_time_ms,
                event.trace_ref,
            ),
        )
    )

    rejected: Counter[str] = Counter()
    usable_sources: list[SourcePriceEvent] = []
    for event in selected_sources:
        usable, reasons = _usable_source_at_arrival(
            event,
            max_age_ms=max_source_age_ms,
            clock=source_clock,
        )
        if not usable:
            rejected.update(reasons)
            continue
        usable_sources.append(event)

    moves: list[tuple[SourcePriceEvent, Decimal]] = []
    for previous, current in zip(usable_sources, usable_sources[1:]):
        if previous.price == ZERO:  # guarded by model, kept fail-closed
            rejected["zero_source_base"] += 1
            continue
        source_return = (current.price - previous.price) / previous.price
        if source_return == ZERO:
            rejected["zero_source_move"] += 1
            continue
        moves.append((current, source_return))

    scores: list[LagScore] = []
    for lag in lags:
        source_returns: list[Decimal] = []
        quote_returns: list[Decimal] = []
        source_refs: list[str] = []
        quote_refs: list[str] = []
        for event, source_return in moves:
            if (
                observation_end_ms is not None
                and event.received_at_ms + lag > observation_end_ms
            ):
                rejected["lag_horizon_after_observation_end"] += 1
                continue
            baseline, baseline_reasons = _usable_quote_asof(
                selected_quotes,
                at_ms=event.received_at_ms,
                max_age_ms=max_quote_age_ms,
                clock=clob_clock,
                at_capture_seq=event.capture_seq,
            )
            future, future_reasons = _usable_quote_asof(
                selected_quotes,
                at_ms=event.received_at_ms + lag,
                max_age_ms=max_quote_age_ms,
                clock=clob_clock,
                at_capture_seq=event.capture_seq if lag == 0 else None,
            )
            reasons = baseline_reasons + future_reasons
            if reasons:
                rejected.update(reasons)
                continue
            assert baseline is not None and future is not None
            if baseline.midpoint == ZERO:
                rejected["zero_quote_base"] += 1
                continue
            quote_return = (
                future.midpoint - baseline.midpoint
            ) / baseline.midpoint
            source_returns.append(source_return)
            quote_returns.append(quote_return)
            source_refs.append(event.trace_ref)
            quote_refs.extend((baseline.trace_ref, future.trace_ref))

        if source_returns:
            hits = sum(
                1
                for source_return, quote_return in zip(
                    source_returns, quote_returns
                )
                if _sign(source_return) != 0
                and _sign(source_return) == _sign(quote_return)
            )
            hit_rate: Optional[Decimal] = Decimal(hits) / Decimal(
                len(source_returns)
            )
        else:
            hit_rate = None
        scores.append(
            LagScore(
                lag_ms=lag,
                samples=len(source_returns),
                correlation=_pearson(source_returns, quote_returns),
                direction_hit_rate=hit_rate,
                source_trace_refs=tuple(source_refs),
                quote_trace_refs=tuple(quote_refs),
            )
        )

    return LeadLagStudy(
        scores=tuple(scores),
        rejection_counts=dict(rejected),
        series_key=(
            source_name,
            symbol,
            market_id,
            token_id,
            normalized_outcome,
        ),
        source_clock=source_clock,
        clob_clock=clob_clock,
    )


FEE_QUANTUM = Decimal("0.00001")


@dataclass(frozen=True)
class Trade:
    """A public CLOB trade used only as maker-fill evidence."""

    trace_ref: str
    market_id: str
    token_id: str
    outcome: str
    aggressor_side: str
    price: Decimal
    size: Decimal
    server_event_time_ms: int
    received_at_ms: int
    is_stale: bool = False
    capture_seq: Optional[int] = None
    raw_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.trace_ref, self.market_id, self.token_id, self.outcome)
        ):
            raise ValueError("trade trace, market, token, and outcome must be non-empty")
        outcome = self.outcome.upper()
        side = self.aggressor_side.upper()
        if outcome not in {"YES", "NO"}:
            raise ValueError("trade outcome must be YES or NO")
        if side not in {"BUY", "SELL"}:
            raise ValueError("aggressor_side must be BUY or SELL")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "aggressor_side", side)
        object.__setattr__(self, "price", _decimal(self.price, name="trade price"))
        object.__setattr__(self, "size", _decimal(self.size, name="trade size"))
        if not ZERO <= self.price <= ONE:
            raise ValueError("trade price must be between 0 and 1")
        if self.size <= ZERO:
            raise ValueError("trade size must be positive")
        _timestamp(self.server_event_time_ms, name="server_event_time_ms")
        _timestamp(self.received_at_ms, name="received_at_ms")
        if self.capture_seq is not None:
            _timestamp(self.capture_seq, name="capture_seq")


@dataclass(frozen=True)
class LatencyStrategyConfig:
    """Fixed strategy and friction assumptions for one latency experiment."""

    action: str
    quantity: Decimal
    signal_threshold: Decimal
    base_placement_latency_ms: int
    slippage_per_share: Decimal
    max_source_age_ms: int
    max_quote_age_ms: int
    fee_rate: Decimal
    fee_exponent: Decimal
    fee_source_ref: str
    market_id: str
    tick_size: Decimal
    min_order_size: Decimal
    fee_taker_only: bool = True
    maker_queue_ahead: Decimal = ZERO
    maker_price_improvement: Decimal = ZERO

    def __post_init__(self) -> None:
        action = self.action.lower()
        if action not in {"taker", "maker"}:
            raise ValueError("action must be taker or maker")
        object.__setattr__(self, "action", action)
        for field_name in (
            "quantity",
            "signal_threshold",
            "slippage_per_share",
            "fee_rate",
            "fee_exponent",
            "tick_size",
            "min_order_size",
            "maker_queue_ahead",
            "maker_price_improvement",
        ):
            object.__setattr__(
                self,
                field_name,
                _decimal(getattr(self, field_name), name=field_name),
            )
        if self.quantity <= ZERO:
            raise ValueError("quantity must be positive")
        if not self.market_id.strip():
            raise ValueError("market_id must be non-empty")
        if not ZERO < self.tick_size <= ONE:
            raise ValueError("tick_size must be between zero and one")
        if self.min_order_size <= ZERO:
            raise ValueError("min_order_size must be positive")
        if self.quantity < self.min_order_size:
            raise ValueError("quantity is below the market minimum order size")
        if any(
            value < ZERO
            for value in (
                self.signal_threshold,
                self.slippage_per_share,
                self.fee_rate,
                self.fee_exponent,
                self.maker_queue_ahead,
                self.maker_price_improvement,
            )
        ):
            raise ValueError("strategy friction parameters cannot be negative")
        if (
            isinstance(self.base_placement_latency_ms, bool)
            or self.base_placement_latency_ms < 0
        ):
            raise ValueError("base placement latency must be non-negative")
        if self.max_source_age_ms < 0 or self.max_quote_age_ms < 0:
            raise ValueError("maximum ages must be non-negative")
        if not self.fee_source_ref.strip():
            raise ValueError("fee_source_ref must be non-empty")


@dataclass(frozen=True)
class ExecutionScenario:
    """One deterministic execution-friction scenario, never a probability."""

    name: str
    placement_latency_ms: int
    slippage_per_share: Decimal
    maker_queue_multiplier: Decimal
    fee_multiplier: Decimal = ONE

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("scenario name must be non-empty")
        if (
            isinstance(self.placement_latency_ms, bool)
            or self.placement_latency_ms < 0
        ):
            raise ValueError("placement latency must be non-negative")
        for field_name in (
            "slippage_per_share",
            "maker_queue_multiplier",
            "fee_multiplier",
        ):
            object.__setattr__(
                self,
                field_name,
                _decimal(getattr(self, field_name), name=field_name),
            )
        if (
            self.slippage_per_share < ZERO
            or self.maker_queue_multiplier < ZERO
            or self.fee_multiplier < ZERO
        ):
            raise ValueError("scenario friction values cannot be negative")


def standard_execution_scenarios(
    config: LatencyStrategyConfig,
) -> tuple[ExecutionScenario, ...]:
    """Return the fixed ideal/optimistic/neutral/pessimistic grid."""

    base_latency = config.base_placement_latency_ms
    optimistic_latency = (
        0 if base_latency == 0 else max(1, base_latency // 2)
    )
    return (
        ExecutionScenario("ideal", 0, ZERO, ZERO),
        ExecutionScenario(
            "optimistic",
            optimistic_latency,
            config.slippage_per_share / Decimal("2"),
            Decimal("0.5"),
        ),
        ExecutionScenario(
            "neutral",
            base_latency,
            config.slippage_per_share,
            ONE,
        ),
        ExecutionScenario(
            "pessimistic",
            base_latency * 2,
            config.slippage_per_share * Decimal("2"),
            Decimal("2"),
        ),
    )


@dataclass(frozen=True)
class ScenarioExecution:
    """Per-market result with source, rule, quote, and trade trace references."""

    scenario: str
    market_id: str
    action: str
    status: str
    outcome: Optional[str]
    decision_at_ms: Optional[int]
    placement_at_ms: Optional[int]
    rule_trace_ref: str
    source_trace_ref: Optional[str] = None
    quote_trace_ref: Optional[str] = None
    trade_trace_refs: tuple[str, ...] = ()
    fill_price: Optional[Decimal] = None
    fill_quantity: Decimal = ZERO
    payout: Decimal = ZERO
    notional: Decimal = ZERO
    fee: Decimal = ZERO
    slippage_cost: Decimal = ZERO
    gross_profit: Decimal = ZERO
    net_profit: Decimal = ZERO
    reject_reasons: tuple[str, ...] = ()

    @property
    def trace_refs(self) -> tuple[str, ...]:
        values: list[str] = [self.rule_trace_ref]
        if self.source_trace_ref is not None:
            values.append(self.source_trace_ref)
        if self.quote_trace_ref is not None:
            values.append(self.quote_trace_ref)
        values.extend(self.trade_trace_refs)
        return tuple(dict.fromkeys(values))

    @property
    def filled(self) -> bool:
        return self.status in {"filled", "partial"} and self.fill_quantity > ZERO

    def to_mapping(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "market_id": self.market_id,
            "action": self.action,
            "status": self.status,
            "outcome": self.outcome,
            "decision_at_ms": self.decision_at_ms,
            "placement_at_ms": self.placement_at_ms,
            "trace_refs": self.trace_refs,
            "fill_price": None if self.fill_price is None else str(self.fill_price),
            "fill_quantity": str(self.fill_quantity),
            "payout": str(self.payout),
            "notional": str(self.notional),
            "fee": str(self.fee),
            "slippage_cost": str(self.slippage_cost),
            "gross_profit": str(self.gross_profit),
            "net_profit": str(self.net_profit),
            "reject_reasons": self.reject_reasons,
        }


@dataclass(frozen=True)
class LatencyScenarioResult:
    name: str
    executions: tuple[ScenarioExecution, ...]

    @property
    def fill_count(self) -> int:
        return sum(execution.filled for execution in self.executions)

    @property
    def rejected_count(self) -> int:
        return sum(execution.status == "rejected" for execution in self.executions)

    @property
    def net_profit(self) -> Decimal:
        return sum((execution.net_profit for execution in self.executions), ZERO)

    @property
    def fees(self) -> Decimal:
        return sum((execution.fee for execution in self.executions), ZERO)

    @property
    def slippage_cost(self) -> Decimal:
        return sum(
            (execution.slippage_cost for execution in self.executions), ZERO
        )

    @property
    def rejection_counts(self) -> Mapping[str, int]:
        counts: Counter[str] = Counter()
        for execution in self.executions:
            counts.update(execution.reject_reasons)
        return MappingProxyType(dict(sorted(counts.items())))

    def event_profit_rows(self) -> tuple[dict[str, object], ...]:
        """Rows accepted by ``experiments.bootstrap_event_profit_ci``."""

        return tuple(
            {"event_id": execution.market_id, "profit": execution.net_profit}
            for execution in self.executions
        )


@dataclass(frozen=True)
class LatencyBacktestResult:
    scenarios: tuple[LatencyScenarioResult, ...]
    classification: str
    reject_reasons: tuple[str, ...]
    fee_source_ref: str

    def scenario(self, name: str) -> LatencyScenarioResult:
        for scenario in self.scenarios:
            if scenario.name == name:
                return scenario
        raise KeyError(f"unknown scenario: {name}")

    def bootstrap(
        self,
        *,
        scenario: str = "pessimistic",
        n_resamples: int = 10_000,
        confidence: Decimal = Decimal("0.95"),
        seed: int = 0,
    ) -> object:
        """Delegate whole-market confidence intervals to the experiment seam."""

        from .experiments import bootstrap_event_profit_ci

        return bootstrap_event_profit_ci(
            self.scenario(scenario).event_profit_rows(),
            n_resamples=n_resamples,
            confidence=confidence,
            seed=seed,
        )


def _fee(
    *,
    quantity: Decimal,
    price: Decimal,
    config: LatencyStrategyConfig,
    scenario: ExecutionScenario,
    maker: bool,
) -> Decimal:
    if quantity <= ZERO or config.fee_rate <= ZERO:
        return ZERO
    if maker and config.fee_taker_only:
        return ZERO
    probability_term = price * (ONE - price)
    if probability_term <= ZERO:
        return ZERO
    raw = (
        quantity
        * config.fee_rate
        * (probability_term**config.fee_exponent)
        * scenario.fee_multiplier
    )
    if raw < FEE_QUANTUM:
        return ZERO
    return raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)


def _rejected_execution(
    *,
    scenario: ExecutionScenario,
    rule: RuleWindow,
    action: str,
    reasons: Iterable[str],
    signal: Optional[SignalDecision] = None,
    placement_at_ms: Optional[int] = None,
    quote: Optional[ClobQuoteEvent] = None,
    trade_trace_refs: tuple[str, ...] = (),
) -> ScenarioExecution:
    return ScenarioExecution(
        scenario=scenario.name,
        market_id=rule.market_id,
        action=action,
        status="rejected",
        outcome=signal.outcome if signal is not None else None,
        decision_at_ms=signal.decision_at_ms if signal is not None else None,
        placement_at_ms=placement_at_ms,
        rule_trace_ref=rule.trace_ref,
        source_trace_ref=(
            signal.source_trace_ref if signal is not None else None
        ),
        quote_trace_ref=quote.trace_ref if quote is not None else None,
        trade_trace_refs=trade_trace_refs,
        reject_reasons=tuple(dict.fromkeys(reasons)),
    )


def _first_signal(
    *,
    rule: RuleWindow,
    source_events: Sequence[SourcePriceEvent],
    config: LatencyStrategyConfig,
    source_clock: Optional[ClockEstimate],
) -> tuple[Optional[SignalDecision], tuple[str, ...]]:
    candidates = sorted(
        (
            event
            for event in source_events
            if event.symbol == rule.source_symbol
            and event.received_at_ms <= rule.closes_at_ms
        ),
        key=lambda event: (
            event.received_at_ms,
            event.server_event_time_ms,
            event.trace_ref,
        ),
    )
    if not candidates:
        return None, ("no_source_for_rule",)
    rejected: list[str] = []
    last_decision: Optional[SignalDecision] = None
    for event in candidates:
        decision = evaluate_rule_signal(
            rule=rule,
            source_event=event,
            decision_at_ms=event.received_at_ms,
            threshold=config.signal_threshold,
            max_source_age_ms=config.max_source_age_ms,
            source_clock=source_clock,
        )
        last_decision = decision
        if decision.accepted:
            return decision, tuple(dict.fromkeys(rejected))
        rejected.extend(decision.reject_reasons)
    return last_decision, tuple(dict.fromkeys(rejected))


def _matching_quotes(
    quote_events: Sequence[ClobQuoteEvent],
    *,
    rule: RuleWindow,
    outcome: str,
) -> tuple[ClobQuoteEvent, ...]:
    return tuple(
        event
        for event in quote_events
        if event.market_id == rule.market_id and event.outcome == outcome
    )


def _execute_taker_signal(
    *,
    scenario: ExecutionScenario,
    rule: RuleWindow,
    signal: SignalDecision,
    placement_at_ms: int,
    quote: ClobQuoteEvent,
    config: LatencyStrategyConfig,
) -> ScenarioExecution:
    if (
        quote.best_bid % config.tick_size != ZERO
        or quote.best_ask % config.tick_size != ZERO
    ):
        return _rejected_execution(
            scenario=scenario,
            rule=rule,
            action=config.action,
            reasons=("quote_tick_mismatch",),
            signal=signal,
            placement_at_ms=placement_at_ms,
            quote=quote,
        )
    if quote.best_ask_size is None:
        return _rejected_execution(
            scenario=scenario,
            rule=rule,
            action=config.action,
            reasons=("unknown_visible_ask_depth",),
            signal=signal,
            placement_at_ms=placement_at_ms,
            quote=quote,
        )
    if quote.best_ask_size < config.quantity:
        return _rejected_execution(
            scenario=scenario,
            rule=rule,
            action=config.action,
            reasons=("insufficient_visible_ask_depth",),
            signal=signal,
            placement_at_ms=placement_at_ms,
            quote=quote,
        )
    if rule.resolved_outcome is None:
        return _rejected_execution(
            scenario=scenario,
            rule=rule,
            action=config.action,
            reasons=("unknown_resolution",),
            signal=signal,
            placement_at_ms=placement_at_ms,
            quote=quote,
        )
    quantity = config.quantity
    fill_price = quote.best_ask
    notional = quantity * fill_price
    fee = _fee(
        quantity=quantity,
        price=fill_price,
        config=config,
        scenario=scenario,
        maker=False,
    )
    slippage = quantity * scenario.slippage_per_share
    payout = quantity if signal.outcome == rule.resolved_outcome else ZERO
    gross = payout - notional
    return ScenarioExecution(
        scenario=scenario.name,
        market_id=rule.market_id,
        action=config.action,
        status="filled",
        outcome=signal.outcome,
        decision_at_ms=signal.decision_at_ms,
        placement_at_ms=placement_at_ms,
        rule_trace_ref=rule.trace_ref,
        source_trace_ref=signal.source_trace_ref,
        quote_trace_ref=quote.trace_ref,
        fill_price=fill_price,
        fill_quantity=quantity,
        payout=payout,
        notional=notional,
        fee=fee,
        slippage_cost=slippage,
        gross_profit=gross,
        net_profit=gross - fee - slippage,
    )


def _execute_maker_signal(
    *,
    scenario: ExecutionScenario,
    rule: RuleWindow,
    signal: SignalDecision,
    placement_at_ms: int,
    quote: ClobQuoteEvent,
    trades: Sequence[Trade],
    config: LatencyStrategyConfig,
    clob_clock: Optional[ClockEstimate],
) -> ScenarioExecution:
    if (
        quote.best_bid % config.tick_size != ZERO
        or quote.best_ask % config.tick_size != ZERO
    ):
        return _rejected_execution(
            scenario=scenario,
            rule=rule,
            action=config.action,
            reasons=("quote_tick_mismatch",),
            signal=signal,
            placement_at_ms=placement_at_ms,
            quote=quote,
        )
    limit = quote.best_bid + config.maker_price_improvement
    if limit >= quote.best_ask or limit > ONE:
        return _rejected_execution(
            scenario=scenario,
            rule=rule,
            action=config.action,
            reasons=("maker_quote_would_cross",),
            signal=signal,
            placement_at_ms=placement_at_ms,
            quote=quote,
        )
    if quote.best_bid_size is None:
        return _rejected_execution(
            scenario=scenario,
            rule=rule,
            action=config.action,
            reasons=("unknown_visible_bid_queue",),
            signal=signal,
            placement_at_ms=placement_at_ms,
            quote=quote,
        )
    if clob_clock is None:
        return _rejected_execution(
            scenario=scenario,
            rule=rule,
            action=config.action,
            reasons=("maker_trade_clock_unverified",),
            signal=signal,
            placement_at_ms=placement_at_ms,
            quote=quote,
        )
    clock_reasons = _clock_reject_reasons(
        clob_clock, placement_at_ms, prefix="maker"
    )
    if clock_reasons:
        return _rejected_execution(
            scenario=scenario,
            rule=rule,
            action=config.action,
            reasons=clock_reasons,
            signal=signal,
            placement_at_ms=placement_at_ms,
            quote=quote,
        )
    queue = (
        quote.best_bid_size + config.maker_queue_ahead
    ) * scenario.maker_queue_multiplier
    remaining = config.quantity
    evidence_trades: list[str] = []
    for trade in sorted(
        trades,
        key=lambda event: (
            event.received_at_ms,
            event.server_event_time_ms,
            event.trace_ref,
        ),
    ):
        trade_lower_time, _ = clob_clock.local_time_bounds(
            trade.server_event_time_ms
        )
        if (
            trade.market_id != rule.market_id
            or trade.token_id != quote.token_id
            or trade.outcome != signal.outcome
            or trade.aggressor_side != "SELL"
            or trade.received_at_ms <= placement_at_ms
            or trade_lower_time <= Decimal(placement_at_ms)
            or not clob_clock.valid_at(trade.received_at_ms)
            or trade.received_at_ms > rule.closes_at_ms
            or trade.is_stale
            or trade.price > limit
        ):
            continue
        volume = trade.size
        evidence_trades.append(trade.trace_ref)
        if queue > ZERO:
            consumed = min(queue, volume)
            queue -= consumed
            volume -= consumed
        if volume <= ZERO:
            continue
        fill = min(remaining, volume)
        if fill > ZERO:
            remaining -= fill
        if remaining == ZERO:
            break
    filled = config.quantity - remaining
    if filled == ZERO:
        return _rejected_execution(
            scenario=scenario,
            rule=rule,
            action=config.action,
            reasons=("no_confirmed_maker_fill",),
            signal=signal,
            placement_at_ms=placement_at_ms,
            quote=quote,
            trade_trace_refs=tuple(evidence_trades),
        )
    if rule.resolved_outcome is None:
        return _rejected_execution(
            scenario=scenario,
            rule=rule,
            action=config.action,
            reasons=("unknown_resolution",),
            signal=signal,
            placement_at_ms=placement_at_ms,
            quote=quote,
        )
    notional = filled * limit
    fee = _fee(
        quantity=filled,
        price=limit,
        config=config,
        scenario=scenario,
        maker=True,
    )
    slippage = filled * scenario.slippage_per_share
    payout = filled if signal.outcome == rule.resolved_outcome else ZERO
    gross = payout - notional
    return ScenarioExecution(
        scenario=scenario.name,
        market_id=rule.market_id,
        action=config.action,
        status="filled" if remaining == ZERO else "partial",
        outcome=signal.outcome,
        decision_at_ms=signal.decision_at_ms,
        placement_at_ms=placement_at_ms,
        rule_trace_ref=rule.trace_ref,
        source_trace_ref=signal.source_trace_ref,
        quote_trace_ref=quote.trace_ref,
        trade_trace_refs=tuple(evidence_trades),
        fill_price=limit,
        fill_quantity=filled,
        payout=payout,
        notional=notional,
        fee=fee,
        slippage_cost=slippage,
        gross_profit=gross,
        net_profit=gross - fee - slippage,
    )


def _run_scenario(
    *,
    scenario: ExecutionScenario,
    source_events: Sequence[SourcePriceEvent],
    quote_events: Sequence[ClobQuoteEvent],
    trades: Sequence[Trade],
    rules: Sequence[RuleWindow],
    config: LatencyStrategyConfig,
    source_clock: Optional[ClockEstimate],
    clob_clock: Optional[ClockEstimate],
) -> LatencyScenarioResult:
    executions: list[ScenarioExecution] = []
    for rule in sorted(
        rules,
        key=lambda item: (item.closes_at_ms, item.market_id, item.trace_ref),
    ):
        signal, signal_rejections = _first_signal(
            rule=rule,
            source_events=source_events,
            config=config,
            source_clock=source_clock,
        )
        if signal is None or not signal.accepted:
            reasons = signal_rejections or (
                signal.reject_reasons if signal is not None else ()
            )
            executions.append(
                _rejected_execution(
                    scenario=scenario,
                    rule=rule,
                    action=config.action,
                    reasons=reasons or ("no_accepted_signal",),
                    signal=signal,
                )
            )
            continue
        placement_at_ms = signal.decision_at_ms + scenario.placement_latency_ms
        timing_reasons: list[str] = []
        if placement_at_ms > rule.entry_deadline_ms:
            timing_reasons.append("placement_after_entry_deadline")
        if placement_at_ms > rule.closes_at_ms:
            timing_reasons.append("placement_after_rule_close")
        if timing_reasons:
            executions.append(
                _rejected_execution(
                    scenario=scenario,
                    rule=rule,
                    action=config.action,
                    reasons=timing_reasons,
                    signal=signal,
                    placement_at_ms=placement_at_ms,
                )
            )
            continue

        assert signal.outcome is not None
        candidate_quotes = _matching_quotes(
            quote_events, rule=rule, outcome=signal.outcome
        )
        selected_quote, quote_reasons = _usable_quote_asof(
            candidate_quotes,
            at_ms=placement_at_ms,
            max_age_ms=config.max_quote_age_ms,
            clock=clob_clock,
            at_capture_seq=(
                signal.source_capture_seq
                if placement_at_ms == signal.decision_at_ms
                else None
            ),
        )
        if quote_reasons or selected_quote is None:
            executions.append(
                _rejected_execution(
                    scenario=scenario,
                    rule=rule,
                    action=config.action,
                    reasons=quote_reasons or ("no_executable_quote",),
                    signal=signal,
                    placement_at_ms=placement_at_ms,
                    quote=selected_quote,
                )
            )
            continue
        if config.action == "taker":
            execution = _execute_taker_signal(
                scenario=scenario,
                rule=rule,
                signal=signal,
                placement_at_ms=placement_at_ms,
                quote=selected_quote,
                config=config,
            )
        else:
            execution = _execute_maker_signal(
                scenario=scenario,
                rule=rule,
                signal=signal,
                placement_at_ms=placement_at_ms,
                quote=selected_quote,
                trades=trades,
                config=config,
                clob_clock=clob_clock,
            )
        executions.append(execution)
    return LatencyScenarioResult(scenario.name, tuple(executions))


def run_latency_backtest(
    *,
    source_events: Iterable[SourcePriceEvent],
    quote_events: Iterable[ClobQuoteEvent],
    trades: Iterable[Trade],
    rules: Iterable[RuleWindow],
    config: LatencyStrategyConfig,
    scenarios: Optional[Sequence[ExecutionScenario]] = None,
    source_clock: Optional[ClockEstimate] = None,
    clob_clock: Optional[ClockEstimate] = None,
) -> LatencyBacktestResult:
    """Replay selective signals under deterministic executable frictions."""

    source_rows = tuple(source_events)
    quote_rows = tuple(quote_events)
    trade_rows = tuple(trades)
    rule_rows = tuple(rules)
    if not rule_rows:
        raise ValueError("at least one rule is required")
    market_ids = tuple(rule.market_id for rule in rule_rows)
    if len(set(market_ids)) != len(market_ids):
        raise ValueError("duplicate market_id rules would reuse execution evidence")
    if len(rule_rows) != 1:
        raise ValueError(
            "latency execution runs are per-market because fee, tick, minimum "
            "size, book depth, and trade capacity are market-versioned; "
            "aggregate independent results through experiments"
        )
    if rule_rows[0].market_id != config.market_id:
        raise ValueError("strategy config market_id does not match the rule")
    scenario_rows = tuple(scenarios or standard_execution_scenarios(config))
    names = tuple(scenario.name for scenario in scenario_rows)
    if len(set(names)) != len(names):
        raise ValueError("scenario names must be unique")
    required = {"ideal", "optimistic", "neutral", "pessimistic"}
    if scenarios is None and set(names) != required:  # pragma: no cover
        raise AssertionError("standard scenario grid is incomplete")

    results = tuple(
        _run_scenario(
            scenario=scenario,
            source_events=source_rows,
            quote_events=quote_rows,
            trades=trade_rows,
            rules=rule_rows,
            config=config,
            source_clock=source_clock,
            clob_clock=clob_clock,
        )
        for scenario in scenario_rows
    )
    by_name = {result.name: result for result in results}
    reasons: list[str] = []
    ideal = by_name.get("ideal")
    realistic = [
        by_name[name]
        for name in ("optimistic", "neutral", "pessimistic")
        if name in by_name
    ]
    if (
        ideal is not None
        and ideal.net_profit > ZERO
        and realistic
        and all(result.net_profit <= ZERO for result in realistic)
    ):
        classification = "rejected"
        reasons.append("ideal_only_latency_edge")
    elif (
        "pessimistic" in by_name
        and by_name["pessimistic"].fill_count > 0
        and by_name["pessimistic"].net_profit > ZERO
    ):
        independent_settled_events = len(
            {
                execution.market_id
                for execution in by_name["pessimistic"].executions
                if execution.filled
            }
        )
        if independent_settled_events < 100:
            classification = "insufficient_data"
            reasons.append("independent_settled_events<100")
        elif by_name["pessimistic"].fill_count < 100:
            classification = "insufficient_data"
            reasons.append("pessimistic_fills<100")
        else:  # Per-market runs deliberately cannot reach this branch alone.
            classification = "promising_not_validated"
    elif all(result.fill_count == 0 for result in results):
        classification = "insufficient_data"
        reasons.append("no_executable_fills")
    else:
        classification = "rejected"
        reasons.append("realistic_net_profit_non_positive")
    return LatencyBacktestResult(
        scenarios=results,
        classification=classification,
        reject_reasons=tuple(reasons),
        fee_source_ref=config.fee_source_ref,
    )
