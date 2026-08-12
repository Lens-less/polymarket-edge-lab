"""Historical public-book replay for paired complete-set quote economics."""

from __future__ import annotations

import statistics
import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from .economics import (
    build_paired_ask_quote,
    build_paired_bid_quote,
    evaluate_paired_ask_quote,
    evaluate_paired_bid_quote,
)
from .models import MarketCandidate, OrderBook, decimal_value
from .public_api import PublicPolymarketAPI
from .scanner import EdgeScanner


@dataclass(frozen=True)
class ReplayConfig:
    hours: Decimal = Decimal("24")
    quote_size: Optional[Decimal] = None
    distance_fraction: Decimal = Decimal("0.5")
    stress_ticks: int = 2
    competition_multiplier: Decimal = Decimal("3")
    reward_multiplier: Decimal = Decimal("1")
    alignment_tolerance_ms: int = 15_000
    max_records_per_token: int = 10_000


def align_books(
    yes_books: list[OrderBook],
    no_books: list[OrderBook],
    *,
    tolerance_ms: int,
) -> list[tuple[OrderBook, OrderBook]]:
    """Nearest-neighbor alignment without reusing a complement snapshot."""
    pairs: list[tuple[OrderBook, OrderBook]] = []
    j = 0
    for yes_book in yes_books:
        if yes_book.timestamp_ms is None:
            continue
        while (
            j + 1 < len(no_books)
            and no_books[j + 1].timestamp_ms is not None
            and abs(no_books[j + 1].timestamp_ms - yes_book.timestamp_ms)
            <= abs((no_books[j].timestamp_ms or 0) - yes_book.timestamp_ms)
        ):
            j += 1
        if j >= len(no_books):
            break
        no_book = no_books[j]
        if no_book.timestamp_ms is None:
            continue
        if abs(no_book.timestamp_ms - yes_book.timestamp_ms) <= tolerance_ms:
            pairs.append((yes_book, no_book))
            j += 1
            if j >= len(no_books):
                break
    return pairs


def _numeric_summary(values: Iterable[Optional[Decimal]]) -> dict[str, Any]:
    usable = sorted(value for value in values if value is not None)
    if not usable:
        return {"count": 0, "min": None, "median": None, "p90": None, "max": None}
    p90_index = min(len(usable) - 1, int((len(usable) - 1) * 0.9))
    return {
        "count": len(usable),
        "min": usable[0],
        "median": Decimal(str(statistics.median(usable))),
        "p90": usable[p90_index],
        "max": usable[-1],
    }


def _integrated_reward_scenario(
    samples: list[dict[str, Any]],
    *,
    max_interval_ms: int = 60_000,
) -> dict[str, Any]:
    if len(samples) < 2:
        return {
            "covered_hours": Decimal("0"),
            "static_competition_reward": Decimal("0"),
        }
    covered_ms = 0
    reward = Decimal("0")
    for current, following in zip(samples, samples[1:]):
        interval_ms = max(
            0,
            min(
                max_interval_ms,
                int(following["timestamp_ms"]) - int(current["timestamp_ms"]),
            ),
        )
        covered_ms += interval_ms
        reward += (
            decimal_value(current["daily_reward_if_static"])
            * Decimal(interval_ms)
            / Decimal("86400000")
        )
    return {
        "covered_hours": Decimal(covered_ms) / Decimal("3600000"),
        "static_competition_reward": reward,
        "zero_reward_stress": Decimal("0"),
        "max_interval_seconds": Decimal(max_interval_ms) / Decimal("1000"),
        "warning": (
            "Uses the current reward rate across historical snapshots and an "
            "aggregate L2 competition proxy. It is not a payout forecast."
        ),
    }


