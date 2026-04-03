import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# This file tracks which videos have already completed the downstream rebuild
# for a given run tag. The run tag lets you distinguish different refresh waves,
# for example:
# - medium-v1-downstream
# - medium-v2-downstream
STATE_FILE_DEFAULT = "downstream_runs.json"

# We use playlist.json to get the full ordered set of video IDs when the user
# does not pass explicit video IDs.
PLAYLIST_FILE_DEFAULT = "playlist.json"

# These are the minimum artifacts we expect after running:
# diarize -> merge -> patch -> scrape -> analyze
#
# If your repo later has a single definitive analyze output you want to require,
# you can add it here.
REQUIRED_ARTIFACTS = [
    "diarization.rttm",
    "segments.csv",
    "segments_patched.csv",
    "intro_roster.json",
]


def load_playlist_ids(playlist_path: Path) -> list[str]:
    """
    Load video IDs from playlist.json.

    Supports either:
    - a plain list of entries
    - an object with {"entries": [...]}

    Returns only valid IDs.
    """
    raw = json.loads(playlist_path.read_text(encoding="utf-8"))
    entries = raw if isinstance(raw, list) else raw.get("entries", [])
    return [entry["id"] for entry in entries if entry.get("id")]


def load_state(state_path: Path) -> dict[str, Any]:
    """
    Load the downstream batch state.

    Shape:
    {
      "runs": {
        "medium-v1-downstream": {
          "completed": [...],
          "failed": {...},
          "last_run_started_at": "...",
          "last_run_finished_at": "..."
        }
      }
    }
    """
    if not state_path.exists():
        return {"runs": {}}
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    """
    Persist state after every video so progress is not lost if a run crashes.
    """
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def ensure_run_state(state: dict[str, Any], run_tag: str) -> dict[str, Any]:
    """
    Ensure the run-tag bucket exists and contains all required keys.
    This also makes the script resilient to older state-file shapes.
    """
    runs = state.setdefault("runs", {})
    run_state = runs.setdefault(run_tag, {})
    run_state.setdefault("completed", [])
    run_state.setdefault("failed", {})
    run_state.setdefault("last_run_started_at", None)
    run_state.setdefault("last_run_finished_at", None)
    return run_state


def get_pending_ids(all_ids: list[str], state: dict[str, Any], run_tag: str) -> list[str]:
    """
    Return videos not yet marked complete for this run tag.
    """
    done = set(state.get("runs", {}).get(run_tag, {}).get("completed", []))
    return [video_id for video_id in all_ids if video_id not in done]


def mark_completed(
    state: dict[str, Any],
    run_tag: str,
    video_id: str,
    returncode: int,
    notes: dict[str, Any] | None = None,
) -> None:
    """
    Mark a video as complete for this downstream refresh wave.
    Removes any older failure record for that same video.
    """
    run_state = ensure_run_state(state, run_tag)

    if video_id not in run_state["completed"]:
        run_state["completed"].append(video_id)

    run_state["failed"].pop(video_id, None)

    if notes:
        run_state.setdefault("notes", {})[video_id] = {
            "returncode": returncode,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "details": notes,
        }


