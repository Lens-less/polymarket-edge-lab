from src.profit_system.strategies.scenarios import (
    run_acceptance_walkthroughs,
    run_track_a_walkthrough,
    run_track_b_walkthrough,
)


def test_track_a_walkthrough_runs_all_stages_and_finishes_go() -> None:
    report = run_track_a_walkthrough()

    assert report["final_status"] == "GO"
    assert report["research"]["status"] == "GO"
    assert report["replay"]["status"] == "EXECUTABLE"
    assert report["paper"]["status"] == "EXECUTABLE"
    assert report["shadow"]["status"] == "EXECUTABLE"
    assert report["shadow_gate"]["status"] == "GO"


def test_track_b_walkthrough_runs_all_stages_and_can_finish_no_go() -> None:
    report = run_track_b_walkthrough()

    assert report["final_status"] == "NO_GO"
    assert report["research"]["status"] == "GO"
    assert report["replay"]["status"] == "EXECUTABLE"
    assert report["paper"]["status"] == "EXECUTABLE"
    assert report["shadow"]["status"] == "EXECUTABLE"
    assert report["shadow_gate"]["status"] == "NO_GO"


def test_acceptance_bundle_contains_both_tracks() -> None:
    report = run_acceptance_walkthroughs()

    assert report["report_version"] == "profit-system-v0.2.acceptance.v2"
    assert [track["track"] for track in report["tracks"]] == ["A", "B"]
    assert report["tracks"][0]["replay_execution"]["confirm_status"] == "CONFIRMED"
    assert report["tracks"][0]["paper_execution"]["confirm_status"] == "CONFIRMED"
    assert report["tracks"][0]["shadow_execution"]["confirm_status"] == "BLOCKED"
