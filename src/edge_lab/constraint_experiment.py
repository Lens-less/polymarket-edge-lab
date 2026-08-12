"""Reproducible public-GET experiments for semantic and Neg-Risk bundles.

The runner is intentionally separated from execution.  It pins event
composition and rule hashes in a checked-in config, re-fetches only public
Gamma/CLOB resources, preserves exact response bodies, and writes every
visible-depth candidate including rejections.

An ``accepted_snapshot`` row is only a synchronous-book diagnostic.  It is not
evidence of fillability, atomic execution, settlement, or live profitability.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING
from hashlib import sha256
import json
import math
from pathlib import Path
import platform
import re
import sys
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests

from .compatibility import LiveExecutionBlocked, assert_new_orders_disabled
from .constraints import (
    BookSnapshotEvidence,
    BundleLeg,
    Constraint,
    ConstraintGraph,
    InventoryTransform,
    Opportunity,
    PredicateNode,
    enumerate_bundle_opportunities,
)
from .models import BookLevel, FeeSchedule, OrderBook
from .sources import (
    GAMMA_BASE,
    CompactCLOBMarket,
    FetchMetadata,
    Fetched,
    PublicSourceError,
    PublicSourcesClient,
    RawResponse,
    ResolutionRules,
    SourceBook,
)


CONFIG_SCHEMA_VERSION = "edge-lab-constraint-experiment-config.v2"
SUMMARY_SCHEMA_VERSION = "edge-lab-constraint-experiment-summary.v2"
CANDIDATE_SCHEMA_VERSION = "edge-lab-constraint-candidate.v1"
SHARE_SCALE = Decimal("1000000")
FEE_QUANTUM = Decimal("0.00001")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_ALLOWED_MODES = frozenset(
    {"logic", "standard_neg_risk", "augmented_neg_risk"}
)
_SENSITIVE_KEY_PARTS = (
    "apikey",
    "accesstoken",
    "apitoken",
    "authtoken",
    "authorization",
    "bearer",
    "bearertoken",
    "clientsecret",
    "cookie",
    "credential",
    "mnemonic",
    "oauthtoken",
    "passphrase",
    "passwd",
    "password",
    "personalaccesstoken",
    "privatekey",
    "proxyauth",
    "proxyauthorization",
    "refreshtoken",
    "secret",
    "seedphrase",
    "sessiontoken",
    "setcookie",
    "signature",
    "wallet",
)
_EDGE_ONLY_FAILURES = frozenset(
    {"edge_nonpositive", "edge_below_rounding_uncertainty"}
)
# A constraint claim cannot approve itself through config.  Each hash here is
# a separately code-reviewed claim over the exact event set, node identities,
# question/rule/resolution hashes, and relation kind.  A new semantic claim
# therefore requires an implementation change and review before it can ever
# produce an accepted row.
_REVIEWED_CONSTRAINT_CLAIMS = frozenset(
    {
        # NYC 2026-07-25 whole-degree, mutually exhaustive temperature bins.
        "a501a1ff6549d5c26a12cc93d1cb38a8d5cfe38e28e1113472730a5ff6bce435",
    }
)


def _digest(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return sha256(payload).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _append_failure(failures: list[str], code: str) -> None:
    if code and code not in failures:
        failures.append(code)


def _credential_like_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _extend_failures(failures: list[str], codes: Sequence[str]) -> None:
    for code in codes:
        _append_failure(failures, code)


def _reject_sensitive_keys(value: Any, *, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _credential_like_key(key):
                raise ValueError(
                    f"{path} contains forbidden credential-like key: {key}"
                )
            _reject_sensitive_keys(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_sensitive_keys(nested, path=f"{path}[{index}]")


def _mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return value


def _list(value: Any, *, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    return value


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _strings(value: Any, *, field_name: str) -> tuple[str, ...]:
    result = tuple(
        _text(item, field_name=f"{field_name}[]")
        for item in _list(value, field_name=field_name)
    )
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} cannot contain duplicates")
    return result


def _decimal(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return result


def _integer(value: Any, *, field_name: str, minimum: int = 0) -> int:
    decimal = _decimal(value, field_name=field_name)
    if decimal != decimal.to_integral_value() or decimal < minimum:
        raise ValueError(
            f"{field_name} must be an integer >= {minimum}"
        )
    return int(decimal)


def _boolean(value: Any, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _hash_pin(value: Any, *, field_name: str) -> str:
    result = _text(value, field_name=field_name).lower()
    if not _HEX_64.fullmatch(result):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
    return result


@dataclass(frozen=True)
class EventSpec:
    event_id: str
    expected_market_ids: tuple[str, ...]
    require_active: bool
    require_open: bool
    expected_neg_risk: bool
    expected_augmented_neg_risk: bool


@dataclass(frozen=True)
class MarketPin:
    node_id: str
    market_id: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    role: str
    question_sha256: str
    rules_sha256: str
    resolution_source_sha256: str

    @property
    def stable_evidence(self) -> tuple[str, ...]:
        return (
            (
                f"gamma-market:{self.market_id}:"
                f"question-sha256:{self.question_sha256}"
            ),
            (
                f"gamma-market:{self.market_id}:"
                f"rules-sha256:{self.rules_sha256}"
            ),
            (
                f"gamma-market:{self.market_id}:resolution-source-sha256:"
                f"{self.resolution_source_sha256}"
            ),
        )


@dataclass(frozen=True)
class ConstraintSpec:
    constraint_id: str
    kind: str
    node_ids: tuple[str, ...]
    verified: bool
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class BundleSpec:
    candidate_id: str
    family: str
    legs: tuple[BundleLeg, ...]
    quantities: Optional[tuple[Decimal, ...]]
    inventory: Mapping[str, Decimal]


@dataclass(frozen=True)
class ConversionSpec:
    candidate_id: str
    selected_node_ids: tuple[str, ...]
    quantities: Optional[tuple[Decimal, ...]]


@dataclass(frozen=True)
class NegRiskProvenance:
    onchain_question_count: Optional[int] = None
    chain_index_map: Optional[Mapping[str, int]] = None
    adapter_address: Optional[str] = None
    adapter_block_number: Optional[int] = None
    adapter_fee_bips: Optional[int] = None
    collateral_decimals: Optional[int] = None
    chain_index_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalysisSpec:
    analysis_id: str
    mode: str
    bundles: tuple[BundleSpec, ...]
    conversions: tuple[ConversionSpec, ...]
    neg_risk_provenance: NegRiskProvenance


@dataclass(frozen=True)
class SnapshotPolicy:
    gas: Decimal
    latency_buffer: Decimal
    max_book_age_ms: int
    max_book_skew_ms: int
    max_book_rtt_ms: int


@dataclass(frozen=True)
class ConstraintExperimentSpec:
    experiment_id: str
    event: EventSpec
    markets: tuple[MarketPin, ...]
    constraints: tuple[ConstraintSpec, ...]
    analyses: tuple[AnalysisSpec, ...]
    policy: SnapshotPolicy
    config_document: Mapping[str, Any] = field(repr=False)
    config_sha256: str


def _event_market_set_sha256(event: EventSpec) -> str:
    return _digest(
        _canonical_json(sorted(event.expected_market_ids))
    )


def _constraint_claim_sha256(
    event: EventSpec,
    markets_by_node: Mapping[str, MarketPin],
    constraint: ConstraintSpec,
) -> str:
    return _digest(
        _canonical_json(
            {
                "schema_version": "edge-lab-constraint-claim.v1",
                "event_id": event.event_id,
                "event_market_set_sha256": _event_market_set_sha256(event),
                "constraint_id": constraint.constraint_id,
                "kind": constraint.kind,
                "node_ids": list(constraint.node_ids),
                "market_pins": [
                    {
                        "node_id": pin.node_id,
                        "market_id": pin.market_id,
                        "condition_id": pin.condition_id,
                        "question_sha256": pin.question_sha256,
                        "rules_sha256": pin.rules_sha256,
                        "resolution_source_sha256": (
                            pin.resolution_source_sha256
                        ),
                    }
                    for pin in (
                        markets_by_node[node_id]
                        for node_id in constraint.node_ids
                    )
                ],
            }
        )
    )


def _required_constraint_evidence(
    event: EventSpec,
    markets_by_node: Mapping[str, MarketPin],
    constraint: ConstraintSpec,
) -> tuple[str, str]:
    return (
        (
            f"constraint-claim:{constraint.constraint_id}:sha256:"
            f"{_constraint_claim_sha256(event, markets_by_node, constraint)}"
        ),
        (
            f"gamma-event:{event.event_id}:market-set-sha256:"
            f"{_event_market_set_sha256(event)}"
        ),
    )


def _constraint_claim_reviewed(
    event: EventSpec,
    markets_by_node: Mapping[str, MarketPin],
    constraint: ConstraintSpec,
) -> bool:
    return (
        _constraint_claim_sha256(event, markets_by_node, constraint)
        in _REVIEWED_CONSTRAINT_CLAIMS
    )


def _parse_quantities(
    value: Any, *, field_name: str
) -> Optional[tuple[Decimal, ...]]:
    if value is None:
        return None
    quantities = tuple(
        _decimal(item, field_name=f"{field_name}[]")
        for item in _list(value, field_name=field_name)
    )
    if any(quantity <= 0 for quantity in quantities):
        raise ValueError(f"{field_name} must contain positive quantities")
    return tuple(sorted(set(quantities)))


def _parse_provenance(value: Any, *, field_name: str) -> NegRiskProvenance:
    raw = _mapping(value, field_name=field_name)
    allowed = {
        "onchain_question_count",
        "chain_index_map",
        "adapter_address",
        "adapter_block_number",
        "adapter_fee_bips",
        "collateral_decimals",
        "chain_index_evidence",
    }
    unexpected = set(raw) - allowed
    if unexpected:
        raise ValueError(
            f"{field_name} contains unsupported fields: "
            + ", ".join(sorted(str(item) for item in unexpected))
        )
    mapping_value = raw.get("chain_index_map")
    chain_map: Optional[Mapping[str, int]]
    if mapping_value is None:
        chain_map = None
    else:
        mapping_raw = _mapping(
            mapping_value, field_name=f"{field_name}.chain_index_map"
        )
        chain_map = MappingProxyType(
            {
                _text(key, field_name="chain_index_map key"): _integer(
                    index,
                    field_name=f"{field_name}.chain_index_map[{key}]",
                )
                for key, index in mapping_raw.items()
            }
        )
    return NegRiskProvenance(
        onchain_question_count=(
            None
            if raw.get("onchain_question_count") is None
            else _integer(
                raw["onchain_question_count"],
                field_name=f"{field_name}.onchain_question_count",
                minimum=1,
            )
        ),
        chain_index_map=chain_map,
        adapter_address=(
            None
            if raw.get("adapter_address") is None
            else _text(
                raw["adapter_address"],
                field_name=f"{field_name}.adapter_address",
            )
        ),
        adapter_block_number=(
            None
            if raw.get("adapter_block_number") is None
            else _integer(
                raw["adapter_block_number"],
                field_name=f"{field_name}.adapter_block_number",
            )
        ),
        adapter_fee_bips=(
            None
            if raw.get("adapter_fee_bips") is None
            else _integer(
                raw["adapter_fee_bips"],
                field_name=f"{field_name}.adapter_fee_bips",
            )
        ),
        collateral_decimals=(
            None
            if raw.get("collateral_decimals") is None
            else _integer(
                raw["collateral_decimals"],
                field_name=f"{field_name}.collateral_decimals",
            )
        ),
        chain_index_evidence=(
            ()
            if raw.get("chain_index_evidence") is None
            else _strings(
                raw["chain_index_evidence"],
                field_name=f"{field_name}.chain_index_evidence",
            )
        ),
    )


def load_constraint_experiment_spec(path: Path) -> ConstraintExperimentSpec:
    """Load and strictly validate a credential-free experiment config."""

    document = json.loads(path.read_text(encoding="utf-8"))
    raw = _mapping(document, field_name="config")
    _reject_sensitive_keys(raw)
    if raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError("unsupported constraint experiment schema_version")
    allowed = {
        "schema_version",
        "experiment_id",
        "event",
        "markets",
        "constraints",
        "analyses",
        "policy",
    }
    unexpected = set(raw) - allowed
    if unexpected:
        raise ValueError(
            "constraint experiment config contains unsupported fields: "
            + ", ".join(sorted(str(item) for item in unexpected))
        )
    experiment_id = _text(
        raw.get("experiment_id"), field_name="experiment_id"
    )
    event_raw = _mapping(raw.get("event"), field_name="event")
    event = EventSpec(
        event_id=_text(event_raw.get("event_id"), field_name="event.event_id"),
        expected_market_ids=_strings(
            event_raw.get("expected_market_ids"),
            field_name="event.expected_market_ids",
        ),
        require_active=_boolean(
            event_raw.get("require_active"),
            field_name="event.require_active",
        ),
        require_open=_boolean(
            event_raw.get("require_open"),
            field_name="event.require_open",
        ),
        expected_neg_risk=_boolean(
            event_raw.get("expected_neg_risk"),
            field_name="event.expected_neg_risk",
        ),
        expected_augmented_neg_risk=_boolean(
            event_raw.get("expected_augmented_neg_risk"),
            field_name="event.expected_augmented_neg_risk",
        ),
    )
    market_rows = _list(raw.get("markets"), field_name="markets")
    markets = tuple(
        MarketPin(
            node_id=_text(
                market.get("node_id"),
                field_name=f"markets[{index}].node_id",
            ),
            market_id=_text(
                market.get("market_id"),
                field_name=f"markets[{index}].market_id",
            ),
            condition_id=_text(
                market.get("condition_id"),
                field_name=f"markets[{index}].condition_id",
            ),
            yes_token_id=_text(
                market.get("yes_token_id"),
                field_name=f"markets[{index}].yes_token_id",
            ),
            no_token_id=_text(
                market.get("no_token_id"),
                field_name=f"markets[{index}].no_token_id",
            ),
            role=_text(
                market.get("role"), field_name=f"markets[{index}].role"
            ),
            question_sha256=_hash_pin(
                market.get("question_sha256"),
                field_name=f"markets[{index}].question_sha256",
            ),
            rules_sha256=_hash_pin(
                market.get("rules_sha256"),
                field_name=f"markets[{index}].rules_sha256",
            ),
            resolution_source_sha256=_hash_pin(
                market.get("resolution_source_sha256"),
                field_name=(
                    f"markets[{index}].resolution_source_sha256"
                ),
            ),
        )
        for index, market_value in enumerate(market_rows)
        for market in [_mapping(
            market_value, field_name=f"markets[{index}]"
        )]
    )
    if not markets:
        raise ValueError("markets cannot be empty")
    identity_groups = {
        "node_id": tuple(market.node_id for market in markets),
        "market_id": tuple(market.market_id for market in markets),
        "condition_id": tuple(market.condition_id for market in markets),
        "token_id": tuple(
            token_id
            for market in markets
            for token_id in (market.yes_token_id, market.no_token_id)
        ),
    }
    for name, values in identity_groups.items():
        if len(set(values)) != len(values):
            raise ValueError(f"markets contain duplicate {name}")
    if set(event.expected_market_ids) != {
        market.market_id for market in markets
    }:
        raise ValueError(
            "event.expected_market_ids must exactly match markets[].market_id"
        )
    constraint_rows = _list(
        raw.get("constraints"), field_name="constraints"
    )
    constraints = tuple(
        ConstraintSpec(
            constraint_id=_text(
                item.get("constraint_id"),
                field_name=f"constraints[{index}].constraint_id",
            ),
            kind=_text(
                item.get("kind"),
                field_name=f"constraints[{index}].kind",
            ),
            node_ids=_strings(
                item.get("node_ids"),
                field_name=f"constraints[{index}].node_ids",
            ),
            verified=_boolean(
                item.get("verified"),
                field_name=f"constraints[{index}].verified",
            ),
            evidence=_strings(
                item.get("evidence"),
                field_name=f"constraints[{index}].evidence",
            ),
        )
        for index, item_value in enumerate(constraint_rows)
        for item in [_mapping(
            item_value, field_name=f"constraints[{index}]"
        )]
    )
    if not constraints:
        raise ValueError("constraints cannot be empty")
    node_ids = {market.node_id for market in markets}
    if any(
        node_id not in node_ids
        for constraint in constraints
        for node_id in constraint.node_ids
    ):
        raise ValueError("constraints reference an unknown node_id")
    markets_by_node = {market.node_id: market for market in markets}
    for index, constraint in enumerate(constraints):
        required_evidence = _required_constraint_evidence(
            event, markets_by_node, constraint
        )
        if constraint.evidence != required_evidence:
            raise ValueError(
                f"constraints[{index}].evidence must exactly bind the "
                "canonical constraint claim and pinned event market set"
            )
    analysis_rows = _list(raw.get("analyses"), field_name="analyses")
    analyses: list[AnalysisSpec] = []
    for analysis_index, analysis_value in enumerate(analysis_rows):
        analysis_raw = _mapping(
            analysis_value, field_name=f"analyses[{analysis_index}]"
        )
        mode = _text(
            analysis_raw.get("mode"),
            field_name=f"analyses[{analysis_index}].mode",
        )
        if mode not in _ALLOWED_MODES:
            raise ValueError(
                f"analyses[{analysis_index}].mode is unsupported"
            )
        bundles: list[BundleSpec] = []
        for bundle_index, bundle_value in enumerate(
            _list(
                analysis_raw.get("bundles", []),
                field_name=f"analyses[{analysis_index}].bundles",
            )
        ):
            bundle_raw = _mapping(
                bundle_value,
                field_name=(
                    f"analyses[{analysis_index}].bundles[{bundle_index}]"
                ),
            )
            legs = tuple(
                BundleLeg(
                    node_id=_text(
                        leg.get("node_id"),
                        field_name="bundle leg node_id",
                    ),
                    outcome=_text(
                        leg.get("outcome"),
                        field_name="bundle leg outcome",
                    ).upper(),
                    side=_text(
                        leg.get("side"), field_name="bundle leg side"
                    ).upper(),
                    units=_decimal(
                        leg.get("units", "1"),
                        field_name="bundle leg units",
                    ),
                )
                for leg_value in _list(
                    bundle_raw.get("legs"), field_name="bundle legs"
                )
                for leg in [_mapping(
                    leg_value, field_name="bundle leg"
                )]
            )
            if not legs:
                raise ValueError("bundle legs cannot be empty")
            if any(
                leg.node_id not in node_ids
                or leg.outcome not in {"YES", "NO"}
                or leg.side not in {"BUY", "SELL"}
                or not leg.units.is_finite()
                or leg.units <= 0
                for leg in legs
            ):
                raise ValueError("bundle contains an invalid leg")
            inventory_raw = _mapping(
                bundle_raw.get("inventory", {}),
                field_name="bundle inventory",
            )
            bundles.append(
                BundleSpec(
                    candidate_id=_text(
                        bundle_raw.get("candidate_id"),
                        field_name="bundle candidate_id",
                    ),
                    family=_text(
                        bundle_raw.get("family"),
                        field_name="bundle family",
                    ),
                    legs=legs,
                    quantities=_parse_quantities(
                        bundle_raw.get("quantities"),
                        field_name="bundle quantities",
                    ),
                    inventory=MappingProxyType(
                        {
                            _text(
                                token_id,
                                field_name="inventory token_id",
                            ): _decimal(
                                quantity,
                                field_name=(
                                    f"inventory[{token_id}]"
                                ),
                            )
                            for token_id, quantity in inventory_raw.items()
                        }
                    ),
                )
            )
        conversions: list[ConversionSpec] = []
        for conversion_index, conversion_value in enumerate(
            _list(
                analysis_raw.get("conversions", []),
                field_name=f"analyses[{analysis_index}].conversions",
            )
        ):
            conversion_raw = _mapping(
                conversion_value,
                field_name=(
                    f"analyses[{analysis_index}].conversions"
                    f"[{conversion_index}]"
                ),
            )
            selected = _strings(
                conversion_raw.get("selected_node_ids"),
                field_name="conversion selected_node_ids",
            )
            if not selected or any(node_id not in node_ids for node_id in selected):
                raise ValueError(
                    "conversion selected_node_ids must be known and non-empty"
                )
            conversions.append(
                ConversionSpec(
                    candidate_id=_text(
                        conversion_raw.get("candidate_id"),
                        field_name="conversion candidate_id",
                    ),
                    selected_node_ids=selected,
                    quantities=_parse_quantities(
                        conversion_raw.get("quantities"),
                        field_name="conversion quantities",
                    ),
                )
            )
        provenance = _parse_provenance(
            analysis_raw.get("neg_risk_provenance", {}),
            field_name=f"analyses[{analysis_index}].neg_risk_provenance",
        )
        if conversions and mode != "standard_neg_risk":
            raise ValueError(
                "Neg-Risk conversions require mode=standard_neg_risk"
            )
        analyses.append(
            AnalysisSpec(
                analysis_id=_text(
                    analysis_raw.get("analysis_id"),
                    field_name=f"analyses[{analysis_index}].analysis_id",
                ),
                mode=mode,
                bundles=tuple(bundles),
                conversions=tuple(conversions),
                neg_risk_provenance=provenance,
            )
        )
    if not analyses or all(
        not analysis.bundles and not analysis.conversions
        for analysis in analyses
    ):
        raise ValueError("analyses must define at least one candidate")
    analysis_ids = tuple(analysis.analysis_id for analysis in analyses)
    if len(set(analysis_ids)) != len(analysis_ids):
        raise ValueError("analysis_id values must be unique")
    candidate_ids = tuple(
        candidate.candidate_id
        for analysis in analyses
        for candidate in (*analysis.bundles, *analysis.conversions)
    )
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate_id values must be globally unique")
    policy_raw = _mapping(raw.get("policy"), field_name="policy")
    policy = SnapshotPolicy(
        gas=_decimal(policy_raw.get("gas"), field_name="policy.gas"),
        latency_buffer=_decimal(
            policy_raw.get("latency_buffer"),
            field_name="policy.latency_buffer",
        ),
        max_book_age_ms=_integer(
            policy_raw.get("max_book_age_ms"),
            field_name="policy.max_book_age_ms",
        ),
        max_book_skew_ms=_integer(
            policy_raw.get("max_book_skew_ms"),
            field_name="policy.max_book_skew_ms",
        ),
        max_book_rtt_ms=_integer(
            policy_raw.get("max_book_rtt_ms"),
            field_name="policy.max_book_rtt_ms",
        ),
    )
    if policy.gas < 0 or policy.latency_buffer < 0:
        raise ValueError("gas and latency_buffer cannot be negative")
    canonical_document = json.loads(_canonical_json(raw))
    return ConstraintExperimentSpec(
        experiment_id=experiment_id,
        event=event,
        markets=markets,
        constraints=constraints,
        analyses=tuple(analyses),
        policy=policy,
        config_document=canonical_document,
        config_sha256=_digest(_canonical_json(canonical_document)),
    )


class ConstraintExperimentSource(Protocol):
    def gamma_event(
        self, event_id: str
    ) -> Fetched[Mapping[str, Any]]: ...

    def resolution_rules(
        self, market_id: str
    ) -> Fetched[ResolutionRules]: ...

    def clob_market(
        self, condition_id: str
    ) -> Fetched[CompactCLOBMarket]: ...

    def book(self, token_id: str) -> Fetched[SourceBook]: ...


def _official_public_url(url: str) -> bool:
    parsed = urlsplit(url)
    path_allowed = (
        parsed.hostname == "gamma-api.polymarket.com"
        and (
            parsed.path.startswith("/events/")
            or parsed.path.startswith("/markets/")
        )
    ) or (
        parsed.hostname == "clob.polymarket.com"
        and (
            parsed.path.startswith("/clob-markets/")
            or parsed.path == "/book"
        )
    )
    query_keys = {
        key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    }
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and path_allowed
        and not any(
            _credential_like_key(key) for key in query_keys
        )
    )


class PublicGETSession(requests.Session):
    """A requests session that enforces the runner's public GET boundary."""

    _edge_public_get_enforced = True

    def __init__(self) -> None:
        super().__init__()
        self.trust_env = False
        self.max_redirects = 0

    def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        if method.upper() != "GET" or not _official_public_url(url):
            raise requests.RequestException(
                "constraint runner rejected a non-public GET before dispatch"
            )
        params = kwargs.get("params") or {}
        if not isinstance(params, Mapping):
            raise requests.RequestException(
                "constraint runner request params must be a mapping"
            )
        for key in params:
            if _credential_like_key(key):
                raise requests.RequestException(
                    "constraint runner rejected a sensitive request parameter"
                )
        kwargs["allow_redirects"] = False
        response = super().request(method, url, **kwargs)
        if response.history or not _official_public_url(response.url):
            response.close()
            raise requests.RequestException(
                "constraint runner received a non-public final URL"
            )
        return response


