"""build_execution_episodes.py — N6: Execution-episode aggregation.

Links N2 VoteSequence windows with N5 execution-context events to produce
one row per execution episode describing likely targets and who drove each
execution conversation.

This is NOT full vote reconstruction.  It does not attempt to identify every
voter.  It aggregates public signals (N5) around structured vote windows (N2)
to answer:
  - who was the likely execution target in each vote window?
  - who nominated / applied kill pressure / defended the target?
  - does execution_result_narration confirm the target after the vote window?

Core algorithm per VoteSequence:
  1. Gather N5 nomination / kill-pressure / opposition events within
     [vote_start - _LOOKBACK_S, vote_end].
  2. Gather N5 execution_result_narration events within
     [vote_start, vote_end + _RESULT_LOOKAHEAD_S].
  3. Score each candidate target from step-1 events using weighted counts.
  4. Resolve the likely target only if one candidate is unambiguously ahead
     (ratio >= _AMBIGUITY_RATIO AND gap >= _AMBIGUITY_GAP); otherwise leave blank.
  5. Record the highest-confidence result narration player (step 2) separately.
  6. Compute episode confidence from evidence quality.

Conservative policy:
  - If candidate evidence is ambiguous, likely_target_player = "".
  - All Day-phase VoteSequence events produce an episode (even with no N5 evidence),
    so coverage is always visible.  Zero-evidence episodes get low confidence.
  - VoteSequence events that fall in Night phase (frame-scan misdetection) are skipped.
  - Prefer false negatives over false positives.

Output:
    outputs/<id>/execution_episodes.csv

Fields:
    timestamp_start      — earliest contributing event or vote_window_start
    timestamp_end        — latest contributing event or vote_window_end
    phase                — always "Day" (Night-phase votes are filtered out)
    round                — game round from day_events.csv
    vote_window_start    — VoteSequence event start
    vote_window_end      — VoteSequence event end
    likely_target_player — inferred from nomination/kill-pressure signals, or ""
    likely_target_role_if_any — from roster
    evidence_count       — number of N5 events (excluding result narration)
    supporting_event_types — pipe-separated event types seen
    confidence           — episode-level confidence (see _compute_episode_confidence)
    nomination_speakers  — pipe-separated player names who made nominations
    pressure_speakers    — pipe-separated player names who applied kill pressure
    opposition_speakers  — pipe-separated player names who opposed
    execution_result_player — from execution_result_narration after vote window
    matched_vote_sequence   — always "true" (all episodes derive from VoteSequence)
    source_timestamps    — pipe-separated N5 event timestamps contributing to this episode

Usage:
    python build_execution_episodes.py <video_id>
    python build_execution_episodes.py <video_id> --force
    python build_execution_episodes.py --all [--force]
"""

from __future__ import annotations

import argparse
import csv
import json
import traceback
from collections import defaultdict
from pathlib import Path

from pipeline_utils import load_player_aliases, load_roster

# ── Paths ──────────────────────────────────────────────────────────────────────

OUTPUTS_DIR = Path("outputs")
PLAYLIST_PATH = Path("playlist.json")

# ── Constants ──────────────────────────────────────────────────────────────────

# Pre-vote lookback: gather N5 events this many seconds *before* vote_window_start.
# Captures discussion that leads to the nomination.
_LOOKBACK_S: float = 300.0

# Post-vote result window: look for execution_result_narration this many seconds
# *after* vote_window_end.  Covers the execution ceremony ("last words", "goodbye").
_RESULT_LOOKAHEAD_S: float = 300.0

# Candidate scoring weights per N5 event type.
_NOMINATION_WEIGHT: float = 1.00   # explicit nomination → strongest signal
_KILL_PRESSURE_WEIGHT: float = 0.70 # public kill pressure → medium
_OPPOSITION_WEIGHT: float = 0.30   # opposition → still indicates who is being discussed

# Disambiguation: top candidate must exceed second by at least this ratio AND gap.
# If not, likely_target_player is left blank (ambiguous).
_AMBIGUITY_RATIO: float = 1.4
_AMBIGUITY_GAP: float = 0.20

# Minimum episode confidence to emit.  Very low — we want all VoteSequences visible
# even when N5 has no data for them.
_MIN_EPISODE_CONF: float = 0.20

# Output fieldnames (per task spec + implementation).
_FIELDNAMES = [
    "timestamp_start",
    "timestamp_end",
    "phase",
    "round",
    "vote_window_start",
    "vote_window_end",
    "likely_target_player",
    "likely_target_role_if_any",
    "evidence_count",
    "supporting_event_types",
    "confidence",
    "nomination_speakers",
    "pressure_speakers",
    "opposition_speakers",
    "execution_result_player",
    "matched_vote_sequence",
    "source_timestamps",
]

