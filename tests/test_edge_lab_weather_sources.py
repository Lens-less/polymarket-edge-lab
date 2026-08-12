"""Contract tests for public, leakage-resistant weather acquisitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any

import pytest
import requests

from src.edge_lab.weather_sources import (
    ForecastNotAvailableError,
    PriceNotAvailableError,
    WeatherSourceError,
    WeatherSources,
    validate_loopback_proxy_url,
)


@dataclass
class StubResponse:
    content: bytes
    status_code: int = 200
    headers: dict[str, str] | None = None
    reason: str = "OK"

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {"content-type": "application/json"}


class StubSession:
    def __init__(self, *responses: StubResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}
        self.auth = None
        self.cookies = requests.cookies.RequestsCookieJar()
        self.proxies: dict[str, str] = {}
        self.trust_env = True

    def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeTime:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _gamma_event(
    event_id: int,
    *,
    end_date: str = "2026-07-26T23:59:00Z",
    closed: bool = False,
    marker: float | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "id": str(event_id),
        "title": f"Highest temperature event {event_id}",
        "endDate": end_date,
        "closed": closed,
        "markets": [],
    }
    if marker is not None:
        value["marker"] = marker
    return value


def test_gamma_daily_temperature_discovery_pages_deduplicates_and_marks_stale_open() -> None:
    first_items = [_gamma_event(index) for index in range(100)]
    first_items[0] = _gamma_event(
        0,
        end_date="2026-07-23T23:59:00Z",
        closed=False,
        marker=0.125,
    )
    first = StubResponse(json.dumps(first_items).encode())
    second = StubResponse(
        json.dumps([_gamma_event(99), _gamma_event(100)]).encode()
    )
    session = StubSession(first, second)
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    sources = WeatherSources(
        session=session,
        clock=lambda: now.timestamp(),
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )

    result = sources.discover_daily_temperature_events(
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 30),
        limit=500,
    )

    assert len(result.events) == 101
    assert result.events[0]["marker"] == Decimal("0.125")
    assert result.duplicate_event_ids == ("99",)
    assert result.stale_open_event_ids == ("0",)
    assert result.truncated_at_offset_cap is False
    assert result.window_semantics == "gamma_end_date_discovery_filter_only"
    assert len(result.pages) == 2
    assert result.pages[0].raw_text == first.content.decode()
    assert len(result.pages[0].sha256) == 64
    assert result.pages[0].provenance.method == "GET"
    expected_common = {
        "tag_id": 103040,
        "end_date_min": "2026-07-20T00:00:00Z",
        "end_date_max": "2026-07-30T23:59:59Z",
        "order": "id",
        "ascending": True,
        "closed": False,
        "limit": 100,
    }
    assert session.calls[0]["params"] == {**expected_common, "offset": 0}
    assert session.calls[1]["params"] == {**expected_common, "offset": 100}
    assert dict(result.pages[0].provenance.request_params) == {
        **expected_common,
        "offset": 0,
    }
    assert all(call["method"] == "GET" for call in session.calls)


def test_gamma_offset_pagination_never_requests_beyond_2000() -> None:
    full_page = json.dumps([_gamma_event(index) for index in range(100)]).encode()
    session = StubSession(
        *(StubResponse(full_page) for _ in range(21))
    )
    sources = WeatherSources(
        session=session,
        clock=lambda: datetime(2026, 7, 24, tzinfo=timezone.utc).timestamp(),
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )

    result = sources.discover_daily_temperature_events(
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 30),
    )

    assert result.truncated_at_offset_cap is True
    assert [call["params"]["offset"] for call in session.calls] == list(
        range(0, 2001, 100)
    )
    assert max(call["params"]["offset"] for call in session.calls) == 2000


def test_http_retry_backoff_timeout_and_rate_limit_are_injected() -> None:
    session = StubSession(
        requests.ConnectionError("offline"),
        StubResponse(b'{"error":"busy"}', status_code=503),
        StubResponse(b"[]"),
        StubResponse(b"[]"),
    )
    fake_time = FakeTime()
    sources = WeatherSources(
        session=session,
        timeout=7.5,
        retries=2,
        clock=fake_time.clock,
        sleeper=fake_time.sleep,
        random_fn=lambda: 0.4,
        backoff_base_seconds=1.0,
        jitter_seconds=0.5,
        min_interval_seconds=1.0,
    )

    first = sources.discover_daily_temperature_events(
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 20),
    )
    sources.discover_daily_temperature_events(
        start_date=date(2026, 7, 21),
        end_date=date(2026, 7, 21),
    )

    assert first.pages[0].provenance.attempt == 3
    assert fake_time.sleeps == pytest.approx([1.2, 2.2, 1.0])
    assert [call["timeout"] for call in session.calls] == [7.5] * 4


def test_public_transport_disables_ambient_state_filters_headers_and_clears_cookies() -> None:
    class CookieSettingSession(StubSession):
        def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
            response = super().request(method, url, **kwargs)
            self.cookies.set("server-cookie", "must-not-survive")
            return response

    sensitive_name = "set-" + "cookie"
    session = CookieSettingSession(
        StubResponse(
            b"[]",
            headers={
                "Content-Type": "application/json",
                "Date": "Fri, 24 Jul 2026 10:43:06 GMT",
                "ETag": (
                    "eyJabcdefghijk"
                    + ".abcdefghijk"
                    + ".abcdefghijk"
                ),
                sensitive_name: "__cf" + "_bm=must-not-persist",
                "Authorization": "must-not-persist",
                "X-Access-Token": "must-not-persist",
                "X-Secret": "must-not-persist",
                "Server": "not-needed",
            },
        )
    )
    sources = WeatherSources(
        session=session,
        clock=lambda: 100.0,
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )

    result = sources.discover_daily_temperature_events(
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 20),
    )

    assert session.trust_env is False
    assert not session.cookies
    assert session.calls[0]["allow_redirects"] is False
    assert dict(result.pages[0].provenance.response_headers) == {
        "content-type": "application/json",
        "date": "Fri, 24 Jul 2026 10:43:06 GMT",
    }


def test_public_transport_rejects_preloaded_cookie_auth_and_non_loopback_proxy() -> None:
    cookie_session = StubSession(StubResponse(b"[]"))
    cookie_session.cookies.set("preloaded", "forbidden")
    with pytest.raises(WeatherSourceError, match="cookie state"):
        WeatherSources(session=cookie_session)

    auth_session = StubSession(StubResponse(b"[]"))
    auth_session.headers["X-Access-Token"] = "forbidden"
    with pytest.raises(WeatherSourceError, match="authentication material"):
        WeatherSources(session=auth_session)

    api_key_session = StubSession(StubResponse(b"[]"))
    api_key_session.headers["X-Api-Key"] = "forbidden"
    with pytest.raises(WeatherSourceError, match="authentication material"):
        WeatherSources(session=api_key_session)

    proxy_session = StubSession(StubResponse(b"[]"))
    proxy_session.proxies["https"] = "http://proxy.example:8080"
    with pytest.raises(WeatherSourceError, match="credential-free loopback"):
        WeatherSources(session=proxy_session)

    loopback = validate_loopback_proxy_url("http://127.0.0.1:7897")
    loopback_session = StubSession(StubResponse(b"[]"))
    loopback_session.proxies.update({"http": loopback, "https": loopback})
    sources = WeatherSources(session=loopback_session)
    assert sources._session.proxies["https"] == loopback


def test_proxy_request_failure_does_not_copy_exception_secret_text() -> None:
    secret = "user:do-not-persist"
    session = StubSession(
        requests.exceptions.ProxyError(
            f"cannot reach http://{secret}@127.0.0.1:7897"
        )
    )
    sources = WeatherSources(
        session=session,
        retries=0,
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )

    with pytest.raises(WeatherSourceError) as caught:
        sources.discover_daily_temperature_events(
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 20),
        )

    assert secret not in str(caught.value)
    assert "127.0.0.1" not in str(caught.value)
    assert caught.value.code == "public_request_failed"
    assert isinstance(
        caught.value.__cause__,
        requests.exceptions.ProxyError,
    )


def test_unexpected_transport_failure_is_sanitized_and_clears_cookies() -> None:
    class UnexpectedFailureSession(StubSession):
        def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
            self.cookies.set("session", "sentinel-cookie")
            raise RuntimeError(
                "https://evil.invalid/?" + "api_key=sentinel-query"
            )

    session = UnexpectedFailureSession()
    sources = WeatherSources(
        session=session,
        retries=0,
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )

    with pytest.raises(WeatherSourceError) as caught:
        sources.discover_daily_temperature_events(
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 20),
        )

    assert caught.value.code == "unexpected_transport_failure"
    assert "sentinel" not in str(caught.value)
    assert not session.cookies


def test_sensitive_request_parameter_is_rejected_before_dispatch() -> None:
    session = StubSession(StubResponse(b"[]"))
    sources = WeatherSources(
        session=session,
        retries=0,
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )

    with pytest.raises(WeatherSourceError) as caught:
        sources._get(
            "https://gamma-api.polymarket.com/events",
            source="gamma",
            params={"apiToken": "sentinel-token"},
        )

    assert caught.value.code == "public_request_metadata_forbidden"
    assert session.calls == []
    assert "sentinel" not in str(caught.value)


def test_default_aviation_weather_rate_limit_is_one_request_per_minute() -> None:
    body = b'[{"icaoId":"KLGA","lat":40.7794,"lon":-73.8803}]'
    session = StubSession(StubResponse(body), StubResponse(body))
    fake_time = FakeTime()
    sources = WeatherSources(
        session=session,
        clock=fake_time.clock,
        sleeper=fake_time.sleep,
    )

    sources.aviation_station_info("KLGA")
    sources.aviation_station_info("KLGA")

    assert fake_time.sleeps == [60.0]


def test_aviation_weather_station_info_keeps_icao_coordinates_and_raw_response() -> None:
    body = (
        b'[{"icaoId":"KLGA","iataId":"LGA","name":"LaGuardia Airport",'
        b'"lat":40.7794,"lon":-73.8803,"elev":3.4,"unknown":"kept"}]'
    )
    session = StubSession(StubResponse(body))
    sources = WeatherSources(
        session=session,
        clock=lambda: 100.0,
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )

    station = sources.aviation_station_info("klga")

    assert station.icao == "KLGA"
    assert station.latitude == Decimal("40.7794")
    assert station.longitude == Decimal("-73.8803")
    assert station.raw["unknown"] == "kept"
    assert station.raw["elev"] == Decimal("3.4")
    assert station.acquisition.raw_text == body.decode()
    assert session.calls[0]["url"].endswith("/api/data/stationinfo")
    assert session.calls[0]["params"] == {"ids": "KLGA", "format": "json"}


def test_response_validation_failure_retains_raw_acquisition_evidence() -> None:
    body = b'[{"icaoId":"KLGA","lat":"not-a-number","lon":-73.8803}]'
    sources = WeatherSources(
        session=StubSession(StubResponse(body)),
        clock=lambda: 100.0,
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )

    with pytest.raises(WeatherSourceError) as caught:
        sources.aviation_station_info("KLGA")

    assert caught.value.acquisition is not None
    assert caught.value.acquisition.raw_text == body.decode()


def test_single_run_uses_exact_run_local_hours_and_configured_availability_guard() -> None:
    body = (
        b'{"latitude":40.78,"longitude":-73.88,'
        b'"timezone":"America/New_York","utc_offset_seconds":-14400,'
        b'"hourly_units":{"time":"iso8601","temperature_2m":"C"},'
        b'"hourly":{"time":["2026-07-23T20:00","2026-07-23T21:00"],'
        b'"temperature_2m":[27.25,26.75]},"generationtime_ms":0.125}'
    )
    initialized = datetime(2026, 7, 24, 0, tzinfo=timezone.utc)
    release_delay = timedelta(hours=4)
    too_early_session = StubSession(StubResponse(body))
    too_early = WeatherSources(
        session=too_early_session,
        clock=lambda: datetime(2026, 7, 24, 6, tzinfo=timezone.utc).timestamp(),
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )

    with pytest.raises(
        ForecastNotAvailableError,
        match=r"2026-07-24T04:10:00\+00:00",
    ):
        too_early.open_meteo_single_run(
            provider="ECMWF",
            model="ecmwf_ifs025",
            initialized_at=initialized,
            release_delay=release_delay,
            decision_at=datetime(2026, 7, 24, 4, 9, tzinfo=timezone.utc),
            latitude=Decimal("40.7794"),
            longitude=Decimal("-73.8803"),
            forecast_days=7,
        )
    assert too_early_session.calls == []

    session = StubSession(StubResponse(body))
    sources = WeatherSources(
        session=session,
        clock=lambda: datetime(2026, 7, 24, 6, tzinfo=timezone.utc).timestamp(),
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )
    vintage = sources.open_meteo_single_run(
        provider="ECMWF",
        model="ecmwf_ifs025",
        initialized_at=initialized,
        release_delay=release_delay,
        decision_at=datetime(2026, 7, 24, 4, 10, tzinfo=timezone.utc),
        latitude=Decimal("40.7794"),
        longitude=Decimal("-73.8803"),
        forecast_days=7,
    )

    assert vintage.provider == "ECMWF"
    assert vintage.model == "ecmwf_ifs025"
    assert vintage.data_kind == "single_run_vintage"
    assert vintage.exact_run is True
    assert vintage.decision_vintage is True
    assert vintage.initialized_at == initialized
    assert vintage.release_delay == timedelta(hours=4)
    assert vintage.safety_delay == timedelta(minutes=10)
    assert vintage.available_at == datetime(
        2026, 7, 24, 4, 10, tzinfo=timezone.utc
    )
    assert vintage.fetched_at == datetime(
        2026, 7, 24, 6, tzinfo=timezone.utc
    )
    assert vintage.timezone == "America/New_York"
    assert vintage.hours[0].local_at.hour == 20
    assert vintage.hours[0].local_at.utcoffset() == timedelta(hours=-4)
    assert vintage.hours[0].utc_at == initialized
    assert vintage.hours[0].temperature_2m == Decimal("27.25")
    assert vintage.raw_hourly["temperature_2m"][1] == Decimal("26.75")
    assert vintage.raw["generationtime_ms"] == Decimal("0.125")
    assert vintage.core_provenance.run_initialized_at == initialized
    assert vintage.core_provenance.available_at == vintage.available_at
    assert vintage.core_provenance.captured_at == vintage.fetched_at
    assert session.calls[0]["url"] == (
        "https://single-runs-api.open-meteo.com/v1/forecast"
    )
    assert session.calls[0]["params"] == {
        "latitude": "40.7794",
        "longitude": "-73.8803",
        "hourly": "temperature_2m",
        "models": "ecmwf_ifs025",
        "run": "2026-07-24T00:00",
        "forecast_days": 7,
        "timezone": "auto",
    }


@pytest.mark.parametrize(
    "model",
    ["best_match", "ecmwf_ifs_seamless", "era5", "gfs_global,ecmwf_ifs025"],
)
def test_single_run_rejects_stitched_reanalysis_or_multi_model_vintage(
    model: str,
) -> None:
    session = StubSession()
    sources = WeatherSources(
        session=session,
        clock=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc).timestamp(),
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )

    with pytest.raises(ValueError, match="exact operational model"):
        sources.open_meteo_single_run(
            provider="explicit-provider",
            model=model,
            initialized_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
            release_delay=timedelta(hours=4),
            decision_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
            latitude=Decimal("0"),
            longitude=Decimal("0"),
            forecast_days=1,
        )

    assert session.calls == []


def test_clob_price_history_as_of_is_strictly_before_decision_and_l1_only() -> None:
    body = (
        b'{"history":[{"t":300,"p":0.55},{"t":100,"p":0.40},'
        b'{"t":200,"p":"0.45"}]}'
    )
    session = StubSession(StubResponse(body))
    sources = WeatherSources(
        session=session,
        clock=lambda: 500.0,
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )

    history = sources.clob_price_history(
        token_id="weather/yes",
        start_at=datetime.fromtimestamp(100, tz=timezone.utc),
        end_at=datetime.fromtimestamp(400, tz=timezone.utc),
        fidelity=5,
    )
    observed = history.as_of(
        datetime.fromtimestamp(300, tz=timezone.utc)
    )

    assert observed.price == Decimal("0.45")
    assert observed.observed_at == datetime.fromtimestamp(
        200, tz=timezone.utc
    )
    assert observed.decision_at == datetime.fromtimestamp(
        300, tz=timezone.utc
    )
    assert observed.data_level == "L1"
    assert history.data_level == "L1"
    assert "not L2 or fill" in history.evidence_limitation
    assert history.acquisition.raw_text == body.decode()
    assert session.calls[0]["url"] == "https://clob.polymarket.com/prices-history"
    assert session.calls[0]["params"] == {
        "market": "weather/yes",
        "startTs": 100,
        "endTs": 400,
        "fidelity": 5,
    }

    with pytest.raises(PriceNotAvailableError, match="strictly before"):
        history.as_of(datetime.fromtimestamp(100, tz=timezone.utc))


def test_previous_runs_adapter_is_explicitly_fixed_lead_not_a_stitched_vintage() -> None:
    body = (
        b'{"latitude":24.90,"longitude":67.13,"timezone":"Asia/Karachi",'
        b'"hourly":{"time":["2026-07-26T14:00"],'
        b'"temperature_2m_previous_day3":[35.5]}}'
    )
    session = StubSession(StubResponse(body))
    sources = WeatherSources(
        session=session,
        clock=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc).timestamp(),
        sleeper=lambda _: None,
        min_interval_seconds=0,
    )

    series = sources.open_meteo_previous_runs_fixed_lead(
        provider="NOAA",
        model="gfs_global",
        lead_days=3,
        release_delay=timedelta(hours=5),
        latitude=Decimal("24.9065"),
        longitude=Decimal("67.1608"),
        start_date=date(2026, 7, 26),
        end_date=date(2026, 7, 26),
    )

    assert series.data_kind == "fixed_lead_previous_runs"
    assert series.exact_run is False
    assert series.decision_vintage is False
    assert series.lead_days == 3
    assert series.hours[0].local_at.hour == 14
    assert series.hours[0].local_at.utcoffset() == timedelta(hours=5)
    assert series.hours[0].temperature_2m == Decimal("35.5")
    assert series.hours[0].lead_reference_at == datetime(
        2026, 7, 23, 9, tzinfo=timezone.utc
    )
    assert series.hours[0].conservative_available_at == datetime(
        2026, 7, 23, 14, 10, tzinfo=timezone.utc
    )
    assert session.calls[0]["url"] == (
        "https://previous-runs-api.open-meteo.com/v1/forecast"
    )
    assert session.calls[0]["params"] == {
        "latitude": "24.9065",
        "longitude": "67.1608",
        "hourly": "temperature_2m_previous_day3",
        "models": "gfs_global",
        "start_date": "2026-07-26",
        "end_date": "2026-07-26",
        "timezone": "auto",
    }