class PublicConstraintSources:
    """Public GET-only adapter for the three resources used by this runner."""

    network_used = True

    def __init__(
        self,
        client: PublicSourcesClient,
        *,
        session_factory: Callable[[], requests.Session] = PublicGETSession,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        if isinstance(client.session, requests.Session) and not getattr(
            client.session, "_edge_public_get_enforced", False
        ):
            raise PublicSourceError(
                "network sources require a PublicGETSession"
            )
        # requests follows redirects by default while PublicSourcesClient
        # records only the requested URL.  A zero redirect budget makes an
        # official-host 30x fail before any non-allowlisted destination is
        # contacted.
        self.client.session.max_redirects = 0
        self.session_factory = session_factory
        self.monotonic = monotonic
        self.sleeper = sleeper
        self._book_cache: dict[
            str, Fetched[SourceBook] | Exception
        ] = {}
        self._validate_proxies()

    def _validate_proxies(self) -> None:
        for proxy_url in dict(self.client.session.proxies).values():
            parsed = urlsplit(str(proxy_url))
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname
                not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise PublicSourceError(
                    "public sources require an unauthenticated loopback "
                    "HTTP(S) proxy or direct HTTPS"
                )

    def gamma_event(
        self, event_id: str
    ) -> Fetched[Mapping[str, Any]]:
        if not event_id:
            raise ValueError("event_id cannot be empty")
        # PublicSourcesClient deliberately centralizes credential/session and
        # GET-only enforcement.  Its protected GET primitive is reused here
        # because the base client currently exposes single-market, not
        # single-event, Gamma reads.
        raw = self.client._get(  # noqa: SLF001
            f"{GAMMA_BASE}/events/{quote(event_id, safe='')}",
            source="gamma_event",
        )
        try:
            value = json.loads(raw.text, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise PublicSourceError(
                "Gamma event returned invalid JSON", raw=raw
            ) from exc
        if not isinstance(value, Mapping) or str(value.get("id")) != event_id:
            raise PublicSourceError(
                "Gamma event id does not match request", raw=raw
            )
        return Fetched(raw=raw, value=MappingProxyType(dict(value)))

    def resolution_rules(
        self, market_id: str
    ) -> Fetched[ResolutionRules]:
        fetched = self.client.gamma_market(market_id)
        market = fetched.value
        try:
            description = str(market["description"])
            question = str(market["question"])
            condition_id = str(market["conditionId"])
            end_date = str(market["endDate"])
            closed = market["closed"]
            if (
                not description.strip()
                or not question.strip()
                or not condition_id.strip()
                or not end_date.strip()
                or not isinstance(closed, bool)
            ):
                raise ValueError("required rule-bearing field is invalid")
            rules_value = market.get("rules")
            rules_text = (
                description
                if rules_value in (None, "")
                else str(rules_value)
            )
            value = ResolutionRules(
                market_id=str(market["id"]),
                condition_id=condition_id,
                question_id=(
                    None
                    if market.get("questionID") in (None, "")
                    else str(market["questionID"])
                ),
                question=question,
                description=description,
                rules_text=rules_text,
                # Empty is deliberately preserved.  The runner records
                # resolution_source_missing and the graph then fails closed,
                # while still retaining books and all rejected candidates.
                resolution_source=str(market.get("resolutionSource") or ""),
                end_date=end_date,
                resolved_by=(
                    None
                    if market.get("resolvedBy") in (None, "")
                    else str(market["resolvedBy"])
                ),
                closed=closed,
                uma_resolution_status=(
                    None
                    if market.get("umaResolutionStatus") in (None, "")
                    else str(market["umaResolutionStatus"])
                ),
                raw=market,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PublicSourceError(
                "Gamma resolution market lacks valid rule-bearing fields",
                raw=fetched.raw,
            ) from exc
        return Fetched(raw=fetched.raw, value=value)

    def clob_market(
        self, condition_id: str
    ) -> Fetched[CompactCLOBMarket]:
        return self.client.clob_market(condition_id)

    def _isolated_book_get(
        self,
        token_id: str,
        *,
        due_at: float,
    ) -> Fetched[SourceBook]:
        delay = due_at - self.monotonic()
        if delay > 0:
            self.sleeper(delay)
        session = self.session_factory()
        session.trust_env = False
        session.max_redirects = 0
        session.proxies.update(dict(self.client.session.proxies))
        isolated = PublicSourcesClient(
            session=session,
            timeout=self.client.timeout,
            retries=self.client.retries,
            backoff_base_seconds=self.client.backoff_base_seconds,
            jitter_seconds=self.client.jitter_seconds,
            # Each isolated client performs one logical GET.  Dispatch timing
            # is governed by the shared schedule in prefetch_books.
            rate_per_second=1_000_000,
            burst=1,
            min_interval_seconds=0,
        )
        try:
            return isolated.book(token_id)
        finally:
            session.close()

    def prefetch_books(self, token_ids: Sequence[str]) -> None:
        """Fetch one synchronized GET-only book fanout with isolated sessions."""

        normalized = tuple(dict.fromkeys(str(token_id) for token_id in token_ids))
        if not normalized or any(not token_id for token_id in normalized):
            raise ValueError("prefetch token_ids must be non-empty")
        self._validate_proxies()
        spacing = max(
            1.0 / self.client.rate_per_second,
            self.client.min_interval_seconds,
        )
        started = self.monotonic()
        max_workers = min(8, len(normalized))
        cache: dict[str, Fetched[SourceBook] | Exception] = {}
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="edge-book-get",
        ) as executor:
            futures = {
                executor.submit(
                    self._isolated_book_get,
                    token_id,
                    due_at=started + index * spacing,
                ): token_id
                for index, token_id in enumerate(normalized)
            }
            for future in as_completed(futures):
                token_id = futures[future]
                try:
                    cache[token_id] = future.result()
                except Exception as exc:
                    cache[token_id] = exc
        self._book_cache.update(cache)

    def book(self, token_id: str) -> Fetched[SourceBook]:
        cached = self._book_cache.get(token_id)
        if isinstance(cached, Exception):
            raise cached
        if cached is not None:
            return cached
        return self.client.book(token_id)


class _RecordedResponse:
    def __init__(self, raw: RawResponse) -> None:
        self.content = raw.body
        self.status_code = raw.metadata.status_code
        self.headers: dict[str, str] = {}


class _RecordedResponseSession:
    """One-response requests-compatible parser shim; it never uses network."""

    def __init__(self, raw: RawResponse) -> None:
        self.raw = raw
        self.headers: dict[str, str] = {}
        self.auth = None
        self.proxies: dict[str, str] = {}
        self.trust_env = False
        self.max_redirects = 0
        self.used = False

    def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> _RecordedResponse:
        if self.used:
            raise AssertionError("recorded parser session can be used once")
        if method.upper() != self.raw.metadata.method.upper():
            raise AssertionError("recorded method does not match parser request")
        if url != self.raw.metadata.url:
            raise AssertionError("recorded URL does not match parser request")
        params = kwargs.get("params", {})
        if dict(params if isinstance(params, Mapping) else {}) != dict(
            self.raw.metadata.request_params
        ):
            raise AssertionError(
                "recorded parameters do not match parser request"
            )
        self.used = True
        return _RecordedResponse(self.raw)

    def close(self) -> None:
        return None


class RecordedConstraintSources:
    """Replay one immutable run's exact raw bodies without network access."""

    network_used = False

    def __init__(
        self,
        source_run: Path,
        *,
        expected_reproducibility_sha256: str,
    ) -> None:
        self.source_run = source_run.expanduser().resolve()
        expected_reproducibility_sha256 = _hash_pin(
            expected_reproducibility_sha256,
            field_name="expected_reproducibility_sha256",
        )
        reproducibility_path = self.source_run / "REPRODUCIBILITY.json"
        reproducibility_bytes = reproducibility_path.read_bytes()
        actual_reproducibility_sha256 = _digest(reproducibility_bytes)
        if (
            actual_reproducibility_sha256
            != expected_reproducibility_sha256
        ):
            raise ValueError(
                "source REPRODUCIBILITY.json hash does not match the "
                "external replay trust anchor"
            )
        reproducibility = _mapping(
            json.loads(reproducibility_bytes),
            field_name="source reproducibility",
        )
        artifact_sha256 = _mapping(
            reproducibility.get("artifact_sha256"),
            field_name="source reproducibility.artifact_sha256",
        )
        required_artifacts = {
            "candidates.jsonl",
            "summary.json",
            "graphs.json",
            "source_manifest.json",
            "config.snapshot.json",
        }
        if set(artifact_sha256) != required_artifacts:
            raise ValueError(
                "source reproducibility artifact set is incomplete"
            )
        for filename in sorted(required_artifacts):
            expected_sha256 = _hash_pin(
                artifact_sha256[filename],
                field_name=(
                    f"source reproducibility.artifact_sha256.{filename}"
                ),
            )
            actual_sha256 = _digest(
                (self.source_run / filename).read_bytes()
            )
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    f"source artifact hash mismatch: {filename}"
                )
        source_implementation = _mapping(
            reproducibility.get("implementation_provenance"),
            field_name="source implementation provenance",
        )
        current_implementation = _implementation_provenance()
        if _canonical_json(source_implementation) != _canonical_json(
            current_implementation
        ):
            raise ValueError(
                "source run implementation does not match current replay "
                "implementation"
            )
        source_summary = json.loads(
            (self.source_run / "summary.json").read_text(encoding="utf-8")
        )
        source_summary_mapping = _mapping(
            source_summary, field_name="source summary"
        )
        source_run_id = _text(
            reproducibility.get("run_id"),
            field_name="source reproducibility.run_id",
        )
        if (
            source_run_id != self.source_run.name
            or source_summary_mapping.get("run_id") != source_run_id
        ):
            raise ValueError("source run identity does not match its directory")
        source_config_sha256 = _hash_pin(
            reproducibility.get("config_sha256"),
            field_name="source reproducibility.config_sha256",
        )
        if source_summary_mapping.get("config_sha256") != (
            source_config_sha256
        ):
            raise ValueError(
                "source summary and reproducibility config hashes differ"
            )
        self.source_config_sha256 = source_config_sha256
        self.replay_lineage = {
            "schema_version": "edge-lab-replay-lineage.v1",
            "source_run_id": source_run_id,
            "source_reproducibility_sha256": (
                actual_reproducibility_sha256
            ),
            "source_artifact_sha256": dict(artifact_sha256),
            "source_implementation_provenance": dict(
                source_implementation
            ),
        }
        acquisition = _mapping(
            source_summary_mapping.get("book_acquisition"),
            field_name="source summary.book_acquisition",
        )
        self.recorded_observed_at_ms = _integer(
            acquisition.get("observed_at_ms"),
            field_name="source summary.book_acquisition.observed_at_ms",
        )
        manifest_path = self.source_run / "source_manifest.json"
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest_value, list):
            raise ValueError("recorded source manifest must be an array")
        self._raw_by_scope: dict[str, RawResponse] = {}
        for index, entry_value in enumerate(manifest_value):
            entry = _mapping(
                entry_value,
                field_name=f"source_manifest[{index}]",
            )
            scope = _text(
                entry.get("scope"),
                field_name=f"source_manifest[{index}].scope",
            )
            if scope in self._raw_by_scope:
                raise ValueError(
                    f"recorded source manifest duplicates scope: {scope}"
                )
            body_relative = Path(
                _text(
                    entry.get("body_path"),
                    field_name=f"source_manifest[{index}].body_path",
                )
            )
            body_path = (self.source_run / body_relative).resolve()
            if not body_path.is_relative_to(self.source_run):
                raise ValueError("recorded body path escapes source run")
            body = body_path.read_bytes()
            body_sha256 = _hash_pin(
                entry.get("body_sha256"),
                field_name=f"source_manifest[{index}].body_sha256",
            )
            if _digest(body) != body_sha256:
                raise ValueError(
                    f"recorded body hash mismatch for scope: {scope}"
                )
            method = _text(
                entry.get("method"),
                field_name=f"source_manifest[{index}].method",
            ).upper()
            url = _text(
                entry.get("url"),
                field_name=f"source_manifest[{index}].url",
            )
            request_params = _mapping(
                entry.get("request_params"),
                field_name=f"source_manifest[{index}].request_params",
            )
            parsed = urlsplit(url)
            allowed = (
                method == "GET"
                and parsed.scheme == "https"
                and parsed.hostname
                in {"gamma-api.polymarket.com", "clob.polymarket.com"}
                and parsed.username is None
                and parsed.password is None
                and entry.get("public_get_valid", True) is True
            )
            if not allowed:
                raise ValueError(
                    f"recorded source is outside public GET boundary: {scope}"
                )
            for key in request_params:
                if _credential_like_key(key):
                    raise ValueError(
                        "recorded source contains a sensitive parameter"
                    )
            metadata = FetchMetadata(
                source=_text(
                    entry.get("source"),
                    field_name=f"source_manifest[{index}].source",
                ),
                method=method,
                url=url,
                request_params=MappingProxyType(dict(request_params)),
                status_code=_integer(
                    entry.get("status_code"),
                    field_name=f"source_manifest[{index}].status_code",
                    minimum=100,
                ),
                requested_at=float(
                    _decimal(
                        entry.get("requested_at"),
                        field_name=(
                            f"source_manifest[{index}].requested_at"
                        ),
                    )
                ),
                received_at=float(
                    _decimal(
                        entry.get("received_at"),
                        field_name=f"source_manifest[{index}].received_at",
                    )
                ),
                attempt=_integer(
                    entry.get("attempt"),
                    field_name=f"source_manifest[{index}].attempt",
                    minimum=1,
                ),
                response_headers=MappingProxyType({}),
            )
            self._raw_by_scope[scope] = RawResponse(
                body=body,
                text=body.decode("utf-8"),
                metadata=metadata,
            )

    def _parse(
        self,
        scope: str,
        call: Callable[[PublicConstraintSources], Fetched[Any]],
    ) -> Fetched[Any]:
        try:
            raw = self._raw_by_scope[scope]
        except KeyError as exc:
            raise PublicSourceError(
                f"recorded source scope is missing: {scope}"
            ) from exc
        session = _RecordedResponseSession(raw)
        client = PublicSourcesClient(
            session=session,
            timeout=1,
            retries=0,
            rate_per_second=1_000_000,
            burst=1,
            min_interval_seconds=0,
            clock=lambda: 0.0,
            sleeper=lambda _: None,
        )
        try:
            parsed = call(PublicConstraintSources(client))
        except PublicSourceError as exc:
            raise PublicSourceError(str(exc), raw=raw) from exc
        if not session.used:
            raise AssertionError("recorded parser did not consume its response")
        return Fetched(raw=raw, value=parsed.value)

    def gamma_event(
        self, event_id: str
    ) -> Fetched[Mapping[str, Any]]:
        return self._parse(
            f"event:{event_id}",
            lambda source: source.gamma_event(event_id),
        )

    def resolution_rules(
        self, market_id: str
    ) -> Fetched[ResolutionRules]:
        return self._parse(
            f"rules:{market_id}",
            lambda source: source.resolution_rules(market_id),
        )

    def clob_market(
        self, condition_id: str
    ) -> Fetched[CompactCLOBMarket]:
        return self._parse(
            f"clob-market:{condition_id}",
            lambda source: source.clob_market(condition_id),
        )

    def prefetch_books(self, token_ids: Sequence[str]) -> None:
        for token_id in token_ids:
            if f"book:{token_id}" not in self._raw_by_scope:
                raise PublicSourceError(
                    f"recorded source scope is missing: book:{token_id}"
                )

    def book(self, token_id: str) -> Fetched[SourceBook]:
        return self._parse(
            f"book:{token_id}",
            lambda source: source.book(token_id),
        )


