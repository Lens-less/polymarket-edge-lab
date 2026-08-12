"""Bounded, resumable acquisition runner for the public weather experiment.

Only public GET endpoints are used.  Every successful HTTP result is cached
with its raw text and provenance before derived experiment artifacts are
written.  Cached evidence is immutable by content hash and can be replayed
without network access.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, TypeVar
from urllib.parse import urlsplit

import requests

from .weather import WeatherDataError, WeatherEventSpec, parse_gamma_weather_event
from .weather_experiment import (
    MappingWeatherEvidenceProvider,
    NoEligibleWeatherEventsError,
    WeatherEventEvidence,
    WeatherExperimentConfig,
    WeatherExperimentError,
    WeatherExperimentResult,
    run_weather_experiment,
)
from .weather_sources import (
    AcquiredForecastRun,
    AcquisitionProvenance,
    AviationStation,
    AVIATION_STATION_URL,
    CLOBPriceHistory,
    CLOBPricePoint,
    DAILY_TEMPERATURE_TAG_ID,
    ForecastHour,
    GAMMA_EVENTS_URL,
    RawAcquisition,
    safe_public_response_headers,
    validate_loopback_proxy_url,
    WeatherSourceError,
    WeatherSources,
)


UTC = timezone.utc
ZERO = Decimal("0")
ONE = Decimal("1")
CACHE_SCHEMA = "weather-public-cache/v2"
LEGACY_CACHE_SCHEMA = "weather-public-cache/v1"
RESULT_SCHEMA = "weather-acquisition-run/v2"
REPRODUCIBILITY_SCHEMA = "weather-run-reproducibility/v1"
RUNNER_CONFIG_SCHEMA = "weather-runner-config/v1"
CODE_IDENTITY_SCHEMA = "weather-runner-code/v1"
T = TypeVar("T")


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError(f"{field} must have a valid UTC offset")
    return value.astimezone(UTC)


def _timedelta_microseconds(value: timedelta) -> int:
    return (
        value.days * 86_400_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, datetime):
        return {"__datetime__": _aware_utc(value, field="datetime").isoformat()}
    if isinstance(value, timedelta):
        return {"__timedelta_us__": _timedelta_microseconds(value)}
    if isinstance(value, Mapping):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        raise TypeError("runner cache refuses binary floats")
    raise TypeError(f"unsupported cache value: {type(value).__name__}")


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"__decimal__"}:
        return Decimal(str(value["__decimal__"]))
    if set(value) == {"__datetime__"}:
        parsed = datetime.fromisoformat(str(value["__datetime__"]))
        return _aware_utc(parsed, field="cached datetime")
    if set(value) == {"__timedelta_us__"}:
        return timedelta(microseconds=int(value["__timedelta_us__"]))
    return {str(key): _decode(item) for key, item in value.items()}


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        _encode(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> Any:
    return _decode(json.loads(path.read_text(encoding="utf-8")))


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _encode(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_binding(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"reproducibility input is not a regular file: {path}")
    return {
        "present": True,
        "size_bytes": path.stat().st_size,
        "sha256": _file_sha256(path),
    }


def _cache_snapshot(cache_root: Path) -> dict[str, Any]:
    """Bind every immutable cache file visible when the manifest is written."""

    if cache_root.is_symlink() or not cache_root.is_dir():
        raise ValueError(f"cache root is not a regular directory: {cache_root}")
    entries: list[dict[str, Any]] = []
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(cache_root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"cache snapshot refuses symlink: {path}")
        if not path.is_file():
            continue
        relative_path = path.relative_to(cache_root).as_posix()
        parts = relative_path.split("/")
        if (
            len(parts) != 2
            or path.suffix != ".json"
            or re.fullmatch(r"[0-9a-f]{64}", path.stem) is None
        ):
            raise ValueError(f"unexpected cache file layout: {relative_path}")
        entry = {
            "kind": parts[0],
            "key": path.stem,
            "path": relative_path,
            "size_bytes": path.stat().st_size,
            "sha256": _file_sha256(path),
        }
        entries.append(entry)
        by_kind.setdefault(parts[0], []).append(entry)
    if not entries:
        raise ValueError("cache snapshot cannot be empty")
    return {
        "scope": "all_regular_files_beneath_cache_at_manifest_time",
        "file_count": len(entries),
        "total_bytes": sum(int(entry["size_bytes"]) for entry in entries),
        "inventory_sha256": _canonical_sha256(entries),
        "by_kind": {
            kind: {
                "file_count": len(kind_entries),
                "total_bytes": sum(
                    int(entry["size_bytes"]) for entry in kind_entries
                ),
                "inventory_sha256": _canonical_sha256(kind_entries),
            }
            for kind, kind_entries in sorted(by_kind.items())
        },
        "entries": entries,
    }


def _code_identity() -> dict[str, Any]:
    """Hash the exact implementation and dependency-lock inputs in use."""

    repository_root = Path(__file__).resolve().parents[2]
    relative_paths = (
        "src/edge_lab/weather.py",
        "src/edge_lab/weather_sources.py",
        "src/edge_lab/economics.py",
        "src/edge_lab/experiments.py",
        "src/edge_lab/weather_experiment.py",
        "src/edge_lab/weather_experiment_runner.py",
        "scripts/run_weather_experiment.py",
        "pyproject.toml",
        "uv.lock",
    )
    files: list[dict[str, Any]] = []
    for relative_path in relative_paths:
        binding = _file_binding(repository_root / relative_path)
        files.append({"path": relative_path, **binding})
    return {
        "schema": CODE_IDENTITY_SCHEMA,
        "identity_kind": "exact_source_and_dependency_lock_bytes",
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "requests_version": requests.__version__,
        "files_sha256": _canonical_sha256(files),
        "files": files,
    }


def _cache_key(value: Any) -> str:
    return _canonical_sha256(value)


def _acquisition_payload(value: RawAcquisition) -> dict[str, Any]:
    return {
        "raw_text": value.raw_text,
        "sha256": value.sha256,
        "provenance": {
            "source": value.provenance.source,
            "method": value.provenance.method,
            "url": value.provenance.url,
            "request_params": dict(value.provenance.request_params),
            "requested_at": value.provenance.requested_at,
            "received_at": value.provenance.received_at,
            "attempt": value.provenance.attempt,
            "status_code": value.provenance.status_code,
            "response_headers": dict(
                safe_public_response_headers(
                    value.provenance.response_headers
                )
            ),
        },
    }


def _acquisition_from(payload: Mapping[str, Any]) -> RawAcquisition:
    raw_text = str(payload["raw_text"])
    sha256 = str(payload["sha256"])
    if hashlib.sha256(raw_text.encode("utf-8")).hexdigest() != sha256:
        raise ValueError("cached raw acquisition checksum mismatch")
    provenance = payload["provenance"]
    if not isinstance(provenance, Mapping):
        raise ValueError("cached acquisition provenance is invalid")
    response_headers = provenance.get("response_headers")
    if not isinstance(response_headers, Mapping):
        raise ValueError("cached response headers are invalid")
    normalized_headers = {
        str(key).strip().casefold(): str(item)
        for key, item in response_headers.items()
    }
    safe_headers = dict(safe_public_response_headers(normalized_headers))
    if normalized_headers != safe_headers:
        raise ValueError("cached response headers violate the safe allowlist")
    return RawAcquisition(
        raw_text=raw_text,
        sha256=sha256,
        provenance=AcquisitionProvenance(
            source=str(provenance["source"]),
            method=str(provenance["method"]),
            url=str(provenance["url"]),
            request_params=MappingProxyType(
                dict(provenance["request_params"])
            ),
            requested_at=_aware_utc(
                provenance["requested_at"],
                field="cached requested_at",
            ),
            received_at=_aware_utc(
                provenance["received_at"],
                field="cached received_at",
            ),
            attempt=int(provenance["attempt"]),
            status_code=int(provenance["status_code"]),
            response_headers=MappingProxyType(safe_headers),
        ),
    )


@dataclass(frozen=True)
class ResolvedWeatherBatch:
    events: tuple[Mapping[str, Any], ...]
    pages: tuple[RawAcquisition, ...]


def _batch_payload(value: ResolvedWeatherBatch) -> dict[str, Any]:
    return {
        "events": [dict(event) for event in value.events],
        "pages": [_acquisition_payload(page) for page in value.pages],
    }


def _batch_from(payload: Mapping[str, Any]) -> ResolvedWeatherBatch:
    events = payload.get("events")
    pages = payload.get("pages")
    if not isinstance(events, list) or not isinstance(pages, list):
        raise ValueError("cached Gamma batch is invalid")
    return ResolvedWeatherBatch(
        events=tuple(
            MappingProxyType(dict(event))
            for event in events
            if isinstance(event, Mapping)
        ),
        pages=tuple(_acquisition_from(page) for page in pages),
    )


def _station_payload(value: AviationStation) -> dict[str, Any]:
    return {
        "icao": value.icao,
        "latitude": value.latitude,
        "longitude": value.longitude,
        "raw": dict(value.raw),
        "acquisition": _acquisition_payload(value.acquisition),
    }


def _station_from(payload: Mapping[str, Any]) -> AviationStation:
    return AviationStation(
        icao=str(payload["icao"]),
        latitude=Decimal(str(payload["latitude"])),
        longitude=Decimal(str(payload["longitude"])),
        raw=MappingProxyType(dict(payload["raw"])),
        acquisition=_acquisition_from(payload["acquisition"]),
    )


def _forecast_payload(value: AcquiredForecastRun) -> dict[str, Any]:
    return {
        "provider": value.provider,
        "model": value.model,
        "initialized_at": value.initialized_at,
        "release_delay": value.release_delay,
        "safety_delay": value.safety_delay,
        "available_at": value.available_at,
        "fetched_at": value.fetched_at,
        "requested_latitude": value.requested_latitude,
        "requested_longitude": value.requested_longitude,
        "grid_latitude": value.grid_latitude,
        "grid_longitude": value.grid_longitude,
        "timezone": value.timezone,
        "forecast_days": value.forecast_days,
        "hours": [
            {
                "local_at": hour.local_at,
                "utc_at": hour.utc_at,
                "temperature_2m": hour.temperature_2m,
            }
            for hour in value.hours
        ],
        "raw_hourly": dict(value.raw_hourly),
        "raw": dict(value.raw),
        "acquisition": _acquisition_payload(value.acquisition),
        "data_kind": value.data_kind,
        "exact_run": value.exact_run,
        "decision_vintage": value.decision_vintage,
    }


def _forecast_from(payload: Mapping[str, Any]) -> AcquiredForecastRun:
    return AcquiredForecastRun(
        provider=str(payload["provider"]),
        model=str(payload["model"]),
        initialized_at=_aware_utc(
            payload["initialized_at"], field="cached initialized_at"
        ),
        release_delay=payload["release_delay"],
        safety_delay=payload["safety_delay"],
        available_at=_aware_utc(
            payload["available_at"], field="cached available_at"
        ),
        fetched_at=_aware_utc(
            payload["fetched_at"], field="cached fetched_at"
        ),
        requested_latitude=Decimal(str(payload["requested_latitude"])),
        requested_longitude=Decimal(str(payload["requested_longitude"])),
        grid_latitude=Decimal(str(payload["grid_latitude"])),
        grid_longitude=Decimal(str(payload["grid_longitude"])),
        timezone=str(payload["timezone"]),
        forecast_days=int(payload["forecast_days"]),
        hours=tuple(
            ForecastHour(
                local_at=hour["local_at"],
                utc_at=hour["utc_at"],
                temperature_2m=(
                    None
                    if hour["temperature_2m"] is None
                    else Decimal(str(hour["temperature_2m"]))
                ),
            )
            for hour in payload["hours"]
        ),
        raw_hourly=MappingProxyType(dict(payload["raw_hourly"])),
        raw=MappingProxyType(dict(payload["raw"])),
        acquisition=_acquisition_from(payload["acquisition"]),
        data_kind=str(payload["data_kind"]),
        exact_run=bool(payload["exact_run"]),
        decision_vintage=bool(payload["decision_vintage"]),
    )


def _history_payload(value: CLOBPriceHistory) -> dict[str, Any]:
    return {
        "token_id": value.token_id,
        "start_at": value.start_at,
        "end_at": value.end_at,
        "fidelity_minutes": value.fidelity_minutes,
        "points": [
            {"observed_at": point.observed_at, "price": point.price}
            for point in value.points
        ],
        "acquisition": _acquisition_payload(value.acquisition),
        "data_level": value.data_level,
        "evidence_limitation": value.evidence_limitation,
    }


def _history_from(payload: Mapping[str, Any]) -> CLOBPriceHistory:
    return CLOBPriceHistory(
        token_id=str(payload["token_id"]),
        start_at=_aware_utc(
            payload["start_at"], field="cached history start_at"
        ),
        end_at=_aware_utc(payload["end_at"], field="cached history end_at"),
        fidelity_minutes=int(payload["fidelity_minutes"]),
        points=tuple(
            CLOBPricePoint(
                observed_at=_aware_utc(
                    point["observed_at"],
                    field="cached price observed_at",
                ),
                price=Decimal(str(point["price"])),
            )
            for point in payload["points"]
        ),
        acquisition=_acquisition_from(payload["acquisition"]),
        data_level=str(payload["data_level"]),
        evidence_limitation=str(payload["evidence_limitation"]),
    )


class WeatherAcquisitionSource(Protocol):
    def resolved_events(
        self,
        *,
        start_date: date,
        end_date: date,
        max_candidates: int,
    ) -> ResolvedWeatherBatch:
        ...

    def station(self, station_code: str) -> AviationStation:
        ...

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
        ...

    def price_history(
        self,
        *,
        token_id: str,
        start_at: datetime,
        end_at: datetime,
        fidelity: int,
    ) -> CLOBPriceHistory:
        ...


class PublicWeatherAcquisitionSource:
    """Production adapter over the public-only :class:`WeatherSources`."""

    def __init__(self, sources: WeatherSources) -> None:
        self.sources = sources

    def resolved_events(
        self,
        *,
        start_date: date,
        end_date: date,
        max_candidates: int,
    ) -> ResolvedWeatherBatch:
        if start_date > end_date:
            raise ValueError("start_date cannot be after end_date")
        pages: list[RawAcquisition] = []
        events: list[Mapping[str, Any]] = []
        offset = 0
        while offset <= 2000 and len(events) < max_candidates:
            limit = min(100, max_candidates - len(events))
            params = {
                "tag_id": DAILY_TEMPERATURE_TAG_ID,
                "end_date_min": f"{start_date.isoformat()}T00:00:00Z",
                "end_date_max": f"{end_date.isoformat()}T23:59:59Z",
                "order": "id",
                "ascending": True,
                "closed": True,
                "limit": limit,
                "offset": offset,
            }
            acquisition = self.sources._get(  # acquisition boundary reuse
                GAMMA_EVENTS_URL,
                source="gamma_daily_temperature_resolved",
                params=params,
            )
            pages.append(acquisition)
            payload = json.loads(
                acquisition.raw_text,
                parse_float=Decimal,
            )
            if not isinstance(payload, list):
                raise WeatherSourceError(
                    "resolved Gamma events response must be an array",
                    acquisition=acquisition,
                )
            for event in payload:
                if not isinstance(event, Mapping):
                    raise WeatherSourceError(
                        "resolved Gamma event must be an object",
                        acquisition=acquisition,
                    )
                events.append(MappingProxyType(dict(event)))
                if len(events) >= max_candidates:
                    break
            if len(payload) < limit or offset == 2000:
                break
            offset += limit
        return ResolvedWeatherBatch(
            events=tuple(events),
            pages=tuple(pages),
        )

    def station(self, station_code: str) -> AviationStation:
        return self.sources.aviation_station_info(station_code)

    def preload_stations(
        self,
        station_codes: Sequence[str],
    ) -> Mapping[str, AviationStation]:
        """Fetch all required ICAO coordinates in one official request."""

        codes = tuple(sorted(set(station_codes)))
        if not codes:
            return MappingProxyType({})
        if any(re.fullmatch(r"[A-Z0-9]{4}", code) is None for code in codes):
            raise ValueError("all station codes must be four-character ICAO IDs")
        acquisition = self.sources._get(
            AVIATION_STATION_URL,
            source="aviation_station_batch",
            params={"ids": ",".join(codes), "format": "json"},
        )
        payload = json.loads(acquisition.raw_text, parse_float=Decimal)
        if not isinstance(payload, list):
            raise WeatherSourceError(
                "AviationWeather batch response must be an array",
                acquisition=acquisition,
            )
        output: dict[str, AviationStation] = {}
        for raw in payload:
            if not isinstance(raw, Mapping):
                raise WeatherSourceError(
                    "AviationWeather station row must be an object",
                    acquisition=acquisition,
                )
            code = str(raw.get("icaoId") or raw.get("icao") or "").upper()
            if code not in codes:
                continue
            latitude = Decimal(str(raw.get("lat", raw.get("latitude"))))
            longitude = Decimal(str(raw.get("lon", raw.get("longitude"))))
            if (
                not latitude.is_finite()
                or not longitude.is_finite()
                or not Decimal("-90") <= latitude <= Decimal("90")
                or not Decimal("-180") <= longitude <= Decimal("180")
            ):
                raise WeatherSourceError(
                    f"AviationWeather station {code} coordinates are invalid",
                    acquisition=acquisition,
                )
            output[code] = AviationStation(
                icao=code,
                latitude=latitude,
                longitude=longitude,
                raw=MappingProxyType(dict(raw)),
                acquisition=acquisition,
            )
        missing = sorted(set(codes) - set(output))
        if missing:
            raise WeatherSourceError(
                "AviationWeather batch omitted stations: " + ",".join(missing),
                acquisition=acquisition,
            )
        return MappingProxyType(output)

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
        return self.sources.open_meteo_single_run(
            provider="open-meteo",
            model=model,
            initialized_at=initialized_at,
            release_delay=release_delay,
            decision_at=decision_at,
            latitude=station.latitude,
            longitude=station.longitude,
            forecast_days=forecast_days,
        )

    def price_history(
        self,
        *,
        token_id: str,
        start_at: datetime,
        end_at: datetime,
        fidelity: int,
    ) -> CLOBPriceHistory:
        return self.sources.clob_price_history(
            token_id=token_id,
            start_at=start_at,
            end_at=end_at,
            fidelity=fidelity,
        )


class EvidenceCache:
    """Content-addressed, typed JSON cache with checksum validation."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, kind: str, key_payload: Mapping[str, Any]) -> tuple[str, Path]:
        key = _cache_key({"kind": kind, "request": key_payload})
        return key, self.root / kind / f"{key}.json"

    def contains(self, *, kind: str, key_payload: Mapping[str, Any]) -> bool:
        _, path = self._path(kind, key_payload)
        return path.exists()

    def get_or_fetch(
        self,
        *,
        kind: str,
        key_payload: Mapping[str, Any],
        fetch: Callable[[], T],
        serialize: Callable[[T], Mapping[str, Any]],
        deserialize: Callable[[Mapping[str, Any]], T],
    ) -> T:
        key, path = self._path(kind, key_payload)
        if path.exists():
            envelope = _read_json(path)
            if not isinstance(envelope, Mapping):
                raise ValueError(f"cache envelope is invalid: {path}")
            if envelope.get("schema") != CACHE_SCHEMA:
                raise ValueError(f"cache schema mismatch: {path}")
            if envelope.get("key") != key:
                raise ValueError(f"cache key mismatch: {path}")
            payload = envelope.get("payload")
            if not isinstance(payload, Mapping):
                raise ValueError(f"cache payload is invalid: {path}")
            return deserialize(payload)
        value = fetch()
        _atomic_write(
            path,
            {
                "schema": CACHE_SCHEMA,
                "key": key,
                "kind": kind,
                "request": dict(key_payload),
                "payload": serialize(value),
            },
        )
        os.chmod(path, 0o444)
        return value


