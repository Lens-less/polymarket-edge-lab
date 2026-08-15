from __future__ import annotations

from pathlib import Path

import scripts.watch_paper_tracks as watcher


def _track(tmp_path: Path, name: str) -> watcher.TrackSpec:
    root = tmp_path / name
    return watcher.TrackSpec(
        name=name,
        root=root,
        expected_report_period_seconds=900,
        expected_capture_period_seconds=900,
        kind="paper",
    )


def test_evaluate_tracks_pages_on_phase_unhealthy_and_report_stall(
    tmp_path: Path,
) -> None:
    track = _track(tmp_path, "v05")
    status = {
        "phase": "error_wait",
        "heartbeat_at": "2026-08-14T15:30:00Z",
        "completed_report_count": 156,
    }
    health = {}
    prior = watcher.WatchState(
        tracks={
            "v05": watcher.TrackState(
                last_report_count=156,
                last_report_change_at="2026-08-14T15:00:00Z",
                last_capture_change_at="2026-08-14T15:00:00Z",
                active_alerts=(),
            )
        }
    )

    events, _next = watcher.evaluate_track(
        track=track,
        status=status,
        health=health,
        prior_state=prior.tracks["v05"],
        now_utc="2026-08-14T15:31:00Z",
        latest_capture_timestamp="2026-08-14T15:00:00Z",
    )

    keys = {(event.kind, event.severity) for event in events}
    assert ("phase_unhealthy", "page") in keys
    assert ("report_stalled", "page") in keys
    assert ("capture_stalled", "page") in keys


def test_evaluate_tracks_emits_recovery_notice_after_page_condition_clears(
    tmp_path: Path,
) -> None:
    track = _track(tmp_path, "v06")
    prior = watcher.TrackState(
        last_report_count=156,
        last_report_change_at="2026-08-14T15:00:00Z",
        last_capture_change_at="2026-08-14T15:00:00Z",
        active_alerts=("phase_unhealthy", "report_stalled"),
    )

    events, _next = watcher.evaluate_track(
        track=track,
        status={
            "phase": "capturing",
            "heartbeat_at": "2026-08-14T15:31:00Z",
            "completed_report_count": 157,
        },
        health={},
        prior_state=prior,
        now_utc="2026-08-14T15:31:00Z",
        latest_capture_timestamp="2026-08-14T15:31:00Z",
    )

    assert len(events) == 1
    assert events[0].kind == "recovery"
    assert events[0].severity == "notice"