class _EvidenceWriter:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.raw_dir = run_dir / "raw"
        self.raw_dir.mkdir()
        self.manifest: list[dict[str, object]] = []

    def preserve(
        self, raw: RawResponse, *, scope: str
    ) -> tuple[str, tuple[str, ...]]:
        failures: list[str] = []
        metadata = raw.metadata
        if metadata.method.upper() != "GET":
            _append_failure(failures, "source_method_not_get")
        parsed = urlsplit(metadata.url)
        allowed_path = (
            parsed.hostname == "gamma-api.polymarket.com"
            and (
                parsed.path.startswith("/events/")
                or parsed.path.startswith("/markets/")
            )
        ) or (
            parsed.hostname == "clob.polymarket.com"
            and (
                parsed.path.startswith("/clob-markets/")
                or parsed.path == "/book"
            )
        )
        if (
            parsed.scheme != "https"
            or parsed.hostname
            not in {"gamma-api.polymarket.com", "clob.polymarket.com"}
            or parsed.username is not None
            or parsed.password is not None
            or not allowed_path
        ):
            _append_failure(failures, "source_url_not_public_allowlist")
        query_keys = {
            key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        }
        sensitive_query = any(
            _credential_like_key(key) for key in query_keys
        )
        if sensitive_query:
            _append_failure(failures, "source_url_sensitive_query")
        if not 200 <= metadata.status_code < 300:
            _append_failure(failures, "source_http_status_invalid")
        if (
            not math.isfinite(metadata.requested_at)
            or not math.isfinite(metadata.received_at)
            or metadata.received_at < metadata.requested_at
        ):
            _append_failure(failures, "source_timestamp_invalid")
        sensitive_parameter_keys: set[str] = set()
        for key in metadata.request_params:
            if _credential_like_key(key):
                sensitive_parameter_keys.add(str(key))
                _append_failure(failures, "source_request_parameter_sensitive")
        safe_url = metadata.url
        if sensitive_query:
            safe_query = urlencode(
                [
                    (
                        key,
                        (
                            "[REDACTED]"
                            if _credential_like_key(key)
                            else value
                        ),
                    )
                    for key, value in parse_qsl(
                        parsed.query, keep_blank_values=True
                    )
                ]
            )
            safe_url = urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    safe_query,
                    parsed.fragment,
                )
            )
        safe_request_params = {
            str(key): (
                "[REDACTED]"
                if str(key) in sensitive_parameter_keys
                else value
            )
            for key, value in metadata.request_params.items()
        }
        boundary_failure_codes = {
            "source_method_not_get",
            "source_url_not_public_allowlist",
            "source_url_sensitive_query",
            "source_request_parameter_sensitive",
        }
        body_sha = _digest(raw.body)
        body_path = self.raw_dir / f"{body_sha}.json"
        if body_path.exists():
            if body_path.read_bytes() != raw.body:
                raise RuntimeError("raw body hash collision")
        else:
            with body_path.open("xb") as handle:
                handle.write(raw.body)
        source_ref = f"{metadata.source}:sha256:{body_sha}"
        self.manifest.append(
            {
                "scope": scope,
                "source_ref": source_ref,
                "source": metadata.source,
                "method": metadata.method.upper(),
                "url": safe_url,
                "request_params": safe_request_params,
                "public_get_valid": not any(
                    code in boundary_failure_codes for code in failures
                ),
                "status_code": metadata.status_code,
                "requested_at": repr(metadata.requested_at),
                "received_at": repr(metadata.received_at),
                "attempt": metadata.attempt,
                "body_sha256": body_sha,
                "body_bytes": len(raw.body),
                "body_path": str(body_path.relative_to(self.run_dir)),
            }
        )
        return source_ref, tuple(failures)


