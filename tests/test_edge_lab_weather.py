"""Behavioral tests for the weather probability tracer bullet."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.edge_lab.weather import (
    CalibrationRow,
    ForecastVintage,
    FutureDataError,
    GaussianResidualCalibration,
    HourlyTemperature,
    WeatherDataError,
    WeatherQuotePolicy,
    brier_score,
    city_weather_split,
    chronological_weather_split,
    gaussian_bin_probabilities,
    log_loss,
    local_day_extreme,
    parse_gamma_weather_event,
    reliability_table,
    select_latest_vintage,
    weather_quote_decision,
)


D = Decimal


def gamma_market(label: str, index: int) -> dict[str, object]:
    return {
        "id": f"market-{index}",
        "conditionId": f"condition-{index}",
        "question": f"Will the highest temperature be {label}?",
        "groupItemTitle": label,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0", "1"]),
        "clobTokenIds": json.dumps([f"yes-{index}", f"no-{index}"]),
        "closed": True,
        "umaResolutionStatus": "resolved",
    }


def gamma_event(
    *,
    city: str,
    event_date: str,
    labels: list[str],
    description: str,
    title: str | None = None,
) -> dict[str, object]:
    return {
        "id": f"event-{city.lower().replace(' ', '-')}-{event_date}",
        "slug": f"highest-temperature-in-{city.lower().replace(' ', '-')}",
        "title": title or f"Highest temperature in {city} on May 26?",
        "eventDate": event_date,
        "description": description,
        "resolutionSource": None,
        "negRisk": True,
        "markets": [gamma_market(label, index) for index, label in enumerate(labels)],
    }


def test_parse_nyc_gamma_event_normalizes_json_arrays_and_two_fahrenheit_bins() -> None:
    event = gamma_event(
        city="NYC",
        event_date="2026-05-26",
        labels=[
            "65°F or below",
            "66-67°F",
            "68-69°F",
            "70-71°F",
            "72-73°F",
            "74-75°F",
            "76-77°F",
            "78-79°F",
            "80-81°F",
            "82-83°F",
            "84°F or higher",
        ],
        description=(
            "The resolution source is Weather Underground, specifically the "
            "highest temperature data point for LaGuardia Airport Station, "
            "which can be found here: "
            "https://www.wunderground.com/history/daily/us/ny/new-york-city/"
            "KLGA/date/2026-5-26. Revisions made after the first data point "
            "for the following day is published will not be considered."
        ),
    )
    event["resolutionSource"] = "https://www.wunderground.com"

    spec = parse_gamma_weather_event(event)

    assert spec.city == "NYC"
    assert spec.station_name == "LaGuardia Airport"
    assert spec.station_code == "KLGA"
    assert spec.local_date == date(2026, 5, 26)
    assert spec.timezone == "America/New_York"
    assert spec.metric == "high"
    assert spec.unit == "F"
    assert spec.revision_cutoff == "first_next_day_publication"
    assert "/KLGA/date/" in spec.resolution_source
    assert len(spec.rules_hash) == 64
    assert len(spec.bins) == 11
    assert spec.bins[0].tail_censored
    assert spec.bins[-1].tail_censored
    assert spec.bins[1].yes_token_id == "yes-1"
    assert spec.bins[1].no_token_id == "no-1"
    assert spec.bins[1].yes_outcome_price == D("0")
    assert spec.bins[1].no_outcome_price == D("1")
    assert spec.bins[1].lower_bound == D("65.5")
    assert spec.bins[1].upper_bound == D("67.5")
    assert spec.map_observation(D("73.4")).label == "72-73°F"


def test_current_wunderground_rule_wording_and_terminal_station_url_parse() -> None:
    event = gamma_event(
        city="Miami",
        event_date="2026-07-23",
        title="Lowest temperature in Miami on July 23?",
        labels=[
            "55°F or below",
            "56-57°F",
            "58-59°F",
            "60-61°F",
            "62-63°F",
            "64-65°F",
            "66-67°F",
            "68-69°F",
            "70-71°F",
            "72-73°F",
            "74°F or higher",
        ],
        description=(
            "This market will resolve to the temperature range that contains "
            "the lowest temperature recorded at the Miami Intl Airport "
            "Station in degrees Fahrenheit on 23 Jul '26. The resolution "
            "source is Wunderground, specifically the lowest temperature "
            "recorded for all times on this day for the Miami Intl Airport "
            "Station, available here: "
            "https://www.wunderground.com/history/daily/us/fl/miami/KMIA. "
            "This market can not resolve until the first data point for the "
            "following date has been published."
        ),
    )
    event["resolutionSource"] = (
        "https://www.wunderground.com/history/daily/us/fl/miami/KMIA"
    )

    spec = parse_gamma_weather_event(event)

    assert spec.station_name == "Miami Intl Airport"
    assert spec.station_code == "KMIA"
    assert spec.revision_cutoff == "first_next_day_publication"
    assert spec.quarantine_reasons == ()


def test_whole_celsius_point_bins_and_tails_are_exhaustive() -> None:
    event = gamma_event(
        city="London",
        event_date="2026-05-26",
        labels=[
            "20°C or below",
            "21°C",
            "22°C",
            "23°C",
            "24°C",
            "25°C",
            "26°C",
            "27°C",
            "28°C",
            "29°C",
            "30°C or higher",
        ],
        description=(
            "The resolution source is Weather Underground, specifically the "
            "highest temperature data point for London City Airport Station, "
            "which can be found here: "
            "https://www.wunderground.com/history/daily/gb/london/EGLC/date/"
            "2026-5-26. Data will be used once finalized."
        ),
    )

    spec = parse_gamma_weather_event(event)

    assert spec.unit == "C"
    assert spec.timezone == "Europe/London"
    assert spec.revision_cutoff == "source_finalized"
    assert spec.bins[0].upper_bound == D("20.5")
    assert spec.bins[1].lower_bound == D("20.5")
    assert spec.bins[-1].lower_bound == D("29.5")
    assert spec.map_observation(D("25")).label == "25°C"


def test_lowest_fahrenheit_single_point_bins_are_supported() -> None:
    event = gamma_event(
        city="Chicago",
        event_date="2026-05-26",
        title="Lowest temperature in Chicago on May 26?",
        labels=[
            "20°F or below",
            "21°F",
            "22°F",
            "23°F",
            "24°F",
            "25°F",
            "26°F",
            "27°F",
            "28°F",
            "29°F",
            "30°F or higher",
        ],
        description=(
            "The resolution source is Weather Underground, specifically the "
            "lowest temperature data point for O'Hare International Airport "
            "Station, which can be found here: "
            "https://www.wunderground.com/history/daily/us/il/chicago/KORD/"
            "date/2026-5-26. Data will be used once finalized."
        ),
    )

    spec = parse_gamma_weather_event(event)

    assert spec.metric == "low"
    assert spec.unit == "F"
    assert spec.timezone == "America/Chicago"
    assert spec.station_code == "KORD"
    assert spec.map_observation(D("24.2")).label == "24°F"


def test_temperature_bins_with_a_gap_or_overlap_are_rejected() -> None:
    event = gamma_event(
        city="London",
        event_date="2026-05-26",
        labels=[
            "20°C or below",
            "22°C",
            "22°C",
            "23°C",
            "24°C",
            "25°C",
            "26°C",
            "27°C",
            "28°C",
            "29°C",
            "30°C or higher",
        ],
        description=(
            "The resolution source is Weather Underground, specifically the "
            "highest temperature data point for London City Airport Station, "
            "which can be found here: "
            "https://www.wunderground.com/history/daily/gb/london/EGLC/date/"
            "2026-5-26. Data will be used once finalized."
        ),
    )

    with pytest.raises(WeatherDataError, match="overlap or leave a gap"):
        parse_gamma_weather_event(event)


def test_hko_decimal_observation_uses_one_degree_intervals_not_rounding() -> None:
    event = gamma_event(
        city="Hong Kong",
        event_date="2026-07-23",
        labels=[
            "29°C or below",
            "30°C",
            "31°C",
            "32°C",
            "33°C",
            "34°C",
            "35°C",
            "36°C",
            "37°C",
            "38°C",
            "39°C or higher",
        ],
        description=(
            "The resolution source for this market is the Hong Kong "
            "Observatory Daily Extract, found here: "
            "https://www.weather.gov.hk/cis/dailyExtract/"
            "dailyExtract_202607.xml. Revisions after the initial publication "
            "will not be considered."
        ),
    )

    spec = parse_gamma_weather_event(event)

    assert spec.station_name == "Hong Kong Observatory"
    assert spec.station_code == "HKO"
    assert spec.timezone == "Asia/Hong_Kong"
    assert spec.revision_cutoff == "initial_publication"
    assert spec.bins[4].lower_bound == D("33")
    assert spec.bins[4].upper_bound == D("34")
    assert spec.map_observation(D("33.4")).label == "33°C"


def test_station_name_and_source_code_conflict_is_quarantined() -> None:
    event = gamma_event(
        city="Karachi",
        event_date="2026-05-26",
        labels=[
            "30°C or below",
            "31°C",
            "32°C",
            "33°C",
            "34°C",
            "35°C",
            "36°C",
            "37°C",
            "38°C",
            "39°C",
            "40°C or higher",
        ],
        description=(
            "The resolution source is Weather Underground, specifically the "
            "highest temperature data point for Masroor Airbase Station, "
            "which can be found here: "
            "https://www.wunderground.com/history/daily/pk/karachi/OPKC/date/"
            "2026-5-26. Data will be used once finalized."
        ),
    )

    spec = parse_gamma_weather_event(event)

    assert spec.station_name == "Masroor Airbase"
    assert spec.station_code == "OPKC"
    assert spec.quarantined
    assert spec.quarantine_reasons == ("station_source_mismatch",)


def test_unknown_revision_cutoff_and_missing_station_code_are_quarantined() -> None:
    event = gamma_event(
        city="London",
        event_date="2026-05-26",
        labels=[
            "20°C or below",
            "21°C",
            "22°C",
            "23°C",
            "24°C",
            "25°C",
            "26°C",
            "27°C",
            "28°C",
            "29°C",
            "30°C or higher",
        ],
        description=(
            "The resolution source is Weather Underground, specifically the "
            "highest temperature data point for London City Airport Station, "
            "which can be found here: "
            "https://www.wunderground.com/weather/gb/london."
        ),
    )

    spec = parse_gamma_weather_event(event)

    assert set(spec.quarantine_reasons) == {
        "revision_cutoff_unspecified",
        "station_code_missing",
    }


def test_duplicate_gamma_market_identity_is_rejected_before_probability_mapping() -> None:
    event = gamma_event(
        city="London",
        event_date="2026-05-26",
        labels=[
            "20°C or below",
            "21°C",
            "22°C",
            "23°C",
            "24°C",
            "25°C",
            "26°C",
            "27°C",
            "28°C",
            "29°C",
            "30°C or higher",
        ],
        description=(
            "The resolution source is Weather Underground, specifically the "
            "highest temperature data point for London City Airport Station, "
            "which can be found here: "
            "https://www.wunderground.com/history/daily/gb/london/EGLC/date/"
            "2026-5-26. Data will be used once finalized."
        ),
    )
    markets = event["markets"]
    assert isinstance(markets, list)
    markets[1]["id"] = markets[0]["id"]

    with pytest.raises(WeatherDataError, match="unique"):
        parse_gamma_weather_event(event)


def test_forecast_vintage_rejects_runs_not_yet_available_or_captured() -> None:
    old = ForecastVintage(
        provider="NOAA",
        model="GFS",
        run_initialized_at=datetime(2026, 5, 25, 0, tzinfo=timezone.utc),
        available_at=datetime(2026, 5, 25, 4, 30, tzinfo=timezone.utc),
        captured_at=datetime(2026, 5, 25, 4, 40, tzinfo=timezone.utc),
    )
    future = ForecastVintage(
        provider="NOAA",
        model="GFS",
        run_initialized_at=datetime(2026, 5, 26, 8, tzinfo=timezone.utc),
        available_at=datetime(2026, 5, 26, 12, 10, tzinfo=timezone.utc),
        captured_at=datetime(2026, 5, 26, 12, 12, tzinfo=timezone.utc),
    )
    decision_at = datetime(2026, 5, 26, 12, 5, tzinfo=timezone.utc)

    with pytest.raises(FutureDataError, match="available"):
        future.require_usable_at(decision_at)

    assert select_latest_vintage((old, future), decision_at=decision_at) is old
    assert future.require_usable_at(
        datetime(2026, 5, 26, 12, 12, tzinfo=timezone.utc)
    ) is future


def test_hourly_extreme_uses_the_city_local_day_not_the_utc_day() -> None:
    points = (
        HourlyTemperature(
            valid_at=datetime(2026, 5, 26, 3, 0, tzinfo=timezone.utc),
            temperature=D("100"),
        ),
        HourlyTemperature(
            valid_at=datetime(2026, 5, 26, 4, 0, tzinfo=timezone.utc),
            temperature=D("70"),
        ),
        HourlyTemperature(
            valid_at=datetime(2026, 5, 27, 3, 59, tzinfo=timezone.utc),
            temperature=D("82"),
        ),
        HourlyTemperature(
            valid_at=datetime(2026, 5, 27, 4, 0, tzinfo=timezone.utc),
            temperature=D("200"),
        ),
    )

    assert local_day_extreme(
        points,
        local_date=date(2026, 5, 26),
        timezone_name="America/New_York",
        metric="high",
        minimum_points=2,
    ) == D("82")
    assert local_day_extreme(
        points,
        local_date=date(2026, 5, 26),
        timezone_name="America/New_York",
        metric="low",
        minimum_points=2,
    ) == D("70")
    with pytest.raises(WeatherDataError, match="coverage"):
        local_day_extreme(
            points[:2],
            local_date=date(2026, 5, 26),
            timezone_name="America/New_York",
            metric="high",
        )


def test_residual_calibration_is_train_only_and_distribution_sums_to_one() -> None:
    rows = (
        CalibrationRow(forecast=D("70"), observed=D("69"), split="train"),
        CalibrationRow(forecast=D("74"), observed=D("74"), split="train"),
        CalibrationRow(forecast=D("78"), observed=D("79"), split="train"),
    )
    calibration = GaussianResidualCalibration.fit(rows)
    assert calibration.bias == D("0")
    assert calibration.standard_deviation == D("1")
    assert calibration.n_train == 3

    with pytest.raises(WeatherDataError, match="standard_deviation"):
        GaussianResidualCalibration(
            bias=D("0"),
            standard_deviation=D("0"),
            n_train=3,
        )

    with pytest.raises(ValueError, match="train"):
        GaussianResidualCalibration.fit(
            rows
            + (
                CalibrationRow(
                    forecast=D("80"),
                    observed=D("100"),
                    split="validation",
                ),
            )
        )

    event = gamma_event(
        city="NYC",
        event_date="2026-05-26",
        labels=[
            "65°F or below",
            "66-67°F",
            "68-69°F",
            "70-71°F",
            "72-73°F",
            "74-75°F",
            "76-77°F",
            "78-79°F",
            "80-81°F",
            "82-83°F",
            "84°F or higher",
        ],
        description=(
            "The resolution source is Weather Underground, specifically the "
            "highest temperature data point for LaGuardia Airport Station, "
            "which can be found here: "
            "https://www.wunderground.com/history/daily/us/ny/new-york-city/"
            "KLGA/date/2026-5-26. Data will be used once finalized."
        ),
    )
    spec = parse_gamma_weather_event(event)

    probabilities = gaussian_bin_probabilities(
        spec,
        forecast=D("74"),
        calibration=calibration,
        model_uncertainty=D("0.5"),
    )

    assert len(probabilities) == 11
    assert all(isinstance(value, Decimal) for value in probabilities.values())
    assert all(value >= D("0") for value in probabilities.values())
    assert sum(probabilities.values(), D("0")) == D("1")
    assert max(probabilities, key=probabilities.get) == "market-5"


def test_probability_scores_and_reliability_use_event_level_outcomes() -> None:
    probabilities = {"a": D("0.7"), "b": D("0.2"), "c": D("0.1")}

    assert brier_score(probabilities, observed="a") == D("0.14")
    assert abs(log_loss(probabilities, observed="a") - D("0.35667494393873245")) < D(
        "1e-15"
    )

    table = reliability_table(
        (
            {"yes": D("0.8"), "no": D("0.2")},
            {"warm": D("0.6"), "cold": D("0.4")},
        ),
        observed=("yes", "cold"),
        n_bins=2,
    )

    assert [(row.count, row.mean_probability, row.empirical_frequency) for row in table] == [
        (2, D("0.3"), D("0.5")),
        (2, D("0.7"), D("0.5")),
    ]


def test_weather_splits_connect_to_experiment_event_splits_without_leakage() -> None:
    labels = [
        "20°C or below",
        "21°C",
        "22°C",
        "23°C",
        "24°C",
        "25°C",
        "26°C",
        "27°C",
        "28°C",
        "29°C",
        "30°C or higher",
    ]
    specs = tuple(
        parse_gamma_weather_event(
            gamma_event(
                city=city,
                event_date=event_date,
                labels=labels,
                title=f"Highest temperature in {city} on May {day}?",
                description=(
                    "The resolution source is Weather Underground, specifically "
                    f"the highest temperature data point for {city} Airport "
                    "Station, which can be found here: "
                    f"https://www.wunderground.com/history/daily/x/{code}/date/"
                    f"2026-5-{day}. Data will be used once finalized."
                ),
            ),
            timezone_name="UTC",
        )
        for city, code, day, event_date in (
            ("Alpha", "AAAA", 1, "2026-05-01"),
            ("Beta", "BBBB", 1, "2026-05-01"),
            ("Alpha", "AAAA", 2, "2026-05-02"),
            ("Gamma", "CCCC", 2, "2026-05-02"),
            ("Beta", "BBBB", 3, "2026-05-03"),
            ("Gamma", "CCCC", 3, "2026-05-03"),
        )
    )

    chronological = chronological_weather_split(
        specs,
        train_fraction=D("0.5"),
        validation_fraction=D("0.25"),
    )
    assert chronological.train == (
        "event-alpha-2026-05-01",
        "event-beta-2026-05-01",
    )
    assert chronological.validation == (
        "event-alpha-2026-05-02",
        "event-gamma-2026-05-02",
    )
    assert chronological.test == (
        "event-beta-2026-05-03",
        "event-gamma-2026-05-03",
    )

    by_city = city_weather_split(
        specs,
        train_fraction=D("0.34"),
        validation_fraction=D("0.33"),
    )
    assignments = {
        event_id: split
        for split, event_ids in by_city.as_dict().items()
        for event_id in event_ids
    }
    assert assignments["event-alpha-2026-05-01"] == assignments[
        "event-alpha-2026-05-02"
    ]
    assert assignments["event-beta-2026-05-01"] == assignments[
        "event-beta-2026-05-03"
    ]
    assert assignments["event-gamma-2026-05-02"] == assignments[
        "event-gamma-2026-05-03"
    ]


def test_selective_quote_has_zero_reward_and_conservative_cancel_rules() -> None:
    policy = WeatherQuotePolicy(
        minimum_net_edge=D("0.03"),
        transaction_cost=D("0.01"),
        model_uncertainty=D("0.02"),
        reprice_probability_delta=D("0.05"),
        cancel_before_settlement=timedelta(hours=1),
    )
    now = datetime(2026, 5, 26, 12, tzinfo=timezone.utc)
    settlement_at = datetime(2026, 5, 27, 12, tzinfo=timezone.utc)

    quote = weather_quote_decision(
        fair_probability=D("0.60"),
        market_price=D("0.52"),
        policy=policy,
        now=now,
        settlement_at=settlement_at,
    )
    assert quote.action == "quote"
    assert quote.net_edge == D("0.05")
    assert quote.reward_assumption == D("0")

    with pytest.raises(ValueError, match="reward"):
        weather_quote_decision(
            fair_probability=D("0.60"),
            market_price=D("0.52"),
            policy=policy,
            now=now,
            settlement_at=settlement_at,
            reward_assumption=D("0.01"),
        )

    updated = weather_quote_decision(
        fair_probability=D("0.60"),
        market_price=D("0.52"),
        policy=policy,
        now=now,
        settlement_at=settlement_at,
        existing_quote=True,
        previous_fair_probability=D("0.54"),
    )
    assert (updated.action, updated.reason) == ("cancel", "forecast_update")

    unknown_previous_fair = weather_quote_decision(
        fair_probability=D("0.60"),
        market_price=D("0.52"),
        policy=policy,
        now=now,
        settlement_at=settlement_at,
        existing_quote=True,
    )
    assert (unknown_previous_fair.action, unknown_previous_fair.reason) == (
        "cancel",
        "previous_fair_unknown",
    )

    near_settlement = weather_quote_decision(
        fair_probability=D("0.60"),
        market_price=D("0.52"),
        policy=policy,
        now=settlement_at - timedelta(minutes=30),
        settlement_at=settlement_at,
        existing_quote=True,
    )
    assert (near_settlement.action, near_settlement.reason) == (
        "cancel",
        "near_settlement",
    )
