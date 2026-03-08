"""
retranscribe_all.py
-------------------
Re-runs transcribe -> merge -> patch -> analyze for all videos that have
audio.wav but were originally transcribed with the small Whisper model.

Skips:
  - Videos already re-done with medium model (tf_LO5NKKUU, HQlYPDUfM4Q, F2f0nNeoWQM)
  - Videos with no audio downloaded yet (DbF9CPOueTI, z79AJOPoNi4)
  - skip=True videos (OYTaTtjk3ac)

Members-only videos with audio (DAb9sq5ku2k, d2M-N5iABRo) ARE included
since we are only re-running transcribe/merge/patch/analyze — no download,
no scrape, no diarize. Manual rosters / roster_overrides are preserved.

Usage:
    python retranscribe_all.py
    python retranscribe_all.py --dry-run    # list videos without running
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ALREADY_MEDIUM = {"tf_LO5NKKUU", "HQlYPDUfM4Q", "F2f0nNeoWQM"}
STEPS = ["transcribe", "merge", "patch", "analyze"]
OUTPUTS_DIR = Path("outputs")
PLAYLIST = Path("playlist.json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="List videos that would be processed without running them")
    args = ap.parse_args()

    if not PLAYLIST.exists():
        sys.exit("playlist.json not found")

    data = json.loads(PLAYLIST.read_text(encoding="utf-8"))
    entries = data.get("entries", [])

    to_process = []
    skipped = []

    for e in entries:
        vid = e["id"]
        reason = None

        if e.get("skip"):
            reason = "skip=true in playlist"
        elif vid in ALREADY_MEDIUM:
            reason = "already medium model"
        elif not (OUTPUTS_DIR / vid / "audio.wav").exists():
            reason = "no audio.wav"

        if reason:
            skipped.append((vid, reason))
        else:
            to_process.append((vid, e.get("members_only", False), e.get("blind", False)))

    print(f"Videos to re-transcribe : {len(to_process)}")
    print(f"Videos skipped          : {len(skipped)}")
    print()

    for vid, members, blind in to_process:
        flags = []
        if members:
            flags.append("members")
        if blind:
            flags.append("blind")
        tag = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  QUEUE  {vid}{tag}")

    if skipped:
        print()
        for vid, reason in skipped:
            print(f"  SKIP   {vid}  ({reason})")

    if args.dry_run:
        print("\n--dry-run: exiting without processing.")
        return

    print()

    # Each video is run as a subprocess so that the WhisperModel CUDA allocator
    # gets a clean VRAM state per video.  In-process calls share one process
    # address space: after the first transcription the GPU memory from the model
    # is not guaranteed to be freed before the next WhisperModel.__init__ fires,
    # which causes a C-level OOM that kills the whole process — not a Python
    # exception, so try/except can't catch it.  A subprocess exits cleanly and
    # the OS reclaims all VRAM before the next video starts.
    cmd_base = [sys.executable, "run_pipeline.py", "--steps"] + STEPS + ["--force"]

    failed = []
    for i, (vid, members, blind) in enumerate(to_process, 1):
        print(f"\n{'='*60}")
        print(f"[{i}/{len(to_process)}] {vid}"
              + (" (members)" if members else "")
              + (" (blind)" if blind else ""))
        print(f"{'='*60}", flush=True)
        result = subprocess.run(cmd_base + [vid])
        if result.returncode != 0:
            print(f"ERROR: {vid} exited with code {result.returncode}")
            failed.append((vid, f"exit code {result.returncode}"))
        else:
            print(f"OK: {vid}")

    print(f"\n{'='*60}")
    print(f"Done. Processed {len(to_process) - len(failed)}/{len(to_process)} videos.")
    if failed:
        print(f"FAILED ({len(failed)}):")
        for vid, err in failed:
            print(f"  {vid}: {err}")
    print("\nNext step: python build_db.py && python validate.py")


if __name__ == "__main__":
    main()
