"""Reward-market scanner and condition-level economic evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from .economics import (
    build_paired_ask_quote,
    build_paired_bid_quote,
    evaluate_paired_ask_quote,
    evaluate_paired_bid_quote,
    evaluate_taker_complete_sets,
    json_safe,
)
from .models import FeeSchedule, MarketCandidate, OrderBook, decimal_value
from .network_safety import safe_error_details
from .public_api import PublicPolymarketAPI, reward_daily_rate


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _token_pair(raw: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    tokens = raw.get("tokens") or []
    if len(tokens) != 2:
        return None
    first = str(tokens[0].get("token_id") or "")
    second = str(tokens[1].get("token_id") or "")
    if not first or not second:
        return None
    return first, second


def _iso_is_future(value: Optional[str]) -> bool:
    if not value:
        return True
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed > datetime.now(timezone.utc)
    except ValueError:
        return True


def _reward_order_age(
    reward_row: Mapping[str, Any],
    clob: Mapping[str, Any],
) -> tuple[Optional[int], Optional[str]]:
    if clob.get("oas") is not None:
        return int(clob["oas"]), "clob.oas"
    for source_name, source in (
        ("clob.r.moas", clob.get("r")),
        ("reward.r.moas", reward_row.get("r")),
    ):
        if isinstance(source, Mapping) and source.get("moas") is not None:
            return int(source["moas"]), source_name
    return None, None


@dataclass(frozen=True)
class ScanConfig:
    pages: int = 4
    page_size: int = 500
    max_evaluations: int = 80
    min_daily_reward: Decimal = Decimal("1")
    min_volume_24h: Decimal = Decimal("1")
    quote_size: Decimal = Decimal("20")
    distance_fraction: Decimal = Decimal("0.5")
    stress_ticks: int = 2
    competition_multiplier: Decimal = Decimal("3")
    reward_multiplier: Decimal = Decimal("1")


class EdgeScanner:
    def __init__(self, api: Optional[PublicPolymarketAPI] = None) -> None:
        self.api = api or PublicPolymarketAPI()

    @staticmethod
    def _preflight(candidate: MarketCandidate) -> dict[str, Any]:
        checks = {
            "reward_order_age_known": (
                candidate.minimum_reward_order_age_seconds is not None
            ),
            "reward_rate_sources_consistent": (
                candidate.reward_rate_sources_consistent is True
            ),
            "positive_tick_size": candidate.tick_size > 0,
            "positive_min_order_size": candidate.min_order_size > 0,
            "positive_reward_min_size": candidate.reward_min_size > 0,
            "positive_reward_max_spread": candidate.reward_max_spread_cents > 0,
        }
        return {
            "checks": checks,
            "production_quote_eligible": all(checks.values()),
            "blocking_reasons": [
                label for label, passed in checks.items() if not passed
            ],
        }

    def collect_reward_rows(self, config: ScanConfig) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(config.pages):
            response = self.api.reward_markets(
                page_size=config.page_size,
                next_cursor=cursor,
            )
            rows.extend(response.get("data", []))
            next_cursor = response.get("next_cursor")
            if not next_cursor or next_cursor == cursor or next_cursor == "LTE=":
                break
            cursor = str(next_cursor)
        return rows

    def _candidate(
        self,
        reward_row: Mapping[str, Any],
        clob: Mapping[str, Any],
        gamma: Mapping[str, Any],
    ) -> Optional[MarketCandidate]:
        pair = _token_pair(reward_row)
        if pair is None:
            return None
        active = clob.get("active", gamma.get("active"))
        closed = clob.get("closed", gamma.get("closed"))
        accepting_orders = clob.get(
            "accepting_orders", gamma.get("acceptingOrders")
        )
        if not active or closed or not accepting_orders:
            return None

        end_date = gamma.get("endDate") or clob.get("end_date_iso")
        if not _iso_is_future(end_date):
            return None

        gamma_tokens = _json_list(gamma.get("clobTokenIds"))
        yes_token, no_token = pair
        if len(gamma_tokens) == 2 and set(map(str, gamma_tokens)) == {
            yes_token,
            no_token,
        }:
            # Preserve reward-endpoint ordering, which carries the outcomes.
            pass

        spread = gamma.get("spread")
        reward_age, reward_age_source = _reward_order_age(reward_row, clob)
        gamma_reward_rows = gamma.get("clobRewards") or []
        gamma_reward = sum(
            (
                decimal_value(
                    item.get("rewardsDailyRate") or item.get("rate_per_day")
                )
                for item in gamma_reward_rows
            ),
            Decimal("0"),
        )
        clob_reward = decimal_value(reward_daily_rate(reward_row))
        return MarketCandidate(
            condition_id=str(reward_row.get("condition_id")),
            question=str(reward_row.get("question") or clob.get("question") or ""),
            yes_token_id=yes_token,
            no_token_id=no_token,
            daily_reward=clob_reward,
            reward_min_size=decimal_value(reward_row.get("rewards_min_size")),
            reward_max_spread_cents=decimal_value(
                reward_row.get("rewards_max_spread")
            ),
            reported_competitiveness=decimal_value(
                reward_row.get("market_competitiveness")
            ),
            minimum_reward_order_age_seconds=reward_age,
            reward_order_age_source=reward_age_source,
            gamma_reported_daily_reward=(
                gamma_reward if gamma_reward_rows else None
            ),
            reward_rate_sources_consistent=(
                gamma_reward == clob_reward if gamma_reward_rows else None
            ),
            end_date=end_date,
            seconds_delay=int(
                clob.get("seconds_delay", gamma.get("secondsDelay")) or 0
            ),
            tick_size=decimal_value(
                clob.get(
                    "minimum_tick_size", gamma.get("orderPriceMinTickSize")
                ),
                Decimal("0.01"),
            ),
            min_order_size=decimal_value(
                clob.get("minimum_order_size", gamma.get("orderMinSize")),
                Decimal("5"),
            ),
            neg_risk=bool(clob.get("neg_risk", gamma.get("negRisk", False))),
            tags=tuple(str(tag) for tag in clob.get("tags", [])),
            volume_24h=decimal_value(gamma.get("volume24hr")),
            liquidity=decimal_value(gamma.get("liquidity")),
            displayed_spread=decimal_value(spread) if spread is not None else None,
            fee_schedule=FeeSchedule.from_api(gamma.get("feeSchedule")),
        )

    def evaluate_condition(
        self,
        condition_id: str,
        *,
        quote_size: Optional[Decimal] = None,
        distance_fraction: Decimal = Decimal("0.5"),
        stress_ticks: int = 2,
        competition_multiplier: Decimal = Decimal("3"),
        reward_multiplier: Decimal = Decimal("1"),
    ) -> dict[str, Any]:
        reward_response = self.api.reward_market(condition_id)
        rows = reward_response.get("data", [])
        if not rows:
            raise ValueError(f"no reward-market record for {condition_id}")
        reward_row = rows[0]
        clob = self.api.clob_market(condition_id)
        gamma = self.api.gamma_markets([condition_id]).get(condition_id, {})
        candidate = self._candidate(reward_row, clob, gamma)
        if candidate is None:
            raise ValueError(f"market {condition_id} is not active/evaluable")
        books = self.api.books(
            [candidate.yes_token_id, candidate.no_token_id]
        )
        yes_book = books.get(candidate.yes_token_id)
        no_book = books.get(candidate.no_token_id)
        if yes_book is None or no_book is None:
            raise ValueError(f"missing order book for {condition_id}")

        size = quote_size or max(
            candidate.reward_min_size,
            candidate.min_order_size,
        )
        quote = build_paired_bid_quote(
            yes_book,
            no_book,
            max_spread_cents=candidate.reward_max_spread_cents,
            size=size,
            distance_fraction=distance_fraction,
        )
        economics = evaluate_paired_bid_quote(
            quote,
            yes_book,
            no_book,
            daily_reward=candidate.daily_reward * reward_multiplier,
            reward_max_spread_cents=candidate.reward_max_spread_cents,
            reward_min_size=candidate.reward_min_size,
            fee_schedule=candidate.fee_schedule,
            stress_ticks=stress_ticks,
            competition_multiplier=competition_multiplier,
        )
        ask_quote = build_paired_ask_quote(
            yes_book,
            no_book,
            max_spread_cents=candidate.reward_max_spread_cents,
            size=size,
            distance_fraction=distance_fraction,
        )
        economics["paired_asks"] = evaluate_paired_ask_quote(
            ask_quote,
            yes_book,
            no_book,
            daily_reward=candidate.daily_reward * reward_multiplier,
            reward_max_spread_cents=candidate.reward_max_spread_cents,
            reward_min_size=candidate.reward_min_size,
            fee_schedule=candidate.fee_schedule,
            stress_ticks=stress_ticks,
            competition_multiplier=competition_multiplier,
        )
        taker_sizes = sorted(
            {
                candidate.min_order_size,
                Decimal("10"),
                Decimal("25"),
                Decimal("50"),
            }
        )
        economics["taker_complete_sets"] = evaluate_taker_complete_sets(
            yes_book,
            no_book,
            sizes=taker_sizes,
            fee_schedule=candidate.fee_schedule,
        )
        return {
            "candidate": candidate,
            "preflight": self._preflight(candidate),
            "book": {
                "yes_best_bid": yes_book.best_bid,
                "yes_best_ask": yes_book.best_ask,
                "no_best_bid": no_book.best_bid,
                "no_best_ask": no_book.best_ask,
                "yes_depth_levels": len(yes_book.bids) + len(yes_book.asks),
                "no_depth_levels": len(no_book.bids) + len(no_book.asks),
            },
            "economics": economics,
        }

    def scan(self, config: ScanConfig) -> dict[str, Any]:
        rows = self.collect_reward_rows(config)
        prefiltered = [
            row
            for row in rows
            if reward_daily_rate(row) >= float(config.min_daily_reward)
            and _token_pair(row) is not None
        ]
        # The endpoint is sorted by reported competitiveness, so cap before
        # condition-level calls while preserving the low-competition search.
        prefiltered = prefiltered[: config.max_evaluations]

        evaluations: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        condition_ids = [str(row.get("condition_id")) for row in prefiltered]
        gamma_markets = self.api.gamma_markets(condition_ids)
        candidates: list[MarketCandidate] = []
        for row in prefiltered:
            condition_id = str(row.get("condition_id"))
            try:
                candidate = self._candidate(
                    row,
                    {},
                    gamma_markets.get(condition_id, {}),
                )
                if candidate is None:
                    raise ValueError("inactive, closed, expired, or not accepting orders")
                if candidate.volume_24h < config.min_volume_24h:
                    rejected.append(
                        {"condition_id": condition_id, "reason": "low_24h_volume"}
                    )
                    continue
                candidates.append(candidate)
            except Exception as exc:
                error = safe_error_details(
                    exc,
                    code="candidate_parse_failed",
                )
                rejected.append(
                    {
                        "condition_id": condition_id,
                        "reason": (
                            f"{error['error_code']}:{error['error_type']}"
                        ),
                    }
                )

        token_ids = [
            token_id
            for candidate in candidates
            for token_id in (candidate.yes_token_id, candidate.no_token_id)
        ]
        books = self.api.books(token_ids)
        for candidate in candidates:
            try:
                yes_book = books[candidate.yes_token_id]
                no_book = books[candidate.no_token_id]
                size = max(
                    config.quote_size,
                    candidate.reward_min_size,
                    candidate.min_order_size,
                )
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
                economics["paired_asks"] = evaluate_paired_ask_quote(
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
                economics["taker_complete_sets"] = evaluate_taker_complete_sets(
                    yes_book,
                    no_book,
                    sizes=sorted(
                        {
                            candidate.min_order_size,
                            Decimal("10"),
                            Decimal("25"),
                            Decimal("50"),
                        }
                    ),
                    fee_schedule=candidate.fee_schedule,
                )
                evaluations.append(
                    {
                        "candidate": candidate,
                        "preflight": self._preflight(candidate),
                        "book": {
                            "yes_best_bid": yes_book.best_bid,
                            "yes_best_ask": yes_book.best_ask,
                            "no_best_bid": no_book.best_bid,
                            "no_best_ask": no_book.best_ask,
                            "yes_depth_levels": len(yes_book.bids)
                            + len(yes_book.asks),
                            "no_depth_levels": len(no_book.bids)
                            + len(no_book.asks),
                        },
                        "economics": economics,
                    }
                )
            except Exception as exc:
                error = safe_error_details(
                    exc,
                    code="candidate_evaluation_failed",
                )
                rejected.append(
                    {
                        "condition_id": candidate.condition_id,
                        "reason": (
                            f"{error['error_code']}:{error['error_type']}"
                        ),
                    }
                )

        def rank_key(result: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
            scenario = result["economics"]["liquidity_reward_scenario"]
            hedge = result["economics"]["one_leg_hedges"]
            reward = scenario["daily_reward_if_static"]
            ask_loss = result["economics"]["paired_asks"]["one_leg_hedges"][
                "worst_loss"
            ]
            losses = [
                item
                for item in (hedge["worst_loss"], ask_loss)
                if item is not None
            ]
            loss = min(losses) if losses else None
            risk_adjusted = (
                reward / (Decimal("1") + loss)
                if loss is not None
                else Decimal("-1")
            )
            return risk_adjusted, reward

        evaluations.sort(key=rank_key, reverse=True)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config": config,
            "reward_rows_seen": len(rows),
            "reward_rows_prefiltered": len(prefiltered),
            "evaluated": len(evaluations),
            "rejected_count": len(rejected),
            "results": evaluations,
            "rejected": rejected,
            "warnings": [
                "Rank is hypothesis triage, not expected PnL.",
                "Reward share uses public aggregate depth without maker identity.",
                "No fill probability is assumed; one-leg hedge loss is reported.",
                "No order was submitted and no credential was read.",
            ],
        }

    def scan_active_complete_sets(
        self,
        *,
        max_markets: int = 2_000,
        page_size: int = 500,
        sizes: tuple[Decimal, ...] = (
            Decimal("5"),
            Decimal("10"),
            Decimal("25"),
            Decimal("50"),
        ),
        execution_buffer_per_share: Decimal = Decimal("0.002"),
    ) -> dict[str, Any]:
        """Scan active binary markets for depth-aware taker complete-set edge."""
        gamma_rows: list[Mapping[str, Any]] = []
        request_size = min(max(page_size, 1), 100)
        for offset in range(0, max_markets, request_size):
            page = self.api.gamma_active_markets(
                limit=min(request_size, max_markets - offset),
                offset=offset,
            )
            if not page:
                break
            gamma_rows.extend(page)
            if len(page) < request_size:
                break

        prepared: list[dict[str, Any]] = []
        for raw in gamma_rows:
            if not raw.get("active") or raw.get("closed"):
                continue
            if not raw.get("acceptingOrders") or not raw.get("enableOrderBook"):
                continue
            token_ids = [str(item) for item in _json_list(raw.get("clobTokenIds"))]
            if len(token_ids) != 2 or not all(token_ids):
                continue
            if not _iso_is_future(raw.get("endDate")):
                continue
            if raw.get("feesEnabled") and not raw.get("feeSchedule"):
                continue
            prepared.append({"raw": raw, "token_ids": token_ids})

        books = self.api.books(
            [
                token_id
                for item in prepared
                for token_id in item["token_ids"]
            ]
        )
        evaluated: list[dict[str, Any]] = []
        skipped_books = 0
        for item in prepared:
            raw = item["raw"]
            first_id, second_id = item["token_ids"]
            first_book = books.get(first_id)
            second_book = books.get(second_id)
            if (
                first_book is None
                or second_book is None
                or first_book.best_bid is None
                or first_book.best_ask is None
                or second_book.best_bid is None
                or second_book.best_ask is None
            ):
                skipped_books += 1
                continue
            min_size = decimal_value(raw.get("orderMinSize"), Decimal("5"))
            market_sizes = tuple(size for size in sizes if size >= min_size)
            if not market_sizes:
                continue
            results = evaluate_taker_complete_sets(
                first_book,
                second_book,
                sizes=market_sizes,
                fee_schedule=FeeSchedule.from_api(raw.get("feeSchedule")),
                execution_buffer_per_share=execution_buffer_per_share,
            )
            edges = [
                edge
                for result in results
                for edge in (
                    result["buy_both"]["net_edge"],
                    result["sell_both_from_split_inventory"]["net_edge"],
                )
                if edge is not None
            ]
            best_edge = max(edges) if edges else None
            evaluated.append(
                {
                    "condition_id": raw.get("conditionId"),
                    "question": raw.get("question"),
                    "end_date": raw.get("endDate"),
                    "fees_enabled": bool(raw.get("feesEnabled")),
                    "fee_schedule": FeeSchedule.from_api(raw.get("feeSchedule")),
                    "min_order_size": min_size,
                    "best_net_edge": best_edge,
                    "results": results,
                }
            )

        evaluated.sort(
            key=lambda item: (
                item["best_net_edge"]
                if item["best_net_edge"] is not None
                else Decimal("-999")
            ),
            reverse=True,
        )
        positive = [
            item
            for item in evaluated
            if item["best_net_edge"] is not None and item["best_net_edge"] > 0
        ]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "gamma_rows_seen": len(gamma_rows),
            "binary_markets_prepared": len(prepared),
            "markets_evaluated": len(evaluated),
            "markets_skipped_for_books": skipped_books,
            "sizes": sizes,
            "execution_buffer_per_share": execution_buffer_per_share,
            "positive_market_count": len(positive),
            "positive_markets": positive,
            "top_near_edges": evaluated[:50],
            "warnings": [
                "Two CLOB legs are non-atomic even when both are FOK.",
                "A positive snapshot is only an observed opportunity, not a fill.",
                "The execution buffer is a scenario and must be replaced by measured latency.",
                "No order was submitted and no credential was read.",
            ],
        }


def serialize_report(report: Any) -> Any:
    return json_safe(report)
