"""CLI orchestration for the public-only liquidity-reward experiment."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import time
from typing import Sequence
from urllib.parse import urlsplit

import requests

from .compatibility import LiveExecutionBlocked, assert_new_orders_disabled
from .reward_archive import (
    RecordingClock,
    RecordingRewardSources,
    replay_reward_run,
    save_reward_run,
)
from .network_safety import configure_public_session
from .reward_experiment import (
    DEFAULT_REWARD_ASSET_ID,
    RewardExperimentConfig,
    run_reward_experiment,
)
from .sources import PublicSourcesClient


DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "edge_discovery_2026-07-24"
)


def _local_proxy_mapping(proxy_url: str | None) -> dict[str, str]:
    if proxy_url is None:
        return {}
    parsed = urlsplit(proxy_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "proxy must be an unauthenticated loopback HTTP(S) URL"
        )
    return {"http": proxy_url, "https": proxy_url}


def _verify_live_order_guard() -> None:
    try:
        assert_new_orders_disabled()
    except LiveExecutionBlocked:
        return
    raise RuntimeError("new-order safety guard is not active")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a credential-free, public-only Polymarket liquidity-reward "
            "snapshot experiment."
        )
    )
    parser.add_argument("--max-markets", type=int, default=10)
    parser.add_argument("--max-reward-pages", type=int, default=25)
    parser.add_argument("--max-book-age-ms", type=int, default=120_000)
    parser.add_argument("--max-book-skew-ms", type=int, default=5_000)
    parser.add_argument(
        "--quote-distance-fraction",
        type=Decimal,
        default=Decimal("0.5"),
    )
    parser.add_argument("--paired-fills-per-day", type=int, default=1)
    parser.add_argument("--adverse-one-leg-fills-per-day", type=int, default=1)
    parser.add_argument(
        "--reward-asset-id",
        default=DEFAULT_REWARD_ASSET_ID,
        help=(
            "Evaluate only rows whose reward configurations use exactly this "
            "asset, preventing cross-asset rate ranking."
        ),
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--proxy")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate safety/configuration without making network requests.",
    )
    parser.add_argument(
        "--replay-manifest",
        type=Path,
        help=(
            "Verify and fully recompute one immutable reward run with zero "
            "network access."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _verify_live_order_guard()
    if args.replay_manifest is not None:
        replay = replay_reward_run(args.replay_manifest)
        print(json.dumps(replay, sort_keys=True))
        return 0
    proxy_mapping = _local_proxy_mapping(args.proxy)
    config = RewardExperimentConfig(
        max_markets=args.max_markets,
        max_reward_pages=args.max_reward_pages,
        max_book_age_ms=args.max_book_age_ms,
        max_book_skew_ms=args.max_book_skew_ms,
        quote_distance_fraction=args.quote_distance_fraction,
        paired_fills_per_day=args.paired_fills_per_day,
        adverse_one_leg_fills_per_day=args.adverse_one_leg_fills_per_day,
        reward_asset_id=args.reward_asset_id,
    )
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    if args.validate_only:
        print(
            json.dumps(
                {
                    "valid": True,
                    "live_order_guard_active": True,
                    "config": config.to_dict(),
                    "output_dir": str(args.output_dir.resolve()),
                    "proxy": "loopback_explicit" if proxy_mapping else None,
                },
                sort_keys=True,
            )
        )
        return 0

    if not proxy_mapping:
        raise ValueError(
            "live public reward capture requires an explicit unauthenticated "
            "loopback proxy"
        )
    session = requests.Session()
    session.trust_env = False
    session.proxies.update(proxy_mapping)
    configure_public_session(session)
    sources = PublicSourcesClient(
        session=session,
        timeout=args.timeout,
        retries=2,
        rate_per_second=8,
        burst=2,
        min_interval_seconds=0.05,
    )
    try:
        recording = RecordingRewardSources(sources)
        decision_clock = RecordingClock(time.time)
        result = run_reward_experiment(
            recording,
            config=config,
            clock=decision_clock,
        )
        run_dir = save_reward_run(
            result,
            recording,
            clock_values=decision_clock.values,
            output_dir=args.output_dir.resolve(),
        )
        target = run_dir / "EXPERIMENT.json"
        manifest = run_dir / "RUN_MANIFEST.json"
    finally:
        session.close()
    print(
        json.dumps(
            {
                "result_path": str(target),
                "run_manifest": str(manifest),
                "experiment_id": result["experiment_id"],
                "classification_counts": result["classification_counts"],
                "selected_markets": result["universe"]["selected_markets"],
                "pagination_complete": result["universe"][
                    "pagination_complete"
                ],
                "orders_submitted": False,
            },
            sort_keys=True,
        )
    )
    return 0


__all__ = ["DEFAULT_OUTPUT_DIR", "DEFAULT_REWARD_ASSET_ID", "main"]
