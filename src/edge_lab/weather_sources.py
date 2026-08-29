"""Public-only source adapters for leakage-resistant weather research.

This module is intentionally an acquisition boundary, not a forecasting
strategy.  Every response keeps its original UTF-8 text and SHA-256 digest,
while decoded JSON numbers use :class:`decimal.Decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import ipaddress
import json
import random
import re
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from .network_safety import (
    high_confidence_secret_findings,
    is_sensitive_header_name,
    is_sensitive_parameter_name,
)


GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
AVIATION_STATION_URL = "https://aviationweather.gov/api/data/stationinfo"
OPEN_METEO_SINGLE_RUNS_URL = (
    "https://single-runs-api.open-meteo.com/v1/forecast"
)
OPEN_METEO_PREVIOUS_RUNS_URL = (
    "https://previous-runs-api.open-meteo.com/v1/forecast"
)
CLOB_PRICE_HISTORY_URL = "https://clob.polymarket.com/prices-history"
DAILY_TEMPERATURE_TAG_ID = 103040
FORECAST_AVAILABILITY_SAFETY = timedelta(minutes=10)
USER_AGENT = "polymarket-edge-lab/0.1.1 (public-read-only)"
SAFE_RESPONSE_HEADER_NAMES = frozenset(
    {
        "age",
        "cache-control",
        "content-encoding",
        "content-length",
        "content-type",
        "date",
        "etag",
        "expires",
        "last-modified",
        "retry-after",
    }
)
DEFAULT_HOST_MIN_INTERVAL_SECONDS = MappingProxyType(
    {
        "aviationweather.gov": 60.0,
        "gamma-api.polymarket.com": 0.1,
        "single-runs-api.open-meteo.com": 0.1,
        "previous-runs-api.open-meteo.com": 0.1,
        "clob.polymarket.com": 0.1,
    }
)


class WeatherSourceError(RuntimeError):
    """A public weather-research source failed or was unsafe to interpret."""

    def __init__(
        self,
        message: str,
        *,
        acquisition: Optional["RawAcquisition"] = None,
        code: str = "weather_source_error",
    ) -> None:
        super().__init__(message)
        self.acquisition = acquisition
        self.code = code


class ForecastNotAvailableError(WeatherSourceError):
    """A forecast run was not yet publicly available at decision time."""


class PriceNotAvailableError(WeatherSourceError):
    """No public L1 price existed before the requested decision time."""


class ResponseLike(Protocol):
    content: bytes
    status_code: int
    headers: Mapping[str, Any]


class SessionLike(Protocol):
    headers: Mapping[str, Any]
    auth: Any
    cookies: Any
    proxies: Mapping[str, Any]
    trust_env: bool

    def request(
        self, method: str, url: str, **kwargs: Any
    ) -> ResponseLike: ...


@dataclass(frozen=True)
class AcquisitionProvenance:
    source: str
    method: str
    url: str
    request_params: Mapping[str, Any]
    requested_at: datetime
    received_at: datetime
    attempt: int
    status_code: int
    response_headers: Mapping[str, str]


@dataclass(frozen=True)
class RawAcquisition:
    raw_text: str
    sha256: str
    provenance: AcquisitionProvenance


@dataclass(frozen=True)
class GammaWeatherDiscovery:
    events: tuple[Mapping[str, Any], ...]
    pages: tuple[RawAcquisition, ...]
    duplicate_event_ids: tuple[str, ...]
    stale_open_event_ids: tuple[str, ...]
    truncated_at_offset_cap: bool
    window_semantics: str = "gamma_end_date_discovery_filter_only"


@dataclass(frozen=True)
class AviationStation:
    icao: str
    latitude: Decimal
    longitude: Decimal
    raw: Mapping[str, Any]
    acquisition: RawAcquisition


@dataclass(frozen=True)
class ForecastHour:
    local_at: datetime
    utc_at: datetime
    temperature_2m: Optional[Decimal]


@dataclass(frozen=True)
class AcquiredForecastRun:
    """Exact-run payload plus acquisition and release-time provenance."""

    provider: str
    model: str
    initialized_at: datetime
    release_delay: timedelta
    safety_delay: timedelta
    available_at: datetime
    fetched_at: datetime
    requested_latitude: Decimal
    requested_longitude: Decimal
    grid_latitude: Decimal
    grid_longitude: Decimal
    timezone: str
    forecast_days: int
    hours: tuple[ForecastHour, ...]
    raw_hourly: Mapping[str, Any]
    raw: Mapping[str, Any]
    acquisition: RawAcquisition
    data_kind: str = "single_run_vintage"
    exact_run: bool = True
    decision_vintage: bool = True

    @property
    def core_provenance(self) -> Any:
        """Adapt acquisition metadata to the weather model's decision seam."""

        from .weather import ForecastVintage as CoreForecastVintage

        return CoreForecastVintage(
            provider=self.provider,
            model=self.model,
            run_initialized_at=self.initialized_at,
            available_at=self.available_at,
            captured_at=self.fetched_at,
        )


