"""Offline tests for the resumable public weather acquisition runner."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import src.edge_lab.weather_experiment_runner as weather_runner_module
from src.edge_lab.weather import WeatherEventSpec
from src.edge_lab.weather_sources import (
    AcquiredForecastRun,
    AcquisitionProvenance,
    AviationStation,
    CLOBPriceHistory,
    CLOBPricePoint,
    ForecastHour,
    RawAcquisition,
)
from src.edge_lab.weather_experiment_runner import (
    ResolvedWeatherBatch,
    WeatherRunnerConfig,
    acquire_weather_experiment,
    build_argument_parser,
    verify_weather_run_manifest,
)
from src.edge_lab.weather_experiment_runner import (
    _persist_failure_acquisition,
    _sanitize_standard_cache_bytes,
)
from src.edge_lab.weather_sources import WeatherSourceError


D = Decimal
UTC = timezone.utc


def _acquisition(source: str, at: datetime, raw_text: str = "{}") -> RawAcquisition:
    return RawAcquisition(
        raw_text=raw_text,
        sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        provenance=AcquisitionProvenance(
            source=source,
            method="GET",
            url=f"https://example.invalid/{source}",
            request_params=MappingProxyType({}),
            requested_at=at,
            received_at=at,
            attempt=1,
            status_code=200,
            response_headers=MappingProxyType({"content-type": "application/json"}),
        ),
    )


def _raw_event(event_id: str, winner_index: int) -> dict[str, Any]:
    local_date = date(2026, 6, 20)
    labels = [
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
    ]
    markets = []
    for index, label in enumerate(labels):
        markets.append(
            {
                "id": f"{event_id}-market-{index}",
                "conditionId": f"{event_id}-condition-{index}",
                "question": label,
                "groupItemTitle": label,
                "outcomes": json.dumps(["Yes", "No"]),
                "outcomePrices": json.dumps(
                    ["1", "0"] if index == winner_index else ["0", "1"]
                ),
                "clobTokenIds": json.dumps(
                    [f"{event_id}-yes-{index}", f"{event_id}-no-{index}"]
                ),
                "closed": True,
                "umaResolutionStatus": "resolved",
                "startDate": "2026-06-18T00:00:00Z",
            }
        )
    return {
        "id": event_id,
        "slug": event_id,
        "title": f"Highest temperature in NYC on {local_date.isoformat()}?",
        "eventDate": local_date.isoformat(),
        "description": (
            "The resolution source is Weather Underground, specifically the "
            "highest temperature data point for LaGuardia Airport Station, "
            "which can be found here: "
            "https://www.wunderground.com/history/daily/us/ny/new-york-city/"
            f"KLGA/date/{local_date.isoformat()}. Data will be used once finalized."
        ),
        "resolutionSource": "https://www.wunderground.com",
        "closed": True,
        "markets": markets,
    }


class FakePublicSource:
    def __init__(self, *, future_forecast: bool = False) -> None:
        self.future_forecast = future_forecast
        self.calls: list[tuple[str, str]] = []
        events = [_raw_event("a-tail", 0), _raw_event("b-valid", 5)]
        at = datetime(2026, 7, 24, tzinfo=UTC)
        self.batch = ResolvedWeatherBatch(
            events=tuple(events),
            pages=(
                _acquisition(
                    "gamma_daily_temperature_resolved",
                    at,
                    json.dumps(events, sort_keys=True),
                ),
            ),
        )

    def resolved_events(
        self,
        *,
        start_date: date,
        end_date: date,
        max_candidates: int,
    ) -> ResolvedWeatherBatch:
        self.calls.append(("gamma", f"{start_date}:{end_date}:{max_candidates}"))
        return self.batch

    def station(self, station_code: str) -> AviationStation:
        self.calls.append(("station", station_code))
        at = datetime(2026, 7, 24, tzinfo=UTC)
        return AviationStation(
            icao=station_code,
            latitude=D("40.7794"),
            longitude=D("-73.8803"),
            raw=MappingProxyType({"icaoId": station_code}),
            acquisition=_acquisition("aviation_station", at),
        )

    def exact_forecast(
        self,
        *,
        spec: WeatherEventSpec,
        station: AviationStation,
        initialized_at: datetime,
        decision_at: datetime,
        release_delay: timedelta,
        model: str,
        forecast_days: int,
    ) -> AcquiredForecastRun:
        self.calls.append(("forecast", spec.event_id))
        zone = ZoneInfo(spec.timezone)
        local_start = datetime.combine(
            spec.local_date,
            datetime.min.time(),
            tzinfo=zone,
        )
        hours = tuple(
            ForecastHour(
                local_at=local_start + timedelta(hours=hour),
                utc_at=(local_start + timedelta(hours=hour)).astimezone(UTC),
                temperature_2m=D("24") + D(hour) / D("24"),
            )
            for hour in range(24)
        )
        safety = timedelta(minutes=10)
        if self.future_forecast:
            release_delay = decision_at - initialized_at + timedelta(minutes=1)
            safety = timedelta(0)
        available_at = initialized_at + release_delay + safety
        acquired_at = datetime(2026, 7, 24, tzinfo=UTC)
        return AcquiredForecastRun(
            provider="open-meteo",
            model=model,
            initialized_at=initialized_at,
            release_delay=release_delay,
            safety_delay=safety,
            available_at=available_at,
            fetched_at=acquired_at,
            requested_latitude=station.latitude,
            requested_longitude=station.longitude,
            grid_latitude=station.latitude,
            grid_longitude=station.longitude,
            timezone=spec.timezone,
            forecast_days=forecast_days,
            hours=hours,
            raw_hourly=MappingProxyType({}),
            raw=MappingProxyType(
                {"hourly_units": {"temperature_2m": "°C"}}
            ),
            acquisition=_acquisition("open_meteo_single_run", acquired_at),
        )

    def price_history(
        self,
        *,
        token_id: str,
        start_at: datetime,
        end_at: datetime,
        fidelity: int,
    ) -> CLOBPriceHistory:
        self.calls.append(("price", token_id))
        acquired_at = datetime(2026, 7, 24, tzinfo=UTC)
        return CLOBPriceHistory(
            token_id=token_id,
            start_at=start_at,
            end_at=end_at,
            fidelity_minutes=fidelity,
            points=(
                CLOBPricePoint(
                    observed_at=end_at - timedelta(minutes=2),
                    price=D("0.09"),
                ),
            ),
            acquisition=_acquisition("clob_price_history_l1", acquired_at),
        )


class ExplodingSource:
    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_"):
            return super().__getattribute__(name)
        raise AssertionError(f"cache miss unexpectedly called {name}")


def _config(output: Path) -> WeatherRunnerConfig:
    return WeatherRunnerConfig(
        output_dir=output,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 30),
        max_events=1,
        max_candidates=10,
        decision_lead=timedelta(hours=48),
        model="ecmwf_ifs025",
        release_delay=timedelta(hours=8),
    )


def test_runner_persists_only_safe_error_marker_for_poisoned_rule_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def poisoned_parser(_raw: Any) -> Any:
        raise ValueError(
            "https://evil.invalid/?" + "api_key=sentinel-query"
        )

    monkeypatch.setattr(
        weather_runner_module,
        "parse_gamma_weather_event",
        poisoned_parser,
    )

    result = acquire_weather_experiment(
        source=FakePublicSource(),
        config=_config(tmp_path / "poisoned-rule"),
    )
    rendered = json.dumps(result.artifact_payload(), sort_keys=True)

    assert "sentinel" not in rendered
    assert "evil.invalid" not in rendered
    assert "rules_or_resolution_invalid" in rendered
    assert "error_type=ValueError" in rendered


def test_runner_caches_exact_public_evidence_and_resumes_without_source_calls(
    tmp_path: Path,
) -> None:
    source = FakePublicSource()
    config = _config(tmp_path / "weather-run")
    config.output_dir.mkdir(parents=True)
    (config.output_dir / "EXPERIMENT.json").write_text(
        "stale",
        encoding="utf-8",
    )

    first = acquire_weather_experiment(source=source, config=config)

    assert first.eligible_events == 1
    assert first.classification == "insufficient_data"
    assert first.data_level == "L1"
    assert first.explainable_fills == 0
    assert any(row.reason == "winner_tail_censored" for row in first.exclusions)
    assert sum(1 for kind, _ in source.calls if kind == "price") == 11
    assert (config.output_dir / "RUN_MANIFEST.json").is_file()
    assert (config.output_dir / "EXCLUSIONS.json").is_file()
    assert not (config.output_dir / "EXPERIMENT.json").exists()
    assert list((config.output_dir / "cache" / "forecast").glob("*.json"))
    assert len(list((config.output_dir / "cache" / "price").glob("*.json"))) == 11
    verify_weather_run_manifest(config.output_dir)

    manifest_path = config.output_dir / "RUN_MANIFEST.json"
    first_manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(first_manifest_bytes)
    assert manifest["schema"] == "weather-acquisition-run/v2"
    assert manifest["source_as_of"] == manifest["completed_at"]
    assert manifest["time_basis"] == (
        "max_received_at_of_bound_public_acquisitions"
    )
    binding = manifest["reproducibility"]
    assert binding["schema"] == "weather-run-reproducibility/v1"
    assert len(binding["run_identity_sha256"]) == 64
    artifacts = {
        entry["path"]: entry
        for entry in binding["derived_artifacts"]["entries"]
    }
    for name in ("ACQUISITION_DATASET.json", "EXCLUSIONS.json"):
        raw = (config.output_dir / name).read_bytes()
        assert artifacts[name]["present"] is True
        assert artifacts[name]["size_bytes"] == len(raw)
        assert artifacts[name]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert artifacts["EXPERIMENT.json"] == {
        "path": "EXPERIMENT.json",
        "present": False,
    }
    cache_entries = binding["cache_snapshot"]["entries"]
    assert [entry["path"] for entry in cache_entries] == sorted(
        entry["path"] for entry in cache_entries
    )
    assert binding["cache_snapshot"]["file_count"] == 14
    assert {
        kind: value["file_count"]
        for kind, value in binding["cache_snapshot"]["by_kind"].items()
    } == {
        "forecast": 1,
        "gamma": 1,
        "price": 11,
        "station": 1,
    }
    assert all(len(entry["sha256"]) == 64 for entry in cache_entries)
    assert binding["code_identity"]["schema"] == "weather-runner-code/v1"
    assert {
        entry["path"]
        for entry in binding["code_identity"]["files"]
    } >= {
        "src/edge_lab/weather_experiment_runner.py",
        "src/edge_lab/weather_experiment.py",
        "src/edge_lab/economics.py",
        "src/edge_lab/experiments.py",
        "pyproject.toml",
        "uv.lock",
    }

    second = acquire_weather_experiment(
        source=ExplodingSource(),
        config=config,
    )

    assert second.artifact_payload() == first.artifact_payload()
    assert manifest_path.read_bytes() == first_manifest_bytes
    manifest_text = (config.output_dir / "RUN_MANIFEST.json").read_text()
    assert "proxy" not in manifest_text.casefold()
    assert "fill" in manifest_text.casefold()


def test_manifest_verifier_rejects_derived_and_cache_tampering(
    tmp_path: Path,
) -> None:
    derived_config = _config(tmp_path / "derived-tamper")
    acquire_weather_experiment(
        source=FakePublicSource(),
        config=derived_config,
    )
    acquisition_path = derived_config.output_dir / "ACQUISITION_DATASET.json"
    acquisition_path.write_bytes(acquisition_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="artifact checksum mismatch"):
        verify_weather_run_manifest(derived_config.output_dir)

    cache_config = _config(tmp_path / "cache-tamper")
    acquire_weather_experiment(
        source=FakePublicSource(),
        config=cache_config,
    )
    cache_path = next((cache_config.output_dir / "cache" / "price").glob("*.json"))
    cache_path.chmod(0o644)
    cache_path.write_bytes(cache_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="cache snapshot mismatch"):
        verify_weather_run_manifest(cache_config.output_dir)


def test_cache_sanitizer_filters_response_metadata_and_failure_body(
    tmp_path: Path,
) -> None:
    sensitive_header = "set-" + "cookie"
    raw = json.dumps(
        {
            "schema": "weather-public-cache/v1",
            "payload": {
                "acquisition": {
                    "provenance": {
                        "response_headers": {
                            "Content-Type": "application/json",
                            sensitive_header: "__cf" + "_bm=forbidden",
                            "X-Access-Token": "forbidden",
                        }
                    }
                }
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    sanitized, removed = _sanitize_standard_cache_bytes(raw)

    assert b"weather-public-cache/v2" in sanitized
    assert json.loads(sanitized)["payload"]["acquisition"]["provenance"][
        "response_headers"
    ] == {"content-type": "application/json"}
    assert sensitive_header in removed
    assert "x-access-token" in removed
    assert b"forbidden" not in sanitized

    acquisition = _acquisition(
        "failed",
        datetime(2026, 7, 24, tzinfo=UTC),
        raw_text="body-must-not-persist",
    )
    error = WeatherSourceError(
        "exception-must-not-persist",
        acquisition=acquisition,
        code="http_status_503",
    )
    _persist_failure_acquisition(
        tmp_path,
        event_id="event",
        stage="forecast",
        error=error,
    )
    failure_text = next((tmp_path / "cache" / "failure").glob("*.json")).read_text()
    assert "body-must-not-persist" not in failure_text
    assert "exception-must-not-persist" not in failure_text
    failure = json.loads(failure_text)
    assert failure["retention"] == {
        "exception_text_persisted": False,
        "response_body_persisted": False,
    }
    assert failure["response_body_sha256"] == acquisition.sha256


def test_runner_excludes_forecast_not_available_at_historical_decision(
    tmp_path: Path,
) -> None:
    source = FakePublicSource(future_forecast=True)
    result = acquire_weather_experiment(
        source=source,
        config=_config(tmp_path / "future"),
    )

    assert result.eligible_events == 0
    assert result.classification == "insufficient_data"
    assert any(
        row.reason == "forecast_unavailable_at_decision"
        for row in result.exclusions
    )
    assert not any(kind == "price" for kind, _ in source.calls)


def test_cli_exposes_bounded_output_and_proxy_options_without_running() -> None:
    args = build_argument_parser().parse_args(
        [
            "--max-events",
            "123",
            "--proxy",
            "socks5://127.0.0.1:1080",
            "--output",
            "/tmp/weather-public-cache",
        ]
    )

    assert args.max_events == 123
    assert args.proxy == "socks5://127.0.0.1:1080"
    assert args.output == Path("/tmp/weather-public-cache")
    with pytest.raises(SystemExit):
        build_argument_parser().parse_args(
            ["--proxy", "http://proxy.example:8080"]
        )
