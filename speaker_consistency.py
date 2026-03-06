"""speaker_consistency.py — N1: Episode-local speaker consistency.

Smooths intra-episode diarization fragmentation without creating cross-episode
voice identities.  Reads the raw RTTM diarization alongside the merged segment
CSV and applies three lightweight deterministic passes:

  Pass 1 — Absorb micro-segments
      Any segment shorter than MIN_FLIP_S seconds is absorbed into whichever
      adjacent segment is longer (or the preceding one on a tie).

  Pass 2 — Smooth A/B/A flips
      If a short B segment is sandwiched between two A segments (same speaker)
      and the B duration < CONTEXT_S, relabel B as A.

  Pass 3 — Merge adjacent same-speaker rows
      Consecutive rows assigned to the same speaker are collapsed into one.

The original diarization.rttm and segments.csv are NOT overwritten.
Output is written to:
    outputs/<video_id>/segments_consistent.csv   (same columns as segments.csv)

Usage:
    python speaker_consistency.py <video_id>
    python speaker_consistency.py <video_id> --min-flip-s 0.4 --context-s 2.0
    python speaker_consistency.py <video_id> --force
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

# ── Tuneable defaults ──────────────────────────────────────────────────────────
DEFAULT_MIN_FLIP_S = 0.5   # segments shorter than this are candidates for absorption
DEFAULT_CONTEXT_S  = 2.0   # A/B/A sandwich: B must be shorter than this
MAX_MERGE_S        = 30.0  # Pass 3 cap: never merge beyond this duration


# ── Segment dataclass (plain dict-like tuple for speed) ───────────────────────

def _load_segments(out_dir: Path) -> list[dict]:
    """Load the best available segment CSV (patched → consistent → raw)."""
    for name in ("segments_patched.csv", "segments_consistent.csv", "segments.csv"):
        p = out_dir / name
        if p.exists():
            with p.open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            print(f"  Reading {name}  ({len(rows)} segments)")
            return rows
    raise FileNotFoundError(f"No segment CSV found in {out_dir}")


# ── Pass 1: absorb micro-segments ─────────────────────────────────────────────

def _pass1_absorb(rows: list[dict], min_s: float) -> list[dict]:
    """Merge segments shorter than min_s into their longer neighbour."""
    if not rows:
        return rows
    segs = [dict(r) for r in rows]
    changed = True
    while changed:
        changed = False
        out: list[dict] = []
        i = 0
        while i < len(segs):
            seg = segs[i]
            dur = float(seg["end"]) - float(seg["start"])
            if dur < min_s and len(segs) > 1:
                prev_dur = (float(out[-1]["end"]) - float(out[-1]["start"])
                            if out else -1)
                next_dur = (float(segs[i + 1]["end"]) - float(segs[i + 1]["start"])
                            if i + 1 < len(segs) else -1)
                if prev_dur >= next_dur and out:
                    # absorb into previous: extend its end, append text
                    p = out[-1]
                    p["end"]  = seg["end"]
                    p["text"] = (p["text"] + " " + seg["text"]).strip()
                    changed = True
                elif i + 1 < len(segs):
                    # absorb into next: extend next's start, prepend text
                    n = segs[i + 1]
                    n["start"] = seg["start"]
                    n["text"]  = (seg["text"] + " " + n["text"]).strip()
                    changed = True
                else:
                    out.append(seg)
            else:
                out.append(seg)
            i += 1
        segs = out
    return segs


# ── Pass 2: smooth A/B/A flips ─────────────────────────────────────────────────

def _pass2_aba(rows: list[dict], context_s: float) -> list[dict]:
    """Relabel short B segments surrounded by the same speaker A."""
    if len(rows) < 3:
        return rows
    segs = [dict(r) for r in rows]
    changed = True
    while changed:
        changed = False
        for i in range(1, len(segs) - 1):
            prev_spk = segs[i - 1]["speaker"]
            curr     = segs[i]
            next_spk = segs[i + 1]["speaker"]
            curr_dur = float(curr["end"]) - float(curr["start"])
            if (prev_spk == next_spk
                    and curr["speaker"] != prev_spk
                    and curr_dur < context_s):
                curr["speaker"] = prev_spk
                changed = True
    return segs


# ── Pass 3: merge adjacent same-speaker rows ──────────────────────────────────

def _pass3_merge(rows: list[dict], max_merge_s: float = MAX_MERGE_S) -> list[dict]:
    """Collapse consecutive same-speaker segments, capped at max_merge_s.

    Without a cap this pass can create very long super-segments that damage
    timestamp precision for downstream claim extraction.  Any merge that would
    push the combined duration past max_merge_s starts a new segment instead.
    """
    if not rows:
        return rows
    out = [dict(rows[0])]
    for seg in rows[1:]:
        prev = out[-1]
        merged_dur = float(seg["end"]) - float(prev["start"])
        if seg["speaker"] == prev["speaker"] and merged_dur <= max_merge_s:
            prev["end"]  = seg["end"]
            prev["text"] = (prev["text"] + " " + seg["text"]).strip()
        else:
            out.append(dict(seg))
    return out


# ── Metrics ───────────────────────────────────────────────────────────────────

def _count_flips(rows: list[dict]) -> int:
    flips = 0
    for i in range(1, len(rows)):
        if rows[i]["speaker"] != rows[i - 1]["speaker"]:
            flips += 1
    return flips


def _speaker_dist(rows: list[dict]) -> dict[str, int]:
    from collections import Counter
    return dict(Counter(r["speaker"] for r in rows))


# ── Main ──────────────────────────────────────────────────────────────────────

def main(video_id: str,
         min_flip_s: float  = DEFAULT_MIN_FLIP_S,
         context_s: float   = DEFAULT_CONTEXT_S,
         max_merge_s: float = MAX_MERGE_S,
         force: bool        = False) -> None:
    out_dir  = Path("outputs") / video_id
    out_path = out_dir / "segments_consistent.csv"

    if out_path.exists() and not force:
        print(f"  [SKIP] segments_consistent.csv already exists for {video_id}")
        return

    print(f"\n=== speaker_consistency.py — {video_id} ===")
    print(f"  min_flip_s={min_flip_s}s  context_s={context_s}s  max_merge_s={max_merge_s}s")

    rows = _load_segments(out_dir)
    if not rows:
        print("  [SKIP] No segments.")
        return

    before_n     = len(rows)
    before_flips = _count_flips(rows)
    before_dist  = _speaker_dist(rows)

    # Apply passes
    segs = _pass1_absorb(rows, min_flip_s)
    segs = _pass2_aba(segs, context_s)
    segs = _pass3_merge(segs, max_merge_s)

    after_n     = len(segs)
    after_flips = _count_flips(segs)
    after_dist  = _speaker_dist(segs)

    # Write output
    fieldnames = list(rows[0].keys())  # preserve original column order
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(segs)

    # Report
    print(f"\n  Before/after metrics:")
    print(f"    Segments : {before_n:4d}  ->  {after_n:4d}"
          f"  (d {after_n - before_n:+d})")
    print(f"    Flips    : {before_flips:4d}  ->  {after_flips:4d}"
          f"  (d {after_flips - before_flips:+d},"
          f"  {(before_flips - after_flips) / max(before_flips, 1) * 100:.1f}% reduction)")
    print(f"    Speaker distribution before: {before_dist}")
    print(f"    Speaker distribution after : {after_dist}")
    print(f"\n  Wrote {after_n} rows -> {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Episode-local speaker consistency smoothing (N1)")
    ap.add_argument("video_id",
                    help="YouTube video ID (e.g. sitpjTGSDhs)")
    ap.add_argument("--min-flip-s", type=float, default=DEFAULT_MIN_FLIP_S,
                    help=f"Absorb segments shorter than this (default: {DEFAULT_MIN_FLIP_S}s)")
    ap.add_argument("--context-s", type=float, default=DEFAULT_CONTEXT_S,
                    help=f"A/B/A window size (default: {DEFAULT_CONTEXT_S}s)")
    ap.add_argument("--max-merge-s", type=float, default=MAX_MERGE_S,
                    help=f"Pass 3 merge duration cap (default: {MAX_MERGE_S}s)")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing segments_consistent.csv")
    args = ap.parse_args()
    main(args.video_id,
         min_flip_s=args.min_flip_s,
         context_s=args.context_s,
         max_merge_s=args.max_merge_s,
         force=args.force)
