"""Offline contracts for the resolved-bin weather experiment orchestrator."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
import hashlib
import json
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import src.edge_lab.weather_experiment as weather_experiment_module
from src.edge_lab.economics import FeeSchedule, taker_fee
from src.edge_lab.weather_sources import (
    AcquiredForecastRun,
    AcquisitionProvenance,
    AviationStation,
    CLOBPriceHistory,
    CLOBPricePoint,
    ForecastHour,
    RawAcquisition,
)
from src.edge_lab.weather_experiment import (
    MappingWeatherEvidenceProvider,
    NoEligibleWeatherEventsError,
    WeatherEventEvidence,
    WeatherExperimentConfig,
    run_weather_experiment,
)


D = Decimal
UTC = timezone.utc


def test_persisted_exclusion_never_contains_raw_parse_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _corpus(n_dates=8)

    def poisoned_parser(_raw: Any) -> Any:
        raise ValueError(
            "https://evil.invalid/?" + "api_key=sentinel-query"
        )

    monkeypatch.setattr(
        weather_experiment_module,
        "parse_gamma_weather_event",
        poisoned_parser,
    )

    with pytest.raises(NoEligibleWeatherEventsError) as caught:
        run_weather_experiment(
            corpus.events,
            evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
            config=_config(),
        )

    rendered = json.dumps(caught.value.artifact_payload(), sort_keys=True)
    assert "sentinel" not in rendered
    assert "evil.invalid" not in rendered
    assert "weather_rule_parse_failed" in rendered


def _raw_acquisition(source: str, received_at: datetime) -> RawAcquisition:
    return RawAcquisition(
        raw_text="{}",
        sha256=hashlib.sha256(b"{}").hexdigest(),
        provenance=AcquisitionProvenance(
            source=source,
            method="GET",
            url=f"https://example.invalid/{source}",
            request_params=MappingProxyType({}),
            requested_at=received_at,
            received_at=received_at,
            attempt=1,
            status_code=200,
            response_headers=MappingProxyType({}),
        ),
    )


def _gamma_event(
    event_id: str,
    *,
    city: str,
    local_date: date,
    winner_index: int,
    revision_rule: bool = True,
) -> dict[str, Any]:
    labels = [
        "10°C or below",
        "11°C",
        "12°C",
        "13°C",
        "14°C",
        "15°C",
        "16°C",
        "17°C",
        "18°C",
        "19°C",
        "20°C or higher",
    ]
    station_codes = {
        "NYC": "KLGA",
        "London": "EGLC",
        "Chicago": "KORD",
        "Tokyo": "RJTT",
        "Paris": "LFPG",
    }
    station = station_codes[city]
    revision = (
        "Data will be used once finalized."
        if revision_rule
        else "The displayed number resolves this market."
    )
    markets: list[dict[str, Any]] = []
    for index, label in enumerate(labels):
        yes = "1" if index == winner_index else "0"
        no = "0" if index == winner_index else "1"
        markets.append(
            {
                "id": f"{event_id}-market-{index}",
                "conditionId": f"{event_id}-condition-{index}",
                "question": f"Will the highest temperature be {label}?",
                "groupItemTitle": label,
                "outcomes": json.dumps(["Yes", "No"]),
                "outcomePrices": json.dumps([yes, no]),
                "clobTokenIds": json.dumps(
                    [f"{event_id}-yes-{index}", f"{event_id}-no-{index}"]
                ),
                "closed": True,
                "umaResolutionStatus": "resolved",
            }
        )
    return {
        "id": event_id,
        "slug": event_id,
        "title": f"Highest temperature in {city} on {local_date.isoformat()}?",
        "eventDate": local_date.isoformat(),
        "description": (
            "The resolution source is Weather Underground, specifically the "
            f"highest temperature data point for {station} Station, which can "
            "be found here: "
            "https://www.wunderground.com/history/daily/example/"
            f"{station}/date/{local_date.isoformat()}. {revision}"
        ),
        "resolutionSource": "https://www.wunderground.com",
        "closed": True,
        "markets": markets,
    }


def _forecast(
    event_id: str,
    *,
    station: AviationStation,
    local_date: date,
    timezone_name: str,
    decision_at: datetime,
    extreme_c: Decimal,
) -> AcquiredForecastRun:
    local_zone = ZoneInfo(timezone_name)
    hours = tuple(
        ForecastHour(
            local_at=datetime.combine(
                local_date,
                datetime.min.time(),
                tzinfo=local_zone,
            )
            + timedelta(hours=hour),
            utc_at=(
                datetime.combine(
                    local_date,
                    datetime.min.time(),
                    tzinfo=local_zone,
                )
                + timedelta(hours=hour)
            ).astimezone(UTC),
            temperature_2m=extreme_c - D("2") + D(hour) / D("12"),
        )
        for hour in range(24)
    )
    initialized_at = decision_at - timedelta(hours=18)
    fetched_at = decision_at + timedelta(days=30)
    return AcquiredForecastRun(
        provider="open-meteo",
        model="ecmwf_ifs025",
        initialized_at=initialized_at,
        release_delay=timedelta(hours=4),
        safety_delay=timedelta(minutes=10),
        available_at=initialized_at + timedelta(hours=4, minutes=10),
        fetched_at=fetched_at,
        requested_latitude=station.latitude,
        requested_longitude=station.longitude,
        grid_latitude=station.latitude,
        grid_longitude=station.longitude,
        timezone=timezone_name,
        forecast_days=4,
        hours=hours,
        raw_hourly=MappingProxyType({}),
        raw=MappingProxyType(
            {"hourly_units": {"temperature_2m": "°C"}, "event_id": event_id}
        ),
        acquisition=_raw_acquisition(f"forecast-{event_id}", fetched_at),
    )


def _station(event_id: str, city: str) -> AviationStation:
    coordinates = {
        "NYC": ("KLGA", "40.7794", "-73.8803"),
        "London": ("EGLC", "51.5053", "0.0553"),
        "Chicago": ("KORD", "41.9742", "-87.9073"),
        "Tokyo": ("RJTT", "35.5494", "139.7798"),
        "Paris": ("LFPG", "49.0097", "2.5479"),
    }
    code, latitude, longitude = coordinates[city]
    acquired_at = datetime(2026, 7, 24, tzinfo=UTC)
    return AviationStation(
        icao=code,
        latitude=D(latitude),
        longitude=D(longitude),
        raw=MappingProxyType({"icaoId": code}),
        acquisition=_raw_acquisition(
            f"station-{event_id}",
            acquired_at,
        ),
    )


def _history(
    token_id: str,
    *,
    decision_at: datetime,
    price: Decimal,
) -> CLOBPriceHistory:
    acquired_at = decision_at + timedelta(days=30)
    return CLOBPriceHistory(
        token_id=token_id,
        start_at=decision_at - timedelta(days=2),
        end_at=decision_at + timedelta(minutes=1),
        fidelity_minutes=1,
        points=(
            CLOBPricePoint(
                observed_at=decision_at - timedelta(minutes=1),
                price=price,
            ),
        ),
        acquisition=_raw_acquisition(f"price-{token_id}", acquired_at),
    )


@dataclass
class FixtureCorpus:
    events: list[dict[str, Any]]
    evidence: dict[str, WeatherEventEvidence]


def _corpus(
    *,
    n_dates: int = 20,
    cities: tuple[str, ...] = ("NYC", "London", "Chicago", "Tokyo", "Paris"),
) -> FixtureCorpus:
    timezones = {
        "NYC": "America/New_York",
        "London": "Europe/London",
        "Chicago": "America/Chicago",
        "Tokyo": "Asia/Tokyo",
        "Paris": "Europe/Paris",
    }
    events: list[dict[str, Any]] = []
    evidence: dict[str, WeatherEventEvidence] = {}
    start = date(2026, 1, 1)
    for day_index in range(n_dates):
        local_date = start + timedelta(days=day_index)
        for city_index, city in enumerate(cities):
            event_id = f"event-{day_index:03d}-{city_index:02d}"
            winner_index = 3 + ((day_index + city_index) % 5)
            raw = _gamma_event(
                event_id,
                city=city,
                local_date=local_date,
                winner_index=winner_index,
            )
            decision_at = datetime.combine(
                local_date - timedelta(days=2),
                datetime.min.time(),
                tzinfo=UTC,
            )
            histories = {
                f"{event_id}-yes-{index}": _history(
                    f"{event_id}-yes-{index}",
                    decision_at=decision_at,
                    # Deliberately imperfect but exhaustive L1 proxy prices.
                    price=D("0.35") if index == winner_index else D("0.065"),
                )
                for index in range(11)
            }
            station = _station(event_id, city)
            evidence[event_id] = WeatherEventEvidence(
                decision_at=decision_at,
                forecast=_forecast(
                    event_id,
                    station=station,
                    local_date=local_date,
                    timezone_name=timezones[city],
                    decision_at=decision_at,
                    extreme_c=D(13 + winner_index) + D("0.2"),
                ),
                station=station,
                price_histories=MappingProxyType(histories),
                independence_group=f"date:{local_date.isoformat()}",
            )
            events.append(raw)
    return FixtureCorpus(events=events, evidence=evidence)


def _config() -> WeatherExperimentConfig:
    return WeatherExperimentConfig(
        bias_grid=(D("-2"), D("-1"), D("0"), D("1"), D("2")),
        standard_deviation_grid=(D("0.5"), D("1"), D("2"), D("3")),
        edge_threshold_candidates=(D("0"), D("0.03"), D("0.08")),
        taker_fee=D("0.01"),
        maker_fee=D("0"),
        slippage=D("0.01"),
        model_margin=D("0.02"),
        random_seed=73,
        minimum_independent_events=100,
        minimum_explainable_fills=100,
    )


def test_orchestrator_uses_resolved_bin_labels_and_emits_four_test_baselines() -> None:
    corpus = _corpus()
    provider = MappingWeatherEvidenceProvider(corpus.evidence)

    result = run_weather_experiment(
        corpus.events,
        evidence_provider=provider,
        config=_config(),
    )

    assert result.data_level == "L1"
    assert result.evidence_limitation == (
        "public price history; not L2 or fill evidence"
    )
    assert result.label_semantics == "resolved_winner_bin_only"
    assert result.reward_assumption == D("0")
    assert result.classification == "insufficient_data"
    # One natural date is one independence group, even when five cities trade
    # that day.  Only the frozen test fold counts toward the validation gate.
    assert result.independent_settled_events == 4
    assert "fewer_than_100_independent_settled_events" in result.gate_reasons
    assert "fewer_than_100_explainable_fills" in result.gate_reasons
    assert set(result.metrics["test"]) == {
        "forecast",
        "market",
        "climatology",
        "normal",
    }
    assert provider.requested_event_ids == tuple(
        sorted(event["id"] for event in corpus.events)
    )

    assert len(result.event_predictions) == len(corpus.events)
    for prediction in result.event_predictions:
        assert prediction.observed_label_kind == "resolved_winner_bin"
        assert len(prediction.forecast_probabilities) == 11
        assert sum(prediction.forecast_probabilities.values(), D("0")) == D("1")
        assert sum(prediction.market_probabilities.values(), D("0")) == D("1")
        assert prediction.forecast_available_at <= prediction.decision_at
        # Historical exact-run retrieval is allowed later, but its acquisition
        # time is retained and never confused with original availability.
        assert prediction.forecast_acquired_at > prediction.decision_at
        assert len(prediction.price_history_sha256) == 11
        assert "observed_temperature" not in prediction.artifact_payload()

    payload = result.artifact_payload()
    assert payload["economics"]["reward_assumption"] == D("0")
    assert payload["label_semantics"] == "resolved_winner_bin_only"
    assert all(
        "observed_temperature" not in row
        for row in payload["event_predictions"]
    )


def test_date_and_city_holdouts_keep_their_grouping_keys_disjoint() -> None:
    corpus = _corpus()

    result = run_weather_experiment(
        corpus.events,
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )

    primary = result.splits.primary_date
    by_id = {row.event_id: row for row in result.event_predictions}
    date_sets = [
        {by_id[event_id].local_date for event_id in getattr(primary, split)}
        for split in ("train", "validation", "test")
    ]
    assert date_sets[0].isdisjoint(date_sets[1])
    assert date_sets[0].isdisjoint(date_sets[2])
    assert date_sets[1].isdisjoint(date_sets[2])

    city = result.splits.city_holdout
    city_sets = [
        {by_id[event_id].city for event_id in getattr(city, split)}
        for split in ("train", "validation", "test")
    ]
    assert city_sets[0].isdisjoint(city_sets[1])
    assert city_sets[0].isdisjoint(city_sets[2])
    assert city_sets[1].isdisjoint(city_sets[2])
    assert set(result.city_holdout_view.calibration.training_event_ids) == set(
        city.train
    )
    assert set(result.city_holdout_view.threshold_selection_event_ids) == set(
        city.validation
    )
    assert set(result.city_holdout_view.metrics["test"]) == {
        "forecast",
        "market",
        "climatology",
        "normal",
    }


def test_calibration_is_train_only_and_validation_selects_one_frozen_threshold() -> None:
    corpus = _corpus()

    result = run_weather_experiment(
        corpus.events,
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )

    train_ids = set(result.splits.primary_date.train)
    assert set(result.calibration.training_event_ids) == train_ids
    assert all(
        len(unit_model.grid_evaluations)
        == len(_config().bias_grid)
        * len(_config().standard_deviation_grid)
        for unit_model in result.calibration.by_unit.values()
    )
    assert set(result.calibration.training_event_ids).isdisjoint(
        result.splits.primary_date.validation
    )
    assert result.selected_threshold in _config().edge_threshold_candidates
    assert result.threshold_selected_on == "validation"
    assert tuple(
        evaluation.threshold for evaluation in result.threshold_evaluations
    ) == _config().edge_threshold_candidates
    assert set(result.threshold_selection_event_ids) == set(
        result.splits.primary_date.validation
    )
    assert result.policy_approved is False
    assert result.policy_reason_codes == (
        "no_positive_validation_threshold_objective",
        "validation_threshold_not_neighbor_stable",
    )
    assert all(
        row.edge_threshold == result.selected_threshold
        and row.threshold_origin == "validation_diagnostic_best"
        for row in result.trade_rows
        if row.split == "test"
    )
    artifact = result.artifact_payload()
    assert artifact["threshold"]["value_role"] == "diagnostic_best_only"
    assert artifact["threshold"]["policy_value"] is None
    assert artifact["policy"]["status"] == "abstain"
    assert artifact["policy"]["action"] == "no_trade"
    assert artifact["policy"]["formal_candidate_events"]["test"] == 0
    test_taker = [
        row
        for row in result.trade_rows
        if row.split == "test"
        and row.execution_style == "taker_shadow"
        and row.diagnostic_action == "diagnostic_counterfactual"
    ]
    diagnostic = artifact["policy"]["diagnostic_counterfactual"]["test"]
    assert diagnostic["taker_counterfactual_events"] == len(test_taker)
    assert diagnostic["taker_counterfactual_profit_sum"] == sum(
        (
            row.shadow_profit
            for row in test_taker
            if row.shadow_profit is not None
        ),
        D("0"),
    )
    assert diagnostic["role"] == (
        "diagnostic_counterfactual_not_policy_candidate_or_fill"
    )


def test_l1_rows_fail_closed_to_no_trade_but_retain_diagnostic_counterfactuals() -> None:
    corpus = _corpus()

    result = run_weather_experiment(
        corpus.events,
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )

    assert {row.execution_style for row in result.trade_rows} == {
        "taker_shadow",
        "maker_shadow",
    }
    assert all(row.reward == D("0") for row in result.trade_rows)
    assert all(row.data_level == "L1" for row in result.trade_rows)
    assert all(row.is_realized is False for row in result.trade_rows)
    assert all(row.explainable_fill is False for row in result.trade_rows)
    assert all(row.fill_evidence == "none_l1_shadow" for row in result.trade_rows)
    assert all(row.independence_group.startswith("date:") for row in result.trade_rows)
    assert all(row.action == "no_trade" for row in result.trade_rows)
    assert all(row.policy_approved is False for row in result.trade_rows)
    assert all(row.diagnostic_only is True for row in result.trade_rows)
    assert {
        row.diagnostic_action for row in result.trade_rows
    } <= {"diagnostic_counterfactual", "diagnostic_skip"}
    assert all(
        row.profit_semantics == "diagnostic_counterfactual_l1_not_realized"
        for row in result.trade_rows
    )
    assert any(
        row.diagnostic_action == "diagnostic_counterfactual"
        for row in result.trade_rows
    )
    assert all(
        row.taker_fee >= D("0")
        and row.maker_fee >= D("0")
        and row.slippage >= D("0")
        and row.model_margin >= D("0")
        for row in result.trade_rows
    )


def test_weather_taker_shadow_uses_price_dependent_official_fee_formula() -> None:
    corpus = _corpus()
    config = replace(
        _config(),
        taker_fee=D("0"),
        taker_fee_rate=D("0.05"),
        taker_fee_exponent=D("1"),
        taker_fee_source="official weather schedule fixture",
    )

    result = run_weather_experiment(
        corpus.events,
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=config,
    )

    schedule = FeeSchedule(rate=D("0.05"), exponent=D("1"))
    taker_rows = [
        row for row in result.trade_rows if row.execution_style == "taker_shadow"
    ]
    assert taker_rows
    assert all(
        row.taker_fee == taker_fee(D("1"), row.market_price, schedule)
        for row in taker_rows
    )
    assert result.artifact_payload()["economics"]["taker_fee_source"] == (
        "official weather schedule fixture"
    )


def test_tail_rule_forecast_and_market_failures_are_auditable_exclusions() -> None:
    corpus = _corpus(n_dates=5)
    # Keep exclusions inside the already-frozen five-date universe so this
    # test exercises exclusion reasons rather than the separate empty-fold
    # fail-closed contract.
    base_date = date(2026, 1, 1)
    valid_template = corpus.evidence[next(iter(corpus.evidence))]

    tail_id = "excluded-tail"
    corpus.events.append(
        _gamma_event(
            tail_id,
            city="NYC",
            local_date=base_date,
            winner_index=0,
        )
    )
    corpus.evidence[tail_id] = valid_template

    rule_id = "excluded-rule"
    corpus.events.append(
        _gamma_event(
            rule_id,
            city="London",
            local_date=base_date + timedelta(days=1),
            winner_index=5,
            revision_rule=False,
        )
    )
    corpus.evidence[rule_id] = valid_template

    forecast_id = "excluded-forecast"
    corpus.events.append(
        _gamma_event(
            forecast_id,
            city="Chicago",
            local_date=base_date + timedelta(days=2),
            winner_index=5,
        )
    )
    corpus.evidence[forecast_id] = WeatherEventEvidence(
        decision_at=valid_template.decision_at,
        forecast=None,
        station=valid_template.station,
        price_histories=valid_template.price_histories,
        independence_group="date:2026-03-03",
    )

    market_id = "excluded-market"
    corpus.events.append(
        _gamma_event(
            market_id,
            city="Tokyo",
            local_date=base_date + timedelta(days=3),
            winner_index=5,
        )
    )
    assert valid_template.forecast is not None
    corpus.evidence[market_id] = WeatherEventEvidence(
        decision_at=valid_template.decision_at,
        forecast=valid_template.forecast,
        station=valid_template.station,
        price_histories=MappingProxyType({}),
        independence_group="date:2026-03-04",
    )

    result = run_weather_experiment(
        corpus.events,
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )

    reasons = {
        exclusion.event_id: exclusion.reason for exclusion in result.exclusions
    }
    assert reasons[tail_id] == "winner_tail_censored"
    assert reasons[rule_id].startswith("rule_conflict:")
    assert reasons[forecast_id] == "forecast_unavailable"
    assert reasons[market_id].startswith("market_asof_missing:")
    assert any(
        "conditional on the non-tail-winner scope" in limitation
        for limitation in result.artifact_payload()["model_limitations"]
    )


def test_same_seed_and_inputs_produce_identical_artifact_payload() -> None:
    corpus = _corpus(n_dates=8)
    first = run_weather_experiment(
        corpus.events,
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )
    second = run_weather_experiment(
        list(reversed(corpus.events)),
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )

    assert first.artifact_payload() == second.artifact_payload()


def test_future_forecast_and_at_decision_price_are_rejected_without_leakage() -> None:
    corpus = _corpus(n_dates=5)
    future_id, equal_price_id, late_decision_id = sorted(corpus.evidence)[:3]

    future_evidence = corpus.evidence[future_id]
    assert future_evidence.forecast is not None
    corpus.evidence[future_id] = WeatherEventEvidence(
        decision_at=future_evidence.decision_at,
        forecast=replace(
            future_evidence.forecast,
            available_at=future_evidence.decision_at + timedelta(seconds=1),
        ),
        station=future_evidence.station,
        price_histories=future_evidence.price_histories,
        independence_group=future_evidence.independence_group,
    )

    equal_evidence = corpus.evidence[equal_price_id]
    token_id, old_history = next(iter(equal_evidence.price_histories.items()))
    equal_history = replace(
        old_history,
        points=(
            CLOBPricePoint(
                observed_at=equal_evidence.decision_at,
                price=D("0.1"),
            ),
        ),
    )
    equal_histories = dict(equal_evidence.price_histories)
    equal_histories[token_id] = equal_history
    corpus.evidence[equal_price_id] = WeatherEventEvidence(
        decision_at=equal_evidence.decision_at,
        forecast=equal_evidence.forecast,
        station=equal_evidence.station,
        price_histories=MappingProxyType(equal_histories),
        independence_group=equal_evidence.independence_group,
    )

    late_evidence = corpus.evidence[late_decision_id]
    late_event = next(
        event for event in corpus.events if event["id"] == late_decision_id
    )
    late_local_date = date.fromisoformat(str(late_event["eventDate"]))
    corpus.evidence[late_decision_id] = WeatherEventEvidence(
        decision_at=datetime.combine(
            late_local_date + timedelta(days=1),
            datetime.min.time(),
            tzinfo=UTC,
        ),
        forecast=late_evidence.forecast,
        station=late_evidence.station,
        price_histories=late_evidence.price_histories,
        independence_group=late_evidence.independence_group,
    )

    result = run_weather_experiment(
        corpus.events,
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )
    reasons = {
        exclusion.event_id: exclusion.reason for exclusion in result.exclusions
    }
    assert reasons[future_id] == "forecast_unavailable"
    assert reasons[equal_price_id].startswith("market_asof_missing:")
    assert reasons[late_decision_id] == "decision_time_leakage"


def test_duplicate_ids_are_wholly_quarantined_independent_of_input_order() -> None:
    corpus = _corpus(n_dates=5)
    duplicated = copy.deepcopy(corpus.events[0])
    markets = duplicated["markets"]
    assert isinstance(markets, list)
    for index, market in enumerate(markets):
        assert isinstance(market, dict)
        market["outcomePrices"] = json.dumps(
            ["1", "0"] if index == 7 else ["0", "1"]
        )

    first = run_weather_experiment(
        [*corpus.events, duplicated],
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )
    second = run_weather_experiment(
        [duplicated, *reversed(corpus.events)],
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )
    duplicate_id = str(duplicated["id"])
    assert duplicate_id not in {
        row.event_id for row in first.event_predictions
    }
    assert [
        row.artifact_payload()
        for row in first.exclusions
        if row.event_id == duplicate_id
    ] == [
        row.artifact_payload()
        for row in second.exclusions
        if row.event_id == duplicate_id
    ]


def test_provider_cannot_inflate_independent_event_count_and_float_costs_fail() -> None:
    corpus = _corpus()
    corpus.evidence = {
        event_id: replace(evidence, independence_group=f"unique:{event_id}")
        for event_id, evidence in corpus.evidence.items()
    }
    result = run_weather_experiment(
        corpus.events,
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )

    assert result.independent_settled_events == 4
    assert {
        row.independence_group
        for row in result.event_predictions
        if row.split == "test"
    } == {
        f"date:{row.local_date}"
        for row in result.event_predictions
        if row.split == "test"
    }
    with pytest.raises(ValueError, match="taker_fee must be a decimal"):
        replace(_config(), taker_fee=0.01)


def test_decimal_context_cannot_change_the_artifact() -> None:
    corpus = _corpus(n_dates=5)
    with localcontext() as context:
        context.prec = 12
        context.rounding = ROUND_DOWN
        low_context = run_weather_experiment(
            corpus.events,
            evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
            config=_config(),
        ).artifact_payload()
    with localcontext() as context:
        context.prec = 70
        context.rounding = ROUND_UP
        high_context = run_weather_experiment(
            corpus.events,
            evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
            config=_config(),
        ).artifact_payload()

    assert low_context == high_context


def test_test_fold_tail_cannot_move_frozen_train_or_validation_boundaries() -> None:
    corpus = _corpus(n_dates=10)
    base = run_weather_experiment(
        corpus.events,
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )
    frozen_test_dates = {
        row.local_date
        for row in base.event_predictions
        if row.split == "test"
    }
    mutated = copy.deepcopy(corpus.events)
    latest_test_date = max(frozen_test_dates)
    for event in mutated:
        if event["eventDate"] != latest_test_date:
            continue
        markets = event["markets"]
        assert isinstance(markets, list)
        for index, market in enumerate(markets):
            assert isinstance(market, dict)
            market["outcomePrices"] = json.dumps(
                ["1", "0"] if index == 0 else ["0", "1"]
            )

    changed = run_weather_experiment(
        mutated,
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )

    assert (
        changed.splits.primary_date_universe
        == base.splits.primary_date_universe
    )
    assert changed.calibration.artifact_payload() == (
        base.calibration.artifact_payload()
    )
    assert changed.selected_threshold == base.selected_threshold
    assert changed.threshold_evaluations == base.threshold_evaluations


def test_conflicting_duplicate_price_timestamp_is_order_invariant_exclusion() -> None:
    corpus = _corpus(n_dates=5)
    event_id = sorted(corpus.evidence)[0]
    event_evidence = corpus.evidence[event_id]
    token_id, history = next(iter(event_evidence.price_histories.items()))
    observed_at = history.points[0].observed_at
    conflicts = (
        CLOBPricePoint(observed_at=observed_at, price=D("0.1")),
        CLOBPricePoint(observed_at=observed_at, price=D("0.2")),
    )

    def result_with(points: tuple[CLOBPricePoint, ...]):
        histories = dict(event_evidence.price_histories)
        histories[token_id] = replace(history, points=points)
        evidence = dict(corpus.evidence)
        evidence[event_id] = replace(
            event_evidence,
            price_histories=MappingProxyType(histories),
        )
        return run_weather_experiment(
            corpus.events,
            evidence_provider=MappingWeatherEvidenceProvider(evidence),
            config=_config(),
        )

    forward = result_with(conflicts)
    reverse = result_with(tuple(reversed(conflicts)))
    expected = (
        "market_evidence_invalid:"
        "price_history_conflicting_duplicate_timestamp"
    )
    assert {
        row.reason for row in forward.exclusions if row.event_id == event_id
    } == {expected}
    assert [
        row.artifact_payload()
        for row in forward.exclusions
        if row.event_id == event_id
    ] == [
        row.artifact_payload()
        for row in reverse.exclusions
        if row.event_id == event_id
    ]


def test_duplicate_forecast_hour_and_station_coordinate_mismatch_fail_closed() -> None:
    corpus = _corpus(n_dates=5)
    duplicate_id, coordinate_id = sorted(corpus.evidence)[:2]
    duplicate_evidence = corpus.evidence[duplicate_id]
    coordinate_evidence = corpus.evidence[coordinate_id]
    assert duplicate_evidence.forecast is not None
    assert coordinate_evidence.forecast is not None
    evidence = dict(corpus.evidence)
    evidence[duplicate_id] = replace(
        duplicate_evidence,
        forecast=replace(
            duplicate_evidence.forecast,
            hours=(duplicate_evidence.forecast.hours[0],) * 24,
        ),
    )
    evidence[coordinate_id] = replace(
        coordinate_evidence,
        forecast=replace(
            coordinate_evidence.forecast,
            requested_latitude=(
                coordinate_evidence.station.latitude + D("1")
            ),
        ),
    )

    result = run_weather_experiment(
        corpus.events,
        evidence_provider=MappingWeatherEvidenceProvider(evidence),
        config=_config(),
    )
    details = {
        row.event_id: row.detail for row in result.exclusions
    }
    assert "forecast_duplicate_target_hour" in details[duplicate_id]
    assert (
        "forecast_requested_coordinates_mismatch_station"
        in details[coordinate_id]
    )


def test_unbiased_normal_baseline_uses_forecast_and_cohort_coverage_is_explicit() -> None:
    corpus = _corpus(n_dates=8)
    result = run_weather_experiment(
        corpus.events,
        evidence_provider=MappingWeatherEvidenceProvider(corpus.evidence),
        config=_config(),
    )
    first, second = next(
        (left, right)
        for left in result.event_predictions
        for right in result.event_predictions
        if left.unit == right.unit
        and left.forecast_extreme != right.forecast_extreme
    )

    assert first.normal_probabilities != second.normal_probabilities
    calibration_payload = result.calibration.artifact_payload()
    assert calibration_payload["unbiased_normal_baseline"]["bias"] == D("0")
    assert set(calibration_payload["cohort_coverage"]) == {
        "city",
        "lead_bucket",
        "metric",
        "provider_model",
        "season",
        "station",
    }
    assert all(
        diagnostic.status in {"insufficient_data", "diagnostic_fit"}
        for diagnostic in result.calibration.cohort_diagnostics
    )