@dataclass(frozen=True)
class FixedLeadForecastHour:
    local_at: datetime
    utc_at: datetime
    temperature_2m: Optional[Decimal]
    lead_reference_at: datetime
    conservative_available_at: datetime


@dataclass(frozen=True)
class FixedLeadForecastSeries:
    provider: str
    model: str
    lead_days: int
    release_delay: timedelta
    fetched_at: datetime
    timezone: str
    hours: tuple[FixedLeadForecastHour, ...]
    raw_hourly: Mapping[str, Any]
    raw: Mapping[str, Any]
    acquisition: RawAcquisition
    data_kind: str = "fixed_lead_previous_runs"
    exact_run: bool = False
    decision_vintage: bool = False


@dataclass(frozen=True)
class CLOBPricePoint:
    observed_at: datetime
    price: Decimal


@dataclass(frozen=True)
class PriceAsOf:
    token_id: str
    observed_at: datetime
    decision_at: datetime
    price: Decimal
    data_level: str = "L1"


@dataclass(frozen=True)
class CLOBPriceHistory:
    token_id: str
    start_at: datetime
    end_at: datetime
    fidelity_minutes: int
    points: tuple[CLOBPricePoint, ...]
    acquisition: RawAcquisition
    data_level: str = "L1"
    evidence_limitation: str = "public price history; not L2 or fill evidence"

    def as_of(self, decision_at: datetime) -> PriceAsOf:
        """Select the latest point strictly earlier than ``decision_at``."""

        decision = _aware_utc(decision_at, field="decision_at")
        eligible = [
            point for point in self.points if point.observed_at < decision
        ]
        if not eligible:
            raise PriceNotAvailableError(
                "no L1 price exists strictly before decision_at"
            )
        point = max(eligible, key=lambda item: item.observed_at)
        return PriceAsOf(
            token_id=self.token_id,
            observed_at=point.observed_at,
            decision_at=decision,
            price=point.price,
        )


def safe_public_response_headers(
    headers: Mapping[str, Any],
) -> Mapping[str, str]:
    """Return only low-risk response metadata under normalized names."""

    if not isinstance(headers, Mapping):
        return MappingProxyType({})
    safe: dict[str, str] = {}
    for name, value in headers.items():
        normalized = str(name).strip().casefold()
        if normalized not in SAFE_RESPONSE_HEADER_NAMES:
            continue
        text = str(value)
        if (
            len(text) > 1024
            or "\r" in text
            or "\n" in text
            or high_confidence_secret_findings(text)
        ):
            continue
        safe[normalized] = text
    return MappingProxyType(dict(sorted(safe.items())))


def validate_loopback_proxy_url(value: str) -> str:
    """Accept an explicit credential-free loopback HTTP/SOCKS proxy only."""

    text = str(value).strip()
    parsed = urlsplit(text)
    if parsed.scheme.casefold() not in {
        "http",
        "https",
        "socks5",
        "socks5h",
    }:
        raise ValueError("proxy must use HTTP(S) or SOCKS5")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("proxy credentials are not allowed")
    host = (parsed.hostname or "").casefold()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback or parsed.port is None:
        raise ValueError("proxy must be an explicit loopback host and port")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("proxy URL cannot contain path, query, or fragment data")
    return text


def _sensitive_header_name(value: Any) -> bool:
    return is_sensitive_header_name(value)


