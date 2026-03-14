"""run_pipeline.py - Orchestrate BotC pipeline steps for one or all videos.

Usage:
    # Process a single video (all steps)
    python run_pipeline.py <video_id>

    # Process specific steps only
    python run_pipeline.py <video_id> --steps download transcribe

    # Force re-run even if output already exists
    python run_pipeline.py <video_id> --force

    # Process all pending, non-members-only videos in playlist.json
    python run_pipeline.py --all [--steps ...] [--force]

    # Authenticate for member-only videos (pick one):
    python run_pipeline.py <video_id> --browser chrome
    python run_pipeline.py <video_id> --browser firefox
    # Or place a cookies.txt (Netscape format) in the project root — picked up automatically.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# If cookies.txt exists in the project root it is used automatically for all yt-dlp calls.
_COOKIES_FILE = Path("cookies.txt")

PLAYLIST_JSON = Path("playlist.json")

ALL_STEPS = ["download", "transcribe", "diarize", "merge", "patch", "scrape", "analyze"]

# Optional enrichment steps — not run by default, not part of playlist.json status machine.
# N0 scan: visual frame scan (requires video.mp4 from download step)
# N1 consistency: speaker consistency correction
# N2 phases: game-phase boundary detection
# N3 claims: role-claim / accusation extraction
ENRICH_STEPS = ["scan", "consistency", "phases", "claims"]

# Expected output file for each step (relative to outputs/<video_id>/)
_STEP_OUTPUT = {
    "download":    "audio.wav",
    "transcribe":  "whisper_segments.jsonl",
    "diarize":     "diarization.rttm",
    "merge":       "segments.csv",
    "patch":       "segments_patched.csv",
    "scrape":      "intro_roster.json",
    "analyze":     "lie_analysis.csv",
    # enrichment steps
    "scan":        "frame_scan.json",
    "consistency": "segments_consistent.csv",
    "phases":      "phase_labels.csv",
    "claims":      "claims.csv",
}

# Phrases in yt-dlp stderr that indicate a members-only video
_MEMBERS_ONLY_PHRASES = [
    "Only images are available",
    "members-only",
    "members only",
    "Join this channel",
]


class MembersOnlyError(Exception):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cookie_args(browser: str | None) -> list[str]:
    """Return yt-dlp cookie flags, preferring --browser over cookies.txt."""
    if browser:
        return ["--cookies-from-browser", browser]
    if _COOKIES_FILE.exists():
        return ["--cookies", str(_COOKIES_FILE)]
    return []


def _get_video_url(video_id: str) -> str:
    """Look up URL from playlist.json, or construct a default YouTube URL."""
    if PLAYLIST_JSON.exists():
        data = json.loads(PLAYLIST_JSON.read_text(encoding="utf-8"))
        for e in data.get("entries", []):
            if e["id"] == video_id:
                return e.get("url") or f"https://www.youtube.com/watch?v={video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def _mark_members_only(video_id: str) -> None:
    """Set members_only=true for this video in playlist.json."""
    if not PLAYLIST_JSON.exists():
        return
    data = json.loads(PLAYLIST_JSON.read_text(encoding="utf-8"))
    for e in data.get("entries", []):
        if e["id"] == video_id:
            e["members_only"] = True
            break
    PLAYLIST_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _refresh_playlist_status(video_id: str) -> None:
    """Update the status field for video_id in playlist.json if it exists."""
    if not PLAYLIST_JSON.exists():
        return
    from fetch_playlist import compute_status
    data = json.loads(PLAYLIST_JSON.read_text(encoding="utf-8"))
    for e in data.get("entries", []):
        if e["id"] == video_id:
            e["status"] = compute_status(video_id)
            break
    PLAYLIST_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _output_exists(video_id: str, step: str) -> bool:
    return (Path(f"outputs/{video_id}") / _STEP_OUTPUT[step]).exists()


def _ytdlp(args: list[str]) -> None:
    """Run yt-dlp, print its output live, and raise MembersOnlyError if applicable."""
    # Use sys.executable -m yt_dlp so we always run the venv's yt-dlp,
    # not whatever system version happens to be on PATH.
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp", *args],
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.stderr:
        print(result.stderr, end="")
    if result.returncode != 0:
        if any(phrase in result.stderr for phrase in _MEMBERS_ONLY_PHRASES):
            raise MembersOnlyError("members-only content detected")
        raise subprocess.CalledProcessError(result.returncode, ["yt-dlp", *args])


# ---------------------------------------------------------------------------
# Step runners
# ---------------------------------------------------------------------------

def _convert_audio_pyav(src: Path, dst: Path, sample_rate: int, stereo: bool) -> None:
    """Convert audio using PyAV + soundfile — no system ffmpeg required."""
    import numpy as np
    from faster_whisper.audio import decode_audio
    import soundfile as sf
    if stereo:
        left, right = decode_audio(str(src), sampling_rate=sample_rate, split_stereo=True)
        audio = np.stack([left, right], axis=1)
    else:
        audio = decode_audio(str(src), sampling_rate=sample_rate)
    sf.write(str(dst), audio, sample_rate, subtype="PCM_16")
    print(f"  [pyav] wrote {dst.name}  ({len(audio) / sample_rate:.0f}s)")


def _run_download(video_id: str, browser: str | None) -> None:
    out_dir = Path(f"outputs/{video_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    url = _get_video_url(video_id)
    cookies = _cookie_args(browser)
    # Prefer full path to node so yt-dlp can solve the n-challenge even when
    # node.exe is not on the system PATH (common on Windows).
    _node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    ejs = ["--js-runtimes", f"node:{_node}"] if Path(_node).exists() else []

    audio_wav = out_dir / "audio.wav"

    # If a raw audio file (e.g. .webm) already exists from a previous partial
    # run, skip re-downloading and go straight to the ffmpeg conversion.
    existing_raw = next(
        (p for p in out_dir.glob("audio.*") if p.suffix != ".wav"), None
    )
    if existing_raw:
        print(f"  Found existing {existing_raw.name} — skipping yt-dlp audio download")
    else:
        print(f"  Downloading audio: {url}")
        _ytdlp(["-x", "--audio-format", "wav",
                "-o", str(out_dir / "audio.%(ext)s"), *cookies, *ejs, url])
        existing_raw = next(
            (p for p in out_dir.glob("audio.*") if p.suffix != ".wav"), None
        )

    if not audio_wav.exists():
        src = existing_raw or next(out_dir.glob("audio.*"))
        print(f"  Converting {src.name} -> audio.wav ...")
        if shutil.which("ffmpeg"):
            subprocess.run(
                ["ffmpeg", "-i", str(src), "-ar", "44100", "-ac", "2",
                 str(audio_wav), "-y"],
                check=True,
            )
        else:
            _convert_audio_pyav(src, audio_wav, sample_rate=44100, stereo=True)

    audio_16k = out_dir / "audio_16k.wav"
    print("  Resampling to 16 kHz mono ...")
    if shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-i", str(audio_wav),
             "-ar", "16000", "-ac", "1", str(audio_16k), "-y"],
            check=True,
        )
    else:
        _convert_audio_pyav(
            existing_raw or next(out_dir.glob("audio.*")),
            audio_16k, sample_rate=16000, stereo=False,
        )

    print(f"  Downloading video: {url}")
    _ytdlp(["-o", str(out_dir / "video.mp4"), *cookies, *ejs, url])


def _run_step(step: str, video_id: str, force: bool, browser: str | None = None) -> None:
    if _output_exists(video_id, step) and not force:
        print(f"  [SKIP] {step}: output already exists")
        return

    print(f"  [RUN]  {step} ...")

    if step == "download":
        _run_download(video_id, browser)

    elif step == "transcribe":
        import transcribe
        transcribe.main(video_id)

    elif step == "diarize":
        import diarize_nemo
        diarize_nemo.main(video_id)

    elif step == "merge":
        import merge_segments
        merge_segments.main(video_id)

    elif step == "patch":
        import patch_transcript
        patch_transcript.main(video_id)

    elif step == "scrape":
        import scrape_intro
        scrape_intro.main(video_id, force=force)

    elif step == "analyze":
        import analyze_roles
        analyze_roles.main(video_id)

    # ── Optional enrichment steps (N0 scan / N1 consistency / N2 phases / N3 claims) ─────────────────
    elif step == "scan":
        import scan_frames
        scan_frames.scan(video_id, force=force)

    elif step == "consistency":
        import speaker_consistency
        speaker_consistency.main(video_id, force=force)

    elif step == "phases":
        import detect_phases
        detect_phases.main(video_id, force=force)

    elif step == "claims":
        import extract_claims
        extract_claims.main(video_id, force=force)

    # Enrichment steps do NOT update playlist.json status (they are optional
    # post-processing and not part of the status state machine).
    if step not in ENRICH_STEPS:
        _refresh_playlist_status(video_id)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def process_video(video_id: str, steps: list[str], force: bool, browser: str | None = None) -> None:
    print(f"\n{'=' * 60}")
    print(f"Processing: {video_id}")
    print(f"{'=' * 60}")
    try:
        for step in steps:
            _run_step(step, video_id, force, browser)
        print(f"Done: {video_id}")
    except MembersOnlyError:
        print(f"  [SKIP] {video_id} is members-only — marking in playlist.json")
        _mark_members_only(video_id)


def main() -> None:
    ap = argparse.ArgumentParser(description="Run BotC pipeline for one or all videos")
    ap.add_argument(
        "video_id", nargs="?",
        help="YouTube video ID (omit when using --all)",
    )
    ap.add_argument(
        "--steps", nargs="+", choices=ALL_STEPS + ENRICH_STEPS, default=ALL_STEPS,
        metavar="STEP",
        help=f"Steps to run (default: all). Choices: {ALL_STEPS}",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="Re-run steps even if output already exists",
    )
    ap.add_argument(
        "--all", action="store_true", dest="all_videos",
        help="Process all pending, non-members-only videos in playlist.json",
    )
    ap.add_argument(
        "--browser", metavar="BROWSER",
        help="Pass cookies from this browser to yt-dlp for member-only videos "
             "(e.g. chrome, firefox, edge). Alternatively, place a cookies.txt "
             "file in the project root to be picked up automatically.",
    )
    args = ap.parse_args()

    if args.all_videos:
        if not PLAYLIST_JSON.exists():
            sys.exit("playlist.json not found — run fetch_playlist.py first")
        data = json.loads(PLAYLIST_JSON.read_text(encoding="utf-8"))

        # If every requested step is an enrichment step, include already-analyzed
        # videos — they still need post-processing (scan, consistency, phases, claims).
        enrich_only = all(s in ENRICH_STEPS for s in args.steps)

        pending = [
            e for e in data.get("entries", [])
            if (enrich_only or e.get("status") != "analyzed")
            and not e.get("members_only")
            and not e.get("skip")
        ]
        if not pending:
            print("Nothing to process. (All videos are either analyzed, members-only, or skipped.)")
            return
        skipped_count = sum(1 for e in data.get("entries", []) if e.get("members_only") or e.get("skip"))
        if skipped_count:
            print(f"Skipping {skipped_count} members-only/skip video(s).")
        print(f"Processing {len(pending)} video(s) with steps: {args.steps}")
        for e in pending:
            process_video(e["id"], args.steps, args.force, args.browser)

    elif args.video_id:
        process_video(args.video_id, args.steps, args.force, args.browser)

    else:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
