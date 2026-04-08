"""
build_annotation_bundle.py
==========================
Assembles a per-video annotation bundle from existing pipeline outputs.
The bundle is a plain dict (serialisable to JSON) used by the Streamlit
annotation tool.  Nothing is written here; the caller decides where to
save it.

Usage (standalone, for inspection):
    python tools/build_annotation_bundle.py <video_id>
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUTS_DIR = REPO_ROOT / "outputs"
PLAYLIST_FILE = REPO_ROOT / "playlist.json"


# ── helpers ──────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ── public API ───────────────────────────────────────────────────────────────

def load_video_meta(video_id: str) -> dict:
    """Return title + duration from playlist.json, or minimal defaults."""
    if PLAYLIST_FILE.exists():
        data = json.loads(PLAYLIST_FILE.read_text(encoding="utf-8"))
        for entry in data.get("entries", []):
            if entry.get("id") == video_id:
                return {
                    "video_id": video_id,
                    "title": entry.get("title", video_id),
                    "duration": entry.get("duration", 0),
                    "winner": entry.get("winner"),
                }
    return {"video_id": video_id, "title": video_id, "duration": 0, "winner": None}


def load_transcript_segments(out_dir: Path) -> list[dict]:
    """
    Return transcript rows as {start, end, speaker, text}.
    Prefers segments_consistent.csv → segments_patched.csv → segments.csv.
    """
    for name in ("segments_consistent.csv", "segments_patched.csv", "segments.csv"):
        p = out_dir / name
        if p.exists():
            rows = _read_csv(p)
            return [
                {
                    "start": _float(r.get("start")),
                    "end": _float(r.get("end")),
                    "speaker": r.get("speaker", ""),
                    "text": r.get("text", ""),
                }
                for r in rows
            ]
    return []


def load_auto_phases(out_dir: Path) -> list[dict]:
    """
    Return auto phase_labels.csv rows as
    {start, end, phase, round, confidence, evidence}.
    """
    rows = _read_csv(out_dir / "phase_labels.csv")
    return [
        {
            "start": _float(r.get("start")),
            "end": _float(r.get("end")),
            "phase": r.get("phase", ""),
            "round": _int(r.get("round")),
            "confidence": _float(r.get("confidence")),
            "evidence": r.get("evidence", ""),
        }
        for r in rows
    ]


def load_auto_day_events(out_dir: Path) -> list[dict]:
    """
    Return day_events.csv rows as
    {start, end, round, event, confidence, evidence}.
    """
    rows = _read_csv(out_dir / "day_events.csv")
    return [
        {
            "start": _float(r.get("start")),
            "end": _float(r.get("end")),
            "round": _int(r.get("round")),
            "event": r.get("event", ""),
            "confidence": _float(r.get("confidence")),
            "evidence": r.get("evidence", ""),
        }
        for r in rows
    ]


def load_frame_scan_cues(out_dir: Path) -> list[dict]:
    """
    Return frame samples where votes_visible or header_visible is True.
    Result: [{t, votes_visible, header_visible}, ...] — sparse, not every frame.
    """
    fscan = out_dir / "frame_scan.json"
    if not fscan.exists():
        return []
    data = json.loads(fscan.read_text(encoding="utf-8"))
    cues = []
    for f in data.get("frames", []):
        if f.get("votes_visible") or f.get("header_visible"):
            cues.append(
                {
                    "t": float(f["t"]),
                    "votes_visible": bool(f.get("votes_visible")),
                    "header_visible": bool(f.get("header_visible")),
                }
            )
    return cues


def build_bundle(video_id: str) -> dict | None:
    """
    Assemble and return the full annotation bundle for video_id.
    Returns None if the outputs directory does not exist.
    """
    out_dir = OUTPUTS_DIR / video_id
    if not out_dir.is_dir():
        return None

    meta = load_video_meta(video_id)
    transcript = load_transcript_segments(out_dir)
    auto_phases = load_auto_phases(out_dir)
    auto_day_events = load_auto_day_events(out_dir)
    frame_cues = load_frame_scan_cues(out_dir)

    # Infer total duration: prefer playlist, fall back to last segment end.
    duration = meta["duration"]
    if not duration and transcript:
        duration = max(r["end"] for r in transcript)

    return {
        "video_id": video_id,
        "title": meta["title"],
        "duration": duration,
        "winner": meta["winner"],
        "transcript": transcript,
        "auto_phases": auto_phases,
        "auto_day_events": auto_day_events,
        "frame_cues": frame_cues,
    }


def list_annotatable_videos() -> list[dict]:
    """
    Return video_ids that have at least phase_labels.csv (N2 run).
    Also includes basic meta for display.
    """
    if not OUTPUTS_DIR.is_dir():
        return []
    result = []
    playlist_meta: dict[str, dict] = {}
    if PLAYLIST_FILE.exists():
        data = json.loads(PLAYLIST_FILE.read_text(encoding="utf-8"))
        for e in data.get("entries", []):
            playlist_meta[e["id"]] = e

    for d in sorted(OUTPUTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        vid = d.name
        if not (d / "phase_labels.csv").exists():
            continue
        meta = playlist_meta.get(vid, {})
        result.append(
            {
                "video_id": vid,
                "title": meta.get("title", vid),
                "has_auto_phases": True,
                "has_frame_scan": (d / "frame_scan.json").exists(),
                "has_segments": any(
                    (d / n).exists()
                    for n in (
                        "segments_consistent.csv",
                        "segments_patched.csv",
                        "segments.csv",
                    )
                ),
            }
        )
    return result


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tools/build_annotation_bundle.py <video_id>")
        sys.exit(1)
    vid = sys.argv[1]
    bundle = build_bundle(vid)
    if bundle is None:
        print(f"No outputs directory for {vid}")
        sys.exit(1)
    print(f"video_id  : {bundle['video_id']}")
    print(f"title     : {bundle['title']}")
    print(f"duration  : {bundle['duration']}s")
    print(f"segments  : {len(bundle['transcript'])}")
    print(f"auto_phases: {len(bundle['auto_phases'])}")
    print(f"auto_day_events: {len(bundle['auto_day_events'])}")
    print(f"frame_cues : {len(bundle['frame_cues'])}")
