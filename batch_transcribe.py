import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

STATE_FILE_DEFAULT = "transcription_runs.json"
PLAYLIST_FILE_DEFAULT = "playlist.json"

# Windows native crash often seen after successful GPU transcription teardown.
SOFT_SUCCESS_EXIT_CODES = {3221226505}

# Safety thresholds for considering a transcript "complete enough".
MIN_SEGMENTS_DEFAULT = 100
MIN_COVERAGE_RATIO_DEFAULT = 0.95


def load_playlist_ids(playlist_path: Path) -> list[str]:
    raw = json.loads(playlist_path.read_text(encoding="utf-8"))
    entries = raw if isinstance(raw, list) else raw.get("entries", [])
    return [entry["id"] for entry in entries if entry.get("id")]


def load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"models": {}}
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def ensure_model_state(state: dict[str, Any], model_tag: str) -> dict[str, Any]:
    models = state.setdefault("models", {})
    model_state = models.setdefault(model_tag, {})

    model_state.setdefault("completed", [])
    model_state.setdefault("failed", {})
    model_state.setdefault("completed_with_soft_crash", {})
    model_state.setdefault("last_run_started_at", None)
    model_state.setdefault("last_run_finished_at", None)

    return model_state


def get_pending_ids(all_ids: list[str], state: dict[str, Any], model_tag: str) -> list[str]:
    done = set(state.get("models", {}).get(model_tag, {}).get("completed", []))
    return [video_id for video_id in all_ids if video_id not in done]


def mark_completed(
    state: dict[str, Any],
    model_tag: str,
    video_id: str,
    returncode: int,
    soft_crash: bool = False,
    notes: dict[str, Any] | None = None,
) -> None:
    model_state = ensure_model_state(state, model_tag)

    if video_id not in model_state["completed"]:
        model_state["completed"].append(video_id)

    model_state["failed"].pop(video_id, None)

    if soft_crash:
        model_state["completed_with_soft_crash"][video_id] = {
            "returncode": returncode,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "notes": notes or {},
        }
    else:
        model_state["completed_with_soft_crash"].pop(video_id, None)


