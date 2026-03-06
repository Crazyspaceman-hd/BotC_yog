"""detect_phases.py — N2: Game-phase boundary detection.

Reads segments_patched.csv (or segments_consistent.csv / segments.csv) and
produces phase_labels.csv labelling each interval of the episode as one of:

    Intro        — pre-game introductions (players announce their roles)
    Night        — storyteller resolves night actions
    Day          — open discussion phase
    Nomination   — nomination / voting window
    Execution    — execution announcement
    Unknown      — gap with no confident signal (forward-filled at output)

Algorithm
---------
1. Identify the Storyteller speaker: the dominant talker in the first
   INTRO_CUTOFF_S seconds (word-count weighted).
2. Scan every segment for keyword patterns.  Each hit produces a
   (timestamp, phase, weight, evidence_text) trigger.
   Storyteller triggers receive full weight; other speakers are down-weighted.
3. For every segment boundary, accumulate trigger weights within +-CONTEXT_S.
   The winning phase is the highest-score phase at that midpoint.
4. Forward-fill Unknown intervals from the previous known phase, with
   confidence decaying linearly.
5. Merge islands shorter than MIN_PHASE_S into their neighbours.
6. Collapse adjacent same-phase intervals.

Output columns: start, end, phase, confidence, evidence

Usage:
    python detect_phases.py <video_id>
    python detect_phases.py <video_id> --force
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from pathlib import Path

# ── Tuning constants ───────────────────────────────────────────────────────────
INTRO_CUTOFF_S        = 330.0   # intro region ends here (unless strong signal)
MIN_PHASE_S           = 30.0    # minimum island duration before merging
CONTEXT_S             = 20.0    # keyword context window (+-seconds)
CONFIDENCE_DECAY_S    = 0.003   # confidence drop per second since last signal
STORYTELLER_DOWNSCALE = 0.3     # weight multiplier for non-ST keywords in ST-only signals

# ── Signal table ───────────────────────────────────────────────────────────────
# (compiled_regex, phase, weight, storyteller_only)
_SIGNALS: list[tuple[re.Pattern, str, float, bool]] = []

def _sig(pat: str, phase: str, w: float = 1.0, st: bool = False) -> None:
    _SIGNALS.append((re.compile(pat, re.I), phase, w, st))

# Night
_sig(r"\bit(?:'s| is) now night\b",               "Night", 1.0, True)
_sig(r"\bnight (?:one|two|three|four|five|six|seven|eight|nine|\d+)\b",
                                                   "Night", 0.9, True)
_sig(r"\beveryone (?:close|shut) your eyes\b",    "Night", 0.9, True)
_sig(r"\bnight (?:begins|starts|time)\b",         "Night", 0.8, True)
_sig(r"\bgood night\b",                           "Night", 0.5, True)
_sig(r"\bsun(?:set|down)\b",                      "Night", 0.6, True)
_sig(r"\bnight falls?\b",                         "Night", 0.7, True)

# Day
_sig(r"\bit(?:'s| is) now day\b",                 "Day",   1.0, True)
_sig(r"\bday (?:one|two|three|four|five|six|seven|eight|nine|\d+)\b",
                                                   "Day",   0.9, True)
_sig(r"\bgood morning\b",                         "Day",   0.8, True)
_sig(r"\beveryone (?:open|opens?) your eyes\b",   "Day",   0.9, True)
_sig(r"\bwake up\b",                              "Day",   0.5, True)
_sig(r"\bthe sun rises?\b",                       "Day",   0.8, True)
_sig(r"\bday (?:begins|starts|time)\b",           "Day",   0.8, True)
_sig(r"\bsunrise\b",                              "Day",   0.7, True)

# Nomination
_sig(r"\bnominations?(?: are| is)? open\b",       "Nomination", 1.0, True)
_sig(r"\bi nominate\b",                           "Nomination", 0.8, False)
_sig(r"\bwho (?:do you |would you )?nominate\b",  "Nomination", 0.7, True)
_sig(r"\bvote(?:s)?(?: are| is)? open\b",         "Nomination", 0.9, True)
_sig(r"\bput(?:ting)? (?:\w+ )?on the block\b",   "Nomination", 0.7, False)
_sig(r"\btime to (?:vote|nominate)\b",            "Nomination", 0.8, True)

# Execution
_sig(r"\bhas been executed\b",                    "Execution", 1.0, True)
_sig(r"\byou(?:'ve| have) been executed\b",       "Execution", 1.0, True)
_sig(r"\bwas executed\b",                         "Execution", 0.9, True)
_sig(r"\bexecution (?:is )?complete\b",           "Execution", 0.9, True)
_sig(r"\bput to death\b",                         "Execution", 0.8, True)
_sig(r"\bthe vote (?:passed|carries)\b",          "Execution", 0.7, True)
_sig(r"\bis now dead\b",                          "Execution", 0.8, True)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_segments(out_dir: Path) -> list[dict]:
    for name in ("segments_consistent.csv", "segments_patched.csv", "segments.csv"):
        p = out_dir / name
        if p.exists():
            with p.open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            print(f"  Using {name}  ({len(rows)} segments)")
            return rows
    raise FileNotFoundError(f"No segment CSV found in {out_dir}")


def _find_storyteller(rows: list[dict]) -> str | None:
    """Dominant speaker in the first INTRO_CUTOFF_S seconds (by word count)."""
    counts: Counter = Counter()
    for r in rows:
        if float(r["start"]) >= INTRO_CUTOFF_S:
            break
        counts[r["speaker"]] += len(r["text"].split())
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _collect_triggers(rows: list[dict],
                      st_id: str | None,
                      ) -> list[tuple[float, str, float, str]]:
    """Return (timestamp, phase, weight, evidence) for every keyword hit."""
    triggers = []
    for row in rows:
        t    = float(row["start"])
        text = row["text"]
        spk  = row["speaker"]
        is_st = (spk == st_id) if st_id else False
        for pat, phase, w, st_only in _SIGNALS:
            if pat.search(text):
                eff_w = w if (not st_only or is_st) else w * STORYTELLER_DOWNSCALE
                triggers.append((t, phase, eff_w, text[:80].replace("\n", " ")))
    return triggers


def _vote(t: float,
          triggers: list[tuple[float, str, float, str]],
          ) -> tuple[str, float, str]:
    """Return (phase, confidence, evidence) by weighting nearby triggers."""
    scores: dict[str, float] = {}
    top_evid = ""
    for trig_t, phase, w, evid in triggers:
        dt = abs(t - trig_t)
        if dt <= CONTEXT_S:
            eff = w * (1.0 - dt / CONTEXT_S)
            scores[phase] = scores.get(phase, 0.0) + eff
            if eff > 0.05 and not top_evid:
                top_evid = evid
    if not scores:
        return "Unknown", 0.0, ""
    best  = max(scores, key=scores.__getitem__)
    total = sum(scores.values())
    conf  = round(min(scores[best] / max(total, 1e-9), 1.0), 3)
    return best, conf, top_evid


def _build_intervals(rows: list[dict],
                     triggers: list[tuple[float, str, float, str]],
                     ) -> list[tuple[float, float, str, float, str]]:
    """One interval per unique segment boundary pair."""
    boundaries = sorted({float(r["start"]) for r in rows}
                        | {float(r["end"])   for r in rows})
    result = []
    for i in range(len(boundaries) - 1):
        t0   = boundaries[i]
        t1   = boundaries[i + 1]
        tmid = (t0 + t1) / 2.0
        if tmid < INTRO_CUTOFF_S:
            phase, conf, evid = _vote(tmid, triggers)
            if phase not in ("Night", "Day", "Nomination", "Execution") or conf < 0.4:
                phase, conf, evid = "Intro", 0.9, "before intro cutoff"
        else:
            phase, conf, evid = _vote(tmid, triggers)
        result.append((t0, t1, phase, conf, evid))
    return result


def _forward_fill(intervals: list[tuple[float, float, str, float, str]],
                  ) -> list[tuple[float, float, str, float, str]]:
    out = []
    last_phase = "Day"
    last_t     = 0.0
    for s, e, ph, cf, ev in intervals:
        if ph == "Unknown":
            age   = s - last_t
            decayed = max(0.05, 0.6 - age * CONFIDENCE_DECAY_S)
            out.append((s, e, last_phase, round(decayed, 3),
                        f"inferred from previous {last_phase}"))
        else:
            last_phase = ph
            last_t     = s
            out.append((s, e, ph, cf, ev))
    return out


def _merge_short(intervals: list[tuple[float, float, str, float, str]],
                 min_s: float,
                 ) -> list[tuple[float, float, str, float, str]]:
    """Absorb islands shorter than min_s into their longer neighbour."""
    changed = True
    result  = list(intervals)
    while changed:
        changed = False
        merged: list[tuple] = []
        i = 0
        while i < len(result):
            s, e, ph, cf, ev = result[i]
            if (e - s) < min_s and len(result) > 1:
                prev_dur = (merged[-1][1] - merged[-1][0]) if merged else -1
                next_dur = ((result[i+1][1] - result[i+1][0])
                            if i + 1 < len(result) else -1)
                if merged and prev_dur >= next_dur:
                    ps, pe, pph, pcf, pev = merged.pop()
                    merged.append((ps, e, pph, pcf, pev))
                elif i + 1 < len(result):
                    ns, ne, nph, ncf, nev = result[i + 1]
                    merged.append((s, ne, nph, ncf, nev))
                    i += 1
                else:
                    merged.append(result[i])
                changed = True
            else:
                merged.append(result[i])
            i += 1
        result = merged
    return result


def _collapse(intervals: list[tuple[float, float, str, float, str]],
              ) -> list[tuple[float, float, str, float, str]]:
    """Merge consecutive same-phase intervals."""
    if not intervals:
        return intervals
    out = [intervals[0]]
    for s, e, ph, cf, ev in intervals[1:]:
        ps, pe, pph, pcf, pev = out[-1]
        if ph == pph:
            out[-1] = (ps, e, pph, round((pcf + cf) / 2, 3), pev)
        else:
            out.append((s, e, ph, cf, ev))
    return out


# ── Entry point ────────────────────────────────────────────────────────────────

def main(video_id: str, force: bool = False) -> None:
    out_dir  = Path("outputs") / video_id
    out_path = out_dir / "phase_labels.csv"

    if out_path.exists() and not force:
        print(f"  [SKIP] phase_labels.csv already exists for {video_id}")
        return

    print(f"\n=== detect_phases.py — {video_id} ===")

    rows     = _load_segments(out_dir)
    if not rows:
        print("  [SKIP] No segments found.")
        return

    total_s  = max(float(r["end"]) for r in rows)
    print(f"  Duration: {total_s:.0f}s ({total_s / 60:.1f} min)")

    st_id    = _find_storyteller(rows)
    print(f"  Storyteller speaker: {st_id}")

    triggers = _collect_triggers(rows, st_id)
    print(f"  Keyword triggers: {len(triggers)}")
    if triggers:
        phase_hits: Counter = Counter(t[1] for t in triggers)
        print(f"  Trigger breakdown: {dict(phase_hits)}")

    ivs  = _build_intervals(rows, triggers)
    ivs  = _forward_fill(ivs)
    ivs  = _merge_short(ivs, MIN_PHASE_S)
    ivs  = _collapse(ivs)

    phase_summary: Counter = Counter(iv[2] for iv in ivs)
    print(f"  Output intervals: {len(ivs)}  phases: {dict(phase_summary)}")

    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["start","end","phase","confidence","evidence"])
        w.writeheader()
        for s, e, ph, cf, ev in ivs:
            w.writerow({"start": round(s, 3), "end": round(e, 3),
                        "phase": ph, "confidence": cf,
                        "evidence": ev})

    print(f"\n  Wrote {len(ivs)} rows -> {out_path}")
    print("\n  Sample (first 10 rows):")
    for iv in ivs[:10]:
        s, e, ph, cf, ev = iv
        print(f"    {s:8.1f}s - {e:8.1f}s  {ph:12s}  conf={cf:.2f}  \"{ev[:55]}\"")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Game-phase boundary detection (N2)")
    ap.add_argument("video_id", help="YouTube video ID")
    ap.add_argument("--force", action="store_true", help="Overwrite existing output")
    args = ap.parse_args()
    main(args.video_id, force=args.force)
