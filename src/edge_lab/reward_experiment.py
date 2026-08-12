"""Reproducible, public-only selective liquidity-reward experiment.

The experiment deliberately does not know how to authenticate, sign, or place
an order.  It joins three official public observations:

* the current reward-market universe;
* each selected condition's compact CLOB configuration; and
* both outcome-token L2 books.

Public books cannot reveal maker identity, queue position, order age, actual
scoring, or payout.  The output therefore keeps theoretical reward, scoring,
and payout ledgers separate and caps every otherwise interesting result at
``promising_not_validated``.  The execution replay is used only to stress a
hypothetical one-leg maker fill against currently executable hedge depth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import time
from typing import Any, Callable, Iterable, Mapping, Protocol

from .execution import (
    BASE_UNIT,
    DepthBook,
    ExecutionFeeSchedule,
    ExecutionStatus,
    FEE_QUANTUM,
    Ledger,
    OrderSide,
    TimeInForce,
    TraceRef,
    execute_taker,
)
from .rewards import (
    PublicRewardBook,
    RewardLevel,
    RewardMarketConfig,
    RewardTheoretical,
    apply_reward_haircuts,
    classify_reward_stress,
    reward_validation_ceiling,
    score_public_reward_book,
)
from .sources import (
    CompactCLOBMarket,
    Fetched,
    PublicSourceError,
    RawResponse,
    RewardPage,
    SourceBook,
)


ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
DEFAULT_COMPETITION_MULTIPLIER = Decimal("3")
DEFAULT_REWARD_ASSET_ID = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
HYPOTHETICAL_MAKER_REF = "public-hypothetical-maker-no-identity"
UNCONFIRMED_REWARD_ASSET = "public-current-reward-asset"


class RewardExperimentSources(Protocol):
    """The public-source subset used by the experiment."""

    def current_reward_pages(
        self,
        *,
        limit: int = 500,
        cursor: str | None = None,
        max_pages: int | None = None,
    ) -> Iterable[Fetched[RewardPage]]: ...

    def clob_market(
        self,
        condition_id: str,
    ) -> Fetched[CompactCLOBMarket]: ...

    def book(self, token_id: str) -> Fetched[SourceBook]: ...


def _finite_decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _conditional_theoretical_dict(
    value: RewardTheoretical,
    *,
    maker_aggregation_semantics: str,
) -> dict[str, Any]:
    """Serialize public reward math without implying a maker-score bound."""

    return {
        "ledger": "theoretical",
        "condition_id": value.condition_id,
        "midpoint": str(value.midpoint),
        "q_one_with_zero_visible_age_verification": str(value.q_one_lower),
        "conditional_q_one_if_all_visible_levels_aged": str(
            value.q_one_upper
        ),
        "q_two_with_zero_visible_age_verification": str(value.q_two_lower),
        "conditional_q_two_if_all_visible_levels_aged": str(
            value.q_two_upper
        ),
        "combined_score_with_zero_visible_age_verification": str(
            value.score_lower
        ),
        "conditional_combined_score_if_all_visible_levels_aged": str(
            value.score_upper
        ),
        "reward_with_zero_visible_age_verification": str(value.reward_lower),
        "conditional_full_pool_if_score_positive_before_competition": str(
            value.reward_upper
        ),
        "maker_aggregation_semantics": maker_aggregation_semantics,
        "recognized_pnl": str(value.recognized_pnl),
        "components": {
            name: str(component) for name, component in value.components
        },
        "rejection_reasons": list(value.rejection_reasons),
        "notes": list(value.notes),
    }


def _iso_timestamp(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(
        epoch_seconds,
        tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")


def _normalized_reward_asset_id(value: Any) -> str:
    text = str(value or "").strip()
    if (
        len(text) == 42
        and text.startswith(("0x", "0X"))
        and all(character in "0123456789abcdefABCDEF" for character in text[2:])
    ):
        return "0x" + text[2:].lower()
    return text


def _source_evidence(raw: RawResponse) -> dict[str, Any]:
    metadata = raw.metadata
    return {
        "source": metadata.source,
        "method": metadata.method,
        "url": metadata.url,
        "request_params": _json_ready(metadata.request_params),
        "status_code": metadata.status_code,
        "requested_at": _iso_timestamp(metadata.requested_at),
        "received_at": _iso_timestamp(metadata.received_at),
        "attempt": metadata.attempt,
        "body_bytes": len(raw.body),
        "sha256": hashlib.sha256(raw.body).hexdigest(),
    }


@dataclass(frozen=True)
class RewardExperimentConfig:
    """All scenario choices persisted in the output."""

    max_markets: int = 10
    max_reward_pages: int = 25
    max_book_age_ms: int = 120_000
    max_book_skew_ms: int = 5_000
    quote_distance_fraction: Decimal = Decimal("0.5")
    competition_multipliers: tuple[Decimal, ...] = (
        Decimal("1"),
        DEFAULT_COMPETITION_MULTIPLIER,
        Decimal("10"),
    )
    paired_fills_per_day: int = 1
    adverse_one_leg_fills_per_day: int = 1
    reward_asset_id: str = DEFAULT_REWARD_ASSET_ID

    def __post_init__(self) -> None:
        if self.max_markets < 1:
            raise ValueError("max_markets must be at least one")
        if self.max_reward_pages < 1:
            raise ValueError("max_reward_pages must be at least one")
        if self.max_book_age_ms < 0 or self.max_book_skew_ms < 0:
            raise ValueError("book age and skew limits cannot be negative")
        if not isinstance(self.quote_distance_fraction, Decimal):
            raise TypeError("quote_distance_fraction must be Decimal")
        if (
            not self.quote_distance_fraction.is_finite()
            or not ZERO < self.quote_distance_fraction < ONE
        ):
            raise ValueError(
                "quote_distance_fraction must be strictly between zero and one"
            )
        if not self.competition_multipliers:
            raise ValueError("competition_multipliers cannot be empty")
        if any(
            not isinstance(multiplier, Decimal)
            or not multiplier.is_finite()
            or multiplier < ONE
            for multiplier in self.competition_multipliers
        ):
            raise ValueError(
                "competition multipliers must be Decimal values at least one"
            )
        if len(set(self.competition_multipliers)) != len(
            self.competition_multipliers
        ):
            raise ValueError("competition_multipliers must be unique")
        if DEFAULT_COMPETITION_MULTIPLIER not in self.competition_multipliers:
            raise ValueError("competition_multipliers must include Decimal('3')")
        if self.paired_fills_per_day < 0:
            raise ValueError("paired_fills_per_day cannot be negative")
        if self.adverse_one_leg_fills_per_day < 0:
            raise ValueError(
                "adverse_one_leg_fills_per_day cannot be negative"
            )
        if (
            not isinstance(self.reward_asset_id, str)
            or not self.reward_asset_id.strip()
        ):
            raise ValueError("reward_asset_id must be a non-empty string")
        object.__setattr__(
            self,
            "reward_asset_id",
            _normalized_reward_asset_id(self.reward_asset_id),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_markets": self.max_markets,
            "max_reward_pages": self.max_reward_pages,
            "max_book_age_ms": self.max_book_age_ms,
            "max_book_skew_ms": self.max_book_skew_ms,
            "quote_distance_fraction": str(self.quote_distance_fraction),
            "competition_multipliers": [
                str(value) for value in self.competition_multipliers
            ],
            "paired_fills_per_day": self.paired_fills_per_day,
            "adverse_one_leg_fills_per_day": (
                self.adverse_one_leg_fills_per_day
            ),
            "reward_asset_id": self.reward_asset_id,
        }


def _empty_evidence_ledgers(
    condition_id: str,
    reward_asset_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    ceiling = reward_validation_ceiling(
        (),
        (),
        condition_id=condition_id,
        payout_asset_id=reward_asset_id,
        maker_evidence_ref=HYPOTHETICAL_MAKER_REF,
    ).to_dict()
    scoring = {
        "ledger": "scoring",
        "records": [],
        "recognized_pnl": "0",
        "collection_status": "not_collected",
        "reason": (
            "Order-specific scoring requires authenticated maker-order "
            "evidence; this experiment never authenticates."
        ),
    }
    payout = {
        "ledger": "payout",
        "records": [],
        "recognized_pnl": "0",
        "collection_status": "not_collected",
        "reason": (
            "No maker identity or confirmed payment evidence is available "
            "from the public endpoints used by this experiment."
        ),
    }
    return scoring, payout, ceiling


def _zero_recognized_economics() -> dict[str, Any]:
    """Fields that prevent an unfilled scenario from being read as PnL."""

    return {
        "profit_claim": "none",
        "recognized_trading_pnl": "0",
        "recognized_reward_pnl": "0",
        "recognized_total_pnl": "0",
        "actual_fill_count": 0,
        "explainable_fill_count": 0,
    }


def _failed_market(
    row: Mapping[str, Any],
    *,
    classification: str,
    reasons: Iterable[str],
    source_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    condition_id = str(row.get("condition_id") or "")
    scoring, payout, ceiling = _empty_evidence_ledgers(
        condition_id or "unknown-condition",
        UNCONFIRMED_REWARD_ASSET,
    )
    return {
        "condition_id": condition_id,
        **_zero_recognized_economics(),
        "classification": classification,
        "classification_basis": "missing_or_rejected_public_evidence",
        "failure_reasons": list(dict.fromkeys(reasons)),
        "validation_failures": ceiling["missing_evidence"],
        "current_reward_row": _json_ready(row),
        "source_evidence": dict(source_evidence or {}),
        "theoretical_ledger": {
            "ledger": "theoretical",
            "recognized_pnl": "0",
            "status": "not_computed",
        },
        "scoring_ledger": scoring,
        "payout_ledger": payout,
        "validation_ceiling": ceiling,
    }


def _token_pair(
    market: CompactCLOBMarket,
) -> tuple[str, str]:
    if len(market.tokens) != 2:
        raise ValueError("compact_market_not_two_token_binary")
    pairs = tuple(
        (
            str(token.get("o") or "").strip(),
            str(token.get("t") or "").strip(),
        )
        for token in market.tokens
    )
    if any(not outcome or not token_id for outcome, token_id in pairs):
        raise ValueError("compact_market_missing_outcome_or_token_id")
    if pairs[0][1] == pairs[1][1]:
        raise ValueError("compact_market_duplicate_token_id")
    by_outcome = {outcome.lower(): token_id for outcome, token_id in pairs}
    if set(by_outcome) == {"yes", "no"}:
        return by_outcome["yes"], by_outcome["no"]
    # Arbitrary binary labels (candidate names, teams, etc.) are valid.  The
    # first compact token is the current side and the second its complement.
    return pairs[0][1], pairs[1][1]


def _best_bid(book: SourceBook) -> Decimal:
    if not book.bids:
        raise ValueError("book_missing_bid_depth")
    return max(level.price for level in book.bids)


def _best_ask(book: SourceBook) -> Decimal:
    if not book.asks:
        raise ValueError("book_missing_ask_depth")
    return min(level.price for level in book.asks)


def _combined_yes_midpoint(
    yes_book: SourceBook,
    no_book: SourceBook,
) -> Decimal:
    yes_bid = _best_bid(yes_book)
    yes_ask = _best_ask(yes_book)
    no_bid = _best_bid(no_book)
    no_ask = _best_ask(no_book)
    if yes_bid >= yes_ask or no_bid >= no_ask:
        raise ValueError("book_is_crossed_or_locked")
    yes_mid = (yes_bid + yes_ask) / Decimal("2")
    yes_from_no_mid = ONE - ((no_bid + no_ask) / Decimal("2"))
    midpoint = (yes_mid + yes_from_no_mid) / Decimal("2")
    if not ZERO < midpoint < ONE:
        raise ValueError("combined_midpoint_out_of_range")
    return midpoint


def _reward_level(
    price: Decimal,
    size: Decimal,
    *,
    age_seconds: Decimal | None,
    evidence_ref: str,
) -> RewardLevel:
    return RewardLevel(
        price=price,
        size=size,
        age_seconds=age_seconds,
        evidence_ref=evidence_ref,
    )


def _public_reward_book(
    yes_book: SourceBook,
    no_book: SourceBook,
    *,
    age_seconds: Decimal | None,
    yes_evidence_ref: str,
    no_evidence_ref: str,
) -> PublicRewardBook:
    return PublicRewardBook(
        current_bids=tuple(
            _reward_level(
                level.price,
                level.size,
                age_seconds=age_seconds,
                evidence_ref=yes_evidence_ref,
            )
            for level in yes_book.bids
        ),
        current_asks=tuple(
            _reward_level(
                level.price,
                level.size,
                age_seconds=age_seconds,
                evidence_ref=yes_evidence_ref,
            )
            for level in yes_book.asks
        ),
        complement_bids=tuple(
            _reward_level(
                level.price,
                level.size,
                age_seconds=age_seconds,
                evidence_ref=no_evidence_ref,
            )
            for level in no_book.bids
        ),
        complement_asks=tuple(
            _reward_level(
                level.price,
                level.size,
                age_seconds=age_seconds,
                evidence_ref=no_evidence_ref,
            )
            for level in no_book.asks
        ),
    )


def _round_down_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    units = (price / tick).to_integral_value(rounding=ROUND_FLOOR)
    return min(max(units * tick, tick), ONE - tick)


def _candidate_bid(
    midpoint: Decimal,
    *,
    maximum_spread_cents: Decimal,
    fraction: Decimal,
    tick: Decimal,
    best_ask: Decimal,
) -> Decimal:
    price = _round_down_to_tick(
        midpoint - maximum_spread_cents * CENT * fraction,
        tick,
    )
    if price >= best_ask:
        price = _round_down_to_tick(best_ask - tick, tick)
    return price


def _base_quantity(value: Decimal) -> Decimal:
    return value.quantize(BASE_UNIT, rounding=ROUND_CEILING)


def _depth_book(
    book: SourceBook,
    market: CompactCLOBMarket,
) -> DepthBook:
    return DepthBook.from_tuples(
        book.token_id,
        bids=((level.price, level.size) for level in book.bids),
        asks=((level.price, level.size) for level in book.asks),
        tick_size=market.tick_size,
        min_order_size=market.min_order_size,
        timestamp_ms=book.timestamp_ms,
    )


def _simulate_one_leg_hedge(
    *,
    condition_id: str,
    yes_token_id: str,
    no_token_id: str,
    filled_token_id: str,
    filled_price: Decimal,
    hedge_source_book: SourceBook,
    market: CompactCLOBMarket,
    fee_schedule: ExecutionFeeSchedule,
    quantity: Decimal,
    decision_timestamp_ms: int,
    max_book_age_ms: int,
) -> dict[str, Any]:
    initial_cash = Decimal("1000000.000000")
    ledger = Ledger(initial_cash)
    ledger.register_binary_market(
        condition_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
    )
    maker_fee = fee_schedule.fee(quantity, filled_price, maker=True)
    ledger.buy(
        filled_token_id,
        quantity,
        filled_price,
        fee=maker_fee,
        trace=TraceRef(
            source_event_id=f"hypothetical-maker-fill:{filled_token_id}",
            decision_id=f"reward-stress:{condition_id}",
            timestamp_ms=decision_timestamp_ms,
        ),
    )
    hedge_book = _depth_book(hedge_source_book, market)
    limit_price = ONE - market.tick_size
    result = execute_taker(
        ledger,
        hedge_book,
        side=OrderSide.BUY,
        quantity=quantity,
        limit_price=limit_price,
        time_in_force=TimeInForce.FOK,
        fee_schedule=fee_schedule,
        trace=TraceRef(
            source_event_id=f"public-book-hedge:{hedge_source_book.token_id}",
            decision_id=f"reward-stress:{condition_id}",
            timestamp_ms=decision_timestamp_ms,
        ),
        execution_latency_ms=0,
        max_book_age_ms=max_book_age_ms,
    )
    if result.status is not ExecutionStatus.FILLED:
        return {
            "complete": False,
            "execution_status": result.status.value,
            "failure_reason": result.reason,
            "requested_quantity": str(quantity),
            "filled_quantity": str(result.filled_quantity),
            "maker_fee": str(maker_fee),
            "taker_fee": str(result.fee),
            "conditional_complete_set_cashflow_before_operation_cost": None,
        }
    total_spent = initial_cash - ledger.cash
    return {
        "complete": True,
        "execution_status": result.status.value,
        "failure_reason": None,
        "requested_quantity": str(quantity),
        "filled_quantity": str(result.filled_quantity),
        "hedge_average_price": (
            str(result.average_price)
            if result.average_price is not None
            else None
        ),
        "maker_fee": str(maker_fee),
        "taker_fee": str(result.fee),
        "total_fee": str(maker_fee + result.fee),
        "total_acquisition_cost": str(total_spent),
        "conditional_complete_set_cashflow_before_operation_cost": str(
            quantity - total_spent
        ),
        "execution_fills": [
            {
                "token_id": fill.token_id,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "fee": str(fill.fee),
                "liquidity_role": fill.liquidity_role.value,
            }
            for fill in result.fills
        ],
    }


def _reward_asset_ids(row: Mapping[str, Any]) -> tuple[str, ...]:
    configs = row.get("rewards_config")
    if not isinstance(configs, list):
        return ()
    ids = {
        _normalized_reward_asset_id(config.get("asset_address"))
        for config in configs
        if isinstance(config, Mapping)
    }
    return tuple(sorted(asset for asset in ids if asset))


def _conditional_reward_sensitivity(
    *,
    yes_book: SourceBook,
    no_book: SourceBook,
    market: CompactCLOBMarket,
    reward_config: RewardMarketConfig,
    base_midpoint: Decimal,
    daily_reward: Decimal,
    quote_size: Decimal,
    capital_risk_denominator: Decimal,
    config: RewardExperimentConfig,
    yes_evidence_ref: str,
    no_evidence_ref: str,
) -> dict[str, Any]:
    """Stress the public snapshot without presenting it as an expectation."""

    assert reward_config.maximum_spread_cents is not None
    assert reward_config.minimum_order_age_seconds is not None
    rows: list[dict[str, Any]] = []
    age_scenarios = (
        ("zero_visible_levels_age_verified", None),
        (
            "all_visible_levels_aged",
            reward_config.minimum_order_age_seconds,
        ),
    )
    for shift_ticks in (-1, 0, 1):
        midpoint = min(
            max(
                base_midpoint + market.tick_size * shift_ticks,
                market.tick_size,
            ),
            ONE - market.tick_size,
        )
        yes_bid = _candidate_bid(
            midpoint,
            maximum_spread_cents=reward_config.maximum_spread_cents,
            fraction=config.quote_distance_fraction,
            tick=market.tick_size,
            best_ask=_best_ask(yes_book),
        )
        no_bid = _candidate_bid(
            ONE - midpoint,
            maximum_spread_cents=reward_config.maximum_spread_cents,
            fraction=config.quote_distance_fraction,
            tick=market.tick_size,
            best_ask=_best_ask(no_book),
        )
        candidate = score_public_reward_book(
            config=reward_config,
            midpoint=midpoint,
            book=PublicRewardBook(
                current_bids=(
                    _reward_level(
                        yes_bid,
                        quote_size,
                        age_seconds=(
                            reward_config.minimum_order_age_seconds
                        ),
                        evidence_ref="conditional-candidate-yes-bid",
                    ),
                ),
                complement_bids=(
                    _reward_level(
                        no_bid,
                        quote_size,
                        age_seconds=(
                            reward_config.minimum_order_age_seconds
                        ),
                        evidence_ref="conditional-candidate-no-bid",
                    ),
                ),
            ),
            daily_reward_pool=daily_reward,
        )
        for age_label, public_age in age_scenarios:
            public = score_public_reward_book(
                config=reward_config,
                midpoint=midpoint,
                book=_public_reward_book(
                    yes_book,
                    no_book,
                    age_seconds=public_age,
                    yes_evidence_ref=yes_evidence_ref,
                    no_evidence_ref=no_evidence_ref,
                ),
                daily_reward_pool=daily_reward,
            )
            for multiplier in config.competition_multipliers:
                denominator = (
                    candidate.score_upper
                    + public.score_upper * multiplier
                )
                share = (
                    candidate.score_upper / denominator
                    if denominator > ZERO
                    else ZERO
                )
                allocation = daily_reward * share
                scenario_id = (
                    f"midpoint_shift_ticks={shift_ticks};"
                    f"public_age={age_label};"
                    f"competition_multiplier={multiplier}"
                )
                rows.append(
                    {
                        "scenario_id": scenario_id,
                        "reference_midpoint_proxy": str(midpoint),
                        "midpoint_shift_ticks": shift_ticks,
                        "public_age_assumption": age_label,
                        "competition_multiplier": str(multiplier),
                        "candidate_score_proxy": str(
                            candidate.score_upper
                        ),
                        "aggregate_public_book_score_proxy": str(
                            public.score_upper
                        ),
                        "conditional_pool_share": str(share),
                        "conditional_pool_allocation": str(allocation),
                        "conditional_reward_to_baseline_capital_risk_proxy": str(
                            allocation / capital_risk_denominator
                        ),
                    }
                )
    return {
        "evidence_kind": "conditional",
        "capital_risk_denominator_held_at_baseline": True,
        "platform_midpoint_verified": False,
        "scenarios": rows,
    }


def _evaluate_market(
    sources: RewardExperimentSources,
    row: Mapping[str, Any],
    *,
    current_row_evidence: Mapping[str, Any],
    config: RewardExperimentConfig,
    clock: Callable[[], float],
) -> dict[str, Any]:
    condition_id = str(row.get("condition_id") or "").strip()
    if not condition_id:
        return _failed_market(
            row,
            classification="rejected",
            reasons=("missing_condition_id",),
            source_evidence={"current_rewards": current_row_evidence},
        )
    try:
        daily_reward = _finite_decimal(
            row.get("total_daily_rate"),
            name="total_daily_rate",
        )
        row_min_size = _finite_decimal(
            row.get("rewards_min_size"),
            name="rewards_min_size",
        )
        row_max_spread = _finite_decimal(
            row.get("rewards_max_spread"),
            name="rewards_max_spread",
        )
    except ValueError:
        return _failed_market(
            row,
            classification="rejected",
            reasons=("invalid_current_reward_numeric_field",),
            source_evidence={"current_rewards": current_row_evidence},
        )
    if daily_reward <= ZERO:
        return _failed_market(
            row,
            classification="rejected",
            reasons=("non_positive_current_daily_reward",),
            source_evidence={"current_rewards": current_row_evidence},
        )

    source_evidence: dict[str, Any] = {
        "current_rewards": dict(current_row_evidence),
    }
    try:
        compact_fetched = sources.clob_market(condition_id)
    except (PublicSourceError, ValueError, RuntimeError) as exc:
        return _failed_market(
            row,
            classification="insufficient_data",
            reasons=(f"compact_market_source_error:{type(exc).__name__}",),
            source_evidence=source_evidence,
        )
    market = compact_fetched.value
    source_evidence["compact_market"] = _source_evidence(
        compact_fetched.raw
    )

    hard_rejections: list[str] = []
    if not market.accepting_orders:
        hard_rejections.append("compact_market_not_accepting_orders")
    reward_config = RewardMarketConfig.from_compact_market(market.raw)
    hard_rejections.extend(reward_config.rejection_reasons)
    if market.rewards.min_size != row_min_size:
        hard_rejections.append("current_compact_min_size_mismatch")
    if market.rewards.max_spread != row_max_spread:
        hard_rejections.append("current_compact_max_spread_mismatch")
    try:
        yes_token_id, no_token_id = _token_pair(market)
    except ValueError:
        hard_rejections.append("compact_market_token_pair_invalid")
        yes_token_id = ""
        no_token_id = ""
    reward_assets = _reward_asset_ids(row)
    if len(reward_assets) > 1:
        hard_rejections.append("multiple_reward_asset_units_not_comparable")
    if hard_rejections:
        return _failed_market(
            row,
            classification="rejected",
            reasons=hard_rejections,
            source_evidence=source_evidence,
        )

    if market.fees is None:
        return _failed_market(
            row,
            classification="insufficient_data",
            reasons=("missing_complete_fee_schedule",),
            source_evidence=source_evidence,
        )
    fee_schedule = ExecutionFeeSchedule.from_market(
        {
            "c": condition_id,
            "fd": {
                "r": market.fees.rate,
                "e": market.fees.exponent,
                "to": market.fees.taker_only,
            },
        }
    )

    try:
        yes_fetched = sources.book(yes_token_id)
        no_fetched = sources.book(no_token_id)
    except (PublicSourceError, ValueError, RuntimeError) as exc:
        return _failed_market(
            row,
            classification="insufficient_data",
            reasons=(f"book_source_error:{type(exc).__name__}",),
            source_evidence=source_evidence,
        )
    yes_book = yes_fetched.value
    no_book = no_fetched.value
    source_evidence["yes_book"] = _source_evidence(yes_fetched.raw)
    source_evidence["no_book"] = _source_evidence(no_fetched.raw)

    book_failures: list[str] = []
    if yes_book.timestamp_ms is None or no_book.timestamp_ms is None:
        book_failures.append("missing_book_timestamp")
    observed_ms = int(clock() * 1000)
    if yes_book.timestamp_ms is not None and no_book.timestamp_ms is not None:
        if abs(yes_book.timestamp_ms - no_book.timestamp_ms) > (
            config.max_book_skew_ms
        ):
            book_failures.append("book_timestamp_skew_exceeds_limit")
        for label, timestamp_ms in (
            ("yes", yes_book.timestamp_ms),
            ("no", no_book.timestamp_ms),
        ):
            age_ms = observed_ms - timestamp_ms
            if age_ms < 0:
                book_failures.append(f"{label}_book_timestamp_in_future")
            elif age_ms > config.max_book_age_ms:
                book_failures.append(f"{label}_book_too_old")
    try:
        midpoint = _combined_yes_midpoint(yes_book, no_book)
    except ValueError:
        book_failures.append("combined_midpoint_invalid")
        midpoint = ZERO
    if book_failures:
        return _failed_market(
            row,
            classification="insufficient_data",
            reasons=book_failures,
            source_evidence=source_evidence,
        )

    assert reward_config.minimum_size is not None
    assert reward_config.maximum_spread_cents is not None
    assert reward_config.minimum_order_age_seconds is not None
    assumed_age = reward_config.minimum_order_age_seconds
    strict_public = score_public_reward_book(
        config=reward_config,
        midpoint=midpoint,
        book=_public_reward_book(
            yes_book,
            no_book,
            age_seconds=None,
            yes_evidence_ref=source_evidence["yes_book"]["sha256"],
            no_evidence_ref=source_evidence["no_book"]["sha256"],
        ),
        daily_reward_pool=daily_reward,
    )
    competition_upper = score_public_reward_book(
        config=reward_config,
        midpoint=midpoint,
        book=_public_reward_book(
            yes_book,
            no_book,
            age_seconds=assumed_age,
            yes_evidence_ref=source_evidence["yes_book"]["sha256"],
            no_evidence_ref=source_evidence["no_book"]["sha256"],
        ),
        daily_reward_pool=daily_reward,
    )

    quote_size = _base_quantity(
        max(reward_config.minimum_size, market.min_order_size)
    )
    yes_bid = _candidate_bid(
        midpoint,
        maximum_spread_cents=reward_config.maximum_spread_cents,
        fraction=config.quote_distance_fraction,
        tick=market.tick_size,
        best_ask=_best_ask(yes_book),
    )
    no_midpoint = ONE - midpoint
    no_bid = _candidate_bid(
        no_midpoint,
        maximum_spread_cents=reward_config.maximum_spread_cents,
        fraction=config.quote_distance_fraction,
        tick=market.tick_size,
        best_ask=_best_ask(no_book),
    )
    own_theoretical = score_public_reward_book(
        config=reward_config,
        midpoint=midpoint,
        book=PublicRewardBook(
            current_bids=(
                _reward_level(
                    yes_bid,
                    quote_size,
                    age_seconds=assumed_age,
                    evidence_ref="hypothetical-candidate-yes-bid",
                ),
            ),
            complement_bids=(
                _reward_level(
                    no_bid,
                    quote_size,
                    age_seconds=assumed_age,
                    evidence_ref="hypothetical-candidate-no-bid",
                ),
            ),
        ),
        daily_reward_pool=daily_reward,
    )
    own_score = own_theoretical.score_upper
    competition_score = competition_upper.score_upper
    stress_grid: dict[str, Any] = {}
    for multiplier in config.competition_multipliers:
        denominator = own_score + competition_score * multiplier
        static_share = own_score / denominator if denominator > ZERO else ZERO
        stress_grid[str(multiplier)] = {
            "competition_score": str(competition_score * multiplier),
            "conditional_candidate_pool_share": str(static_share),
            "conditional_pool_allocation": str(
                daily_reward * static_share
            ),
        }
    theoretical_daily_reward = _finite_decimal(
        stress_grid[str(DEFAULT_COMPETITION_MULTIPLIER)][
            "conditional_pool_allocation"
        ],
        name="conditional snapshot pool allocation",
    )

    maker_yes_fee = fee_schedule.fee(quote_size, yes_bid, maker=True)
    maker_no_fee = fee_schedule.fee(quote_size, no_bid, maker=True)
    paired_fill_pnl = (
        quote_size
        - quote_size * yes_bid
        - quote_size * no_bid
        - maker_yes_fee
        - maker_no_fee
    )
    yes_fills_first = _simulate_one_leg_hedge(
        condition_id=condition_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        filled_token_id=yes_token_id,
        filled_price=yes_bid,
        hedge_source_book=no_book,
        market=market,
        fee_schedule=fee_schedule,
        quantity=quote_size,
        decision_timestamp_ms=observed_ms,
        max_book_age_ms=config.max_book_age_ms,
    )
    no_fills_first = _simulate_one_leg_hedge(
        condition_id=condition_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        filled_token_id=no_token_id,
        filled_price=no_bid,
        hedge_source_book=yes_book,
        market=market,
        fee_schedule=fee_schedule,
        quantity=quote_size,
        decision_timestamp_ms=observed_ms,
        max_book_age_ms=config.max_book_age_ms,
    )
    paths = (yes_fills_first, no_fills_first)
    if not all(path["complete"] for path in paths):
        reasons = [
            f"one_leg_hedge_incomplete:{path['failure_reason']}"
            for path in paths
            if not path["complete"]
        ]
        return _failed_market(
            row,
            classification="insufficient_data",
            reasons=reasons,
            source_evidence=source_evidence,
        )
    worst_adverse_pnl = min(
        _finite_decimal(
            path[
                "conditional_complete_set_cashflow_before_operation_cost"
            ],
            name="one-leg complete-set PnL",
        )
        for path in paths
    )
    worst_adverse_fee = max(
        _finite_decimal(path["total_fee"], name="one-leg total fee")
        for path in paths
    )
    maximum_taker_fills_per_shadow_hedge = max(
        len(path["execution_fills"]) for path in paths
    )
    maker_fee_incurrences_per_shadow_fill = (
        0 if fee_schedule.taker_only else 1
    )
    maximum_fee_incurrences_per_shadow_one_leg = (
        maximum_taker_fills_per_shadow_hedge
        + maker_fee_incurrences_per_shadow_fill
    )
    fee_incurrences_per_shadow_paired_fill = (
        0 if fee_schedule.taker_only else 2
    )
    non_reward_profit = (
        paired_fill_pnl * config.paired_fills_per_day
        + worst_adverse_pnl * config.adverse_one_leg_fills_per_day
    )
    quote_capital = quote_size * (yes_bid + no_bid)
    one_leg_downside = max(ZERO, -worst_adverse_pnl)
    capital_risk_denominator = quote_capital + one_leg_downside
    conditional_sensitivity = _conditional_reward_sensitivity(
        yes_book=yes_book,
        no_book=no_book,
        market=market,
        reward_config=reward_config,
        base_midpoint=midpoint,
        daily_reward=daily_reward,
        quote_size=quote_size,
        capital_risk_denominator=capital_risk_denominator,
        config=config,
        yes_evidence_ref=source_evidence["yes_book"]["sha256"],
        no_evidence_ref=source_evidence["no_book"]["sha256"],
    )
    theoretical_epoch_ids = (
        (f"public-snapshot:{_iso_timestamp(observed_ms / 1000)}",)
        if theoretical_daily_reward > ZERO
        else ()
    )
    reward_asset_id = (
        reward_assets[0] if reward_assets else UNCONFIRMED_REWARD_ASSET
    )
    haircut_rows = apply_reward_haircuts(
        non_reward_profit=non_reward_profit,
        theoretical_reward=theoretical_daily_reward,
        condition_id=condition_id,
        reward_asset_id=reward_asset_id,
        maker_evidence_ref=HYPOTHETICAL_MAKER_REF,
        theoretical_epoch_ids=theoretical_epoch_ids,
        confirmed_payouts=(),
    )
    stress = classify_reward_stress(haircut_rows)
    shadow_stress_label = (
        f"shadow_{stress.classification}_if_assumed_fills"
    )
    scoring, payout, ceiling = _empty_evidence_ledgers(
        condition_id,
        reward_asset_id,
    )
    classification = "promising_not_validated"
    failure_reasons: list[str] = []
    if not haircut_rows or haircut_rows[0].net_profit <= ZERO:
        classification = "rejected"
        failure_reasons.append(
            "stress_scenario_not_positive_at_full_theoretical_reward"
        )

    strict_book_dict = _conditional_theoretical_dict(
        strict_public,
        maker_aggregation_semantics=(
            "aggregate_public_l2_not_a_maker_score_bound"
        ),
    )
    competition_dict = _conditional_theoretical_dict(
        competition_upper,
        maker_aggregation_semantics=(
            "aggregate_public_l2_not_a_maker_score_bound"
        ),
    )
    own_dict = _conditional_theoretical_dict(
        own_theoretical,
        maker_aggregation_semantics="single_hypothetical_candidate_maker",
    )
    return {
        "condition_id": condition_id,
        **_zero_recognized_economics(),
        "classification": classification,
        "classification_basis": (
            "conditional_public_snapshot_discovery_only"
        ),
        "conditional_robustness_label": stress.classification,
        "failure_reasons": failure_reasons,
        "validation_failures": ceiling["missing_evidence"],
        "current_reward_row": _json_ready(row),
        "compact_market": _json_ready(market.raw),
        "books": {
            "yes": _json_ready(yes_book.raw),
            "no": _json_ready(no_book.raw),
        },
        "source_evidence": source_evidence,
        "reward_asset_ids": list(reward_assets),
        "market_snapshot": {
            "observed_at": _iso_timestamp(observed_ms / 1000),
            "current_token_id": yes_token_id,
            "complement_token_id": no_token_id,
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
            "token_roles": [
                {
                    "role": "current",
                    "token_id": str(market.tokens[0].get("t") or ""),
                    "outcome": str(market.tokens[0].get("o") or ""),
                },
                {
                    "role": "complement",
                    "token_id": str(market.tokens[1].get("t") or ""),
                    "outcome": str(market.tokens[1].get("o") or ""),
                },
            ],
            "reward_reference_price_proxy": {
                "value": str(midpoint),
                "method": "proxy_blended_l1_midpoint",
                "platform_midpoint_verified": False,
                "size_cutoff_method": None,
                "reason": (
                    "The platform size-cutoff-adjusted reward midpoint is not "
                    "fully published or observed by this public snapshot."
                ),
            },
            "tick_size": str(market.tick_size),
            "minimum_order_size": str(market.min_order_size),
            "daily_reward_pool": str(daily_reward),
        },
        "candidate": {
            "type": "hypothetical_balanced_outcome_token_bids",
            "yes_bid": str(yes_bid),
            "no_bid": str(no_bid),
            "size_each": str(quote_size),
            "distance_fraction_of_maximum": str(
                config.quote_distance_fraction
            ),
            "minimum_age_assumption_seconds": str(assumed_age),
            "not_submitted": True,
        },
        "theoretical_ledger": {
            "ledger": "theoretical",
            "recognized_pnl": "0",
            "strict_public_snapshot": strict_book_dict,
            "aggregate_public_book_if_all_levels_aged": competition_dict,
            "candidate_score_if_age_requirement_is_met": own_dict,
            "conditional_pool_allocation_if_snapshot_share_persisted_full_epoch_at_3x": str(
                theoretical_daily_reward
            ),
            "expected_reward": {
                "value": None,
                "reason": (
                    "One public snapshot cannot reconstruct randomized minute "
                    "samples, per-sample maker normalization, epoch persistence, "
                    "or confirmed payout."
                ),
            },
            "epoch_model": {
                "verified": False,
                "sampling_cadence": "minute_randomized_documented",
                "epoch_duration": None,
                "reason": (
                    "The public documentation does not establish a replayable "
                    "current payout epoch from this snapshot."
                ),
            },
            "warning": (
                "The public book has no maker identity or order age. The "
                "candidate share is a static scenario, not a payout forecast."
            ),
        },
        "scoring_ledger": scoring,
        "payout_ledger": payout,
        "competition_proxy": {
            "evidence_kind": "conditional",
            "maker_identity_available": False,
            "aggregate_book_score_is_upper_bound": False,
            "competition_multiplier_assumption": str(
                DEFAULT_COMPETITION_MULTIPLIER
            ),
            "public_score_lower_bound": str(strict_public.score_lower),
            "public_age_verified_score": str(strict_public.score_upper),
            "aggregate_public_book_score_if_all_levels_aged": str(
                competition_score
            ),
            "candidate_score_if_aged": str(own_score),
            "conditional_share_grid": stress_grid,
            "reason": (
                "Aggregate L2 lacks maker identity; scoring the aggregate as "
                "one maker is a proxy, not a mathematical upper bound."
            ),
        },
        "conditional_reward_sensitivity": conditional_sensitivity,
        "shadow_fill_scenario": {
            "evidence_kind": "shadow",
            "public_fill_probability_lower_bound": "0",
            "estimated_fill_probability": None,
            "assumed_paired_fills_per_day": (
                config.paired_fills_per_day
            ),
            "assumed_adverse_one_leg_fills_per_day": (
                config.adverse_one_leg_fills_per_day
            ),
            "conditional_paired_fill_cashflow_before_operation_cost": str(
                paired_fill_pnl
            ),
            "conditional_non_reward_cashflow_before_operation_cost": str(
                non_reward_profit
            ),
            "warning": (
                "Counts are explicit stress inputs, not inferred fill rates. "
                "Split/merge transaction cost is unknown and excluded."
            ),
        },
        "instantaneous_zero_latency_shadow_one_leg": {
            "evidence_kind": "shadow",
            "hedge_execution_latency_ms": 0,
            "cancel_latency_ms": None,
            "cancel_latency_reason": (
                "No order was submitted and no cancel acknowledgement exists."
            ),
            "operation_cost": None,
            "operation_cost_reason": (
                "Split, merge, and transaction costs were not observed."
            ),
            "yes_fills_first": yes_fills_first,
            "no_fills_first": no_fills_first,
            "instantaneous_zero_latency_shadow_worst_one_leg_cashflow_before_operation_cost": str(
                worst_adverse_pnl
            ),
            "paired_fills_needed_per_adverse_fill": (
                str(max(ZERO, -worst_adverse_pnl) / paired_fill_pnl)
                if paired_fill_pnl > ZERO
                else None
            ),
        },
        "fee_pressure": {
            "fee_schedule": {
                "rate": str(fee_schedule.rate),
                "exponent": str(fee_schedule.exponent),
                "taker_only": fee_schedule.taker_only,
            },
            "rounding": {
                "quantum": "0.00001",
                "rule": "ROUND_HALF_UP_assumption",
                "platform_halfway_boundary_verified": False,
            },
            "paired_maker_fee": str(maker_yes_fee + maker_no_fee),
            "worst_one_leg_total_fee": str(worst_adverse_fee),
            "conditional_shadow_non_reward_cashflow_if_fees_double": str(
                non_reward_profit
                - worst_adverse_fee
                * config.adverse_one_leg_fills_per_day
                - (maker_yes_fee + maker_no_fee)
                * config.paired_fills_per_day
            ),
            "one_fee_quantum_stress": {
                "evidence_kind": "conditional",
                "quantum": str(FEE_QUANTUM),
                "scope": "every_modeled_fee_incurrence_in_shadow_day",
                "shadow_one_leg_maximum_fee_incurrences": (
                    maximum_fee_incurrences_per_shadow_one_leg
                ),
                "shadow_paired_fill_fee_incurrences": (
                    fee_incurrences_per_shadow_paired_fill
                ),
                "conditional_shadow_non_reward_cashflow": str(
                    non_reward_profit
                    - FEE_QUANTUM
                    * (
                        maximum_fee_incurrences_per_shadow_one_leg
                        * config.adverse_one_leg_fills_per_day
                        + fee_incurrences_per_shadow_paired_fill
                        * config.paired_fills_per_day
                    )
                ),
            },
        },
        "conditional_haircuts": [
            {
                "evidence_kind": "conditional",
                "reward_multiplier": str(row.multiplier),
                "conditional_non_reward_cashflow": str(
                    row.non_reward_profit
                ),
                "conditional_theoretical_reward": str(
                    row.theoretical_reward
                ),
                "recognized_confirmed_reward": str(row.confirmed_reward),
                "conditional_total_cashflow": str(row.net_profit),
            }
            for row in haircut_rows
        ],
        "shadow_reward_sensitivity": {
            "evidence_kind": "shadow",
            "scenario_label": shadow_stress_label,
            "conditional_robustness_label": stress.classification,
            "reward_zero_conditional_cashflow_positive": (
                stress.reward_zero_positive
            ),
            "conditional_cashflow_by_reward_multiplier": {
                str(multiplier): str(value)
                for multiplier, value in stress.net_profit_by_multiplier
            },
        },
        "validation_ceiling": ceiling,
    }


def _sort_daily_reward(row: Mapping[str, Any]) -> Decimal:
    try:
        value = _finite_decimal(
            row.get("total_daily_rate"),
            name="total_daily_rate",
        )
    except ValueError:
        return ZERO
    return value


def _preselection_reward_risk_score(row: Mapping[str, Any]) -> Decimal:
    """Rank the public universe by a configuration-only reward/risk proxy.

    A larger incentive spread permits a quote farther from midpoint, while a
    larger minimum size increases capital and fill exposure.  This score is
    only a bounded prefilter: the final ranking uses observed competition and
    one-leg hedge stress after fetching both books.  It is not an expected
    reward or profitability estimate.
    """

    try:
        daily_reward = _finite_decimal(
            row.get("total_daily_rate"),
            name="total_daily_rate",
        )
        maximum_spread = _finite_decimal(
            row.get("rewards_max_spread"),
            name="rewards_max_spread",
        )
        minimum_size = _finite_decimal(
            row.get("rewards_min_size"),
            name="rewards_min_size",
        )
    except ValueError:
        return ZERO
    if (
        daily_reward <= ZERO
        or maximum_spread <= ZERO
        or minimum_size <= ZERO
    ):
        return ZERO
    return daily_reward * maximum_spread / minimum_size


def _rank_evaluated_market(
    market: Mapping[str, Any],
    *,
    preselection_rank: int,
    preselection_score: Decimal,
) -> dict[str, Any]:
    """Attach a transparent conditional-reward / shadow-risk proxy record."""

    ranked = dict(market)
    ranking: dict[str, Any] = {
        "preselection_rank": preselection_rank,
        "preselection_reward_risk_proxy": str(preselection_score),
        "public_fill_probability_lower_bound": "0",
        "recognized_reward_pnl": "0",
        "validated_reward_to_risk_score": None,
        "validated_score_reason": (
            "No authenticated maker fill, order scoring, or payout evidence."
        ),
        "conditional_reward_to_capital_risk_proxy": None,
        "conditional_score_reason": None,
    }
    try:
        theoretical = _finite_decimal(
            market["theoretical_ledger"][
                "conditional_pool_allocation_if_snapshot_share_persisted_full_epoch_at_3x"
            ],
            name="conditional pool allocation",
        )
        candidate = market["candidate"]
        size = _finite_decimal(candidate["size_each"], name="candidate size")
        yes_bid = _finite_decimal(candidate["yes_bid"], name="yes bid")
        no_bid = _finite_decimal(candidate["no_bid"], name="no bid")
        worst_one_leg = _finite_decimal(
            market["instantaneous_zero_latency_shadow_one_leg"][
                "instantaneous_zero_latency_shadow_worst_one_leg_cashflow_before_operation_cost"
            ],
            name="worst one-leg PnL",
        )
        static_share = _finite_decimal(
            market["competition_proxy"]["conditional_share_grid"][
                str(DEFAULT_COMPETITION_MULTIPLIER)
            ]["conditional_candidate_pool_share"],
            name="candidate static share",
        )
        quote_capital = size * (yes_bid + no_bid)
        one_leg_downside = max(ZERO, -worst_one_leg)
        capital_risk = quote_capital + one_leg_downside
        if capital_risk <= ZERO:
            raise ValueError("capital risk must be positive")
        ranking.update(
            {
                "conditional_candidate_reward_share_at_3x": str(
                    static_share
                ),
                "conditional_pool_allocation_at_3x": str(theoretical),
                "quote_capital": str(quote_capital),
                "worst_one_leg_downside_before_operation_cost": str(
                    one_leg_downside
                ),
                "capital_risk_denominator": str(capital_risk),
                "conditional_reward_to_capital_risk_proxy": str(
                    theoretical / capital_risk
                ),
            }
        )
    except (KeyError, TypeError, ValueError):
        ranking["conditional_score_reason"] = (
            "Required synchronized books, complete fee schedule, or one-leg "
            "hedge evidence was unavailable."
        )
    else:
        ranking["conditional_score_reason"] = (
            "Static public-book scenario only; fill probability lower bound "
            "is zero and operation cost is not known."
        )
    ranked["ranking"] = ranking
    return ranked


def _attach_rank_stability(markets: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach per-market rank ranges across the declared sensitivity grid."""

    scenario_maps: dict[str, dict[str, Decimal]] = {}
    by_condition: dict[str, dict[str, Any]] = {}
    for market in markets:
        sensitivity = market.get("conditional_reward_sensitivity")
        if not isinstance(sensitivity, Mapping):
            continue
        condition_id = str(market.get("condition_id") or "")
        by_condition[condition_id] = market
        for row in sensitivity.get("scenarios", ()):
            if not isinstance(row, Mapping):
                continue
            scenario_id = str(row.get("scenario_id") or "")
            if not scenario_id:
                continue
            scenario_maps.setdefault(scenario_id, {})[condition_id] = (
                _finite_decimal(
                    row.get(
                        "conditional_reward_to_baseline_capital_risk_proxy"
                    ),
                    name="sensitivity ranking proxy",
                )
            )
    ranks_by_condition: dict[str, list[int]] = {
        condition_id: [] for condition_id in by_condition
    }
    for scenario_id in sorted(scenario_maps):
        scores = scenario_maps[scenario_id]
        ordered = sorted(
            by_condition,
            key=lambda condition_id: (
                -scores.get(condition_id, ZERO),
                int(
                    by_condition[condition_id]["ranking"][
                        "preselection_rank"
                    ]
                ),
                condition_id,
            ),
        )
        for rank, condition_id in enumerate(ordered, start=1):
            ranks_by_condition[condition_id].append(rank)
    for condition_id, rank_values in ranks_by_condition.items():
        ranking = by_condition[condition_id]["ranking"]
        ranking["sensitivity_rank_min"] = min(rank_values)
        ranking["sensitivity_rank_max"] = max(rank_values)
        ranking["sensitivity_rank_span"] = (
            max(rank_values) - min(rank_values)
        )
    spans = [
        max(values) - min(values)
        for values in ranks_by_condition.values()
        if values
    ]
    return {
        "scenario_count": len(scenario_maps),
        "market_count": len(by_condition),
        "maximum_rank_span": max(spans) if spans else 0,
        "warning": (
            "Ranks are conditional public-snapshot diagnostics, not expected "
            "reward, fill probability, payout, or realized PnL."
        ),
    }


