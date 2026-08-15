"""Frozen settlement-regime identities for BTC TWAP capture and qualification.

The classifier deliberately separates observation from qualification.  An
unknown source combination is returned as ``quarantine`` so a recorder can
continue persisting public data while every evidence-producing consumer still
fails closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

REGISTRY_SCHEMA_VERSION = "btc-twap-settlement-regime-registry.v1"
LEGACY_SETTLEMENT_REGIME_ID = "chainlink_twap_30s_5m_and_60s_15m.v1"
V06_SETTLEMENT_REGIME_ID = "chainlink_twap_60s_5m_and_60s_15m.v1"

_HORIZONS = ("5m", "15m")
_TOPIC_BY_WINDOW = {
    30: "crypto_prices_twap_thirty",
    60: "crypto_prices_twap_sixty",
}
_REGIME_VERSION_SUFFIX = re.compile(r"\.v[0-9]+$")


def canonicalize_resolution_source(value: str) -> str:
    """Return the semantic Chainlink stream URL identity.

    Display-only query strings, fragments, URL case, and a trailing slash do
    not change the source identity.  Provider, path, or stream-window changes
    remain distinct and therefore fail closed at qualification time.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError("resolution source must be a non-empty URL")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("resolution source must be an unauthenticated HTTPS URL")
    path = parsed.path.rstrip("/").casefold()
    if not path:
        raise ValueError("resolution source path must be non-empty")
    return f"https://{parsed.hostname.casefold()}{path}"


def regime_scope_value(regime_id: str) -> str:
    if not isinstance(regime_id, str) or not regime_id.strip():
        raise ValueError("regime_id must be a non-empty string")
    return _REGIME_VERSION_SUFFIX.sub("", regime_id.strip())


@dataclass(frozen=True)
class SettlementSourceSpec:
    provider: str
    symbol: str
    product: str
    window_seconds: int
    stream_slug: str

    def __post_init__(self) -> None:
        for name in ("provider", "symbol", "product", "stream_slug"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip().casefold())
        if self.window_seconds not in _TOPIC_BY_WINDOW:
            raise ValueError("window_seconds must be a supported TWAP window")
        expected_slug = (
            f"btc-usd-twap-{self.window_seconds}s-streams"
            if self.symbol == "btc/usd"
            else None
        )
        if (
            self.provider != "chainlink"
            or self.product != "twap"
            or expected_slug is None
            or self.stream_slug != expected_slug
        ):
            raise ValueError("unsupported settlement-source identity")

    @property
    def resolution_source(self) -> str:
        return f"https://data.chain.link/streams/{self.stream_slug}"

    @property
    def source_topic(self) -> str:
        return _TOPIC_BY_WINDOW[self.window_seconds]

    def to_document(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "symbol": self.symbol,
            "product": self.product,
            "window_seconds": self.window_seconds,
            "stream_slug": self.stream_slug,
            "resolution_source": self.resolution_source,
            "source_topic": self.source_topic,
        }


@dataclass(frozen=True)
class SettlementRegimeSpec:
    regime_id: str
    sources: Mapping[str, SettlementSourceSpec]
    destination: str

    def __post_init__(self) -> None:
        if not isinstance(self.regime_id, str) or not self.regime_id.strip():
            raise ValueError("regime_id must be a non-empty string")
        if set(self.sources) != set(_HORIZONS):
            raise ValueError("settlement regime must define 5m and 15m sources")
        if any(
            not isinstance(source, SettlementSourceSpec)
            for source in self.sources.values()
        ):
            raise TypeError("settlement regime sources must be source specs")
        if not isinstance(self.destination, str) or not self.destination.strip():
            raise ValueError("destination must be a non-empty string")
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))

    def source_for(self, horizon: str) -> SettlementSourceSpec:
        try:
            return self.sources[horizon]
        except KeyError as exc:
            raise ValueError(f"unsupported horizon: {horizon}") from exc

    def to_document(self) -> dict[str, object]:
        return {
            "regime_id": self.regime_id,
            "destination": self.destination,
            "sources": {
                horizon: self.sources[horizon].to_document()
                for horizon in _HORIZONS
            },
        }


@dataclass(frozen=True)
class RegimeClassification:
    regime_id: str | None
    qualification_status: str
    destination: str
    reason_codes: tuple[str, ...]
    normalized_sources: Mapping[str, str | None]

    def __post_init__(self) -> None:
        if self.qualification_status not in {"eligible", "quarantine"}:
            raise ValueError("unsupported qualification status")
        object.__setattr__(
            self,
            "normalized_sources",
            MappingProxyType(dict(self.normalized_sources)),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "regime_id": self.regime_id,
            "qualification_status": self.qualification_status,
            "destination": self.destination,
            "reason_codes": list(self.reason_codes),
            "normalized_sources": dict(self.normalized_sources),
        }