def _safe_acquisition_error_detail(
    error: BaseException,
    *,
    code: Optional[str] = None,
) -> str:
    raw_code = str(
        code
        or getattr(error, "code", "")
        or type(error).__name__.casefold()
    )
    code = (
        raw_code
        if re.fullmatch(r"[a-z0-9_]{1,80}", raw_code)
        else "public_acquisition_failed"
    )
    return f"error_type={type(error).__name__};error_code={code}"


def _persist_failure_acquisition(
    output_dir: Path,
    *,
    event_id: str,
    stage: str,
    error: BaseException,
) -> None:
    acquisition = getattr(error, "acquisition", None)
    if not isinstance(acquisition, RawAcquisition):
        return
    raw_code = str(
        getattr(error, "code", "")
        or type(error).__name__.casefold()
    )
    error_code = (
        raw_code
        if re.fullmatch(r"[a-z0-9_]{1,80}", raw_code)
        else "public_acquisition_failed"
    )
    key = _cache_key(
        {
            "event_id": event_id,
            "stage": stage,
            "sha256": acquisition.sha256,
            "error_code": error_code,
        }
    )
    path = output_dir / "cache" / "failure" / f"{key}.json"
    if path.exists():
        return
    _atomic_write(
        path,
        {
            "schema": CACHE_SCHEMA,
            "kind": "failed_public_response_metadata",
            "event_id": event_id,
            "stage": stage,
            "error_type": type(error).__name__,
            "error_code": error_code,
            "response_body_sha256": acquisition.sha256,
            "provenance": {
                "source": acquisition.provenance.source,
                "method": acquisition.provenance.method,
                "endpoint_host": (
                    urlsplit(acquisition.provenance.url).hostname or ""
                ).casefold(),
                "endpoint_path": urlsplit(
                    acquisition.provenance.url
                ).path,
                "request_identity_sha256": _canonical_sha256(
                    {
                        "url": acquisition.provenance.url,
                        "request_params": dict(
                            acquisition.provenance.request_params
                        ),
                    }
                ),
                "requested_at": acquisition.provenance.requested_at,
                "received_at": acquisition.provenance.received_at,
                "attempt": acquisition.provenance.attempt,
                "status_code": acquisition.provenance.status_code,
                "response_headers": dict(
                    safe_public_response_headers(
                        acquisition.provenance.response_headers
                    )
                ),
            },
            "retention": {
                "response_body_persisted": False,
                "exception_text_persisted": False,
            },
        },
    )
    os.chmod(path, 0o444)


