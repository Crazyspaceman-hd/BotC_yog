"""extract_player_status.py — N4 (player_status): Player status tracking and death-event extraction.

Determines when each player's game status changes (alive -> dead) and emits two
linked artifacts:

    player_status.csv   — one row per status transition (alive/dead/unknown)
    death_events.csv    — one row per death event (execution/night_death/uncertain_death)

Every death event corresponds to a player_status row transitioning to "dead."

Signal priority (highest to lowest):
    1. Explicit name+died pattern in transcript   ("Ravs has died")
    2. Execution-context pattern during/after a VoteSequence ("Goodbye Duncan, any last words")
    3. Night-death announcement at Day-phase start ("died last night / killed at night")
    4. Self-declaration ("I'm dead") when speaker identity is known
    frame_scan header_visible used to boost confidence when player list is on screen.

Event classification:
    execution      — death during/after VoteSequence in Day phase
    night_death    — death announced at Day start, attributed to night phase
    uncertain_death — death confirmed but cause ambiguous

Conservative policy: prefer false negatives over false positives.
Only emit a death event when evidence is explicit. Speculation is suppressed.

Usage:
    python extract_player_status.py <video_id>
    python extract_player_status.py <video_id> --force
    python extract_player_status.py --all [--force]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from pipeline_utils import load_player_aliases, resolve_player_name

# ── Paths ──────────────────────────────────────────────────────────────────────

OUTPUTS_DIR = Path("outputs")
PLAYLIST_PATH = Path("playlist.json")
DB_PATH = Path("botc.db")
ALIASES_PATH = Path("player_aliases.json")

# ── Constants ──────────────────────────────────────────────────────────────────

# How far back before a death announcement to look for a VoteSequence anchor (seconds).
# Short window: only credit a VoteSequence if it ended recently AND was in a Day phase.
# Prevents night-death day-start announcements from being misclassified as executions.
_VOTE_LOOKBACK_S: float = 120.0

# Day-start window: if death is announced within this many seconds of a Day
# phase start, treat it as a night-death morning announcement
_DAY_START_WINDOW_S: float = 600.0

# Maximum time between two death signals for the same player before treating
# them as duplicate firings of the same event
_DEDUP_WINDOW_S: float = 120.0

# Time window around the death timestamp to search for supporting header visibility
_HEADER_WINDOW_S: float = 60.0

# Confidence boost when player header is visible during the death announcement
_HEADER_CONF_BOOST: float = 0.05

# Confidence boost when night-target dialogue corroborates a confirmed death.
# Small — night-target alone is not proof of who was killed (protection can occur).
_NIGHT_TARGET_CONF_BOOST: float = 0.05

# Minimum confidence to emit a death event (anything below is silently dropped)
_MIN_CONF: float = 0.45

# ── Status vocabulary ──────────────────────────────────────────────────────────

STATUS_ALIVE = "alive"
STATUS_DEAD = "dead"
STATUS_UNKNOWN = "unknown"

# ── Death-signal patterns ──────────────────────────────────────────────────────
# Each pattern: (compiled_regex, confidence, event_type_hint)
# The player-name slot is filled dynamically per video from the roster.
# These patterns match text containing the player name + a death keyword.

# Pattern placeholders — {name} is substituted per-player at runtime.
# Ordered from most-explicit (highest confidence) to least.
_NAME_DIED_TEMPLATES: list[tuple[str, float, str]] = [
    # "<Name> has died"  /  "<Name>'s died"  /  "<Name> died"
    (r"\b{name}(?:'s?)?\s+(?:has\s+)?died\b", 0.90, "night_death"),
    # "<Name> is dead"  /  "<Name>'s dead"  /  "<Name> was dead"
    # Also catches possessive contraction "Nilesy's dead" (='s = is)
    (r"\b{name}(?:'s?)?\s+(?:is|was|are)\s+dead\b", 0.85, "uncertain_death"),
    (r"\b{name}'s\s+dead\b", 0.85, "uncertain_death"),
    # "killed <Name>"  /  "<Name> was killed"
    (r"\b{name}\s+(?:was|has been)\s+killed\b", 0.85, "night_death"),
    (r"\bkilled\s+{name}\b", 0.80, "night_death"),
    # "executed <Name>"  /  "<Name> was executed"  /  "<Name> has been executed"
    # Only past tense — "to execute X" is discussion, not a confirmed death fact.
    (r"\b{name}(?:'s?)?\s+(?:was|has been|is)\s+executed\b", 0.90, "execution"),
    (r"\bexecuted\s+{name}\b", 0.85, "execution"),
    # Goodbye / last words patterns — implies active execution ceremony.
    # Allow up to 30 chars between "Goodbye" and the name ("Goodbye, crowd around... Duncan").
    (r"\bgoodbye.{{0,30}}{name}\b", 0.75, "execution"),
    (r"\b{name}[,\s]+(?:any\s+)?last\s+words\b", 0.80, "execution"),
    # "last words <name>" / "last words, <name>"
    (r"\blast\s+words.{{0,20}}{name}\b", 0.80, "execution"),
    # Self-reference: "I'm dead" by known player (matched by speaker, not name)
    # handled separately below via speaker_map
]

# Pattern for night-death morning announcements (speaker-agnostic)
# The player name is extracted from the broader text.
_NIGHT_DEATH_TEMPLATES: list[tuple[str, float]] = [
    (r"\b{name}\s+died?\s+(at|in|during)\s+(the\s+)?night\b", 0.90),
    (r"\b{name}\s+(?:was\s+)?killed\s+(at|in|during)\s+(the\s+)?night\b", 0.90),
    (r"\blast\s+night.{{0,40}}{name}\b", 0.80),
    (r"\b{name}.{{0,20}}last\s+night\b", 0.80),
]

# Self-declaration: speaker says "I'm dead" / "I am dead" / "I was executed"
_SELF_DEAD_RE = re.compile(
    r"\b(?:i'?m|i\s+am|i\s+was|i\s+have\s+been)\s+"
    r"(?:dead|executed|killed|a\s+ghost|dead\s+already)\b",
    re.I,
)

# "X has used their ghost vote" — confirms X is already dead
_GHOST_VOTE_TEMPLATE = r"\b{name}(?:'s|s)?\s+(?:has\s+used|used)\s+(?:their|(?:his|her)\s+)?ghost\s+vote\b"

# Night-target dialogue: demon explicitly selecting a kill target during Night phase.
# Collected separately — NEVER alone create a death event.
# Only corroborate already-confirmed deaths (improve classification + confidence).
# Night-phase filter is applied in the scan loop; these patterns are too
# ambiguous to fire meaningfully during Day discussion.
_NIGHT_TARGET_TEMPLATES: list[tuple[str, float]] = [
    # "I'll kill [name]" / "I'm going to kill [name]" / "I'm gonna kill [name]"
    (r"\b(?:i'?ll|i\s+will|i'?m\s+(?:going\s+to|gonna))\s+kill\s+{name}\b", 0.65),
    # "I choose [name]" / "I pick [name]" / "I select [name]"  (direct form)
    (r"\bi\s+(?:choose|pick|select)\s+{name}\b", 0.60),
    # "I'll pick [name]" / "I'm gonna pick [name]" / "I'm going to choose [name]"
    (r"\b(?:i'?ll|i\s+will|i'?m\s+(?:going\s+to|gonna))\s+(?:pick|choose|select)\s+{name}\b", 0.60),
    # "I want to kill [name]"
    (r"\bi\s+want\s+to\s+kill\s+{name}\b", 0.65),
    # "[name] is my kill/target/pick"
    (r"\b{name}\s+is\s+my\s+(?:kill|target|pick)\b", 0.65),
    # "my kill/target/pick is [name]" / "my kill/target/pick [name]"
    (r"\bmy\s+(?:kill|target|pick)\s+(?:is\s+)?{name}\b", 0.65),
]

# Cross-segment lookahead: when demon says kill-intent WITHOUT naming the target
# in the same segment, the target name often appears in the immediately following
# segment (Whisper splits at natural speech pauses).
# Matches intent-only forms (no player name required in same segment).
_KILL_INTENT_RE = re.compile(
    r"\b(?:i'?ll|i\s+will|i'?m\s+(?:going\s+to|gonna)|i\s+want\s+to)\s+kill\b",
    re.I,
)

# How far ahead (seconds) to scan for the target name after a kill-intent segment.
_KILL_INTENT_LOOKAHEAD_S: float = 15.0


# ── Data loading helpers ───────────────────────────────────────────────────────


def _load_segments(out_dir: Path) -> list[dict]:
    for fname in ("segments_patched.csv", "segments_consistent.csv", "segments.csv"):
        p = out_dir / fname
        if p.exists():
            with p.open(encoding="utf-8", newline="") as fh:
                return list(csv.DictReader(fh))
    return []


def _load_roster(out_dir: Path, aliases: dict) -> dict[str, str]:
    """Return {canonical_name: actual_role} from intro_roster + overrides."""
    roster: dict[str, str] = {}
    intro = out_dir / "intro_roster.json"
    if intro.exists():
        try:
            data = json.loads(intro.read_text(encoding="utf-8"))
            for p in data.get("players", []):
                raw_name = p.get("name", "").strip()
                role = p.get("actual_role", "").strip()
                if raw_name:
                    canon = resolve_player_name(raw_name, aliases)
                    roster[canon] = role
        except Exception:
            pass
    overrides = out_dir / "roster_overrides.json"
    if overrides.exists():
        try:
            ov = json.loads(overrides.read_text(encoding="utf-8"))
            for spk, info in ov.items():
                raw_name = info.get("name", "").strip()
                role = info.get("actual_role", "").strip()
                if raw_name and raw_name.lower() not in ("storyteller", spk.lower()):
                    canon = resolve_player_name(raw_name, aliases)
                    roster.setdefault(canon, role)
        except Exception:
            pass
    return roster


def _load_speaker_map(out_dir: Path, aliases: dict) -> dict[str, str]:
    """Return {speaker_id: canonical_player_name}."""
    result: dict[str, str] = {}
    overrides = out_dir / "roster_overrides.json"
    if overrides.exists():
        try:
            ov = json.loads(overrides.read_text(encoding="utf-8"))
            for spk, info in ov.items():
                raw_name = info.get("name", "").strip()
                if raw_name:
                    canon = resolve_player_name(raw_name, aliases)
                    result[spk] = canon
        except Exception:
            pass
    return result


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


def _load_frame_scan(video_id: str) -> list[tuple[float, int]]:
    """Return [(t, header_visible)] sorted by t."""
    if not DB_PATH.exists():
        return []
    try:
        con = sqlite3.connect(str(DB_PATH))
        rows = con.execute(
            "SELECT t, header_visible FROM frame_scan WHERE video_id=? ORDER BY t",
            [video_id],
        ).fetchall()
        con.close()
        return [(float(t), int(h)) for t, h in rows]
    except Exception:
        return []


# ── Context helpers ────────────────────────────────────────────────────────────


def _phase_at(t: float, labels: list[dict]) -> str:
    for row in reversed(labels):
        if float(row["start"]) <= t:
            return row.get("phase", "Unknown")
    return "Unknown"


def _round_at(t: float, labels: list[dict]) -> int:
    for row in reversed(labels):
        if float(row["start"]) <= t:
            return int(row.get("round", 0))
    return 0


def _day_start_at_or_before(t: float, labels: list[dict]) -> float | None:
    """Return the start time of the Day phase that contains t, or None."""
    for row in reversed(labels):
        if row.get("phase") == "Day" and float(row["start"]) <= t:
            return float(row["start"])
    return None


def _has_vote_sequence_before(t: float, day_evts: list[dict]) -> bool:
    """True if a VoteSequence ended within _VOTE_LOOKBACK_S before t."""
    for evt in day_evts:
        if evt.get("event") == "VoteSequence":
            end = float(evt.get("end", 0))
            if end <= t and (t - end) <= _VOTE_LOOKBACK_S:
                return True
    return False


def _header_visible_near(t: float, frame_scan: list[tuple[float, int]]) -> bool:
    """True if header was visible at any frame within _HEADER_WINDOW_S of t."""
    for ft, hv in frame_scan:
        if abs(ft - t) <= _HEADER_WINDOW_S and hv:
            return True
    return False


# ── Pattern builder ────────────────────────────────────────────────────────────


def _build_patterns(player_name: str) -> list[tuple[re.Pattern, float, str]]:
    """Build (regex, confidence, event_hint) for one player name."""
    parts: list[tuple[re.Pattern, float, str]] = []
    # Escape the name; also build first-name only variant
    tokens = player_name.strip().split()
    name_variants = [re.escape(player_name)]
    if len(tokens) > 1:
        name_variants.append(re.escape(tokens[0]))  # first name only

    for variant in name_variants:
        for template, conf, hint in _NAME_DIED_TEMPLATES:
            pat = re.compile(template.format(name=variant), re.I)
            parts.append((pat, conf, hint))
        for template, conf in _NIGHT_DEATH_TEMPLATES:
            try:
                pat = re.compile(template.format(name=variant), re.I)
                parts.append((pat, conf, "night_death"))
            except re.error:
                pass
        # Ghost vote pattern
        try:
            ghost_pat = re.compile(_GHOST_VOTE_TEMPLATE.format(name=variant), re.I)
            parts.append((ghost_pat, 0.80, "uncertain_death"))
        except re.error:
            pass

    return parts


def _build_night_target_patterns(player_name: str) -> list[tuple[re.Pattern, float]]:
    """Build (regex, confidence) pairs for night-target detection for one player."""
    parts: list[tuple[re.Pattern, float]] = []
    tokens = player_name.strip().split()
    name_variants = [re.escape(player_name)]
    if len(tokens) > 1:
        name_variants.append(re.escape(tokens[0]))  # first name only
    for variant in name_variants:
        for template, conf in _NIGHT_TARGET_TEMPLATES:
            try:
                pat = re.compile(template.format(name=variant), re.I)
                parts.append((pat, conf))
            except re.error:
                pass
    return parts


# ── Main extraction ────────────────────────────────────────────────────────────


def _classify_event_type(
    candidate_hint: str,
    t: float,
    phase: str,
    day_evts: list[dict],
    phase_labels: list[dict],
) -> str:
    """Refine the event_type hint using phase and vote context."""
    has_vote = _has_vote_sequence_before(t, day_evts)
    day_start = _day_start_at_or_before(t, phase_labels)
    is_day_start = day_start is not None and (t - day_start) <= _DAY_START_WINDOW_S

    if candidate_hint == "execution":
        return "execution"
    if candidate_hint == "night_death":
        return "night_death"
    # uncertain_death: refine by context
    if has_vote and phase == "Day":
        return "execution"
    if is_day_start and phase == "Day":
        return "night_death"
    return "uncertain_death"


def extract(video_id: str, force: bool = False) -> bool:
    """Extract player_status.csv and death_events.csv for one video.
    Returns True on success, False on skip (outputs already exist + not force).
    """
    out_dir = OUTPUTS_DIR / video_id
    status_path = out_dir / "player_status.csv"
    death_path = out_dir / "death_events.csv"

    if status_path.exists() and death_path.exists() and not force:
        return False

    aliases = load_player_aliases()
    segs = _load_segments(out_dir)
    if not segs:
        print(f"  SKIP {video_id}: no segments file")
        return False

    roster = _load_roster(out_dir, aliases)
    if not roster:
        print(f"  SKIP {video_id}: empty roster (blind/members game?)")
        return False

    speaker_map = _load_speaker_map(out_dir, aliases)
    phase_labels = _load_phase_labels(out_dir)
    day_evts = _load_day_events(out_dir)
    frame_scan = _load_frame_scan(video_id)

    # Identify storyteller speaker(s): highest word count in first INTRO_CUTOFF_S.
    # Self-declarations from the storyteller are NOT player deaths and must be suppressed.
    _INTRO_CUTOFF_S = 330.0
    word_counts: dict[str, int] = defaultdict(int)
    for row in segs:
        if float(row.get("start", 0)) <= _INTRO_CUTOFF_S:
            spk = row.get("speaker", "")
            word_counts[spk] += len((row.get("text") or "").split())
    st_speakers: set[str] = set()
    if word_counts:
        max_words = max(word_counts.values())
        # Primary storyteller: top speaker by word count
        for spk, wc in word_counts.items():
            if wc == max_words:
                st_speakers.add(spk)
    # Also mark anyone explicitly mapped as "Storyteller" in speaker_map
    for spk, canon in speaker_map.items():
        if canon.lower() == "storyteller":
            st_speakers.add(spk)

    players = sorted(roster.keys())
    print(f"  Players ({len(players)}): {', '.join(players)}")
    print(f"  Storyteller speaker(s): {st_speakers}")

    # Build per-player patterns
    player_patterns: dict[str, list[tuple[re.Pattern, float, str]]] = {
        name: _build_patterns(name) for name in players
    }

    # Build per-player night-target patterns (used only during Night phase)
    night_target_pats: dict[str, list[tuple[re.Pattern, float]]] = {
        name: _build_night_target_patterns(name) for name in players
    }

    # Name-only patterns for cross-segment lookahead (no intent clause required).
    # Used when kill-intent fires in one segment and the target name is in the next.
    night_target_name_pats: dict[str, list[re.Pattern]] = {}
    for _pname in players:
        _toks = _pname.strip().split()
        _variants = [re.escape(_pname)]
        if len(_toks) > 1:
            _variants.append(re.escape(_toks[0]))  # first name only
        night_target_name_pats[_pname] = [
            re.compile(r"\b" + v + r"\b", re.I) for v in _variants
        ]

    # Collect timestamps where each player was explicitly targeted at night.
    # These NEVER alone cause a death event.
    night_targets: dict[str, list[float]] = defaultdict(list)

    # ── Scan transcript ────────────────────────────────────────────────────────
    # Collect raw death candidates: {player_name: [(t, conf, hint, source_text, speaker)]}
    raw_candidates: dict[str, list[tuple[float, float, str, str, str]]] = defaultdict(list)

    for row in segs:
        t = float(row.get("start", 0))
        text = (row.get("text") or "").strip()
        speaker = row.get("speaker", "")
        if not text:
            continue

        # --- per-player name patterns ---
        for pname, pats in player_patterns.items():
            for pat, conf, hint in pats:
                if pat.search(text):
                    raw_candidates[pname].append((t, conf, hint, text[:120], speaker))

        # --- self-declaration (speaker identity known, non-storyteller only) ---
        # Storytellers routinely say "I'm dead" narrating the game. Suppress them.
        # Dead players (ghosts) can speak at any point during Day, so accept
        # self-declarations from any non-ST roster player regardless of timing.
        if speaker not in st_speakers and _SELF_DEAD_RE.search(text):
            canon = speaker_map.get(speaker)
            if canon and canon in roster:
                raw_candidates[canon].append(
                    (t, 0.60, "uncertain_death", text[:120], speaker)
                )

        # --- night-target collection (Night phase, non-ST speakers only) ---
        # Demon choosing a kill target. Night-phase filter limits false positives
        # from Day discussion ("I'm going to kill Duncan in the vote").
        # A player cannot be their own target (demon cannot self-kill in BotC).
        if _phase_at(t, phase_labels) == "Night" and speaker not in st_speakers:
            speaker_canon = speaker_map.get(speaker)
            for pname, pats in night_target_pats.items():
                if speaker_canon == pname:
                    continue  # skip self-references
                for pat, _ in pats:
                    if pat.search(text):
                        night_targets[pname].append(t)
                        break  # one match per player per segment is enough

    # ── Cross-segment kill-intent lookahead (Night phase) ──────────────────────
    # Handles split transcriptions where the demon's kill-intent and the target
    # name are in consecutive segments (e.g. "I'm going to kill" / "Bryony").
    # Only fires during Night phase, for non-ST speakers, when the intent segment
    # does NOT already name a player (those are fully handled above).
    for i, row in enumerate(segs):
        t = float(row.get("start", 0))
        text = (row.get("text") or "").strip()
        speaker = row.get("speaker", "")
        if not text:
            continue
        if _phase_at(t, phase_labels) != "Night":
            continue
        if speaker in st_speakers:
            continue
        if not _KILL_INTENT_RE.search(text):
            continue
        # Skip if a player name was already present in this segment — the main
        # loop already captured the full match; no lookahead needed.
        speaker_canon = speaker_map.get(speaker)
        already_named = any(
            any(pat.search(text) for pat, _ in pats)
            for pname, pats in night_target_pats.items()
            if pname != speaker_canon
        )
        if already_named:
            continue
        # Scan forward within the lookahead window for a player name.
        for j in range(i + 1, len(segs)):
            next_row = segs[j]
            next_t = float(next_row.get("start", 0))
            if next_t - t > _KILL_INTENT_LOOKAHEAD_S:
                break
            next_text = (next_row.get("text") or "").strip()
            if not next_text:
                continue
            matched_player: str | None = None
            # First: try compiled name patterns (exact / first-name)
            for pname, pats in night_target_name_pats.items():
                if pname == speaker_canon:
                    continue  # demon cannot self-target
                for pat in pats:
                    if pat.search(next_text):
                        matched_player = pname
                        break
                if matched_player:
                    break
            # Fallback: resolve each word via aliases (catches transcription
            # variants like "Bryony" → "Briony" that aren't in name patterns)
            if not matched_player:
                for word in next_text.split():
                    resolved = resolve_player_name(word, aliases)
                    if resolved and resolved in roster and resolved != speaker_canon:
                        matched_player = resolved
                        break
            if matched_player:
                night_targets[matched_player].append(t)
                print(
                    f"    [lookahead] kill-intent at {t:.1f}s"
                    f" -> target '{matched_player}' found at {next_t:.1f}s"
                )
                break  # one target per kill-intent event

    # ── Deduplicate: per player, collapse events within _DEDUP_WINDOW_S ────────
    death_events: list[dict] = []

    for pname, candidates in raw_candidates.items():
        if not candidates:
            continue
        # Sort by timestamp then by descending confidence
        candidates.sort(key=lambda x: (x[0], -x[1]))

        # Cluster: group candidates within _DEDUP_WINDOW_S of each other
        clusters: list[list] = []
        for cand in candidates:
            if not clusters or (cand[0] - clusters[-1][-1][0]) > _DEDUP_WINDOW_S:
                clusters.append([cand])
            else:
                clusters[-1].append(cand)

        for cluster in clusters:
            # Best candidate = highest confidence, then earliest time
            best = max(cluster, key=lambda x: (x[1], -x[0]))
            t, conf, hint, src_text, speaker = best

            # Apply header-visible confidence boost
            if _header_visible_near(t, frame_scan):
                conf = min(1.0, conf + _HEADER_CONF_BOOST)

            if conf < _MIN_CONF:
                continue

            phase = _phase_at(t, phase_labels)
            rnd = _round_at(t, phase_labels)
            event_type = _classify_event_type(hint, t, phase, day_evts, phase_labels)

            # Apply night-target corroboration:
            # If demon-target dialogue named this player during ANY Night phase
            # before the death announcement, use it to improve classification
            # and add a small confidence boost.
            #
            # Rules:
            #   - Does not create a death event on its own (signals collected separately)
            #   - Upgrades uncertain_death → night_death (night-target is strong cause evidence)
            #   - Does NOT override execution (vote context takes precedence)
            #   - Protected/failed kills never cause false deaths because no death
            #     announcement exists to pair with — the corroboration only fires
            #     on already-confirmed death events.
            has_night_target = any(nt < t for nt in night_targets.get(pname, []))
            if has_night_target:
                conf = min(1.0, conf + _NIGHT_TARGET_CONF_BOOST)
                if event_type == "uncertain_death":
                    event_type = "night_death"

            # header-visible flag for the record
            hv = _header_visible_near(t, frame_scan)

            # storyteller_anchored: True when the death signal was spoken by a ST speaker.
            st_anchored = int(speaker in st_speakers)

            death_events.append({
                "timestamp_start": t,
                "player_name": pname,
                "event_type": event_type,
                "source": "transcript",
                "confidence": round(conf, 3),
                "phase": phase,
                "source_text": src_text,
                "inferred_round": rnd,
                "storyteller_anchored": st_anchored,
                "header_visible": int(hv),
                "linked_status_change": 1,  # always linked (death == status dead)
                "cause_confidence": round(conf, 3),
                "night_target_evidence": int(has_night_target),
            })

    # ── Sort by time ──────────────────────────────────────────────────────────
    death_events.sort(key=lambda x: x["timestamp_start"])

    # ── Remove duplicate deaths per player (keep earliest) ────────────────────
    seen_dead: dict[str, float] = {}
    final_deaths: list[dict] = []
    for evt in death_events:
        pname = evt["player_name"]
        if pname in seen_dead:
            continue  # already dead — ignore second death
        seen_dead[pname] = evt["timestamp_start"]
        final_deaths.append(evt)

    # ── Build player_status rows ───────────────────────────────────────────────
    status_rows: list[dict] = []

    # Initial alive state for each player at Intro start (t=0)
    intro_start = 0.0
    for pname in players:
        status_rows.append({
            "timestamp_start": intro_start,
            "player_name": pname,
            "prior_status": STATUS_UNKNOWN,
            "status": STATUS_ALIVE,
            "source": "intro_roster",
            "confidence": 0.90,
        })

    # Status transition: alive -> dead at first confirmed death
    for evt in final_deaths:
        status_rows.append({
            "timestamp_start": evt["timestamp_start"],
            "player_name": evt["player_name"],
            "prior_status": STATUS_ALIVE,
            "status": STATUS_DEAD,
            "source": "transcript",
            "confidence": evt["confidence"],
        })

    status_rows.sort(key=lambda x: (x["timestamp_start"], x["player_name"]))

    # ── Write outputs ──────────────────────────────────────────────────────────
    status_fields = [
        "timestamp_start", "player_name", "prior_status", "status",
        "source", "confidence",
    ]
    with status_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=status_fields)
        w.writeheader()
        w.writerows(status_rows)

    death_fields = [
        "timestamp_start", "player_name", "event_type", "source",
        "confidence", "phase", "source_text", "inferred_round",
        "storyteller_anchored", "header_visible", "linked_status_change",
        "cause_confidence", "night_target_evidence",
    ]
    with death_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=death_fields)
        w.writeheader()
        w.writerows(final_deaths)

    print(f"  Wrote {len(status_rows)} rows -> {status_path.name}")
    print(f"  Wrote {len(final_deaths)} death events -> {death_path.name}")
    print(
        f"  Deaths detected: "
        + ", ".join(
            f"{e['player_name']}={e['event_type']}@{e['timestamp_start']:.0f}s"
            for e in final_deaths
        )
    )

    return True


# ── CLI ────────────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(
        description="N4: Extract player status changes and death events."
    )
    ap.add_argument(
        "video_id",
        nargs="?",
        help="Video ID to process (omit with --all for batch mode)",
    )
    ap.add_argument("--all", action="store_true", help="Process all eligible videos")
    ap.add_argument(
        "--force", action="store_true", help="Overwrite existing output files"
    )
    args = ap.parse_args()

    if args.all or args.video_id is None:
        pl = json.loads(PLAYLIST_PATH.read_text(encoding="utf-8"))
        entries = pl.get("entries", [])
        eligible = [
            e["id"]
            for e in entries
            if e.get("status") == "analyzed"
            and not e.get("skip")
            and not e.get("blind")
        ]
        ok = skip = fail = 0
        for vid in eligible:
            print(f"\n=== {vid} ===")
            try:
                result = extract(vid, force=args.force)
                if result:
                    ok += 1
                else:
                    skip += 1
            except Exception as exc:
                print(f"  ERROR: {exc}")
                fail += 1
        print(f"\nDone: {ok} extracted, {skip} skipped (already exist), {fail} errors")
    elif args.video_id:
        print(f"=== extract_player_status.py  {args.video_id} ===")
        try:
            extract(args.video_id, force=args.force)
        except Exception as exc:
            print(f"ERROR: {exc}")
            raise
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