# ── Data loaders ───────────────────────────────────────────────────────────────


def _load_day_events(out_dir: Path) -> list[dict]:
    p = out_dir / "day_events.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _load_execution_context_events(out_dir: Path) -> list[dict]:
    """Load execution_context_events.csv (N5 output), sorted ascending by timestamp."""
    p = out_dir / "execution_context_events.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        event_rows = list(csv.DictReader(fh))
    event_rows.sort(key=lambda r: float(r.get("timestamp_start", 0)))
    return event_rows


def _load_phase_labels(out_dir: Path) -> list[dict]:
    p = out_dir / "phase_labels.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# ── Phase helper ───────────────────────────────────────────────────────────────


def _phase_at(t: float, labels: list[dict]) -> str:
    """Return the broad phase label at timestamp t."""
    result = "Unknown"
    for row in labels:
        try:
            if float(row["start"]) <= t:
                result = row.get("phase", "Unknown")
            else:
                break
        except (KeyError, ValueError):
            pass
    return result


# ── Candidate scoring ──────────────────────────────────────────────────────────


def _score_candidates(pre_vote_context_events: list[dict]) -> dict[str, float]:
    """Accumulate weighted scores per target player from pre-vote execution context events."""
    candidate_scores: dict[str, float] = defaultdict(float)
    for context_event in pre_vote_context_events:
        target = context_event.get("target_player", "").strip()
        if not target:
            continue
        event_type = context_event.get("event_type", "")
        event_conf = float(context_event.get("confidence", 0.0))
        if event_type == "nomination_reference":
            candidate_scores[target] += event_conf * _NOMINATION_WEIGHT
        elif event_type == "public_kill_pressure":
            candidate_scores[target] += event_conf * _KILL_PRESSURE_WEIGHT
        elif event_type == "execution_opposition":
            # Opposition is evidence the person is being discussed, not strong target signal.
            candidate_scores[target] += event_conf * _OPPOSITION_WEIGHT
    return dict(candidate_scores)


def _resolve_target(scores: dict[str, float]) -> str:
    """Return the unambiguous top-scored candidate, or "" if ambiguous.

    Requires top score to exceed second by _AMBIGUITY_RATIO AND _AMBIGUITY_GAP.
    With only one candidate, returns that candidate unconditionally.
    """
    if not scores:
        return ""
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    if len(ranked) == 1:
        return ranked[0][0]
    top_name, top_score = ranked[0]
    _, second_score = ranked[1]
    if (
        top_score >= second_score * _AMBIGUITY_RATIO
        and (top_score - second_score) >= _AMBIGUITY_GAP
    ):
        return top_name
    return ""  # ambiguous — prefer false negative


# ── Episode confidence ─────────────────────────────────────────────────────────


def _compute_episode_confidence(
    vote_seq_conf: float,
    pre_vote_events: list[dict],
    result_player: str,
    likely_target: str,
) -> float:
    """Episode-level confidence: how well characterised is this execution episode?

    Components (additive, capped at 1.0):
      base          = vote_seq_conf * 0.40  (VoteSequence frame-scan = 1.0 → 0.40)
      nomination    = +0.25 if any nomination_reference in pre-vote events
      kill_pressure = +0.10 if any public_kill_pressure in pre-vote events
      result        = +0.15 if execution_result_narration exists after vote window
      confirmation  = +0.10 if likely_target matches execution_result_player
    """
    conf = vote_seq_conf * 0.40
    has_nom = any(e["event_type"] == "nomination_reference" for e in pre_vote_events)
    has_prs = any(e["event_type"] == "public_kill_pressure" for e in pre_vote_events)
    if has_nom:
        conf += 0.25
    if has_prs:
        conf += 0.10
    if result_player:
        conf += 0.15
    if likely_target and result_player and likely_target.lower() == result_player.lower():
        conf += 0.10
    return round(min(1.0, conf), 3)


# ── Per-video extraction ───────────────────────────────────────────────────────


