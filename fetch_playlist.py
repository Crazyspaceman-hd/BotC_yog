"""fetch_playlist.py - Fetch Yogscast BotC playlist metadata via yt-dlp.

Writes playlist.json in the project root with id, title, duration, url,
and status for each video (derived from which output files exist).

Usage:
    python fetch_playlist.py --url <youtube_playlist_url>   # first run
    python fetch_playlist.py                                # subsequent runs (URL cached in playlist.json)
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

PLAYLIST_JSON = Path("playlist.json")

# Status priority: highest-completed step wins.
# Listed from most complete to least — first match wins.
_STATUS_FILES = [
    ("analyzed",    "lie_analysis.csv"),
    ("patched",     "segments_patched.csv"),
    ("merged",      "segments.csv"),
    ("diarized",    "diarization.rttm"),
    ("transcribed", "whisper_segments.jsonl"),
    ("downloaded",  "audio.wav"),
]


def compute_status(video_id: str) -> str:
    out_dir = Path(f"outputs/{video_id}")
    for status, filename in _STATUS_FILES:
        if (out_dir / filename).exists():
            return status
    return "pending"


def fetch_entries(playlist_url: str) -> list[dict]:
    """Run yt-dlp --flat-playlist and return a list of entry dicts."""
    print(f"Running yt-dlp to fetch playlist metadata ...")
    result = subprocess.run(
        ["yt-dlp", "--flat-playlist", "-J", playlist_url],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("yt-dlp stderr:", result.stderr[:2000], file=sys.stderr)
        raise RuntimeError(f"yt-dlp exited with code {result.returncode}")

    data = json.loads(result.stdout)
    entries = []
    for entry in data.get("entries", []):
        vid_id = entry.get("id", "")
        if not vid_id:
            continue
        entries.append({
            "id":       vid_id,
            "title":    entry.get("title", ""),
            "duration": entry.get("duration"),
            "url":      f"https://www.youtube.com/watch?v={vid_id}",
        })
    return entries


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch BotC playlist metadata")
    ap.add_argument("--url", help="YouTube playlist URL (required on first run)")
    args = ap.parse_args()

    # Determine URL: CLI arg > cached in playlist.json
    url = args.url
    if not url and PLAYLIST_JSON.exists():
        existing = json.loads(PLAYLIST_JSON.read_text(encoding="utf-8"))
        url = existing.get("playlist_url")

    if not url:
        ap.error(
            "--url is required on the first run.\n"
            "Example: python fetch_playlist.py --url 'https://www.youtube.com/playlist?list=...'"
        )

    # Load existing entries so we can preserve curated flags
    # (winner, blind, members_only, skip, skip_reason, num_speakers, needs_manual_st, etc.)
    existing_by_id: dict[str, dict] = {}
    if PLAYLIST_JSON.exists():
        existing_data = json.loads(PLAYLIST_JSON.read_text(encoding="utf-8"))
        for e in existing_data.get("entries", []):
            existing_by_id[e["id"]] = e

    fetched_entries = fetch_entries(url)

    # Fields that yt-dlp provides — always overwrite these from the fresh fetch
    YT_FIELDS = {"id", "title", "duration", "url"}
    # Fields managed by us — never overwrite, only set on new entries
    CURATED_FIELDS = {"winner", "blind", "members_only", "skip", "skip_reason",
                      "num_speakers", "needs_manual_st", "status"}

    merged: list[dict] = []
    new_count = 0
    for fe in fetched_entries:
        vid = fe["id"]
        if vid in existing_by_id:
            # Start from existing entry (preserves all curated flags)
            entry = dict(existing_by_id[vid])
            # Update only the yt-dlp-sourced fields
            for k in YT_FIELDS:
                if k in fe:
                    entry[k] = fe[k]
        else:
            # Brand-new video — no curated data yet
            entry = dict(fe)
            new_count += 1
            print(f"  NEW: {vid}  {fe.get('title', '')[:60]}")

        # Always recompute status from filesystem
        entry["status"] = compute_status(vid)
        merged.append(entry)

    # Preserve any existing entries NOT returned by YouTube
    # (members-only / unlisted videos that yt-dlp can't see without auth)
    fetched_ids = {fe["id"] for fe in fetched_entries}
    for vid, existing_entry in existing_by_id.items():
        if vid not in fetched_ids:
            existing_entry["status"] = compute_status(vid)
            merged.append(existing_entry)
            print(f"  KEPT (not in playlist fetch): {vid}  {existing_entry.get('title', '')[:50]}")

    output = {
        "playlist_url": url,
        "entries": merged,
    }
    PLAYLIST_JSON.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    # Print summary table
    col_id    = 15
    col_stat  = 12
    col_title = 52
    header = f"{'ID':<{col_id}} {'Status':<{col_stat}} {'Title'}"
    print(f"\n{header}")
    print("-" * (col_id + col_stat + col_title + 2))
    for e in merged:
        title = (e.get("title") or "(no title)")[:col_title]
        print(f"{e['id']:<{col_id}} {e.get('status',''):<{col_stat}} {title}")

    print(f"\nTotal: {len(merged)} entries ({new_count} new). Written to {PLAYLIST_JSON}")
    status_counts: dict[str, int] = {}
    for e in merged:
        status_counts[e.get("status", "unknown")] = status_counts.get(e.get("status", "unknown"), 0) + 1
    for s, n in sorted(status_counts.items()):
        print(f"  {s}: {n}")


if __name__ == "__main__":
    main()