def mark_failed(
    state: dict[str, Any],
    run_tag: str,
    video_id: str,
    returncode: int,
    reason: str,
) -> None:
    """
    Record a true failure. This leaves the video pending for future reruns.
    """
    run_state = ensure_run_state(state, run_tag)
    run_state["failed"][video_id] = {
        "returncode": returncode,
        "reason": reason,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


def run_downstream(video_id: str, repo_root: Path) -> int:
    """
    Run all transcript-dependent downstream steps for one video in a fresh subprocess.

    Fresh subprocess per video is simpler and safer than trying to keep a long-lived
    Python process healthy across many heavy pipeline steps.
    """
    cmd = [
        sys.executable,
        "run_pipeline.py",
        video_id,
        "--steps",
        "diarize",
        "merge",
        "patch",
        "scrape",
        "analyze",
        "--force",
    ]
    result = subprocess.run(cmd, cwd=repo_root)
    return result.returncode


def validate_artifacts(video_id: str, repo_root: Path) -> tuple[bool, str, dict[str, Any]]:
    """
    Validate the minimum expected downstream outputs for one video.

    This is intentionally artifact-driven. We do not want to mark a video done
    just because the subprocess returned 0 if the real outputs are missing.
    """
    out_dir = repo_root / "outputs" / video_id
    details: dict[str, Any] = {
        "output_dir": str(out_dir),
        "required_artifacts": REQUIRED_ARTIFACTS,
        "present": [],
        "missing": [],
    }

    if not out_dir.exists():
        return False, "output directory missing", details

    for name in REQUIRED_ARTIFACTS:
        path = out_dir / name
        if path.exists() and path.stat().st_size > 0:
            details["present"].append(name)
        else:
            details["missing"].append(name)

    if details["missing"]:
        return False, f"missing required artifacts: {', '.join(details['missing'])}", details

    return True, "required downstream artifacts present", details


def main() -> None:
    """
    Main batch runner for transcript-dependent downstream steps.

    Typical usage:
        python batch_downstream.py --run-tag medium-v1-downstream --batch-size 5

    You can also pass explicit video IDs if you only want to refresh a subset:
        python batch_downstream.py --run-tag medium-v1-downstream --video-ids abc123 def456
    """
    parser = argparse.ArgumentParser(
        description="Run downstream pipeline steps in tracked batches."
    )
    parser.add_argument(
        "--run-tag",
        required=True,
        help="Label for this downstream refresh, e.g. medium-v1-downstream",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="How many videos to process in this batch",
    )
    parser.add_argument(
        "--state-file",
        default=STATE_FILE_DEFAULT,
        help="JSON file tracking completed downstream runs",
    )
    parser.add_argument(
        "--playlist-file",
        default=PLAYLIST_FILE_DEFAULT,
        help="Path to playlist.json",
    )
    parser.add_argument(
        "--video-ids",
        nargs="*",
        help="Optional explicit video IDs instead of playlist order",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    state_path = repo_root / args.state_file
    playlist_path = repo_root / args.playlist_file

    state = load_state(state_path)
    run_state = ensure_run_state(state, args.run_tag)
    run_state["last_run_started_at"] = datetime.now().isoformat(timespec="seconds")
    save_state(state_path, state)

    all_ids = args.video_ids if args.video_ids else load_playlist_ids(playlist_path)
    pending_ids = get_pending_ids(all_ids, state, args.run_tag)
    batch_ids = pending_ids[: args.batch_size]

    if not batch_ids:
        print(f"No pending videos for run tag: {args.run_tag}")
        return

    print(f"Run tag: {args.run_tag}")
    print(f"Running batch of {len(batch_ids)} video(s):")
    for video_id in batch_ids:
        print(f"  - {video_id}")

    successes: list[str] = []
    failures: list[tuple[str, int, str]] = []

    for index, video_id in enumerate(batch_ids, start=1):
        print(f"\n[{index}/{len(batch_ids)}] Processing downstream steps for {video_id} ...")
        returncode = run_downstream(video_id, repo_root)

        artifact_ok, artifact_reason, artifact_details = validate_artifacts(video_id, repo_root)

        if returncode == 0 and artifact_ok:
            print(f"  OK: {video_id}")
            print(f"     validation: {artifact_reason}")
            print(f"     details: {artifact_details}")
            mark_completed(state, args.run_tag, video_id, returncode, artifact_details)
            successes.append(video_id)
        else:
            print(f"  FAIL: {video_id} (exit {returncode})")
            print(f"     validation: {artifact_reason}")
            print(f"     details: {artifact_details}")
            mark_failed(state, args.run_tag, video_id, returncode, artifact_reason)
            failures.append((video_id, returncode, artifact_reason))

        # Save after every video.
        save_state(state_path, state)

    run_state["last_run_finished_at"] = datetime.now().isoformat(timespec="seconds")
    save_state(state_path, state)

    remaining = len(get_pending_ids(all_ids, state, args.run_tag))

    print("\nBatch complete.")
    print(f"  Succeeded: {len(successes)}")
    print(f"  Failed:    {len(failures)}")
    print(f"  Remaining for {args.run_tag}: {remaining}")

    if failures:
        print("\nFailures:")
        for video_id, returncode, reason in failures:
            print(f"  - {video_id}: exit {returncode} | {reason}")


if __name__ == "__main__":
    main()