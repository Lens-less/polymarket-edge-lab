"""Read-only experiment runner tests for logical and Neg-Risk candidates."""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import pytest
import requests

from src.edge_lab.constraint_experiment import (
    CONFIG_SCHEMA_VERSION,
    PublicConstraintSources,
    PublicGETSession,
    RecordedConstraintSources,
    load_constraint_experiment_spec,
    run_constraint_experiment,
)
from src.edge_lab.sources import (
    CompactCLOBMarket,
    CompactFeeSchedule,
    CompactRewardConfig,
    FetchMetadata,
    Fetched,
    PublicSourceError,
    PublicSourcesClient,
    RawResponse,
    ResolutionRules,
    SourceBook,
    SourceBookLevel,
)


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _raw(
    *,
    source: str,
    url: str,
    value: object,
    requested_at: float = 1.000,
    received_at: float = 1.010,
    method: str = "GET",
    request_params: Mapping[str, object] | None = None,
) -> RawResponse:
    body = json.dumps(value, sort_keys=True).encode("utf-8")
    return RawResponse(
        body=body,
        text=body.decode("utf-8"),
        metadata=FetchMetadata(
            source=source,
            method=method,
            url=url,
            request_params=MappingProxyType(dict(request_params or {})),
            status_code=200,
            requested_at=requested_at,
            received_at=received_at,
            attempt=1,
            response_headers=MappingProxyType({}),
        ),
    )


def _market_values(index: int) -> dict[str, str]:
    return {
        "node_id": f"node-{index}",
        "market_id": f"market-{index}",
        "condition_id": f"condition-{index}",
        "yes_token_id": f"yes-{index}",
        "no_token_id": f"no-{index}",
        "question": f"Will outcome {index} win?",
        "rules": "Exactly one listed outcome resolves Yes.",
        "resolution_source": "https://example.test/rules",
    }


def _config_document(
    *,
    mode: str = "logic",
    include_conversion: bool = False,
    augmented: bool = False,
) -> dict[str, object]:
    markets = [_market_values(1), _market_values(2)]
    analyses: list[dict[str, object]] = [
        {
            "analysis_id": f"{mode}-analysis",
            "mode": mode,
            "bundles": [
                {
                    "candidate_id": "buy-all-yes",
                    "family": "logic-buy-all-yes",
                    "legs": [
                        {
                            "node_id": market["node_id"],
                            "outcome": "YES",
                            "side": "BUY",
                            "units": "1",
                        }
                        for market in markets
                    ],
                }
            ],
            "conversions": (
                [
                    {
                        "candidate_id": "convert-first-no",
                        "selected_node_ids": ["node-1"],
                        "quantities": ["5"],
                    }
                ]
                if include_conversion
                else []
            ),
            "neg_risk_provenance": (
                {
                    "onchain_question_count": 2,
                    "chain_index_map": {
                        "market-1": 0,
                        "market-2": 1,
                    },
                    "adapter_address": "0xadapter-fixture",
                    "adapter_block_number": 123,
                    "adapter_fee_bips": 0,
                    "collateral_decimals": 6,
                    "chain_index_evidence": [
                        "fixture-only:pinned-adapter-block-123"
                    ],
                }
                if mode == "standard_neg_risk" and include_conversion
                else {}
            ),
        }
    ]
    document: dict[str, object] = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "experiment_id": "fixture-event-experiment",
        "event": {
            "event_id": "event-1",
            "expected_market_ids": [
                market["market_id"] for market in markets
            ],
            "require_active": True,
            "require_open": True,
            "expected_neg_risk": mode != "logic",
            "expected_augmented_neg_risk": augmented,
        },
        "markets": [
            {
                "node_id": market["node_id"],
                "market_id": market["market_id"],
                "condition_id": market["condition_id"],
                "yes_token_id": market["yes_token_id"],
                "no_token_id": market["no_token_id"],
                "role": "named",
                "question_sha256": _digest(market["question"]),
                "rules_sha256": _digest(market["rules"]),
                "resolution_source_sha256": _digest(
                    market["resolution_source"]
                ),
            }
            for market in markets
        ],
        "constraints": [
            {
                "constraint_id": "event-exactly-one",
                "kind": "exactly_one",
                "node_ids": [market["node_id"] for market in markets],
                "verified": True,
                "evidence": [],
            }
        ],
        "analyses": analyses,
        "policy": {
            "gas": "0",
            "latency_buffer": "0",
            "max_book_age_ms": 1_000,
            "max_book_skew_ms": 100,
            "max_book_rtt_ms": 100,
        },
    }
    _bind_constraint_evidence(document)
    return document


