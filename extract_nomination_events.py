"""extract_nomination_events.py — N9: Nomination event extraction.

Extracts individual nomination events within each day's vote sequence using:
  1. VoteSequence windows from day_events.csv (authoritative timing, via frame_scan)
  2. Transcript speech acts ("I nominate X") from segments_patched.csv
  3. N5 nomination_reference events from execution_context_events.csv

Evidence class hierarchy:
  fused                   — transcript speech act and N5 agree on nominee (highest trust)
  n5_nomination_reference — N5 nomination_reference event; nominee already resolved
  transcript_speech_act   — direct "I nominate X" / "nominating X" in transcript

Design notes:
  - VoteSequence windows are NOT reconstructed here; they are read from day_events.csv,
    which is already produced by detect_phases.py using frame_scan vote-table detection.
    This script EXTENDS that existing path rather than duplicating it.
  - Nominee names from transcript are resolved via fuzzy match against the known player
    roster. No Minecraft username mapping is attempted (those names appear in the vote
    table pixel-font OCR, which is unreliable; the transcript uses spoken names).
  - One row is written per detected nomination, not per window. A long window may
    contain several nominations (e.g., the round-2 window at 2115–2715s).
  - Conservative: _MIN_CONF = 0.55. Prefer false negatives.

Nomination window search:
  Transcript is searched in [vote_window_start - _PRE_WINDOW_S, vote_window_end].
  A _PRE_WINDOW_S of 90s is used because nominators often speak just before the
  vote table appears (the ST opens nominations, players discuss, then vote table pops).
  N5 events are searched in the same expanded window.

Output:
    outputs/<id>/nomination_events.csv

Usage:
    python extract_nomination_events.py <video_id>
    python extract_nomination_events.py <video_id> --force
    python extract_nomination_events.py --all [--force]
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import traceback
from pathlib import Path

from pipeline_utils import load_player_aliases, resolve_player_name

# ── Paths ───────────────────────────────────────────────────────────────────────

OUTPUTS_DIR = Path("outputs")
PLAYLIST_PATH = Path("playlist.json")

# ── Constants ───────────────────────────────────────────────────────────────────

# Minimum confidence to emit a nomination row.
_MIN_CONF: float = 0.55

# How far before vote_window_start to look for transcript nominations.
# Nominations often happen during the inter-window discussion period, which can
# extend 200–300s before the vote table appears.  This value matches N6's
# _LOOKBACK_S so both layers see the same pre-vote context.
# The effective lower bound is clamped to max(prev_window_end, win_start - _PRE_WINDOW_S)
# inside the extraction loop, so nominations already assigned to a previous window
# are never double-counted.
_PRE_WINDOW_S: float = 300.0

# Fuzzy name matching threshold.
_FUZZY_THRESHOLD: float = 0.72

# Minimum token length for fuzzy matching (avoid matching single-char noise).
_FUZZY_MIN_LEN: int = 3

# Confidence assigned to each evidence tier.
_CONF_FUSED: float = 0.90
_CONF_N5: float = 0.82
_CONF_TRANSCRIPT: float = 0.70

# When a transcript nominee is resolved via an exact match (after normalization)
# vs. a fuzzy match, scale confidence.
_FUZZY_CONF_SCALE: float = 0.88

# ── Output schema ────────────────────────────────────────────────────────────────

_FIELDNAMES = [
    "round",
    "vote_window_start",
    "vote_window_end",
    "timestamp_start",
    "nominee",
    "nominator",
    "evidence_source",
    "confidence",
    "source_text",
]

# ── Regex patterns ───────────────────────────────────────────────────────────────

# Direct "I nominate X" speech act by the nominator themselves.
# Captures the nominee name token(s).
# Handles: "I nominate X", "I'm going to nominate X", "I'll nominate X",
#          "I want to nominate X", "I would like to nominate X", "I'd like to nominate X",
#          "I am going to nominate X"
_SELF_NOMINATE = re.compile(
    r"\b(?:"
    r"I(?:'m| am| will|'ll)?(?:\s+gonna|\s+going\s+to)?"
    r"|I\s+(?:want\s+to|would\s+like\s+to|'d\s+like\s+to|am\s+going\s+to)"
    r")\s+nominate\s+([\w\s]+?)(?:\s*[,!.\"]|$)",
    re.I,
)

# Storyteller / observer announcement: "X is nominating Y" or "X has nominated Y".
_ST_ANNOUNCE = re.compile(
    r"\b(\w[\w\s]{1,20}?)\s+(?:is\s+|has\s+)?nominating\s+([\w\s]+?)(?:\s*[,!.\"]|$)",
    re.I,
)

# Shorter "I'll nominate X" variant.
_ILLL_NOMINATE = re.compile(
    r"\bI\'ll\s+nominate\s+([\w\s]+?)(?:\s*[,!.\"]|$)",
    re.I,
)

# ── Data loaders ─────────────────────────────────────────────────────────────────


def _load_vote_sequences(out_dir: Path) -> list[dict]:
    """Return VoteSequence rows from day_events.csv."""
    p = out_dir / "day_events.csv"
    if not p.exists():
        return []
    rows = []
    with p.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("event") == "VoteSequence":
                rows.append(row)
    return rows


def _load_segments(out_dir: Path) -> list[dict]:
    """Return all transcript segments (segments_patched.csv)."""
    p = out_dir / "segments_patched.csv"
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _load_n5_nominations(out_dir: Path) -> list[dict]:
    """Return nomination_reference events from execution_context_events.csv."""
    p = out_dir / "execution_context_events.csv"
    if not p.exists():
        return []
    rows = []
    with p.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("event_type") == "nomination_reference":
                rows.append(row)
    return rows


def _build_speaker_map(out_dir: Path, aliases: dict) -> dict[str, str]:
    """Return {speaker_id: canonical_player_name} from roster_overrides.json."""
    speaker_map: dict[str, str] = {}
    overrides_path = out_dir / "roster_overrides.json"
    if overrides_path.exists():
        try:
            ov = json.loads(overrides_path.read_text(encoding="utf-8"))
            for speaker_id, info in ov.items():
                raw = info.get("name", "").strip()
                if raw and raw.lower() not in ("storyteller", speaker_id.lower()):
                    speaker_map[speaker_id] = resolve_player_name(raw, aliases)
        except Exception:
            pass
    # Fallback: intro_roster.json (less reliable for speaker ID binding)
    if not speaker_map:
        intro_path = out_dir / "intro_roster.json"
        if intro_path.exists():
            try:
                data = json.loads(intro_path.read_text(encoding="utf-8"))
                for player in data.get("players", []):
                    sid = player.get("speaker_id", "").strip()
                    raw = player.get("name", "").strip()
                    if sid and raw:
                        speaker_map[sid] = resolve_player_name(raw, aliases)
            except Exception:
                pass
    return speaker_map


def _build_player_roster(out_dir: Path, aliases: dict) -> list[str]:
    """Return list of canonical player names for this video."""
    names: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        c = resolve_player_name(raw.strip(), aliases)
        if c and c not in seen:
            seen.add(c)
            names.append(c)

    intro_path = out_dir / "intro_roster.json"
    if intro_path.exists():
        try:
            data = json.loads(intro_path.read_text(encoding="utf-8"))
            for p in data.get("players", []):
                _add(p.get("name", ""))
        except Exception:
            pass

    overrides_path = out_dir / "roster_overrides.json"
    if overrides_path.exists():
        try:
            ov = json.loads(overrides_path.read_text(encoding="utf-8"))
            for info in ov.values():
                raw = info.get("name", "").strip()
                if raw and raw.lower() != "storyteller":
                    _add(raw)
        except Exception:
            pass

    return names


# ── Name resolution ──────────────────────────────────────────────────────────────


def _fuzzy_match_player(name_token: str, roster: list[str]) -> tuple[str, float] | None:
    """Fuzzy-match name_token against roster.  Returns (canonical, score) or None."""
    token = name_token.strip().lower()
    if len(token) < _FUZZY_MIN_LEN:
        return None

    best_name = ""
    best_score = 0.0
    for canonical in roster:
        # Check against each word in canonical name (first name, last name).
        for part in canonical.lower().split():
            if len(part) < _FUZZY_MIN_LEN:
                continue
            score = difflib.SequenceMatcher(None, token, part).ratio()
            if score > best_score:
                best_score = score
                best_name = canonical
        # Also check the full canonical name.
        score = difflib.SequenceMatcher(None, token, canonical.lower()).ratio()
        if score > best_score:
            best_score = score
            best_name = canonical

    if best_score >= _FUZZY_THRESHOLD and best_name:
        return (best_name, best_score)
    return None


def _extract_name_token(raw: str) -> str:
    """Trim a raw regex-captured name token to the first 1–2 words."""
    # Drop trailing noise (articles, conjunctions, etc.)
    words = raw.strip().split()
    stop = {"the", "a", "an", "and", "or", "but", "because", "since", "as", "to"}
    kept: list[str] = []
    for w in words[:3]:
        if w.lower() in stop and kept:
            break
        kept.append(w)
    return " ".join(kept)


# ── Transcript-based nomination extraction ────────────────────────────────────────


def _transcript_nominations(
    segments: list[dict],
    window_start: float,
    window_end: float,
    speaker_map: dict[str, str],
    roster: list[str],
    aliases: dict,
    search_start: float | None = None,
) -> list[dict]:
    """Extract nomination speech acts from transcript segments within a window.

    search_start: explicit lower-bound timestamp for the search (seconds).
        If None, defaults to window_start - _PRE_WINDOW_S.
        Pass the clamped value from the extraction loop to avoid double-counting
        nominations already assigned to a previous vote window.

    Returns a list of candidate dicts:
        {timestamp_start, nominee, nominator, evidence_source, confidence, source_text}
    """
    if search_start is None:
        search_start = window_start - _PRE_WINDOW_S
    results: list[dict] = []

    for seg in segments:
        try:
            t = float(seg.get("start", 0))
        except ValueError:
            continue
        if t < search_start or t > window_end:
            continue

        text = seg.get("text", "").strip()
        speaker_id = seg.get("speaker", "")
        nominator_canonical = speaker_map.get(speaker_id, "")

        # Pattern 1: "I nominate X" / "I'll nominate X" / "I'm gonna nominate X"
        for pat in (_SELF_NOMINATE, _ILLL_NOMINATE):
            m = pat.search(text)
            if m:
                raw_nominee = _extract_name_token(m.group(1))
                match_result = _fuzzy_match_player(raw_nominee, roster)
                if match_result:
                    nominee_canonical, match_score = match_result
                    conf = _CONF_TRANSCRIPT * (1.0 if match_score >= 0.90 else _FUZZY_CONF_SCALE)
                    results.append(
                        {
                            "timestamp_start": t,
                            "nominee": nominee_canonical,
                            "nominator": nominator_canonical,
                            "evidence_source": "transcript_speech_act",
                            "confidence": round(conf, 3),
                            "source_text": text[:120],
                        }
                    )

        # Pattern 2: "X is nominating Y" / "X has nominated Y" (ST announcement)
        m2 = _ST_ANNOUNCE.search(text)
        if m2:
            raw_nominator = _extract_name_token(m2.group(1))
            raw_nominee = _extract_name_token(m2.group(2))
            nr = _fuzzy_match_player(raw_nominator, roster)
            nm = _fuzzy_match_player(raw_nominee, roster)
            if nm:
                nominee_canonical, nm_score = nm
                # Prefer extracted nominator over speaker_id for ST-announcement lines.
                resolved_nominator = nr[0] if nr else nominator_canonical
                conf = _CONF_TRANSCRIPT * (1.0 if nm_score >= 0.90 else _FUZZY_CONF_SCALE)
                results.append(
                    {
                        "timestamp_start": t,
                        "nominee": nominee_canonical,
                        "nominator": resolved_nominator,
                        "evidence_source": "transcript_speech_act",
                        "confidence": round(conf, 3),
                        "source_text": text[:120],
                    }
                )

    return results


# ── Deduplication ────────────────────────────────────────────────────────────────

_DEDUP_WINDOW_S = 30.0  # Merge transcript hits within 30s of each other for same nominee.


def _dedup_transcript_hits(hits: list[dict]) -> list[dict]:
    """Collapse multiple transcript hits for the same nominee within _DEDUP_WINDOW_S.

    Keeps the highest-confidence hit per nominee+time-cluster.
    """
    if not hits:
        return []

    # Sort by nominee then timestamp.
    hits = sorted(hits, key=lambda h: (h["nominee"].lower(), h["timestamp_start"]))
    merged: list[dict] = []
    i = 0
    while i < len(hits):
        cluster = [hits[i]]
        j = i + 1
        while j < len(hits):
            if (
                hits[j]["nominee"].lower() == hits[i]["nominee"].lower()
                and hits[j]["timestamp_start"] - hits[i]["timestamp_start"] <= _DEDUP_WINDOW_S
            ):
                cluster.append(hits[j])
                j += 1
            else:
                break
        # Keep best confidence in cluster.
        best = max(cluster, key=lambda h: h["confidence"])
        merged.append(best)
        i = j
    return merged


_SOURCE_PRIORITY = {"fused": 3, "n5_nomination_reference": 2, "transcript_speech_act": 1}


def _dedup_output_rows(rows: list[dict]) -> list[dict]:
    """Collapse duplicate nominations for the same nominee within _DEDUP_WINDOW_S.

    Keeps the highest-source-priority row (fused > n5 > transcript), then highest
    confidence as tiebreaker.
    """
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: (r["nominee"].lower(), r["timestamp_start"]))
    merged: list[dict] = []
    i = 0
    while i < len(rows):
        cluster = [rows[i]]
        j = i + 1
        while j < len(rows):
            same_nominee = rows[j]["nominee"].lower() == rows[i]["nominee"].lower()
            close_enough = rows[j]["timestamp_start"] - rows[i]["timestamp_start"] <= _DEDUP_WINDOW_S
            if same_nominee and close_enough:
                cluster.append(rows[j])
                j += 1
            else:
                break
        best = max(
            cluster,
            key=lambda r: (
                _SOURCE_PRIORITY.get(r["evidence_source"], 0),
                r["confidence"],
            ),
        )
        merged.append(best)
        i = j
    return merged


# ── Per-window nomination fusion ──────────────────────────────────────────────────


def _fuse_nominations(
    transcript_hits: list[dict],
    n5_events: list[dict],
) -> list[dict]:
    """Merge transcript and N5 evidence; return final nomination rows."""
    results: list[dict] = []
    used_n5: set[int] = set()

    for hit in transcript_hits:
        nominee_lower = hit["nominee"].lower()
        # Look for a matching N5 event for the same nominee.
        matched_n5 = None
        for idx, n5_ev in enumerate(n5_events):
            if idx in used_n5:
                continue
            n5_nominee = (n5_ev.get("target_player") or "").strip().lower()
            if n5_nominee == nominee_lower:
                matched_n5 = n5_ev
                used_n5.add(idx)
                break

        if matched_n5:
            # Fused: both sources agree.
            conf = _CONF_FUSED
            nominator = hit["nominator"] or (matched_n5.get("source_player_name") or "")
            source_text = matched_n5.get("source_text", hit["source_text"])[:120]
            results.append(
                {
                    "timestamp_start": hit["timestamp_start"],
                    "nominee": hit["nominee"],
                    "nominator": nominator,
                    "evidence_source": "fused",
                    "confidence": round(conf, 3),
                    "source_text": source_text,
                }
            )
        else:
            # Transcript only.
            results.append(hit)

    # Append any remaining N5 events not matched by transcript.
    for idx, n5_ev in enumerate(n5_events):
        if idx in used_n5:
            continue
        nominee = (n5_ev.get("target_player") or "").strip()
        nominator = (n5_ev.get("source_player_name") or "").strip()
        if not nominee:
            continue
        try:
            t = float(n5_ev.get("timestamp_start", 0))
        except ValueError:
            t = 0.0
        results.append(
            {
                "timestamp_start": t,
                "nominee": nominee,
                "nominator": nominator,
                "evidence_source": "n5_nomination_reference",
                "confidence": _CONF_N5,
                "source_text": (n5_ev.get("source_text") or "")[:120],
            }
        )

    # Filter by minimum confidence.
    return [r for r in results if r["confidence"] >= _MIN_CONF]


# ── Per-video extraction ──────────────────────────────────────────────────────────


def extract(video_id: str, force: bool = False) -> bool:
    """Build nomination_events.csv for one video.

    Returns True on success, False on skip.
    """
    out_dir = OUTPUTS_DIR / video_id
    out_path = out_dir / "nomination_events.csv"

    if out_path.exists() and not force:
        return False

    aliases = load_player_aliases()
    vote_sequences = _load_vote_sequences(out_dir)
    if not vote_sequences:
        print(f"  SKIP {video_id}: no VoteSequence events in day_events.csv")
        return False

    segments = _load_segments(out_dir)
    n5_nominations = _load_n5_nominations(out_dir)
    speaker_map = _build_speaker_map(out_dir, aliases)
    roster = _build_player_roster(out_dir, aliases)

    print(
        f"  VoteSequences: {len(vote_sequences)}"
        f"  |  Transcript segs: {len(segments)}"
        f"  |  N5 nomination events: {len(n5_nominations)}"
        f"  |  Roster size: {len(roster)}"
    )

    output_rows: list[dict] = []

    # Sort ascending so we can track the previous window's end timestamp.
    # This lets us clamp the search lower bound and prevent nominations that
    # fall inside a completed vote window from being re-attributed to the next one.
    vote_sequences_sorted = sorted(
        vote_sequences, key=lambda vs: float(vs.get("start", 0))
    )
    prev_window_end: float | None = None

    for vs in vote_sequences_sorted:
        try:
            win_start = float(vs.get("start", 0))
            win_end = float(vs.get("end", win_start))
            rnd = vs.get("round", "")
        except ValueError:
            prev_window_end = None
            continue

        # Effective search lower bound: start of the extended pre-vote window,
        # clamped to never overlap with the previous window's time range.
        effective_search_start = win_start - _PRE_WINDOW_S
        if prev_window_end is not None:
            effective_search_start = max(prev_window_end, effective_search_start)

        # Collect N5 events within this window (with clamped pre-window).
        n5_in_window = [
            ev
            for ev in n5_nominations
            if effective_search_start <= float(ev.get("timestamp_start") or 0) <= win_end
        ]

        # Collect transcript nominations (pass the clamped lower bound explicitly).
        transcript_hits = _transcript_nominations(
            segments, win_start, win_end, speaker_map, roster, aliases,
            search_start=effective_search_start,
        )
        transcript_hits = _dedup_transcript_hits(transcript_hits)

        # Fuse.
        fused = _fuse_nominations(transcript_hits, n5_in_window)
        fused = _dedup_output_rows(fused)
        fused.sort(key=lambda r: r["timestamp_start"])

        for nom in fused:
            output_rows.append(
                {
                    "round": rnd,
                    "vote_window_start": win_start,
                    "vote_window_end": win_end,
                    "timestamp_start": nom["timestamp_start"],
                    "nominee": nom["nominee"],
                    "nominator": nom["nominator"],
                    "evidence_source": nom["evidence_source"],
                    "confidence": nom["confidence"],
                    "source_text": nom["source_text"],
                }
            )

        # Advance the window boundary for the next iteration.
        prev_window_end = win_end

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(output_rows)

    fused_count = sum(1 for r in output_rows if r["evidence_source"] == "fused")
    n5_only = sum(1 for r in output_rows if r["evidence_source"] == "n5_nomination_reference")
    transcript_only = sum(1 for r in output_rows if r["evidence_source"] == "transcript_speech_act")

    print(f"  Wrote {len(output_rows)} nomination rows -> {out_path.name}")
    print(f"    fused:                   {fused_count}")
    print(f"    n5_nomination_reference: {n5_only}")
    print(f"    transcript_speech_act:   {transcript_only}")

    if output_rows:
        print("  Examples:")
        for row in output_rows[:6]:
            print(
                f"    round={row['round']}  t={row['timestamp_start']:.0f}s"
                f"  nominee={row['nominee'] or '?'}"
                f"  nominator={row['nominator'] or '?'}"
                f"  src={row['evidence_source']}  conf={row['confidence']}"
            )

    return True


# ── Batch helpers ─────────────────────────────────────────────────────────────────


def _all_video_ids() -> list[str]:
    """Return all eligible video IDs (analyzed, public, with day_events.csv)."""
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


# ── CLI ────────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="N9: Extract nomination events from VoteSequence windows."
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
                if extract(vid, force=args.force):
                    done += 1
                else:
                    skipped += 1
            except Exception as exc:
                print(f"  ERROR: {exc}")
                traceback.print_exc()
                errors += 1
        print(f"\nDone: {done} extracted, {skipped} skipped, {errors} errors.")
    elif args.video_id:
        print(f"=== extract_nomination_events.py  {args.video_id} ===")
        try:
            extract(args.video_id, force=args.force)
        except Exception as exc:
            print(f"ERROR: {exc}")
            raise
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
