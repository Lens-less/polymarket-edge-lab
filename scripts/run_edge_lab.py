#!/usr/bin/env python3
"""Run the read-only paired-outcome edge research lab."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.edge_lab.compatibility import compatibility_audit
from src.edge_lab.economics import json_safe
from src.edge_lab.network_safety import safe_error_details
from src.edge_lab.public_api import PublicPolymarketAPI
from src.edge_lab.replay import HistoricalReplay, ReplayConfig
from src.edge_lab.scanner import EdgeScanner, ScanConfig


def decimal_arg(value: str) -> Decimal:
    try:
        return Decimal(value)
    except Exception as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def write_report(report: Any, output: str | None) -> None:
    rendered = json.dumps(json_safe(report), indent=2, ensure_ascii=False)
    if output:
        path = Path(output).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
        print(path)
    else:
        print(rendered)


def observe(
    scanner: EdgeScanner,
    condition_id: str,
    *,
    minutes: Decimal,
    interval_seconds: Decimal,
    output: str,
    size: Decimal | None,
    distance_fraction: Decimal,
    stress_ticks: int,
    competition_multiplier: Decimal,
) -> None:
    """Persist independent public snapshots; still never submits an order."""
    path = Path(output).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    end_monotonic = time.monotonic() + float(minutes * Decimal("60"))
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        while True:
            started = time.monotonic()
            try:
                report = scanner.evaluate_condition(
                    condition_id,
                    quote_size=size,
                    distance_fraction=distance_fraction,
                    stress_ticks=stress_ticks,
                    competition_multiplier=competition_multiplier,
                )
                record = {
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "ok": True,
                    "report": report,
                }
            except Exception as exc:
                record = {
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "ok": False,
                    "error": safe_error_details(
                        exc,
                        code="edge_observation_failed",
                    ),
                }
            handle.write(json.dumps(json_safe(record), ensure_ascii=False) + "\n")
            handle.flush()
            count += 1
            print(f"snapshot={count} ok={record['ok']}", flush=True)
            if time.monotonic() >= end_monotonic:
                break
            delay = max(0.0, float(interval_seconds) - (time.monotonic() - started))
            time.sleep(delay)
    print(path)


def parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--size", type=decimal_arg)
    shared.add_argument(
        "--distance-fraction", type=decimal_arg, default=Decimal("0.5")
    )
    shared.add_argument("--stress-ticks", type=int, default=2)
    shared.add_argument(
        "--competition-multiplier", type=decimal_arg, default=Decimal("3")
    )
    shared.add_argument(
        "--reward-multiplier", type=decimal_arg, default=Decimal("1")
    )

    root = argparse.ArgumentParser(
        description=(
            "Read-only Polymarket edge lab. It never reads credentials or "
            "submits orders."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    evaluate_cmd = commands.add_parser("evaluate", parents=[shared])
    evaluate_cmd.add_argument("condition_id")
    evaluate_cmd.add_argument("--output")

    scan_cmd = commands.add_parser("scan", parents=[shared])
    scan_cmd.add_argument("--pages", type=int, default=4)
    scan_cmd.add_argument("--page-size", type=int, default=500)
    scan_cmd.add_argument("--max-evaluations", type=int, default=80)
    scan_cmd.add_argument(
        "--min-daily-reward", type=decimal_arg, default=Decimal("1")
    )
    scan_cmd.add_argument(
        "--min-volume-24h", type=decimal_arg, default=Decimal("1")
    )
    scan_cmd.add_argument("--output")

    replay_cmd = commands.add_parser("replay", parents=[shared])
    replay_cmd.add_argument("condition_id")
    replay_cmd.add_argument("--hours", type=decimal_arg, default=Decimal("24"))
    replay_cmd.add_argument("--alignment-tolerance-ms", type=int, default=15_000)
    replay_cmd.add_argument("--max-records", type=int, default=10_000)
    replay_cmd.add_argument("--output")

    observe_cmd = commands.add_parser("observe", parents=[shared])
    observe_cmd.add_argument("condition_id")
    observe_cmd.add_argument("--minutes", type=decimal_arg, default=Decimal("10"))
    observe_cmd.add_argument(
        "--interval-seconds", type=decimal_arg, default=Decimal("15")
    )
    observe_cmd.add_argument("--output", required=True)

    audit_cmd = commands.add_parser("audit")
    audit_cmd.add_argument("--output")

    complete_cmd = commands.add_parser("complete-set-scan")
    complete_cmd.add_argument("--max-markets", type=int, default=2_000)
    complete_cmd.add_argument("--page-size", type=int, default=500)
    complete_cmd.add_argument(
        "--sizes", type=decimal_arg, nargs="+", default=[
            Decimal("5"),
            Decimal("10"),
            Decimal("25"),
            Decimal("50"),
        ]
    )
    complete_cmd.add_argument(
        "--execution-buffer-per-share",
        type=decimal_arg,
        default=Decimal("0.002"),
    )
    complete_cmd.add_argument("--output")
    return root


def main() -> int:
    args = parser().parse_args()
    api = PublicPolymarketAPI()
    scanner = EdgeScanner(api)
    size = getattr(args, "size", None)

    if args.command == "evaluate":
        report = scanner.evaluate_condition(
            args.condition_id,
            quote_size=size,
            distance_fraction=args.distance_fraction,
            stress_ticks=args.stress_ticks,
            competition_multiplier=args.competition_multiplier,
            reward_multiplier=args.reward_multiplier,
        )
        write_report(report, args.output)
    elif args.command == "scan":
        report = scanner.scan(
            ScanConfig(
                pages=args.pages,
                page_size=args.page_size,
                max_evaluations=args.max_evaluations,
                min_daily_reward=args.min_daily_reward,
                min_volume_24h=args.min_volume_24h,
                quote_size=size or Decimal("20"),
                distance_fraction=args.distance_fraction,
                stress_ticks=args.stress_ticks,
                competition_multiplier=args.competition_multiplier,
                reward_multiplier=args.reward_multiplier,
            )
        )
        write_report(report, args.output)
    elif args.command == "replay":
        report = HistoricalReplay(api).run(
            args.condition_id,
            ReplayConfig(
                hours=args.hours,
                quote_size=size,
                distance_fraction=args.distance_fraction,
                stress_ticks=args.stress_ticks,
                competition_multiplier=args.competition_multiplier,
                reward_multiplier=args.reward_multiplier,
                alignment_tolerance_ms=args.alignment_tolerance_ms,
                max_records_per_token=args.max_records,
            ),
        )
        write_report(report, args.output)
    elif args.command == "observe":
        observe(
            scanner,
            args.condition_id,
            minutes=args.minutes,
            interval_seconds=args.interval_seconds,
            output=args.output,
            size=size,
            distance_fraction=args.distance_fraction,
            stress_ticks=args.stress_ticks,
            competition_multiplier=args.competition_multiplier,
        )
    elif args.command == "audit":
        write_report(compatibility_audit(), args.output)
    elif args.command == "complete-set-scan":
        report = scanner.scan_active_complete_sets(
            max_markets=args.max_markets,
            page_size=args.page_size,
            sizes=tuple(args.sizes),
            execution_buffer_per_share=args.execution_buffer_per_share,
        )
        write_report(report, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
