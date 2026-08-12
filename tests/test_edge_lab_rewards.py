"""Evidence-tiered liquidity-reward accounting tests."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from src.edge_lab.rewards import (
    PublicRewardBook,
    RewardLevel,
    RewardMarketConfig,
    RewardPayout,
    RewardScoring,
    apply_reward_haircuts,
    classify_reward_stress,
    official_combined_score,
    reward_validation_ceiling,
    score_public_reward_book,
)


D = Decimal


def compact_config(**reward_overrides: object) -> RewardMarketConfig:
    reward: dict[str, object] = {
        "mi": "10",
        "ma": "10",
        "e": True,
        "moas": "30",
    }
    reward.update(reward_overrides)
    return RewardMarketConfig.from_compact_market(
        {"c": "condition-1", "r": reward}
    )


def level(price: str, size: str = "100", age: str | None = "30") -> RewardLevel:
    return RewardLevel(
        price=D(price),
        size=D(size),
        age_seconds=D(age) if age is not None else None,
    )


def test_official_q_formula_uses_complement_ask_and_c_equals_three() -> None:
    # At a 0.50 midpoint, both a current 0.46 bid and a complement 0.54 ask
    # express the same current-side 0.46 price.  They therefore both belong in
    # Q_one.  Each contributes ((10 - 4) / 10)^2 * 100 = 36.
    book = PublicRewardBook(
        current_bids=(level("0.46"),),
        current_asks=(level("0.56"),),
        complement_bids=(level("0.44"),),
        complement_asks=(level("0.54"),),
    )
    result = score_public_reward_book(
        config=compact_config(),
        midpoint=D("0.50"),
        book=book,
        daily_reward_pool=D("25"),
    )

    assert result.component_score("current_bids") == D("36.00")
    assert result.component_score("complement_asks") == D("36.00")
    assert result.q_one_upper == D("72.00")
    assert result.q_two_upper == D("32.00")
    # max(min(72, 32), max(72 / 3, 32 / 3)) = 32
    assert result.score_upper == D("32.00")
    assert result.score_lower == D("0")
    assert result.reward_lower == D("0")
    assert result.reward_upper == D("25")
    assert result.recognized_pnl == D("0")


def test_aggregate_public_book_score_is_not_a_maker_score_upper_bound() -> None:
    # Maker A has balanced (1, 1) score 1. Maker B has one-sided (1, 0)
    # score 1/3. Public L2 exposes only aggregate (2, 1), whose score is 1.
    # Therefore scoring aggregate depth as one maker understates the actual
    # sum 4/3 and cannot be labeled an upper bound.
    maker_sum = official_combined_score(D("1"), D("1"), D("0.5")) + (
        official_combined_score(D("1"), D("0"), D("0.5"))
    )
    aggregate = official_combined_score(D("2"), D("1"), D("0.5"))

    assert maker_sum == D("1.333333333333333333333333333")
    assert aggregate == D("1")
    assert aggregate < maker_sum


def test_extreme_midpoint_requires_both_sides() -> None:
    one_sided = PublicRewardBook(current_bids=(level("0.04"),))

    middle = score_public_reward_book(
        config=compact_config(),
        midpoint=D("0.05"),
        book=one_sided,
    )
    assert middle.q_one_upper > D("0")
    assert middle.q_two_upper == D("0")
    assert middle.score_upper == D("0")
    assert "extreme_midpoint_requires_two_sides" in middle.notes


def test_compact_config_and_eligibility_fail_closed() -> None:
    config = compact_config()
    assert config.condition_id == "condition-1"
    assert config.minimum_size == D("10")
    assert config.maximum_spread_cents == D("10")
    assert config.minimum_order_age_seconds == D("30")
    assert config.enabled
    assert config.eligibility_configured

    observations = PublicRewardBook(
        current_bids=(
            level("0.46", size="9"),  # below min size
            level("0.46", age=None),  # unknown age
            level("0.39"),  # outside max spread
        )
    )
    result = score_public_reward_book(
        config=config,
        midpoint=D("0.50"),
        book=observations,
    )
    assert result.score_upper == D("0")
    assert set(result.rejection_reasons) >= {
        "current_bids:below_minimum_size",
        "current_bids:unknown_order_age",
        "current_bids:outside_maximum_spread",
    }

    missing_age_config = RewardMarketConfig.from_compact_market(
        {"c": "condition-1", "r": {"mi": "10", "ma": "10", "e": True}}
    )
    assert not missing_age_config.eligibility_configured
    failed = score_public_reward_book(
        config=missing_age_config,
        midpoint=D("0.50"),
        book=PublicRewardBook(current_bids=(level("0.46"),)),
    )
    assert failed.score_upper == D("0")
    assert "config:missing_minimum_order_age" in failed.rejection_reasons

    top_level_age = RewardMarketConfig.from_compact_market(
        {
            "c": "condition-1",
            "r": {"mi": "10", "ma": "10", "e": True},
            "oas": "9",
        }
    )
    assert top_level_age.minimum_order_age_seconds == D("9")
    assert top_level_age.eligibility_configured

    malformed = RewardMarketConfig.from_compact_market(
        {
            "c": "condition-1",
            "r": {"mi": "not-a-number", "ma": "10", "e": True, "moas": "30"},
        }
    )
    assert not malformed.eligibility_configured
    assert "missing_minimum_size" in malformed.rejection_reasons


def test_three_ledgers_never_promote_theory_or_scoring_to_realized_pnl() -> None:
    theoretical = score_public_reward_book(
        config=compact_config(),
        midpoint=D("0.50"),
        book=PublicRewardBook(
            current_bids=(level("0.46"),),
            current_asks=(level("0.54"),),
        ),
        daily_reward_pool=D("12"),
    )
    scoring = RewardScoring(
        order_id="order-1",
        auth_evidence_ref="evidence/auth/order-1.json",
        maker_evidence_ref="evidence/maker-address.txt",
        condition_id="condition-1",
        asset_id="yes-token",
        epoch_id="2026-07-24",
        observed_at="2026-07-24T10:00:00Z",
        scoring=True,
        reward_percentage=D("0.01"),
    )
    unconfirmed = RewardPayout(
        payout_date="2026-07-24",
        epoch_id="epoch-unconfirmed",
        condition_id="condition-1",
        asset_id="pUSD",
        maker_evidence_ref="evidence/maker-address.txt",
        amount=D("0.90"),
        payment_confirmed=False,
    )
    confirmed = RewardPayout(
        payout_date="2026-07-25",
        epoch_id="epoch-confirmed",
        condition_id="condition-1",
        asset_id="pUSD",
        maker_evidence_ref="evidence/maker-address.txt",
        amount=D("0.80"),
        payment_confirmed=True,
        payment_evidence_ref="evidence/payout/tx-1.json",
    )

    assert theoretical.recognized_pnl == D("0")
    assert scoring.recognized_pnl == D("0")
    assert unconfirmed.recognized_pnl == D("0")
    # The USD 1 minimum must not be assumed, but direct payment evidence wins:
    # a confirmed sub-threshold payment is an observed fact, not an estimate.
    assert confirmed.recognized_pnl == D("0.80")
    assert theoretical.to_dict()["score_upper"] == "36.00"
    assert scoring.to_dict()["reward_percentage"] == "0.01"
    assert confirmed.to_dict()["recognized_pnl"] == "0.80"


def test_scoring_and_confirmed_payout_require_evidence() -> None:
    with pytest.raises(ValueError, match="order_id"):
        RewardScoring(
            order_id="",
            auth_evidence_ref="evidence.json",
            maker_evidence_ref="maker.json",
            condition_id="condition-1",
            asset_id="yes-token",
            epoch_id="epoch-1",
            observed_at="2026-07-24T10:00:00Z",
            scoring=True,
        )
    with pytest.raises(ValueError, match="auth_evidence_ref"):
        RewardScoring(
            order_id="order-1",
            auth_evidence_ref="",
            maker_evidence_ref="maker.json",
            condition_id="condition-1",
            asset_id="yes-token",
            epoch_id="epoch-1",
            observed_at="2026-07-24T10:00:00Z",
            scoring=True,
        )
    with pytest.raises(ValueError, match="payment_evidence_ref"):
        RewardPayout(
            payout_date="2026-07-24",
            epoch_id="epoch-1",
            condition_id="condition-1",
            asset_id="pUSD",
            maker_evidence_ref="maker.json",
            amount=D("2"),
            payment_confirmed=True,
        )


def test_fixed_haircuts_leave_confirmed_payout_untouched_and_include_zero() -> None:
    confirmed = RewardPayout(
        payout_date="2026-07-24",
        epoch_id="epoch-confirmed",
        condition_id="condition-1",
        asset_id="pUSD",
        maker_evidence_ref="maker.json",
        amount=D("2"),
        payment_confirmed=True,
        payment_evidence_ref="payment.json",
    )
    scenarios = apply_reward_haircuts(
        non_reward_profit=D("-5"),
        theoretical_reward=D("10"),
        condition_id="condition-1",
        reward_asset_id="pUSD",
        maker_evidence_ref="maker.json",
        theoretical_epoch_ids=("epoch-theory",),
        confirmed_payouts=(confirmed,),
    )

    assert [item.multiplier for item in scenarios] == [
        D("1"),
        D("0.5"),
        D("0.3"),
        D("0"),
    ]
    assert [item.confirmed_reward for item in scenarios] == [D("2")] * 4
    assert [item.net_profit for item in scenarios] == [
        D("7"),
        D("2.0"),
        D("0.0"),
        D("-3"),
    ]


def test_fragile_and_epoch_evidence_ceiling() -> None:
    fragile_scenarios = apply_reward_haircuts(
        non_reward_profit=D("-9"),
        theoretical_reward=D("10"),
        condition_id="condition-1",
        reward_asset_id="pUSD",
        maker_evidence_ref="maker.json",
        theoretical_epoch_ids=("epoch-theory",),
    )
    stress = classify_reward_stress(fragile_scenarios)
    assert stress.classification == "fragile"
    assert not stress.reward_zero_positive

    scoring = RewardScoring(
        order_id="order-1",
        auth_evidence_ref="auth.json",
        maker_evidence_ref="maker.json",
        condition_id="condition-1",
        asset_id="yes-token",
        epoch_id="epoch-1",
        observed_at="2026-07-24T10:00:00Z",
        scoring=True,
    )
    payout = RewardPayout(
        payout_date="2026-07-24",
        epoch_id="epoch-1",
        condition_id="condition-1",
        asset_id="pUSD",
        maker_evidence_ref="maker.json",
        amount=D("2"),
        payment_confirmed=True,
        payment_evidence_ref="payment.json",
    )
    ceiling = reward_validation_ceiling(
        (scoring,),
        (payout,),
        condition_id="condition-1",
        payout_asset_id="pUSD",
        maker_evidence_ref="maker.json",
    )
    assert ceiling.classification_ceiling == "promising_not_validated"
    assert ceiling.scoring_epochs == 1
    assert ceiling.payout_epochs == 1
    assert ceiling.paired_epochs == 1
    assert set(ceiling.missing_evidence) == {
        "multiple_scoring_epochs",
        "multiple_confirmed_payout_epochs",
        "multiple_paired_reward_epochs",
    }


def test_reward_accounting_rejects_cross_asset_duplicates_and_epoch_overlap() -> None:
    confirmed = RewardPayout(
        payout_date="2026-07-24",
        epoch_id="epoch-1",
        condition_id="condition-1",
        asset_id="pUSD",
        maker_evidence_ref="maker.json",
        amount=D("2"),
        payment_confirmed=True,
        payment_evidence_ref="payment-1.json",
    )
    deduplicated = apply_reward_haircuts(
        non_reward_profit=D("-1"),
        theoretical_reward=D("0"),
        condition_id="condition-1",
        reward_asset_id="pUSD",
        maker_evidence_ref="maker.json",
        confirmed_payouts=(confirmed, confirmed),
    )
    assert deduplicated[-1].confirmed_reward == D("2")
    stress = classify_reward_stress(deduplicated)
    assert stress.classification == "reward_dependent"
    assert not stress.reward_zero_positive

    cross_asset = RewardPayout(
        payout_date="2026-07-24",
        epoch_id="epoch-2",
        condition_id="condition-1",
        asset_id="NOT_USD",
        maker_evidence_ref="maker.json",
        amount=D("100"),
        payment_confirmed=True,
        payment_evidence_ref="payment-2.json",
    )
    with pytest.raises(ValueError, match="reward asset scope"):
        apply_reward_haircuts(
            non_reward_profit=D("-50"),
            theoretical_reward=D("0"),
            condition_id="condition-1",
            reward_asset_id="pUSD",
            maker_evidence_ref="maker.json",
            confirmed_payouts=(cross_asset,),
        )

    with pytest.raises(ValueError, match="overlaps a confirmed payout"):
        apply_reward_haircuts(
            non_reward_profit=D("-1"),
            theoretical_reward=D("10"),
            condition_id="condition-1",
            reward_asset_id="pUSD",
            maker_evidence_ref="maker.json",
            theoretical_epoch_ids=("epoch-1",),
            confirmed_payouts=(confirmed,),
        )


def test_reward_validation_ceiling_is_condition_scoped_and_epoch_paired() -> None:
    scoring = tuple(
        RewardScoring(
            order_id=f"order-{index}",
            auth_evidence_ref=f"auth-{index}.json",
            maker_evidence_ref="maker.json",
            condition_id="condition-1",
            asset_id="yes-token",
            epoch_id=f"epoch-{index}",
            observed_at=f"2026-07-2{index}T10:00:00Z",
            scoring=True,
        )
        for index in (1, 2)
    )
    payouts = tuple(
        RewardPayout(
            payout_date=f"2026-07-2{index}",
            epoch_id=f"epoch-{index}",
            condition_id="condition-1",
            asset_id="pUSD",
            maker_evidence_ref="maker.json",
            amount=D("1"),
            payment_confirmed=True,
            payment_evidence_ref=f"payment-{index}.json",
        )
        for index in (1, 2)
    )
    ceiling = reward_validation_ceiling(
        scoring,
        payouts,
        condition_id="condition-1",
        payout_asset_id="pUSD",
        maker_evidence_ref="maker.json",
    )
    assert ceiling.classification_ceiling == "validated_profitable"
    assert ceiling.paired_epochs == 2

    wrong_maker = (
        replace(scoring[0], maker_evidence_ref="another-maker.json"),
        scoring[1],
    )
    with pytest.raises(ValueError, match="scoring.*maker scope"):
        reward_validation_ceiling(
            wrong_maker,
            payouts,
            condition_id="condition-1",
            payout_asset_id="pUSD",
            maker_evidence_ref="maker.json",
        )

    unrelated = RewardScoring(
        order_id="unrelated",
        auth_evidence_ref="unrelated.json",
        maker_evidence_ref="maker.json",
        condition_id="condition-2",
        asset_id="yes-token",
        epoch_id="epoch-3",
        observed_at="2026-07-23T10:00:00Z",
        scoring=True,
    )
    with pytest.raises(ValueError, match="condition scope"):
        reward_validation_ceiling(
            scoring + (unrelated,),
            payouts,
            condition_id="condition-1",
            payout_asset_id="pUSD",
            maker_evidence_ref="maker.json",
        )
