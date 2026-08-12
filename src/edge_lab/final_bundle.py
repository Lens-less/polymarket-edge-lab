"""Fail-closed aggregation of Edge Discovery experiment artifacts.

The builder is deliberately offline and has no execution dependencies.  It
normalizes heterogeneous research outputs into one conservative bundle while
preserving their evidence ceilings:

* public touches and theoretical reward scenarios are never promoted to fills;
* snapshot candidates are never promoted to atomic execution or realized PnL;
* missing or malformed inputs produce explicit ``null`` metrics with reasons;
* every discovered run is retained in the experiment registry; and
* live trading is always reported as disabled.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shlex
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


BACKTEST_SCHEMA = "edge-final-backtest-results.v1"
REGISTRY_SCHEMA = "edge-final-experiment-registry.v1"
CONFIG_SCHEMA = "edge-final-bundle-config.v1"
EXPECTED_METRICS = (
    "initial_capital",
    "pnl",
    "return",
    "fees",
    "rewards",
    "max_drawdown",
    "sharpe",
    "sortino",
    "hit_rate",
    "turnover",
    "capacity",
    "one_leg_cvar",
    "forecast_alpha",
    "maker_spread_pnl",
    "adverse_selection",
    "confidence_interval_95",
    "confidence_interval",
    "pnl_concentration",
)
METRIC_UNITS = {
    "initial_capital": "pUSD",
    "pnl": "pUSD",
    "return": "ratio",
    "fees": "pUSD",
    "rewards": "pUSD",
    "max_drawdown": "ratio",
    "sharpe": "ratio",
    "sortino": "ratio",
    "hit_rate": "ratio",
    "turnover": "pUSD",
    "capacity": "pUSD",
    "one_leg_cvar": "pUSD",
    "forecast_alpha": "pUSD",
    "maker_spread_pnl": "pUSD",
    "adverse_selection": "pUSD",
    "confidence_interval_95": "pUSD",
    "confidence_interval": "ratio",
    "pnl_concentration": "ratio",
}
CLASSIFICATION_ORDER = {
    "rejected": 0,
    "insufficient_data": 1,
    "promising_not_validated": 2,
    "validated_profitable": 3,
}
TRADE_COLUMNS = (
    "experiment_id",
    "strategy",
    "event_id",
    "market_id",
    "condition_id",
    "token_id",
    "side",
    "price",
    "quantity",
    "fee",
    "filled_at",
    "evidence_kind",
    "source_record_id",
    "data_level",
)


@dataclass(frozen=True)
class FinalBundleConfig:
    """Paths and fixed reporting date for an offline bundle build."""

    research_dir: Path
    output_dir: Path
    report_date: str
    weather_run_dir: Path | None = None

    def __post_init__(self) -> None:
        research = Path(self.research_dir).expanduser().resolve()
        output = Path(self.output_dir).expanduser().resolve()
        weather = (
            None
            if self.weather_run_dir is None
            else Path(self.weather_run_dir).expanduser().resolve()
        )
        try:
            datetime.strptime(self.report_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("report_date must be YYYY-MM-DD") from exc
        object.__setattr__(self, "research_dir", research)
        object.__setattr__(self, "output_dir", output)
        object.__setattr__(self, "weather_run_dir", weather)


@dataclass(frozen=True)
class FinalBundle:
    """Fixed paths emitted by :func:`build_final_bundle`."""

    backtest_results: Path
    trades: Path
    metrics: Path
    data_quality: Path
    live_readiness: Path
    experiment_registry: Path
    reproduction: Path
    config_snapshot: Path

    @property
    def artifact_paths(self) -> tuple[Path, ...]:
        return (
            self.backtest_results,
            self.trades,
            self.metrics,
            self.data_quality,
            self.live_readiness,
            self.experiment_registry,
            self.reproduction,
            self.config_snapshot,
        )


@dataclass(frozen=True)
class _Input:
    kind: str
    path: Path
    relative_path: str
    sha256: str | None
    payload: Mapping[str, Any] | None
    error: str | None

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.relative_path,
            "sha256": self.sha256,
            "status": "loaded" if self.payload is not None else "missing_or_invalid",
            "reason": self.error,
        }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _relative(path: Path, anchor: Path) -> str:
    try:
        return path.resolve().relative_to(anchor.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _load_input(kind: str, path: Path, *, anchor: Path) -> _Input:
    relative = _relative(path, anchor)
    if not path.is_file():
        return _Input(kind, path, relative, None, None, "file is missing")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _Input(kind, path, relative, None, None, f"read failed: {exc}")
    digest = _sha256_bytes(raw)
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        return _Input(kind, path, relative, digest, None, f"invalid JSON: {exc}")
    if not isinstance(payload, Mapping):
        return _Input(
            kind,
            path,
            relative,
            digest,
            None,
            f"expected JSON object, got {type(payload).__name__}",
        )
    return _Input(kind, path, relative, digest, payload, None)


def _discover_inputs(config: FinalBundleConfig) -> list[_Input]:
    root = config.research_dir
    paths: list[tuple[str, Path]] = [
        ("baseline", root / "BASELINE_EVIDENCE_PACK.json"),
    ]
    rewards = sorted(root.glob("selective-liquidity-rewards-*.json"))
    paths.extend(("rewards", path) for path in rewards)
    reward_manifests = sorted(root.glob("reward_runs/*/RUN_MANIFEST.json"))
    reward_v2_experiments: list[Path] = []
    for manifest_path in reward_manifests:
        paths.append(("reward_manifest", manifest_path))
        experiment_path = manifest_path.with_name("EXPERIMENT.json")
        if experiment_path.is_file():
            reward_v2_experiments.append(experiment_path)
            paths.append(("rewards", experiment_path))
    constraints = sorted(root.glob("constraint_experiment_runs/*/summary.json"))
    paths.extend(("constraints", path) for path in constraints)
    latency_paths = sorted(root.glob("LATENCY_CAPTURE_EXPERIMENT*.json"))
    if latency_paths:
        paths.extend(("latency", path) for path in latency_paths)
    else:
        paths.append(("latency", root / "LATENCY_CAPTURE_EXPERIMENT.json"))
    if config.weather_run_dir is not None:
        weather_path = config.weather_run_dir / "RUN_MANIFEST.json"
    else:
        project_root = root.parents[1] if len(root.parents) > 1 else root.parent
        weather_candidates = (
            root / "weather_public_run" / "RUN_MANIFEST.json",
            root / "weather_public" / "RUN_MANIFEST.json",
            project_root / "data" / "edge_lab" / "weather_public" / "RUN_MANIFEST.json",
        )
        weather_path = next(
            (path for path in weather_candidates if path.is_file()),
            weather_candidates[0],
        )
    paths.append(("weather", weather_path))
    weather_experiment = weather_path.parent / "EXPERIMENT.json"
    if weather_experiment.is_file():
        paths.append(("weather_experiment", weather_experiment))
    top_level_manifest = root / "DATA_MANIFEST.json"
    frozen_manifests = sorted(root.glob("capture_freeze*/DATA_MANIFEST.json"))
    if frozen_manifests:
        for manifest_path in frozen_manifests:
            paths.append(("data_manifest", manifest_path))
            quality_path = manifest_path.with_name("DATA_QUALITY.json")
            if quality_path.is_file():
                paths.append(("data_quality", quality_path))
        if top_level_manifest.is_file():
            paths.append(("data_manifest", top_level_manifest))
    else:
        paths.append(("data_manifest", top_level_manifest))
    # Missing singleton inputs remain in the inventory.  Variable-cardinality
    # experiment families have one explicit placeholder when no run exists.
    if not rewards and not reward_v2_experiments:
        paths.append(("rewards", root / "selective-liquidity-rewards-<missing>.json"))
    if not constraints:
        paths.append(
            (
                "constraints",
                root / "constraint_experiment_runs" / "<missing>" / "summary.json",
            )
        )
    return [_load_input(kind, path, anchor=root) for kind, path in paths]


def _metric(
    value: Any,
    *,
    name: str,
    reason: str,
    unit: str | None = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "unit": METRIC_UNITS.get(name) if unit is None else unit,
        "reason": reason,
    }


def _unavailable_metrics(reason: str) -> dict[str, dict[str, Any]]:
    return {
        name: _metric(None, name=name, reason=reason)
        for name in EXPECTED_METRICS
    }


def _read_only_zero_cost_metrics(reason: str) -> dict[str, dict[str, Any]]:
    metrics = _unavailable_metrics(reason)
    metrics["fees"] = _metric(
        "0",
        name="fees",
        reason="recognized/realized fees are zero because no orders or fills occurred",
    )
    metrics["rewards"] = _metric(
        "0",
        name="rewards",
        reason="recognized rewards are zero because no confirmed payout occurred",
    )
    return metrics


def _complete_metrics(
    metrics: Any,
    *,
    missing_reason: str,
) -> dict[str, dict[str, Any]]:
    source = metrics if isinstance(metrics, Mapping) else {}
    result: dict[str, dict[str, Any]] = {}
    for name in EXPECTED_METRICS:
        value = source.get(name)
        if isinstance(value, Mapping):
            result[name] = {
                "value": value.get("value"),
                "unit": value.get("unit", METRIC_UNITS[name]),
                "reason": value.get("reason") or missing_reason,
            }
        else:
            result[name] = _metric(None, name=name, reason=missing_reason)
    return result


def _safe_counts(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"authenticated_fill_count": 0}
    result = {
        str(key): item
        for key, item in value.items()
        if item is None or (isinstance(item, int) and not isinstance(item, bool))
    }
    # Nothing in the source set used here is authenticated own-fill evidence.
    result["authenticated_fill_count"] = 0
    result["actual_fill_count"] = 0
    result.setdefault("explainable_fill_count", 0)
    return result


def _source_safety_valid(payload: Mapping[str, Any]) -> bool:
    safety = payload.get("safety")
    values: list[Mapping[str, Any]] = [payload]
    if isinstance(safety, Mapping):
        values.append(safety)
    for value in values:
        if (
            "live_trading_enabled" in value
            and value["live_trading_enabled"] is not False
        ):
            return False
        if (
            "new_orders_disabled" in value
            and value["new_orders_disabled"] is not True
        ):
            return False
        for key in (
            "orders_submitted",
            "authenticated_endpoints_used",
        ):
            if key not in value:
                continue
            count = value[key]
            if count is False:
                # Retain compatibility with older read-only experiment
                # artifacts that encoded a zero activity count as false.
                continue
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count != 0
            ):
                return False
    return True


def _strategy_row(
    *,
    strategy_id: str,
    family: str,
    experiment_id: str,
    classification: str,
    data_level: str,
    status: str,
    counts: Mapping[str, Any],
    metrics: Mapping[str, Any],
    limitations: Sequence[str],
    source: _Input,
    diagnostics: Mapping[str, Any] | None = None,
    sample: Mapping[str, Any] | None = None,
    stress: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if classification not in CLASSIFICATION_ORDER:
        classification = "insufficient_data"
    safety_valid = (
        source.payload is not None and _source_safety_valid(source.payload)
    )
    if not safety_valid and source.payload is not None:
        classification = "rejected"
        limitations = (
            *limitations,
            "source artifact violates the required disabled-order safety boundary",
        )
    unavailable_split = {
        "count": None,
        "interval": {"start": None, "end": None},
        "reason": "source artifact does not define a leakage-safe split",
    }
    default_sample = {
        "train": dict(unavailable_split),
        "validation": dict(unavailable_split),
        "test": dict(unavailable_split),
    }
    default_stress = {
        "reward_zero_pnl": _metric(
            None,
            name="pnl",
            reason="reward=0 realized PnL is unavailable without qualifying fills",
        ),
        "largest_contributor": _metric(
            None,
            name="pnl_concentration",
            reason="event-level reconciled PnL contributions are unavailable",
        ),
        "pessimistic_assumptions": [
            "no fill is inferred from touch, public flow, or a static snapshot",
            "unconfirmed rewards are recognized as zero",
        ],
    }
    return {
        "strategy_id": strategy_id,
        "family": family,
        "experiment_id": experiment_id,
        "classification": classification,
        "data_level": data_level,
        "status": status,
        "counts": _safe_counts(counts),
        "metrics": _complete_metrics(
            metrics,
            missing_reason="metric is unavailable under this experiment's evidence ceiling",
        ),
        "limitations": list(limitations),
        "diagnostics": dict(diagnostics or {}),
        "sample": dict(sample or default_sample),
        "stress": dict(stress or default_stress),
        "source_artifact": source.relative_path,
        "source_sha256": source.sha256,
        "profitable_strategy_validated": False,
        "live_trading_enabled": False,
    }


def _baseline_rows(source: _Input) -> list[dict[str, Any]]:
    if source.payload is None:
        return []
    evidence_sets = source.payload.get("evidence_sets")
    if not isinstance(evidence_sets, list):
        return [
            _strategy_row(
                strategy_id="legacy-baseline-invalid",
                family="legacy_mm",
                experiment_id="baseline:invalid-pack",
                classification="insufficient_data",
                data_level="L0",
                status="failed",
                counts={},
                metrics=_unavailable_metrics("baseline evidence_sets are missing"),
                limitations=("baseline evidence_sets are missing",),
                source=source,
            )
        ]
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(evidence_sets):
        if not isinstance(item, Mapping):
            continue
        evidence_id = str(item.get("evidence_id") or f"row-{index}")
        limitations = item.get("disqualifying_reasons")
        if not isinstance(limitations, list):
            limitations = ["baseline is not current validated profitability evidence"]
        rows.append(
            _strategy_row(
                strategy_id=f"legacy-baseline:{evidence_id}",
                family="legacy_mm",
                experiment_id=f"baseline:{evidence_id}",
                classification=_cap_unverified_classification(
                    str(item.get("classification") or "rejected"),
                    maximum="insufficient_data",
                ),
                data_level=str(item.get("data_level") or "L0"),
                status="completed",
                counts=_safe_counts(item.get("counts")),
                metrics=_complete_metrics(
                    item.get("metrics"),
                    missing_reason="baseline pack did not contain this metric",
                ),
                limitations=tuple(str(value) for value in limitations),
                source=source,
            )
        )
    return rows


def _baseline_summary_row(source: _Input) -> dict[str, Any]:
    detailed = _baseline_rows(source)
    synthetic_diagnostic = None
    for row in detailed:
        if row["experiment_id"] == "baseline:legacy_mock_crossing_backtest":
            synthetic_diagnostic = {
                "experiment_id": row["experiment_id"],
                "initial_capital": row["metrics"]["initial_capital"]["value"],
                "pnl": row["metrics"]["pnl"]["value"],
                "return": row["metrics"]["return"]["value"],
                "max_drawdown": row["metrics"]["max_drawdown"]["value"],
                "scope": "L0 synthetic diagnostic, excluded from realized metrics",
            }
            break
    level_counts = {
        level: sum(row["data_level"] == level for row in detailed)
        for level in ("L0", "L1", "L2")
    }
    return _strategy_row(
        strategy_id="legacy-mm:canonical-baseline-audit",
        family="legacy_mm",
        experiment_id="baseline:canonical-audit",
        classification="rejected",
        data_level="mixed_L0_L1",
        status="completed",
        counts={
            "evidence_set_count": len(detailed),
            "authenticated_fill_count": 0,
        },
        metrics=_unavailable_metrics(
            "legacy evidence has no authenticated, settled, reconciled fill ledger"
        ),
        limitations=(
            "legacy evidence is synthetic, simulated, public-touch, or snapshot-only",
            "no legacy run satisfies current execution and validation gates",
        ),
        diagnostics={
            "classification_counts": dict(
                sorted(
                    {
                        label: sum(
                            row["classification"] == label for row in detailed
                        )
                        for label in CLASSIFICATION_ORDER
                    }.items()
                )
            ),
            "evidence_level_counts": level_counts,
            "evidence_level_range": ["L0", "L1"],
            "synthetic_reconciled_diagnostic": synthetic_diagnostic,
        },
        stress={
            "reward_zero_pnl": _metric(
                None,
                name="pnl",
                reason="legacy runs have no recognized reward ledger",
            ),
            "largest_contributor": _metric(
                None,
                name="pnl_concentration",
                reason="legacy evidence lacks independent event PnL",
            ),
            "pessimistic_assumptions": [
                "synthetic PnL is diagnostic and excluded from profitability evidence",
                "naked simulated inventory is disqualifying",
                "unconfirmed rewards are zero",
            ],
        },
        source=source,
    )


def _classification_from_counts(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "insufficient_data"
    present = [
        label
        for label, count in value.items()
        if label in CLASSIFICATION_ORDER
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count > 0
    ]
    if not present:
        return "insufficient_data"
    return max(present, key=lambda label: CLASSIFICATION_ORDER[label])


def _cap_unverified_classification(
    classification: str,
    *,
    maximum: str,
) -> str:
    """Never inherit a stronger conclusion than this aggregator can verify."""

    if classification not in CLASSIFICATION_ORDER:
        return "insufficient_data"
    if CLASSIFICATION_ORDER[classification] > CLASSIFICATION_ORDER[maximum]:
        return maximum
    return classification


def _count_key_recursive(value: Any, key: str) -> int:
    count = 0
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            if child_key == key and isinstance(child, list):
                count += len(child)
            count += _count_key_recursive(child, key)
    elif isinstance(value, list):
        for child in value:
            count += _count_key_recursive(child, key)
    return count


def _reward_row(
    source: _Input,
    manifest_verification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    assert source.payload is not None
    payload = source.payload
    experiment_id = str(
        payload.get("experiment_id") or f"reward:{source.sha256 or 'invalid'}"
    )
    markets = payload.get("markets")
    market_rows = markets if isinstance(markets, list) else []
    universe = payload.get("universe")
    universe_map = universe if isinstance(universe, Mapping) else {}
    schema_version = str(payload.get("schema_version") or "")
    is_v2 = schema_version == "edge-lab-liquidity-reward-experiment-v2"
    theoretical_rewards: list[Decimal] = []
    fill_lower_bounds: set[str] = set()
    shadow_fill_scenario_count = 0
    instantaneous_shadow_one_leg_count = 0
    conditional_haircut_scenario_count = 0
    shadow_reward_sensitivity_count = 0
    for market in market_rows:
        if not isinstance(market, Mapping):
            continue
        ledger = market.get("theoretical_ledger")
        value = None
        if isinstance(ledger, Mapping):
            value = ledger.get(
                "conditional_pool_allocation_if_snapshot_share_persisted_full_epoch_at_3x"
            )
            if value is None:
                value = ledger.get(
                    "theoretical_daily_reward_at_3x_competition"
                )
        if isinstance(value, str):
            try:
                theoretical_rewards.append(Decimal(value))
            except InvalidOperation:
                pass
        pressure = market.get("fill_pressure")
        if not isinstance(pressure, Mapping):
            pressure = market.get("shadow_fill_scenario")
        lower = (
            pressure.get("public_fill_probability_lower_bound")
            if isinstance(pressure, Mapping)
            else None
        )
        if lower is not None:
            fill_lower_bounds.add(str(lower))
        shadow_fill_scenario_count += int(
            isinstance(market.get("shadow_fill_scenario"), Mapping)
        )
        instantaneous_shadow_one_leg_count += int(
            isinstance(
                market.get("instantaneous_zero_latency_shadow_one_leg"),
                Mapping,
            )
        )
        haircuts = market.get("conditional_haircuts")
        if isinstance(haircuts, list):
            conditional_haircut_scenario_count += len(haircuts)
        shadow_reward_sensitivity_count += int(
            isinstance(market.get("shadow_reward_sensitivity"), Mapping)
        )
    theoretical_count = max(
        _count_key_recursive(market_rows, "daily_scenarios"),
        sum(
            1
            for market in market_rows
            if isinstance(market, Mapping)
            and isinstance(market.get("theoretical_ledger"), Mapping)
        ),
    )
    classification = _cap_unverified_classification(
        _classification_from_counts(payload.get("classification_counts")),
        maximum="promising_not_validated",
    )
    # Public reward snapshots have no authenticated scoring or payout evidence.
    return _strategy_row(
        strategy_id=f"rewards:{experiment_id}",
        family="rewards",
        experiment_id=experiment_id,
        classification=classification,
        data_level="L1",
        status="completed",
        counts={
            "market_count": len(market_rows),
            "universe_market_count": universe_map.get(
                "unique_current_reward_markets"
            ),
            "evaluated_market_count": universe_map.get("selected_markets"),
            "authenticated_fill_count": 0,
            "confirmed_scoring_epoch_count": 0,
            "confirmed_payout_epoch_count": 0,
        },
        metrics={
            **_read_only_zero_cost_metrics(
                "theoretical reward scenarios and public price touches are not fills or realized PnL"
            ),
            "rewards": _metric(
                "0",
                name="rewards",
                reason="no confirmed payout ledger; recognized rewards are zero",
            ),
        },
        limitations=(
            "theoretical reward estimates are diagnostics only",
            "no authenticated maker scoring records",
            "no confirmed reward payout epochs",
            "no authenticated fills or settlements",
        ),
        diagnostics={
            "schema_version": schema_version,
            "theoretical_scenario_count": theoretical_count,
            "conditional_theoretical_allocation_scenario_count": len(
                theoretical_rewards
            ),
            "classification_counts": payload.get("classification_counts", {}),
            "universe_market_count": universe_map.get(
                "unique_current_reward_markets"
            ),
            "evaluated_market_count": universe_map.get("selected_markets"),
            "top_theoretical_daily_reward_at_3x_competition": (
                str(max(theoretical_rewards)) if theoretical_rewards else None
            ),
            "sum_theoretical_daily_reward_at_3x_competition": (
                str(sum(theoretical_rewards, Decimal("0")))
                if theoretical_rewards
                else None
            ),
            "top_conditional_pool_allocation_if_snapshot_persisted_at_3x": (
                str(max(theoretical_rewards))
                if theoretical_rewards and is_v2
                else None
            ),
            "sum_conditional_pool_allocation_if_snapshot_persisted_at_3x": (
                str(sum(theoretical_rewards, Decimal("0")))
                if theoretical_rewards and is_v2
                else None
            ),
            "shadow_fill_scenario_count": shadow_fill_scenario_count,
            "instantaneous_zero_latency_shadow_one_leg_count": (
                instantaneous_shadow_one_leg_count
            ),
            "conditional_haircut_scenario_count": (
                conditional_haircut_scenario_count
            ),
            "shadow_reward_sensitivity_count": (
                shadow_reward_sensitivity_count
            ),
            "manifest_binding_verified": manifest_verification is not None,
            "manifest_path": (
                manifest_verification.get("manifest_path")
                if manifest_verification is not None
                else None
            ),
            "manifest_sha256": (
                manifest_verification.get("manifest_sha256")
                if manifest_verification is not None
                else None
            ),
            "profit_claim": payload.get("profit_claim"),
            "recognized_trading_pnl": payload.get(
                "recognized_trading_pnl", "0"
            ),
            "recognized_reward_pnl": payload.get(
                "recognized_reward_pnl", "0"
            ),
            "recognized_total_pnl": payload.get(
                "recognized_total_pnl", "0"
            ),
            "recognized_reward": "0",
            "public_fill_probability_lower_bounds": sorted(fill_lower_bounds),
            "counterfactual_l1_not_realized_pnl": True,
            "canonical_result": False,
        },
        stress={
            "reward_zero_pnl": _metric(
                None,
                name="pnl",
                reason=(
                    "reward=0 values in this artifact are static shadow "
                    "scenarios, not realized fill-ledger PnL"
                ),
            ),
            "largest_contributor": _metric(
                None,
                name="pnl_concentration",
                reason="no event-level realized reward or trading PnL exists",
            ),
            "pessimistic_assumptions": [
                "recognized reward is zero without confirmed payout records",
                "public fill lower bound is zero",
                "theoretical 1 paired plus 1 adverse fill per day is not a trade claim",
                "unknown operation costs are not silently treated as zero",
            ],
        },
        source=source,
    )


def _zero_decimal(value: Any) -> bool:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    return parsed.is_finite() and parsed == 0


def _verified_reward_manifest(
    source: _Input,
    inputs: Sequence[_Input],
) -> tuple[Mapping[str, Any] | None, str | None]:
    manifest = next(
        (
            candidate
            for candidate in inputs
            if candidate.kind == "reward_manifest"
            and candidate.payload is not None
            and candidate.path.parent == source.path.parent
        ),
        None,
    )
    if manifest is None or manifest.payload is None:
        return None, "reward v2 RUN_MANIFEST is missing or invalid"
    if manifest.payload.get("schema") != "edge-lab-reward-run-manifest-v2":
        return None, "reward v2 RUN_MANIFEST schema is invalid"
    if manifest.payload.get("canonical_artifact") != source.path.name:
        return None, "reward v2 canonical artifact is not sibling-bound"
    try:
        from .reward_archive import verify_reward_run_manifest

        verified = verify_reward_run_manifest(manifest.path)
    except Exception as exc:
        return (
            None,
            "reward manifest binding verification failed: "
            f"{type(exc).__name__}: {exc}",
        )
    if (
        verified.get("canonical_artifact") != source.path.name
        or verified.get("canonical_artifact_sha256") != source.sha256
        or verified.get("network_requests") != 0
        or verified.get("writes_performed") != 0
    ):
        return None, "reward manifest verifier returned an unsafe binding"
    return verified, None


def _valid_reward_v2_semantics(payload: Mapping[str, Any]) -> bool:
    safety = payload.get("safety")
    if not isinstance(safety, Mapping):
        return False
    if (
        payload.get("profit_claim") != "none"
        or payload.get("actual_fill_count") != 0
        or payload.get("explainable_fill_count") != 0
        or safety.get("credentials_read") is not False
        or safety.get("orders_submitted") is not False
        or safety.get("transactions_signed") is not False
    ):
        return False
    if not all(
        _zero_decimal(payload.get(key))
        for key in (
            "recognized_trading_pnl",
            "recognized_reward_pnl",
            "recognized_total_pnl",
        )
    ):
        return False
    markets = payload.get("markets")
    if not isinstance(markets, list):
        return False
    for market in markets:
        if not isinstance(market, Mapping):
            return False
        if (
            market.get("profit_claim") != "none"
            or market.get("actual_fill_count") != 0
            or market.get("explainable_fill_count") != 0
            or not all(
                _zero_decimal(market.get(key))
                for key in (
                    "recognized_trading_pnl",
                    "recognized_reward_pnl",
                    "recognized_total_pnl",
                )
            )
        ):
            return False
    return True


def _valid_reward_source(
    source: _Input,
    inputs: Sequence[_Input],
) -> bool:
    payload = source.payload
    if not (
        isinstance(payload, Mapping)
        and isinstance(payload.get("experiment_id"), str)
        and payload.get("experiment_id")
        and isinstance(payload.get("markets"), list)
        and isinstance(payload.get("classification_counts"), Mapping)
    ):
        return False
    schema = payload.get("schema_version")
    if schema == "edge-lab-liquidity-reward-experiment-v1":
        return True
    if schema != "edge-lab-liquidity-reward-experiment-v2":
        return False
    verification, _ = _verified_reward_manifest(source, inputs)
    return bool(
        verification is not None
        and _valid_reward_v2_semantics(payload)
    )


def _canonical_reward_source(inputs: Sequence[_Input]) -> _Input | None:
    candidates = [
        source
        for source in inputs
        if source.kind == "rewards"
        and _valid_reward_source(source, inputs)
    ]
    if not candidates:
        return None
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return max(
        candidates,
        key=lambda source: (
            source.payload.get("schema_version")
            == "edge-lab-liquidity-reward-experiment-v2",
            _known_timestamp(source) or floor,
            source.relative_path,
        ),
    )


def _valid_data_manifest(source: _Input) -> bool:
    payload = source.payload
    if not isinstance(payload, Mapping):
        return False
    if payload.get("schema_version") != "edge-data-manifest.v1":
        return False
    claimed = payload.get("data_manifest_hash")
    if (
        not isinstance(claimed, str)
        or len(claimed) != 64
        or any(character not in "0123456789abcdef" for character in claimed)
    ):
        return False
    unhashed = dict(payload)
    unhashed.pop("data_manifest_hash", None)
    return _sha256_bytes(_canonical_bytes(unhashed)) == claimed


def _canonical_data_manifest(inputs: Sequence[_Input]) -> _Input | None:
    candidates = [
        source
        for source in inputs
        if source.kind == "data_manifest" and _valid_data_manifest(source)
    ]
    if not candidates:
        return None
    def priority(source: _Input) -> tuple[int, str]:
        directory = source.path.parent.name
        prefix = "capture_freeze_entity_clock_strict_v"
        if directory.startswith(prefix) and directory[len(prefix) :].isdigit():
            # Higher strict version is authoritative; negate for min().
            return (-int(directory[len(prefix) :]), source.relative_path)
        fallback = {
            "capture_freeze_entity_clock_strict": 10,
            "capture_freeze_strict": 11,
        }
        return (fallback.get(directory, 12), source.relative_path)

    return min(
        candidates,
        key=priority,
    )


def _matching_data_quality(
    inputs: Sequence[_Input],
    manifest: _Input | None,
) -> _Input | None:
    if manifest is None or manifest.payload is None:
        return None
    expected = manifest.payload.get("data_manifest_hash")
    for source in inputs:
        payload = source.payload
        if (
            source.kind == "data_quality"
            and isinstance(payload, Mapping)
            and payload.get("schema_version") == "edge-data-quality.v1"
            and payload.get("data_manifest_hash") == expected
        ):
            return source
    return None


def _constraint_kind(payload: Mapping[str, Any]) -> str:
    text = " ".join(
        str(payload.get(key) or "")
        for key in ("experiment_id", "run_id")
    ).casefold()
    if "augmented" in text:
        return "augmented"
    analysis_count = payload.get("analysis_count")
    if (
        (isinstance(analysis_count, int) and analysis_count >= 2)
        or "standard" in text
    ):
        return "standard"
    if "neg-risk" in text or "negrisk" in text:
        return "augmented"
    return "logic"


def _constraint_families(payload: Mapping[str, Any]) -> tuple[str, ...]:
    kind = _constraint_kind(payload)
    if kind == "standard":
        # The standard current experiment deliberately contains a cross-market
        # logical analysis and a separate standard Neg-Risk analysis.
        return ("constraints", "neg_risk_standard")
    if kind == "augmented":
        return ("neg_risk_augmented",)
    return ("constraints",)


def _valid_constraint_source(source: _Input) -> bool:
    payload = source.payload
    if not isinstance(payload, Mapping):
        return False
    schema = str(payload.get("schema_version") or "")
    if not schema.startswith("edge-lab-constraint-experiment-summary.v"):
        return False
    if (
        not isinstance(payload.get("run_id"), str)
        or not payload.get("run_id")
        or payload.get("new_orders_disabled") is not True
        or payload.get("public_get_only") is not True
        or payload.get("profit_claim")
        != "none_snapshot_diagnostics_only"
    ):
        return False
    for key in ("candidate_count", "accepted_snapshot_count"):
        value = payload.get(key)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            return False
    return True


def _canonical_constraint_sources(
    inputs: Sequence[_Input],
) -> dict[str, _Input]:
    grouped: dict[str, list[_Input]] = {}
    for source in inputs:
        if source.kind != "constraints" or not _valid_constraint_source(source):
            continue
        assert source.payload is not None
        grouped.setdefault(_constraint_kind(source.payload), []).append(source)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    approved_final_runs = {
        "standard": "current-20260724-standard-v6-security-final",
        "augmented": "current-20260724-augmented-v5-security-final",
    }
    return {
        kind: max(
            sources,
            key=lambda source: (
                source.path.parent.name == approved_final_runs.get(kind),
                source.path.parent.name.startswith("current-")
                and "replay" not in source.path.parent.name,
                _known_timestamp(source) or floor,
                source.path.parent.name,
            ),
        )
        for kind, sources in grouped.items()
    }


def _valid_latency_source(source: _Input) -> bool:
    payload = source.payload
    if not isinstance(payload, Mapping):
        return False
    if payload.get("schema_version") not in {
        "edge-lab-capture-latency-experiment.v1",
        "edge-lab-latency-capture-experiment.v1",
    }:
        return False
    if not isinstance(payload.get("experiment_id"), str) or not payload.get(
        "experiment_id"
    ):
        return False
    if payload.get("experiment_id") in {
        # Exact-object replay exposed unbound .partial inventory drift.
        "capture-latency-6fc27a1af41426368176",
    }:
        return False
    digest = payload.get("input_digest_sha256")
    if digest is not None and (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return False
    if isinstance(digest, str) and payload.get("experiment_id") != (
        f"capture-latency-{digest[:20]}"
    ):
        return False
    if source.path.name != "LATENCY_CAPTURE_EXPERIMENT.json":
        expected_name = (
            "LATENCY_CAPTURE_EXPERIMENT_v1_"
            f"{payload.get('experiment_id')}.json"
        )
        if source.path.name != expected_name:
            return False
    safety = payload.get("safety")
    conclusion = payload.get("conclusion")
    if not isinstance(safety, Mapping) or not isinstance(conclusion, Mapping):
        return False
    order_guarded = (
        safety.get("orders_submitted") is False
        or safety.get("new_orders_disabled") is True
    )
    unsafe_optional_flag = any(
        safety.get(key) is not None and safety.get(key) is not False
        for key in (
            "credentials_accessed",
            "network_requests",
            "fills_simulated",
            "backtest_performed",
        )
    )
    if (
        not order_guarded
        or unsafe_optional_flag
        or conclusion.get("profitable_strategy_validated") is not False
        or conclusion.get("trades_or_fills_claimed") != 0
    ):
        return False
    return True


def _canonical_latency_source(inputs: Sequence[_Input]) -> _Input | None:
    candidates = [
        source
        for source in inputs
        if source.kind == "latency" and _valid_latency_source(source)
    ]
    if not candidates:
        return None
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return max(
        candidates,
        key=lambda source: (
            source.payload.get("experiment_id")
            == "capture-latency-fddaaadb3cdbf2fc8c93",
            source.path.name != "LATENCY_CAPTURE_EXPERIMENT.json",
            _known_timestamp(source) or floor,
            source.path.name,
        ),
    )


def _constraint_rows(source: _Input) -> list[dict[str, Any]]:
    assert source.payload is not None
    payload = source.payload
    run_id = str(payload.get("run_id") or source.path.parent.name)
    accepted = payload.get("accepted_snapshot_count")
    accepted_count = (
        accepted
        if isinstance(accepted, int) and not isinstance(accepted, bool)
        else 0
    )
    limitations = [
        "snapshot candidates are not atomic fills, settlements, or realized PnL",
        "no authenticated own-order or fill evidence",
    ]
    if accepted_count:
        limitations.append(
            "accepted snapshot count is retained only as a mathematical diagnostic"
        )
    failures = payload.get("failure_counts")
    families = _constraint_families(payload)
    return [
        _strategy_row(
            strategy_id=f"constraint:{run_id}:{family}",
            family=family,
            experiment_id=(
                run_id if len(families) == 1 else f"{run_id}:{family}"
            ),
            classification="insufficient_data",
            data_level="L1",
            status="completed",
            counts={
                "candidate_count": payload.get("candidate_count"),
                "accepted_snapshot_count": accepted_count,
                "authenticated_fill_count": 0,
            },
            metrics=_read_only_zero_cost_metrics(
                "read-only synchronized snapshot screening has no executable fill ledger"
            ),
            limitations=tuple(limitations),
            diagnostics={
                "source_run_id": run_id,
                "failure_counts": (
                    failures if isinstance(failures, Mapping) else {}
                ),
                "profit_claim": payload.get("profit_claim"),
            },
            stress={
                "reward_zero_pnl": _metric(
                    None,
                    name="pnl",
                    reason="constraint snapshot run has no fill ledger",
                ),
                "largest_contributor": _metric(
                    None,
                    name="pnl_concentration",
                    reason="no realized event PnL contributions exist",
                ),
                "pessimistic_assumptions": [
                    "accepted snapshot is not atomic execution",
                    "each leg remains subject to depth, fee, latency and rounding",
                    "missing chain mapping or adapter provenance blocks Neg-Risk",
                    "unconfirmed rewards are zero",
                ],
            },
            source=source,
        )
        for family in families
    ]


def _latency_row(source: _Input) -> dict[str, Any]:
    assert source.payload is not None
    payload = source.payload
    conclusion = payload.get("conclusion")
    conclusion_map = conclusion if isinstance(conclusion, Mapping) else {}
    coverage = payload.get("coverage")
    coverage_map = coverage if isinstance(coverage, Mapping) else {}
    first_ms = coverage_map.get("first_received_at_ms")
    last_ms = coverage_map.get("last_received_at_ms")
    observation_interval = {
        "start": (
            datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
            if isinstance(first_ms, int) and not isinstance(first_ms, bool)
            else None
        ),
        "end": (
            datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
            if isinstance(last_ms, int) and not isinstance(last_ms, bool)
            else None
        ),
    }
    experiment_id = str(payload.get("experiment_id") or "latency-capture")
    return _strategy_row(
        strategy_id=f"latency:{experiment_id}",
        family="latency",
        experiment_id=experiment_id,
        classification=_cap_unverified_classification(
            str(conclusion_map.get("classification") or "insufficient_data"),
            maximum="insufficient_data",
        ),
        data_level="L2",
        status=str(payload.get("status") or "completed"),
        counts={
            "independent_market_count": coverage_map.get("independent_markets"),
            "quote_observation_count": coverage_map.get("quote_observations"),
            "authenticated_fill_count": 0,
        },
        metrics=_read_only_zero_cost_metrics(
            "receive-time co-movement and price touches are not executable settled fills"
        ),
        limitations=(
            str(
                conclusion_map.get("statement")
                or "latency capture does not establish executable profitability"
            ),
            "no independent settled-market sample",
            "no authenticated fills",
        ),
        diagnostics={
            "sample_gate": payload.get("sample_gate", {}),
            "trades_or_fills_claimed": conclusion_map.get(
                "trades_or_fills_claimed", 0
            ),
        },
        sample={
            "train": {
                "count": None,
                "interval": observation_interval,
                "reason": "capture is descriptive and has no training split",
            },
            "validation": {
                "count": None,
                "interval": observation_interval,
                "reason": "capture is descriptive and has no validation split",
            },
            "test": {
                "count": coverage_map.get("independent_markets"),
                "interval": observation_interval,
                "reason": (
                    "observation interval only; not a settled out-of-sample "
                    "execution test"
                ),
            },
        },
        stress={
            "reward_zero_pnl": _metric(
                None,
                name="pnl",
                reason="no fill or settlement ledger exists",
            ),
            "largest_contributor": _metric(
                None,
                name="pnl_concentration",
                reason="only one independent market was captured",
            ),
            "pessimistic_assumptions": [
                "no cross-connection or quarantined record becomes a sample",
                "price co-movement is not a fill",
                "missing clock, queue, price-to-beat and settlement evidence block PnL",
                "unconfirmed rewards are zero",
            ],
        },
        source=source,
    )


def _weather_split_sample(
    experiment: Mapping[str, Any],
) -> dict[str, Any]:
    splits = experiment.get("splits")
    primary = (
        splits.get("primary_date")
        if isinstance(splits, Mapping)
        and isinstance(splits.get("primary_date"), Mapping)
        else {}
    )
    predictions = experiment.get("event_predictions")
    prediction_by_id = {
        str(row.get("event_id")): row
        for row in predictions
        if isinstance(predictions, list)
        and isinstance(row, Mapping)
        and row.get("event_id") is not None
        and row.get("local_date") is not None
    } if isinstance(predictions, list) else {}
    result: dict[str, Any] = {}
    for split_name in ("train", "validation", "test"):
        ids = primary.get(split_name)
        event_ids = [str(value) for value in ids] if isinstance(ids, list) else []
        split_dates = sorted(
            str(prediction_by_id[event_id].get("local_date"))
            for event_id in event_ids
            if event_id in prediction_by_id
        )
        result[split_name] = {
            "count": len(event_ids) if isinstance(ids, list) else None,
            "independence_group_count": len(
                {
                    str(prediction_by_id[event_id].get("independence_group"))
                    for event_id in event_ids
                    if event_id in prediction_by_id
                }
            ),
            "interval": {
                "start": split_dates[0] if split_dates else None,
                "end": split_dates[-1] if split_dates else None,
            },
            "reason": (
                "whole-local-date split frozen before winner/availability inspection"
                if isinstance(ids, list)
                else "primary_date split is missing"
            ),
        }
    return result


def _decimal_wrapper(value: Any) -> Any:
    if isinstance(value, Mapping) and set(value) == {"__decimal__"}:
        return value.get("__decimal__")
    return value


def _verified_weather_experiment_source(
    inputs: Sequence[_Input],
) -> tuple[_Input | None, str | None]:
    manifest = next(
        (
            source
            for source in inputs
            if source.kind == "weather" and source.payload is not None
        ),
        None,
    )
    if manifest is None or manifest.payload is None:
        return None, "weather RUN_MANIFEST is missing or invalid"
    if manifest.payload.get("schema") != "weather-acquisition-run/v2":
        return None, "weather RUN_MANIFEST is not the bound v2 schema"
    if manifest.payload.get("experiment_available") is not True:
        return None, "weather RUN_MANIFEST does not declare an experiment"
    experiment = next(
        (
            source
            for source in inputs
            if source.kind == "weather_experiment"
            and source.payload is not None
            and source.path.parent == manifest.path.parent
        ),
        None,
    )
    if experiment is None:
        return None, "bound weather EXPERIMENT.json is missing or invalid"
    try:
        from .weather_experiment_runner import verify_weather_run_manifest

        verify_weather_run_manifest(manifest.path.parent)
    except Exception as exc:
        return None, f"weather manifest binding verification failed: {type(exc).__name__}: {exc}"
    return experiment, None


def _weather_row(
    source: _Input,
    experiment_source: _Input | None = None,
    binding_error: str | None = None,
) -> dict[str, Any]:
    assert source.payload is not None
    manifest = source.payload
    experiment = (
        experiment_source.payload
        if experiment_source is not None
        and isinstance(experiment_source.payload, Mapping)
        else None
    )
    evidence_counts = (
        experiment.get("evidence_counts")
        if isinstance(experiment, Mapping)
        and isinstance(experiment.get("evidence_counts"), Mapping)
        else {}
    )
    metrics = (
        experiment.get("metrics")
        if isinstance(experiment, Mapping)
        and isinstance(experiment.get("metrics"), Mapping)
        else {}
    )
    test_metrics = (
        metrics.get("test")
        if isinstance(metrics, Mapping)
        and isinstance(metrics.get("test"), Mapping)
        else {}
    )
    forecast_metrics = (
        test_metrics.get("forecast")
        if isinstance(test_metrics, Mapping)
        and isinstance(test_metrics.get("forecast"), Mapping)
        else {}
    )
    market_metrics = (
        test_metrics.get("market")
        if isinstance(test_metrics, Mapping)
        and isinstance(test_metrics.get("market"), Mapping)
        else {}
    )
    source_for_row = experiment_source or source
    gate_reasons = (
        experiment.get("gate_reasons")
        if isinstance(experiment, Mapping)
        and isinstance(experiment.get("gate_reasons"), list)
        else []
    )
    model_limitations = (
        experiment.get("model_limitations")
        if isinstance(experiment, Mapping)
        and isinstance(experiment.get("model_limitations"), list)
        else []
    )
    trade_rows = (
        experiment.get("trade_rows")
        if isinstance(experiment, Mapping)
        and isinstance(experiment.get("trade_rows"), list)
        else []
    )
    preliminary_splits = (
        experiment.get("splits")
        if isinstance(experiment, Mapping)
        and isinstance(experiment.get("splits"), Mapping)
        else {}
    )
    preliminary_primary = (
        preliminary_splits.get("primary_date")
        if isinstance(preliminary_splits.get("primary_date"), Mapping)
        else {}
    )
    preliminary_test_ids = preliminary_primary.get("test")
    test_event_denominator = (
        len(preliminary_test_ids)
        if isinstance(preliminary_test_ids, list) and preliminary_test_ids
        else None
    )
    shadow_summary: dict[str, Any] = {}
    policy_candidate_count = sum(
        isinstance(row, Mapping) and row.get("action") == "policy_candidate"
        for row in trade_rows
    )
    for split_name in ("validation", "test"):
        selected = [
            row
            for row in trade_rows
            if isinstance(row, Mapping)
            and row.get("split") == split_name
            and row.get("shadow_profit") is not None
            and row.get("diagnostic_action", "diagnostic_counterfactual")
            == "diagnostic_counterfactual"
        ]
        profit = sum(
            (
                Decimal(str(_decimal_wrapper(row.get("shadow_profit"))))
                for row in selected
            ),
            Decimal("0"),
        )
        fees = sum(
            (
                Decimal(str(_decimal_wrapper(row.get("taker_fee") or "0")))
                + Decimal(str(_decimal_wrapper(row.get("maker_fee") or "0")))
                for row in selected
            ),
            Decimal("0"),
        )
        wins = sum(
            Decimal(str(_decimal_wrapper(row.get("resolved_payoff") or "0")))
            > 0
            for row in selected
        )
        shadow_summary[split_name] = {
            "counterfactual_diagnostic_count": len(selected),
            "independence_group_count": len(
                {str(row.get("independence_group")) for row in selected}
            ),
            "shadow_profit_sum": str(profit),
            "fee_sum": str(fees),
            "win_count": wins,
            "scope": "counterfactual L1 diagnostic, not policy action or realized PnL",
        }
        maker_candidates = [
            row
            for row in trade_rows
            if isinstance(row, Mapping)
            and row.get("split") == split_name
            and row.get("execution_style") == "maker_shadow"
            and row.get("diagnostic_action", "diagnostic_counterfactual")
            == "diagnostic_counterfactual"
        ]
        shadow_summary[split_name]["maker_counterfactual_diagnostic_count"] = len(
            maker_candidates
        )
        shadow_summary[split_name][
            "maker_shadow_pnl"
        ] = None
        shadow_summary[split_name][
            "maker_shadow_pnl_reason"
        ] = "maker candidates have no queue or fill evidence"
        if split_name == "test":
            shadow_summary[split_name][
                "all_test_event_mean_shadow_profit"
            ] = (
                str(profit / Decimal(test_event_denominator))
                if test_event_denominator is not None
                else None
            )
    threshold = (
        experiment.get("threshold")
        if isinstance(experiment, Mapping)
        and isinstance(experiment.get("threshold"), Mapping)
        else {}
    )
    formal_policy = (
        experiment.get("policy")
        if isinstance(experiment, Mapping)
        and isinstance(experiment.get("policy"), Mapping)
        else {}
    )
    formal_candidates = (
        formal_policy.get("formal_candidate_events")
        if isinstance(formal_policy.get("formal_candidate_events"), Mapping)
        else {}
    )
    city_robustness = (
        experiment.get("city_holdout_robustness")
        if isinstance(experiment, Mapping)
        and isinstance(experiment.get("city_holdout_robustness"), Mapping)
        else {}
    )
    selected_threshold = _decimal_wrapper(
        threshold.get("best_diagnostic_value", threshold.get("value"))
    )
    validation_objective = None
    evaluations = threshold.get("evaluations")
    if isinstance(evaluations, list):
        for evaluation in evaluations:
            if (
                isinstance(evaluation, Mapping)
                and _decimal_wrapper(evaluation.get("threshold"))
                == selected_threshold
            ):
                validation_objective = _decimal_wrapper(
                    evaluation.get("validation_mean_l1_shadow_profit")
                )
                break
    split_payload = (
        experiment.get("splits")
        if isinstance(experiment, Mapping)
        and isinstance(experiment.get("splits"), Mapping)
        else {}
    )
    city_holdout = (
        split_payload.get("city_holdout")
        if isinstance(split_payload.get("city_holdout"), Mapping)
        else {}
    )
    prediction_rows = (
        experiment.get("event_predictions")
        if isinstance(experiment, Mapping)
        and isinstance(experiment.get("event_predictions"), list)
        else []
    )
    prediction_by_id = {
        str(row.get("event_id")): row
        for row in prediction_rows
        if isinstance(row, Mapping) and row.get("event_id") is not None
    }
    city_holdout_summary: dict[str, Any] = {}
    for split_name in ("train", "validation", "test"):
        event_ids = city_holdout.get(split_name)
        normalized = (
            [str(event_id) for event_id in event_ids]
            if isinstance(event_ids, list)
            else []
        )
        city_holdout_summary[split_name] = {
            "event_count": len(normalized),
            "city_count": len(
                {
                    str(prediction_by_id[event_id].get("city"))
                    for event_id in normalized
                    if event_id in prediction_by_id
                }
            ),
        }
    reproducibility = (
        manifest.get("reproducibility")
        if isinstance(manifest.get("reproducibility"), Mapping)
        else {}
    )
    cache_snapshot = (
        reproducibility.get("cache_snapshot")
        if isinstance(reproducibility.get("cache_snapshot"), Mapping)
        else {}
    )
    derived_artifacts = (
        reproducibility.get("derived_artifacts")
        if isinstance(reproducibility.get("derived_artifacts"), Mapping)
        else {}
    )
    derived_entries = (
        derived_artifacts.get("entries")
        if isinstance(derived_artifacts.get("entries"), list)
        else []
    )
    security_sanitization_sha256 = next(
        (
            entry.get("sha256")
            for entry in derived_entries
            if isinstance(entry, Mapping)
            and entry.get("path") == "SECURITY_SANITIZATION.json"
        ),
        None,
    )
    return _strategy_row(
        strategy_id="weather:public-experiment",
        family="weather",
        experiment_id="weather-public-experiment",
        classification=_cap_unverified_classification(
            str(
                (experiment or {}).get("classification")
                or manifest.get("classification")
                or "insufficient_data"
            ),
            maximum="insufficient_data",
        ),
        data_level=str(
            (experiment or {}).get("data_level")
            or manifest.get("data_level")
            or "L1"
        ),
        status="completed",
        counts={
            "discovered_event_count": manifest.get("discovered_events"),
            "eligible_event_count": manifest.get("eligible_events"),
            "independent_settled_event_count": evidence_counts.get(
                "independent_settled_events"
            ),
            "diagnostic_trade_row_count": len(trade_rows),
            "policy_candidate_count": policy_candidate_count,
            "formal_policy_validation_candidate_count": formal_candidates.get(
                "validation", 0
            ),
            "formal_policy_test_candidate_count": formal_candidates.get(
                "test", 0
            ),
            "explainable_fill_count": evidence_counts.get(
                "explainable_fills",
                manifest.get("explainable_fills", 0),
            ),
            "authenticated_fill_count": 0,
        },
        metrics=_read_only_zero_cost_metrics(
            "public L1 price history has no queue-aware, executable fill ledger"
        ),
        limitations=(
            str(
                (experiment or {}).get("evidence_limitation")
                or manifest.get("evidence_limitation")
                or "public price history only; no realized PnL evidence"
            ),
            *(
                str(value)
                for value in (
                    gate_reasons
                    or [manifest.get("experiment_error") or "profit validation gate not met"]
                )
            ),
            *(str(value) for value in model_limitations),
            *([binding_error] if binding_error is not None else []),
        ),
        diagnostics={
            "experiment_available": manifest.get("experiment_available", False),
            "run_manifest_artifact": source.relative_path,
            "run_manifest_sha256": source.sha256,
            "run_identity_sha256": reproducibility.get(
                "run_identity_sha256"
            ),
            "cache_inventory_sha256": cache_snapshot.get(
                "inventory_sha256"
            ),
            "security_sanitization_sha256": (
                security_sanitization_sha256
            ),
            "manifest_binding_verified": binding_error is None,
            "manifest_binding_error": binding_error,
            "exclusion_count": len(manifest.get("exclusions", []))
            if isinstance(manifest.get("exclusions"), list)
            else None,
            "diagnostic_trade_rows_excluded_from_trades_csv": len(trade_rows),
            "taker_shadow_summary": shadow_summary,
            "selected_threshold": selected_threshold,
            "threshold_value_role": threshold.get(
                "value_role", "diagnostic_best_only"
            ),
            "policy_threshold": _decimal_wrapper(threshold.get("policy_value")),
            "threshold_selected_on": threshold.get("selected_on"),
            "threshold_neighbor_stable": threshold.get("neighbor_stable"),
            "validation_selected_threshold_mean_l1_shadow_profit": (
                validation_objective
            ),
            "city_holdout": city_holdout_summary,
            "counterfactual_l1_not_realized_pnl": True,
            "policy": (
                formal_policy
                if formal_policy
                else {
                    "status": "abstain",
                    "action": "no_trade",
                    "approved": False,
                    "reason": "no verified execution or fill evidence",
                }
            ),
            "city_holdout_policy": city_robustness.get("policy"),
            "test_forecast_mean_brier": _decimal_wrapper(
                forecast_metrics.get("mean_brier")
            ),
            "test_market_mean_brier": _decimal_wrapper(
                market_metrics.get("mean_brier")
            ),
            "test_forecast_mean_log_loss": _decimal_wrapper(
                forecast_metrics.get("mean_log_loss")
            ),
            "test_market_mean_log_loss": _decimal_wrapper(
                market_metrics.get("mean_log_loss")
            ),
        },
        sample=(
            _weather_split_sample(experiment)
            if isinstance(experiment, Mapping)
            else None
        ),
        stress={
            "reward_zero_pnl": _metric(
                None,
                name="pnl",
                reason=(
                    "weather L1 shadow rows have no execution or queue evidence; "
                    "reward assumption is zero but realized PnL is unavailable"
                ),
            ),
            "largest_contributor": _metric(
                None,
                name="pnl_concentration",
                reason="no realized event-level PnL ledger exists",
            ),
            "pessimistic_assumptions": [
                "all trade_rows are L1 diagnostic rows and excluded from TRADES.csv",
                "taker fee uses the queried price-dependent weather schedule",
                "slippage and model margin are retained but cannot manufacture fills",
                "unconfirmed rewards are zero",
            ],
        },
        source=source_for_row,
    )


def _placeholder(family: str, reason: str, source: _Input) -> dict[str, Any]:
    return _strategy_row(
        strategy_id=f"{family}:missing",
        family=family,
        experiment_id=f"missing:{family}",
        classification="insufficient_data",
        data_level="unavailable",
        status="failed",
        counts={},
        metrics=_unavailable_metrics(reason),
        limitations=(reason,),
        source=source,
    )


def _build_strategies(inputs: Sequence[_Input]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    loaded_families: set[str] = set()
    canonical_reward = _canonical_reward_source(inputs)
    canonical_constraints = _canonical_constraint_sources(inputs)
    canonical_constraint_paths = {
        source.path for source in canonical_constraints.values()
    }
    canonical_latency = _canonical_latency_source(inputs)
    (
        weather_experiment_source,
        weather_binding_error,
    ) = _verified_weather_experiment_source(inputs)
    for source in inputs:
        if source.payload is None:
            continue
        if source.kind == "baseline":
            if source.payload is not None:
                rows.append(_baseline_summary_row(source))
                loaded_families.add("legacy_mm")
        elif source.kind == "rewards":
            if source is canonical_reward:
                verification, _ = _verified_reward_manifest(source, inputs)
                row = _reward_row(source, verification)
                row["diagnostics"]["canonical_result"] = True
                rows.append(row)
                loaded_families.add("rewards")
        elif source.kind == "constraints":
            if source.path in canonical_constraint_paths:
                constraint_rows = _constraint_rows(source)
                rows.extend(constraint_rows)
                loaded_families.update(
                    str(row["family"]) for row in constraint_rows
                )
        elif source.kind == "latency":
            if source is canonical_latency:
                rows.append(_latency_row(source))
                loaded_families.add("latency")
        elif source.kind == "weather":
            rows.append(
                _weather_row(
                    source,
                    weather_experiment_source,
                    weather_binding_error,
                )
            )
            loaded_families.add("weather")

    first_by_kind = {source.kind: source for source in inputs}
    missing_specs = {
        "legacy_mm": ("baseline", "baseline evidence pack is missing or invalid"),
        "weather": ("weather", "weather acquisition result is missing or invalid"),
        "constraints": (
            "constraints",
            "cross-market constraint experiment is missing or invalid",
        ),
        "neg_risk_standard": (
            "constraints",
            "standard Neg-Risk experiment is missing or invalid",
        ),
        "neg_risk_augmented": (
            "constraints",
            "augmented Neg-Risk experiment is missing or invalid",
        ),
        "rewards": ("rewards", "reward experiment is missing or invalid"),
        "latency": ("latency", "latency experiment is missing or invalid"),
    }
    for family, (source_kind, reason) in missing_specs.items():
        if family in loaded_families:
            continue
        source = first_by_kind[source_kind]
        rows.append(_placeholder(family, reason, source))
    return sorted(rows, key=lambda row: (row["family"], row["experiment_id"]))


def _known_timestamp(source: _Input) -> datetime | None:
    payload = source.payload
    if payload is None:
        return None
    for key in (
        "generated_at",
        "completed_at",
        "finished_at",
        "as_of_received_at",
        "source_as_of",
        "started_at",
    ):
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(
                f"{value[:-1]}+00:00" if value.endswith("Z") else value
            )
        except ValueError:
            continue
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc)
    for key in ("finished_at_ms", "started_at_ms"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return None


def _as_of(inputs: Sequence[_Input], report_date: str) -> str:
    timestamps = [
        timestamp
        for timestamp in (_known_timestamp(source) for source in inputs)
        if timestamp is not None
    ]
    if timestamps:
        return max(timestamps).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return f"{report_date}T00:00:00.000000Z"


def _validation_policy() -> dict[str, Any]:
    return {
        "minimum_independent_events": 100,
        "minimum_explainable_fills": 100,
        "out_of_sample_required": True,
        "leakage_free_required": True,
        "all_costs_required": True,
        "reward_zero_pnl_must_be_positive": True,
        "bootstrap_resamples": 5000,
        "bootstrap_confidence": "0.95",
        "bootstrap_lower_bound_must_exceed": "0",
        "maximum_single_event_pnl_share": "0.20",
        "minimum_parameter_neighbors": 3,
        "minimum_positive_neighbor_fraction": "0.666666666666666666",
        "maximum_one_leg_cvar_share": "0.20",
        "reconciled_decimal_ledger_required": True,
        "authenticated_or_execution_replay_fill_required": True,
        "snapshot_or_touch_is_fill": False,
        "theoretical_reward_is_realized_pnl": False,
    }


def _capture_session_registry_rows(
    manifest: _Input,
    *,
    as_of: str,
) -> list[dict[str, Any]]:
    if manifest.payload is None:
        return []
    capture = manifest.payload.get("capture")
    batches = (
        capture.get("finalized_batches")
        if isinstance(capture, Mapping)
        and isinstance(capture.get("finalized_batches"), list)
        else []
    )
    partials = (
        capture.get("partial_files")
        if isinstance(capture, Mapping)
        and isinstance(capture.get("partial_files"), list)
        else []
    )
    ongoing_ids: set[str] = set()
    for partial in partials:
        path = (
            partial.get("path")
            if isinstance(partial, Mapping)
            else partial
        )
        if isinstance(path, str):
            name = Path(path).name
            if "-" in name:
                ongoing_ids.add(name.split("-", 1)[0])

    grouped: dict[str, dict[str, Any]] = {}
    for batch in batches:
        if not isinstance(batch, Mapping):
            continue
        batch_id = batch.get("batch_id")
        if not isinstance(batch_id, str) or "-" not in batch_id:
            continue
        session_id = batch_id.split("-", 1)[0]
        row = grouped.setdefault(
            session_id,
            {
                "batch_count": 0,
                "record_count": 0,
                "domain_record_count": 0,
                "ws_domain_record_count": 0,
                "usable_l2_record_count": 0,
                "integrity_invalid_batch_count": 0,
                "source_counts": {},
                "event_type_counts": {},
                "evidence_level_counts": {},
                "received_min": None,
                "received_max": None,
            },
        )
        row["batch_count"] += 1
        counts = batch.get("counts")
        content = batch.get("content")
        counts_map = counts if isinstance(counts, Mapping) else {}
        content_map = content if isinstance(content, Mapping) else {}
        record_count = counts_map.get("record_count")
        if isinstance(record_count, int) and not isinstance(record_count, bool):
            row["record_count"] += record_count
        domain_count = content_map.get("domain_record_count")
        if isinstance(domain_count, int) and not isinstance(domain_count, bool):
            row["domain_record_count"] += domain_count
        usable = content_map.get("usable_l2_record_count")
        if isinstance(usable, int) and not isinstance(usable, bool):
            row["usable_l2_record_count"] += usable
        raw_path = str(batch.get("raw_path") or "")
        source = (
            raw_path.split("/raw/", 1)[1].split("/", 1)[0]
            if "/raw/" in raw_path
            else "unknown"
        )
        row["source_counts"][source] = (
            row["source_counts"].get(source, 0) + (record_count or 0)
        )
        if source in {"clob_market_ws", "rtds_ws"} and isinstance(
            domain_count, int
        ):
            row["ws_domain_record_count"] += domain_count
        event_counts = content_map.get("event_type_counts")
        if isinstance(event_counts, Mapping):
            for event_type, count in event_counts.items():
                if isinstance(count, int) and not isinstance(count, bool):
                    row["event_type_counts"][str(event_type)] = (
                        row["event_type_counts"].get(str(event_type), 0) + count
                    )
        level = str(batch.get("evidence_level") or "unknown")
        row["evidence_level_counts"][level] = (
            row["evidence_level_counts"].get(level, 0) + 1
        )
        if batch.get("integrity_valid") is not True:
            row["integrity_invalid_batch_count"] += 1
        coverage = content_map.get("coverage")
        received = (
            coverage.get("received_at")
            if isinstance(coverage, Mapping)
            and isinstance(coverage.get("received_at"), Mapping)
            else {}
        )
        minimum = received.get("min")
        maximum = received.get("max")
        if isinstance(minimum, str):
            row["received_min"] = (
                minimum
                if row["received_min"] is None
                else min(row["received_min"], minimum)
            )
        if isinstance(maximum, str):
            row["received_max"] = (
                maximum
                if row["received_max"] is None
                else max(row["received_max"], maximum)
            )

    results: list[dict[str, Any]] = []
    for session_id, counts in sorted(grouped.items()):
        events = counts["event_type_counts"]
        error_count = int(events.get("error", 0))
        ongoing = session_id in ongoing_ids
        failed = (
            not ongoing
            and error_count > 0
            and counts["ws_domain_record_count"] == 0
        )
        status = "running" if ongoing else ("failed" if failed else "completed")
        results.append(
            {
                "schema": REGISTRY_SCHEMA,
                "experiment_id": f"capture-session:{session_id}",
                "strategy": "data_capture",
                "status": status,
                "classification": "insufficient_data",
                "data_level": (
                    "L2"
                    if counts["evidence_level_counts"].get("L2", 0) > 0
                    else "L0"
                ),
                "counts": {
                    **counts,
                    "actual_fill_count": 0,
                    "explainable_fill_count": 0,
                    "authenticated_fill_count": 0,
                },
                "metrics": _unavailable_metrics(
                    "capture session records market data, not trading PnL"
                ),
                "source_artifact": manifest.relative_path,
                "source_sha256": manifest.sha256,
                "as_of": as_of,
                "canonical": True,
                "superseded": False,
                "ongoing": ongoing,
                "failed_reason": (
                    "websocket lifecycle errors with zero domain websocket records"
                    if failed
                    else None
                ),
                "profitable_strategy_validated": False,
                "live_trading_enabled": False,
            }
        )
    return results


def _registry_rows(
    inputs: Sequence[_Input],
    strategies: Sequence[Mapping[str, Any]],
    as_of: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    registered_ids: set[str] = set()
    for strategy in strategies:
        if str(strategy["experiment_id"]).startswith("missing:"):
            continue
        registered_ids.add(str(strategy["experiment_id"]))
        rows.append(
            {
                "schema": REGISTRY_SCHEMA,
                "experiment_id": strategy["experiment_id"],
                "strategy": strategy["family"],
                "status": strategy["status"],
                "classification": strategy["classification"],
                "data_level": strategy["data_level"],
                "counts": strategy["counts"],
                "metrics": strategy["metrics"],
                "source_artifact": strategy["source_artifact"],
                "source_sha256": strategy["source_sha256"],
                "as_of": as_of,
                "canonical": True,
                "superseded": False,
                "profitable_strategy_validated": False,
                "live_trading_enabled": False,
            }
        )
    for source in inputs:
        if source.kind != "baseline" or source.payload is None:
            continue
        for strategy in _baseline_rows(source):
            experiment_id = str(strategy["experiment_id"])
            if experiment_id in registered_ids:
                continue
            rows.append(
                {
                    "schema": REGISTRY_SCHEMA,
                    "experiment_id": experiment_id,
                    "strategy": strategy["family"],
                    "status": strategy["status"],
                    "classification": strategy["classification"],
                    "data_level": strategy["data_level"],
                    "counts": strategy["counts"],
                    "metrics": strategy["metrics"],
                    "source_artifact": strategy["source_artifact"],
                    "source_sha256": strategy["source_sha256"],
                    "as_of": as_of,
                    "canonical": False,
                    "superseded": False,
                    "evidence_component": True,
                    "profitable_strategy_validated": False,
                    "live_trading_enabled": False,
                }
            )
            registered_ids.add(experiment_id)
    for source in inputs:
        if source.kind != "constraints" or source.payload is None:
            continue
        for strategy in _constraint_rows(source):
            experiment_id = str(strategy["experiment_id"])
            if experiment_id in registered_ids:
                continue
            rows.append(
                {
                    "schema": REGISTRY_SCHEMA,
                    "experiment_id": experiment_id,
                    "strategy": strategy["family"],
                    "status": strategy["status"],
                    "classification": strategy["classification"],
                    "data_level": strategy["data_level"],
                    "counts": strategy["counts"],
                    "metrics": strategy["metrics"],
                    "source_artifact": strategy["source_artifact"],
                    "source_sha256": strategy["source_sha256"],
                    "as_of": as_of,
                    "canonical": False,
                    "superseded": True,
                    "reproducibility_replay": (
                        "replay" in source.path.parent.name
                    ),
                    "profitable_strategy_validated": False,
                    "live_trading_enabled": False,
                }
            )
            registered_ids.add(experiment_id)
    for source in inputs:
        if source.kind != "latency" or source.payload is None:
            continue
        strategy = _latency_row(source)
        experiment_id = str(strategy["experiment_id"])
        if experiment_id in registered_ids:
            continue
        rows.append(
            {
                "schema": REGISTRY_SCHEMA,
                "experiment_id": experiment_id,
                "strategy": strategy["family"],
                "status": strategy["status"],
                "classification": strategy["classification"],
                "data_level": strategy["data_level"],
                "counts": strategy["counts"],
                "metrics": strategy["metrics"],
                "source_artifact": strategy["source_artifact"],
                "source_sha256": strategy["source_sha256"],
                "as_of": as_of,
                "canonical": False,
                "superseded": True,
                "profitable_strategy_validated": False,
                "live_trading_enabled": False,
            }
        )
        registered_ids.add(experiment_id)
    for source in inputs:
        if source.kind != "weather" or source.payload is None:
            continue
        payload = source.payload
        experiment_id = "weather-public-acquisition"
        if experiment_id in registered_ids:
            continue
        rows.append(
            {
                "schema": REGISTRY_SCHEMA,
                "experiment_id": experiment_id,
                "strategy": "weather_acquisition",
                "status": "completed",
                "classification": _cap_unverified_classification(
                    str(payload.get("classification") or "insufficient_data"),
                    maximum="insufficient_data",
                ),
                "data_level": str(payload.get("data_level") or "L1"),
                "counts": _safe_counts(
                    {
                        "discovered_event_count": payload.get(
                            "discovered_events"
                        ),
                        "eligible_event_count": payload.get("eligible_events"),
                        "explainable_fill_count": payload.get(
                            "explainable_fills", 0
                        ),
                    }
                ),
                "metrics": _unavailable_metrics(
                    "weather acquisition is not an execution experiment"
                ),
                "source_artifact": source.relative_path,
                "source_sha256": source.sha256,
                "as_of": as_of,
                "canonical": False,
                "superseded": False,
                "evidence_component": True,
                "profitable_strategy_validated": False,
                "live_trading_enabled": False,
            }
        )
        registered_ids.add(experiment_id)
    # Only the latest schema-valid reward result appears in BACKTEST_RESULTS,
    # but prior attempts remain immutable registry evidence.
    for source in inputs:
        if source.kind != "rewards" or source.payload is None:
            continue
        if _valid_reward_source(source, inputs):
            verification, _ = _verified_reward_manifest(source, inputs)
            strategy = _reward_row(source, verification)
            experiment_id = str(strategy["experiment_id"])
            if experiment_id in registered_ids:
                continue
            rows.append(
                {
                    "schema": REGISTRY_SCHEMA,
                    "experiment_id": experiment_id,
                    "strategy": "rewards",
                    "status": strategy["status"],
                    "classification": strategy["classification"],
                    "data_level": strategy["data_level"],
                    "counts": strategy["counts"],
                    "metrics": strategy["metrics"],
                    "source_artifact": strategy["source_artifact"],
                    "source_sha256": strategy["source_sha256"],
                    "as_of": as_of,
                    "historical_noncanonical_run": True,
                    "canonical": False,
                    "superseded": True,
                    "profitable_strategy_validated": False,
                    "live_trading_enabled": False,
                }
            )
            registered_ids.add(experiment_id)
        else:
            invalid_reason = "reward artifact schema is invalid"
            if (
                source.payload.get("schema_version")
                == "edge-lab-liquidity-reward-experiment-v2"
            ):
                _, binding_error = _verified_reward_manifest(source, inputs)
                invalid_reason = (
                    binding_error
                    or "reward v2 zero-recognized-economics contract failed"
                )
            experiment_id = str(
                source.payload.get("experiment_id")
                or f"invalid-reward-schema:{source.sha256[:16]}"
            )
            if experiment_id in registered_ids:
                continue
            rows.append(
                {
                    "schema": REGISTRY_SCHEMA,
                    "experiment_id": experiment_id,
                    "strategy": "rewards",
                    "status": "failed",
                    "classification": "insufficient_data",
                    "data_level": "unavailable",
                    "counts": {"authenticated_fill_count": 0},
                    "metrics": _unavailable_metrics(
                        invalid_reason
                    ),
                    "source_artifact": source.relative_path,
                    "source_sha256": source.sha256,
                    "as_of": as_of,
                    "historical_noncanonical_run": True,
                    "canonical": False,
                    "superseded": True,
                    "profitable_strategy_validated": False,
                    "live_trading_enabled": False,
                    "invalid_reason": invalid_reason,
                }
            )
            registered_ids.add(experiment_id)
    canonical_manifest = _canonical_data_manifest(inputs)
    for manifest in (
        source
        for source in inputs
        if source.kind == "data_manifest" and _valid_data_manifest(source)
    ):
        summary = manifest.payload.get("summary")
        if not isinstance(summary, Mapping):
            summary = manifest.payload.get("totals")
        canonical = manifest is canonical_manifest
        rows.append(
            {
                "schema": REGISTRY_SCHEMA,
                "experiment_id": (
                    "data-manifest-audit"
                    if canonical
                    else f"data-manifest-audit:{manifest.sha256[:16]}"
                ),
                "strategy": "data_capture",
                "status": "completed",
                "classification": "insufficient_data",
                "data_level": (
                    summary.get("highest_evidence_level", "mixed")
                    if isinstance(summary, Mapping)
                    else "unknown"
                ),
                "counts": dict(summary) if isinstance(summary, Mapping) else {},
                "metrics": _unavailable_metrics(
                    "data inventory is not a trading experiment"
                ),
                "source_artifact": manifest.relative_path,
                "source_sha256": manifest.sha256,
                "as_of": as_of,
                "canonical": canonical,
                "superseded": not canonical,
                "profitable_strategy_validated": False,
                "live_trading_enabled": False,
            }
        )
    if canonical_manifest is not None:
        for capture_row in _capture_session_registry_rows(
            canonical_manifest,
            as_of=as_of,
        ):
            if capture_row["experiment_id"] not in registered_ids:
                rows.append(capture_row)
                registered_ids.add(str(capture_row["experiment_id"]))
    for manifest in (
        source
        for source in inputs
        if source.kind == "data_manifest"
        and source.payload is not None
        and not _valid_data_manifest(source)
    ):
        rows.append(
            {
                "schema": REGISTRY_SCHEMA,
                "experiment_id": f"invalid-data-manifest:{manifest.sha256[:16]}",
                "strategy": "data_capture",
                "status": "failed",
                "classification": "insufficient_data",
                "data_level": "unavailable",
                "counts": {"authenticated_fill_count": 0},
                "metrics": _unavailable_metrics(
                    "DATA_MANIFEST schema or self-hash validation failed"
                ),
                "source_artifact": manifest.relative_path,
                "source_sha256": manifest.sha256,
                "as_of": as_of,
                "canonical": False,
                "superseded": True,
                "profitable_strategy_validated": False,
                "live_trading_enabled": False,
            }
        )
    # Invalid non-placeholder artifacts are themselves failed experiment runs.
    for source in inputs:
        if (
            source.payload is None
            and source.sha256 is not None
            and "<missing>" not in source.relative_path
        ):
            rows.append(
                {
                    "schema": REGISTRY_SCHEMA,
                    "experiment_id": f"invalid-artifact:{source.sha256[:16]}",
                    "strategy": source.kind,
                    "status": "failed",
                    "classification": "insufficient_data",
                    "data_level": "unavailable",
                    "counts": {"authenticated_fill_count": 0},
                    "metrics": _unavailable_metrics(source.error or "invalid input"),
                    "source_artifact": source.relative_path,
                    "source_sha256": source.sha256,
                    "as_of": as_of,
                    "profitable_strategy_validated": False,
                    "live_trading_enabled": False,
                }
            )
    return sorted(rows, key=lambda row: (row["strategy"], row["experiment_id"]))


def _overall(
    inputs: Sequence[_Input],
    strategies: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    loaded = sum(1 for source in inputs if source.payload is not None)
    if loaded == 0:
        classification = "insufficient_data"
        reason = "no valid experiment or data-manifest inputs were available"
    else:
        classification = "rejected"
        reason = (
            "no strategy satisfies the strict fill-aware, out-of-sample "
            "profitability validation gates"
        )
    return {
        "classification": classification,
        "profitable_strategy_validated": False,
        "validated_strategy_count": 0,
        "strategy_result_count": len(strategies),
        "reason": reason,
        "recommendation": "不建议实盘交易",
    }


def _backtest_payload(
    *,
    report_date: str,
    as_of: str,
    inputs: Sequence[_Input],
    strategies: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": BACKTEST_SCHEMA,
        "report_date": report_date,
        "as_of": as_of,
        "overall": _overall(inputs, strategies),
        "validation_policy": _validation_policy(),
        "strategies": list(strategies),
        "trade_artifact": {
            "path": "TRADES.csv",
            "row_count": 0,
            "reason": (
                "No source artifact contains qualifying authenticated or "
                "queue-aware execution-replay fills. Touches, public flow, "
                "theoretical reward scenarios, and snapshot candidates are excluded."
            ),
        },
        "safety": {
            "live_trading_enabled": False,
            "new_orders_disabled": True,
            "orders_submitted_by_builder": 0,
            "authenticated_endpoints_used_by_builder": 0,
            "network_access_by_builder": False,
        },
    }


def _input_table(inputs: Sequence[_Input]) -> str:
    lines = [
        "| 类型 | 路径 | 状态 | SHA-256 / 原因 |",
        "|---|---|---|---|",
    ]
    for source in inputs:
        status = "loaded" if source.payload is not None else "missing/invalid"
        evidence = source.sha256 or source.error or "unknown"
        lines.append(
            f"| {source.kind} | `{source.relative_path}` | {status} | `{evidence}` |"
        )
    return "\n".join(lines)


def _metrics_markdown(
    strategies: Sequence[Mapping[str, Any]],
    as_of: str,
) -> str:
    lines = [
        "# Edge Discovery Metrics",
        "",
        f"As of: `{as_of}`",
        "",
        "所有当前策略的严格盈利验证均未通过。理论奖励、price touch、公开流量和同步快照候选不计为成交或 PnL。",
        "",
        "| 策略族 | 实验 | 数据级别 | 结论 | 可认证成交 | PnL |",
        "|---|---|---:|---|---:|---|",
    ]
    for row in strategies:
        pnl = row["metrics"]["pnl"]
        pnl_text = pnl["value"] if pnl["value"] is not None else f"null ({pnl['reason']})"
        lines.append(
            f"| {row['family']} | `{row['experiment_id']}` | {row['data_level']} "
            f"| {row['classification']} | {row['counts'].get('authenticated_fill_count', 0)} "
            f"| {pnl_text} |"
        )
    lines.extend(
        [
            "",
            "## Sample and fill coverage",
            "",
            "| 策略族 | Train count | Validation count | Test/OOS count | Actual fills | Explainable fills |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in strategies:
        lines.append(
            f"| {row['family']} | {row['sample']['train']['count']} "
            f"| {row['sample']['validation']['count']} "
            f"| {row['sample']['test']['count']} "
            f"| {row['counts'].get('actual_fill_count', 0)} "
            f"| {row['counts'].get('explainable_fill_count', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Required metric contract",
            "",
            "Every strategy row in `BACKTEST_RESULTS.json` includes `null + reason` "
            "when unavailable for: "
            + ", ".join(f"`{name}`" for name in EXPECTED_METRICS)
            + ".",
            "",
            "## 统一结论",
            "",
            "- 严格验证盈利策略：0。",
            "- 当前可认证/可解释成交：0。",
            "- 实盘交易：禁用。",
            "- 结论：不建议实盘交易。",
            "",
        ]
    )
    return "\n".join(lines)


def _data_quality_markdown(inputs: Sequence[_Input], as_of: str) -> str:
    manifest = _canonical_data_manifest(inputs)
    quality = _matching_data_quality(inputs, manifest)
    if manifest is None:
        manifest_text = (
            "No schema-valid, self-hash-valid `DATA_MANIFEST.json` is available; "
            "forward-capture integrity and evidence-level claims therefore "
            "remain unavailable."
        )
    else:
        summary = manifest.payload.get("summary")
        if not isinstance(summary, Mapping):
            summary = manifest.payload.get("totals")
        manifest_text = (
            f"Canonical DATA_MANIFEST: `{manifest.relative_path}` "
            f"(`{manifest.payload.get('data_manifest_hash')}`). Summary: `"
            + json.dumps(summary, sort_keys=True, ensure_ascii=False)
            + "`"
            if isinstance(summary, Mapping)
            else (
                f"Canonical DATA_MANIFEST `{manifest.relative_path}` passed "
                "schema/self-hash validation, but its summary is missing."
            )
        )
        manifest_text += (
            f"\n\nMatching DATA_QUALITY: `{quality.relative_path}` "
            "(schema and manifest hash matched), "
            f"reported status=`{quality.payload.get('status')}`. "
            "A non-pass status remains a readiness blocker."
            if quality is not None
            else "\n\nMatching DATA_QUALITY is missing or failed cross-hash validation."
        )
    missing = sum(1 for source in inputs if source.payload is None)
    return "\n".join(
        [
            "# Data Quality",
            "",
            f"As of: `{as_of}`",
            "",
            manifest_text,
            "",
            f"Input inventory contains {len(inputs)} entries; {missing} are missing or invalid.",
            "",
            _input_table(inputs),
            "",
            "## Evidence ceilings",
            "",
            "- L0 synthetic and simulator artifacts are diagnostic only.",
            "- L1 public histories, touches, flow proxies and reward snapshots cannot establish fills.",
            "- L2 forward streams support replay only when integrity, clock, queue and settlement gates all pass.",
            "- Missing metrics remain `null` with a reason; they are never imputed.",
            "",
        ]
    )


def _live_readiness_markdown(
    strategies: Sequence[Mapping[str, Any]],
    inputs: Sequence[_Input],
) -> str:
    missing = [
        source.relative_path
        for source in inputs
        if source.payload is None
    ]
    lines = [
        "# Live Readiness",
        "",
        "Status: **DISABLED**",
        "",
        "结论：**不建议实盘交易**。",
        "",
        "没有策略满足至少 100 个独立事件、100 个可解释成交、严格样本外、完整费用/滑点/延迟、reward=0 仍盈利、bootstrap 下界为正、集中度和单腿风险限制等全部门槛。",
        "",
        f"- 已验证盈利策略：0 / {len(strategies)}",
        "- 新订单：禁用",
        "- 资金/钱包/密钥：未使用",
        "- 聚合器网络访问：无",
        "- 自动解锁实盘：不支持",
    ]
    if missing:
        lines.extend(
            [
                "",
                "## 缺失或无效输入",
                "",
                *[f"- `{path}`" for path in missing],
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _reproduction_markdown(config: FinalBundleConfig, inputs: Sequence[_Input]) -> str:
    quote = lambda value: shlex.quote(str(value))
    reward = _canonical_reward_source(inputs)
    reward_manifest = (
        reward.path.with_name("RUN_MANIFEST.json")
        if reward is not None
        and reward.payload is not None
        and reward.payload.get("schema_version")
        == "edge-lab-liquidity-reward-experiment-v2"
        else None
    )
    constraints = _canonical_constraint_sources(inputs)
    latency = _canonical_latency_source(inputs)
    data_manifest = _canonical_data_manifest(inputs)
    data_quality = _matching_data_quality(inputs, data_manifest)
    weather_dir = (
        config.weather_run_dir
        if config.weather_run_dir is not None
        else config.research_dir / "weather_public_run"
    )
    standard_config = (
        config.research_dir / "CONSTRAINT_STANDARD_NEGRISK_CONFIG.json"
    )
    augmented_config = (
        config.research_dir / "CONSTRAINT_AUGMENTED_NEGRISK_CONFIG.json"
    )
    capture_config = config.research_dir / "FORWARD_CAPTURE_CONFIG.json"
    lines = [
        "# Reproduction",
        "",
        "除最后单独标出的 live network canary 外，以下验证均为离线、只读输入；"
        "不读取 `.env`、不使用凭证、不提交订单。需要写出 replay 结果的命令只写入"
        " `mktemp` 临时目录。",
        "",
        "## Canonical paths",
        "",
        f"- Weather run: `{weather_dir}`",
        (
            f"- Reward run manifest: `{reward_manifest}`"
            if reward_manifest is not None
            else "- Reward run manifest: unavailable (no verified v2 run)"
        ),
        (
            f"- Standard constraint run: `{constraints['standard'].path.parent}`"
            if "standard" in constraints
            else "- Standard constraint run: unavailable"
        ),
        (
            f"- Augmented constraint run: `{constraints['augmented'].path.parent}`"
            if "augmented" in constraints
            else "- Augmented constraint run: unavailable"
        ),
        (
            f"- Latency artifact: `{latency.path}`"
            if latency is not None
            else "- Latency artifact: unavailable"
        ),
        (
            f"- Data manifest: `{data_manifest.path}`"
            if data_manifest is not None
            else "- Data manifest: unavailable"
        ),
        (
            f"- Data quality: `{data_quality.path}`"
            if data_quality is not None
            else "- Data quality: unavailable"
        ),
        f"- Capture config: `{capture_config}`",
        f"- Standard constraint config: `{standard_config}`",
        f"- Augmented constraint config: `{augmented_config}`",
        "",
        "## Offline manifest and artifact verification",
        "",
        "```bash",
        'repro_dir="$(mktemp -d)"',
        "./.venv/bin/python - <<'PY'",
        "from pathlib import Path",
        "from src.edge_lab.weather_experiment_runner import verify_weather_run_manifest",
        f"verify_weather_run_manifest(Path({json.dumps(str(weather_dir))}))",
        "print('weather manifest verified')",
        "PY",
    ]
    if reward_manifest is not None:
        lines.append(
            "./.venv/bin/python scripts/run_reward_experiment.py "
            f"--replay-manifest {quote(reward_manifest)}"
        )
    else:
        lines.append(
            "# unavailable now; command shape: ./.venv/bin/python "
            "scripts/run_reward_experiment.py --replay-manifest "
            "<canonical-reward-v2-RUN_MANIFEST.json>"
        )
    lines.extend(
        [
            "./.venv/bin/python scripts/run_latency_capture_experiment.py --validate-only",
        ]
    )
    if latency is not None:
        lines.append(
            "./.venv/bin/python scripts/run_latency_capture_experiment.py "
            f"--pin-from {quote(latency.path)} "
            '--output "$repro_dir/latency-replay.json"'
        )
    lines.extend(
        [
            "./.venv/bin/python scripts/run_edge_capture.py "
            f"--config {quote(capture_config)} --validate-only",
            "```",
            "",
            "## Offline constraint replay",
            "",
            "```bash",
        ]
    )
    for kind, config_path in (
        ("standard", standard_config),
        ("augmented", augmented_config),
    ):
        source = constraints.get(kind)
        if source is None:
            lines.append(
                "# unavailable now; command shape: ./.venv/bin/python "
                "scripts/run_constraint_experiment.py "
                f"--config {quote(config_path)} --output-root \"$repro_dir\" "
                "--replay-source-run <canonical-run-dir> "
                "--replay-source-repro-sha256 <sha256>"
            )
            continue
        reproducibility = source.path.parent / "REPRODUCIBILITY.json"
        if reproducibility.is_file():
            repro_sha = _sha256_bytes(reproducibility.read_bytes())
            lines.append(
                "./.venv/bin/python scripts/run_constraint_experiment.py "
                f"--config {quote(config_path)} "
                '--output-root "$repro_dir" '
                f"--run-id verify-{kind} "
                f"--replay-source-run {quote(source.path.parent)} "
                f"--replay-source-repro-sha256 {repro_sha}"
            )
        else:
            lines.append(
                "# canonical source lacks REPRODUCIBILITY.json; command shape: "
                "./.venv/bin/python scripts/run_constraint_experiment.py "
                f"--config {quote(config_path)} --output-root \"$repro_dir\" "
                f"--replay-source-run {quote(source.path.parent)} "
                "--replay-source-repro-sha256 <sha256>"
            )
    lines.extend(
        [
            "```",
            "",
            "## Build and test",
            "",
            "```bash",
            "./.venv/bin/python scripts/build_edge_discovery_bundle.py \\",
            f"  --research-dir {quote(config.research_dir)} \\",
            f"  --output-dir {quote(config.output_dir)} \\",
            f"  --report-date {quote(config.report_date)}"
            + (" \\" if config.weather_run_dir is not None else ""),
            *(
                [f"  --weather-run-dir {quote(config.weather_run_dir)}"]
                if config.weather_run_dir is not None
                else []
            ),
            "./.venv/bin/python -m compileall -q src scripts",
            "./.venv/bin/pytest -q",
            "git diff --check",
            "```",
            "",
            "输入哈希与缺失原因固定在 `FINAL_BUNDLE_CONFIG.json`。在输入文件集不变时，"
            "重复运行应生成逐字节相同的八个交付文件。",
            "",
            "## Optional live network canary",
            "",
            "以下命令会访问外部公共 WebSocket，**不属于离线复现或盈利验证**，本次"
            " bundle 构建不会执行它：",
            "",
            "```bash",
            "./.venv/bin/pytest -q -m network tests/test_websocket_canary.py",
            "```",
            "",
            _input_table(inputs),
            "",
        ]
    )
    return "\n".join(lines)


def _csv_bytes() -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=TRADE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    return buffer.getvalue().encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_bytes(row) + b"\n" for row in rows)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def build_final_bundle(config: FinalBundleConfig) -> FinalBundle:
    """Build all fixed-name final artifacts without network or execution calls."""

    inputs = _discover_inputs(config)
    strategies = _build_strategies(inputs)
    as_of = _as_of(inputs, config.report_date)
    registry = _registry_rows(inputs, strategies, as_of)
    canonical_manifest = _canonical_data_manifest(inputs)
    matching_quality = _matching_data_quality(inputs, canonical_manifest)
    canonical_reward = _canonical_reward_source(inputs)
    reward_verification = (
        _verified_reward_manifest(canonical_reward, inputs)[0]
        if canonical_reward is not None
        else None
    )
    reward_manifest_source = (
        next(
            (
                source
                for source in inputs
                if source.kind == "reward_manifest"
                and canonical_reward is not None
                and source.path.parent == canonical_reward.path.parent
            ),
            None,
        )
        if canonical_reward is not None
        else None
    )
    canonical_constraints = _canonical_constraint_sources(inputs)
    canonical_latency = _canonical_latency_source(inputs)
    weather_manifest_source = next(
        (
            source
            for source in inputs
            if source.kind == "weather" and source.payload is not None
        ),
        None,
    )
    weather_experiment_source, weather_binding_error = (
        _verified_weather_experiment_source(inputs)
    )
    config_payload = {
        "schema": CONFIG_SCHEMA,
        "report_date": config.report_date,
        "as_of": as_of,
        "research_dir": str(config.research_dir),
        "output_dir": str(config.output_dir),
        "weather_run_dir": (
            None if config.weather_run_dir is None else str(config.weather_run_dir)
        ),
        "canonical_data_manifest": (
            {
                "path": canonical_manifest.relative_path,
                "file_sha256": canonical_manifest.sha256,
                "data_manifest_hash": canonical_manifest.payload.get(
                    "data_manifest_hash"
                ),
                "matching_data_quality_path": (
                    None
                    if matching_quality is None
                    else matching_quality.relative_path
                ),
                "matching_data_quality_status": (
                    None
                    if matching_quality is None
                    else matching_quality.payload.get("status")
                ),
            }
            if canonical_manifest is not None
            else {
                "path": None,
                "file_sha256": None,
                "data_manifest_hash": None,
                "matching_data_quality_path": None,
                "reason": "no schema-valid, self-hash-valid manifest",
            }
        ),
        "canonical_sources": {
            "rewards": (
                {
                    "artifact_path": canonical_reward.relative_path,
                    "artifact_sha256": canonical_reward.sha256,
                    "schema_version": canonical_reward.payload.get(
                        "schema_version"
                    ),
                    "manifest_path": (
                        None
                        if reward_manifest_source is None
                        else reward_manifest_source.relative_path
                    ),
                    "manifest_sha256": (
                        None
                        if reward_manifest_source is None
                        else reward_manifest_source.sha256
                    ),
                    "manifest_binding_verified": (
                        reward_verification is not None
                    ),
                }
                if canonical_reward is not None
                else {
                    "artifact_path": None,
                    "reason": "no valid reward experiment",
                }
            ),
            "constraints": {
                kind: {
                    "summary_path": source.relative_path,
                    "summary_sha256": source.sha256,
                    "run_id": source.payload.get("run_id"),
                    "schema_version": source.payload.get("schema_version"),
                }
                for kind, source in sorted(canonical_constraints.items())
            },
            "latency": (
                {
                    "artifact_path": canonical_latency.relative_path,
                    "artifact_sha256": canonical_latency.sha256,
                    "experiment_id": canonical_latency.payload.get(
                        "experiment_id"
                    ),
                    "schema_version": canonical_latency.payload.get(
                        "schema_version"
                    ),
                }
                if canonical_latency is not None
                else {
                    "artifact_path": None,
                    "reason": "no valid latency experiment",
                }
            ),
            "weather": {
                "manifest_path": (
                    None
                    if weather_manifest_source is None
                    else weather_manifest_source.relative_path
                ),
                "manifest_sha256": (
                    None
                    if weather_manifest_source is None
                    else weather_manifest_source.sha256
                ),
                "experiment_path": (
                    None
                    if weather_experiment_source is None
                    else weather_experiment_source.relative_path
                ),
                "experiment_sha256": (
                    None
                    if weather_experiment_source is None
                    else weather_experiment_source.sha256
                ),
                "manifest_binding_verified": (
                    weather_binding_error is None
                    and weather_experiment_source is not None
                ),
                "manifest_binding_error": weather_binding_error,
            },
        },
        "inputs": [source.snapshot() for source in inputs],
        "validation_policy": _validation_policy(),
        "live_trading_enabled": False,
        "new_orders_disabled": True,
        "network_access": False,
    }
    backtest = _backtest_payload(
        report_date=config.report_date,
        as_of=as_of,
        inputs=inputs,
        strategies=strategies,
    )
    output = config.output_dir
    paths = FinalBundle(
        backtest_results=output / "BACKTEST_RESULTS.json",
        trades=output / "TRADES.csv",
        metrics=output / "METRICS.md",
        data_quality=output / "DATA_QUALITY.md",
        live_readiness=output / "LIVE_READINESS.md",
        experiment_registry=output / "EXPERIMENT_REGISTRY.jsonl",
        reproduction=output / "REPRODUCTION.md",
        config_snapshot=output / "FINAL_BUNDLE_CONFIG.json",
    )
    payloads = {
        paths.backtest_results: _canonical_bytes(backtest) + b"\n",
        paths.trades: _csv_bytes(),
        paths.metrics: _metrics_markdown(strategies, as_of).encode("utf-8"),
        paths.data_quality: _data_quality_markdown(inputs, as_of).encode("utf-8"),
        paths.live_readiness: _live_readiness_markdown(
            strategies, inputs
        ).encode("utf-8"),
        paths.experiment_registry: _jsonl_bytes(registry),
        paths.reproduction: _reproduction_markdown(config, inputs).encode("utf-8"),
        paths.config_snapshot: _canonical_bytes(config_payload) + b"\n",
    }
    for path, data in payloads.items():
        _atomic_write(path, data)
    return paths


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build the offline, fail-closed Edge Discovery final artifact bundle."
        )
    )
    parser.add_argument(
        "--research-dir",
        type=Path,
        default=Path("research/edge_discovery_2026-07-24"),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--report-date", default="2026-07-24")
    parser.add_argument("--weather-run-dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    research = args.research_dir
    output = args.output_dir or research
    bundle = build_final_bundle(
        FinalBundleConfig(
            research_dir=research,
            output_dir=output,
            report_date=args.report_date,
            weather_run_dir=args.weather_run_dir,
        )
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "artifacts": [str(path) for path in bundle.artifact_paths],
                "live_trading_enabled": False,
                "network_access": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