def _bind_constraint_evidence(document: dict[str, object]) -> None:
    event = document["event"]
    market_pins = document["markets"]
    constraints = document["constraints"]
    assert isinstance(event, dict)
    assert isinstance(market_pins, list)
    assert isinstance(constraints, list)
    constraint = constraints[0]
    assert isinstance(constraint, dict)
    event_set_sha = _digest(
        _canonical_json(sorted(event["expected_market_ids"]))
    )
    claim_sha = _digest(
        _canonical_json(
            {
                "schema_version": "edge-lab-constraint-claim.v1",
                "event_id": event["event_id"],
                "event_market_set_sha256": event_set_sha,
                "constraint_id": constraint["constraint_id"],
                "kind": constraint["kind"],
                "node_ids": constraint["node_ids"],
                "market_pins": [
                    {
                        "node_id": pin["node_id"],
                        "market_id": pin["market_id"],
                        "condition_id": pin["condition_id"],
                        "question_sha256": pin["question_sha256"],
                        "rules_sha256": pin["rules_sha256"],
                        "resolution_source_sha256": (
                            pin["resolution_source_sha256"]
                        ),
                    }
                    for pin in market_pins
                ],
            }
        )
    )
    constraint["evidence"] = [
        (
            "constraint-claim:event-exactly-one:sha256:"
            f"{claim_sha}"
        ),
        f"gamma-event:event-1:market-set-sha256:{event_set_sha}",
    ]


