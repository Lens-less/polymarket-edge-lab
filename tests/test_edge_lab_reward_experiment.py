"""Read-only selective-liquidity-reward experiment tests."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from types import MappingProxyType

import pytest

import src.edge_lab.reward_experiment as reward_experiment_module
from src.edge_lab.reward_archive import (
    RecordingClock,
    RecordingRewardSources,
    replay_reward_run,
    save_reward_run,
    verify_reward_run_manifest,
)
from src.edge_lab.reward_experiment import (
    DEFAULT_REWARD_ASSET_ID,
    RewardExperimentConfig,
    run_reward_experiment,
)
from src.edge_lab.reward_experiment_cli import (
    _local_proxy_mapping,
    main,
)
from src.edge_lab.sources import (
    CompactCLOBMarket,
    CompactFeeSchedule,
    CompactRewardConfig,
    FetchMetadata,
    Fetched,
    PublicSourceError,
    RawResponse,
    RewardPage,
    SourceBook,
    SourceBookLevel,
)


D = Decimal
CONDITION = "0xcondition"
YES = "yes-token"
NO = "no-token"


def raw_response(
    source: str,
    body: bytes,
    at: float = 100.0,
    *,
    params: MappingProxyType[str, object] | None = None,
    url: str | None = None,
) -> RawResponse:
    return RawResponse(
        body=body,
        text=body.decode(),
        metadata=FetchMetadata(
            source=source,
            method="GET",
            url=url or f"https://public.example/{source}",
            request_params=params or MappingProxyType({}),
            status_code=200,
            requested_at=at,
            received_at=at + 0.01,
            attempt=1,
            response_headers=MappingProxyType({}),
        ),
    )


def reward_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "condition_id": CONDITION,
        "rewards_config": [
            {
                "asset_address": DEFAULT_REWARD_ASSET_ID,
                "rate_per_day": D("51"),
                "start_date": "2026-07-23",
                "end_date": "2500-12-31",
            }
        ],
        "rewards_max_spread": D("4.5"),
        "rewards_min_size": D("50"),
        "native_daily_rate": D("51"),
        "total_daily_rate": D("51"),
    }
    row.update(overrides)
    return row


def source_book(
    token: str,
    *,
    timestamp_ms: int | None = 100_000,
    size: str = "100",
) -> SourceBook:
    raw = {
        "asset_id": token,
        "timestamp": str(timestamp_ms) if timestamp_ms is not None else None,
        "bids": [{"price": "0.48", "size": size}],
        "asks": [{"price": "0.52", "size": size}],
    }
    return SourceBook(
        token_id=token,
        bids=(
            SourceBookLevel(
                price=D("0.48"),
                size=D(size),
                raw=MappingProxyType(raw["bids"][0]),
            ),
        ),
        asks=(
            SourceBookLevel(
                price=D("0.52"),
                size=D(size),
                raw=MappingProxyType(raw["asks"][0]),
            ),
        ),
        timestamp_ms=timestamp_ms,
        raw=MappingProxyType(raw),
    )


class FakeSources:
    def __init__(
        self,
        *,
        row: dict[str, object] | None = None,
        timestamp_ms: int | None = 100_000,
    ) -> None:
        self.row = row or reward_row()
        self.timestamp_ms = timestamp_ms

    def current_reward_pages(self, **_: object):
        page = RewardPage(
            items=(MappingProxyType(self.row),),
            next_cursor=None,
            response_limit=500,
            count=1,
        )
        yield Fetched(raw=raw_response("current_rewards", b"{}"), value=page)

    def clob_market(self, condition_id: str) -> Fetched[CompactCLOBMarket]:
        assert condition_id == CONDITION
        raw = {
            "c": CONDITION,
            "r": {"mi": 50, "ma": D("4.5"), "e": True, "moas": 30},
            "t": [{"t": YES, "o": "Yes"}, {"t": NO, "o": "No"}],
            "mos": 5,
            "mts": D("0.01"),
            "ao": True,
            "fd": {"r": D("0.05"), "e": D("1"), "to": True},
        }
        market = CompactCLOBMarket(
            condition_id=CONDITION,
            tokens=tuple(MappingProxyType(item) for item in raw["t"]),
            min_order_size=D("5"),
            tick_size=D("0.01"),
            accepting_orders=True,
            rewards=CompactRewardConfig(
                min_size=D("50"),
                max_spread=D("4.5"),
                enabled=True,
                min_order_age_seconds=30,
                skip_min_order_age=False,
            ),
            fees=CompactFeeSchedule(
                rate=D("0.05"),
                exponent=D("1"),
                taker_only=True,
            ),
            raw=MappingProxyType(raw),
        )
        return Fetched(
            raw=raw_response("compact_market", b"{}"),
            value=market,
        )

    def book(self, token_id: str) -> Fetched[SourceBook]:
        return Fetched(
            raw=raw_response(f"book-{token_id}", b"{}"),
            value=source_book(token_id, timestamp_ms=self.timestamp_ms),
        )


class RankingOnlySources:
    def current_reward_pages(self, **_: object):
        rows = (
            reward_row(
                condition_id="high-pool-high-risk",
                total_daily_rate=D("100"),
                native_daily_rate=D("100"),
                rewards_max_spread=D("1"),
                rewards_min_size=D("100"),
            ),
            reward_row(
                condition_id="lower-pool-better-risk",
                total_daily_rate=D("50"),
                native_daily_rate=D("50"),
                rewards_max_spread=D("5"),
                rewards_min_size=D("10"),
            ),
        )
        yield Fetched(
            raw=raw_response("current_rewards", b"{}"),
            value=RewardPage(
                items=tuple(MappingProxyType(row) for row in rows),
                next_cursor=None,
                response_limit=500,
                count=2,
            ),
        )

    def clob_market(self, condition_id: str) -> Fetched[CompactCLOBMarket]:
        raise RuntimeError(f"offline ranking fixture: {condition_id}")

    def book(self, token_id: str) -> Fetched[SourceBook]:
        raise AssertionError(f"book should not be requested: {token_id}")


class AssetScopeSources:
    def current_reward_pages(self, **_: object):
        rows = (
            reward_row(
                condition_id="other-asset-high-pool",
                total_daily_rate=D("1000"),
                rewards_config=[
                    {
                        "asset_address": "OTHER",
                        "rate_per_day": D("1000"),
                    }
                ],
            ),
            reward_row(
                condition_id="target-asset",
                total_daily_rate=D("10"),
                rewards_config=[
                    {
                        "asset_address": "TARGET",
                        "rate_per_day": D("10"),
                    }
                ],
            ),
        )
        yield Fetched(
            raw=raw_response("current_rewards", b"{}"),
            value=RewardPage(
                items=tuple(MappingProxyType(row) for row in rows),
                next_cursor=None,
                response_limit=500,
                count=2,
            ),
        )

    def clob_market(self, condition_id: str) -> Fetched[CompactCLOBMarket]:
        raise RuntimeError(f"offline asset-scope fixture: {condition_id}")

    def book(self, token_id: str) -> Fetched[SourceBook]:
        raise AssertionError(f"book should not be requested: {token_id}")


class FinalRankingSources:
    conditions = ("high-prefilter-high-competition", "lower-prefilter")

    def current_reward_pages(self, **_: object):
        rows = (
            reward_row(
                condition_id=self.conditions[0],
                total_daily_rate=D("1000"),
                native_daily_rate=D("1000"),
            ),
            reward_row(
                condition_id=self.conditions[1],
                total_daily_rate=D("10"),
                native_daily_rate=D("10"),
            ),
        )
        yield Fetched(
            raw=raw_response("current_rewards", b"{}"),
            value=RewardPage(
                items=tuple(MappingProxyType(row) for row in rows),
                next_cursor=None,
                response_limit=500,
                count=2,
            ),
        )

    def clob_market(self, condition_id: str) -> Fetched[CompactCLOBMarket]:
        token_prefix = "high" if condition_id == self.conditions[0] else "low"
        raw = {
            "c": condition_id,
            "r": {"mi": 50, "ma": D("4.5"), "e": True, "moas": 30},
            "t": [
                {"t": f"{token_prefix}-yes", "o": "Yes"},
                {"t": f"{token_prefix}-no", "o": "No"},
            ],
            "mos": 5,
            "mts": D("0.01"),
            "ao": True,
            "fd": {"r": D("0.05"), "e": D("1"), "to": True},
        }
        return Fetched(
            raw=raw_response("compact_market", b"{}"),
            value=CompactCLOBMarket(
                condition_id=condition_id,
                tokens=tuple(MappingProxyType(item) for item in raw["t"]),
                min_order_size=D("5"),
                tick_size=D("0.01"),
                accepting_orders=True,
                rewards=CompactRewardConfig(
                    min_size=D("50"),
                    max_spread=D("4.5"),
                    enabled=True,
                    min_order_age_seconds=30,
                    skip_min_order_age=False,
                ),
                fees=CompactFeeSchedule(
                    rate=D("0.05"),
                    exponent=D("1"),
                    taker_only=True,
                ),
                raw=MappingProxyType(raw),
            ),
        )

    def book(self, token_id: str) -> Fetched[SourceBook]:
        size = "10000" if token_id.startswith("high-") else "50"
        return Fetched(
            raw=raw_response(f"book-{token_id}", b"{}"),
            value=source_book(token_id, size=size),
        )


class ReplayableFakeSources(FakeSources):
    """Fake public source whose raw bytes exactly encode its parsed values."""

    def current_reward_pages(self, **_: object):
        payload = {
            "data": [self.row],
            "limit": 500,
            "count": 1,
            "next_cursor": None,
        }
        body = json.dumps(payload, default=str, sort_keys=True).encode()
        page = RewardPage(
            items=(MappingProxyType(self.row),),
            next_cursor=None,
            response_limit=500,
            count=1,
        )
        yield Fetched(
            raw=raw_response(
                "clob_rewards",
                body,
                params=MappingProxyType({"limit": 500}),
                url="https://clob.polymarket.com/rewards/markets/current",
            ),
            value=page,
        )

    def clob_market(self, condition_id: str) -> Fetched[CompactCLOBMarket]:
        fetched = super().clob_market(condition_id)
        body = json.dumps(
            dict(fetched.value.raw),
            default=str,
            sort_keys=True,
        ).encode()
        return Fetched(
            raw=raw_response(
                "clob",
                body,
                url=(
                    "https://clob.polymarket.com/clob-markets/"
                    f"{condition_id}"
                ),
            ),
            value=fetched.value,
        )

    def book(self, token_id: str) -> Fetched[SourceBook]:
        fetched = super().book(token_id)
        body = json.dumps(
            dict(fetched.value.raw),
            default=str,
            sort_keys=True,
        ).encode()
        return Fetched(
            raw=raw_response(
                "clob_book",
                body,
                params=MappingProxyType({"token_id": token_id}),
                url="https://clob.polymarket.com/book",
            ),
            value=fetched.value,
        )


class TruncatedReplayableSources(ReplayableFakeSources):
    def current_reward_pages(self, **_: object):
        payload = {
            "data": [self.row],
            "limit": 500,
            "count": 1,
            "next_cursor": "MORE",
        }
        body = json.dumps(payload, default=str, sort_keys=True).encode()
        yield Fetched(
            raw=raw_response(
                "clob_rewards",
                body,
                params=MappingProxyType({"limit": 500}),
                url="https://clob.polymarket.com/rewards/markets/current",
            ),
            value=RewardPage(
                items=(MappingProxyType(self.row),),
                next_cursor="MORE",
                response_limit=500,
                count=1,
            ),
        )


class FailedCompactReplayableSources(ReplayableFakeSources):
    def clob_market(self, condition_id: str) -> Fetched[CompactCLOBMarket]:
        raise PublicSourceError(
            "sanitized parse failure",
            raw=raw_response(
                "clob",
                b'{"malformed":true}',
                url=(
                    "https://clob.polymarket.com/clob-markets/"
                    f"{condition_id}"
                ),
            ),
        )


def test_runner_keeps_theory_scoring_and_payout_separate() -> None:
    result = run_reward_experiment(
        FakeSources(),
        config=RewardExperimentConfig(max_markets=5),
        clock=lambda: 100.1,
    )

    assert result["safety"]["read_only_public_endpoints_only"] is True
    assert result["universe"]["selected_markets"] == 1
    market = result["markets"][0]
    assert market["condition_id"] == CONDITION
    assert market["classification"] == "promising_not_validated"
    assert market["validation_ceiling"]["classification_ceiling"] == (
        "promising_not_validated"
    )
    assert market["theoretical_ledger"]["recognized_pnl"] == "0"
    assert market["scoring_ledger"]["records"] == []
    assert market["payout_ledger"]["records"] == []
    assert market["scoring_ledger"]["recognized_pnl"] == "0"
    assert market["payout_ledger"]["recognized_pnl"] == "0"
    assert [
        row["reward_multiplier"] for row in market["conditional_haircuts"]
    ] == [
        "1",
        "0.5",
        "0.3",
        "0",
    ]
    assert set(market["competition_proxy"]["conditional_share_grid"]) == {
        "1",
        "3",
        "10",
    }
    assert market["shadow_fill_scenario"][
        "public_fill_probability_lower_bound"
    ] == "0"
    assert market["instantaneous_zero_latency_shadow_one_leg"][
        "yes_fills_first"
    ][
        "complete"
    ]
    assert market["fee_pressure"]["fee_schedule"]["taker_only"] is True
    assert market["ranking"]["public_fill_probability_lower_bound"] == "0"
    assert market["ranking"]["recognized_reward_pnl"] == "0"
    assert market["ranking"]["validated_reward_to_risk_score"] is None


def test_runner_labels_all_unfilled_economics_as_conditional_shadow() -> None:
    result = run_reward_experiment(
        FakeSources(),
        config=RewardExperimentConfig(max_markets=1),
        clock=lambda: 100.1,
    )
    market = result["markets"][0]

    assert result["schema_version"] == "edge-lab-liquidity-reward-experiment-v2"
    assert result["profit_claim"] == "none"
    assert result["recognized_trading_pnl"] == "0"
    assert result["recognized_reward_pnl"] == "0"
    assert result["recognized_total_pnl"] == "0"
    assert result["actual_fill_count"] == 0
    assert result["explainable_fill_count"] == 0
    assert market["profit_claim"] == "none"
    assert market["recognized_trading_pnl"] == "0"
    assert market["recognized_reward_pnl"] == "0"
    assert market["recognized_total_pnl"] == "0"
    assert market["actual_fill_count"] == 0
    assert market["explainable_fill_count"] == 0
    assert market["shadow_fill_scenario"][
        "conditional_paired_fill_cashflow_before_operation_cost"
    ] is not None
    assert market["instantaneous_zero_latency_shadow_one_leg"][
        "hedge_execution_latency_ms"
    ] == 0
    assert market["instantaneous_zero_latency_shadow_one_leg"][
        "cancel_latency_ms"
    ] is None
    assert market["instantaneous_zero_latency_shadow_one_leg"][
        "operation_cost"
    ] is None
    assert market["shadow_reward_sensitivity"]["scenario_label"] == (
        "shadow_reward_independent_if_assumed_fills"
    )
    assert market["fee_pressure"]["rounding"] == {
        "quantum": "0.00001",
        "rule": "ROUND_HALF_UP_assumption",
        "platform_halfway_boundary_verified": False,
    }
    assert market["fee_pressure"]["one_fee_quantum_stress"][
        "conditional_shadow_non_reward_cashflow"
    ] is not None
    assert [row["reward_multiplier"] for row in market["conditional_haircuts"]] == [
        "1",
        "0.5",
        "0.3",
        "0",
    ]

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {
                key
                for child in value.values()
                for key in keys(child)
            }
        if isinstance(value, list):
            return {
                key
                for child in value
                for key in keys(child)
            }
        return set()

    artifact_keys = keys(result)
    assert "pnl" not in artifact_keys
    assert "net_profit" not in artifact_keys
    assert "unit_scenario_non_reward_profit_before_operation_cost" not in artifact_keys
    assert "paired_fill_pnl_before_operation_cost" not in artifact_keys
    assert "worst_one_leg_pnl_before_operation_cost" not in artifact_keys


def test_runner_marks_full_reward_only_shadow_economics_as_fragile() -> None:
    result = run_reward_experiment(
        FakeSources(),
        config=RewardExperimentConfig(
            max_markets=1,
            adverse_one_leg_fills_per_day=31,
        ),
        clock=lambda: 100.1,
    )
    market = result["markets"][0]
    cashflow_by_reward = market["shadow_reward_sensitivity"][
        "conditional_cashflow_by_reward_multiplier"
    ]

    assert Decimal(cashflow_by_reward["1"]) > 0
    assert Decimal(cashflow_by_reward["0"]) <= 0
    assert market["classification"] == "promising_not_validated"
    assert market["conditional_robustness_label"] == "fragile"
    assert market["shadow_reward_sensitivity"][
        "conditional_robustness_label"
    ] == "fragile"


def test_one_quantum_stress_covers_non_taker_only_maker_fees() -> None:
    sources = FakeSources()
    original = sources.clob_market

    def all_fills_pay_fees(
        condition_id: str,
    ) -> Fetched[CompactCLOBMarket]:
        fetched = original(condition_id)
        raw = dict(fetched.value.raw)
        raw["fd"] = {"r": D("0.05"), "e": D("1"), "to": False}
        return Fetched(
            raw=fetched.raw,
            value=CompactCLOBMarket(
                condition_id=fetched.value.condition_id,
                tokens=fetched.value.tokens,
                min_order_size=fetched.value.min_order_size,
                tick_size=fetched.value.tick_size,
                accepting_orders=fetched.value.accepting_orders,
                rewards=fetched.value.rewards,
                fees=CompactFeeSchedule(
                    rate=D("0.05"),
                    exponent=D("1"),
                    taker_only=False,
                ),
                raw=MappingProxyType(raw),
            ),
        )

    sources.clob_market = all_fills_pay_fees  # type: ignore[method-assign]
    result = run_reward_experiment(
        sources,
        config=RewardExperimentConfig(max_markets=1),
        clock=lambda: 100.1,
    )
    market = result["markets"][0]
    stress = market["fee_pressure"]["one_fee_quantum_stress"]
    baseline = Decimal(
        market["conditional_haircuts"][0][
            "conditional_non_reward_cashflow"
        ]
    )
    stressed = Decimal(stress["conditional_shadow_non_reward_cashflow"])

    assert stress["shadow_paired_fill_fee_incurrences"] == 2
    assert stress["shadow_one_leg_maximum_fee_incurrences"] == 2
    assert baseline - stressed == D("0.00004")


def test_runner_exposes_proxy_midpoint_conditional_epoch_and_rank_sensitivity() -> None:
    result = run_reward_experiment(
        FakeSources(),
        config=RewardExperimentConfig(max_markets=1),
        clock=lambda: 100.1,
    )
    market = result["markets"][0]
    reference = market["market_snapshot"]["reward_reference_price_proxy"]
    ledger = market["theoretical_ledger"]
    competition = market["competition_proxy"]
    sensitivity = market["conditional_reward_sensitivity"]

    assert reference["method"] == "proxy_blended_l1_midpoint"
    assert reference["platform_midpoint_verified"] is False
    assert reference["size_cutoff_method"] is None
    assert reference["value"] == "0.50"
    assert ledger["expected_reward"]["value"] is None
    assert "minute" in ledger["expected_reward"]["reason"]
    assert ledger["epoch_model"]["verified"] is False
    assert ledger[
        "conditional_pool_allocation_if_snapshot_share_persisted_full_epoch_at_3x"
    ] is not None
    assert competition["maker_identity_available"] is False
    assert competition["aggregate_book_score_is_upper_bound"] is False
    assert competition["competition_multiplier_assumption"] == "3"
    assert len(sensitivity["scenarios"]) == 18
    assert sensitivity["capital_risk_denominator_held_at_baseline"] is True
    assert market["ranking"]["sensitivity_rank_min"] == 1
    assert market["ranking"]["sensitivity_rank_max"] == 1
    assert result["rank_stability"]["scenario_count"] == 18
    assert result["rank_stability"]["market_count"] == 1


def test_runner_prefilters_by_reward_to_risk_not_displayed_pool() -> None:
    result = run_reward_experiment(
        RankingOnlySources(),
        config=RewardExperimentConfig(max_markets=2),
        clock=lambda: 100.1,
    )

    assert [market["condition_id"] for market in result["markets"]] == [
        "lower-pool-better-risk",
        "high-pool-high-risk",
    ]
    assert result["markets"][0]["ranking"][
        "preselection_reward_risk_proxy"
    ] == "25"
    assert "total_daily_rate * rewards_max_spread" in result["universe"][
        "selection_rule"
    ]


def test_runner_applies_explicit_reward_asset_scope_before_prefilter() -> None:
    result = run_reward_experiment(
        AssetScopeSources(),
        config=RewardExperimentConfig(
            max_markets=2,
            reward_asset_id="TARGET",
        ),
        clock=lambda: 100.1,
    )

    assert [market["condition_id"] for market in result["markets"]] == [
        "target-asset"
    ]
    assert result["universe"]["unique_current_reward_markets"] == 2
    assert result["universe"]["asset_scoped_reward_markets"] == 1
    assert result["universe"]["excluded_by_reward_asset_scope"] == 1
    assert result["universe"]["reward_asset_scope"] == "TARGET"


def test_reward_v2_config_requires_a_single_reward_asset_scope() -> None:
    with pytest.raises(ValueError, match="reward_asset_id"):
        RewardExperimentConfig(reward_asset_id=None)


@pytest.mark.parametrize("invalid", (D("NaN"), D("Infinity")))
def test_reward_v2_config_rejects_non_finite_decimals(
    invalid: Decimal,
) -> None:
    with pytest.raises(ValueError, match="quote_distance_fraction"):
        RewardExperimentConfig(quote_distance_fraction=invalid)
    with pytest.raises(ValueError, match="competition multipliers"):
        RewardExperimentConfig(
            competition_multipliers=(D("1"), D("3"), invalid)
        )


def test_reward_asset_scope_normalizes_evm_address_case() -> None:
    row = reward_row(
        rewards_config=[
            {
                "asset_address": DEFAULT_REWARD_ASSET_ID.lower(),
                "rate_per_day": D("51"),
            }
        ]
    )
    result = run_reward_experiment(
        FakeSources(row=row),
        config=RewardExperimentConfig(
            max_markets=1,
            reward_asset_id=DEFAULT_REWARD_ASSET_ID.upper(),
        ),
        clock=lambda: 100.1,
    )

    assert result["universe"]["asset_scoped_reward_markets"] == 1
    assert result["universe"]["reward_asset_scope"] == (
        DEFAULT_REWARD_ASSET_ID.lower()
    )


def test_final_ranking_can_reverse_prefilter_and_includes_one_leg_downside() -> None:
    result = run_reward_experiment(
        FinalRankingSources(),
        config=RewardExperimentConfig(max_markets=2),
        clock=lambda: 100.1,
    )

    assert [market["condition_id"] for market in result["markets"]] == [
        "lower-prefilter",
        "high-prefilter-high-competition",
    ]
    lower, high = result["markets"]
    assert lower["ranking"]["preselection_rank"] == 2
    assert high["ranking"]["preselection_rank"] == 1
    assert D(
        lower["ranking"]["worst_one_leg_downside_before_operation_cost"]
    ) > D("0")
    assert D(lower["ranking"]["capital_risk_denominator"]) == (
        D(lower["ranking"]["quote_capital"])
        + D(lower["ranking"]["worst_one_leg_downside_before_operation_cost"])
    )
    assert D(
        lower["ranking"]["conditional_reward_to_capital_risk_proxy"]
    ) > D(high["ranking"]["conditional_reward_to_capital_risk_proxy"])


def test_runner_fails_closed_when_book_timestamp_is_missing() -> None:
    result = run_reward_experiment(
        FakeSources(timestamp_ms=None),
        config=RewardExperimentConfig(max_markets=1),
        clock=lambda: 100.1,
    )

    market = result["markets"][0]
    assert market["classification"] == "insufficient_data"
    assert "missing_book_timestamp" in market["failure_reasons"]
    assert market["scoring_ledger"]["records"] == []
    assert market["payout_ledger"]["records"] == []


def test_runner_rejects_current_and_compact_reward_term_mismatch() -> None:
    result = run_reward_experiment(
        FakeSources(row=reward_row(rewards_min_size=D("40"))),
        config=RewardExperimentConfig(max_markets=1),
        clock=lambda: 100.1,
    )

    market = result["markets"][0]
    assert market["classification"] == "rejected"
    assert "current_compact_min_size_mismatch" in market["failure_reasons"]


def test_runner_accepts_arbitrary_binary_outcome_labels() -> None:
    sources = FakeSources()
    original = sources.clob_market

    def arbitrary(condition_id: str) -> Fetched[CompactCLOBMarket]:
        fetched = original(condition_id)
        raw = dict(fetched.value.raw)
        raw["t"] = [
            {"t": YES, "o": "Rune Eaters"},
            {"t": NO, "o": "Entropy"},
        ]
        market = CompactCLOBMarket(
            condition_id=fetched.value.condition_id,
            tokens=tuple(MappingProxyType(item) for item in raw["t"]),
            min_order_size=fetched.value.min_order_size,
            tick_size=fetched.value.tick_size,
            accepting_orders=fetched.value.accepting_orders,
            rewards=fetched.value.rewards,
            fees=fetched.value.fees,
            raw=MappingProxyType(raw),
        )
        return Fetched(raw=fetched.raw, value=market)

    sources.clob_market = arbitrary  # type: ignore[method-assign]
    result = run_reward_experiment(
        sources,
        config=RewardExperimentConfig(max_markets=1),
        clock=lambda: 100.1,
    )

    market = result["markets"][0]
    assert market["classification"] == "promising_not_validated"
    assert market["market_snapshot"]["token_roles"][0]["outcome"] == (
        "Rune Eaters"
    )


@pytest.mark.parametrize(
    ("target", "safe_code"),
    (
        (
            "src.edge_lab.reward_experiment._finite_decimal",
            "invalid_current_reward_numeric_field",
        ),
        (
            "src.edge_lab.reward_experiment._token_pair",
            "compact_market_token_pair_invalid",
        ),
        (
            "src.edge_lab.reward_experiment._combined_yes_midpoint",
            "combined_midpoint_invalid",
        ),
    ),
)
def test_persisted_validation_failures_never_include_raw_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    safe_code: str,
) -> None:
    sensitive = "https://public.example/?" + "api_key=do-not-persist"
    original_finite_decimal = reward_experiment_module._finite_decimal

    def explode(*args: object, **kwargs: object) -> object:
        if (
            target.endswith("._finite_decimal")
            and kwargs.get("name") != "total_daily_rate"
        ):
            return original_finite_decimal(*args, **kwargs)
        raise ValueError(sensitive)

    monkeypatch.setattr(target, explode)
    result = run_reward_experiment(
        FakeSources(),
        config=RewardExperimentConfig(max_markets=1),
        clock=lambda: 100.1,
    )
    serialized = json.dumps(result, sort_keys=True)

    assert sensitive not in serialized
    assert safe_code in result["markets"][0]["failure_reasons"]


@pytest.mark.parametrize(
    ("delegate", "reason"),
    (
        (
            FailedCompactReplayableSources(),
            "uncaptured source failure",
        ),
        (
            TruncatedReplayableSources(),
            "pagination is incomplete",
        ),
    ),
)
def test_versioned_archive_refuses_non_replayable_or_partial_runs(
    tmp_path: Path,
    delegate: ReplayableFakeSources,
    reason: str,
) -> None:
    recording = RecordingRewardSources(delegate)
    result = run_reward_experiment(
        recording,
        config=RewardExperimentConfig(max_markets=1, max_reward_pages=1),
        clock=lambda: 100.1,
    )

    with pytest.raises(ValueError, match=reason):
        save_reward_run(
            result,
            recording,
            clock_values=(100.1,),
            output_dir=tmp_path,
        )
    assert not (tmp_path / "reward_runs").exists()


def test_cli_validate_only_checks_guard_without_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "--validate-only",
                "--max-markets",
                "2",
                "--output-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert '"live_order_guard_active": true' in capsys.readouterr().out


def test_cli_rejects_remote_or_authenticated_proxy() -> None:
    with pytest.raises(ValueError, match="loopback"):
        _local_proxy_mapping("https://proxy.example:443")
    with pytest.raises(ValueError, match="loopback"):
        _local_proxy_mapping("http://user:" + "pass@127.0.0.1:7897")


def test_cli_requires_explicit_loopback_proxy_before_live_public_capture(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="explicit.*loopback proxy"):
        main(["--output-dir", str(tmp_path)])


def test_versioned_reward_run_replays_offline_and_rejects_tampered_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = RecordingRewardSources(ReplayableFakeSources())
    clock_values = iter((100.1, 100.1, 100.1))
    clock = RecordingClock(lambda: next(clock_values))
    config = RewardExperimentConfig(max_markets=1)
    result = run_reward_experiment(source, config=config, clock=clock)

    run_dir = save_reward_run(
        result,
        source,
        clock_values=clock.values,
        output_dir=tmp_path,
    )
    manifest_path = run_dir / "RUN_MANIFEST.json"
    original_manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_path.read_text())
    universe_rows = (run_dir / "REWARD_UNIVERSE.jsonl").read_text().splitlines()

    assert manifest["capture"]["raw_reward_page_count"] == 1
    assert manifest["capture"]["raw_universe_row_count"] == 1
    assert manifest["capture"]["unique_condition_count"] == 1
    assert [
        entry["source_sequence"]
        for entry in manifest["files"]
        if entry["kind"].endswith("_raw")
    ] == list(range(1, 5))
    assert json.loads(universe_rows[0])["preselection_rank"] == 1
    verified = verify_reward_run_manifest(manifest_path)
    assert verified["canonical_artifact"] == "EXPERIMENT.json"
    assert verified["canonical_artifact_sha256"] == next(
        entry["sha256"]
        for entry in manifest["files"]
        if entry["kind"] == "experiment"
    )
    replay = replay_reward_run(manifest_path)
    assert replay["artifact_bytes_match"] is True
    assert replay["universe_bytes_match"] is True
    assert replay["network_requests"] == 0
    monkeypatch.setattr(
        "src.edge_lab.reward_experiment_cli.requests.Session",
        lambda: (_ for _ in ()).throw(
            AssertionError("replay must not construct a network session")
        ),
    )
    assert main(["--replay-manifest", str(manifest_path)]) == 0
    cli_replay = json.loads(capsys.readouterr().out)
    assert cli_replay["artifact_bytes_match"] is True
    assert cli_replay["network_requests"] == 0

    page_entry = next(
        entry
        for entry in manifest["files"]
        if entry["kind"] == "reward_page_raw"
    )
    page_entry["cursor_out"] = "tampered-cursor"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="cursor_out mismatch"):
        replay_reward_run(manifest_path)
    manifest_path.write_bytes(original_manifest_bytes)

    missing_provenance = json.loads(original_manifest_bytes)
    missing_provenance["provenance"]["code"] = []
    manifest_path.write_text(json.dumps(missing_provenance))
    with pytest.raises(ValueError, match="provenance code paths mismatch"):
        verify_reward_run_manifest(manifest_path)
    manifest_path.write_bytes(original_manifest_bytes)

    untrusted_root = json.loads(original_manifest_bytes)
    untrusted_root["provenance"]["repo_root"] = ""
    manifest_path.write_text(json.dumps(untrusted_root))
    with pytest.raises(ValueError, match="trusted repo"):
        verify_reward_run_manifest(manifest_path)
    manifest_path.write_bytes(original_manifest_bytes)

    book_entry = next(
        entry
        for entry in manifest["files"]
        if entry["kind"] == "book_raw"
    )
    book_path = run_dir / book_entry["path"]
    book_path.write_bytes(book_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="mismatch"):
        replay_reward_run(manifest_path)
