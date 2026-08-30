"""CLI entrypoint for the V0.2 profit-system offline acceptance surface."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from pathlib import Path

from .readiness import JSONValue, dumps_json, evaluate_doctor
from .reports import (
    build_acceptance_report,
    build_desk_report,
    build_replay_report,
    build_scan_report,
    build_shadow_report,
    build_status_report,
    write_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymm",
        description=(
            "Offline-safe V0.2 readiness, scan, replay, shadow, desk, and status tooling."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor",
        help="Run the fail-closed live preflight without reading or printing secrets.",
    )
    doctor.add_argument(
        "--config",
        type=Path,
        help="Path to a sanitized canary config JSON document.",
    )
    doctor.add_argument("--output", type=Path, help="Optional JSON output path.")

    scan = subparsers.add_parser("scan", help="Emit a deterministic scan report.")
    scan.add_argument("--strategy", default="arbitrage")
    scan.add_argument(
        "--scenario",
        choices=("acceptance_pass", "acceptance_no_go"),
        default="acceptance_pass",
    )
    scan.add_argument("--output", type=Path, help="Optional JSON output path.")

    replay = subparsers.add_parser("replay", help="Emit a deterministic replay report.")
    replay.add_argument("--strategy", default="maker")
    replay.add_argument(
        "--scenario",
        choices=("acceptance_pass", "acceptance_no_go"),
        default="acceptance_no_go",
    )
    replay.add_argument("--output", type=Path, help="Optional JSON output path.")

    shadow = subparsers.add_parser("shadow", help="Emit a deterministic shadow report.")
    shadow.add_argument("--strategy", default="maker")
    shadow.add_argument(
        "--scenario",
        choices=("acceptance_pass", "acceptance_no_go"),
        default="acceptance_no_go",
    )
    shadow.add_argument("--output", type=Path, help="Optional JSON output path.")

    acceptance = subparsers.add_parser(
        "acceptance",
        help="Emit deterministic Track A/Track B Research -> Replay -> Paper -> Shadow evidence.",
    )
    acceptance.add_argument("--track", choices=("all", "A", "B"), default="all")
    acceptance.add_argument("--output", type=Path, help="Optional JSON output path.")

    desk = subparsers.add_parser("desk", help="Show the offline operator overview.")
    desk.add_argument("--config", type=Path, help="Optional sanitized canary config.")
    desk.add_argument("--output", type=Path, help="Optional JSON output path.")

    status = subparsers.add_parser(
        "status",
        help="Show the current fail-closed readiness status.",
    )
    status.add_argument("--config", type=Path, help="Optional sanitized canary config.")
    status.add_argument("--output", type=Path, help="Optional JSON output path.")

    return parser


def _emit(document: Mapping[str, JSONValue], output: Path | None) -> int:
    payload = dumps_json(dict(document))
    if output is not None:
        write_report(output, dict(document))
    print(payload, end="")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return _emit(
                evaluate_doctor(args.config).to_document(),
                args.output,
            )
        if args.command == "scan":
            return _emit(
                build_scan_report(strategy=args.strategy, scenario=args.scenario),
                args.output,
            )
        if args.command == "replay":
            return _emit(
                build_replay_report(strategy=args.strategy, scenario=args.scenario),
                args.output,
            )
        if args.command == "shadow":
            return _emit(
                build_shadow_report(strategy=args.strategy, scenario=args.scenario),
                args.output,
            )
        if args.command == "acceptance":
            return _emit(build_acceptance_report(track=args.track), args.output)
        if args.command == "desk":
            return _emit(build_desk_report(args.config), args.output)
        if args.command == "status":
            return _emit(build_status_report(args.config), args.output)
    except ValueError as exc:
        parser.error(str(exc))
    raise RuntimeError(f"unsupported command: {args.command}")


__all__ = ["build_parser", "main"]