def _utc_from_epoch(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _parse_json(acquisition: RawAcquisition) -> Any:
    try:
        return json.loads(acquisition.raw_text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise WeatherSourceError(
            f"{acquisition.provenance.source} returned invalid JSON: {exc}",
            acquisition=acquisition,
        ) from exc


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            return None
        # A date-only fallback is conservatively stale only after that entire
        # UTC calendar day.  It is never interpreted as the target local day.
        return datetime.combine(
            parsed_date + timedelta(days=1),
            datetime.min.time(),
            timezone.utc,
        )
    try:
        parsed = datetime.fromisoformat(
            f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _decimal(
    value: Any,
    *,
    field: str,
    acquisition: Optional[RawAcquisition] = None,
) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise WeatherSourceError(
            f"{field} must be a decimal number", acquisition=acquisition
        )
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise WeatherSourceError(
            f"{field} must be a decimal number", acquisition=acquisition
        ) from exc
    if not result.is_finite():
        raise WeatherSourceError(
            f"{field} must be finite", acquisition=acquisition
        )
    return result


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(timezone.utc)


def _exact_operational_model(value: str) -> str:
    model = str(value).strip()
    normalized = model.casefold().replace("-", "_").replace(" ", "_")
    forbidden = (
        not model
        or "," in model
        or "seamless" in normalized
        or normalized in {"auto", "default", "best_match"}
        or normalized.startswith("era5")
    )
    if forbidden:
        raise ValueError(
            "model must identify one exact operational model; stitched, "
            "reanalysis, default, and multi-model inputs are not vintages"
        )
    return model


class WeatherSources:
    """Synchronous GET-only adapters with injected I/O and time."""

    def __init__(
        self,
        *,
        session: Optional[SessionLike] = None,
        fetch: Optional[Callable[..., ResponseLike]] = None,
        timeout: float = 30.0,
        retries: int = 2,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        random_fn: Callable[[], float] = random.random,
        backoff_base_seconds: float = 0.5,
        jitter_seconds: float = 0.1,
        min_interval_seconds: Optional[float] = None,
    ) -> None:
        if session is not None and fetch is not None:
            raise ValueError("provide session or fetch, not both")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if retries < 0:
            raise ValueError("retries cannot be negative")
        if backoff_base_seconds < 0 or jitter_seconds < 0:
            raise ValueError("backoff and jitter cannot be negative")
        if min_interval_seconds is not None and min_interval_seconds < 0:
            raise ValueError("min_interval_seconds cannot be negative")
        actual_session = session or requests.Session()
        try:
            actual_session.trust_env = False
        except Exception as exc:
            raise WeatherSourceError(
                "public source transport cannot disable ambient environment",
                code="ambient_environment_disable_failed",
            ) from exc
        if getattr(actual_session, "trust_env", None) is not False:
            raise WeatherSourceError(
                "public source transport did not disable ambient environment",
                code="ambient_environment_disable_failed",
            )
        self._session = actual_session
        self._fetch = fetch or actual_session.request
        self._assert_public_session()
        self.timeout = timeout
        self.retries = retries
        self.clock = clock
        self.sleeper = sleeper
        self.random_fn = random_fn
        self.backoff_base_seconds = backoff_base_seconds
        self.jitter_seconds = jitter_seconds
        self.min_interval_seconds = min_interval_seconds
        self._last_request_at: dict[str, float] = {}

    def _assert_public_session(self) -> None:
        if getattr(self._session, "trust_env", None) is not False:
            raise WeatherSourceError(
                "public source transport enabled ambient environment",
                code="ambient_environment_enabled",
            )
        headers = getattr(self._session, "headers", {}) or {}
        for name in headers:
            if _sensitive_header_name(name):
                raise WeatherSourceError(
                    "public source session contains authentication material",
                    code="session_authentication_material",
                )
        if getattr(self._session, "auth", None):
            raise WeatherSourceError(
                "public source session contains authentication credentials",
                code="session_authentication_material",
            )
        cookies = getattr(self._session, "cookies", None)
        if cookies is not None and bool(cookies):
            raise WeatherSourceError(
                "public source session contains cookie state",
                code="session_cookie_state",
            )
        proxies = getattr(self._session, "proxies", {}) or {}
        if not isinstance(proxies, Mapping):
            raise WeatherSourceError(
                "public source proxy configuration is invalid",
                code="proxy_configuration_invalid",
            )
        for scheme, proxy_url in proxies.items():
            if str(scheme).casefold() not in {"http", "https"}:
                raise WeatherSourceError(
                    "public source proxy scheme is invalid",
                    code="proxy_configuration_invalid",
                )
            try:
                validate_loopback_proxy_url(str(proxy_url))
            except ValueError as exc:
                raise WeatherSourceError(
                    "public source proxy must be credential-free loopback",
                    code="proxy_configuration_invalid",
                ) from exc

    def _discard_response_cookie_state(self) -> None:
        cookies = getattr(self._session, "cookies", None)
        if cookies is None or not bool(cookies):
            return
        clear = getattr(cookies, "clear", None)
        if not callable(clear):
            raise WeatherSourceError(
                "public source response cookie state cannot be cleared",
                code="response_cookie_clear_failed",
            )
        clear()
        if bool(cookies):
            raise WeatherSourceError(
                "public source response cookie state survived clearing",
                code="response_cookie_clear_failed",
            )

    def _rate_limit(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        interval = (
            self.min_interval_seconds
            if self.min_interval_seconds is not None
            else DEFAULT_HOST_MIN_INTERVAL_SECONDS.get(host, 0.1)
        )
        now = self.clock()
        previous = self._last_request_at.get(host)
        if previous is not None:
            wait = interval - (now - previous)
            if wait > 0:
                self.sleeper(wait)
                now = self.clock()
        self._last_request_at[host] = now

    def _get(
        self,
        url: str,
        *,
        source: str,
        params: Mapping[str, Any],
    ) -> RawAcquisition:
        parsed_url = urlsplit(url)
        allowed_urls = {
            GAMMA_EVENTS_URL,
            AVIATION_STATION_URL,
            OPEN_METEO_SINGLE_RUNS_URL,
            OPEN_METEO_PREVIOUS_RUNS_URL,
            CLOB_PRICE_HISTORY_URL,
        }
        if (
            url not in allowed_urls
            or parsed_url.scheme != "https"
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
            or any(
                is_sensitive_parameter_name(key)
                for key, _ in parse_qsl(
                    parsed_url.query,
                    keep_blank_values=True,
                )
            )
            or any(is_sensitive_parameter_name(key) for key in params)
        ):
            raise WeatherSourceError(
                "public source request metadata is forbidden",
                code="public_request_metadata_forbidden",
            )
        last_error: Optional[BaseException] = None
        for attempt_index in range(self.retries + 1):
            self._assert_public_session()
            self._rate_limit(url)
            requested_epoch = self.clock()
            try:
                response = self._fetch(
                    "GET",
                    url,
                    params=dict(params),
                    timeout=self.timeout,
                    allow_redirects=False,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                )
            except requests.RequestException as exc:
                self._discard_response_cookie_state()
                last_error = exc
                if attempt_index >= self.retries:
                    raise WeatherSourceError(
                        f"{source} request failed after "
                        f"{attempt_index + 1} attempts "
                        f"({type(exc).__name__})",
                        code="public_request_failed",
                    ) from exc
            except Exception as exc:
                self._discard_response_cookie_state()
                raise WeatherSourceError(
                    f"{source} request failed ({type(exc).__name__})",
                    code="unexpected_transport_failure",
                ) from exc
            except BaseException:
                self._discard_response_cookie_state()
                raise
            else:
                self._discard_response_cookie_state()
                received_epoch = self.clock()
                body = bytes(response.content)
                provenance = AcquisitionProvenance(
                    source=source,
                    method="GET",
                    url=url,
                    request_params=MappingProxyType(dict(params)),
                    requested_at=_utc_from_epoch(requested_epoch),
                    received_at=_utc_from_epoch(received_epoch),
                    attempt=attempt_index + 1,
                    status_code=int(response.status_code),
                    response_headers=safe_public_response_headers(
                        response.headers
                    ),
                )
                try:
                    raw_text = body.decode("utf-8")
                except UnicodeDecodeError as exc:
                    acquisition = RawAcquisition(
                        raw_text=body.decode("utf-8", errors="replace"),
                        sha256=hashlib.sha256(body).hexdigest(),
                        provenance=provenance,
                    )
                    raise WeatherSourceError(
                        f"{source} returned non-UTF-8 content",
                        acquisition=acquisition,
                        code="non_utf8_response",
                    ) from exc
                acquisition = RawAcquisition(
                    raw_text=raw_text,
                    sha256=hashlib.sha256(body).hexdigest(),
                    provenance=provenance,
                )
                if 200 <= response.status_code < 300:
                    return acquisition
                last_error = WeatherSourceError(
                    f"{source} returned HTTP {response.status_code}",
                    acquisition=acquisition,
                    code=f"http_status_{int(response.status_code)}",
                )
                retryable = response.status_code == 429 or (
                    500 <= response.status_code < 600
                )
                if not retryable or attempt_index >= self.retries:
                    raise last_error
            delay = (
                self.backoff_base_seconds * (2**attempt_index)
                + self.jitter_seconds * self.random_fn()
            )
            if delay > 0:
                self.sleeper(delay)
        raise WeatherSourceError(
            f"{source} request failed",
            code="public_request_failed",
        )

    def discover_daily_temperature_events(
        self,
        *,
        start_date: date,
        end_date: date,
        limit: int = 100,
    ) -> GammaWeatherDiscovery:
        """Fetch Daily Temperature candidates using a bounded discovery filter.

        Gamma ``end_date_*`` bounds are only a coarse discovery filter.  The
        market's target local observation day must still be parsed from
        ``eventDate``, title, and rules and cross-checked elsewhere.
        """

        if start_date > end_date:
            raise ValueError("start_date cannot be after end_date")
        page_limit = min(max(int(limit), 1), 100)
        pages: list[RawAcquisition] = []
        events_by_id: dict[str, Mapping[str, Any]] = {}
        duplicate_ids: set[str] = set()
        stale_ids: set[str] = set()
        now = _utc_from_epoch(self.clock())
        offset = 0
        truncated = False
        while offset <= 2000:
            params = {
                "tag_id": DAILY_TEMPERATURE_TAG_ID,
                "end_date_min": f"{start_date.isoformat()}T00:00:00Z",
                "end_date_max": f"{end_date.isoformat()}T23:59:59Z",
                "order": "id",
                "ascending": True,
                "closed": False,
                "limit": page_limit,
                "offset": offset,
            }
            acquisition = self._get(
                GAMMA_EVENTS_URL,
                source="gamma_daily_temperature",
                params=params,
            )
            pages.append(acquisition)
            payload = _parse_json(acquisition)
            if not isinstance(payload, list):
                raise WeatherSourceError(
                    "Gamma events response must be an array",
                    acquisition=acquisition,
                )
            for item in payload:
                if not isinstance(item, Mapping):
                    raise WeatherSourceError(
                        "Gamma event must be an object",
                        acquisition=acquisition,
                    )
                event_id = str(item.get("id") or "")
                if not event_id:
                    raise WeatherSourceError(
                        "Gamma event id cannot be empty",
                        acquisition=acquisition,
                    )
                if event_id in events_by_id:
                    duplicate_ids.add(event_id)
                    continue
                event = MappingProxyType(dict(item))
                events_by_id[event_id] = event
                event_end = _parse_timestamp(
                    item.get("endDate") or item.get("eventDate")
                )
                if (
                    item.get("closed") is False
                    and event_end is not None
                    and event_end < now
                ):
                    stale_ids.add(event_id)
            if len(payload) < page_limit:
                break
            if offset == 2000:
                truncated = True
                break
            offset = min(offset + page_limit, 2000)
        return GammaWeatherDiscovery(
            events=tuple(events_by_id.values()),
            pages=tuple(pages),
            duplicate_event_ids=tuple(sorted(duplicate_ids)),
            stale_open_event_ids=tuple(sorted(stale_ids)),
            truncated_at_offset_cap=truncated,
        )

    def aviation_station_info(self, icao: str) -> AviationStation:
        """Fetch one ICAO station's coordinates from AviationWeather.gov."""

        station_id = str(icao).strip().upper()
        if re.fullmatch(r"[A-Z0-9]{4}", station_id) is None:
            raise ValueError("icao must be a four-character station identifier")
        acquisition = self._get(
            AVIATION_STATION_URL,
            source="aviationweather_stationinfo",
            params={"ids": station_id, "format": "json"},
        )
        payload = _parse_json(acquisition)
        if not isinstance(payload, list):
            raise WeatherSourceError(
                "AviationWeather station response must be an array",
                acquisition=acquisition,
            )
        matches = [
            item
            for item in payload
            if isinstance(item, Mapping)
            and str(
                item.get("icaoId")
                or item.get("icao")
                or item.get("station")
                or ""
            ).upper()
            == station_id
        ]
        if len(matches) != 1:
            raise WeatherSourceError(
                f"AviationWeather returned {len(matches)} matches for {station_id}",
                acquisition=acquisition,
            )
        raw = dict(matches[0])
        latitude = _decimal(
            raw.get("lat", raw.get("latitude")),
            field="station latitude",
            acquisition=acquisition,
        )
        longitude = _decimal(
            raw.get("lon", raw.get("longitude")),
            field="station longitude",
            acquisition=acquisition,
        )
        if not Decimal("-90") <= latitude <= Decimal("90"):
            raise WeatherSourceError(
                "station latitude is out of range",
                acquisition=acquisition,
            )
        if not Decimal("-180") <= longitude <= Decimal("180"):
            raise WeatherSourceError(
                "station longitude is out of range",
                acquisition=acquisition,
            )
        return AviationStation(
            icao=station_id,
            latitude=latitude,
            longitude=longitude,
            raw=MappingProxyType(raw),
            acquisition=acquisition,
        )

    def open_meteo_single_run(
        self,
        *,
        provider: str,
        model: str,
        initialized_at: datetime,
        release_delay: timedelta,
        decision_at: datetime,
        latitude: Decimal,
        longitude: Decimal,
        forecast_days: int,
    ) -> AcquiredForecastRun:
        """Fetch one exact model run only after its conservative availability."""

        provider_name = str(provider).strip()
        if not provider_name:
            raise ValueError("provider and model must be non-empty")
        model_name = _exact_operational_model(model)
        initialized = _aware_utc(initialized_at, field="initialized_at")
        if initialized.second or initialized.microsecond:
            raise ValueError("initialized_at must be exact to the minute")
        if not isinstance(release_delay, timedelta) or release_delay < timedelta(0):
            raise ValueError("release_delay must be a non-negative timedelta")
        decision = _aware_utc(decision_at, field="decision_at")
        available_at = (
            initialized + release_delay + FORECAST_AVAILABILITY_SAFETY
        )
        if decision < available_at:
            raise ForecastNotAvailableError(
                "forecast is not visible at decision time; "
                f"available_at={available_at.isoformat()}"
            )
        requested_latitude = _decimal(latitude, field="latitude")
        requested_longitude = _decimal(longitude, field="longitude")
        if not Decimal("-90") <= requested_latitude <= Decimal("90"):
            raise ValueError("latitude is out of range")
        if not Decimal("-180") <= requested_longitude <= Decimal("180"):
            raise ValueError("longitude is out of range")
        days = int(forecast_days)
        if not 1 <= days <= 16:
            raise ValueError("forecast_days must be between 1 and 16")
        params = {
            "latitude": str(requested_latitude),
            "longitude": str(requested_longitude),
            "hourly": "temperature_2m",
            "models": model_name,
            "run": initialized.strftime("%Y-%m-%dT%H:%M"),
            "forecast_days": days,
            "timezone": "auto",
        }
        acquisition = self._get(
            OPEN_METEO_SINGLE_RUNS_URL,
            source="open_meteo_single_run",
            params=params,
        )
        payload_value = _parse_json(acquisition)
        if not isinstance(payload_value, Mapping):
            raise WeatherSourceError(
                "Open-Meteo single run response must be an object",
                acquisition=acquisition,
            )
        payload = dict(payload_value)
        timezone_name = str(payload.get("timezone") or "")
        try:
            local_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise WeatherSourceError(
                f"Open-Meteo returned unknown timezone {timezone_name!r}",
                acquisition=acquisition,
            ) from exc
        hourly_value = payload.get("hourly")
        if not isinstance(hourly_value, Mapping):
            raise WeatherSourceError(
                "Open-Meteo single run hourly must be an object",
                acquisition=acquisition,
            )
        times = hourly_value.get("time")
        temperatures = hourly_value.get("temperature_2m")
        if not isinstance(times, list) or not isinstance(temperatures, list):
            raise WeatherSourceError(
                "Open-Meteo hourly time and temperature_2m must be arrays",
                acquisition=acquisition,
            )
        if len(times) != len(temperatures):
            raise WeatherSourceError(
                "Open-Meteo hourly arrays have different lengths",
                acquisition=acquisition,
            )
        hours: list[ForecastHour] = []
        for index, (time_value, temperature_value) in enumerate(
            zip(times, temperatures)
        ):
            try:
                local_at = datetime.fromisoformat(str(time_value))
            except ValueError as exc:
                raise WeatherSourceError(
                    f"Open-Meteo hourly time {index} is invalid",
                    acquisition=acquisition,
                ) from exc
            if local_at.tzinfo is None or local_at.utcoffset() is None:
                local_at = local_at.replace(tzinfo=local_timezone)
            else:
                local_at = local_at.astimezone(local_timezone)
            temperature = (
                None
                if temperature_value is None
                else _decimal(
                    temperature_value,
                    field=f"hourly temperature_2m[{index}]",
                    acquisition=acquisition,
                )
            )
            hours.append(
                ForecastHour(
                    local_at=local_at,
                    utc_at=local_at.astimezone(timezone.utc),
                    temperature_2m=temperature,
                )
            )
        frozen_hourly = MappingProxyType(
            {
                str(key): tuple(value) if isinstance(value, list) else value
                for key, value in hourly_value.items()
            }
        )
        return AcquiredForecastRun(
            provider=provider_name,
            model=model_name,
            initialized_at=initialized,
            release_delay=release_delay,
            safety_delay=FORECAST_AVAILABILITY_SAFETY,
            available_at=available_at,
            fetched_at=acquisition.provenance.received_at,
            requested_latitude=requested_latitude,
            requested_longitude=requested_longitude,
            grid_latitude=_decimal(
                payload.get("latitude"),
                field="grid latitude",
                acquisition=acquisition,
            ),
            grid_longitude=_decimal(
                payload.get("longitude"),
                field="grid longitude",
                acquisition=acquisition,
            ),
            timezone=timezone_name,
            forecast_days=days,
            hours=tuple(hours),
            raw_hourly=frozen_hourly,
            raw=MappingProxyType(payload),
            acquisition=acquisition,
        )

    def clob_price_history(
        self,
        *,
        token_id: str,
        start_at: datetime,
        end_at: datetime,
        fidelity: int = 1,
    ) -> CLOBPriceHistory:
        """Fetch official public price history, explicitly classified as L1."""

        token = str(token_id).strip()
        if not token:
            raise ValueError("token_id cannot be empty")
        start = _aware_utc(start_at, field="start_at")
        end = _aware_utc(end_at, field="end_at")
        if start >= end:
            raise ValueError("start_at must be before end_at")
        if start.microsecond or end.microsecond:
            raise ValueError("price history boundaries must use whole seconds")
        fidelity_minutes = int(fidelity)
        if fidelity_minutes < 1:
            raise ValueError("fidelity must be a positive number of minutes")
        params = {
            "market": token,
            "startTs": int(start.timestamp()),
            "endTs": int(end.timestamp()),
            "fidelity": fidelity_minutes,
        }
        acquisition = self._get(
            CLOB_PRICE_HISTORY_URL,
            source="clob_price_history_l1",
            params=params,
        )
        payload = _parse_json(acquisition)
        if not isinstance(payload, Mapping):
            raise WeatherSourceError(
                "CLOB price history response must be an object",
                acquisition=acquisition,
            )
        raw_history = payload.get("history")
        if not isinstance(raw_history, list):
            raise WeatherSourceError(
                "CLOB price history must contain a history array",
                acquisition=acquisition,
            )
        points: list[CLOBPricePoint] = []
        seen_times: dict[datetime, Decimal] = {}
        for index, raw_point in enumerate(raw_history):
            if not isinstance(raw_point, Mapping):
                raise WeatherSourceError(
                    f"CLOB price history point {index} must be an object",
                    acquisition=acquisition,
                )
            timestamp = _decimal(
                raw_point.get("t"),
                field=f"price history point {index} t",
                acquisition=acquisition,
            )
            if timestamp != timestamp.to_integral_value():
                raise WeatherSourceError(
                    "CLOB price history timestamps must use whole seconds",
                    acquisition=acquisition,
                )
            observed_at = datetime.fromtimestamp(
                int(timestamp), tz=timezone.utc
            )
            price = _decimal(
                raw_point.get("p"),
                field=f"price history point {index} p",
                acquisition=acquisition,
            )
            if not Decimal("0") <= price <= Decimal("1"):
                raise WeatherSourceError(
                    f"CLOB price history point {index} is outside [0, 1]",
                    acquisition=acquisition,
                )
            previous = seen_times.get(observed_at)
            if previous is not None and previous != price:
                raise WeatherSourceError(
                    "CLOB price history contains conflicting duplicate timestamps",
                    acquisition=acquisition,
                )
            if previous is None:
                seen_times[observed_at] = price
                points.append(
                    CLOBPricePoint(
                        observed_at=observed_at,
                        price=price,
                    )
                )
        points.sort(key=lambda point: point.observed_at)
        return CLOBPriceHistory(
            token_id=token,
            start_at=start,
            end_at=end,
            fidelity_minutes=fidelity_minutes,
            points=tuple(points),
            acquisition=acquisition,
        )

    def open_meteo_previous_runs_fixed_lead(
        self,
        *,
        provider: str,
        model: str,
        lead_days: int,
        release_delay: timedelta,
        latitude: Decimal,
        longitude: Decimal,
        start_date: date,
        end_date: date,
    ) -> FixedLeadForecastSeries:
        """Fetch the optional Previous Runs series at one explicit lead."""

        provider_name = str(provider).strip()
        if not provider_name:
            raise ValueError("provider and model must be non-empty")
        model_name = _exact_operational_model(model)
        lead = int(lead_days)
        if not 1 <= lead <= 7:
            raise ValueError("lead_days must be between 1 and 7")
        if not isinstance(release_delay, timedelta) or release_delay < timedelta(0):
            raise ValueError("release_delay must be a non-negative timedelta")
        if start_date > end_date:
            raise ValueError("start_date cannot be after end_date")
        requested_latitude = _decimal(latitude, field="latitude")
        requested_longitude = _decimal(longitude, field="longitude")
        variable = f"temperature_2m_previous_day{lead}"
        params = {
            "latitude": str(requested_latitude),
            "longitude": str(requested_longitude),
            "hourly": variable,
            "models": model_name,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "timezone": "auto",
        }
        acquisition = self._get(
            OPEN_METEO_PREVIOUS_RUNS_URL,
            source="open_meteo_previous_runs_fixed_lead",
            params=params,
        )
        payload_value = _parse_json(acquisition)
        if not isinstance(payload_value, Mapping):
            raise WeatherSourceError(
                "Open-Meteo Previous Runs response must be an object",
                acquisition=acquisition,
            )
        payload = dict(payload_value)
        timezone_name = str(payload.get("timezone") or "")
        try:
            local_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise WeatherSourceError(
                f"Open-Meteo returned unknown timezone {timezone_name!r}",
                acquisition=acquisition,
            ) from exc
        hourly_value = payload.get("hourly")
        if not isinstance(hourly_value, Mapping):
            raise WeatherSourceError(
                "Open-Meteo Previous Runs hourly must be an object",
                acquisition=acquisition,
            )
        times = hourly_value.get("time")
        temperatures = hourly_value.get(variable)
        if not isinstance(times, list) or not isinstance(temperatures, list):
            raise WeatherSourceError(
                f"Open-Meteo hourly time and {variable} must be arrays",
                acquisition=acquisition,
            )
        if len(times) != len(temperatures):
            raise WeatherSourceError(
                "Open-Meteo Previous Runs arrays have different lengths",
                acquisition=acquisition,
            )
        hours: list[FixedLeadForecastHour] = []
        for index, (time_value, temperature_value) in enumerate(
            zip(times, temperatures)
        ):
            try:
                local_at = datetime.fromisoformat(str(time_value))
            except ValueError as exc:
                raise WeatherSourceError(
                    f"Open-Meteo hourly time {index} is invalid",
                    acquisition=acquisition,
                ) from exc
            if local_at.tzinfo is None or local_at.utcoffset() is None:
                local_at = local_at.replace(tzinfo=local_timezone)
            else:
                local_at = local_at.astimezone(local_timezone)
            utc_at = local_at.astimezone(timezone.utc)
            lead_reference_at = utc_at - timedelta(days=lead)
            hours.append(
                FixedLeadForecastHour(
                    local_at=local_at,
                    utc_at=utc_at,
                    temperature_2m=(
                        None
                        if temperature_value is None
                        else _decimal(
                            temperature_value,
                            field=f"{variable}[{index}]",
                            acquisition=acquisition,
                        )
                    ),
                    lead_reference_at=lead_reference_at,
                    conservative_available_at=(
                        lead_reference_at
                        + release_delay
                        + FORECAST_AVAILABILITY_SAFETY
                    ),
                )
            )
        frozen_hourly = MappingProxyType(
            {
                str(key): tuple(value) if isinstance(value, list) else value
                for key, value in hourly_value.items()
            }
        )
        return FixedLeadForecastSeries(
            provider=provider_name,
            model=model_name,
            lead_days=lead,
            release_delay=release_delay,
            fetched_at=acquisition.provenance.received_at,
            timezone=timezone_name,
            hours=tuple(hours),
            raw_hourly=frozen_hourly,
            raw=MappingProxyType(payload),
            acquisition=acquisition,
        )


__all__ = [
    "AcquisitionProvenance",
    "AcquiredForecastRun",
    "AVIATION_STATION_URL",
    "AviationStation",
    "CLOB_PRICE_HISTORY_URL",
    "CLOBPriceHistory",
    "CLOBPricePoint",
    "DAILY_TEMPERATURE_TAG_ID",
    "FORECAST_AVAILABILITY_SAFETY",
    "FixedLeadForecastHour",
    "FixedLeadForecastSeries",
    "ForecastHour",
    "ForecastNotAvailableError",
    "GammaWeatherDiscovery",
    "RawAcquisition",
    "OPEN_METEO_SINGLE_RUNS_URL",
    "OPEN_METEO_PREVIOUS_RUNS_URL",
    "PriceAsOf",
    "PriceNotAvailableError",
    "SAFE_RESPONSE_HEADER_NAMES",
    "WeatherSourceError",
    "WeatherSources",
    "safe_public_response_headers",
    "validate_loopback_proxy_url",
]