def _trade_flow(
    trades: list[dict[str, Any]],
    *,
    candidate: MarketCandidate,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    start_s = start_ms // 1000
    end_s = end_ms // 1000
    selected = [
        trade
        for trade in trades
        if start_s <= int(trade.get("timestamp") or 0) <= end_s
    ]
    buckets: dict[str, dict[str, Decimal | int]] = {}
    for token_id, label in (
        (candidate.yes_token_id, "first_outcome"),
        (candidate.no_token_id, "second_outcome"),
    ):
        for side in ("BUY", "SELL"):
            buckets[f"{label}_{side.lower()}"] = {
                "count": 0,
                "shares": Decimal("0"),
                "notional": Decimal("0"),
            }

    for trade in selected:
        token_id = str(trade.get("asset") or "")
        if token_id == candidate.yes_token_id:
            label = "first_outcome"
        elif token_id == candidate.no_token_id:
            label = "second_outcome"
        else:
            continue
        side = str(trade.get("side") or "").upper()
        key = f"{label}_{side.lower()}"
        if key not in buckets:
            continue
        size = decimal_value(trade.get("size"))
        price = decimal_value(trade.get("price"))
        buckets[key]["count"] = int(buckets[key]["count"]) + 1
        buckets[key]["shares"] = Decimal(buckets[key]["shares"]) + size
        buckets[key]["notional"] = Decimal(buckets[key]["notional"]) + size * price

    first_sells = Decimal(buckets["first_outcome_sell"]["shares"])
    second_sells = Decimal(buckets["second_outcome_sell"]["shares"])
    return {
        "records": len(selected),
        "by_outcome_and_side": buckets,
        "paired_bid_hit_flow_proxy": {
            "first_outcome_sell_shares": first_sells,
            "second_outcome_sell_shares": second_sells,
            "absolute_imbalance": abs(first_sells - second_sells),
            "warning": (
                "Public trade direction is only a flow proxy. It does not reveal "
                "our queue position and is not counted as a fill."
            ),
        },
    }


def _matched_volume_within(
    first: list[tuple[int, Decimal]],
    second: list[tuple[int, Decimal]],
    *,
    horizon_ms: int,
) -> Decimal:
    left = [[timestamp, size] for timestamp, size in first]
    right = [[timestamp, size] for timestamp, size in second]
    i = 0
    j = 0
    matched = Decimal("0")
    while i < len(left) and j < len(right):
        left_time, left_size = left[i]
        right_time, right_size = right[j]
        if abs(left_time - right_time) <= horizon_ms:
            quantity = min(left_size, right_size)
            matched += quantity
            left[i][1] -= quantity
            right[j][1] -= quantity
            if left[i][1] <= 0:
                i += 1
            if right[j][1] <= 0:
                j += 1
        elif left_time < right_time - horizon_ms:
            i += 1
        else:
            j += 1
    return matched


def _shadow_fill_proxy(
    trades: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    *,
    candidate: MarketCandidate,
) -> dict[str, Any]:
    """Optimistic public-flow touches against the hypothetical quote path."""
    if not samples:
        return {"events": 0, "modes": {}, "warning": "no aligned samples"}
    timestamps = [int(sample["timestamp_ms"]) for sample in samples]
    modes: dict[str, dict[str, list[tuple[int, Decimal]]]] = {
        "paired_bids": {"first": [], "second": []},
        "paired_asks": {"first": [], "second": []},
    }
    events = 0
    for trade in trades:
        timestamp_ms = int(trade.get("timestamp") or 0) * 1000
        index = bisect_right(timestamps, timestamp_ms) - 1
        if index < 0:
            continue
        sample = samples[index]
        token_id = str(trade.get("asset") or "")
        if token_id == candidate.yes_token_id:
            leg = "first"
            bid = sample["yes_bid"]
            ask = sample["yes_ask"]
        elif token_id == candidate.no_token_id:
            leg = "second"
            bid = sample["no_bid"]
            ask = sample["no_ask"]
        else:
            continue
        side = str(trade.get("side") or "").upper()
        price = decimal_value(trade.get("price"))
        size = decimal_value(trade.get("size"))
        size = min(size, decimal_value(sample.get("quote_size")))
        if side == "SELL" and price <= bid:
            modes["paired_bids"][leg].append((timestamp_ms, size))
            events += 1
        if side == "BUY" and price >= ask:
            modes["paired_asks"][leg].append((timestamp_ms, size))
            events += 1

    summarized: dict[str, Any] = {}
    for mode, legs in modes.items():
        first_volume = sum((size for _, size in legs["first"]), Decimal("0"))
        second_volume = sum((size for _, size in legs["second"]), Decimal("0"))
        matched = {
            f"{seconds}s": _matched_volume_within(
                legs["first"],
                legs["second"],
                horizon_ms=seconds * 1000,
            )
            for seconds in (60, 300, 1800)
        }
        if mode == "paired_bids":
            margin_key = "paired_bid_margin"
            loss_key = "paired_bid_worst_one_leg_loss"
        else:
            margin_key = "paired_ask_margin"
            loss_key = "paired_ask_worst_one_leg_loss"
        margin_per_share = sorted(
            decimal_value(sample[margin_key]) / decimal_value(sample["quote_size"])
            for sample in samples
        )
        loss_per_share = sorted(
            decimal_value(sample[loss_key]) / decimal_value(sample["quote_size"])
            for sample in samples
            if sample.get(loss_key) is not None
        )
        median_margin = Decimal(str(statistics.median(margin_per_share)))
        p90_loss = (
            loss_per_share[
                min(len(loss_per_share) - 1, int((len(loss_per_share) - 1) * 0.9))
            ]
            if loss_per_share
            else None
        )
        pnl_scenarios: dict[str, Any] = {}
        for horizon, paired_volume in matched.items():
            unmatched_volume = (
                first_volume + second_volume - Decimal("2") * paired_volume
            )
            pnl = (
                paired_volume * median_margin - unmatched_volume * p90_loss
                if p90_loss is not None
                else None
            )
            pnl_scenarios[horizon] = {
                "paired_shares": paired_volume,
                "unmatched_one_leg_shares": unmatched_volume,
                "median_pair_margin_per_share": median_margin,
                "p90_single_leg_loss_per_share": p90_loss,
                "spread_only_pnl_proxy": pnl,
                "reward_needed_to_break_even": (
                    max(Decimal("0"), -pnl) if pnl is not None else None
                ),
            }
        summarized[mode] = {
            "first_leg_events": len(legs["first"]),
            "second_leg_events": len(legs["second"]),
            "first_leg_shares": first_volume,
            "second_leg_shares": second_volume,
            "absolute_volume_imbalance": abs(first_volume - second_volume),
            "optimistically_pairable_shares_by_horizon": matched,
            "stress_pnl_proxy_by_horizon": pnl_scenarios,
        }
    return {
        "events": events,
        "modes": summarized,
        "warning": (
            "Upper-bound flow proxy only: public trades do not prove taker "
            "direction, queue priority, or that our order would fill. The "
            "conservative lower bound remains zero fills."
        ),
    }


class HistoricalReplay:
    def __init__(self, api: Optional[PublicPolymarketAPI] = None) -> None:
        self.api = api or PublicPolymarketAPI()
        self.scanner = EdgeScanner(self.api)

    def run(self, condition_id: str, config: ReplayConfig) -> dict[str, Any]:
        current = self.scanner.evaluate_condition(
            condition_id,
            quote_size=config.quote_size,
            distance_fraction=config.distance_fraction,
            stress_ticks=config.stress_ticks,
            competition_multiplier=config.competition_multiplier,
            reward_multiplier=config.reward_multiplier,
        )
        candidate: MarketCandidate = current["candidate"]

        end_ms = int(time.time() * 1000)
        start_ms = end_ms - int(config.hours * Decimal("3600000"))
        yes_history = self.api.orderbook_history(
            candidate.yes_token_id,
            start_ms=start_ms,
            end_ms=end_ms,
            max_records=config.max_records_per_token,
        )
        no_history = self.api.orderbook_history(
            candidate.no_token_id,
            start_ms=start_ms,
            end_ms=end_ms,
            max_records=config.max_records_per_token,
        )
        pairs = align_books(
            yes_history,
            no_history,
            tolerance_ms=config.alignment_tolerance_ms,
        )

        size = config.quote_size or max(
            candidate.reward_min_size,
            candidate.min_order_size,
        )
        samples: list[dict[str, Any]] = []
        skipped = 0
        previous_quote: Optional[
            tuple[Decimal, Decimal, Decimal, Decimal]
        ] = None
        quote_changes = 0
        for yes_book, no_book in pairs:
            try:
                quote = build_paired_bid_quote(
                    yes_book,
                    no_book,
                    max_spread_cents=candidate.reward_max_spread_cents,
                    size=size,
                    distance_fraction=config.distance_fraction,
                )
                economics = evaluate_paired_bid_quote(
                    quote,
                    yes_book,
                    no_book,
                    daily_reward=candidate.daily_reward * config.reward_multiplier,
                    reward_max_spread_cents=candidate.reward_max_spread_cents,
                    reward_min_size=candidate.reward_min_size,
                    fee_schedule=candidate.fee_schedule,
                    stress_ticks=config.stress_ticks,
                    competition_multiplier=config.competition_multiplier,
                )
                ask_quote = build_paired_ask_quote(
                    yes_book,
                    no_book,
                    max_spread_cents=candidate.reward_max_spread_cents,
                    size=size,
                    distance_fraction=config.distance_fraction,
                )
                ask_economics = evaluate_paired_ask_quote(
                    ask_quote,
                    yes_book,
                    no_book,
                    daily_reward=candidate.daily_reward * config.reward_multiplier,
                    reward_max_spread_cents=candidate.reward_max_spread_cents,
                    reward_min_size=candidate.reward_min_size,
                    fee_schedule=candidate.fee_schedule,
                    stress_ticks=config.stress_ticks,
                    competition_multiplier=config.competition_multiplier,
                )
            except ValueError:
                skipped += 1
                continue

            quote_key = (
                quote.yes_bid,
                quote.no_bid,
                ask_quote.yes_ask,
                ask_quote.no_ask,
            )
            if previous_quote is not None and quote_key != previous_quote:
                quote_changes += 1
            previous_quote = quote_key
            hedge = economics["one_leg_hedges"]
            ask_hedge = ask_economics["one_leg_hedges"]
            reward = economics["liquidity_reward_scenario"]
            samples.append(
                {
                    "timestamp_ms": max(
                        yes_book.timestamp_ms or 0,
                        no_book.timestamp_ms or 0,
                    ),
                    "quote_size": size,
                    "yes_bid": quote.yes_bid,
                    "no_bid": quote.no_bid,
                    "yes_ask": ask_quote.yes_ask,
                    "no_ask": ask_quote.no_ask,
                    "reference_yes_mid": quote.reference_yes_mid,
                    "paired_bid_margin": quote.paired_margin,
                    "paired_ask_margin": ask_quote.paired_margin,
                    "paired_bid_worst_one_leg_loss": hedge["worst_loss"],
                    "paired_ask_worst_one_leg_loss": ask_hedge["worst_loss"],
                    "paired_bid_both_hedges_executable": (
                        hedge["yes_fills_first"]["complete"]
                        and hedge["no_fills_first"]["complete"]
                    ),
                    "paired_ask_both_hedges_executable": (
                        ask_hedge["yes_sells_first"]["complete"]
                        and ask_hedge["no_sells_first"]["complete"]
                    ),
                    "visible_competition_proxy": reward[
                        "visible_competition_proxy"
                    ]["q_min_proxy"],
                    "daily_reward_if_static": reward["daily_reward_if_static"],
                }
            )

        trades = self.api.trades(condition_id, limit=1000)
        executable_count = sum(
            1
            for sample in samples
            if sample["paired_bid_both_hedges_executable"]
        )
        ask_executable_count = sum(
            1
            for sample in samples
            if sample["paired_ask_both_hedges_executable"]
        )
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "condition_id": condition_id,
            "question": candidate.question,
            "candidate": candidate,
            "assumptions": {
                "reward_multiplier": config.reward_multiplier,
                "distance_fraction": config.distance_fraction,
                "stress_ticks": config.stress_ticks,
                "competition_multiplier": config.competition_multiplier,
            },
            "window": {
                "requested_start_ms": start_ms,
                "requested_end_ms": end_ms,
                "requested_hours": config.hours,
                "first_outcome_history_records": len(yes_history),
                "second_outcome_history_records": len(no_history),
                "aligned_records": len(pairs),
                "evaluated_records": len(samples),
                "skipped_records": skipped,
            },
            "quote_stability": {
                "quote_changes": quote_changes,
                "change_rate_per_transition": (
                    Decimal(quote_changes) / Decimal(len(samples) - 1)
                    if len(samples) > 1
                    else None
                ),
            },
            "economics_distribution": {
                "paired_bid_margin": _numeric_summary(
                    sample["paired_bid_margin"] for sample in samples
                ),
                "paired_ask_margin": _numeric_summary(
                    sample["paired_ask_margin"] for sample in samples
                ),
                "paired_bid_worst_one_leg_loss": _numeric_summary(
                    sample["paired_bid_worst_one_leg_loss"]
                    for sample in samples
                ),
                "paired_ask_worst_one_leg_loss": _numeric_summary(
                    sample["paired_ask_worst_one_leg_loss"]
                    for sample in samples
                ),
                "visible_competition_proxy": _numeric_summary(
                    sample["visible_competition_proxy"] for sample in samples
                ),
                "daily_reward_if_static": _numeric_summary(
                    sample["daily_reward_if_static"] for sample in samples
                ),
                "paired_bid_both_hedges_executable_rate": (
                    Decimal(executable_count) / Decimal(len(samples))
                    if samples
                    else None
                ),
                "paired_ask_both_hedges_executable_rate": (
                    Decimal(ask_executable_count) / Decimal(len(samples))
                    if samples
                    else None
                ),
            },
            "integrated_reward_scenario": _integrated_reward_scenario(samples),
            "trade_flow": _trade_flow(
                trades,
                candidate=candidate,
                start_ms=start_ms,
                end_ms=end_ms,
            ),
            "shadow_fill_proxy": _shadow_fill_proxy(
                trades,
                samples,
                candidate=candidate,
            ),
            "samples": samples,
            "current_snapshot": current,
            "verdict": {
                "status": "research_only",
                "why": [
                    "L2 snapshots cannot identify maker addresses or queue position.",
                    "No passive fill is inferred from a book touch.",
                    "Static reward share is a scenario, not realized payout.",
                    "One-leg adverse selection remains the dominant loss path.",
                ],
            },
            "data_warning": (
                "/orderbook-history currently returns data but is absent from "
                "the public API/SDK contract. Keep an independent WebSocket "
                "capture before any capital decision."
            ),
        }
        return report
