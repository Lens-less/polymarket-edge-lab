"""Command-line entry point for the offline capture latency experiment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .compatibility import LiveExecutionBlocked, assert_new_orders_disabled
from .latency_capture_experiment import (
    CaptureLatencyConfig,
    DEFAULT_CONDITION_ID,
    SCHEMA_VERSION,
    run_capture_latency_experiment,
    save_capture_latency_experiment,
)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = _REPOSITORY_ROOT / "data" / "edge_discovery_2026-07-24"
DEFAULT_OUTPUT_DIR = (
    _REPOSITORY_ROOT
    / "research"
    / "edge_discovery_2026-07-24"
)


def _verify_live_order_guard() -> None:
    try:
        assert_new_orders_disabled()
    except LiveExecutionBlocked:
        return
    raise RuntimeError("new-order safety guard is not active")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read immutable local recorder JSONL and produce a descriptive, "
            "receive-time BTC/Polymarket fixed-lag report. No network or order "
            "path is available."
        )
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--condition-id", default=DEFAULT_CONDITION_ID)
    parser.add_argument("--source-symbol", default="btcusdt")
    parser.add_argument(
        "--lag-grid-ms",
        type=int,
        nargs="+",
        default=[0, 100, 250, 500, 1_000, 2_000, 5_000],
    )
    parser.add_argument("--max-quote-age-ms", type=int, default=30_000)
    parser.add_argument(
        "--min-capture-duration-ms",
        type=int,
        default=3_600_000,
    )
    parser.add_argument("--min-source-observations", type=int, default=100)
    parser.add_argument("--min-lag-samples", type=int, default=100)
    parser.add_argument(
        "--min-independent-settled-markets",
        type=int,
        default=100,
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "New report path. Existing files and paths inside --data-root are "
            "always rejected. Defaults to a content-versioned filename."
        ),
    )
    parser.add_argument(
        "--pin-from",
        type=Path,
        help=(
            "Replay exactly the finalized batch paths pinned by a prior "
            "capture-latency report."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate safety and arguments without reading capture files.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _verify_live_order_guard()
    pinned_raw_paths: tuple[str, ...] = ()
    prior: dict[str, object] | None = None
    prior_pinned_batches: list[dict[str, object]] | None = None
    if args.pin_from is not None:
        loaded_prior = json.loads(
            args.pin_from.read_text(encoding="utf-8")
        )
        if (
            not isinstance(loaded_prior, dict)
            or loaded_prior.get("schema_version") != SCHEMA_VERSION
            or not isinstance(loaded_prior.get("configuration"), dict)
            or not isinstance(loaded_prior.get("frozen_input"), dict)
            or not isinstance(
                loaded_prior["frozen_input"].get("pinned_batches"),
                list,
            )
            or not isinstance(
                loaded_prior.get("input_digest_sha256"),
                str,
            )
            or not isinstance(loaded_prior.get("experiment_id"), str)
        ):
            raise ValueError("pin-from must be a capture-latency v1 report")
        prior = loaded_prior
        batches = prior["frozen_input"]["pinned_batches"]
        if not batches:
            raise ValueError(
                "pin-from must contain at least one finalized batch pin"
            )
        pins: list[str] = []
        checked_batches: list[dict[str, object]] = []
        for batch in batches:
            checksum = (
                batch.get("checksum")
                if isinstance(batch, dict)
                else None
            )
            if (
                not isinstance(batch, dict)
                or not isinstance(batch.get("raw_path"), str)
                or not isinstance(batch.get("manifest_path"), str)
                or not isinstance(checksum, dict)
                or checksum.get("algorithm") != "sha256"
                or not isinstance(checksum.get("value"), str)
                or not isinstance(checksum.get("bytes"), int)
                or not isinstance(checksum.get("lines"), int)
            ):
                raise ValueError("pin-from contains an invalid batch pin")
            pins.append(batch["raw_path"])
            checked_batches.append(batch)
        if len(set(pins)) != len(pins):
            raise ValueError("pin-from contains duplicate batch pins")
        pinned_raw_paths = tuple(pins)
        prior_pinned_batches = checked_batches
    config = CaptureLatencyConfig(
        data_root=args.data_root,
        condition_id=args.condition_id,
        source_symbol=args.source_symbol,
        lag_grid_ms=tuple(args.lag_grid_ms),
        max_quote_age_ms=args.max_quote_age_ms,
        min_capture_duration_ms=args.min_capture_duration_ms,
        min_source_observations=args.min_source_observations,
        min_lag_samples=args.min_lag_samples,
        min_independent_settled_markets=(
            args.min_independent_settled_markets
        ),
        pinned_raw_paths=pinned_raw_paths,
    )
    if prior is not None:
        current_identity = config.to_dict()
        prior_identity = dict(prior["configuration"])
        for value in (current_identity, prior_identity):
            value.pop("data_root", None)
            value.pop("pinned_raw_paths", None)
        if current_identity != prior_identity:
            raise ValueError(
                "pin-from configuration does not match the current "
                "experiment configuration"
            )
    if args.validate_only:
        print(
            json.dumps(
                {
                    "valid": True,
                    "live_order_guard_active": True,
                    "network_requests": False,
                    "orders_submitted": False,
                    "configuration": config.to_dict(),
                    "output": (
                        str(args.output.resolve())
                        if args.output is not None
                        else "auto_content_versioned"
                    ),
                },
                sort_keys=True,
            )
        )
        return 0

    result = run_capture_latency_experiment(config)
    if prior is not None:
        assert prior_pinned_batches is not None
        if (
            result["frozen_input"]["pinned_batches"]
            != prior_pinned_batches
            or result["input_digest_sha256"]
            != prior["input_digest_sha256"]
            or result["experiment_id"] != prior["experiment_id"]
        ):
            raise ValueError(
                "pin-from exact replay failed: input checksums, method "
                "identity, or result digest changed"
            )
    output = args.output or (
        DEFAULT_OUTPUT_DIR
        / f"LATENCY_CAPTURE_EXPERIMENT_v1_{result['experiment_id']}.json"
    )
    target = save_capture_latency_experiment(result, output)
    print(
        json.dumps(
            {
                "result_path": str(target),
                "experiment_id": result["experiment_id"],
                "status": result["status"],
                "source_observations": result["coverage"][
                    "source_observations"
                ],
                "quote_observations": result["coverage"][
                    "quote_observations"
                ],
                "orders_submitted": False,
                "network_requests": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())


__all__ = ["DEFAULT_DATA_ROOT", "DEFAULT_OUTPUT_DIR", "main"]