@dataclass
class _Snapshot:
    nodes: dict[str, PredicateNode]
    books: dict[str, OrderBook]
    book_evidence: dict[str, BookSnapshotEvidence]
    fee_schedules: dict[str, FeeSchedule]
    fee_sources: dict[str, str]
    failures: list[str]
    semantic_failures: list[str]
    source_refs: list[str]
    event_source_ref: Optional[str]
    rule_source_refs: dict[str, str]
    observed_at_ms: int


def _event_market_ids(event: Mapping[str, Any]) -> tuple[str, ...]:
    markets = event.get("markets")
    if not isinstance(markets, (list, tuple)):
        return ()
    result: list[str] = []
    for market in markets:
        if isinstance(market, Mapping) and market.get("id") not in (None, ""):
            result.append(str(market["id"]))
    return tuple(result)


def _gamma_tokens(
    raw: Mapping[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    token_ids = raw.get("clobTokenIds")
    outcomes = raw.get("outcomes")
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except json.JSONDecodeError:
            return None, None
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(token_ids, (list, tuple)) or not isinstance(
        outcomes, (list, tuple)
    ):
        return None, None
    if len(token_ids) != 2 or len(outcomes) != 2:
        return None, None
    mapping = {
        str(outcome).strip().lower(): str(token_id)
        for token_id, outcome in zip(token_ids, outcomes, strict=True)
    }
    return mapping.get("yes"), mapping.get("no")


def _compact_tokens(
    compact: CompactCLOBMarket,
) -> tuple[Optional[str], Optional[str]]:
    mapping: dict[str, str] = {}
    for token in compact.tokens:
        token_id = token.get("t", token.get("token_id"))
        outcome = token.get("o", token.get("outcome"))
        if token_id not in (None, "") and outcome not in (None, ""):
            mapping[str(outcome).strip().lower()] = str(token_id)
    return mapping.get("yes"), mapping.get("no")


def _gamma_fee_schedule(
    raw: Mapping[str, Any],
) -> Optional[FeeSchedule]:
    value = raw.get("feeSchedule")
    if not isinstance(value, Mapping):
        return None
    try:
        taker_only = value["takerOnly"]
        if not isinstance(taker_only, bool):
            return None
        schedule = FeeSchedule(
            rate=Decimal(str(value["rate"])),
            exponent=Decimal(str(value["exponent"])),
            taker_only=taker_only,
            rebate_rate=Decimal(str(value.get("rebateRate", 0))),
        )
    except (KeyError, ArithmeticError, ValueError):
        return None
    if (
        not schedule.rate.is_finite()
        or not schedule.exponent.is_finite()
        or not schedule.rebate_rate.is_finite()
        or not Decimal("0") <= schedule.rate <= Decimal("1")
        or schedule.exponent <= 0
        or not Decimal("0") <= schedule.rebate_rate <= Decimal("1")
    ):
        return None
    return schedule


def _fetch_with_evidence(
    fetch: Callable[[], Fetched[Any]],
    *,
    writer: _EvidenceWriter,
    scope: str,
    failures: list[str],
) -> Optional[Fetched[Any]]:
    try:
        fetched = fetch()
    except PublicSourceError as exc:
        if exc.raw is not None:
            source_ref, raw_failures = writer.preserve(exc.raw, scope=scope)
            _extend_failures(failures, raw_failures)
            _append_failure(failures, f"source_fetch_failed:{scope}:{source_ref}")
        else:
            _append_failure(failures, f"source_fetch_failed:{scope}")
        return None
    except Exception as exc:
        _append_failure(
            failures,
            f"source_fetch_failed:{scope}:{type(exc).__name__}",
        )
        return None
    source_ref, raw_failures = writer.preserve(fetched.raw, scope=scope)
    _extend_failures(failures, raw_failures)
    return fetched


def _source_ref(fetched: Fetched[Any]) -> str:
    # Fetched is frozen, so normal setattr cannot be relied on.  Derive the
    # stable reference directly from the exact raw body.
    return f"{fetched.raw.metadata.source}:sha256:{_digest(fetched.raw.body)}"


def _fetch_snapshot(
    spec: ConstraintExperimentSpec,
    *,
    source: ConstraintExperimentSource,
    writer: _EvidenceWriter,
    clock_ms: Callable[[], int],
) -> _Snapshot:
    failures: list[str] = []
    semantic_failures: list[str] = []
    source_refs: list[str] = []
    event_source_ref: Optional[str] = None
    rule_source_refs: dict[str, str] = {}
    event_fetched = _fetch_with_evidence(
        lambda: source.gamma_event(spec.event.event_id),
        writer=writer,
        scope=f"event:{spec.event.event_id}",
        failures=failures,
    )
    if event_fetched is None:
        _append_failure(semantic_failures, "event_source_missing")
    else:
        event_source_ref = _source_ref(event_fetched)
        source_refs.append(event_source_ref)
        event = event_fetched.value
        observed_ids = _event_market_ids(event)
        if set(observed_ids) != set(spec.event.expected_market_ids) or len(
            observed_ids
        ) != len(spec.event.expected_market_ids):
            _append_failure(semantic_failures, "event_market_set_mismatch")
        if spec.event.require_active and event.get("active") is not True:
            _append_failure(semantic_failures, "event_not_active")
        if spec.event.require_open and event.get("closed") is not False:
            _append_failure(semantic_failures, "event_not_open")
        if event.get("negRisk") is not spec.event.expected_neg_risk:
            _append_failure(semantic_failures, "event_neg_risk_flag_mismatch")
        if (
            event.get("negRiskAugmented")
            is not spec.event.expected_augmented_neg_risk
        ):
            _append_failure(
                semantic_failures,
                "event_augmented_neg_risk_flag_mismatch",
            )

    nodes: dict[str, PredicateNode] = {}
    books: dict[str, OrderBook] = {}
    book_evidence: dict[str, BookSnapshotEvidence] = {}
    fee_schedules: dict[str, FeeSchedule] = {}
    fee_sources: dict[str, str] = {}
    book_targets: list[tuple[MarketPin, str, CompactCLOBMarket]] = []
    for pin in spec.markets:
        node_failures: list[str] = []
        rules_fetched = _fetch_with_evidence(
            lambda pin=pin: source.resolution_rules(pin.market_id),
            writer=writer,
            scope=f"rules:{pin.market_id}",
            failures=failures,
        )
        if rules_fetched is None:
            _append_failure(semantic_failures, "market_rules_source_missing")
            continue
        rule_source_ref = _source_ref(rules_fetched)
        source_refs.append(rule_source_ref)
        rule_source_refs[pin.node_id] = rule_source_ref
        rules = rules_fetched.value
        if rules.market_id != pin.market_id:
            _append_failure(node_failures, "market_id_mismatch")
        if rules.condition_id != pin.condition_id:
            _append_failure(node_failures, "condition_id_mismatch")
        if _digest(rules.question) != pin.question_sha256:
            _append_failure(node_failures, "question_hash_mismatch")
        if _digest(rules.rules_text) != pin.rules_sha256:
            _append_failure(node_failures, "rules_hash_mismatch")
        if (
            _digest(rules.resolution_source)
            != pin.resolution_source_sha256
        ):
            _append_failure(
                node_failures, "resolution_source_hash_mismatch"
            )
        if not rules.resolution_source.strip():
            _append_failure(node_failures, "resolution_source_missing")
        gamma_yes, gamma_no = _gamma_tokens(rules.raw)
        if (
            gamma_yes != pin.yes_token_id
            or gamma_no != pin.no_token_id
        ):
            _append_failure(node_failures, "gamma_token_mapping_mismatch")
        if rules.closed:
            _append_failure(node_failures, "gamma_market_closed")
        nodes[pin.node_id] = PredicateNode(
            node_id=pin.node_id,
            event_id=spec.event.event_id,
            market_id=pin.market_id,
            condition_id=rules.condition_id,
            yes_token_id=pin.yes_token_id,
            no_token_id=pin.no_token_id,
            question=rules.question,
            rules=rules.rules_text,
            resolution_source=rules.resolution_source,
            role=pin.role,
            evidence=pin.stable_evidence,
        )
        if node_failures:
            _extend_failures(semantic_failures, node_failures)

        compact_fetched = _fetch_with_evidence(
            lambda pin=pin: source.clob_market(pin.condition_id),
            writer=writer,
            scope=f"clob-market:{pin.condition_id}",
            failures=failures,
        )
        if compact_fetched is None:
            _append_failure(failures, "compact_market_missing")
            continue
        source_refs.append(_source_ref(compact_fetched))
        compact = compact_fetched.value
        if compact.condition_id != pin.condition_id:
            _append_failure(failures, "compact_condition_mismatch")
        compact_yes, compact_no = _compact_tokens(compact)
        if (
            compact_yes != pin.yes_token_id
            or compact_no != pin.no_token_id
        ):
            _append_failure(failures, "compact_token_mapping_mismatch")
        if not compact.accepting_orders:
            _append_failure(failures, "compact_market_not_accepting_orders")
        gamma_fee = _gamma_fee_schedule(rules.raw)
        compact_fee = compact.fees
        schedule: Optional[FeeSchedule] = None
        if compact_fee is None:
            _append_failure(failures, "fee_unknown")
        else:
            schedule = FeeSchedule(
                rate=compact_fee.rate,
                exponent=compact_fee.exponent,
                taker_only=compact_fee.taker_only,
                rebate_rate=(
                    Decimal("0")
                    if gamma_fee is None
                    else gamma_fee.rebate_rate
                ),
            )
            if gamma_fee is not None and (
                gamma_fee.rate != schedule.rate
                or gamma_fee.exponent != schedule.exponent
                or gamma_fee.taker_only != schedule.taker_only
            ):
                _append_failure(failures, "fee_source_conflict")
                schedule = None
        fee_ref = (
            f"{_source_ref(compact_fetched)};"
            f"{_source_ref(rules_fetched)}"
        )
        for token_id in (pin.yes_token_id, pin.no_token_id):
            if schedule is not None:
                fee_schedules[token_id] = schedule
                fee_sources[token_id] = fee_ref
            book_targets.append((pin, token_id, compact))

    prefetch = getattr(source, "prefetch_books", None)
    if callable(prefetch) and book_targets:
        try:
            prefetch([token_id for _, token_id, _ in book_targets])
        except Exception as exc:
            _append_failure(
                failures,
                f"book_prefetch_failed:{type(exc).__name__}",
            )
    for pin, token_id, compact in book_targets:
        book_fetched = _fetch_with_evidence(
            lambda token_id=token_id: source.book(token_id),
            writer=writer,
            scope=f"book:{token_id}",
            failures=failures,
        )
        if book_fetched is None:
            _append_failure(failures, "book_missing")
            continue
        source_refs.append(_source_ref(book_fetched))
        book = book_fetched.value
        raw_book = book.raw
        if str(raw_book.get("market", "")) != pin.condition_id:
            _append_failure(failures, "book_condition_mismatch")
        try:
            raw_tick = (
                None
                if raw_book.get("tick_size") in (None, "")
                else Decimal(str(raw_book["tick_size"]))
            )
        except (ArithmeticError, ValueError):
            raw_tick = None
            _append_failure(failures, "book_tick_source_invalid")
        if raw_tick is not None and not raw_tick.is_finite():
            raw_tick = None
            _append_failure(failures, "book_tick_source_invalid")
        if raw_tick is not None and raw_tick != compact.tick_size:
            _append_failure(failures, "book_tick_source_conflict")
        try:
            raw_minimum = (
                None
                if raw_book.get("min_order_size") in (None, "")
                else Decimal(str(raw_book["min_order_size"]))
            )
        except (ArithmeticError, ValueError):
            raw_minimum = None
            _append_failure(failures, "book_min_size_source_invalid")
        if raw_minimum is not None and not raw_minimum.is_finite():
            raw_minimum = None
            _append_failure(failures, "book_min_size_source_invalid")
        if (
            raw_minimum is not None
            and raw_minimum != compact.min_order_size
        ):
            _append_failure(failures, "book_min_size_source_conflict")
        if raw_book.get("neg_risk") not in (
            None,
            spec.event.expected_neg_risk,
        ):
            _append_failure(failures, "book_neg_risk_flag_mismatch")
        books[token_id] = OrderBook(
            token_id=book.token_id,
            bids=tuple(
                BookLevel(level.price, level.size) for level in book.bids
            ),
            asks=tuple(
                BookLevel(level.price, level.size) for level in book.asks
            ),
            timestamp_ms=book.timestamp_ms,
            condition_id=pin.condition_id,
            tick_size=compact.tick_size,
            min_order_size=compact.min_order_size,
            neg_risk=bool(raw_book.get("neg_risk", False)),
        )
        requested = book_fetched.raw.metadata.requested_at
        received = book_fetched.raw.metadata.received_at
        book_evidence[token_id] = BookSnapshotEvidence(
            book_hash=_digest(book_fetched.raw.body),
            received_at_ms=int(
                (Decimal(str(received)) * Decimal("1000")).to_integral_value(
                    rounding=ROUND_CEILING
                )
            ),
            rtt_ms=max(0, math.ceil((received - requested) * 1000)),
        )
    _extend_failures(failures, semantic_failures)
    observed_at_ms = (
        max(
            evidence.received_at_ms
            for evidence in book_evidence.values()
        )
        if book_evidence
        else int(
            getattr(source, "recorded_observed_at_ms", clock_ms())
        )
    )
    return _Snapshot(
        nodes=nodes,
        books=books,
        book_evidence=book_evidence,
        fee_schedules=fee_schedules,
        fee_sources=fee_sources,
        failures=failures,
        semantic_failures=semantic_failures,
        source_refs=source_refs,
        event_source_ref=event_source_ref,
        rule_source_refs=rule_source_refs,
        observed_at_ms=observed_at_ms,
    )


def _graph(
    spec: ConstraintExperimentSpec,
    analysis: AnalysisSpec,
    snapshot: _Snapshot,
) -> ConstraintGraph:
    semantics_verified = not snapshot.semantic_failures
    markets_by_node = {market.node_id: market for market in spec.markets}
    constraints: list[Constraint] = []
    for item in spec.constraints:
        source_refs = tuple(
            ref
            for ref in (
                snapshot.event_source_ref,
                *(
                    snapshot.rule_source_refs.get(node_id)
                    for node_id in item.node_ids
                ),
            )
            if ref is not None
        )
        source_provenance_complete = (
            len(source_refs) == len(item.node_ids) + 1
        )
        # Gamma/CLOB prove the event, rule text, market identifiers, and
        # books.  They do not prove an adapter deployment/block/fee.  Until
        # such an authority is added as an exact saved source, config-only
        # Neg-Risk provenance can drive diagnostics but never graph
        # acceptance.
        neg_risk_provenance_verified = analysis.mode == "logic"
        constraints.append(
            Constraint(
                constraint_id=item.constraint_id,
                kind=item.kind,
                node_ids=item.node_ids,
                verified=(
                    item.verified
                    and semantics_verified
                    and source_provenance_complete
                    and _constraint_claim_reviewed(
                        spec.event, markets_by_node, item
                    )
                    and neg_risk_provenance_verified
                ),
                evidence=tuple(item.evidence) + source_refs,
            )
        )
    provenance = analysis.neg_risk_provenance
    return ConstraintGraph(
        event_id=spec.event.event_id,
        nodes=tuple(
            snapshot.nodes[pin.node_id]
            for pin in spec.markets
            if pin.node_id in snapshot.nodes
        ),
        constraints=tuple(constraints),
        augmented_neg_risk=analysis.mode == "augmented_neg_risk",
        neg_risk=analysis.mode
        in {"standard_neg_risk", "augmented_neg_risk"},
        onchain_question_count=provenance.onchain_question_count,
        chain_index_map=provenance.chain_index_map,
        adapter_address=provenance.adapter_address,
        adapter_block_number=provenance.adapter_block_number,
        adapter_fee_bips=provenance.adapter_fee_bips,
        collateral_decimals=provenance.collateral_decimals,
        chain_index_evidence=provenance.chain_index_evidence,
    )


def _snapshot_kwargs(
    spec: ConstraintExperimentSpec, snapshot: _Snapshot
) -> dict[str, Any]:
    return {
        "observed_at_ms": snapshot.observed_at_ms,
        "max_book_age_ms": spec.policy.max_book_age_ms,
        "max_book_skew_ms": spec.policy.max_book_skew_ms,
        "max_book_rtt_ms": spec.policy.max_book_rtt_ms,
        "book_evidence": snapshot.book_evidence,
    }


def _classification(
    *, accepted: bool, failures: Sequence[str]
) -> str:
    if accepted:
        return "snapshot_candidate_only"
    if failures and set(failures) <= _EDGE_ONLY_FAILURES:
        return "rejected_snapshot"
    return "blocked"


def _append_analysis_provenance_failures(
    spec: ConstraintExperimentSpec,
    analysis: AnalysisSpec,
    failures: list[str],
) -> None:
    markets_by_node = {market.node_id: market for market in spec.markets}
    for constraint in spec.constraints:
        if not _constraint_claim_reviewed(
            spec.event, markets_by_node, constraint
        ):
            _append_failure(
                failures,
                f"constraint_claim_unreviewed:{constraint.constraint_id}",
            )
    if analysis.mode == "standard_neg_risk":
        _append_failure(
            failures, "standard_neg_risk_provenance_unverified"
        )
    elif analysis.mode == "augmented_neg_risk":
        _append_failure(failures, "augmented_neg_risk_blocked")


def _bundle_rows(
    spec: ConstraintExperimentSpec,
    analysis: AnalysisSpec,
    bundle: BundleSpec,
    graph: ConstraintGraph,
    snapshot: _Snapshot,
) -> list[dict[str, object]]:
    opportunities = enumerate_bundle_opportunities(
        graph=graph,
        family=bundle.family,
        legs=bundle.legs,
        books=snapshot.books,
        fee_schedule=snapshot.fee_schedules,
        fee_source=snapshot.fee_sources,
        gas=spec.policy.gas,
        latency_buffer=spec.policy.latency_buffer,
        inventory=bundle.inventory,
        quantities=bundle.quantities,
        expected_graph_hash=graph.graph_hash,
        **_snapshot_kwargs(spec, snapshot),
    )
    rows: list[dict[str, object]] = []
    for opportunity in opportunities:
        failures = list(snapshot.failures)
        _append_analysis_provenance_failures(
            spec, analysis, failures
        )
        _extend_failures(failures, opportunity.failure_codes)
        accepted = opportunity.accepted and not failures
        row_identity = {
            "config_sha256": spec.config_sha256,
            "analysis_id": analysis.analysis_id,
            "candidate_id": bundle.candidate_id,
            "opportunity_id": opportunity.opportunity_id,
        }
        rows.append(
            {
                "schema_version": CANDIDATE_SCHEMA_VERSION,
                "record_id": _digest(_canonical_json(row_identity)),
                "experiment_id": spec.experiment_id,
                "event_id": spec.event.event_id,
                "analysis_id": analysis.analysis_id,
                "mode": analysis.mode,
                "candidate_id": bundle.candidate_id,
                "candidate_kind": "bundle",
                "family": bundle.family,
                "quantity": str(opportunity.quantity),
                "accepted_snapshot": accepted,
                "classification": _classification(
                    accepted=accepted, failures=failures
                ),
                "failure_codes": failures,
                "graph_hash": graph.graph_hash,
                "config_sha256": spec.config_sha256,
                "source_refs": sorted(set(snapshot.source_refs)),
                "opportunity": opportunity.to_record(),
            }
        )
    return rows


def _amount_out_units(quantity_units: int, fee_bips: int) -> int:
    return quantity_units - quantity_units * fee_bips // 10_000


def _quantity_for_amount_out(
    amount_out: Decimal, *, fee_bips: int
) -> Optional[Decimal]:
    target = amount_out * SHARE_SCALE
    if target != target.to_integral_value() or target <= 0:
        return None
    if not 0 <= fee_bips < 10_000:
        return None
    target_units = int(target)
    guess = (
        target_units * 10_000 + (10_000 - fee_bips) - 1
    ) // (10_000 - fee_bips)
    while _amount_out_units(guess, fee_bips) < target_units:
        guess += 1
    while guess > 1 and _amount_out_units(
        guess - 1, fee_bips
    ) >= target_units:
        guess -= 1
    if _amount_out_units(guess, fee_bips) != target_units:
        return None
    return Decimal(guess) / SHARE_SCALE


def _conversion_quantities(
    conversion: ConversionSpec,
    *,
    graph: ConstraintGraph,
    snapshot: _Snapshot,
) -> tuple[Decimal, ...]:
    if conversion.quantities is not None:
        return conversion.quantities
    nodes = {node.node_id: node for node in graph.nodes}
    selected = set(conversion.selected_node_ids)
    quantities: set[Decimal] = set()
    for node_id in selected:
        node = nodes.get(node_id)
        book = (
            None if node is None else snapshot.books.get(node.no_token_id)
        )
        if book is None:
            continue
        cumulative = Decimal("0")
        for level in sorted(book.asks, key=lambda item: item.price):
            cumulative += level.size
            quantities.add(cumulative)
        quantities.add(book.min_order_size)
    fee_bips = graph.adapter_fee_bips
    if fee_bips is not None:
        for node_id, node in nodes.items():
            if node_id in selected:
                continue
            book = snapshot.books.get(node.yes_token_id)
            if book is None:
                continue
            cumulative = Decimal("0")
            for level in sorted(
                book.bids, key=lambda item: item.price, reverse=True
            ):
                cumulative += level.size
                quantity = _quantity_for_amount_out(
                    cumulative, fee_bips=fee_bips
                )
                if quantity is not None:
                    quantities.add(quantity)
            minimum = _quantity_for_amount_out(
                book.min_order_size, fee_bips=fee_bips
            )
            if minimum is not None:
                quantities.add(minimum)
    return tuple(sorted(quantity for quantity in quantities if quantity > 0))


def _structural_failures(opportunity: Opportunity) -> tuple[str, ...]:
    return tuple(
        code
        for code in opportunity.failure_codes
        if code not in _EDGE_ONLY_FAILURES
    )


def _transform_record(result: Any) -> dict[str, object]:
    return {
        "failure_codes": list(result.failure_codes),
        "index_set": result.index_set,
        "amount_out": str(result.amount_out),
        "collateral_delta": str(result.collateral_delta),
        "token_deltas": {
            token_id: str(quantity)
            for token_id, quantity in sorted(result.token_deltas.items())
        },
    }


def _conversion_rows(
    spec: ConstraintExperimentSpec,
    analysis: AnalysisSpec,
    conversion: ConversionSpec,
    graph: ConstraintGraph,
    snapshot: _Snapshot,
) -> list[dict[str, object]]:
    nodes = {node.node_id: node for node in graph.nodes}
    selected_nodes = tuple(
        nodes[node_id]
        for node_id in conversion.selected_node_ids
        if node_id in nodes
    )
    complement_nodes = tuple(
        node
        for node_id, node in nodes.items()
        if node_id not in set(conversion.selected_node_ids)
    )
    quantities = _conversion_quantities(
        conversion, graph=graph, snapshot=snapshot
    )
    if not quantities:
        quantities = (Decimal("0"),)
    rows: list[dict[str, object]] = []
    for quantity in quantities:
        buy_legs = tuple(
            BundleLeg(node.node_id, "NO", "BUY") for node in selected_nodes
        )
        (buy,) = enumerate_bundle_opportunities(
            graph=graph,
            family=f"{conversion.candidate_id}:buy-no",
            legs=buy_legs,
            books=snapshot.books,
            fee_schedule=snapshot.fee_schedules,
            fee_source=snapshot.fee_sources,
            quantities=(quantity,),
            expected_graph_hash=graph.graph_hash,
            **_snapshot_kwargs(spec, snapshot),
        )
        provenance = analysis.neg_risk_provenance
        transform = InventoryTransform.neg_risk_convert(
            transform_id=(
                f"{analysis.analysis_id}:{conversion.candidate_id}:"
                f"{quantity}"
            ),
            quantity=quantity,
            selected_market_ids=tuple(
                node.market_id for node in selected_nodes
            ),
            fee_bips=(
                -1
                if provenance.adapter_fee_bips is None
                else provenance.adapter_fee_bips
            ),
            chain_index_map=dict(provenance.chain_index_map or {}),
            expected_graph_hash=graph.graph_hash,
            collateral_decimals=(
                -1
                if provenance.collateral_decimals is None
                else provenance.collateral_decimals
            ),
        ).evaluate(
            graph,
            inventory={
                node.no_token_id: quantity for node in selected_nodes
            },
        )
        sell: Optional[Opportunity] = None
        if complement_nodes and transform.amount_out > 0:
            sell_legs = tuple(
                BundleLeg(node.node_id, "YES", "SELL")
                for node in complement_nodes
            )
            (sell,) = enumerate_bundle_opportunities(
                graph=graph,
                family=f"{conversion.candidate_id}:sell-yes",
                legs=sell_legs,
                books=snapshot.books,
                fee_schedule=snapshot.fee_schedules,
                fee_source=snapshot.fee_sources,
                inventory={
                    node.yes_token_id: transform.amount_out
                    for node in complement_nodes
                },
                quantities=(transform.amount_out,),
                expected_graph_hash=graph.graph_hash,
                **_snapshot_kwargs(spec, snapshot),
            )
        failures = list(snapshot.failures)
        _append_analysis_provenance_failures(
            spec, analysis, failures
        )
        if len(selected_nodes) != len(conversion.selected_node_ids):
            _append_failure(failures, "conversion_node_missing")
        _extend_failures(failures, _structural_failures(buy))
        _extend_failures(failures, transform.failure_codes)
        if sell is not None:
            _extend_failures(failures, _structural_failures(sell))
        elif complement_nodes:
            _append_failure(failures, "conversion_output_unavailable")
        sell_gross = Decimal("0") if sell is None else sell.gross_cash_flow
        sell_fees = Decimal("0") if sell is None else sell.taker_fees
        sell_rounding = (
            Decimal("0") if sell is None else sell.rounding_uncertainty
        )
        gross_cash_flow = (
            buy.gross_cash_flow
            + sell_gross
            + transform.collateral_delta
        )
        fees = buy.taker_fees + sell_fees
        rounding = buy.rounding_uncertainty + sell_rounding
        diagnostic_edge = (
            gross_cash_flow
            - fees
            - spec.policy.gas
            - spec.policy.latency_buffer
        )
        conservative_edge = diagnostic_edge - rounding
        if not failures:
            if diagnostic_edge <= 0:
                _append_failure(failures, "edge_nonpositive")
            elif conservative_edge <= Decimal("2") * rounding:
                _append_failure(
                    failures, "edge_below_rounding_uncertainty"
                )
        accepted = not failures
        blocked = bool(set(failures) - _EDGE_ONLY_FAILURES)
        net_edge: Optional[Decimal] = (
            None
            if blocked or "edge_below_rounding_uncertainty" in failures
            else conservative_edge
        )
        identity = {
            "config_sha256": spec.config_sha256,
            "analysis_id": analysis.analysis_id,
            "candidate_id": conversion.candidate_id,
            "selected_node_ids": conversion.selected_node_ids,
            "quantity": str(quantity),
            "buy_opportunity_id": buy.opportunity_id,
            "sell_opportunity_id": (
                None if sell is None else sell.opportunity_id
            ),
            "graph_hash": graph.graph_hash,
        }
        rows.append(
            {
                "schema_version": CANDIDATE_SCHEMA_VERSION,
                "record_id": _digest(_canonical_json(identity)),
                "experiment_id": spec.experiment_id,
                "event_id": spec.event.event_id,
                "analysis_id": analysis.analysis_id,
                "mode": analysis.mode,
                "candidate_id": conversion.candidate_id,
                "candidate_kind": "neg_risk_conversion",
                "family": "standard-neg-risk-convert-and-cross",
                "selected_node_ids": list(conversion.selected_node_ids),
                "quantity": str(quantity),
                "accepted_snapshot": accepted,
                "classification": _classification(
                    accepted=accepted, failures=failures
                ),
                "failure_codes": failures,
                "graph_hash": graph.graph_hash,
                "config_sha256": spec.config_sha256,
                "source_refs": sorted(set(snapshot.source_refs)),
                "gross_cash_flow": str(gross_cash_flow),
                "taker_fees": str(fees),
                "gas": str(spec.policy.gas),
                "latency_buffer": str(spec.policy.latency_buffer),
                "rounding_uncertainty": str(rounding),
                "rounding_coverage_guard": "2",
                "diagnostic_edge": str(diagnostic_edge),
                "net_edge": (
                    None if net_edge is None else str(net_edge)
                ),
                "transform": _transform_record(transform),
                "buy_no_legs": buy.to_record()["leg_fills"],
                "sell_yes_legs": (
                    [] if sell is None else sell.to_record()["leg_fills"]
                ),
                "buy_opportunity": buy.to_record(),
                "sell_opportunity": (
                    None if sell is None else sell.to_record()
                ),
            }
        )
    return rows


def _verify_live_order_guard() -> None:
    try:
        assert_new_orders_disabled()
    except LiveExecutionBlocked:
        return
    raise RuntimeError("new-order safety guard is not active")


def _write_json(path: Path, value: object) -> None:
    payload = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True
    )
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload)
        handle.write("\n")