def _write_config(
    tmp_path: Path, document: Mapping[str, object]
) -> Path:
    path = tmp_path / "constraint-experiment.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class FakePublicConstraintSource:
    network_used = False

    def __init__(
        self,
        *,
        augmented: bool = False,
        neg_risk: bool = False,
        fee_conflict: bool = False,
        invalid_compact: bool = False,
        non_get_book: bool = False,
        sensitive_book: bool = False,
        stale_second_book: bool = False,
    ) -> None:
        self.market_values = {
            values["market_id"]: values
            for values in (_market_values(1), _market_values(2))
        }
        self.augmented = augmented
        self.neg_risk = neg_risk
        self.fee_conflict = fee_conflict
        self.invalid_compact = invalid_compact
        self.non_get_book = non_get_book
        self.sensitive_book = sensitive_book
        self.stale_second_book = stale_second_book

    def gamma_event(
        self, event_id: str
    ) -> Fetched[Mapping[str, Any]]:
        payload = {
            "id": event_id,
            "active": True,
            "closed": False,
            "negRisk": self.neg_risk,
            "enableNegRisk": self.neg_risk,
            "negRiskAugmented": self.augmented,
            "markets": [
                {"id": market_id} for market_id in self.market_values
            ],
        }
        return Fetched(
            raw=_raw(
                source="gamma_event",
                url=f"https://gamma-api.polymarket.com/events/{event_id}",
                value=payload,
            ),
            value=payload,
        )

    def resolution_rules(
        self, market_id: str
    ) -> Fetched[ResolutionRules]:
        values = self.market_values[market_id]
        raw_value = {
            "id": market_id,
            "question": values["question"],
            "conditionId": values["condition_id"],
            "clobTokenIds": [
                values["yes_token_id"],
                values["no_token_id"],
            ],
            "outcomes": ["Yes", "No"],
            "feeSchedule": {
                "rate": "0.01" if self.fee_conflict else "0",
                "exponent": "1",
                "takerOnly": True,
                "rebateRate": "0",
            },
            "description": values["rules"],
            "resolutionSource": values["resolution_source"],
            "endDate": "2030-01-01T00:00:00Z",
            "closed": False,
        }
        raw = _raw(
            source="gamma",
            url=(
                "https://gamma-api.polymarket.com/markets/"
                f"{market_id}"
            ),
            value=raw_value,
        )
        return Fetched(
            raw=raw,
            value=ResolutionRules(
                market_id=market_id,
                condition_id=values["condition_id"],
                question_id=None,
                question=values["question"],
                description=values["rules"],
                rules_text=values["rules"],
                resolution_source=values["resolution_source"],
                end_date="2030-01-01T00:00:00Z",
                resolved_by=None,
                closed=False,
                uma_resolution_status=None,
                raw=MappingProxyType(raw_value),
            ),
        )

    def clob_market(
        self, condition_id: str
    ) -> Fetched[CompactCLOBMarket]:
        values = next(
            values
            for values in self.market_values.values()
            if values["condition_id"] == condition_id
        )
        raw_value = {
            "c": condition_id,
            "t": [
                {"t": values["yes_token_id"], "o": "Yes"},
                {"t": values["no_token_id"], "o": "No"},
            ],
            "mos": "5",
            "mts": "0.01",
            "ao": True,
            "r": {},
            "fd": {"r": "0", "e": "1", "to": True},
        }
        raw = _raw(
            source="clob",
            url=(
                "https://clob.polymarket.com/clob-markets/"
                f"{condition_id}"
            ),
            value=(
                {"error": "fixture compact unavailable"}
                if self.invalid_compact
                else raw_value
            ),
        )
        if self.invalid_compact:
            raise PublicSourceError(
                "fixture compact market unavailable", raw=raw
            )
        return Fetched(
            raw=raw,
            value=CompactCLOBMarket(
                condition_id=condition_id,
                tokens=tuple(
                    MappingProxyType(token) for token in raw_value["t"]
                ),
                min_order_size=Decimal("5"),
                tick_size=Decimal("0.01"),
                accepting_orders=True,
                rewards=CompactRewardConfig(
                    min_size=None,
                    max_spread=None,
                    enabled=None,
                    min_order_age_seconds=None,
                    skip_min_order_age=None,
                ),
                fees=CompactFeeSchedule(
                    rate=Decimal("0"),
                    exponent=Decimal("1"),
                    taker_only=True,
                ),
                raw=MappingProxyType(raw_value),
            ),
        )

    def book(self, token_id: str) -> Fetched[SourceBook]:
        market_index = int(token_id.rsplit("-", 1)[1])
        is_yes = token_id.startswith("yes-")
        prices = (
            (("0.30", "5"), ("0.31", "5"))
            if market_index == 1
            else (("0.40", "5"), ("0.41", "5"))
        )
        bids = (
            (SourceBookLevel(Decimal("0.60"), Decimal("10"), {}),)
            if is_yes
            else (SourceBookLevel(Decimal("0.50"), Decimal("10"), {}),)
        )
        asks = tuple(
            SourceBookLevel(Decimal(price), Decimal(size), {})
            for price, size in prices
        )
        condition_id = f"condition-{market_index}"
        timestamp_ms = 1_000 if not (
            self.stale_second_book and market_index == 2
        ) else 100
        raw_value = {
            "asset_id": token_id,
            "market": condition_id,
            "timestamp": str(timestamp_ms),
            "tick_size": "0.01",
            "min_order_size": "5",
            "neg_risk": self.neg_risk,
            "bids": [
                {"price": str(level.price), "size": str(level.size)}
                for level in bids
            ],
            "asks": [
                {"price": str(level.price), "size": str(level.size)}
                for level in asks
            ],
        }
        method = "POST" if self.non_get_book and token_id == "yes-2" else "GET"
        request_params: dict[str, object] = {"token_id": token_id}
        url = "https://clob.polymarket.com/book"
        if self.sensitive_book and token_id == "yes-2":
            request_params["api_key"] = "fixture-secret-must-not-survive"
            url += "?" + "api_key=fixture-secret-must-not-survive"
        return Fetched(
            raw=_raw(
                source="clob_book",
                url=url,
                value=raw_value,
                method=method,
                request_params=request_params,
            ),
            value=SourceBook(
                token_id=token_id,
                bids=bids,
                asks=asks,
                timestamp_ms=timestamp_ms,
                raw=MappingProxyType(raw_value),
            ),
        )


