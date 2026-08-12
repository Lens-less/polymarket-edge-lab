"""Strict Gamma reconciliation for cataloged short-horizon crypto markets.

The reconciler consumes the exact raw body returned by the public
``GET /markets/{id}`` endpoint.  It validates the market against an immutable
``ShortCryptoTarget`` and retains an address of those exact response bytes.
It never treats Gamma ``startDate`` as the contract window opening time; the
opening boundary comes from the cataloged slug epoch.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .data_store import canonical_json_bytes
from .short_crypto_catalog import ShortCryptoTarget


class GammaReconciliationError(ValueError):
    """A Gamma response could not be bound safely to its catalog target."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class GammaMarketStatus:
    """The three independent lifecycle flags exposed by Gamma."""

    active: bool
    closed: bool
    accepting_orders: bool


@dataclass(frozen=True)
class GammaRawEvidence:
    """Content address for the exact public response body."""

    sha256: str
    byte_length: int


@dataclass(frozen=True)
class ShortCryptoGamma:
    """A Gamma market proven to be the same contract as a catalog target."""

    slug: str
    market_id: str
    condition_id: str
    outcomes: tuple[str, str]
    token_ids: tuple[str, str]
    resolution_source: str
    end_date_ms: int
    status: GammaMarketStatus
    evidence: GammaRawEvidence


_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "slug",
        "conditionId",
        "outcomes",
        "clobTokenIds",
        "description",
        "resolutionSource",
        "endDate",
        "active",
        "closed",
        "acceptingOrders",
    }
)
_DURATION_MS = {"5m": 5 * 60 * 1_000, "15m": 15 * 60 * 1_000}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GammaReconciliationError(
                "gamma_schema_mismatch",
                f"Gamma response contains duplicate field {key!r}",
            )
        result[key] = value
    return result


def _market_from_raw(raw_body: bytes) -> Mapping[str, Any]:
    if not isinstance(raw_body, bytes) or not raw_body:
        raise GammaReconciliationError(
            "gamma_raw_body_invalid",
            "raw_body must be non-empty immutable bytes",
        )
    try:
        market = json.loads(raw_body, object_pairs_hook=_unique_object)
    except GammaReconciliationError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GammaReconciliationError(
            "gamma_json_invalid",
            "Gamma raw body must be valid UTF-8 JSON",
        ) from exc
    if not isinstance(market, Mapping):
        raise GammaReconciliationError(
            "gamma_schema_mismatch",
            "Gamma market response must be an object",
        )
    missing = sorted(_REQUIRED_FIELDS - set(market))
    if missing:
        raise GammaReconciliationError(
            "gamma_schema_mismatch",
            f"Gamma market response is missing required fields: {missing}",
        )
    return market


def _array(value: Any, *, code: str, label: str) -> tuple[Any, ...]:
    candidate = value
    if isinstance(value, str):
        try:
            candidate = json.loads(value)
        except json.JSONDecodeError as exc:
            raise GammaReconciliationError(
                code,
                f"{label} must be a JSON array",
            ) from exc
    if not isinstance(candidate, Sequence) or isinstance(
        candidate, (str, bytes, bytearray)
    ):
        raise GammaReconciliationError(code, f"{label} must be an array")
    return tuple(candidate)