def _book_acquisition_summary(
    snapshot: _Snapshot,
    manifest: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    book_entries = [
        entry
        for entry in manifest
        if str(entry.get("scope", "")).startswith("book:")
    ]
    requested = [
        Decimal(str(entry["requested_at"])) for entry in book_entries
    ]
    received = [
        Decimal(str(entry["received_at"])) for entry in book_entries
    ]
    rtts = [
        (end - start) * Decimal("1000")
        for start, end in zip(requested, received, strict=True)
    ]
    server_timestamps = [
        book.timestamp_ms
        for book in snapshot.books.values()
        if book.timestamp_ms is not None
    ]
    receipt_timestamps = [
        evidence.received_at_ms
        for evidence in snapshot.book_evidence.values()
    ]

    def window(values: Sequence[Any]) -> Optional[str]:
        if not values:
            return None
        return str(max(values) - min(values))

    future_server_timestamps = sum(
        book.timestamp_ms is not None
        and token_id in snapshot.book_evidence
        and book.timestamp_ms
        > snapshot.book_evidence[token_id].received_at_ms
        for token_id, book in snapshot.books.items()
    )
    return {
        "book_count": len(book_entries),
        "request_window_ms": (
            None
            if not requested
            else str(
                (max(requested) - min(requested)) * Decimal("1000")
            )
        ),
        "receipt_window_ms": (
            None
            if not received
            else str(
                (max(received) - min(received)) * Decimal("1000")
            )
        ),
        "evidence_receipt_window_ms": window(receipt_timestamps),
        "server_timestamp_window_ms": window(server_timestamps),
        "max_rtt_ms": None if not rtts else str(max(rtts)),
        "future_server_timestamp_count": future_server_timestamps,
        "observed_at_ms": snapshot.observed_at_ms,
    }


def _implementation_provenance() -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[2]
    relative_paths = (
        "src/edge_lab/constraint_experiment.py",
        "src/edge_lab/constraints.py",
        "src/edge_lab/models.py",
        "src/edge_lab/sources.py",
        "src/edge_lab/network_safety.py",
        "src/edge_lab/compatibility.py",
        "scripts/run_constraint_experiment.py",
    )
    file_sha256: dict[str, str] = {}
    for relative_path in relative_paths:
        path = repo_root / relative_path
        if not path.is_file():
            raise RuntimeError(
                f"implementation source is missing: {relative_path}"
            )
        file_sha256[relative_path] = _digest(path.read_bytes())
    return {
        "schema_version": "edge-lab-implementation-provenance.v1",
        "file_sha256": file_sha256,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "requests_version": requests.__version__,
        "byteorder": sys.byteorder,
    }


def run_constraint_experiment(
    spec: ConstraintExperimentSpec,
    *,
    source: ConstraintExperimentSource,
    output_root: Path,
    run_id: str,
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> dict[str, object]:
    """Run one immutable, public-only snapshot experiment."""

    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id contains unsupported characters")
    _verify_live_order_guard()
    source_config_sha256 = getattr(
        source, "source_config_sha256", spec.config_sha256
    )
    if source_config_sha256 != spec.config_sha256:
        raise ValueError(
            "replay config does not match the source run config"
        )
    root = output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / run_id
    run_dir.mkdir()
    writer = _EvidenceWriter(run_dir)
    implementation_provenance = _implementation_provenance()
    started_at_ms = clock_ms()
    snapshot = _fetch_snapshot(
        spec, source=source, writer=writer, clock_ms=clock_ms
    )
    rows: list[dict[str, object]] = []
    graph_records: list[dict[str, object]] = []
    analysis_summaries: list[dict[str, object]] = []
    for analysis in spec.analyses:
        analysis_started_at_ms = clock_ms()
        analysis_rows: list[dict[str, object]] = []
        graph = _graph(spec, analysis, snapshot)
        graph_state = graph.enumerate_payoff_states(
            expected_graph_hash=graph.graph_hash
        )
        graph_records.append(
            {
                "analysis_id": analysis.analysis_id,
                "mode": analysis.mode,
                "graph_hash": graph.graph_hash,
                "state_count": len(graph_state.states),
                "failure_codes": list(graph_state.failure_codes),
                "constraint_provenance": [
                    {
                        "constraint_id": constraint.constraint_id,
                        "verified": constraint.verified,
                        "evidence": list(constraint.evidence),
                    }
                    for constraint in graph.constraints
                ],
            }
        )
        for bundle in analysis.bundles:
            analysis_rows.extend(
                _bundle_rows(
                    spec, analysis, bundle, graph, snapshot
                )
            )
        for conversion in analysis.conversions:
            analysis_rows.extend(
                _conversion_rows(
                    spec, analysis, conversion, graph, snapshot
                )
            )
        analysis_finished_at_ms = clock_ms()
        rows.extend(analysis_rows)
        analysis_failure_counts = Counter(
            str(code)
            for row in analysis_rows
            for code in row.get("failure_codes", [])
        )
        analysis_summaries.append(
            {
                "analysis_id": analysis.analysis_id,
                "mode": analysis.mode,
                "candidate_count": len(analysis_rows),
                "accepted_snapshot_count": sum(
                    bool(row["accepted_snapshot"])
                    for row in analysis_rows
                ),
                "classification_counts": dict(
                    Counter(
                        str(row["classification"])
                        for row in analysis_rows
                    )
                ),
                "failure_counts": dict(
                    sorted(analysis_failure_counts.items())
                ),
                "started_at_ms": analysis_started_at_ms,
                "finished_at_ms": analysis_finished_at_ms,
                "duration_ms": (
                    analysis_finished_at_ms - analysis_started_at_ms
                ),
            }
        )
    finished_at_ms = clock_ms()
    failure_counts = Counter(
        str(code)
        for row in rows
        for code in row.get("failure_codes", [])
    )
    summary: dict[str, object] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "experiment_id": spec.experiment_id,
        "event_id": spec.event.event_id,
        "run_id": run_id,
        "started_at_ms": started_at_ms,
        "finished_at_ms": finished_at_ms,
        "config_sha256": spec.config_sha256,
        "analysis_count": len(spec.analyses),
        "analyses": analysis_summaries,
        "candidate_count": len(rows),
        "accepted_snapshot_count": sum(
            bool(row["accepted_snapshot"]) for row in rows
        ),
        "classification_counts": dict(
            Counter(str(row["classification"]) for row in rows)
        ),
        "failure_counts": dict(sorted(failure_counts.items())),
        "source_response_count": len(writer.manifest),
        "source_failure_codes": snapshot.failures,
        "implementation_provenance": implementation_provenance,
        "book_acquisition": _book_acquisition_summary(
            snapshot, writer.manifest
        ),
        "new_orders_disabled": True,
        "network_used": bool(getattr(source, "network_used", True)),
        "replay_lineage": getattr(source, "replay_lineage", None),
        "public_get_only": all(
            entry["method"] == "GET"
            and entry["public_get_valid"] is True
            for entry in writer.manifest
        ),
        "profit_claim": "none_snapshot_diagnostics_only",
    }
    with (run_dir / "candidates.jsonl").open(
        "x", encoding="utf-8"
    ) as handle:
        for row in rows:
            handle.write(_canonical_json(row))
            handle.write("\n")
    _write_json(run_dir / "summary.json", summary)
    _write_json(run_dir / "graphs.json", graph_records)
    _write_json(run_dir / "source_manifest.json", writer.manifest)
    _write_json(run_dir / "config.snapshot.json", spec.config_document)
    artifact_sha256 = {
        filename: _digest((run_dir / filename).read_bytes())
        for filename in (
            "candidates.jsonl",
            "summary.json",
            "graphs.json",
            "source_manifest.json",
            "config.snapshot.json",
        )
    }
    _write_json(
        run_dir / "REPRODUCIBILITY.json",
        {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "artifact_sha256": artifact_sha256,
            "config_sha256": spec.config_sha256,
            "implementation_provenance": implementation_provenance,
            "run_id": run_id,
            "command_shape": (
                "python scripts/run_constraint_experiment.py "
                "--config <config> --output-root <new-directory> "
                f"--run-id {run_id} [--proxy <loopback-http-url>]"
            ),
            "offline_replay_command_shape": (
                "python scripts/run_constraint_experiment.py "
                "--config <source-run>/config.snapshot.json "
                "--replay-source-run <source-run> "
                "--replay-source-repro-sha256 <trusted-sha256> "
                "--output-root <new-directory> "
                "--run-id <new-unique-run-id>"
            ),
            "network_boundary": (
                "public Gamma/CLOB HTTPS GET only; no authenticated routes"
            ),
            "network_used": bool(getattr(source, "network_used", True)),
            "replay_lineage": getattr(source, "replay_lineage", None),
            "economic_boundary": (
                "snapshot screening only; no atomic-fill, finality, or "
                "settlement claim"
            ),
        },
    )
    return summary
