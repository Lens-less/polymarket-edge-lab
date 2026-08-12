"""Machine-recomputable strategy evidence and report-core tests."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from src.edge_lab.evidence import (
    BACKTEST_RESULTS_SCHEMA,
    DEFAULT_BOOTSTRAP_SEED,
    DataLevel,
    EvidenceStatus,
    StrategyEvidence,
    StrategyKind,
    TradeEvidence,
    VerifiedEvidenceContext,
    build_backtest_results,
    build_experiment_summaries,
    build_trades_csv_rows,
    experiment_summaries_jsonl,
    trades_csv_text,
)
from src.edge_lab.data_store import canonical_record_id


D = Decimal
RAW_RECORDS: dict[str, dict[str, object]] = {}


def evidence_record_id(label: str) -> str:
    base: dict[str, object] = {
        "schema_version": "test.evidence.v1",
        "source": "test_fixture",
        "received_at": "2026-01-01T00:00:00Z",
        "event_at": "2026-01-01T00:00:00Z",
        "sequence": None,
        "payload": {"label": label},
    }
    record_id = canonical_record_id(base)
    RAW_RECORDS[record_id] = {**base, "record_id": record_id}
    return record_id


def trade(
    index: int,
    *,
    event_id: str | None = None,
    spread_pnl: Decimal = D("1"),
    forecast_pnl: Decimal = D("0"),
    adverse_selection_pnl: Decimal = D("0"),
    hedge_pnl: Decimal = D("0"),
    fees: Decimal = D("0.10"),
    rewards: Decimal = D("0"),
    theoretical_rewards: Decimal = D("0"),
    scoring_rewards: Decimal = D("0"),
    other_costs: Decimal = D("0"),
    pessimistic_cost: Decimal = D("0"),
    claimed_net_pnl: Decimal | None = D("0.90"),
    one_leg_pnl: Decimal | None = D("-0.20"),
    is_real: bool | None = True,
    is_oos: bool | None = True,
    leakage_free: bool | None = True,
    all_costs_included: bool | None = True,
    settled: bool | None = True,
    fill_explainable: bool | None = True,
    notional: Decimal = D("10"),
    capacity_notional: Decimal = D("25"),
) -> TradeEvidence:
    return TradeEvidence(
        trade_id=f"trade-{index:03d}",
        event_id=event_id or f"event-{index:03d}",
        independence_group=event_id or f"event-{index:03d}",
        market_id=f"market-{index:03d}",
        source_event_id=evidence_record_id(f"source-{index:03d}"),
        settlement_source_event_id=evidence_record_id(
            f"settlement-source-{index:03d}"
        ),
        decision_id=f"decision-{index:03d}",
        one_leg_episode_id=f"one-leg-{index:03d}",
        filled_at=f"2026-01-{(index % 28) + 1:02d}T00:00:00Z",
        settled_at=f"2026-02-{(index % 28) + 1:02d}T00:00:00Z",
        settled=settled,
        is_real=is_real,
        is_oos=is_oos,
        leakage_free=leakage_free,
        all_costs_included=all_costs_included,
        fill_explainable=fill_explainable,
        notional=notional,
        capacity_notional=capacity_notional,
        spread_pnl=spread_pnl,
        forecast_pnl=forecast_pnl,
        adverse_selection_pnl=adverse_selection_pnl,
        hedge_pnl=hedge_pnl,
        fees=fees,
        theoretical_rewards=theoretical_rewards,
        scoring_rewards=scoring_rewards,
        confirmed_reward_payout=rewards,
        reward_theoretical_source_event_id=(
            evidence_record_id(f"reward-theoretical-{index:03d}")
            if theoretical_rewards > 0
            else None
        ),
        reward_scoring_source_event_id=(
            evidence_record_id(f"reward-scoring-{index:03d}")
            if scoring_rewards > 0
            else None
        ),
        reward_payout_epoch_id=(
            f"reward-epoch-{index:03d}" if rewards > 0 else None
        ),
        reward_payout_source_event_id=(
            evidence_record_id(f"reward-payout-{index:03d}")
            if rewards > 0
            else None
        ),
        other_costs=other_costs,
        pessimistic_cost=pessimistic_cost,
        claimed_net_pnl=claimed_net_pnl,
        one_leg_pnl=one_leg_pnl,
    )


def verified_context_for(
    *,
    experiment_id: str,
    rows: tuple[TradeEvidence, ...],
    neighbors: dict[str, Decimal],
    reward_scoring_evidence_ids: tuple[str, ...],
    reward_payout_epoch_sources: dict[str, str],
) -> VerifiedEvidenceContext:
    raw_ids = {
        reference
        for row in rows
        for reference in (
            row.source_event_id,
            row.settlement_source_event_id,
            row.reward_theoretical_source_event_id,
            row.reward_scoring_source_event_id,
            row.reward_payout_source_event_id,
        )
        if reference is not None
    }
    raw_ids.update(reward_scoring_evidence_ids)
    raw_ids.update(reward_payout_epoch_sources.values())
    fee_source = evidence_record_id("verified-fee-config")
    raw_ids.add(fee_source)
    return VerifiedEvidenceContext.verify(
        capture_audit={
            "orphan_partials": [],
            "raw_without_manifest": [],
            "manifest_without_raw": [],
            "checksum_mismatches": [],
            "invalid_manifests": [],
        },
        raw_records=tuple(RAW_RECORDS[record_id] for record_id in sorted(raw_ids)),
        experiment_record={
            "experiment_id": experiment_id,
            "trade_ids": [row.trade_id for row in rows],
            "parameter_neighbor_reward_zero_pnls": neighbors,
        },
        split_manifest={
            "train": [],
            "validation": [],
            "test": sorted({row.event_id for row in rows}),
        },
        execution_trace=tuple(
            {"trade_id": row.trade_id, "decision_id": row.decision_id}
            for row in rows
        ),
        ledger_snapshot={
            "reconciled": True,
            "pessimistic_execution": True,
            "queue_bounded": True,
            "all_costs_included": True,
            "trade_net_pnls": {
                row.trade_id: row.recomputed_net_pnl for row in rows
            },
        },
        fee_config={
            "verified": True,
            "source_record_ids": [fee_source],
        },
    )


def strong_strategy(**overrides: object) -> StrategyEvidence:
    fields: dict[str, object] = {
        "strategy_id": "weather-edge",
        "experiment_id": "exp-weather-001",
        "strategy_kind": StrategyKind.PREDICTIVE,
        "data_level": DataLevel.L2,
        "initial_capital": D("1000"),
        "trades": tuple(trade(index) for index in range(100)),
        "parameter_neighbor_reward_zero_pnls": {
            "edge_threshold=-10%": D("20"),
            "edge_threshold=base": D("18"),
            "edge_threshold=+10%": D("12"),
        },
        "one_leg_cvar_limit": D("0.50"),
        "bootstrap_resamples": 5_000,
    }
    fields.update(overrides)
    if "reward_payout_epoch_sources" not in overrides:
        rows = tuple(fields["trades"])
        fields["reward_payout_epoch_sources"] = {
            row.reward_payout_epoch_id: row.reward_payout_source_event_id
            for row in rows
            if row.confirmed_reward_payout > 0
            and row.reward_payout_epoch_id is not None
            and row.reward_payout_source_event_id is not None
        }
    if "verified_context" not in overrides:
        rows = tuple(fields["trades"])
        neighbors = dict(fields["parameter_neighbor_reward_zero_pnls"])
        scoring_ids = tuple(fields.get("reward_scoring_evidence_ids", ()))
        payout_sources = dict(fields["reward_payout_epoch_sources"])
        fields["verified_context"] = verified_context_for(
            experiment_id=str(fields["experiment_id"]),
            rows=rows,
            neighbors=neighbors,
            reward_scoring_evidence_ids=scoring_ids,
            reward_payout_epoch_sources=payout_sources,
        )
    return StrategyEvidence(**fields)


def test_trade_evidence_recomputes_pnl_and_serializes_decimals_as_strings() -> None:
    row = trade(
        1,
        spread_pnl=D("2.100"),
        forecast_pnl=D("0.500"),
        adverse_selection_pnl=D("-0.300"),
        hedge_pnl=D("-0.200"),
        fees=D("0.100"),
        rewards=D("0.400"),
        theoretical_rewards=D("9.000"),
        scoring_rewards=D("3.000"),
        other_costs=D("0.050"),
        pessimistic_cost=D("0.250"),
        claimed_net_pnl=D("2.350"),
    )

    assert row.recomputed_net_pnl == D("2.350")
    assert row.pessimistic_reward_zero_pnl == D("1.700")
    assert row.pnl_recomputable
    payload = row.to_mapping()
    assert payload["spread_pnl"] == "2.100"
    assert payload["fees"] == "0.100"
    assert payload["confirmed_reward_payout"] == "0.400"
    assert payload["theoretical_rewards"] == "9.000"
    assert payload["scoring_rewards"] == "3.000"
    assert payload["claimed_net_pnl"] == "2.350"
    assert payload["pnl_recomputable"] is True


def test_financial_fields_reject_binary_floats() -> None:
    with pytest.raises(TypeError, match="notional"):
        trade(1, notional=10.0)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="settled_at cannot precede"):
        replace(trade(1), settled_at="2025-01-01T00:00:00Z")


def test_pnl_recomputation_is_independent_of_process_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 6
        rows = tuple(
            trade(
                index,
                spread_pnl=D("10000000000"),
                forecast_pnl=D("-2"),
                adverse_selection_pnl=D("-10000000000"),
                hedge_pnl=D("1"),
                fees=D("0"),
                claimed_net_pnl=D("-1"),
                one_leg_pnl=D("-1"),
            )
            for index in range(100)
        )
        result = strong_strategy(trades=rows)

    assert result.metrics.net_pnl == D("-100")
    assert result.metrics.pessimistic_reward_zero_net_pnl == D("-100")
    assert result.validation.status is EvidenceStatus.REJECTED


def test_validated_profitable_requires_every_strict_gate() -> None:
    result = strong_strategy()

    assert result.validation.status is EvidenceStatus.VALIDATED_PROFITABLE
    assert not result.validation.failure_reasons
    assert result.metrics.independent_settled_events == 100
    assert result.metrics.explainable_fills == 100
    assert result.metrics.pessimistic_reward_zero_net_pnl == D("90.00")
    assert result.metrics.bootstrap_95_lower > 0
    assert result.metrics.max_event_profit_contribution == D("0.01")
    assert set(result.validation.passed_gates) == {
        "verified_evidence_context",
        "data_level>=L2",
        "real_data",
        "out_of_sample",
        "no_leakage",
        "pessimistic_reward_zero_net_pnl>0",
        "all_costs_included",
        "independence_labels_complete",
        "settlement_status_complete",
        "settlement_provenance_consistent",
        "all_trades_settled",
        "independent_settled_events>=100",
        "explainable_fills>=100",
        "bootstrap_95_lower>0",
        "bootstrap_resamples>=5000",
        "max_event_profit_contribution<=0.20",
        "parameter_neighborhood_stable",
        "one_leg_cvar_limit<=20%_initial_capital",
        "one_leg_tail_risk",
        "pnl_recomputable",
    }


def test_unverified_or_tampered_evidence_context_cannot_validate() -> None:
    with pytest.raises(TypeError, match=r"VerifiedEvidenceContext\.verify"):
        VerifiedEvidenceContext()

    verified = strong_strategy()
    missing = replace(verified, verified_context=None)
    assert missing.validation.status is EvidenceStatus.INSUFFICIENT_DATA
    assert "verified_evidence_context: missing" in (
        missing.validation.failure_reasons
    )

    rows = list(verified.trades)
    rows[0] = replace(
        rows[0],
        spread_pnl=D("2"),
        claimed_net_pnl=D("1.90"),
    )
    tampered = replace(verified, trades=tuple(rows))
    assert tampered.validation.status is EvidenceStatus.INSUFFICIENT_DATA
    assert any(
        "ledger_pnl_mismatch" in reason
        for reason in tampered.validation.failure_reasons
    )


def test_reward_supported_profit_is_rejected_when_reward_zero_is_not_positive() -> None:
    rows = tuple(
        trade(
            index,
            spread_pnl=D("0"),
            fees=D("0.10"),
            rewards=D("0.20"),
            claimed_net_pnl=D("0.10"),
            one_leg_pnl=D("-0.01"),
        )
        for index in range(100)
    )
    result = strong_strategy(trades=rows)

    assert result.metrics.net_pnl == D("10.00")
    assert result.metrics.pessimistic_reward_zero_net_pnl == D("-10.00")
    assert result.validation.status is EvidenceStatus.REJECTED
    assert any(
        reason.startswith("pessimistic_reward_zero_net_pnl>0:")
        for reason in result.validation.failure_reasons
    )


def test_reward_strategy_is_capped_without_real_scoring_and_payout_epochs() -> None:
    result = strong_strategy(
        strategy_id="quiet-reward-harvest",
        strategy_kind=StrategyKind.REWARD,
    )

    assert result.validation.status is EvidenceStatus.PROMISING_NOT_VALIDATED
    assert set(result.validation.failure_reasons) >= {
        "reward_strategy_data_level>=L3: observed=L2",
        "real_reward_scoring_observations>=1: observed=0",
        "confirmed_reward_payout_epochs>=2: observed=0",
    }


def test_reward_strategy_requires_l3_scoring_and_two_confirmed_epochs() -> None:
    result = strong_strategy(
        strategy_id="probed-reward-harvest",
        strategy_kind=StrategyKind.REWARD,
        data_level=DataLevel.L3,
        reward_scoring_evidence_ids=(
            evidence_record_id("score-source-1"),
        ),
        reward_payout_epoch_sources={
            "epoch-1": evidence_record_id("payout-source-1"),
            "epoch-2": evidence_record_id("payout-source-2"),
        },
    )

    assert result.validation.status is EvidenceStatus.VALIDATED_PROFITABLE
    assert {
        "reward_strategy_data_level>=L3",
        "real_reward_scoring_observations>=1",
        "confirmed_reward_payout_epochs>=2",
    } <= set(result.validation.passed_gates)


def test_missing_evidence_fails_closed_as_insufficient_data() -> None:
    rows = tuple(
        trade(index, is_real=None, one_leg_pnl=None) for index in range(100)
    )
    result = strong_strategy(
        data_level=DataLevel.L1,
        trades=rows,
        one_leg_cvar_limit=None,
    )

    assert result.validation.status is EvidenceStatus.INSUFFICIENT_DATA
    assert "data_level>=L2: observed=L1" in result.validation.failure_reasons
    assert "real_data: missing" in result.validation.failure_reasons
    assert "one_leg_tail_risk: missing" in result.validation.failure_reasons


def test_correlated_markets_do_not_count_as_independent_events() -> None:
    rows = tuple(
        replace(trade(index), independence_group="same-natural-date")
        for index in range(100)
    )
    result = strong_strategy(trades=rows)

    assert result.metrics.independent_settled_events == 1
    assert result.validation.status is EvidenceStatus.INSUFFICIENT_DATA
    assert (
        "independent_settled_events>=100: observed=1"
        in result.validation.failure_reasons
    )


def test_duplicate_fill_and_settlement_provenance_cannot_fake_sample_size() -> None:
    rows = tuple(
        replace(
            trade(index),
            source_event_id=evidence_record_id("one-fill-source"),
            decision_id="one-decision",
            settlement_source_event_id=evidence_record_id(
                "one-settlement-source"
            ),
        )
        for index in range(100)
    )
    result = strong_strategy(trades=rows)

    assert result.metrics.explainable_fills == 1
    assert result.validation.status is EvidenceStatus.INSUFFICIENT_DATA
    assert "settlement_provenance_consistent: observed=false" in (
        result.validation.failure_reasons
    )


def test_missing_claimed_pnl_is_reported_as_missing_not_a_mismatch() -> None:
    rows = list(trade(index) for index in range(100))
    rows[7] = trade(7, claimed_net_pnl=None)
    result = strong_strategy(trades=tuple(rows))

    assert result.metrics.pnl_recomputable is None
    assert "pnl_recomputable: missing" in result.validation.failure_reasons
    assert result.validation.status is EvidenceStatus.INSUFFICIENT_DATA


def test_missing_independence_or_settlement_labels_fail_closed() -> None:
    rows = list(trade(index) for index in range(101))
    rows[0] = replace(rows[0], independence_group=None)
    rows[1] = replace(rows[1], settled=None)
    result = strong_strategy(trades=tuple(rows))

    assert "independence_labels_complete: missing" in (
        result.validation.failure_reasons
    )
    assert "settlement_status_complete: missing" in (
        result.validation.failure_reasons
    )
    assert result.validation.status is EvidenceStatus.INSUFFICIENT_DATA


def test_unsettled_theoretical_profit_cannot_validate_a_settled_sample() -> None:
    rows = list(trade(index) for index in range(100))
    rows.append(
        trade(
            999,
            theoretical_rewards=D("1000000000"),
            spread_pnl=D("5"),
            fees=D("0"),
            claimed_net_pnl=D("5"),
        )
    )
    # Unsettled rows cannot carry a settlement timestamp or settlement proof.
    rows[-1] = replace(
        rows[-1],
        settled=False,
        settled_at=None,
        settlement_source_event_id=None,
    )
    result = strong_strategy(trades=tuple(rows))

    assert result.validation.status is EvidenceStatus.INSUFFICIENT_DATA
    assert result.metrics.net_pnl == D("90.00")
    assert result.metrics.final_capital == D("1090.00")
    assert result.metrics.unsettled_marked_pnl == D("5")
    assert result.metrics.marked_final_capital == D("1095.00")
    assert "all_trades_settled: observed=false" in (
        result.validation.failure_reasons
    )


def test_bootstrap_is_invariant_to_event_label_assignment() -> None:
    first_rows = [
        trade(
            index,
            spread_pnl=D("-24") if index == 4 else D("1"),
            fees=D("0"),
            claimed_net_pnl=D("-24") if index == 4 else D("1"),
        )
        for index in range(100)
    ]
    second_rows = [
        trade(
            index,
            spread_pnl=D("-24") if index == 99 else D("1"),
            fees=D("0"),
            claimed_net_pnl=D("-24") if index == 99 else D("1"),
        )
        for index in range(100)
    ]

    first = strong_strategy(trades=tuple(first_rows))
    second = strong_strategy(trades=tuple(second_rows))
    assert first.metrics.bootstrap_95_lower == second.metrics.bootstrap_95_lower
    assert first.metrics.bootstrap_95_upper == second.metrics.bootstrap_95_upper


def test_arbitrarily_large_one_leg_limit_is_not_an_acceptable_tail_gate() -> None:
    result = strong_strategy(one_leg_cvar_limit=D("1000000"))

    assert result.validation.status is EvidenceStatus.REJECTED
    assert "one_leg_cvar_limit<=20%_initial_capital: observed=false" in (
        result.validation.failure_reasons
    )


def test_opposite_one_leg_episodes_cannot_net_inside_an_event_group() -> None:
    rows: list[TradeEvidence] = []
    for index in range(100):
        event_id = f"paired-event-{index:03d}"
        negative = trade(index * 2, event_id=event_id, one_leg_pnl=D("-1000"))
        positive = trade(index * 2 + 1, event_id=event_id, one_leg_pnl=D("1000"))
        positive = replace(
            positive,
            settlement_source_event_id=negative.settlement_source_event_id,
        )
        rows.extend((negative, positive))

    result = strong_strategy(
        trades=tuple(rows),
        one_leg_cvar_limit=D("0"),
    )
    assert result.metrics.one_leg_empirical_cvar_95 == D("1000")
    assert result.validation.status is EvidenceStatus.REJECTED
    assert "one_leg_tail_risk: observed=false" in (
        result.validation.failure_reasons
    )


def test_parameter_neighborhood_failure_is_promising_not_validated() -> None:
    result = strong_strategy(
        parameter_neighbor_reward_zero_pnls={
            "edge_threshold=-10%": D("2"),
            "edge_threshold=base": D("0"),
            "edge_threshold=+10%": D("0"),
        }
    )

    assert result.validation.status is EvidenceStatus.PROMISING_NOT_VALIDATED
    assert any(
        reason.startswith("parameter_neighborhood_stable:")
        for reason in result.validation.failure_reasons
    )


def test_pnl_mismatch_is_reported_and_rejected() -> None:
    rows = list(trade(index) for index in range(100))
    rows[7] = trade(7, claimed_net_pnl=D("999"))
    result = strong_strategy(trades=tuple(rows))

    assert result.metrics.pnl_recomputable is False
    assert result.validation.status is EvidenceStatus.REJECTED
    assert any(
        reason.startswith("pnl_recomputable:")
        for reason in result.validation.failure_reasons
    )


def test_metrics_are_recomputed_from_sorted_trade_economics() -> None:
    rows = (
        trade(
            3,
            event_id="event-c",
            spread_pnl=D("-4"),
            fees=D("0"),
            claimed_net_pnl=D("-4"),
            notional=D("30"),
            capacity_notional=D("60"),
            one_leg_pnl=D("-8"),
        ),
        trade(
            1,
            event_id="event-a",
            spread_pnl=D("5"),
            fees=D("1"),
            rewards=D("1"),
            claimed_net_pnl=D("5"),
            notional=D("10"),
            capacity_notional=D("40"),
            one_leg_pnl=D("-2"),
        ),
        trade(
            2,
            event_id="event-b",
            spread_pnl=D("2"),
            forecast_pnl=D("1"),
            adverse_selection_pnl=D("-1"),
            hedge_pnl=D("-0.5"),
            fees=D("0.5"),
            claimed_net_pnl=D("1"),
            notional=D("20"),
            capacity_notional=D("50"),
            one_leg_pnl=D("-4"),
        ),
    )
    result = strong_strategy(
        strategy_id="metrics",
        experiment_id="exp-metrics",
        strategy_kind=StrategyKind.STRUCTURAL,
        data_level=DataLevel.L2,
        initial_capital=D("100"),
        trades=rows,
        parameter_neighbor_reward_zero_pnls={
            "low": D("1"),
            "high": D("1"),
        },
        one_leg_cvar_limit=D("10"),
        bootstrap_resamples=100,
    )
    metrics = result.metrics

    assert metrics.initial_capital == D("100")
    assert metrics.final_capital == D("102")
    assert metrics.net_pnl == D("2")
    assert metrics.return_on_initial_capital == D("0.02")
    assert metrics.max_drawdown == D("4")
    assert metrics.max_drawdown_fraction == D("4") / D("106")
    assert metrics.hit_rate == D("2") / D("3")
    assert metrics.turnover == D("60")
    assert metrics.capacity == D("40")
    assert metrics.one_leg_empirical_cvar_95 == D("8")
    assert metrics.decomposition == {
        "spread_pnl": D("3"),
        "forecast_pnl": D("1"),
        "adverse_selection_pnl": D("-1"),
        "hedge_pnl": D("-0.5"),
        "fees": D("1.5"),
        "theoretical_rewards": D("0"),
        "scoring_rewards": D("0"),
        "confirmed_reward_payout": D("1"),
        "other_costs": D("0"),
        "pessimistic_cost": D("0"),
    }


def test_bootstrap_is_event_level_and_fixed_seed() -> None:
    first = strong_strategy()
    second = strong_strategy()

    assert first.bootstrap_seed == DEFAULT_BOOTSTRAP_SEED
    assert first.metrics.bootstrap_95_lower == second.metrics.bootstrap_95_lower
    assert first.metrics.bootstrap_95_upper == second.metrics.bootstrap_95_upper


def test_strict_validation_requires_at_least_5000_bootstrap_resamples() -> None:
    result = strong_strategy(bootstrap_resamples=100)

    assert result.validation.status is EvidenceStatus.INSUFFICIENT_DATA
    assert "bootstrap_resamples>=5000: observed=100" in (
        result.validation.failure_reasons
    )


def test_report_structures_are_deterministically_sorted_and_decimal_safe() -> None:
    zulu = strong_strategy(strategy_id="zulu", experiment_id="exp-2")
    alpha = strong_strategy(strategy_id="alpha", experiment_id="exp-1")

    results = build_backtest_results((zulu, alpha))
    summaries = build_experiment_summaries((zulu, alpha))
    trade_rows = build_trades_csv_rows((zulu, alpha))

    assert results["schema_version"] == BACKTEST_RESULTS_SCHEMA
    assert [row["strategy_id"] for row in results["strategies"]] == [
        "alpha",
        "zulu",
    ]
    assert [row["strategy_id"] for row in summaries] == ["alpha", "zulu"]
    assert trade_rows[0]["strategy_id"] == "alpha"
    assert trade_rows[-1]["strategy_id"] == "zulu"
    assert trade_rows[0]["notional"] == "10"
    assert isinstance(results["strategies"][0]["metrics"]["net_pnl"], str)

    encoded = json.dumps(results, sort_keys=True, allow_nan=False)
    assert "Decimal" not in encoded
    csv_text = trades_csv_text((zulu, alpha))
    parsed = list(csv.DictReader(io.StringIO(csv_text)))
    assert parsed == list(trade_rows)
    assert csv_text == trades_csv_text((alpha, zulu))
    jsonl_rows = [
        json.loads(line)
        for line in experiment_summaries_jsonl((zulu, alpha)).splitlines()
    ]
    assert jsonl_rows == list(summaries)


def test_duplicate_strategy_or_trade_identity_is_fail_closed() -> None:
    duplicate_trade = trade(1)
    with pytest.raises(ValueError, match="duplicate trade_id"):
        strong_strategy(
            trades=(duplicate_trade, duplicate_trade),
            verified_context=None,
        )

    evidence = strong_strategy()
    with pytest.raises(ValueError, match="duplicate strategy evidence key"):
        build_backtest_results((evidence, evidence))
