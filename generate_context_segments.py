"""generate_context_segments.py — N2b: Synthesize context_segments.csv from N2 outputs.

Reads:
    outputs/<id>/phase_labels.csv   (from N2)
    outputs/<id>/day_events.csv     (from N2, optional)

Produces:
    outputs/<id>/context_segments.csv

Each row describes the interpretation context of a time window:
    broad_phase, context_mode, speaker_type, audience_scope, confidence, source

Contract: docs/context_contract.md

Usage:
    python generate_context_segments.py <video_id>
    python generate_context_segments.py <video_id> --force
    python generate_context_segments.py --all [--force]
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

OUTPUTS_DIR = Path("outputs")
PLAYLIST_PATH = Path("playlist.json")

# ── Tuning constants ───────────────────────────────────────────────────────────

# First N seconds of a Day phase immediately following a Night phase are
# labelled morning_result_announcement (ST announcing last night's death).
# Generous: the phase detector sometimes labels Night 1 as Day, so the real
# morning can be well into what looks like a Day interval.
MORNING_WINDOW_S: float = 600.0

# Events in day_events.csv that qualify as private-like windows.
_PRIVATE_EVENTS = {"StorytellerInterruption"}

# Events that mark execution windows.
_EXECUTION_EVENTS = {"VoteSequence"}

# Output field names (must match context_contract.md)
_FIELDNAMES = [
    "timestamp_start",
    "timestamp_end",
    "broad_phase",
    "context_mode",
    "speaker_type",
    "audience_scope",
    "confidence",
    "source",
]


# ── Loaders ────────────────────────────────────────────────────────────────────


def _load_phase_labels(out_dir: Path) -> list[dict]:
    p = out_dir / "phase_labels.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _load_day_events(out_dir: Path) -> list[dict]:
    p = out_dir / "day_events.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# ── Core logic ─────────────────────────────────────────────────────────────────


def _build_context_rows(
    phase_labels: list[dict],
    day_events: list[dict],
) -> list[dict]:
    """Produce context_segments rows from N2 outputs."""
    rows: list[dict] = []

    for i, phase_row in enumerate(phase_labels):
        try:
            phase_start = float(phase_row["start"])
            phase_end = float(phase_row["end"])
        except (KeyError, ValueError):
            continue

        broad_phase = phase_row.get("phase", "Unknown").strip()
        broad_lower = broad_phase.lower()
        phase_conf = float(phase_row.get("confidence", 0.8) or 0.8)

        # Previous phase (for morning window logic)
        prev_phase_lower = phase_labels[i - 1].get("phase", "").strip().lower() if i > 0 else ""

        if broad_lower == "intro":
            rows.append(_make_row(
                phase_start, phase_end,
                broad_phase="intro",
                context_mode="intro_meta",
                speaker_type="mixed",
                audience_scope="public",
                confidence=phase_conf,
                source="phase_label",
            ))

        elif broad_lower == "night":
            rows.append(_make_row(
                phase_start, phase_end,
                broad_phase="night",
                context_mode="night_private_action",
                speaker_type="mixed",
                audience_scope="private_like",
                confidence=phase_conf,
                source="phase_label",
            ))

        elif broad_lower == "day":
            # Collect relevant day_events that fall (even partially) within this Day interval.
            # We only care about VoteSequence and StorytellerInterruption.
            relevant_events: list[dict] = []
            for e in day_events:
                evt = e.get("event", "")
                if evt not in (_PRIVATE_EVENTS | _EXECUTION_EVENTS):
                    continue
                try:
                    e_start = float(e["start"])
                    e_end = float(e["end"])
                except (KeyError, ValueError):
                    continue
                # Clamp to phase interval
                e_start = max(e_start, phase_start)
                e_end = min(e_end, phase_end)
                if e_end <= e_start:
                    continue
                relevant_events.append({**e, "_start": e_start, "_end": e_end})

            relevant_events.sort(key=lambda e: e["_start"])

            # Build a sorted, de-duplicated list of breakpoints
            breakpoints: list[float] = [phase_start]
            for e in relevant_events:
                breakpoints.append(e["_start"])
                breakpoints.append(e["_end"])
            breakpoints.append(phase_end)
            # Deduplicate and sort; drop any point outside [phase_start, phase_end]
            breakpoints = sorted({
                round(b, 3)
                for b in breakpoints
                if phase_start - 0.01 <= b <= phase_end + 0.01
            })

            # Whether the morning window applies (this Day follows a Night)
            has_morning = prev_phase_lower == "night"

            # For each sub-interval between consecutive breakpoints
            for j in range(len(breakpoints) - 1):
                sub_start = breakpoints[j]
                sub_end = breakpoints[j + 1]
                if sub_end - sub_start < 0.01:
                    continue

                # Find events whose clamped [_start, _end] covers this sub-interval.
                # "Covers" means: e._start <= sub_start AND e._end >= sub_end
                # (the sub-interval is entirely within the event, which is guaranteed
                # by the breakpoints construction).
                covering_evts = [
                    e for e in relevant_events
                    if e["_start"] <= sub_start + 0.01 and e["_end"] >= sub_end - 0.01
                ]

                # Priority: VoteSequence > StorytellerInterruption > morning > default
                if any(e.get("event") in _EXECUTION_EVENTS for e in covering_evts):
                    ctx_mode = "execution_window"
                    scope = "public"
                    spk_type = "mixed"
                    src = "day_event:VoteSequence"
                    conf = 0.85

                elif any(e.get("event") in _PRIVATE_EVENTS for e in covering_evts):
                    ctx_mode = "storyteller_narration"
                    scope = "private_like"
                    spk_type = "storyteller"
                    src = "day_event:StorytellerInterruption"
                    conf = 0.80

                elif has_morning and (sub_start - phase_start) < MORNING_WINDOW_S:
                    ctx_mode = "morning_result_announcement"
                    scope = "public"
                    spk_type = "mixed"
                    src = "day_event:morning_window"
                    conf = 0.75

                else:
                    ctx_mode = "public_day_discussion"
                    scope = "public"
                    spk_type = "mixed"
                    src = "phase_label"
                    conf = phase_conf

                rows.append(_make_row(
                    sub_start, sub_end,
                    broad_phase="day",
                    context_mode=ctx_mode,
                    speaker_type=spk_type,
                    audience_scope=scope,
                    confidence=conf,
                    source=src,
                ))

        else:
            # Unknown or unrecognised phase
            rows.append(_make_row(
                phase_start, phase_end,
                broad_phase=broad_lower,
                context_mode="ambiguous",
                speaker_type="unknown",
                audience_scope="ambiguous",
                confidence=0.3,
                source="phase_label",
            ))

    return rows


def _make_row(
    ts: float,
    te: float,
    *,
    broad_phase: str,
    context_mode: str,
    speaker_type: str,
    audience_scope: str,
    confidence: float,
    source: str,
) -> dict:
    return {
        "timestamp_start": round(ts, 3),
        "timestamp_end": round(te, 3),
        "broad_phase": broad_phase,
        "context_mode": context_mode,
        "speaker_type": speaker_type,
        "audience_scope": audience_scope,
        "confidence": round(confidence, 3),
        "source": source,
    }


# ── Per-video entry point ───────────────────────────────────────────────────────


def generate(video_id: str, force: bool = False) -> bool:
    """Generate context_segments.csv for one video.

    Returns True on success, False on skip (output exists and not force).
    """
    out_dir = OUTPUTS_DIR / video_id
    out_path = out_dir / "context_segments.csv"

    if out_path.exists() and not force:
        return False

    phase_labels = _load_phase_labels(out_dir)
    if not phase_labels:
        print(f"  SKIP {video_id}: phase_labels.csv not found")
        return False

    day_events = _load_day_events(out_dir)

    rows = _build_context_rows(phase_labels, day_events)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    # Summary
    mode_counts: dict[str, int] = {}
    for r in rows:
        mode_counts[r["context_mode"]] = mode_counts.get(r["context_mode"], 0) + 1

    print(f"  {video_id}: {len(rows)} context rows written")
    for mode, cnt in sorted(mode_counts.items()):
        print(f"    {mode}: {cnt}")

    return True


# ── Playlist helpers ────────────────────────────────────────────────────────────


def _all_video_ids() -> list[str]:
    if not PLAYLIST_PATH.exists():
        return []
    try:
        data = json.loads(PLAYLIST_PATH.read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else data.get("entries", [])
        return [
            e["id"]
            for e in entries
            if not e.get("skip") and not e.get("members_only")
            and e.get("status") not in (None, "pending", "downloaded")
            and (OUTPUTS_DIR / e["id"] / "phase_labels.csv").exists()
        ]
    except Exception:
        return []


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="N2b: generate context_segments.csv from N2 phase outputs"
    )
    parser.add_argument("video_id", nargs="?", help="single video ID to process")
    parser.add_argument("--all", action="store_true", help="process all eligible videos")
    parser.add_argument("--force", action="store_true", help="overwrite existing output")
    args = parser.parse_args()

    if args.all:
        ids = _all_video_ids()
        print(f"Processing {len(ids)} videos ...")
        done = skipped = 0
        for vid in ids:
            result = generate(vid, force=args.force)
            if result:
                done += 1
            else:
                skipped += 1
        print(f"\nDone: {done} generated, {skipped} skipped (already exist).")
    elif args.video_id:
        result = generate(args.video_id, force=args.force)
        if not result:
            print(f"  Skipped (already exists — use --force to overwrite).")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