@dataclass(frozen=True)
class SettlementRegimeRegistry:
    regimes: Mapping[str, SettlementRegimeSpec]
    unknown_destination: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.regimes:
            raise ValueError("settlement registry must contain at least one regime")
        if any(
            key != regime.regime_id
            for key, regime in self.regimes.items()
        ):
            raise ValueError("settlement registry keys must match regime ids")
        if not isinstance(self.unknown_destination, str) or not self.unknown_destination:
            raise ValueError("unknown destination must be non-empty")
        if len(self.sha256) != 64:
            raise ValueError("registry sha256 must be a lowercase digest")
        object.__setattr__(self, "regimes", MappingProxyType(dict(self.regimes)))

    def require(self, regime_id: str) -> SettlementRegimeSpec:
        try:
            return self.regimes[regime_id]
        except KeyError as exc:
            raise ValueError(f"unknown settlement regime: {regime_id}") from exc

    def classify(self, sources: Mapping[str, str]) -> RegimeClassification:
        normalized: dict[str, str | None] = {}
        for horizon in _HORIZONS:
            raw = sources.get(horizon)
            try:
                normalized[horizon] = canonicalize_resolution_source(str(raw))
            except (TypeError, ValueError):
                normalized[horizon] = None
        for regime_id in sorted(self.regimes):
            regime = self.regimes[regime_id]
            if all(
                normalized[horizon]
                == canonicalize_resolution_source(
                    regime.source_for(horizon).resolution_source
                )
                for horizon in _HORIZONS
            ):
                return RegimeClassification(
                    regime_id=regime_id,
                    qualification_status="eligible",
                    destination=regime.destination,
                    reason_codes=(),
                    normalized_sources=normalized,
                )
        return RegimeClassification(
            regime_id=None,
            qualification_status="quarantine",
            destination=self.unknown_destination,
            reason_codes=("unknown_settlement_regime",),
            normalized_sources=normalized,
        )


def _source_spec(document: object, *, label: str) -> SettlementSourceSpec:
    if not isinstance(document, Mapping):
        raise TypeError(f"{label} must be an object")
    required = {
        "provider",
        "symbol",
        "product",
        "window_seconds",
        "stream_slug",
    }
    if set(document) != required:
        raise ValueError(f"{label} has unsupported fields")
    window = document["window_seconds"]
    if isinstance(window, bool) or not isinstance(window, int):
        raise TypeError(f"{label}.window_seconds must be an integer")
    return SettlementSourceSpec(
        provider=str(document["provider"]),
        symbol=str(document["symbol"]),
        product=str(document["product"]),
        window_seconds=window,
        stream_slug=str(document["stream_slug"]),
    )


def load_settlement_regime_registry(path: Path) -> SettlementRegimeRegistry:
    resolved = path.expanduser().resolve()
    encoded = resolved.read_bytes()
    try:
        document = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("settlement registry is not valid UTF-8 JSON") from exc
    if not isinstance(document, Mapping):
        raise TypeError("settlement registry must be an object")
    if document.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise ValueError("unsupported settlement registry schema")
    raw_regimes = document.get("regimes")
    unknown = document.get("unknown_regime")
    if not isinstance(raw_regimes, Mapping) or not isinstance(unknown, Mapping):
        raise TypeError("settlement registry regimes are malformed")
    regimes: dict[str, SettlementRegimeSpec] = {}
    for raw_id, raw_regime in raw_regimes.items():
        if not isinstance(raw_id, str) or not isinstance(raw_regime, Mapping):
            raise TypeError("settlement regime entry is malformed")
        sources = {
            horizon: _source_spec(
                raw_regime.get(horizon),
                label=f"regimes.{raw_id}.{horizon}",
            )
            for horizon in _HORIZONS
        }
        default_destination = (
            "v0.6" if raw_id == V06_SETTLEMENT_REGIME_ID else "observe_only"
        )
        destination = raw_regime.get("destination", default_destination)
        regimes[raw_id] = SettlementRegimeSpec(
            regime_id=raw_id,
            sources=sources,
            destination=str(destination),
        )
    unknown_destination = unknown.get("destination")
    if not isinstance(unknown_destination, str) or not unknown_destination:
        raise ValueError("unknown_regime.destination must be non-empty")
    return SettlementRegimeRegistry(
        regimes=regimes,
        unknown_destination=unknown_destination,
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def legacy_settlement_regime() -> SettlementRegimeSpec:
    return SettlementRegimeSpec(
        regime_id=LEGACY_SETTLEMENT_REGIME_ID,
        destination="observe_only",
        sources={
            "5m": SettlementSourceSpec(
                provider="chainlink",
                symbol="btc/usd",
                product="twap",
                window_seconds=30,
                stream_slug="btc-usd-twap-30s-streams",
            ),
            "15m": SettlementSourceSpec(
                provider="chainlink",
                symbol="btc/usd",
                product="twap",
                window_seconds=60,
                stream_slug="btc-usd-twap-60s-streams",
            ),
        },
    )


def v06_settlement_regime() -> SettlementRegimeSpec:
    return SettlementRegimeSpec(
        regime_id=V06_SETTLEMENT_REGIME_ID,
        destination="v0.6",
        sources={
            "5m": SettlementSourceSpec(
                provider="chainlink",
                symbol="btc/usd",
                product="twap",
                window_seconds=60,
                stream_slug="btc-usd-twap-60s-streams",
            ),
            "15m": SettlementSourceSpec(
                provider="chainlink",
                symbol="btc/usd",
                product="twap",
                window_seconds=60,
                stream_slug="btc-usd-twap-60s-streams",
            ),
        },
    )


def builtin_settlement_regime(regime_id: str) -> SettlementRegimeSpec:
    if regime_id == LEGACY_SETTLEMENT_REGIME_ID:
        return legacy_settlement_regime()
    if regime_id == V06_SETTLEMENT_REGIME_ID:
        return v06_settlement_regime()
    raise ValueError(f"unknown settlement regime: {regime_id}")


__all__ = [
    "LEGACY_SETTLEMENT_REGIME_ID",
    "REGISTRY_SCHEMA_VERSION",
    "V06_SETTLEMENT_REGIME_ID",
    "RegimeClassification",
    "SettlementRegimeRegistry",
    "SettlementRegimeSpec",
    "SettlementSourceSpec",
    "builtin_settlement_regime",
    "canonicalize_resolution_source",
    "legacy_settlement_regime",
    "load_settlement_regime_registry",
    "regime_scope_value",
    "v06_settlement_regime",
]
