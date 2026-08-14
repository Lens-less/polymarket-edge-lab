#!/usr/bin/env python3
"""Progress-oriented watcher for BTC paper and raw-capture tracks."""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.build_btc_regime_candidate_scoreboard import build_candidate_scoreboard


def _parse_utc(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_object_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Alert:
    track: str
    kind: str
    severity: str
    detail: str


@dataclass(frozen=True)
class AlertRecord:
    key: str
    severity: str
    subject: str
    body: dict[str, Any]


class Publisher:
    def publish(self, alert: AlertRecord) -> None:
        raise NotImplementedError


class JsonLinePublisher(Publisher):
    def publish(self, alert: AlertRecord) -> None:
        print(
            json.dumps(
                {
                    "key": alert.key,
                    "severity": alert.severity,
                    "subject": alert.subject,
                    "body": alert.body,
                },
                sort_keys=True,
            )
        )


class AwsCliSnsPublisher(Publisher):
    """Use the instance role through AWS CLI without a new Python dependency."""

    def __init__(self, topic_arn: str) -> None:
        if not topic_arn.startswith("arn:aws:sns:"):
            raise ValueError("invalid SNS topic ARN")
        self.topic_arn = topic_arn
        self.region = topic_arn.split(":", 5)[3]
        self.aws = shutil.which("aws")
        if self.aws is None:
            raise RuntimeError("AWS CLI is required for SNS publishing")

    def publish(self, alert: AlertRecord) -> None:
        subprocess.run(
            [
                self.aws,
                "sns",
                "publish",
                "--region",
                self.region,
                "--topic-arn",
                self.topic_arn,
                "--subject",
                alert.subject[:100],
                "--message",
                json.dumps(alert.body, sort_keys=True),
            ],
            check=True,
            capture_output=True,
            text=True,
        )


@dataclass(frozen=True)
class TrackState:
    last_report_count: int | None = None
    last_report_change_at: str | None = None
    last_capture_change_at: str | None = None
    active_alerts: tuple[str, ...] = ()


@dataclass(frozen=True)
class WatchState:
    tracks: Mapping[str, TrackState]


@dataclass(frozen=True)
class TrackSpec:
    name: str
    root: Path
    expected_report_period_seconds: int
    expected_capture_period_seconds: int
    kind: str


@dataclass(frozen=True)
class TrackConfig:
    name: str
    status_path: Path
    health_path: Path | None = None
    data_root: Path | None = None
    kind: str = "paper"
    capture_summary_glob: str = ""
    expected_cycle_seconds: int = 900
    expected_regime: str | None = None
    regime_state_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status_path", self.status_path.expanduser().resolve())
        if self.health_path is not None:
            object.__setattr__(self, "health_path", self.health_path.expanduser().resolve())
        if self.data_root is not None:
            object.__setattr__(self, "data_root", self.data_root.expanduser().resolve())
        if self.regime_state_path is not None:
            object.__setattr__(
                self,
                "regime_state_path",
                self.regime_state_path.expanduser().resolve(),
            )


@dataclass(frozen=True)
class HostConfig:
    status_path: Path | None = None
    mem_available_threshold_bytes: int = 300 * 1024 * 1024
    cpu_credit_threshold: float = 100.0
    cpu_credit_decrease_seconds: int = 1800
    process_rss_budgets_bytes: Mapping[str, int] | None = None


@dataclass(frozen=True)
class WatchConfig:
    state_path: Path
    tracks: tuple[TrackConfig, ...]
    sns_topic_arn: str | None
    alerts_path: Path | None = None
    host: HostConfig | None = None
    digest_interval_seconds: int = 3600


@dataclass(frozen=True)
class WatchResult:
    state: dict[str, Any]
    alerts: tuple[Alert, ...]


def _mapping_track_state(
    value: TrackState | Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if isinstance(value, TrackState):
        return {
            "last_report_count": value.last_report_count,
            "last_report_change_at": value.last_report_change_at,
            "last_capture_change_at": value.last_capture_change_at,
            "active_alerts": {key: "page" for key in value.active_alerts},
        }
    if isinstance(value, Mapping):
        return {
            "last_report_count": value.get(
                "last_report_count",
                value.get("last_completed_report_count"),
            ),
            "last_report_change_at": value.get(
                "last_report_change_at",
                value.get("last_report_progress_at"),
            ),
            "last_capture_change_at": value.get(
                "last_capture_change_at",
                value.get("last_capture_progress_at"),
            ),
            "active_alerts": value.get("active_alerts", {}),
            "latest_capture_timestamp": value.get("latest_capture_timestamp"),
            "last_quarantine_count": value.get("last_quarantine_count", 0),
        }
    return {}


def evaluate_track(
    *,
    track: TrackSpec,
    status: Mapping[str, Any],
    health: Mapping[str, Any],
    prior_state: TrackState | Mapping[str, Any] | None,
    now_utc: str,
    latest_capture_timestamp: str | None,
) -> tuple[tuple[Alert, ...], TrackState]:
    now = _parse_utc(now_utc)
    if now is None:
        raise ValueError("now_utc must be a UTC timestamp")
    previous = _mapping_track_state(prior_state)
    alerts: list[Alert] = []
    active_alerts: list[str] = []

    phase = status.get("phase")
    if phase in {"error_wait", "stopped"}:
        alerts.append(Alert(track.name, "phase_unhealthy", "page", f"phase={phase}"))
        active_alerts.append("phase_unhealthy")

    completed_report_count = (
        status.get("completed_report_count")
        if isinstance(status.get("completed_report_count"), int)
        else None
    )
    last_report_change_at = previous.get("last_report_change_at")
    if completed_report_count != previous.get("last_report_count"):
        last_report_change_at = now_utc
    else:
        previous_report_change = _parse_utc(last_report_change_at)
        if (
            completed_report_count is not None
            and previous_report_change is not None
            and now - previous_report_change
            >= timedelta(seconds=track.expected_report_period_seconds * 2)
        ):
            alerts.append(
                Alert(
                    track.name,
                    "report_stalled",
                    "page",
                    "completed_report_count did not advance for two cycles",
                )
            )
            active_alerts.append("report_stalled")

    last_capture_change_at = previous.get("last_capture_change_at")
    previous_capture_marker = (
        previous.get("latest_capture_timestamp")
        or previous.get("last_capture_change_at")
    )
    if latest_capture_timestamp != previous_capture_marker:
        last_capture_change_at = latest_capture_timestamp or now_utc
    else:
        previous_capture_change = _parse_utc(last_capture_change_at)
        if (
            latest_capture_timestamp is not None
            and previous_capture_change is not None
            and now - previous_capture_change
            >= timedelta(seconds=track.expected_capture_period_seconds * 2)
        ):
            alerts.append(
                Alert(
                    track.name,
                    "capture_stalled",
                    "page",
                    "latest capture-summary did not advance for two cycles",
                )
            )
            active_alerts.append("capture_stalled")

    if previous.get("active_alerts") and not active_alerts:
        alerts.append(Alert(track.name, "recovery", "notice", "track recovered"))

    return (
        tuple(alerts),
        TrackState(
            last_report_count=completed_report_count,
            last_report_change_at=last_report_change_at,
            last_capture_change_at=last_capture_change_at,
            active_alerts=tuple(active_alerts),
        ),
    )


def _latest_capture_timestamp(pattern: str) -> str | None:
    latest_generated_at: datetime | None = None
    for candidate in sorted(Path(item) for item in glob.glob(pattern)):
        document = _read_object(candidate)
        generated_at = _parse_utc(
            document.get("generated_at") if isinstance(document, Mapping) else None
        )
        if generated_at is not None and (
            latest_generated_at is None or generated_at > latest_generated_at
        ):
            latest_generated_at = generated_at
    return None if latest_generated_at is None else _utc_text(latest_generated_at)


def evaluate_watch(
    config: WatchConfig,
    *,
    now: datetime,
    previous_state: Mapping[str, Any] | None = None,
) -> WatchResult:
    previous_tracks = (
        previous_state.get("tracks", {}) if isinstance(previous_state, Mapping) else {}
    )
    alerts: list[Alert] = []
    state_tracks: dict[str, Any] = {}
    for track in config.tracks:
        status = _read_object(track.status_path) or {}
        health = _read_object(track.health_path) or {}
        latest_capture_timestamp = _latest_capture_timestamp(track.capture_summary_glob)
        track_alerts, next_state = evaluate_track(
            track=TrackSpec(
                name=track.name,
                root=track.status_path.parent.parent,
                expected_report_period_seconds=track.expected_cycle_seconds,
                expected_capture_period_seconds=track.expected_cycle_seconds,
                kind=track.kind,
            ),
            status=status,
            health=health,
            prior_state=previous_tracks.get(track.name),
            now_utc=_utc_text(now),
            latest_capture_timestamp=latest_capture_timestamp,
        )
        alerts.extend(track_alerts)
        previous_track = _mapping_track_state(previous_tracks.get(track.name))
        quarantine_count = (
            1 if status.get("latest_classification") == "quarantine" else 0
        )
        if quarantine_count > int(previous_track.get("last_quarantine_count", 0) or 0):
            alerts.append(
                Alert(
                    track.name,
                    "quarantine_growth",
                    "warn",
                    "quarantine count increased",
                )
            )
        active_map = {
            alert.kind: alert.severity
            for alert in track_alerts
            if alert.kind != "recovery"
        }
        state_tracks[track.name] = {
            "last_completed_report_count": next_state.last_report_count,
            "last_report_progress_at": next_state.last_report_change_at,
            "last_capture_progress_at": next_state.last_capture_change_at,
            "last_phase_healthy_at": _utc_text(now) if not active_map else None,
            "last_quarantine_count": quarantine_count,
            "active_alerts": active_map,
            "latest_capture_timestamp": latest_capture_timestamp,
        }
    return WatchResult(
        state={"checked_at": _utc_text(now), "tracks": state_tracks},
        alerts=tuple(alerts),
    )


def watch_once(
    config: Mapping[str, Any],
    previous_state: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    watch_config = WatchConfig(
        state_path=Path(str(config["state_path"])),
        tracks=tuple(
            TrackConfig(
                name=str(track["id"]),
                kind=str(track.get("kind", "paper")),
                status_path=Path(str(track["status_path"])),
                health_path=Path(str(track.get("health_path", track["status_path"]))),
                capture_summary_glob=str(track.get("capture_summary_glob", "")),
                expected_cycle_seconds=int(track.get("expected_cycle_seconds", 900)),
            )
            for track in config.get("tracks", [])
            if isinstance(track, Mapping)
        ),
        sns_topic_arn=(
            str(config["sns_topic_arn"])
            if config.get("sns_topic_arn")
            else None
        ),
    )
    result = evaluate_watch(
        watch_config,
        now=datetime.now(timezone.utc),
        previous_state=previous_state,
    )
    state = result.state
    if isinstance(config.get("candidate_scoreboard"), Mapping):
        reports_dir = Path(str(config["candidate_scoreboard"]["reports_dir"]))
        scoreboard = build_candidate_scoreboard(sorted(reports_dir.glob("*.json")))
        state["candidate_scoreboard"] = scoreboard
        output_path = config["candidate_scoreboard"].get("output_path")
        if isinstance(output_path, str) and output_path:
            Path(output_path).write_text(
                json.dumps(scoreboard, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    alerts = {
        "schema_version": "btc-paper-track-watch-alerts.v1",
        "checked_at": state["checked_at"],
        "events": [
            {
                "track": alert.track,
                "kind": alert.kind,
                "severity": alert.severity,
                "detail": alert.detail,
            }
            for alert in result.alerts
        ],
    }
    return state, alerts


def _load_watch_config(path: Path) -> WatchConfig:
    document = _read_object(path)
    if document is None:
        raise ValueError("watch config does not exist")
    tracks = tuple(
        TrackConfig(
            name=str(track["name"]),
            status_path=Path(str(track["status_path"])),
            health_path=(
                Path(str(track["health_path"]))
                if track.get("health_path")
                else None
            ),
            data_root=(
                Path(str(track["data_root"]))
                if track.get("data_root")
                else None
            ),
            kind=str(track.get("kind", "paper")),
            capture_summary_glob=str(track.get("capture_summary_glob", "")),
            expected_cycle_seconds=int(track.get("expected_cycle_seconds", 900)),
            expected_regime=(
                str(track["expected_regime"])
                if track.get("expected_regime")
                else None
            ),
            regime_state_path=(
                Path(str(track["regime_state_path"]))
                if track.get("regime_state_path")
                else None
            ),
        )
        for track in document.get("tracks", [])
    )
    host_document = document.get("host")
    host = None
    if isinstance(host_document, Mapping):
        host = HostConfig(
            status_path=(
                Path(str(host_document["status_path"]))
                if host_document.get("status_path")
                else None
            ),
            mem_available_threshold_bytes=int(
                host_document.get("mem_available_threshold_bytes", 300 * 1024 * 1024)
            ),
            cpu_credit_threshold=float(
                host_document.get("cpu_credit_threshold", 100.0)
            ),
            cpu_credit_decrease_seconds=int(
                host_document.get("cpu_credit_decrease_seconds", 1800)
            ),
            process_rss_budgets_bytes=(
                {
                    str(key): int(value)
                    for key, value in host_document.get(
                        "process_rss_budgets_bytes",
                        {},
                    ).items()
                }
                if isinstance(host_document.get("process_rss_budgets_bytes"), Mapping)
                else None
            ),
        )
    return WatchConfig(
        state_path=Path(str(document["state_path"])),
        tracks=tracks,
        sns_topic_arn=(
            str(document["sns_topic_arn"])
            if document.get("sns_topic_arn")
            else None
        ),
        alerts_path=(
            Path(str(document["alerts_path"]))
            if document.get("alerts_path")
            else None
        ),
        host=host,
        digest_interval_seconds=int(document.get("digest_interval_seconds", 3600)),
    )


def _proc_kib_value(path: Path, field: str) -> int | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    prefix = f"{field}:"
    for line in lines:
        if not line.startswith(prefix):
            continue
        parts = line[len(prefix) :].split()
        if not parts or not parts[0].isdigit():
            return None
        return int(parts[0]) * 1024
    return None


def collect_local_host_status(
    tracks: Sequence[TrackConfig],
    *,
    now: datetime,
    proc_root: Path = Path("/proc"),
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect Linux-local memory and per-track RSS without extra privileges."""

    snapshot: dict[str, Any] = {
        "schema_version": "polymm-local-host-status.v1",
        "collected_at": _utc_text(now),
        "mem_available_bytes": _proc_kib_value(
            proc_root / "meminfo",
            "MemAvailable",
        ),
        "process_rss_bytes": {},
    }
    process_rss: dict[str, int] = {}
    for track in tracks:
        status = _read_object(track.status_path) or {}
        pids = {
            value
            for field in ("pid", "child_pid")
            if isinstance((value := status.get(field)), int)
            and not isinstance(value, bool)
            and value > 0
        }
        rss_values = [
            _proc_kib_value(proc_root / str(pid) / "status", "VmRSS")
            for pid in pids
        ]
        known = [value for value in rss_values if value is not None]
        if known:
            process_rss[track.name] = sum(known)
    snapshot["process_rss_bytes"] = process_rss
    if isinstance(previous, Mapping):
        for field in ("cpu_credit_balance", "cpu_credit_observed_at"):
            if field in previous:
                snapshot[field] = previous[field]
    return snapshot


def _refresh_local_host_status(
    config: WatchConfig,
    *,
    now: datetime,
    proc_root: Path = Path("/proc"),
) -> None:
    if (
        config.host is None
        or config.host.status_path is None
        or not (proc_root / "meminfo").is_file()
    ):
        return
    previous = _read_object(config.host.status_path)
    _write_object_atomic(
        config.host.status_path,
        collect_local_host_status(
            config.tracks,
            now=now,
            proc_root=proc_root,
            previous=previous,
        ),
    )


def _auxiliary_alerts(
    config: WatchConfig,
    *,
    prior_state: Mapping[str, Any],
    result_tracks: dict[str, Any],
) -> dict[str, AlertRecord]:
    alerts: dict[str, AlertRecord] = {}
    if config.host is not None and config.host.status_path is not None:
        host = _read_object(config.host.status_path) or {}
        memory = host.get("mem_available_bytes")
        if isinstance(memory, int) and memory < config.host.mem_available_threshold_bytes:
            key = "host:memory_pressure"
            alerts[key] = AlertRecord(
                key=key,
                severity="warn",
                subject="host memory pressure",
                body={"mem_available_bytes": memory},
            )
        credits = host.get("cpu_credit_balance")
        if isinstance(credits, (int, float)) and float(credits) < config.host.cpu_credit_threshold:
            key = "host:cpu_credit_low"
            alerts[key] = AlertRecord(
                key=key,
                severity="warn",
                subject="host CPU credit low",
                body={"cpu_credit_balance": float(credits)},
            )
        rss = host.get("process_rss_bytes")
        budgets = config.host.process_rss_budgets_bytes
        if isinstance(rss, Mapping) and isinstance(budgets, Mapping):
            for name, budget in budgets.items():
                usage = rss.get(name)
                if isinstance(usage, int) and usage > budget:
                    key = f"host:rss:{name}"
                    alerts[key] = AlertRecord(
                        key=key,
                        severity="warn",
                        subject=f"{name} RSS over budget",
                        body={"process_name": name, "rss_bytes": usage},
                    )

    prior_tracks = prior_state.get("tracks", {})
    for track in config.tracks:
        if track.regime_state_path is None:
            continue
        regime = _read_object(track.regime_state_path) or {}
        observed = regime.get("observed_regime")
        consecutive = regime.get("consecutive_rejections", 0)
        if track.expected_regime and observed and observed != track.expected_regime:
            key = f"{track.name}:regime_mismatch"
            alerts[key] = AlertRecord(
                key=key,
                severity=(
                    "page"
                    if isinstance(consecutive, int) and consecutive >= 3
                    else "warn"
                ),
                subject=f"{track.name} regime mismatch",
                body={
                    "expected_regime": track.expected_regime,
                    "observed_regime": observed,
                    "first_seen_at": regime.get("first_seen_at"),
                    "last_success_at": regime.get("last_success_at"),
                    "consecutive_rejections": consecutive,
                    "affected_market_slugs": regime.get(
                        "affected_market_slugs", []
                    ),
                },
            )
        quarantine = regime.get("quarantine_count")
        prior_track = (
            prior_tracks.get(track.name, {})
            if isinstance(prior_tracks, Mapping)
            else {}
        )
        previous_count = (
            prior_track.get("last_quarantine_count", 0)
            if isinstance(prior_track, Mapping)
            else 0
        )
        if isinstance(quarantine, int) and quarantine > int(previous_count or 0):
            key = f"{track.name}:quarantine_growth"
            alerts[key] = AlertRecord(
                key=key,
                severity="warn",
                subject=f"{track.name} quarantine growth",
                body={"quarantine_count": quarantine},
            )
            result_tracks.setdefault(track.name, {})[
                "last_quarantine_count"
            ] = quarantine
    return alerts


def run_watch_cycle(
    config: WatchConfig,
    *,
    publisher: Publisher,
    now: datetime,
) -> None:
    state_path = config.state_path
    _refresh_local_host_status(config, now=now)
    state = _read_object(state_path) or {
        "schema_version": "polymm-paper-track-watch-state.v1",
        "alerts": {},
        "tracks": {},
        "host": {},
    }
    active_alerts: dict[str, AlertRecord] = {}
    result_tracks: dict[str, Any] = {}
    for track in config.tracks:
        status = _read_object(track.status_path) or {}
        heartbeat_at = _parse_utc(status.get("heartbeat_at"))
        if heartbeat_at is not None and now - heartbeat_at > timedelta(seconds=60):
            active_alerts[f"{track.name}:heartbeat_stale"] = AlertRecord(
                key=f"{track.name}:heartbeat_stale",
                severity="page",
                subject=f"{track.name} heartbeat stale",
                body={"track": track.name},
            )
        report_activity = None
        latest_summary_path = status.get("latest_summary_path")
        if isinstance(latest_summary_path, str) and latest_summary_path:
            summary_path = Path(latest_summary_path)
            if summary_path.exists():
                report_activity = datetime.fromtimestamp(
                    summary_path.stat().st_mtime,
                    tz=timezone.utc,
                )
        if track.kind != "rawcap" and (
            report_activity is None
            or now - report_activity >= timedelta(seconds=1800)
        ):
            active_alerts[f"{track.name}:report_stalled"] = AlertRecord(
                key=f"{track.name}:report_stalled",
                severity="page",
                subject=f"{track.name} reports stalled",
                body={"track": track.name},
            )
        capture_activity = None
        if track.data_root is not None:
            candidates = sorted((track.data_root / "runs").glob("*"))
            if candidates:
                latest = candidates[-1]
                capture_activity = datetime.fromtimestamp(
                    latest.stat().st_mtime,
                    tz=timezone.utc,
                )
        if capture_activity is None or now - capture_activity >= timedelta(seconds=1800):
            active_alerts[f"{track.name}:capture_stalled"] = AlertRecord(
                key=f"{track.name}:capture_stalled",
                severity="page",
                subject=f"{track.name} capture stalled",
                body={"track": track.name},
            )
        last_success = max(
            [value for value in (report_activity, capture_activity) if value is not None],
            default=None,
        )
        if status.get("phase") in {"error_wait", "stopped"} and (
            last_success is None or now - last_success >= timedelta(minutes=10)
        ):
            active_alerts[f"{track.name}:phase_unhealthy"] = AlertRecord(
                key=f"{track.name}:phase_unhealthy",
                severity="page",
                subject=f"{track.name} phase unhealthy",
                body={"track": track.name},
            )
        result_tracks[track.name] = {
            "last_completed_report_count": status.get("completed_report_count"),
            "last_report_progress_at": _utc_text(report_activity) if report_activity else None,
            "last_capture_progress_at": _utc_text(capture_activity) if capture_activity else None,
            "last_phase_healthy_at": (
                _utc_text(now)
                if f"{track.name}:phase_unhealthy" not in active_alerts
                else None
            ),
            "last_quarantine_count": 0,
        }
    active_alerts.update(
        _auxiliary_alerts(
            config,
            prior_state=state,
            result_tracks=result_tracks,
        )
    )
    prior_alerts = state.get("alerts", {})
    for key, alert in active_alerts.items():
        previous_record = (
            prior_alerts.get(key) if isinstance(prior_alerts, Mapping) else None
        )
        last_published = _parse_utc(
            previous_record.get("last_published_at")
            if isinstance(previous_record, Mapping)
            else None
        )
        if previous_record is None:
            publisher.publish(alert)
            prior_alerts[key] = {"last_published_at": _utc_text(now)}
        elif (
            last_published is not None
            and now - last_published
            >= timedelta(seconds=config.digest_interval_seconds)
        ):
            publisher.publish(
                AlertRecord(
                    key=key,
                    severity="digest",
                    subject=f"{alert.subject} digest",
                    body=alert.body,
                )
            )
            prior_alerts[key] = {"last_published_at": _utc_text(now)}
        else:
            prior_alerts[key] = dict(previous_record)
    for key in list(prior_alerts):
        if key not in active_alerts:
            publisher.publish(
                AlertRecord(
                    key=key,
                    severity="notice",
                    subject=f"{key} recovered",
                    body={"recovered_at": _utc_text(now)},
                )
            )
            del prior_alerts[key]
    state["alerts"] = prior_alerts
    state["tracks"] = result_tracks
    state["checked_at"] = _utc_text(now)
    _write_object_atomic(state_path, state)
    if config.alerts_path is not None:
        _write_object_atomic(
            config.alerts_path,
            {
                "schema_version": "polymm-paper-track-alerts.v1",
                "checked_at": _utc_text(now),
                "active_alerts": [
                    {
                        "key": alert.key,
                        "severity": alert.severity,
                        "subject": alert.subject,
                        "body": alert.body,
                    }
                    for alert in sorted(
                        active_alerts.values(),
                        key=lambda item: item.key,
                    )
                ],
            },
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stdout-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw = _read_object(args.config)
    if raw is None:
        raise SystemExit("watch config must be a JSON object")
    config = _load_watch_config(args.config)
    topic_arn = os.environ.get("POLYMM_SNS_TOPIC_ARN") or config.sns_topic_arn
    publisher: Publisher = (
        AwsCliSnsPublisher(topic_arn)
        if topic_arn and not args.stdout_only
        else JsonLinePublisher()
    )
    run_watch_cycle(config, publisher=publisher, now=datetime.now(timezone.utc))
    candidate = raw.get("candidate_scoreboard")
    if isinstance(candidate, Mapping) and candidate.get("reports_dir"):
        reports_dir = Path(str(candidate["reports_dir"]))
        scoreboard = build_candidate_scoreboard(reports_dir.glob("*.json"))
        if candidate.get("output_path"):
            _write_object_atomic(Path(str(candidate["output_path"])), scoreboard)
    print(
        json.dumps(
            {"checked_at": _utc_text(datetime.now(timezone.utc)), "status": "ok"},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