def _timestamp_ms(value: Any) -> int:
    if not isinstance(value, str) or not value.strip():
        raise GammaReconciliationError(
            "gamma_end_date_conflict",
            "Gamma endDate must be an ISO-8601 timestamp",
        )
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(
            f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError as exc:
        raise GammaReconciliationError(
            "gamma_end_date_conflict",
            "Gamma endDate must be an ISO-8601 timestamp",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GammaReconciliationError(
            "gamma_end_date_conflict",
            "Gamma endDate must include a timezone",
        )
    utc = parsed.astimezone(timezone.utc)
    if utc.microsecond % 1_000:
        raise GammaReconciliationError(
            "gamma_end_date_conflict",
            "Gamma endDate must be millisecond-aligned",
        )
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc - epoch
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def reconcile_short_crypto_gamma(
    target: ShortCryptoTarget,
    raw_body: bytes,
) -> ShortCryptoGamma:
    """Bind an exact public Gamma response to ``target``."""

    market = _market_from_raw(raw_body)
    if market.get("id") != target.market_id:
        raise GammaReconciliationError(
            "gamma_market_id_conflict",
            "Gamma id does not match the catalog target",
        )
    if market.get("slug") != target.slug:
        raise GammaReconciliationError(
            "gamma_slug_conflict",
            "Gamma slug does not match the catalog target",
        )
    if market.get("conditionId") != target.condition_id:
        raise GammaReconciliationError(
            "gamma_condition_conflict",
            "Gamma conditionId does not match the catalog target",
        )
    outcomes = _array(
        market.get("outcomes"),
        code="gamma_outcomes_conflict",
        label="Gamma outcomes",
    )
    if outcomes != ("Up", "Down"):
        raise GammaReconciliationError(
            "gamma_outcomes_conflict",
            "Gamma outcomes must be exactly ['Up', 'Down']",
        )
    token_ids = _array(
        market.get("clobTokenIds"),
        code="gamma_token_mapping_conflict",
        label="Gamma clobTokenIds",
    )
    if token_ids != (target.up_token_id, target.down_token_id):
        raise GammaReconciliationError(
            "gamma_token_mapping_conflict",
            "Gamma token order does not match Up/Down catalog mapping",
        )

    if (
        target.source_topic != "crypto_prices_chainlink"
        or target.source_symbol not in {"btc/usd", "eth/usd"}
    ):
        raise GammaReconciliationError(
            "target_rule_invalid",
            "target must name a supported Chainlink BTC/USD or ETH/USD source",
        )
    duration_ms = _DURATION_MS.get(target.horizon)
    if (
        duration_ms is None
        or target.closes_at_ms != target.opens_at_ms + duration_ms
    ):
        raise GammaReconciliationError(
            "target_window_invalid",
            "target close must equal its slug opening plus the declared horizon",
        )
    expected_source = (
        f"https://data.chain.link/streams/{target.source_symbol.split('/')[0]}-usd"
    )
    if market.get("resolutionSource") != expected_source:
        raise GammaReconciliationError(
            "gamma_resolution_source_conflict",
            "Gamma resolutionSource does not match the cataloged Chainlink stream",
        )
    end_date_ms = _timestamp_ms(market.get("endDate"))
    if end_date_ms != target.closes_at_ms:
        raise GammaReconciliationError(
            "gamma_end_date_conflict",
            "Gamma endDate does not equal the cataloged slug window close",
        )

    description = market.get("description")
    if not isinstance(description, str) or not description.strip():
        raise GammaReconciliationError(
            "gamma_rule_hash_conflict",
            "Gamma description must contain the cataloged resolution rule",
        )
    rules = market.get("rules")
    if rules not in (None, ""):
        if not isinstance(rules, str) or rules != description:
            raise GammaReconciliationError(
                "gamma_rule_text_conflict",
                "Gamma rules add text not bound by the market announcement",
            )
    rule_identity = {
        "catalog_schema": "edge-lab.short-crypto-target.v1",
        "slug": target.slug,
        "market_id": target.market_id,
        "condition_id": target.condition_id.lower(),
        "outcomes": list(outcomes),
        "token_ids": list(token_ids),
        "source_topic": target.source_topic,
        "source_symbol": target.source_symbol,
        "source_url": expected_source,
        "horizon": target.horizon,
        "opens_at_ms": target.opens_at_ms,
        "closes_at_ms": target.closes_at_ms,
        "description": description,
    }
    observed_rule_hash = hashlib.sha256(
        canonical_json_bytes(rule_identity)
    ).hexdigest()
    if observed_rule_hash != target.rule_hash:
        raise GammaReconciliationError(
            "gamma_rule_hash_conflict",
            "Gamma rule-bearing identity does not match target.rule_hash",
        )

    status_values = (
        market.get("active"),
        market.get("closed"),
        market.get("acceptingOrders"),
    )
    if any(type(value) is not bool for value in status_values):
        raise GammaReconciliationError(
            "gamma_status_schema_mismatch",
            "active, closed, and acceptingOrders must all be booleans",
        )

    return ShortCryptoGamma(
        slug=target.slug,
        market_id=target.market_id,
        condition_id=target.condition_id,
        outcomes=outcomes,
        token_ids=token_ids,
        resolution_source=expected_source,
        end_date_ms=end_date_ms,
        status=GammaMarketStatus(
            active=market["active"],
            closed=market["closed"],
            accepting_orders=market["acceptingOrders"],
        ),
        evidence=GammaRawEvidence(
            sha256=hashlib.sha256(raw_body).hexdigest(),
            byte_length=len(raw_body),
        ),
    )


def is_strict_gamma_preactivation(
    target: ShortCryptoTarget,
    raw_body: bytes,
) -> bool:
    """Recognize one exact, identity-valid Gamma pre-activation shape.

    Newly announced markets can briefly omit ``acceptingOrders`` while their
    immutable identity and rules are already published.  This predicate is
    intentionally narrower than the normal reconciler: it permits only that
    lifecycle omission, verifies the full contract by injecting ``False`` for
    validation, and never promotes the synthetic value as captured evidence.
    """

    if not isinstance(raw_body, bytes) or not raw_body:
        return False
    try:
        market = json.loads(raw_body, object_pairs_hook=_unique_object)
    except (
        GammaReconciliationError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return False
    if not isinstance(market, Mapping):
        return False
    missing = _REQUIRED_FIELDS - set(market)
    accepting_orders = market.get("acceptingOrders")
    if (
        missing not in (set(), {"acceptingOrders"})
        or (
            "acceptingOrders" in market
            and accepting_orders is not None
        )
        or market.get("active") is not False
        or market.get("closed") is not False
        or market.get("ready") is not False
        or market.get("funded") is not False
        or market.get("outcomePrices") is not None
    ):
        return False
    candidate = dict(market)
    candidate["acceptingOrders"] = False
    try:
        synthetic_raw = json.dumps(
            candidate,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        reconciled = reconcile_short_crypto_gamma(target, synthetic_raw)
    except (
        GammaReconciliationError,
        TypeError,
        ValueError,
    ):
        return False
    return (
        reconciled.status.active is False
        and reconciled.status.closed is False
        and reconciled.status.accepting_orders is False
    )