def mark_failed(
    state: dict[str, Any],
    model_tag: str,
    video_id: str,
    returncode: int,
    reason: str,
) -> None:
    model_state = ensure_model_state(state, model_tag)
    model_state["failed"][video_id] = {
        "returncode": returncode,
        "reason": reason,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


def run_transcribe(video_id: str, repo_root: Path) -> int:
    cmd = [
    	sys.executable,
    	"run_pipeline.py",
    	"--steps",
    	"transcribe",
    	"--force",
    	"--",
    	video_id,
    ]
    result = subprocess.run(cmd, cwd=repo_root)
    return result.returncode


def candidate_output_paths(video_id: str, repo_root: Path) -> list[Path]:
    out_dir = repo_root / "outputs" / video_id
    return [
        out_dir / "whisper_segments.json",
        out_dir / "whisper_segments.jsonl",
    ]


def find_existing_output_path(video_id: str, repo_root: Path) -> Path | None:
    for path in candidate_output_paths(video_id, repo_root):
        if path.exists():
            return path
    return None


def load_audio_duration_seconds(video_id: str, repo_root: Path) -> float | None:
    out_dir = repo_root / "outputs" / video_id

    # Prefer metadata from playlist.json if present, because it's simpler and
    # avoids audio probing dependencies.
    playlist_path = repo_root / PLAYLIST_FILE_DEFAULT
    if playlist_path.exists():
        try:
            raw = json.loads(playlist_path.read_text(encoding="utf-8"))
            entries = raw if isinstance(raw, list) else raw.get("entries", [])
            for entry in entries:
                if entry.get("id") == video_id:
                    duration = entry.get("duration")
                    if isinstance(duration, (int, float)) and duration > 0:
                        return float(duration)
        except Exception:
            pass

    # No duration available.
    return None


def normalize_segment_row(row: dict[str, Any]) -> tuple[float | None, float | None]:
    """
    Try to extract start/end timestamps from either JSON or JSONL records.

    Common possibilities:
    - {"start": 1.23, "end": 4.56, ...}
    - {"start_s": 1.23, "end_s": 4.56, ...}
    """
    start = row.get("start", row.get("start_s"))
    end = row.get("end", row.get("end_s"))

    try:
        start_f = float(start) if start is not None else None
    except (TypeError, ValueError):
        start_f = None

    try:
        end_f = float(end) if end is not None else None
    except (TypeError, ValueError):
        end_f = None

    return start_f, end_f


def validate_json_output(
    path: Path,
    min_segments: int,
    min_coverage_ratio: float,
    audio_duration_s: float | None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Validate a JSON transcript file.

    Expected shape is either:
    - list[dict]
    - dict with a "segments" key containing list[dict]
    """
    details: dict[str, Any] = {"path": str(path)}

    if not path.exists():
        return False, "missing output file", details

    if path.stat().st_size == 0:
        return False, "empty output file", details

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"invalid json: {exc}", details

    if isinstance(data, dict):
        segments = data.get("segments", [])
    elif isinstance(data, list):
        segments = data
    else:
        return False, f"unexpected json top-level type: {type(data).__name__}", details

    if not isinstance(segments, list):
        return False, "segments field is not a list", details

    segment_count = len(segments)
    details["segment_count"] = segment_count

    if segment_count < min_segments:
        return False, f"too few segments: {segment_count} < {min_segments}", details

    last_end = None
    parsed_rows = 0

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        _, end = normalize_segment_row(seg)
        if end is not None:
            parsed_rows += 1
            last_end = end if last_end is None else max(last_end, end)

    details["timestamped_segments"] = parsed_rows
    details["last_end_s"] = last_end

    if parsed_rows == 0:
        return False, "no usable timestamps found in transcript output", details

    if audio_duration_s and audio_duration_s > 0:
        coverage_ratio = (last_end / audio_duration_s) if last_end is not None else 0.0
        details["audio_duration_s"] = audio_duration_s
        details["coverage_ratio"] = coverage_ratio

        if coverage_ratio < min_coverage_ratio:
            return (
                False,
                f"insufficient coverage: {coverage_ratio:.3f} < {min_coverage_ratio:.3f}",
                details,
            )

    return True, "valid transcript artifact", details


def validate_jsonl_output(
    path: Path,
    min_segments: int,
    min_coverage_ratio: float,
    audio_duration_s: float | None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Validate a JSONL transcript file.
    """
    details: dict[str, Any] = {"path": str(path)}

    if not path.exists():
        return False, "missing output file", details

    if path.stat().st_size == 0:
        return False, "empty output file", details

    segment_count = 0
    parsed_rows = 0
    last_end = None

    try:
        with path.open("r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                segment_count += 1

                if isinstance(row, dict):
                    _, end = normalize_segment_row(row)
                    if end is not None:
                        parsed_rows += 1
                        last_end = end if last_end is None else max(last_end, end)
    except Exception as exc:
        return False, f"invalid jsonl: {exc}", details

    details["segment_count"] = segment_count
    details["timestamped_segments"] = parsed_rows
    details["last_end_s"] = last_end

    if segment_count < min_segments:
        return False, f"too few segments: {segment_count} < {min_segments}", details

    if parsed_rows == 0:
        return False, "no usable timestamps found in transcript output", details

    if audio_duration_s and audio_duration_s > 0:
        coverage_ratio = (last_end / audio_duration_s) if last_end is not None else 0.0
        details["audio_duration_s"] = audio_duration_s
        details["coverage_ratio"] = coverage_ratio

        if coverage_ratio < min_coverage_ratio:
            return (
                False,
                f"insufficient coverage: {coverage_ratio:.3f} < {min_coverage_ratio:.3f}",
                details,
            )

    return True, "valid transcript artifact", details


def validate_transcription_output(
    video_id: str,
    repo_root: Path,
    min_segments: int,
    min_coverage_ratio: float,
) -> tuple[bool, str, dict[str, Any]]:
    path = find_existing_output_path(video_id, repo_root)
    if path is None:
        checked = ", ".join(str(p) for p in candidate_output_paths(video_id, repo_root))
        return False, f"no transcription artifact found ({checked})", {}

    audio_duration_s = load_audio_duration_seconds(video_id, repo_root)

    if path.suffix == ".json":
        return validate_json_output(path, min_segments, min_coverage_ratio, audio_duration_s)
    if path.suffix == ".jsonl":
        return validate_jsonl_output(path, min_segments, min_coverage_ratio, audio_duration_s)

    return False, f"unsupported artifact type: {path.suffix}", {"path": str(path)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run transcription in small tracked batches with artifact validation."
    )
    parser.add_argument(
        "--model-tag",
        required=True,
        help="Label for this transcription setup, e.g. medium-v1",
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
        help="JSON file tracking completed videos",
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
    parser.add_argument(
        "--min-segments",
        type=int,
        default=MIN_SEGMENTS_DEFAULT,
        help="Minimum segment count required to mark a transcript complete",
    )
    parser.add_argument(
        "--min-coverage-ratio",
        type=float,
        default=MIN_COVERAGE_RATIO_DEFAULT,
        help="Minimum ratio of transcript end time to audio duration",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    state_path = repo_root / args.state_file
    playlist_path = repo_root / args.playlist_file

    state = load_state(state_path)
    model_state = ensure_model_state(state, args.model_tag)
    model_state["last_run_started_at"] = datetime.now().isoformat(timespec="seconds")
    save_state(state_path, state)

    all_ids = args.video_ids if args.video_ids else load_playlist_ids(playlist_path)
    pending_ids = get_pending_ids(all_ids, state, args.model_tag)
    batch_ids = pending_ids[: args.batch_size]

    if not batch_ids:
        print(f"No pending videos for model tag: {args.model_tag}")
        return

    print(f"Model tag: {args.model_tag}")
    print(f"Running batch of {len(batch_ids)} video(s):")
    for video_id in batch_ids:
        print(f"  - {video_id}")

    clean_successes: list[str] = []
    soft_successes: list[tuple[str, int]] = []
    failures: list[tuple[str, int, str]] = []

    for index, video_id in enumerate(batch_ids, start=1):
        print(f"\n[{index}/{len(batch_ids)}] Transcribing {video_id} ...")
        returncode = run_transcribe(video_id, repo_root)

        artifact_ok, artifact_reason, artifact_details = validate_transcription_output(
            video_id=video_id,
            repo_root=repo_root,
            min_segments=args.min_segments,
            min_coverage_ratio=args.min_coverage_ratio,
        )

        if returncode == 0 and artifact_ok:
            print(f"  OK: {video_id}")
            print(f"     validation: {artifact_reason}")
            print(f"     details: {artifact_details}")
            mark_completed(
                state,
                args.model_tag,
                video_id,
                returncode,
                soft_crash=False,
                notes=artifact_details,
            )
            clean_successes.append(video_id)

        elif returncode in SOFT_SUCCESS_EXIT_CODES and artifact_ok:
            print(f"  SOFT OK: {video_id} (exit {returncode})")
            print(f"     validation: {artifact_reason}")
            print(f"     details: {artifact_details}")
            mark_completed(
                state,
                args.model_tag,
                video_id,
                returncode,
                soft_crash=True,
                notes=artifact_details,
            )
            soft_successes.append((video_id, returncode))

        else:
            print(f"  FAIL: {video_id} (exit {returncode})")
            print(f"     validation: {artifact_reason}")
            print(f"     details: {artifact_details}")
            mark_failed(state, args.model_tag, video_id, returncode, artifact_reason)
            failures.append((video_id, returncode, artifact_reason))

        save_state(state_path, state)

    model_state["last_run_finished_at"] = datetime.now().isoformat(timespec="seconds")
    save_state(state_path, state)

    remaining = len(get_pending_ids(all_ids, state, args.model_tag))

    print("\nBatch complete.")
    print(f"  Succeeded cleanly:         {len(clean_successes)}")
    print(f"  Succeeded with soft crash: {len(soft_successes)}")
    print(f"  Failed:                    {len(failures)}")
    print(f"  Remaining for {args.model_tag}: {remaining}")

    if soft_successes:
        print("\nSoft successes:")
        for video_id, returncode in soft_successes:
            print(f"  - {video_id}: exit {returncode} but artifact validated")

    if failures:
        print("\nFailures:")
        for video_id, returncode, reason in failures:
            print(f"  - {video_id}: exit {returncode} | {reason}")


if __name__ == "__main__":
    main()