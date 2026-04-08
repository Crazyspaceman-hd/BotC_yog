"""
compare_phase_annotations.py
============================
Evaluate auto phase predictions against manual ground-truth annotations.

For each annotated video:
  - Phase interval comparison: IoU per ground-truth interval; overall coverage
  - Event marker comparison: nearest auto event within tolerance window
  - Summary table across all annotated videos

Usage:
    python tools/compare_phase_annotations.py              # all annotated videos
    python tools/compare_phase_annotations.py <video_id>   # one video
    python tools/compare_phase_annotations.py --csv        # write results.csv

Outputs diagnostic text to stdout.  With --csv, also writes:
    data/annotations/phase_eval_results.csv
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TRUTH_DIR = REPO_ROOT / "data" / "annotations" / "phase_truth"
OUTPUTS_DIR = REPO_ROOT / "outputs"
EVAL_CSV = REPO_ROOT / "data" / "annotations" / "phase_eval_results.csv"

# Event matching tolerance: auto event must start within ±N seconds of truth
EVENT_TOL_S = 30.0


# ── data classes ─────────────────────────────────────────────────────────────

class PhaseInterval(NamedTuple):
    start: float
    end: float
    phase: str
    round: int


class EventMarker(NamedTuple):
    start: float
    end: float
    event: str
    round: int


# ── IoU helpers ───────────────────────────────────────────────────────────────

def _iou(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    inter = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    union = (a_end - a_start) + (b_end - b_start) - inter
    return inter / union if union > 0 else 0.0


def _overlap_seconds(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


# ── loaders ───────────────────────────────────────────────────────────────────

def _load_truth(video_id: str) -> dict | None:
    p = TRUTH_DIR / f"{video_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _load_auto(video_id: str) -> tuple[list[PhaseInterval], list[EventMarker]]:
    out = OUTPUTS_DIR / video_id
    phases: list[PhaseInterval] = []
    events: list[EventMarker] = []

    p = out / "phase_labels.csv"
    if p.exists():
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    phases.append(
                        PhaseInterval(
                            float(row["start"]),
                            float(row["end"]),
                            row["phase"],
                            int(row.get("round", 0) or 0),
                        )
                    )
                except (KeyError, ValueError):
                    pass

    e = out / "day_events.csv"
    if e.exists():
        with open(e, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    events.append(
                        EventMarker(
                            float(row["start"]),
                            float(row["end"]),
                            row["event"],
                            int(row.get("round", 0) or 0),
                        )
                    )
                except (KeyError, ValueError):
                    pass

    return phases, events


# ── per-video evaluation ──────────────────────────────────────────────────────

def evaluate_video(video_id: str) -> dict | None:
    truth = _load_truth(video_id)
    if truth is None:
        return None

    auto_phases, auto_events = _load_auto(video_id)

    truth_phases = [
        PhaseInterval(p["start"], p["end"], p["phase"], p.get("round", 0))
        for p in truth.get("phases", [])
    ]
    truth_events = [
        EventMarker(e["start"], e["end"], e["event"], e.get("round", 0))
        for e in truth.get("events", [])
    ]
    duration = truth.get("duration", 1.0) or 1.0

    # ── Phase interval evaluation ─────────────────────────────────────────────
    phase_rows = []
    total_truth_duration = 0.0
    total_correct_overlap = 0.0

    for tp in truth_phases:
        tp_dur = tp.end - tp.start
        total_truth_duration += tp_dur

        # Best matching auto interval: same phase label, highest IoU
        best_iou = 0.0
        best_match: PhaseInterval | None = None
        for ap in auto_phases:
            if ap.phase != tp.phase:
                continue
            iou = _iou(tp.start, tp.end, ap.start, ap.end)
            if iou > best_iou:
                best_iou = iou
                best_match = ap

        # Overlap with any same-phase auto interval (for coverage)
        overlap = sum(
            _overlap_seconds(tp.start, tp.end, ap.start, ap.end)
            for ap in auto_phases
            if ap.phase == tp.phase
        )
        overlap = min(overlap, tp_dur)
        total_correct_overlap += overlap

        phase_rows.append(
            {
                "truth_start": tp.start,
                "truth_end": tp.end,
                "phase": tp.phase,
                "round": tp.round,
                "best_auto_start": best_match.start if best_match else None,
                "best_auto_end": best_match.end if best_match else None,
                "iou": round(best_iou, 3),
                "overlap_s": round(overlap, 1),
                "truth_dur_s": round(tp_dur, 1),
                "matched": best_match is not None,
            }
        )

    phase_coverage = total_correct_overlap / total_truth_duration if total_truth_duration > 0 else 0.0

    # ── Event marker evaluation ───────────────────────────────────────────────
    event_rows = []
    n_matched = 0
    n_wrong_type = 0

    for te in truth_events:
        # Find nearest auto event within tolerance, same type first
        same_type = [
            ae for ae in auto_events
            if ae.event == te.event
            and abs(ae.start - te.start) <= EVENT_TOL_S
        ]
        any_type = [
            ae for ae in auto_events
            if abs(ae.start - te.start) <= EVENT_TOL_S
        ]

        if same_type:
            nearest = min(same_type, key=lambda ae: abs(ae.start - te.start))
            match_status = "correct"
            n_matched += 1
        elif any_type:
            nearest = min(any_type, key=lambda ae: abs(ae.start - te.start))
            match_status = "wrong_type"
            n_wrong_type += 1
        else:
            nearest = None
            match_status = "missed"

        event_rows.append(
            {
                "truth_start": te.start,
                "event": te.event,
                "round": te.round,
                "auto_start": nearest.start if nearest else None,
                "auto_event": nearest.event if nearest else None,
                "delta_s": round(nearest.start - te.start, 1) if nearest else None,
                "match_status": match_status,
            }
        )

    n_truth_events = len(truth_events)
    n_spurious = len(
        [
            ae for ae in auto_events
            if not any(
                ae.event == te.event and abs(ae.start - te.start) <= EVENT_TOL_S
                for te in truth_events
            )
        ]
    )

    return {
        "video_id": video_id,
        "title": truth.get("title", video_id),
        "duration": duration,
        "n_truth_phases": len(truth_phases),
        "n_auto_phases": len(auto_phases),
        "phase_coverage": round(phase_coverage, 3),
        "phase_rows": phase_rows,
        "n_truth_events": n_truth_events,
        "n_auto_events": len(auto_events),
        "n_event_matched": n_matched,
        "n_event_wrong_type": n_wrong_type,
        "n_event_missed": n_truth_events - n_matched - n_wrong_type,
        "n_event_spurious": n_spurious,
        "event_rows": event_rows,
    }


# ── printing ──────────────────────────────────────────────────────────────────

def _bar(value: float, width: int = 20) -> str:
    filled = int(round(value * width))
    return "[" + "#" * filled + "." * (width - filled) + f"] {value:.0%}"


def print_video_report(result: dict) -> None:
    vid = result["video_id"]
    title = result["title"]
    print(f"\n{'='*70}")
    print(f"  {vid}  {title[:50]}")
    print(f"{'='*70}")

    print(f"\n  Phases  (truth={result['n_truth_phases']}  auto={result['n_auto_phases']})")
    print(f"  Coverage  {_bar(result['phase_coverage'])}")
    for row in result["phase_rows"]:
        matched_str = (
            f"  -> auto {_fmt_ts(row['best_auto_start'])} - {_fmt_ts(row['best_auto_end'])}"
            f"  IoU={row['iou']:.2f}"
            if row["matched"]
            else "  -> NO MATCH"
        )
        print(
            f"    [{_fmt_ts(row['truth_start'])} - {_fmt_ts(row['truth_end'])}]"
            f"  {row['phase']:8s}  R{row['round']}"
            f"  ({row['truth_dur_s']}s)"
            f"{matched_str}"
        )

    print(
        f"\n  Events  truth={result['n_truth_events']}  auto={result['n_auto_events']}"
        f"  matched={result['n_event_matched']}"
        f"  wrong_type={result['n_event_wrong_type']}"
        f"  missed={result['n_event_missed']}"
        f"  spurious={result['n_event_spurious']}"
    )
    for row in result["event_rows"]:
        delta_str = f"  d={row['delta_s']:+.0f}s" if row["delta_s"] is not None else ""
        auto_str = (
            f"  -> {row['auto_event']} @ {_fmt_ts(row['auto_start'])}{delta_str}"
            if row["auto_start"] is not None
            else "  -> MISSED"
        )
        status_marker = {"correct": "OK", "wrong_type": "~", "missed": "XX"}.get(
            row["match_status"], "?"
        )
        print(
            f"    {status_marker}  {_fmt_ts(row['truth_start'])}  {row['event']:30s}"
            f"  R{row['round']}{auto_str}"
        )


def _fmt_ts(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def print_summary(results: list[dict]) -> None:
    if not results:
        return
    print(f"\n{'='*70}")
    print("  SUMMARY")
    print(f"{'='*70}")
    print(
        f"  {'Video':20s}  {'Cov':>6s}  {'Ph T/A':>8s}  "
        f"{'Ev T/A':>8s}  {'Match':>7s}  {'Miss':>6s}  {'Spur':>6s}"
    )
    print(f"  {'-'*67}")
    for r in results:
        cov = f"{r['phase_coverage']:.0%}"
        ph = f"{r['n_truth_phases']}/{r['n_auto_phases']}"
        ev = f"{r['n_truth_events']}/{r['n_auto_events']}"
        match_pct = (
            r["n_event_matched"] / r["n_truth_events"]
            if r["n_truth_events"] > 0
            else 0.0
        )
        print(
            f"  {r['video_id']:20s}  {cov:>6s}  {ph:>8s}  {ev:>8s}"
            f"  {match_pct:>6.0%}  {r['n_event_missed']:>6d}  {r['n_event_spurious']:>6d}"
        )

    avg_cov = sum(r["phase_coverage"] for r in results) / len(results)
    total_truth_ev = sum(r["n_truth_events"] for r in results)
    total_matched = sum(r["n_event_matched"] for r in results)
    total_missed = sum(r["n_event_missed"] for r in results)
    total_spurious = sum(r["n_event_spurious"] for r in results)
    overall_match = total_matched / total_truth_ev if total_truth_ev > 0 else 0.0

    print(f"  {'-'*67}")
    print(
        f"  {'OVERALL':20s}  {avg_cov:>6.0%}  {'':>8s}  {'':>8s}"
        f"  {overall_match:>6.0%}  {total_missed:>6d}  {total_spurious:>6d}"
    )
    print(
        f"\n  Videos evaluated: {len(results)} | "
        f"Phase coverage: {avg_cov:.0%} | "
        f"Event match rate: {overall_match:.0%}"
    )


# ── CSV writer ────────────────────────────────────────────────────────────────

def write_csv(results: list[dict]) -> None:
    EVAL_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "video_id", "title", "duration",
        "n_truth_phases", "n_auto_phases", "phase_coverage",
        "n_truth_events", "n_auto_events",
        "n_event_matched", "n_event_wrong_type", "n_event_missed", "n_event_spurious",
    ]
    with open(EVAL_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    print(f"\n  CSV written -> {EVAL_CSV}")


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    write_csv_flag = "--csv" in args
    args = [a for a in args if a != "--csv"]

    if args:
        video_ids = args
    else:
        video_ids = [p.stem for p in sorted(TRUTH_DIR.glob("*.json"))]

    if not video_ids:
        print("No annotated videos found in data/annotations/phase_truth/")
        print("Run: streamlit run tools/annotate_phases_app.py")
        sys.exit(0)

    results = []
    for vid in video_ids:
        res = evaluate_video(vid)
        if res is None:
            print(f"  Skipping {vid}: no truth file or outputs missing")
            continue
        results.append(res)
        print_video_report(res)

    print_summary(results)

    if write_csv_flag:
        write_csv(results)


if __name__ == "__main__":
    main()
