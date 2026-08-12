#!/usr/bin/env python3
"""Run the public-only cross-market/Neg-Risk snapshot experiment."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Sequence
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.edge_lab.constraint_experiment import (  # noqa: E402
    CONFIG_SCHEMA_VERSION,
    PublicConstraintSources,
    PublicGETSession,
    RecordedConstraintSources,
    load_constraint_experiment_spec,
    run_constraint_experiment,
)
from src.edge_lab.sources import PublicSourcesClient  # noqa: E402


def _proxy_mapping(proxy_url: str | None) -> dict[str, str]:
    if proxy_url is None:
        return {}
    parsed = urlsplit(proxy_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "proxy must be an unauthenticated loopback HTTP(S) URL"
        )
    return {"http": proxy_url, "https": proxy_url}


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate pinned logical and Neg-Risk bundles using public "
            "Gamma/CLOB GETs only."
        )
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--proxy")
    parser.add_argument(
        "--replay-source-run",
        type=Path,
        help=(
            "recompute from one saved run's manifest/raw bodies without "
            "network access"
        ),
    )
    parser.add_argument(
        "--replay-source-repro-sha256",
        help=(
            "externally trusted SHA-256 of the source run's "
            "REPRODUCIBILITY.json"
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the config and proxy boundary without network or writes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = load_constraint_experiment_spec(args.config)
    proxies = _proxy_mapping(args.proxy)
    if args.replay_source_run is not None and args.proxy is not None:
        raise ValueError("--proxy cannot be combined with --replay-source-run")
    if (args.replay_source_run is None) != (
        args.replay_source_repro_sha256 is None
    ):
        raise ValueError(
            "--replay-source-run and --replay-source-repro-sha256 "
            "must be supplied together"
        )
    if args.validate_only:
        print(
            json.dumps(
                {
                    "schema_version": CONFIG_SCHEMA_VERSION,
                    "valid": True,
                    "experiment_id": spec.experiment_id,
                    "market_count": len(spec.markets),
                    "analysis_count": len(spec.analyses),
                    "network_used": False,
                    "new_orders_disabled": True,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.replay_source_run is not None:
        summary = run_constraint_experiment(
            spec,
            source=RecordedConstraintSources(
                args.replay_source_run,
                expected_reproducibility_sha256=(
                    args.replay_source_repro_sha256
                ),
            ),
            output_root=args.output_root,
            run_id=args.run_id or _default_run_id(),
        )
    else:
        session = PublicGETSession()
        session.trust_env = False
        session.max_redirects = 0
        session.proxies.update(proxies)
        client = PublicSourcesClient(
            session=session,
            timeout=20,
            retries=2,
            rate_per_second=8,
            burst=2,
            min_interval_seconds=0.05,
        )
        try:
            summary = run_constraint_experiment(
                spec,
                source=PublicConstraintSources(client),
                output_root=args.output_root,
                run_id=args.run_id or _default_run_id(),
            )
        finally:
            session.close()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