def extract(video_id: str, force: bool = False) -> bool:
    """Build execution_episodes.csv for one video.

    Returns True on success (output written), False on skip.
    """
    out_dir = OUTPUTS_DIR / video_id
    out_path = out_dir / "execution_episodes.csv"

    if out_path.exists() and not force:
        return False

    aliases = load_player_aliases()

    day_events = _load_day_events(out_dir)
    if not day_events:
        print(f"  SKIP {video_id}: no day_events.csv")
        return False

    execution_context_events = _load_execution_context_events(out_dir)
    phase_labels = _load_phase_labels(out_dir)
    roster = load_roster(out_dir, aliases)

    # Only VoteSequence rows — skip NominationStart, DayEnd, ExecutionAnnouncement, etc.
    vote_seqs = [e for e in day_events if e.get("event") == "VoteSequence"]
    if not vote_seqs:
        print(f"  SKIP {video_id}: no VoteSequence events in day_events.csv")
        return False

    # Filter to Day-phase only.
    # Include VoteSequences where the START falls in Day (vote debate started in Day),
    # OR where the END falls in Day (execution ceremony completes in Day, straddling
    # a Night->Day transition).
    # Fully Night-phase VoteSequences (both start and end in Night) are skipped
    # as frame-scan misdetections or unmodelled Night-vote patterns.
    day_vote_seqs: list[dict] = []
    for vs in vote_seqs:
        vs_start = float(vs["start"])
        vs_end = float(vs["end"])
        phase_start = _phase_at(vs_start, phase_labels)
        phase_end = _phase_at(vs_end, phase_labels)
        if phase_start == "Day" or phase_end == "Day":
            day_vote_seqs.append(vs)
        else:
            print(
                f"  NOTE: skipping VoteSequence t={vs_start:.0f}-{vs_end:.0f}s "
                f"(start={phase_start}, end={phase_end}, round={vs.get('round')}) — fully outside Day"
            )

    if not day_vote_seqs:
        print(f"  SKIP {video_id}: no Day-phase VoteSequence events after filtering")
        return False

    print(
        f"  VoteSequences (Day): {len(day_vote_seqs)}"
        f"  |  Execution context events: {len(execution_context_events)}"
        f"  |  Roster players: {len(roster)}"
    )

    execution_episode_rows: list[dict] = []

    for vote_sequence in day_vote_seqs:
        vs_start = float(vote_sequence["start"])
        vs_end = float(vote_sequence["end"])
        round_num = vote_sequence.get("round", "")
        vote_sequence_conf = float(vote_sequence.get("confidence", 1.0))

        # Gather window bounds.
        pre_vote_window_start = vs_start - _LOOKBACK_S
        result_window_end = vs_end + _RESULT_LOOKAHEAD_S

        # Partition execution context events into two groups:
        #   pre_vote_context_events  — nomination / kill-pressure / opposition in lookback window
        #   result_narration_events  — result narration from vote_start onwards
        pre_vote_context_events: list[dict] = []
        result_narration_events: list[dict] = []

        for context_event in execution_context_events:
            event_time_s = float(context_event.get("timestamp_start", 0))
            event_type = context_event.get("event_type", "")
            if event_type == "execution_result_narration":
                # Accept result narration from vote_start through result lookahead.
                if vs_start <= event_time_s <= result_window_end:
                    result_narration_events.append(context_event)
            else:
                # Accept nomination / pressure / opposition within lookback + vote window.
                if pre_vote_window_start <= event_time_s <= vs_end:
                    pre_vote_context_events.append(context_event)

        all_contributing_events = pre_vote_context_events + result_narration_events

        # Score candidates and resolve likely target.
        candidate_scores = _score_candidates(pre_vote_context_events)
        likely_target = _resolve_target(candidate_scores)

        # Best result narration: prefer post-vote events (execution ceremony after vote).
        # Fall back to within-vote result narration (ST announces during the vote itself).
        post_vote_result_events = [
            e for e in result_narration_events if float(e["timestamp_start"]) > vs_end
        ]
        in_vote_result_events = [
            e for e in result_narration_events if float(e["timestamp_start"]) <= vs_end
        ]
        candidate_result_events = post_vote_result_events if post_vote_result_events else in_vote_result_events
        execution_result_player = ""
        if candidate_result_events:
            best_result_narration = max(candidate_result_events, key=lambda e: float(e.get("confidence", 0)))
            execution_result_player = best_result_narration.get("target_player", "")

        # Episode temporal bounds: expand to cover earliest/latest contributing event.
        if all_contributing_events:
            earliest_event_t = min(float(e["timestamp_start"]) for e in all_contributing_events)
            latest_event_t = max(float(e["timestamp_start"]) for e in all_contributing_events)
            episode_start = min(vs_start, earliest_event_t)
            episode_end = max(vs_end, latest_event_t)
        else:
            episode_start = vs_start
            episode_end = vs_end

        # Compute confidence.
        episode_conf = _compute_episode_confidence(
            vote_sequence_conf, pre_vote_context_events, execution_result_player, likely_target
        )
        if episode_conf < _MIN_EPISODE_CONF:
            continue

        # Build per-type speaker sets from pre-vote events.
        nomination_speakers: set[str] = set()
        pressure_speakers: set[str] = set()
        opposition_speakers: set[str] = set()
        for context_event in pre_vote_context_events:
            speaker_name = (context_event.get("source_player_name") or context_event.get("speaker", "")).strip()
            event_type = context_event.get("event_type", "")
            if event_type == "nomination_reference" and speaker_name:
                nomination_speakers.add(speaker_name)
            elif event_type == "public_kill_pressure" and speaker_name:
                pressure_speakers.add(speaker_name)
            elif event_type == "execution_opposition" and speaker_name:
                opposition_speakers.add(speaker_name)

        event_types_seen = sorted(set(e["event_type"] for e in pre_vote_context_events))

        # contributing_timestamps: all contributing events sorted.
        contributing_timestamps = sorted(
            set(str(round(float(e["timestamp_start"]), 2)) for e in all_contributing_events)
        )

        execution_episode_rows.append({
            "timestamp_start": round(episode_start, 3),
            "timestamp_end": round(episode_end, 3),
            "phase": "Day",
            "round": round_num,
            "vote_window_start": round(vs_start, 3),
            "vote_window_end": round(vs_end, 3),
            "likely_target_player": likely_target,
            "likely_target_role_if_any": roster.get(likely_target, "") if likely_target else "",
            "evidence_count": len(pre_vote_context_events),
            "supporting_event_types": "|".join(event_types_seen),
            "confidence": episode_conf,
            "nomination_speakers": "|".join(sorted(nomination_speakers)),
            "pressure_speakers": "|".join(sorted(pressure_speakers)),
            "opposition_speakers": "|".join(sorted(opposition_speakers)),
            "execution_result_player": execution_result_player,
            "matched_vote_sequence": "true",
            "source_timestamps": "|".join(contributing_timestamps),
        })

    # Sort by vote window.
    execution_episode_rows.sort(key=lambda episode: episode["vote_window_start"])

    # Write output — always, even if empty, so downstream can detect N6 coverage.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(execution_episode_rows)

    # Summary.
    resolved_count = sum(1 for ep in execution_episode_rows if ep["likely_target_player"])
    result_confirmed_count = sum(1 for ep in execution_episode_rows if ep["execution_result_player"])
    confirmed_match_count = sum(
        1 for ep in execution_episode_rows
        if ep["likely_target_player"]
        and ep["execution_result_player"]
        and ep["likely_target_player"].lower() == ep["execution_result_player"].lower()
    )
    print(f"  Wrote {len(execution_episode_rows)} episodes -> {out_path.name}")
    print(f"    Resolved targets: {resolved_count}/{len(execution_episode_rows)}")
    print(f"    Result confirmed: {result_confirmed_count}/{len(execution_episode_rows)}")
    if confirmed_match_count:
        print(f"    Confirmed matches (target == result): {confirmed_match_count}")

    if execution_episode_rows:
        print("  Episodes:")
        for episode in execution_episode_rows:
            target_display = episode["likely_target_player"] or "(unresolved)"
            result_display = episode["execution_result_player"] or "(no result)"
            print(
                f"    round={episode['round']}  "
                f"vote={episode['vote_window_start']:.0f}-{episode['vote_window_end']:.0f}s  "
                f"target={target_display}  result={result_display}  "
                f"evid={episode['evidence_count']}  conf={episode['confidence']}"
            )

    return True