def run_reward_experiment(
    sources: RewardExperimentSources,
    *,
    config: RewardExperimentConfig | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Run one bounded public snapshot experiment without side effects."""

    settings = config or RewardExperimentConfig()
    started_at = clock()
    pages: list[Fetched[RewardPage]] = []
    source_errors: list[str] = []
    try:
        pages = list(
            sources.current_reward_pages(
                limit=500,
                max_pages=settings.max_reward_pages,
            )
        )
    except (PublicSourceError, ValueError, RuntimeError) as exc:
        source_errors.append(
            f"current_rewards_source_error:{type(exc).__name__}"
        )

    row_by_condition: dict[str, Mapping[str, Any]] = {}
    row_page_evidence: dict[str, Mapping[str, Any]] = {}
    duplicate_conditions: list[str] = []
    for fetched in pages:
        evidence = _source_evidence(fetched.raw)
        for row in fetched.value.items:
            condition_id = str(row.get("condition_id") or "")
            if condition_id in row_by_condition:
                duplicate_conditions.append(condition_id)
                continue
            row_by_condition[condition_id] = row
            row_page_evidence[condition_id] = evidence
    if duplicate_conditions:
        source_errors.append("duplicate_current_reward_conditions")

    all_unique_rows = tuple(row_by_condition.values())
    scoped_rows = tuple(
        row
        for row in all_unique_rows
        if _reward_asset_ids(row) == (settings.reward_asset_id,)
    )
    ordered_rows = sorted(
        scoped_rows,
        key=lambda row: (
            -_preselection_reward_risk_score(row),
            -_sort_daily_reward(row),
            str(row.get("condition_id") or ""),
        ),
    )
    selected_rows = ordered_rows[: settings.max_markets]
    markets = []
    for preselection_rank, row in enumerate(selected_rows, start=1):
        evaluated = _evaluate_market(
            sources,
            row,
            current_row_evidence=row_page_evidence.get(
                str(row.get("condition_id") or ""),
                {},
            ),
            config=settings,
            clock=clock,
        )
        markets.append(
            _rank_evaluated_market(
                evaluated,
                preselection_rank=preselection_rank,
                preselection_score=_preselection_reward_risk_score(row),
            )
        )
    markets.sort(
        key=lambda market: (
            -_finite_decimal(
                market["ranking"][
                    "conditional_reward_to_capital_risk_proxy"
                ]
                or ZERO,
                name="ranking score",
            ),
            int(market["ranking"]["preselection_rank"]),
            str(market.get("condition_id") or ""),
        )
    )
    for final_rank, market in enumerate(markets, start=1):
        market["ranking"]["final_rank"] = final_rank
    rank_stability = _attach_rank_stability(markets)
    terminal_cursors = {None, "", "LTE="}
    pagination_complete = bool(pages) and (
        pages[-1].value.next_cursor in terminal_cursors
    )
    counts: dict[str, int] = {}
    for market in markets:
        label = str(market["classification"])
        counts[label] = counts.get(label, 0) + 1
    completed_at = clock()
    return {
        "schema_version": "edge-lab-liquidity-reward-experiment-v2",
        "experiment_id": (
            "selective-liquidity-rewards-"
            + datetime.fromtimestamp(
                started_at,
                tz=timezone.utc,
            ).strftime("%Y%m%dT%H%M%S.%fZ")
        ),
        "started_at": _iso_timestamp(started_at),
        "completed_at": _iso_timestamp(completed_at),
        "config": settings.to_dict(),
        **_zero_recognized_economics(),
        "safety": {
            "read_only_public_endpoints_only": True,
            "credentials_read": False,
            "orders_submitted": False,
            "transactions_signed": False,
            "live_order_guard_required": True,
        },
        "universe": {
            "reward_pages_fetched": len(pages),
            "pagination_complete": pagination_complete,
            "unique_current_reward_markets": len(all_unique_rows),
            "all_unique_current_reward_markets": len(all_unique_rows),
            "asset_scoped_reward_markets": len(ordered_rows),
            "excluded_by_reward_asset_scope": (
                len(all_unique_rows) - len(ordered_rows)
            ),
            "reward_asset_scope": settings.reward_asset_id,
            "selected_markets": len(selected_rows),
            "not_evaluated_due_to_deterministic_cap": max(
                0,
                len(ordered_rows) - len(selected_rows),
            ),
            "selection_rule": (
                "configuration prefilter: descending "
                "(total_daily_rate * rewards_max_spread / "
                "rewards_min_size), then daily rate and condition_id; final "
                "ranking: descending conditional pool allocation at 3x "
                "aggregate-book competition proxy "
                "competition divided by quote capital plus worst one-leg "
                "downside"
            ),
            "source_errors": source_errors,
            "duplicate_condition_ids": sorted(set(duplicate_conditions)),
            "page_evidence": [
                _source_evidence(page.raw) for page in pages
            ],
        },
        "classification_counts": counts,
        "markets": markets,
        "rank_stability": rank_stability,
        "classification_basis": (
            "public_snapshot_discovery_with_zero_recognized_pnl"
        ),
        "global_conclusion": (
            "No public-only snapshot can validate reward profitability. "
            "Without actual order scoring and confirmed payout evidence, the "
            "highest permitted per-market classification is "
            "promising_not_validated. Conditional and shadow cashflows are "
            "not backtest PnL."
        ),
    }


__all__ = [
    "DEFAULT_REWARD_ASSET_ID",
    "RewardExperimentConfig",
    "RewardExperimentSources",
    "run_reward_experiment",
]