def _rows(output_dir: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (output_dir / "candidates.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def test_logic_runner_walks_all_visible_depth_and_writes_immutable_evidence(
    tmp_path: Path,
) -> None:
    spec = load_constraint_experiment_spec(
        _write_config(tmp_path, _config_document())
    )
    output_root = tmp_path / "runs"

    summary = run_constraint_experiment(
        spec,
        source=FakePublicConstraintSource(),
        output_root=output_root,
        run_id="run-logic",
        clock_ms=lambda: 1_050,
    )
    rows = _rows(output_root / "run-logic")

    assert summary["candidate_count"] == 2
    assert [row["quantity"] for row in rows] == ["5", "10"]
    assert all(not row["accepted_snapshot"] for row in rows)
    assert all(
        "constraint_claim_unreviewed:event-exactly-one"
        in row["failure_codes"]
        for row in rows
    )
    assert all(
        len(row["opportunity"]["leg_fills"][0]["levels"]) >= 1
        for row in rows
    )
    assert all(
        row["classification"] == "blocked"
        for row in rows
    )
    manifest = json.loads(
        (output_root / "run-logic" / "source_manifest.json")
        .read_text(encoding="utf-8")
    )
    assert manifest
    assert {entry["method"] for entry in manifest} == {"GET"}
    assert all(entry["public_get_valid"] for entry in manifest)
    assert all(Path(entry["body_path"]).name.endswith(".json") for entry in manifest)
    graph_record = json.loads(
        (output_root / "run-logic" / "graphs.json")
        .read_text(encoding="utf-8")
    )[0]
    assert graph_record["constraint_provenance"][0]["verified"] is False
    constraint_evidence = graph_record["constraint_provenance"][0][
        "evidence"
    ]
    assert any(
        item.startswith("gamma_event:sha256:")
        for item in constraint_evidence
    )
    assert sum(
        item.startswith("gamma:sha256:")
        for item in constraint_evidence
    ) == 2
    reproducibility = json.loads(
        (output_root / "run-logic" / "REPRODUCIBILITY.json")
        .read_text(encoding="utf-8")
    )
    file_hashes = reproducibility["implementation_provenance"][
        "file_sha256"
    ]
    for relative_path, expected_sha in file_hashes.items():
        assert sha256(Path(relative_path).read_bytes()).hexdigest() == (
            expected_sha
        )

    with pytest.raises(FileExistsError):
        run_constraint_experiment(
            spec,
            source=FakePublicConstraintSource(),
            output_root=output_root,
            run_id="run-logic",
            clock_ms=lambda: 1_050,
        )


def test_rule_pin_mismatch_and_non_get_source_are_both_retained(
    tmp_path: Path,
) -> None:
    document = _config_document()
    document["markets"][0]["rules_sha256"] = "0" * 64
    _bind_constraint_evidence(document)
    spec = load_constraint_experiment_spec(_write_config(tmp_path, document))

    summary = run_constraint_experiment(
        spec,
        source=FakePublicConstraintSource(non_get_book=True),
        output_root=tmp_path / "runs",
        run_id="run-blocked",
        clock_ms=lambda: 1_050,
    )
    rows = _rows(tmp_path / "runs" / "run-blocked")

    assert summary["accepted_snapshot_count"] == 0
    assert rows
    assert all("rules_hash_mismatch" in row["failure_codes"] for row in rows)
    assert all("source_method_not_get" in row["failure_codes"] for row in rows)
    assert all(row["classification"] == "blocked" for row in rows)


def test_stale_cross_market_books_fail_closed(
    tmp_path: Path,
) -> None:
    spec = load_constraint_experiment_spec(
        _write_config(tmp_path, _config_document())
    )

    run_constraint_experiment(
        spec,
        source=FakePublicConstraintSource(stale_second_book=True),
        output_root=tmp_path / "runs",
        run_id="run-stale",
        clock_ms=lambda: 1_050,
    )
    rows = _rows(tmp_path / "runs" / "run-stale")

    assert rows
    assert all(
        "stale_or_unsynced_books" in row["failure_codes"] for row in rows
    )
    assert all(not row["accepted_snapshot"] for row in rows)


def test_config_only_standard_neg_risk_provenance_cannot_manufacture_acceptance(
    tmp_path: Path,
) -> None:
    document = _config_document(
        mode="standard_neg_risk",
        include_conversion=True,
    )
    spec = load_constraint_experiment_spec(_write_config(tmp_path, document))

    run_constraint_experiment(
        spec,
        source=FakePublicConstraintSource(neg_risk=True),
        output_root=tmp_path / "runs",
        run_id="run-convert",
        clock_ms=lambda: 1_050,
    )
    rows = _rows(tmp_path / "runs" / "run-convert")
    conversion = next(
        row for row in rows if row["candidate_kind"] == "neg_risk_conversion"
    )

    assert conversion["selected_node_ids"] == ["node-1"]
    assert conversion["quantity"] == "5"
    assert "neg_risk_mutex_missing" in conversion["transform"]["failure_codes"]
    assert conversion["transform"]["amount_out"] == "0"
    assert conversion["buy_no_legs"]
    assert conversion["sell_yes_legs"] == []
    assert "standard_neg_risk_provenance_unverified" in (
        conversion["failure_codes"]
    )
    assert not conversion["accepted_snapshot"]
    assert conversion["net_edge"] is None


def test_standard_neg_risk_without_chain_mapping_is_retained_as_blocked(
    tmp_path: Path,
) -> None:
    document = _config_document(mode="standard_neg_risk")
    spec = load_constraint_experiment_spec(_write_config(tmp_path, document))

    run_constraint_experiment(
        spec,
        source=FakePublicConstraintSource(neg_risk=True),
        output_root=tmp_path / "runs",
        run_id="run-unmapped",
        clock_ms=lambda: 1_050,
    )
    rows = _rows(tmp_path / "runs" / "run-unmapped")

    assert rows
    assert all("index_mapping_missing" in row["failure_codes"] for row in rows)
    assert all(row["classification"] == "blocked" for row in rows)


def test_gamma_compact_fee_conflict_removes_all_leg_fee_schedules(
    tmp_path: Path,
) -> None:
    spec = load_constraint_experiment_spec(
        _write_config(tmp_path, _config_document())
    )

    run_constraint_experiment(
        spec,
        source=FakePublicConstraintSource(fee_conflict=True),
        output_root=tmp_path / "runs",
        run_id="run-fee-conflict",
        clock_ms=lambda: 1_050,
    )
    rows = _rows(tmp_path / "runs" / "run-fee-conflict")

    assert rows
    assert all("fee_source_conflict" in row["failure_codes"] for row in rows)
    assert all("fee_unknown" in row["failure_codes"] for row in rows)
    assert all(not row["accepted_snapshot"] for row in rows)


def test_augmented_neg_risk_is_recorded_but_never_priced_as_accepted(
    tmp_path: Path,
) -> None:
    document = _config_document(
        mode="augmented_neg_risk",
        augmented=True,
    )
    spec = load_constraint_experiment_spec(_write_config(tmp_path, document))

    run_constraint_experiment(
        spec,
        source=FakePublicConstraintSource(
            augmented=True,
            neg_risk=True,
        ),
        output_root=tmp_path / "runs",
        run_id="run-augmented",
        clock_ms=lambda: 1_050,
    )
    rows = _rows(tmp_path / "runs" / "run-augmented")

    assert rows
    assert all(
        "augmented_neg_risk_blocked" in row["failure_codes"]
        for row in rows
    )
    assert all(row["classification"] == "blocked" for row in rows)


def test_config_rejects_credential_like_fields(
    tmp_path: Path,
) -> None:
    document = _config_document()
    document["private_key"] = "must-not-be-read"

    with pytest.raises(ValueError, match="credential-like"):
        load_constraint_experiment_spec(_write_config(tmp_path, document))


@pytest.mark.parametrize(
    ("container", "key"),
    (
        ("market", "apiKey"),
        ("market", "apiToken"),
        ("market", "accessToken"),
        ("event", "authToken"),
        ("event", "password"),
        ("event", "bearer_token"),
    ),
)
def test_nested_camel_case_credentials_are_rejected(
    tmp_path: Path,
    container: str,
    key: str,
) -> None:
    document = _config_document()
    target = (
        document["markets"][0]
        if container == "market"
        else document["event"]
    )
    target[key] = "SENTINEL-MUST-NOT-SURVIVE"

    with pytest.raises(ValueError, match="credential-like"):
        load_constraint_experiment_spec(_write_config(tmp_path, document))


def test_constraint_evidence_cannot_be_replaced_by_free_text(
    tmp_path: Path,
) -> None:
    document = _config_document()
    document["constraints"][0]["evidence"] = [
        "reviewer-says-this-is-exactly-one"
    ]

    with pytest.raises(ValueError, match="canonical constraint claim"):
        load_constraint_experiment_spec(_write_config(tmp_path, document))


def test_sensitive_source_metadata_is_blocked_and_redacted(
    tmp_path: Path,
) -> None:
    spec = load_constraint_experiment_spec(
        _write_config(tmp_path, _config_document())
    )
    summary = run_constraint_experiment(
        spec,
        source=FakePublicConstraintSource(sensitive_book=True),
        output_root=tmp_path / "runs",
        run_id="run-sensitive",
        clock_ms=lambda: 1_050,
    )
    run_dir = tmp_path / "runs" / "run-sensitive"
    manifest_text = (run_dir / "source_manifest.json").read_text(
        encoding="utf-8"
    )
    rows = _rows(run_dir)

    assert not summary["public_get_only"]
    assert "fixture-secret-must-not-survive" not in manifest_text
    assert "[REDACTED]" in manifest_text
    assert all(
        "source_request_parameter_sensitive" in row["failure_codes"]
        and "source_url_sensitive_query" in row["failure_codes"]
        for row in rows
    )
    assert all(not row["accepted_snapshot"] for row in rows)


def test_saved_raw_run_replays_without_network_and_reproduces_candidates(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path, _config_document())
    spec = load_constraint_experiment_spec(config_path)
    output_root = tmp_path / "runs"
    run_constraint_experiment(
        spec,
        source=FakePublicConstraintSource(),
        output_root=output_root,
        run_id="original",
        clock_ms=lambda: 1_050,
    )
    replay_summary = run_constraint_experiment(
        spec,
        source=RecordedConstraintSources(
            output_root / "original",
            expected_reproducibility_sha256=sha256(
                (
                    output_root
                    / "original"
                    / "REPRODUCIBILITY.json"
                ).read_bytes()
            ).hexdigest(),
        ),
        output_root=output_root,
        run_id="replay",
        clock_ms=lambda: 2_050,
    )

    assert replay_summary["network_used"] is False
    assert replay_summary["replay_lineage"]["source_run_id"] == "original"
    for filename in (
        "candidates.jsonl",
        "graphs.json",
        "source_manifest.json",
    ):
        assert (output_root / "original" / filename).read_bytes() == (
            output_root / "replay" / filename
        ).read_bytes()


def test_saved_failed_response_replay_preserves_original_manifest_times(
    tmp_path: Path,
) -> None:
    spec = load_constraint_experiment_spec(
        _write_config(tmp_path, _config_document())
    )
    output_root = tmp_path / "runs"
    run_constraint_experiment(
        spec,
        source=FakePublicConstraintSource(invalid_compact=True),
        output_root=output_root,
        run_id="original-failure",
        clock_ms=lambda: 1_050,
    )
    run_constraint_experiment(
        spec,
        source=RecordedConstraintSources(
            output_root / "original-failure",
            expected_reproducibility_sha256=sha256(
                (
                    output_root
                    / "original-failure"
                    / "REPRODUCIBILITY.json"
                ).read_bytes()
            ).hexdigest(),
        ),
        output_root=output_root,
        run_id="replay-failure",
        clock_ms=lambda: 2_050,
    )

    for filename in (
        "candidates.jsonl",
        "graphs.json",
        "source_manifest.json",
    ):
        assert (output_root / "original-failure" / filename).read_bytes() == (
            output_root / "replay-failure" / filename
        ).read_bytes()


def test_replay_rejects_artifact_tamper_self_rehash_and_code_drift(
    tmp_path: Path,
) -> None:
    spec = load_constraint_experiment_spec(
        _write_config(tmp_path, _config_document())
    )
    output_root = tmp_path / "runs"
    run_constraint_experiment(
        spec,
        source=FakePublicConstraintSource(),
        output_root=output_root,
        run_id="trusted-source",
        clock_ms=lambda: 1_050,
    )
    source_run = output_root / "trusted-source"
    summary_path = source_run / "summary.json"
    reproducibility_path = source_run / "REPRODUCIBILITY.json"
    original_summary = summary_path.read_bytes()
    original_reproducibility = reproducibility_path.read_bytes()
    external_anchor = sha256(original_reproducibility).hexdigest()

    tampered_summary = json.loads(original_summary)
    tampered_summary["book_acquisition"]["observed_at_ms"] += 1
    summary_path.write_text(
        json.dumps(tampered_summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="source artifact hash mismatch"):
        RecordedConstraintSources(
            source_run,
            expected_reproducibility_sha256=external_anchor,
        )

    tampered_reproducibility = json.loads(original_reproducibility)
    tampered_reproducibility["artifact_sha256"]["summary.json"] = sha256(
        summary_path.read_bytes()
    ).hexdigest()
    reproducibility_path.write_text(
        json.dumps(
            tampered_reproducibility, indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="external replay trust anchor"):
        RecordedConstraintSources(
            source_run,
            expected_reproducibility_sha256=external_anchor,
        )

    summary_path.write_bytes(original_summary)
    drifted_reproducibility = json.loads(original_reproducibility)
    drifted_reproducibility["implementation_provenance"][
        "python_version"
    ] = "0.0-drifted"
    reproducibility_path.write_text(
        json.dumps(
            drifted_reproducibility, indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )
    drifted_anchor = sha256(reproducibility_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="implementation does not match"):
        RecordedConstraintSources(
            source_run,
            expected_reproducibility_sha256=drifted_anchor,
        )


def test_public_source_book_fanout_uses_isolated_get_sessions() -> None:
    calls: list[tuple[int, str, str]] = []
    sessions: list[_FakeBookSession] = []

    def session_factory() -> _FakeBookSession:
        session = _FakeBookSession(calls)
        sessions.append(session)
        return session

    base_session = _FakeBookSession(calls, reject_requests=True)
    client = PublicSourcesClient(
        session=base_session,
        retries=0,
        rate_per_second=1_000,
        burst=2,
    )
    source = PublicConstraintSources(
        client,
        session_factory=session_factory,
        monotonic=lambda: 1.0,
        sleeper=lambda _: None,
    )

    source.prefetch_books(("token-a", "token-b", "token-c"))

    assert [source.book(token_id).value.token_id for token_id in (
        "token-a",
        "token-b",
        "token-c",
    )] == ["token-a", "token-b", "token-c"]
    assert len(sessions) == 3
    assert len({id(session) for session in sessions}) == 3
    assert {method for _, method, _ in calls} == {"GET"}
    assert base_session.request_count == 0
    assert base_session.max_redirects == 0
    assert all(session.max_redirects == 0 for session in sessions)


def test_public_get_session_rejects_before_dispatch_and_checks_final_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched: list[dict[str, object]] = []

    def fake_request(
        _session: requests.Session,
        method: str,
        url: str,
        **kwargs: object,
    ) -> requests.Response:
        dispatched.append(
            {"method": method, "url": url, "kwargs": kwargs}
        )
        response = requests.Response()
        response.status_code = 200
        response.url = "https://example.test/redirect-target"
        response._content = b"{}"
        response.raw = BytesIO(response.content)
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)
    session = PublicGETSession()
    try:
        assert session.trust_env is False
        assert session.max_redirects == 0
        with pytest.raises(requests.RequestException, match="non-public GET"):
            session.get("https://example.test/not-allowed")
        with pytest.raises(
            requests.RequestException, match="sensitive request parameter"
        ):
            session.get(
                "https://clob.polymarket.com/book",
                params={"api_key": "must-not-dispatch"},
            )
        assert dispatched == []

        with pytest.raises(requests.RequestException, match="final URL"):
            session.get(
                "https://clob.polymarket.com/book",
                params={"token_id": "public-token"},
            )
        assert len(dispatched) == 1
        assert dispatched[0]["kwargs"]["allow_redirects"] is False
    finally:
        session.close()


def test_public_constraint_sources_reject_non_loopback_proxy() -> None:
    session = PublicGETSession()
    session.proxies["https"] = "http://proxy.example.test:8080"
    try:
        with pytest.raises(PublicSourceError, match="loopback"):
            client = PublicSourcesClient(session=session, retries=0)
            PublicConstraintSources(client)
    finally:
        session.close()


class _FakeResponse:
    def __init__(self, payload: Mapping[str, object]) -> None:
        self.content = json.dumps(payload).encode("utf-8")
        self.status_code = 200
        self.headers: dict[str, str] = {}


class _FakeBookSession:
    def __init__(
        self,
        calls: list[tuple[int, str, str]],
        *,
        reject_requests: bool = False,
    ) -> None:
        self.calls = calls
        self.reject_requests = reject_requests
        self.request_count = 0
        self.headers: dict[str, str] = {}
        self.auth = None
        self.proxies: dict[str, str] = {}
        self.trust_env = False

    def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> _FakeResponse:
        self.request_count += 1
        if self.reject_requests:
            raise AssertionError("base session must not service fanout books")
        params = kwargs["params"]
        assert isinstance(params, Mapping)
        token_id = str(params["token_id"])
        self.calls.append((id(self), method, token_id))
        return _FakeResponse(
            {
                "asset_id": token_id,
                "bids": [{"price": "0.40", "size": "5"}],
                "asks": [{"price": "0.60", "size": "5"}],
                "timestamp": "1000",
            }
        )

    def close(self) -> None:
        return None