@dataclass(frozen=True)
class WeatherRunnerConfig:
    output_dir: Path
    start_date: date
    end_date: date
    max_events: int = 100
    max_candidates: int = 2000
    decision_lead: timedelta = timedelta(hours=24)
    model: str = "ecmwf_ifs025"
    release_delay: timedelta = timedelta(hours=4)
    cycle_hours: int = 6
    maximum_price_age: timedelta = timedelta(hours=6)
    market_warmup: timedelta = timedelta(minutes=30)

    def __post_init__(self) -> None:
        output = Path(self.output_dir).expanduser().resolve()
        if self.start_date > self.end_date:
            raise ValueError("start_date cannot be after end_date")
        for field in ("max_events", "max_candidates", "cycle_hours"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        if self.max_candidates < self.max_events:
            raise ValueError("max_candidates cannot be below max_events")
        if (
            not isinstance(self.decision_lead, timedelta)
            or self.decision_lead <= timedelta(0)
            or not isinstance(self.release_delay, timedelta)
            or self.release_delay < timedelta(0)
            or not isinstance(self.maximum_price_age, timedelta)
            or self.maximum_price_age <= timedelta(0)
            or not isinstance(self.market_warmup, timedelta)
            or self.market_warmup < timedelta(0)
        ):
            raise ValueError("runner time windows are invalid")
        normalized_model = self.model.casefold().replace("-", "_")
        if (
            not normalized_model
            or "seamless" in normalized_model
            or normalized_model in {"auto", "default", "best_match"}
            or normalized_model.startswith("era5")
            or "," in normalized_model
        ):
            raise ValueError("model must be one exact operational model")
        object.__setattr__(self, "output_dir", output)

    def cache_identity(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "max_candidates": self.max_candidates,
            "decision_lead_us": _timedelta_microseconds(self.decision_lead),
            "model": self.model,
            "release_delay_us": _timedelta_microseconds(self.release_delay),
            "cycle_hours": self.cycle_hours,
            "maximum_price_age_us": _timedelta_microseconds(
                self.maximum_price_age
            ),
            "market_warmup_us": _timedelta_microseconds(self.market_warmup),
            "candidate_order": (
                "local_date_round_robin_city_rotated_without_winner"
            ),
        }


@dataclass(frozen=True)
class AcquisitionExclusion:
    event_id: str
    stage: str
    reason: str
    detail: str

    def artifact_payload(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "stage": self.stage,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class WeatherRunnerResult:
    config: Mapping[str, Any]
    discovered_events: int
    eligible_events: int
    exclusions: tuple[AcquisitionExclusion, ...]
    experiment: Optional[WeatherExperimentResult]
    experiment_error: Optional[str]
    output_dir: Path
    classification: str = "insufficient_data"
    data_level: str = "L1"
    explainable_fills: int = 0

    def artifact_payload(self) -> dict[str, Any]:
        return {
            "schema": RESULT_SCHEMA,
            "config": dict(self.config),
            "discovered_events": self.discovered_events,
            "eligible_events": self.eligible_events,
            "classification": self.classification,
            "data_level": self.data_level,
            "explainable_fills": self.explainable_fills,
            "evidence_limitation": (
                "public L1 price history; no queue, fill, or realized PnL evidence"
            ),
            "exclusions": [
                exclusion.artifact_payload()
                for exclusion in self.exclusions
            ],
            "experiment_error": self.experiment_error,
            "experiment_available": self.experiment is not None,
            "safety": {
                "orders_submitted": False,
                "authenticated_endpoints_used": False,
                "rewards_assumed": "0",
                "live_trading_enabled": False,
            },
        }


def _bound_source_as_of(
    batch: ResolvedWeatherBatch,
    evidence: Mapping[str, WeatherEventEvidence],
) -> datetime:
    """Return the deterministic maximum received_at of bound acquisitions."""

    received_at = [
        page.provenance.received_at
        for page in batch.pages
    ]
    for event in evidence.values():
        received_at.extend(
            (
                event.station.acquisition.provenance.received_at,
                event.forecast.acquisition.provenance.received_at,
            )
        )
        received_at.extend(
            history.acquisition.provenance.received_at
            for history in event.price_histories.values()
        )
    if not received_at:
        raise ValueError("source_as_of requires at least one bound acquisition")
    return max(
        _aware_utc(value, field="acquisition received_at")
        for value in received_at
    )


def _manifest_payload(
    *,
    result: WeatherRunnerResult,
    source_as_of: datetime,
) -> dict[str, Any]:
    output = result.output_dir
    experiment_path = output / "EXPERIMENT.json"
    artifact_entries = [
        {
            "path": "ACQUISITION_DATASET.json",
            **_file_binding(output / "ACQUISITION_DATASET.json"),
        },
        {
            "path": "EXCLUSIONS.json",
            **_file_binding(output / "EXCLUSIONS.json"),
        },
    ]
    if result.experiment is None:
        if experiment_path.exists() or experiment_path.is_symlink():
            raise ValueError(
                "stale EXPERIMENT.json exists for an unavailable experiment"
            )
        artifact_entries.append(
            {
                "path": "EXPERIMENT.json",
                "present": False,
            }
        )
    else:
        artifact_entries.append(
            {
                "path": "EXPERIMENT.json",
                **_file_binding(experiment_path),
            }
        )
    security_audit_path = output / "SECURITY_SANITIZATION.json"
    if security_audit_path.exists() or security_audit_path.is_symlink():
        artifact_entries.append(
            {
                "path": "SECURITY_SANITIZATION.json",
                **_file_binding(security_audit_path),
            }
        )
    source_timestamp = _aware_utc(
        source_as_of,
        field="source_as_of",
    ).isoformat()
    runner_config = dict(result.config)
    reproducibility = {
        "schema": REPRODUCIBILITY_SCHEMA,
        "implementation_version": "weather-experiment-pipeline/1",
        "hash_algorithm": "sha256",
        "cache_schema": CACHE_SCHEMA,
        "invalidation_policy": (
            "any bound byte, inventory, code, runtime, configuration, or "
            "deterministic timestamp mismatch invalidates the run"
        ),
        "limitations": [
            (
                "the cache binding is a whole-directory byte snapshot, not "
                "a minimal per-event dependency graph; event predictions "
                "retain their station, forecast, and price-history hashes"
            )
        ],
        "runner_config": {
            "schema": RUNNER_CONFIG_SCHEMA,
            "sha256": _canonical_sha256(runner_config),
        },
        "code_identity": _code_identity(),
        "derived_artifacts": {
            "scope": "exact_file_bytes_before_RUN_MANIFEST_write",
            "inventory_sha256": _canonical_sha256(artifact_entries),
            "entries": artifact_entries,
        },
        "cache_snapshot": _cache_snapshot(output / "cache"),
        "deterministic_time": {
            "basis": "max_received_at_of_bound_public_acquisitions",
            "source_as_of": source_timestamp,
            "completed_at": source_timestamp,
        },
    }
    reproducibility["run_identity_sha256"] = _canonical_sha256(
        reproducibility
    )
    return {
        **result.artifact_payload(),
        "source_as_of": source_timestamp,
        "completed_at": source_timestamp,
        "time_basis": "max_received_at_of_bound_public_acquisitions",
        "reproducibility": reproducibility,
    }


def verify_weather_run_manifest(output_dir: Path) -> None:
    """Fail closed when a manifest no longer matches its bound snapshot."""

    output = Path(output_dir).expanduser().resolve()
    manifest = _read_json(output / "RUN_MANIFEST.json")
    if not isinstance(manifest, Mapping):
        raise ValueError("RUN_MANIFEST must be an object")
    if manifest.get("schema") != RESULT_SCHEMA:
        raise ValueError("RUN_MANIFEST schema mismatch")
    reproducibility = manifest.get("reproducibility")
    if not isinstance(reproducibility, Mapping):
        raise ValueError("RUN_MANIFEST reproducibility section is missing")
    if reproducibility.get("schema") != REPRODUCIBILITY_SCHEMA:
        raise ValueError("RUN_MANIFEST reproducibility schema mismatch")
    if reproducibility.get("cache_schema") != CACHE_SCHEMA:
        raise ValueError("RUN_MANIFEST cache schema mismatch")

    stored_identity = reproducibility.get("run_identity_sha256")
    identity_payload = dict(reproducibility)
    identity_payload.pop("run_identity_sha256", None)
    if stored_identity != _canonical_sha256(identity_payload):
        raise ValueError("RUN_MANIFEST run identity checksum mismatch")

    time_binding = reproducibility.get("deterministic_time")
    if not isinstance(time_binding, Mapping):
        raise ValueError("RUN_MANIFEST deterministic time binding is missing")
    if (
        manifest.get("source_as_of") != time_binding.get("source_as_of")
        or manifest.get("completed_at") != time_binding.get("completed_at")
        or manifest.get("time_basis") != time_binding.get("basis")
    ):
        raise ValueError("RUN_MANIFEST deterministic time binding mismatch")
    for field in ("source_as_of", "completed_at"):
        parsed = datetime.fromisoformat(str(manifest[field]))
        _aware_utc(parsed, field=f"manifest {field}")

    runner_config = reproducibility.get("runner_config")
    if (
        not isinstance(runner_config, Mapping)
        or runner_config.get("schema") != RUNNER_CONFIG_SCHEMA
        or runner_config.get("sha256")
        != _canonical_sha256(manifest.get("config"))
    ):
        raise ValueError("RUN_MANIFEST runner configuration binding mismatch")
    if reproducibility.get("code_identity") != _code_identity():
        raise ValueError("RUN_MANIFEST code identity mismatch")

    derived = reproducibility.get("derived_artifacts")
    if not isinstance(derived, Mapping):
        raise ValueError("RUN_MANIFEST artifact inventory is missing")
    entries = derived.get("entries")
    if not isinstance(entries, list):
        raise ValueError("RUN_MANIFEST artifact entries are invalid")
    if derived.get("inventory_sha256") != _canonical_sha256(entries):
        raise ValueError("RUN_MANIFEST artifact inventory checksum mismatch")
    expected_paths = {
        "ACQUISITION_DATASET.json",
        "EXCLUSIONS.json",
        "EXPERIMENT.json",
    }
    security_audit_path = output / "SECURITY_SANITIZATION.json"
    if security_audit_path.exists() or security_audit_path.is_symlink():
        expected_paths.add("SECURITY_SANITIZATION.json")
    if {str(entry.get("path")) for entry in entries} != expected_paths:
        raise ValueError("RUN_MANIFEST artifact set mismatch")
    for entry in entries:
        path = output / str(entry["path"])
        if entry.get("present") is False:
            if path.exists() or path.is_symlink():
                raise ValueError(f"unexpected artifact is present: {path.name}")
            continue
        current = _file_binding(path)
        if any(
            current.get(field) != entry.get(field)
            for field in ("present", "size_bytes", "sha256")
        ):
            raise ValueError(f"bound artifact checksum mismatch: {path.name}")

    if reproducibility.get("cache_snapshot") != _cache_snapshot(
        output / "cache"
    ):
        raise ValueError("RUN_MANIFEST cache snapshot mismatch")


def _json_object_end(data: bytes, start: int) -> int:
    if start >= len(data) or data[start] != ord("{"):
        raise ValueError("response_headers must be a JSON object")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(data)):
        value = data[index]
        if in_string:
            if escaped:
                escaped = False
            elif value == ord("\\"):
                escaped = True
            elif value == ord('"'):
                in_string = False
            continue
        if value == ord('"'):
            in_string = True
        elif value == ord("{"):
            depth += 1
        elif value == ord("}"):
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("response_headers JSON object is truncated")


def _sanitize_standard_cache_bytes(
    data: bytes,
) -> tuple[bytes, tuple[str, ...]]:
    legacy_marker = (
        b'"schema":"' + LEGACY_CACHE_SCHEMA.encode("ascii") + b'"'
    )
    current_marker = b'"schema":"' + CACHE_SCHEMA.encode("ascii") + b'"'
    if legacy_marker in data:
        data = data.replace(legacy_marker, current_marker, 1)
    elif current_marker not in data:
        raise ValueError("cache file has no recognized schema marker")

    marker = b'"response_headers":'
    parts: list[bytes] = []
    removed: set[str] = set()
    cursor = 0
    while True:
        marker_at = data.find(marker, cursor)
        if marker_at < 0:
            parts.append(data[cursor:])
            break
        object_start = marker_at + len(marker)
        object_end = _json_object_end(data, object_start)
        raw_headers = json.loads(data[object_start:object_end])
        if not isinstance(raw_headers, Mapping):
            raise ValueError("cached response_headers is not an object")
        normalized = {
            str(name).strip().casefold(): str(value)
            for name, value in raw_headers.items()
        }
        safe = dict(safe_public_response_headers(normalized))
        removed.update(set(normalized) - set(safe))
        safe_bytes = json.dumps(
            safe,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        parts.append(data[cursor:object_start])
        parts.append(safe_bytes)
        cursor = object_end
    return b"".join(parts), tuple(sorted(removed))


def _sanitized_legacy_failure_payload(
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    acquisition = envelope.get("acquisition")
    if not isinstance(acquisition, Mapping):
        raise ValueError("legacy failure acquisition is invalid")
    provenance = acquisition.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("legacy failure provenance is invalid")
    url = str(provenance.get("url") or "")
    request_params = provenance.get("request_params")
    if not isinstance(request_params, Mapping):
        request_params = {}
    raw_headers = provenance.get("response_headers")
    if not isinstance(raw_headers, Mapping):
        raw_headers = {}
    return {
        "schema": CACHE_SCHEMA,
        "kind": "failed_public_response_metadata",
        "event_id": str(envelope.get("event_id") or "<unknown>"),
        "stage": str(envelope.get("stage") or "unknown"),
        "error_type": "LegacyPublicResponseFailure",
        "error_code": "legacy_public_response_failure",
        "response_body_sha256": str(acquisition.get("sha256") or ""),
        "provenance": {
            "source": str(provenance.get("source") or ""),
            "method": str(provenance.get("method") or ""),
            "endpoint_host": (urlsplit(url).hostname or "").casefold(),
            "endpoint_path": urlsplit(url).path,
            "request_identity_sha256": _canonical_sha256(
                {
                    "url": url,
                    "request_params": dict(request_params),
                }
            ),
            "requested_at": provenance.get("requested_at"),
            "received_at": provenance.get("received_at"),
            "attempt": int(provenance.get("attempt") or 0),
            "status_code": int(provenance.get("status_code") or 0),
            "response_headers": dict(
                safe_public_response_headers(raw_headers)
            ),
        },
        "retention": {
            "response_body_persisted": False,
            "exception_text_persisted": False,
        },
    }


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _verify_pre_sanitization_snapshot(
    output: Path,
    manifest: Mapping[str, Any],
) -> None:
    reproducibility = manifest.get("reproducibility")
    if not isinstance(reproducibility, Mapping):
        raise ValueError("legacy manifest reproducibility is missing")
    snapshot = reproducibility.get("cache_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("legacy manifest cache snapshot is missing")
    entries = snapshot.get("entries")
    if not isinstance(entries, list):
        raise ValueError("legacy manifest cache entries are invalid")
    expected = {
        str(entry.get("path")): entry
        for entry in entries
        if isinstance(entry, Mapping)
    }
    actual_paths = {
        path.relative_to(output / "cache").as_posix(): path
        for path in (output / "cache").rglob("*.json")
    }
    if set(expected) != set(actual_paths):
        raise ValueError("legacy cache file set does not match its manifest")
    for relative_path, path in actual_paths.items():
        entry = expected[relative_path]
        if (
            int(entry.get("size_bytes") or -1) != path.stat().st_size
            or str(entry.get("sha256") or "") != _file_sha256(path)
        ):
            raise ValueError(
                f"legacy cache file does not match manifest: {relative_path}"
            )


def _high_confidence_cache_security_findings(cache_root: Path) -> list[str]:
    patterns = (
        b"__cf" + b"_bm",
        b'"set-' + b'cookie":',
        b'"cookie":',
        b'"authorization":',
        b'"proxy-' + b'authorization":',
    )
    findings: list[str] = []
    for path in sorted(cache_root.rglob("*.json")):
        lowered = path.read_bytes().lower()
        if any(pattern in lowered for pattern in patterns):
            findings.append(path.relative_to(cache_root).as_posix())
    return findings


def sanitize_weather_cache_in_place(output_dir: Path) -> Mapping[str, Any]:
    """Deterministically remove unsafe cached metadata and supersede the run."""

    output = Path(output_dir).expanduser().resolve()
    audit_path = output / "SECURITY_SANITIZATION.json"
    if audit_path.exists():
        existing = _read_json(audit_path)
        if not isinstance(existing, Mapping):
            raise ValueError("security sanitization audit is invalid")
        if _high_confidence_cache_security_findings(output / "cache"):
            raise ValueError("sanitized cache regressed to unsafe metadata")
        return existing

    manifest_path = output / "RUN_MANIFEST.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, Mapping):
        raise ValueError("pre-sanitization RUN_MANIFEST is invalid")
    _verify_pre_sanitization_snapshot(output, manifest)
    reproducibility = manifest.get("reproducibility")
    assert isinstance(reproducibility, Mapping)
    cache_snapshot = reproducibility.get("cache_snapshot")
    assert isinstance(cache_snapshot, Mapping)

    removed_header_counts: dict[str, int] = {}
    sensitive_files: list[dict[str, Any]] = []
    legacy_failure_payloads = 0
    cache_paths = sorted((output / "cache").rglob("*.json"))
    for path in cache_paths:
        old_sha256 = _file_sha256(path)
        raw = path.read_bytes()
        if b'"kind":"failed_public_response"' in raw:
            envelope = _read_json(path)
            if not isinstance(envelope, Mapping):
                raise ValueError(f"legacy failure cache is invalid: {path}")
            sanitized = json.dumps(
                _encode(_sanitized_legacy_failure_payload(envelope)),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            removed_names = tuple()
            legacy_failure_payloads += 1
        else:
            sanitized, removed_names = _sanitize_standard_cache_bytes(raw)
        for name in removed_names:
            removed_header_counts[name] = (
                removed_header_counts.get(name, 0) + 1
            )
        sensitive_names = tuple(
            name
            for name in removed_names
            if any(
                marker in name
                for marker in (
                    "authorization",
                    "cookie",
                    "credential",
                    "secret",
                    "signature",
                    "token",
                )
            )
        )
        if sanitized != raw:
            _atomic_write_bytes(path, sanitized)
            os.chmod(path, 0o444)
        if sensitive_names:
            sensitive_files.append(
                {
                    "path": path.relative_to(output).as_posix(),
                    "prior_file_sha256": old_sha256,
                    "sanitized_file_sha256": _file_sha256(path),
                    "removed_header_names": list(sensitive_names),
                }
            )

    findings = _high_confidence_cache_security_findings(output / "cache")
    if findings:
        raise ValueError(
            "cache sanitization left unsafe metadata in: "
            + ",".join(findings[:5])
        )
    previous_identity = reproducibility.get("run_identity_sha256")
    audit = {
        "schema": "weather-cache-security-sanitization/v1",
        "scope": "in_place_deterministic_metadata_sanitization",
        "previous_run": {
            "run_manifest_sha256": hashlib.sha256(
                manifest_bytes
            ).hexdigest(),
            "run_identity_sha256": previous_identity,
            "cache_inventory_sha256": cache_snapshot.get(
                "inventory_sha256"
            ),
            "status": "superseded_after_security_sanitization",
        },
        "canonical_run": {
            "status": "bound_by_containing_RUN_MANIFEST_after_replay",
            "cache_schema": CACHE_SCHEMA,
        },
        "cache_files_rewritten": len(cache_paths),
        "legacy_failure_payloads_without_body": legacy_failure_payloads,
        "removed_response_headers": [
            {
                "name": name,
                "affected_file_count": count,
            }
            for name, count in sorted(removed_header_counts.items())
        ],
        "sensitive_header_files": sensitive_files,
        "sensitive_header_values_retained": False,
        "exception_text_retained": False,
        "post_sanitization_high_confidence_findings": 0,
        "time_basis": manifest.get("time_basis"),
        "source_as_of": manifest.get("source_as_of"),
    }
    _atomic_write(audit_path, audit)
    return MappingProxyType(audit)


def _winner_preflight(spec: WeatherEventSpec) -> int:
    prices = tuple(weather_bin.yes_outcome_price for weather_bin in spec.bins)
    if any(price not in {ZERO, ONE} for price in prices):
        raise ValueError("resolved winner prices are not one-hot")
    winners = [index for index, price in enumerate(prices) if price == ONE]
    if len(winners) != 1:
        raise ValueError("resolved event does not have exactly one winner")
    return winners[0]


def _forecast_cycle(
    *,
    decision_at: datetime,
    release_delay: timedelta,
    cycle_hours: int,
) -> datetime:
    visible_cutoff = (
        decision_at - release_delay - timedelta(minutes=10)
    ).astimezone(UTC)
    cycle_hour = (visible_cutoff.hour // cycle_hours) * cycle_hours
    return visible_cutoff.replace(
        hour=cycle_hour,
        minute=0,
        second=0,
        microsecond=0,
    )


def _forecast_days(spec: WeatherEventSpec, initialized_at: datetime) -> int:
    from zoneinfo import ZoneInfo

    target_end = datetime.combine(
        spec.local_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=ZoneInfo(spec.timezone),
    ).astimezone(UTC)
    horizon = target_end - initialized_at
    days = max(1, (horizon.days + (1 if horizon.seconds else 0)) + 1)
    if days > 16:
        raise ValueError("target date is outside the 16-day forecast horizon")
    return days


def _can_run_experiment(
    specs: Sequence[WeatherEventSpec],
) -> bool:
    return (
        len({spec.local_date for spec in specs}) >= 5
        and len({spec.city for spec in specs}) >= 5
    )


def _market_open_timestamp(raw_value: Any) -> Optional[datetime]:
    if raw_value is None or isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, (int, Decimal)):
        numeric = Decimal(str(raw_value))
        if not numeric.is_finite():
            return None
        # Gamma has exposed both seconds and millisecond epoch fields.
        if numeric > Decimal("100000000000"):
            numeric /= Decimal("1000")
        if numeric != numeric.to_integral_value():
            return None
        return datetime.fromtimestamp(int(numeric), tz=UTC)
    text = str(raw_value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return _market_open_timestamp(Decimal(text))
    try:
        parsed = datetime.fromisoformat(
            f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _all_markets_open_at(raw: Mapping[str, Any]) -> datetime:
    markets = raw.get("markets")
    if not isinstance(markets, list) or not markets:
        raise ValueError("Gamma markets are missing")
    opened: list[datetime] = []
    for index, market in enumerate(markets):
        if not isinstance(market, Mapping):
            raise ValueError(f"Gamma market {index} is invalid")
        timestamp = _market_open_timestamp(
            market.get("acceptingOrdersTimestamp")
            or market.get("startDate")
            or market.get("startDateIso")
        )
        if timestamp is None:
            raise ValueError(f"Gamma market {index} has no auditable open time")
        opened.append(timestamp)
    return max(opened)


def _balanced_candidate_order(
    events: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Interleave local dates and rotate cities without inspecting winners."""

    by_date: dict[date, list[tuple[str, str, Mapping[str, Any]]]] = {}
    invalid: list[Mapping[str, Any]] = []
    for raw in events:
        try:
            spec = parse_gamma_weather_event(raw)
        except (WeatherDataError, KeyError, TypeError, ValueError):
            invalid.append(raw)
            continue
        by_date.setdefault(spec.local_date, []).append(
            (spec.city, spec.event_id, raw)
        )
    rotated: dict[date, list[Mapping[str, Any]]] = {}
    for date_index, event_date in enumerate(sorted(by_date)):
        ordered = [
            raw
            for _, _, raw in sorted(by_date[event_date])
        ]
        offset = date_index % len(ordered)
        rotated[event_date] = ordered[offset:] + ordered[:offset]
    result: list[Mapping[str, Any]] = []
    maximum_bucket = max(
        (len(values) for values in rotated.values()),
        default=0,
    )
    for round_index in range(maximum_bucket):
        for event_date in sorted(rotated):
            bucket = rotated[event_date]
            if round_index < len(bucket):
                result.append(bucket[round_index])
    result.extend(
        sorted(invalid, key=lambda item: str(item.get("id") or ""))
    )
    return tuple(result)


def acquire_weather_experiment(
    *,
    source: WeatherAcquisitionSource,
    config: WeatherRunnerConfig,
) -> WeatherRunnerResult:
    """Acquire up to ``max_events`` complete public event bundles and evaluate."""

    output = config.output_dir
    output.mkdir(parents=True, exist_ok=True)
    cache = EvidenceCache(output / "cache")
    batch = cache.get_or_fetch(
        kind="gamma",
        key_payload=config.cache_identity(),
        fetch=lambda: source.resolved_events(
            start_date=config.start_date,
            end_date=config.end_date,
            max_candidates=config.max_candidates,
        ),
        serialize=_batch_payload,
        deserialize=_batch_from,
    )

    if isinstance(source, PublicWeatherAcquisitionSource):
        candidate_station_codes: set[str] = set()
        for raw in batch.events:
            try:
                candidate_spec = parse_gamma_weather_event(raw)
            except (WeatherDataError, KeyError, TypeError, ValueError):
                continue
            if re.fullmatch(r"[A-Z0-9]{4}", candidate_spec.station_code):
                candidate_station_codes.add(candidate_spec.station_code)
        missing_station_codes = [
            code
            for code in sorted(candidate_station_codes)
            if not cache.contains(
                kind="station",
                key_payload={"station_code": code},
            )
        ]
        if missing_station_codes:
            try:
                preloaded = source.preload_stations(missing_station_codes)
            except (WeatherSourceError, KeyError, TypeError, ValueError) as exc:
                _persist_failure_acquisition(
                    output,
                    event_id="<station-batch>",
                    stage="station_batch",
                    error=exc,
                )
            else:
                for code, station_value in preloaded.items():
                    cache.get_or_fetch(
                        kind="station",
                        key_payload={"station_code": code},
                        fetch=lambda value=station_value: value,
                        serialize=_station_payload,
                        deserialize=_station_from,
                    )

    exclusions: list[AcquisitionExclusion] = []
    evidence: dict[str, WeatherEventEvidence] = {}
    accepted_raw: list[Mapping[str, Any]] = []
    accepted_specs: list[WeatherEventSpec] = []
    processed_raw: list[Mapping[str, Any]] = []
    id_counts: dict[str, int] = {}
    for candidate in batch.events:
        candidate_id = str(candidate.get("id") or "<missing>")
        id_counts[candidate_id] = id_counts.get(candidate_id, 0) + 1
    reported_duplicate_ids: set[str] = set()
    for raw in _balanced_candidate_order(batch.events):
        if len(evidence) >= config.max_events:
            break
        event_id = str(raw.get("id") or "<missing>")
        if id_counts[event_id] > 1:
            if event_id not in reported_duplicate_ids:
                exclusions.append(
                    AcquisitionExclusion(
                        event_id=event_id,
                        stage="gamma",
                        reason="duplicate_event_id",
                        detail="all duplicate candidates are quarantined",
                    )
                )
                reported_duplicate_ids.add(event_id)
            continue
        processed_raw.append(raw)
        try:
            spec = parse_gamma_weather_event(raw)
            if spec.quarantined:
                raise ValueError(
                    "rules quarantined: "
                    + ",".join(spec.quarantine_reasons)
                )
            winner_index = _winner_preflight(spec)
            if spec.bins[winner_index].tail_censored:
                exclusions.append(
                    AcquisitionExclusion(
                        event_id=event_id,
                        stage="gamma",
                        reason="winner_tail_censored",
                        detail=spec.bins[winner_index].label,
                    )
                )
                continue
            if re.fullmatch(r"[A-Z0-9]{4}", spec.station_code) is None:
                raise ValueError(
                    f"station code {spec.station_code!r} is not an ICAO code"
                )
        except (WeatherDataError, KeyError, TypeError, ValueError) as exc:
            exclusions.append(
                AcquisitionExclusion(
                    event_id=event_id,
                    stage="gamma",
                    reason="rules_or_resolution_invalid",
                    detail=_safe_acquisition_error_detail(
                        exc,
                        code="rules_or_resolution_invalid",
                    ),
                )
            )
            continue

        from zoneinfo import ZoneInfo

        local_start = datetime.combine(
            spec.local_date,
            datetime.min.time(),
            tzinfo=ZoneInfo(spec.timezone),
        ).astimezone(UTC)
        try:
            all_markets_open_at = _all_markets_open_at(raw)
        except ValueError as exc:
            exclusions.append(
                AcquisitionExclusion(
                    event_id=event_id,
                    stage="decision_time",
                    reason="market_open_time_missing",
                    detail=_safe_acquisition_error_detail(
                        exc,
                        code="market_open_time_missing",
                    ),
                )
            )
            continue
        nominal_decision = local_start - config.decision_lead
        decision_at = max(
            nominal_decision,
            all_markets_open_at + config.market_warmup,
        )
        if decision_at >= local_start:
            exclusions.append(
                AcquisitionExclusion(
                    event_id=event_id,
                    stage="decision_time",
                    reason="insufficient_market_warmup_before_target_day",
                    detail=(
                        f"market_open={all_markets_open_at.isoformat()},"
                        f"local_start={local_start.isoformat()}"
                    ),
                )
            )
            continue
        station_key = {"station_code": spec.station_code}
        try:
            station = cache.get_or_fetch(
                kind="station",
                key_payload=station_key,
                fetch=lambda code=spec.station_code: source.station(code),
                serialize=_station_payload,
                deserialize=_station_from,
            )
            initialized_at = _forecast_cycle(
                decision_at=decision_at,
                release_delay=config.release_delay,
                cycle_hours=config.cycle_hours,
            )
            forecast_days = _forecast_days(spec, initialized_at)
            forecast_key = {
                "event_id": event_id,
                "rules_hash": spec.rules_hash,
                "station_code": station.icao,
                "latitude": station.latitude,
                "longitude": station.longitude,
                "initialized_at": initialized_at,
                "decision_at": decision_at,
                "release_delay": config.release_delay,
                "model": config.model,
                "forecast_days": forecast_days,
            }
            forecast = cache.get_or_fetch(
                kind="forecast",
                key_payload=forecast_key,
                fetch=lambda: source.exact_forecast(
                    spec=spec,
                    station=station,
                    initialized_at=initialized_at,
                    decision_at=decision_at,
                    release_delay=config.release_delay,
                    model=config.model,
                    forecast_days=forecast_days,
                ),
                serialize=_forecast_payload,
                deserialize=_forecast_from,
            )
            if forecast.available_at > decision_at:
                raise ValueError("forecast_unavailable_at_decision")
            if (
                forecast.initialized_at != initialized_at
                or forecast.model != config.model
                or forecast.requested_latitude != station.latitude
                or forecast.requested_longitude != station.longitude
            ):
                raise ValueError("forecast_identity_mismatch")
        except (WeatherSourceError, KeyError, TypeError, ValueError) as exc:
            _persist_failure_acquisition(
                output,
                event_id=event_id,
                stage="forecast",
                error=exc,
            )
            reason = (
                "forecast_unavailable_at_decision"
                if (
                    isinstance(exc, ValueError)
                    and exc.args == ("forecast_unavailable_at_decision",)
                )
                else "forecast_or_station_acquisition_failed"
            )
            exclusions.append(
                AcquisitionExclusion(
                    event_id=event_id,
                    stage="forecast",
                    reason=reason,
                    detail=_safe_acquisition_error_detail(exc),
                )
            )
            continue

        histories: dict[str, CLOBPriceHistory] = {}
        price_failure: Optional[str] = None
        for weather_bin in spec.bins:
            token_id = weather_bin.yes_token_id
            start_at = decision_at - config.maximum_price_age
            end_at = decision_at + timedelta(minutes=1)
            price_key = {
                "event_id": event_id,
                "token_id": token_id,
                "start_at": start_at,
                "end_at": end_at,
                "fidelity": 1,
            }
            try:
                history = cache.get_or_fetch(
                    kind="price",
                    key_payload=price_key,
                    fetch=lambda token=token_id: source.price_history(
                        token_id=token,
                        start_at=start_at,
                        end_at=end_at,
                        fidelity=1,
                    ),
                    serialize=_history_payload,
                    deserialize=_history_from,
                )
                point = history.as_of(decision_at)
                if decision_at - point.observed_at > config.maximum_price_age:
                    raise ValueError("L1 point is stale at decision time")
                histories[token_id] = history
            except (WeatherSourceError, KeyError, TypeError, ValueError) as exc:
                _persist_failure_acquisition(
                    output,
                    event_id=event_id,
                    stage="price_history",
                    error=exc,
                )
                price_failure = _safe_acquisition_error_detail(exc)
                break
        if price_failure is not None:
            exclusions.append(
                AcquisitionExclusion(
                    event_id=event_id,
                    stage="price_history",
                    reason="market_asof_missing",
                    detail=price_failure,
                )
            )
            continue
        evidence[event_id] = WeatherEventEvidence(
            decision_at=decision_at,
            forecast=forecast,
            station=station,
            price_histories=MappingProxyType(histories),
            independence_group=f"date:{spec.local_date.isoformat()}",
        )
        accepted_raw.append(raw)
        accepted_specs.append(spec)

    experiment: Optional[WeatherExperimentResult] = None
    experiment_error: Optional[str] = None
    if accepted_raw and _can_run_experiment(accepted_specs):
        experiment_config = WeatherExperimentConfig(
            bias_grid=tuple(Decimal(value) for value in range(-8, 9, 2)),
            standard_deviation_grid=(
                Decimal("0.5"),
                Decimal("1"),
                Decimal("1.5"),
                Decimal("2"),
                Decimal("3"),
                Decimal("4"),
                Decimal("6"),
                Decimal("8"),
            ),
            edge_threshold_candidates=(
                Decimal("0"),
                Decimal("0.02"),
                Decimal("0.04"),
                Decimal("0.06"),
                Decimal("0.08"),
            ),
            taker_fee=ZERO,
            taker_fee_rate=Decimal("0.05"),
            taker_fee_exponent=Decimal("1"),
            taker_fee_source=(
                "Polymarket weather category fee schedule queried "
                "2026-07-24; price-dependent five-decimal formula"
            ),
            maker_fee=ZERO,
            slippage=Decimal("0.01"),
            model_margin=Decimal("0.02"),
            random_seed=73,
            minimum_independent_events=100,
            minimum_explainable_fills=100,
            # Per-event decision_at may move later than the nominal lead to
            # guarantee all 11 markets have opened plus the fixed warmup.
            minimum_decision_lead=timedelta(0),
            maximum_price_age=config.maximum_price_age,
        )
        try:
            experiment = run_weather_experiment(
                processed_raw,
                evidence_provider=MappingWeatherEvidenceProvider(evidence),
                config=experiment_config,
            )
        except (NoEligibleWeatherEventsError, WeatherExperimentError) as exc:
            experiment_error = _safe_acquisition_error_detail(
                exc,
                code="weather_experiment_unavailable",
            )
    else:
        experiment_error = (
            "need at least five local dates and five cities for both split views"
        )

    result = WeatherRunnerResult(
        config=MappingProxyType(
            {
                **config.cache_identity(),
                "max_events": config.max_events,
            }
        ),
        discovered_events=len(batch.events),
        eligible_events=len(evidence),
        exclusions=tuple(
            sorted(
                exclusions,
                key=lambda row: (row.event_id, row.stage, row.reason),
            )
        ),
        experiment=experiment,
        experiment_error=experiment_error,
        output_dir=output,
    )
    _atomic_write(
        output / "EXCLUSIONS.json",
        [row.artifact_payload() for row in result.exclusions],
    )
    _atomic_write(
        output / "ACQUISITION_DATASET.json",
        {
            "schema": "weather-acquisition-dataset/v1",
            "event_ids": sorted(evidence),
            "gamma_page_sha256": [page.sha256 for page in batch.pages],
            "decision_semantics": (
                "max(target local midnight minus nominal lead, latest of all "
                f"11 market opens plus {config.market_warmup})"
            ),
            "data_level": "L1",
            "explainable_fills": 0,
        },
    )
    if experiment is not None:
        _atomic_write(
            output / "EXPERIMENT.json",
            experiment.artifact_payload(),
        )
    else:
        (output / "EXPERIMENT.json").unlink(missing_ok=True)
    source_as_of = _bound_source_as_of(batch, evidence)
    _atomic_write(
        output / "RUN_MANIFEST.json",
        _manifest_payload(
            result=result,
            source_as_of=source_as_of,
        ),
    )
    verify_weather_run_manifest(output)
    return result


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Acquire public resolved weather evidence and run an L1-only "
            "shadow experiment. No orders or authenticated endpoints are used."
        )
    )
    parser.add_argument(
        "--start-date",
        type=_iso_date,
        default=date(2026, 4, 3),
    )
    parser.add_argument(
        "--end-date",
        type=_iso_date,
        default=date.today() - timedelta(days=1),
    )
    parser.add_argument("--max-events", type=int, default=100)
    parser.add_argument("--max-candidates", type=int, default=2000)
    parser.add_argument(
        "--proxy",
        type=validate_loopback_proxy_url,
        help="Explicit HTTP/SOCKS proxy; never written to artifacts.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/edge_lab/weather_public"),
    )
    parser.add_argument("--model", default="ecmwf_ifs025")
    parser.add_argument("--decision-lead-hours", type=int, default=24)
    parser.add_argument("--release-delay-hours", type=int, default=4)
    parser.add_argument("--market-warmup-minutes", type=int, default=30)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argument_parser().parse_args(argv)
    session = requests.Session()
    session.trust_env = False
    if args.proxy:
        session.proxies.update({"http": args.proxy, "https": args.proxy})
    sources = WeatherSources(session=session)
    result = acquire_weather_experiment(
        source=PublicWeatherAcquisitionSource(sources),
        config=WeatherRunnerConfig(
            output_dir=args.output,
            start_date=args.start_date,
            end_date=args.end_date,
            max_events=args.max_events,
            max_candidates=args.max_candidates,
            decision_lead=timedelta(hours=args.decision_lead_hours),
            model=args.model,
            release_delay=timedelta(hours=args.release_delay_hours),
            market_warmup=timedelta(minutes=args.market_warmup_minutes),
        ),
    )
    print(
        json.dumps(
            _encode(result.artifact_payload()),
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    return 0


__all__ = [
    "AcquisitionExclusion",
    "EvidenceCache",
    "PublicWeatherAcquisitionSource",
    "ResolvedWeatherBatch",
    "WeatherAcquisitionSource",
    "WeatherRunnerConfig",
    "WeatherRunnerResult",
    "acquire_weather_experiment",
    "build_argument_parser",
    "main",
    "sanitize_weather_cache_in_place",
    "verify_weather_run_manifest",
]