# ── Batch helpers ──────────────────────────────────────────────────────────────


def _all_video_ids() -> list[str]:
    """Return all analyzed, public, non-skip video IDs with day_events.csv."""
    if not PLAYLIST_PATH.exists():
        return []
    try:
        data = json.loads(PLAYLIST_PATH.read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else data.get("entries", [])
        return [
            e["id"]
            for e in entries
            if not e.get("skip")
            and not e.get("members_only")
            and e.get("status") == "analyzed"
            and (OUTPUTS_DIR / e["id"] / "day_events.csv").exists()
        ]
    except Exception:
        return []


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="N6: Build execution episodes from N2 VoteSequence + N5 events."
    )
    parser.add_argument("video_id", nargs="?", help="Single video ID to process")
    parser.add_argument("--all", action="store_true", help="Process all eligible videos")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output")
    args = parser.parse_args()

    if args.all:
        ids = _all_video_ids()
        print(f"Processing {len(ids)} videos ...")
        done = skipped = errors = 0
        for vid in ids:
            print(f"\n=== {vid} ===")
            try:
                result = extract(vid, force=args.force)
                if result:
                    done += 1
                else:
                    skipped += 1
            except Exception as exc:
                print(f"  ERROR: {exc}")
                traceback.print_exc()
                errors += 1
        print(f"\nDone: {done} extracted, {skipped} skipped, {errors} errors.")
    elif args.video_id:
        print(f"=== build_execution_episodes.py  {args.video_id} ===")
        try:
            extract(args.video_id, force=args.force)
        except Exception as exc:
            print(f"ERROR: {exc}")
            raise
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
