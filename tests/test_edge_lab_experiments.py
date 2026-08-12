"""Deterministic experiment-registry and validation-gate tests."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from src.edge_lab.experiments import (
    ExperimentConflictError,
    ExperimentRecord,
    ExperimentRegistry,
    ValidationEvidence,
    apply_reward_haircuts,
    bootstrap_event_profit_ci,
    chronological_event_split,
    classify_validation,
    city_event_split,
    profit_concentration,
    seeded_event_split,
)


def completed_record(**overrides: object) -> ExperimentRecord:
    fields: dict[str, object] = {
        "experiment_id": "exp-weather-001",
        "canonical_params": {
            "edge_threshold": Decimal("0.0250"),
            "size": Decimal("10"),
        },
        "data_manifest_hash": "sha256:data",
        "git_snapshot": "tree:deadbeef-dirty",
        "status": "completed",
        "strategy": "weather_probability",
        "data_level": "L2",
        "splits": {
            "train": ("event-1", "event-2"),
            "validation": ("event-3",),
            "test": ("event-4",),
        },
        "random_seed": 17,
        "started_at": "2026-07-24T08:00:00Z",
        "finished_at": "2026-07-24T08:03:00Z",
        "metrics": {
            "net_profit": Decimal("12.3400"),
            "ci_lower": Decimal("1.2000"),
        },
        "artifacts": ("results/weather.json",),
        "failed_reason": None,
    }
    fields.update(overrides)
    return ExperimentRecord(**fields)


def test_registry_is_append_only_idempotent_and_decimal_safe(tmp_path) -> None:
    path = tmp_path / "experiments.jsonl"
    registry = ExperimentRegistry(path)
    record = completed_record()

    assert registry.append(record)
    assert not registry.append(record)
    assert len(registry.read_all()) == 1
    assert registry.read_all()[0] == record

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["canonical_params"]["edge_threshold"] == "0.0250"
    assert raw["metrics"]["net_profit"] == "12.3400"
    assert raw["splits"]["test"] == ["event-4"]


def test_registry_rejects_same_id_with_different_content(tmp_path) -> None:
    registry = ExperimentRegistry(tmp_path / "experiments.jsonl")
    registry.append(completed_record())

    with pytest.raises(ExperimentConflictError, match="exp-weather-001"):
        registry.append(
            completed_record(metrics={"net_profit": Decimal("999")})
        )


def test_record_takes_an_immutable_snapshot_of_nested_inputs() -> None:
    params = {"nested": {"thresholds": [Decimal("0.1")]}}
    metrics = {"profit": Decimal("1")}
    splits = {
        "train": ["event-1"],
        "validation": ["event-2"],
        "test": ["event-3"],
    }
    record = completed_record(
        canonical_params=params,
        metrics=metrics,
        splits=splits,
    )

    params["nested"]["thresholds"].append(Decimal("999"))
    metrics["profit"] = Decimal("-100")
    splits["test"].append("leaked-event")

    persisted = record.to_mapping()
    assert persisted["canonical_params"]["nested"]["thresholds"] == ["0.1"]
    assert persisted["metrics"]["profit"] == "1"
    assert persisted["splits"]["test"] == ["event-3"]


def test_record_rejects_event_leakage_across_splits() -> None:
    with pytest.raises(ValueError, match="event-2"):
        completed_record(
            splits={
                "train": ("event-1", "event-2"),
                "validation": ("event-2",),
                "test": ("event-3",),
            }
        )


def test_bootstrap_resamples_whole_events_and_is_seeded() -> None:
    fills = [
        {"event_id": "a", "profit": Decimal("2")},
        {"event_id": "a", "profit": Decimal("3")},
        {"event_id": "b", "profit": Decimal("-1")},
        {"event_id": "c", "profit": Decimal("4")},
        {"event_id": "d", "profit": Decimal("2")},
    ]
    first = bootstrap_event_profit_ci(fills, n_resamples=2_000, seed=91)
    second = bootstrap_event_profit_ci(fills, n_resamples=2_000, seed=91)

    assert first == second
    assert first.n_events == 4
    assert first.estimate == Decimal("2.5")
    assert first.unit == "mean_profit_per_event"
    assert first.lower <= first.estimate <= first.upper


def test_profit_concentration_uses_positive_event_contributions() -> None:
    result = profit_concentration(
        [
            {"event_id": "a", "profit": Decimal("8")},
            {"event_id": "a", "profit": Decimal("2")},
            {"event_id": "b", "profit": Decimal("5")},
            {"event_id": "c", "profit": Decimal("-4")},
        ]
    )
    assert result.leading_event_id == "a"
    assert result.positive_profit == Decimal("15")
    assert result.total_profit == Decimal("11")
    assert result.max_event_share == Decimal("0.6666666666666666666666666667")


def sample_split_rows() -> list[dict[str, object]]:
    return [
        {"event_id": "e1", "city": "NYC", "settled_at": "2026-01-01T00:00:00Z"},
        {"event_id": "e1", "city": "NYC", "settled_at": "2026-01-01T01:00:00Z"},
        {"event_id": "e2", "city": "NYC", "settled_at": "2026-01-02T00:00:00Z"},
        {"event_id": "e3", "city": "London", "settled_at": "2026-01-03T00:00:00Z"},
        {"event_id": "e4", "city": "London", "settled_at": "2026-01-04T00:00:00Z"},
        {"event_id": "e5", "city": "Paris", "settled_at": "2026-01-05T00:00:00Z"},
        {"event_id": "e6", "city": "Tokyo", "settled_at": "2026-01-06T00:00:00Z"},
    ]


def assert_disjoint(splits: object) -> None:
    train = set(splits.train)
    validation = set(splits.validation)
    test = set(splits.test)
    assert not train & validation
    assert not train & test
    assert not validation & test
    assert train | validation | test == {f"e{i}" for i in range(1, 7)}


def test_split_helpers_keep_events_disjoint_and_chronological() -> None:
    rows = sample_split_rows()
    chronological = chronological_event_split(
        rows,
        train_fraction=Decimal("0.5"),
        validation_fraction=Decimal("0.25"),
    )
    assert_disjoint(chronological)
    assert chronological.train == ("e1", "e2", "e3")
    assert chronological.validation == ("e4", "e5")
    assert chronological.test == ("e6",)

    seeded = seeded_event_split(rows, seed=123)
    assert seeded == seeded_event_split(rows, seed=123)
    assert_disjoint(seeded)


def test_city_split_keeps_cities_and_events_disjoint() -> None:
    rows = sample_split_rows()
    splits = city_event_split(
        rows,
        train_fraction=Decimal("0.5"),
        validation_fraction=Decimal("0.25"),
    )
    assert_disjoint(splits)

    assignment = {
        event_id: split_name
        for split_name, event_ids in splits.as_dict().items()
        for event_id in event_ids
    }
    cities: dict[str, set[str]] = {}
    for row in rows:
        cities.setdefault(str(row["city"]), set()).add(
            assignment[str(row["event_id"])]
        )
    assert all(len(assigned_splits) == 1 for assigned_splits in cities.values())


def test_reward_haircuts_include_reward_zero_case() -> None:
    scenarios = apply_reward_haircuts(
        non_reward_profit=Decimal("-2"),
        theoretical_rewards=Decimal("10"),
    )
    assert [item.multiplier for item in scenarios] == [
        Decimal("1"),
        Decimal("0.5"),
        Decimal("0.3"),
        Decimal("0"),
    ]
    assert [item.net_profit for item in scenarios] == [
        Decimal("8"),
        Decimal("3.0"),
        Decimal("1.0"),
        Decimal("-2"),
    ]


def strong_evidence(**overrides: object) -> ValidationEvidence:
    fields: dict[str, object] = {
        "data_level": "L2",
        "independent_settled_events": 120,
        "oos_fills": 250,
        "pessimistic_profit": Decimal("4"),
        "reward_zero_profit": Decimal("3"),
        "bootstrap_ci_lower": Decimal("0.2"),
        "max_event_profit_share": Decimal("0.18"),
        "stable_neighbors": True,
        "tail_acceptable": True,
        "recomputable": True,
    }
    fields.update(overrides)
    return ValidationEvidence(**fields)


def test_strict_validation_classification_gates() -> None:
    validated = classify_validation(strong_evidence())
    assert validated.classification == "validated_profitable"
    assert not validated.failed_gates

    insufficient = classify_validation(
        strong_evidence(independent_settled_events=99)
    )
    assert insufficient.classification == "insufficient_data"
    assert "independent_settled_events>=100" in insufficient.failed_gates

    rejected = classify_validation(strong_evidence(reward_zero_profit=Decimal("0")))
    assert rejected.classification == "rejected"
    assert "reward_zero_profit>0" in rejected.failed_gates

    promising = classify_validation(strong_evidence(stable_neighbors=False))
    assert promising.classification == "promising_not_validated"
    assert "stable_neighbors" in promising.failed_gates


def test_validation_requires_l2_and_all_failure_modes() -> None:
    result = classify_validation(
        strong_evidence(
            data_level="L1",
            oos_fills=99,
            pessimistic_profit=Decimal("0"),
            bootstrap_ci_lower=Decimal("0"),
            max_event_profit_share=Decimal("0.2000001"),
            tail_acceptable=False,
            recomputable=False,
        )
    )
    assert result.classification == "insufficient_data"
    assert set(result.failed_gates) >= {
        "data_level>=L2",
        "oos_fills>=100",
        "pessimistic_profit>0",
        "bootstrap_ci_lower>0",
        "max_event_profit_share<=0.20",
        "tail_acceptable",
        "recomputable",
    }
