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
import difflib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from pipeline_utils import load_player_aliases, normalize_player, resolve_player_name

try:
    from botc_ui import _team as _botc_team  # type: ignore[import]
except Exception:
    _botc_team = None  # type: ignore[assignment]

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
# phase start, treat it as a night-death morning announcement.
# 1200s (20 min) is generous to absorb videos where the phase detector
# mislabels Night 1 as part of Day 1 — the morning announcement of night kills
# happens at the real Day 1 start which may be well into the "Day" label.
_DAY_START_WINDOW_S: float = 1200.0

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

# ── Transcript name normalization ──────────────────────────────────────────────
# ASR (Whisper) frequently mangles player names.  Rather than maintaining a
# giant hand-curated alias file, we discover per-video transcript variants by:
#   1. Reversing player_aliases.json to add alias-backed regex variants (high conf)
#   2. Fuzzy-scanning all transcript tokens for close matches (lower conf)
# Discovered variants augment pattern matching; they NEVER alone create events.

# SequenceMatcher ratio threshold for accepting a fuzzy variant (0–1).
# 0.78 accepts ~1–2 char differences in 6-char names; rejects short noise words.
_FUZZY_NAME_THRESHOLD: float = 0.78

# Minimum token length to attempt fuzzy name matching (avoids matching "nil", "bri").
_FUZZY_MIN_TOKEN_LEN: int = 4

# Confidence scale factor applied to pattern matches via alias/fuzzy variants.
# Small penalty acknowledges minor additional uncertainty vs. exact canonical spelling.
_VARIANT_CONF_SCALE: float = 0.95

# Time window (seconds) used when reconciling a night-target event to a confirmed
# night-kill.  Only night_death events (not executions) within this window are
# eligible for positive matches.  Intentionally tight: prefer no link over a
# weak link.  10 minutes covers the longest observed night-to-morning transitions
# in the dataset without spanning into the next day phase.
_NIGHT_TARGET_LINK_WINDOW_S: float = 600.0

# Dedup window for night_target_events: two targeting records for the same
# speaker+target within this window are collapsed to the higher-confidence one.
_NT_DEDUP_WINDOW_S: float = 60.0

# Window for suppressing self-declaration false positives caused by the British-
# English idiom "I'm dead" as a shocked reaction to another player's death.
# If a high-confidence death signal fires for any OTHER player within this
# window of a self-declaration, the self-declaration is discarded.
_SELF_DECL_REACTION_WINDOW_S: float = 60.0

# Minimum confidence of the OTHER player's signal required to trigger
# self-declaration suppression. Filters accidental name mentions.
_SELF_DECL_REACTION_MIN_CONF: float = 0.70

# Confidence boost when a death signal falls during morning_result_announcement
# context (start of Day after Night — ST announcing last night's death).
_CONTEXT_MORNING_CONF_BOOST: float = 0.08

# Confidence boost for execution-type signals during execution_window context.
_CONTEXT_EXECUTION_CONF_BOOST: float = 0.05

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


def _load_context_segments(out_dir: Path) -> list[dict]:
    """Return context_segments.csv rows (N2b output), sorted by timestamp_start.

    Returns [] if absent — consumers must handle gracefully (fall back to raw phase lookup).
    """
    p = out_dir / "context_segments.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    rows.sort(key=lambda r: float(r.get("timestamp_start", 0)))
    return rows


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


def _context_at(t: float, ctx_segs: list[dict]) -> tuple[str, str]:
    """Return (context_mode, audience_scope) for timestamp t.

    ctx_segs must be sorted ascending by timestamp_start (guaranteed by _load_context_segments).
    Returns ("ambiguous", "ambiguous") when ctx_segs is empty or t precedes all rows.
    Consumers must treat unknown context_mode values as "ambiguous" per the contract.
    """
    mode = "ambiguous"
    scope = "ambiguous"
    for row in ctx_segs:
        try:
            if float(row["timestamp_start"]) <= t:
                mode = row.get("context_mode", "ambiguous")
                scope = row.get("audience_scope", "ambiguous")
            else:
                break  # rows sorted ascending — no later row can match
        except (KeyError, ValueError):
            continue
    return mode, scope


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


def _actor_alignment(speaker: str, speaker_map: dict, roster: dict) -> str:
    """Return Evil/Good/unknown alignment for the speaker using the per-video roster."""
    if _botc_team is None:
        return "unknown"
    canon = speaker_map.get(speaker, "")
    if not canon:
        return "unknown"
    role = roster.get(canon, "")
    if not role:
        return "unknown"
    try:
        result = _botc_team(role)
        return result if result else "unknown"
    except Exception:
        return "unknown"


def _header_visible_near(t: float, frame_scan: list[tuple[float, int]]) -> bool:
    """True if header was visible at any frame within _HEADER_WINDOW_S of t."""
    for ft, hv in frame_scan:
        if abs(ft - t) <= _HEADER_WINDOW_S and hv:
            return True
    return False


# ── Pattern builder ────────────────────────────────────────────────────────────


def _discover_name_variants(
    players: list[str],
    segs: list[dict],
    aliases: dict[str, str],
    threshold: float = _FUZZY_NAME_THRESHOLD,
) -> tuple[dict[str, set[str]], list[dict]]:
    """Scan transcript tokens to find ASR variant spellings of each player name.

    Two-stage approach:
      Stage 1 — alias: resolve each token via player_aliases.json; if the resolved
        canonical name is in the roster, that token is a known alias variant.
      Stage 2 — fuzzy: use difflib.SequenceMatcher against the roster candidate pool;
        only accept when a token uniquely resolves to one player (ambiguous → skip).

    Returns:
        variants       — {canonical_player_name: {variant_str, ...}}
        debug_records  — rows for name_resolution_debug.csv
    """
    variants: dict[str, set[str]] = {p: set() for p in players}
    debug: list[dict] = []

    # Build canonical form set for fast membership checks and possessive filtering.
    canonical_lowers: set[str] = set()
    for p in players:
        canonical_lowers.add(p.lower())
        parts = p.split()
        if len(parts) > 1:
            canonical_lowers.add(parts[0].lower())  # first-name shorthand

    unique_tokens: set[str] = set()
    for row in segs:
        text = (row.get("text") or "").strip()
        for tok in re.findall(r"[A-Za-z']+", text):
            if len(tok) >= _FUZZY_MIN_TOKEN_LEN and tok.lower() not in canonical_lowers:
                unique_tokens.add(tok)

    for tok in sorted(unique_tokens):
        tok_lower = tok.lower()

        # Filter: skip possessives of canonical names ("Nilesy's" → "nilesy" already canonical).
        # The existing patterns already handle possessives via (?:'s?) suffixes.
        if tok_lower.endswith("'s") and tok_lower[:-2] in canonical_lowers:
            continue

        # Stage 1: alias lookup
        resolved = resolve_player_name(tok, aliases)
        if resolved != tok:
            matched = next((p for p in players if p.lower() == resolved.lower()), None)
            if matched:
                variants[matched].add(tok)
                debug.append({
                    "raw_mention": tok,
                    "normalized_name": matched,
                    "confidence": 0.95,
                    "method": "alias",
                    "candidate_pool": len(players),
                })
            continue  # don't also run fuzzy if alias matched

        # Stage 2: fuzzy match against each player's name and first name.
        # Guards:
        #   - Skip if the TOKEN itself is < 5 chars: short tokens match too many things.
        #   - Skip each PLAYER whose normalized key is < 5 chars: short names (Ben=3,
        #     Ravs=4, Osie=4) produce too many false positives against common words.
        #     Add per-player aliases to player_aliases.json for short-name variants instead.
        if len(tok_lower) < 5:
            continue

        scores: list[tuple[float, str]] = []
        for p in players:
            p_key = normalize_player(p.split()[0] if ' ' in p else p)
            if len(p_key) < 5:
                continue  # too short — fuzzy unreliable; rely on alias stage only
            r = difflib.SequenceMatcher(None, tok_lower, normalize_player(p)).ratio()
            parts = p.split()
            if len(parts) > 1:
                r = max(r, difflib.SequenceMatcher(None, tok_lower, parts[0].lower()).ratio())
            scores.append((r, p))
        if not scores:
            continue
        scores.sort(reverse=True)

        best_r, best_p = scores[0]
        if best_r < threshold:
            continue  # no match — leave unresolved

        # Ambiguity check: reject if second-best is within 0.05 and also above threshold
        if len(scores) > 1 and scores[1][0] >= threshold * 0.92 and (best_r - scores[1][0]) < 0.05:
            debug.append({
                "raw_mention": tok,
                "normalized_name": "AMBIGUOUS",
                "confidence": round(best_r, 3),
                "method": "fuzzy_ambiguous",
                "candidate_pool": f"{best_p} vs {scores[1][1]}",
            })
            continue

        variants[best_p].add(tok)
        debug.append({
            "raw_mention": tok,
            "normalized_name": best_p,
            "confidence": round(best_r, 3),
            "method": "fuzzy",
            "candidate_pool": len(players),
        })

    return variants, debug


def _build_patterns(
    player_name: str,
    extra_variants: set[str] | None = None,
) -> list[tuple[re.Pattern, float, str]]:
    """Build (regex, confidence, event_hint) for one player name.

    extra_variants: alias or fuzzy-discovered ASR variant strings to also match.
    They are compiled at _VARIANT_CONF_SCALE of the canonical confidence.
    """
    parts: list[tuple[re.Pattern, float, str]] = []
    # Canonical name + first-name shorthand
    tokens = player_name.strip().split()
    name_variants: list[tuple[str, float]] = [(re.escape(player_name), 1.0)]
    if len(tokens) > 1:
        name_variants.append((re.escape(tokens[0]), 1.0))

    # Extra variants (aliases, fuzzy) at reduced confidence
    if extra_variants:
        for ev in extra_variants:
            ev = ev.strip()
            if ev and ev.lower() != player_name.lower():
                name_variants.append((re.escape(ev), _VARIANT_CONF_SCALE))

    for variant_esc, scale in name_variants:
        for template, conf, hint in _NAME_DIED_TEMPLATES:
            try:
                pat = re.compile(template.format(name=variant_esc), re.I)
                parts.append((pat, conf * scale, hint))
            except re.error:
                pass
        for template, conf in _NIGHT_DEATH_TEMPLATES:
            try:
                pat = re.compile(template.format(name=variant_esc), re.I)
                parts.append((pat, conf * scale, "night_death"))
            except re.error:
                pass
        try:
            ghost_pat = re.compile(_GHOST_VOTE_TEMPLATE.format(name=variant_esc), re.I)
            parts.append((ghost_pat, 0.80 * scale, "uncertain_death"))
        except re.error:
            pass

    return parts


def _build_night_target_patterns(
    player_name: str,
    extra_variants: set[str] | None = None,
) -> list[tuple[re.Pattern, float]]:
    """Build (regex, confidence) pairs for night-target detection for one player."""
    parts: list[tuple[re.Pattern, float]] = []
    tokens = player_name.strip().split()
    name_variants: list[tuple[str, float]] = [(re.escape(player_name), 1.0)]
    if len(tokens) > 1:
        name_variants.append((re.escape(tokens[0]), 1.0))
    if extra_variants:
        for ev in extra_variants:
            ev = ev.strip()
            if ev and ev.lower() != player_name.lower():
                name_variants.append((re.escape(ev), _VARIANT_CONF_SCALE))
    for variant_esc, scale in name_variants:
        for template, conf in _NIGHT_TARGET_TEMPLATES:
            try:
                pat = re.compile(template.format(name=variant_esc), re.I)
                parts.append((pat, conf * scale))
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
    if candidate_hint == "self_declaration":
        # Ghost saying "I'm dead": confirms death but not cause. The nearby
        # VoteSequence may be for a different player, so do NOT upgrade to
        # execution. Use day-start proximity for night_death, else uncertain.
        if is_day_start and phase == "Day":
            return "night_death"
        return "uncertain_death"
    # uncertain_death: refine by context.
    # Day-start takes priority over VoteSequence: a vote may have occurred for a
    # *different* player, and the morning announcement of a night kill should not
    # be misclassified as an execution. Explicit execution patterns (goodbye,
    # last words, "was executed") bypass this path via hint="execution" above.
    if is_day_start and phase == "Day":
        return "night_death"
    if has_vote and phase == "Day":
        return "execution"
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
    ctx_segs = _load_context_segments(out_dir)
    if ctx_segs:
        print(f"  Context segments: {len(ctx_segs)} rows loaded")

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

    # ── Transcript name normalization ───────────────────────────────────────────
    # Build a reverse alias map: canonical_name → [alias, alias, ...]
    # These are known ASR/OCR variants from player_aliases.json.
    rev_aliases: dict[str, set[str]] = defaultdict(set)
    for alias_str, canon in aliases.items():
        if canon in roster:
            rev_aliases[canon].add(alias_str)

    # Fuzzy-scan all segment tokens to discover additional per-video variants.
    # This catches ASR mangles not yet in player_aliases.json (e.g. "Briney").
    asr_variants, name_debug_records = _discover_name_variants(players, segs, aliases)

    # Merge alias variants into asr_variants (they share the same pattern slots)
    for pname in players:
        asr_variants[pname].update(rev_aliases.get(pname, set()))

    # Report discoveries
    total_variants = sum(len(v) for v in asr_variants.values())
    if total_variants:
        print(f"  Name variants (alias+fuzzy): {total_variants}")
        for p, vs in sorted(asr_variants.items()):
            if vs:
                print(f"    {p}: {sorted(vs)}")

    # Build per-player patterns (with variant augmentation)
    player_patterns: dict[str, list[tuple[re.Pattern, float, str]]] = {
        name: _build_patterns(name, extra_variants=asr_variants.get(name))
        for name in players
    }

    # Build per-player night-target patterns (used only during Night phase)
    night_target_pats: dict[str, list[tuple[re.Pattern, float]]] = {
        name: _build_night_target_patterns(name, extra_variants=asr_variants.get(name))
        for name in players
    }

    # Name-only patterns for cross-segment lookahead (no intent clause required).
    # Used when kill-intent fires in one segment and the target name is in the next.
    night_target_name_pats: dict[str, list[re.Pattern]] = {}
    for _pname in players:
        _toks = _pname.strip().split()
        _extra = asr_variants.get(_pname, set())
        _variant_strings = [_pname] + ([_toks[0]] if len(_toks) > 1 else []) + list(_extra)
        night_target_name_pats[_pname] = [
            re.compile(r"\b" + re.escape(v.strip()) + r"\b", re.I)
            for v in _variant_strings if v.strip()
        ]

    # Collect timestamps where each player was explicitly targeted at night.
    # These NEVER alone cause a death event.
    night_targets: dict[str, list[float]] = defaultdict(list)

    # Full metadata records for night-target events → written to night_target_events.csv.
    # Separate concept from confirmed deaths — intended target ≠ actual victim.
    night_target_records: list[dict] = []

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
                    (t, 0.60, "self_declaration", text[:120], speaker)
                )

        # --- night-target collection (Night phase or private_like context) ---
        # Demon choosing a kill target.  We restrict to Night phase OR to
        # private_like Day windows (StorytellerInterruption events where the demon
        # whispers their kill choice to the ST at end of Day).  Both contexts
        # suppress the common false positive of "I'll kill X" in public Day
        # discussion about executions.
        # A player cannot be their own target (demon cannot self-kill in BotC).
        _is_private = (
            _phase_at(t, phase_labels) == "Night"
            or (bool(ctx_segs) and _context_at(t, ctx_segs)[1] == "private_like")
        )
        if _is_private and speaker not in st_speakers:
            speaker_canon = speaker_map.get(speaker)
            for pname, pats in night_target_pats.items():
                if speaker_canon == pname:
                    continue  # skip self-references
                for pat, conf in pats:
                    if pat.search(text):
                        night_targets[pname].append(t)
                        night_target_records.append({
                            "timestamp_start": t,
                            "source_speaker": speaker,
                            "target_player": pname,
                            "target_role_if_any": roster.get(pname, ""),
                            "source_text": text[:120],
                            "confidence": round(conf, 3),
                            "evidence_type": "named_intent",
                            "actor_hint": speaker_canon or "",
                            "candidate_actor_alignment": _actor_alignment(
                                speaker, speaker_map, roster
                            ),
                            # filled after final_deaths computed:
                            "linked_death_player": "",
                            "linked_death_timestamp": "",
                            "outcome_relation": "unknown",
                        })
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
        _is_private_lookahead = (
            _phase_at(t, phase_labels) == "Night"
            or (bool(ctx_segs) and _context_at(t, ctx_segs)[1] == "private_like")
        )
        if not _is_private_lookahead:
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
                # Combine intent + target segments for source_text clarity
                combined_src = f"{text[:60].strip()} | {next_text[:60].strip()}"
                night_target_records.append({
                    "timestamp_start": t,
                    "source_speaker": speaker,
                    "target_player": matched_player,
                    "target_role_if_any": roster.get(matched_player, ""),
                    "source_text": combined_src[:120],
                    "confidence": 0.70,  # split across segments → lower certainty
                    "evidence_type": "split_intent",
                    "actor_hint": speaker_map.get(speaker, "") or "",
                    "candidate_actor_alignment": _actor_alignment(
                        speaker, speaker_map, roster
                    ),
                    "linked_death_player": "",
                    "linked_death_timestamp": "",
                    "outcome_relation": "unknown",
                })
                print(
                    f"    [lookahead] kill-intent at {t:.1f}s"
                    f" -> target '{matched_player}' found at {next_t:.1f}s"
                )
                break  # one target per kill-intent event

    # ── Self-declaration reaction suppression ──────────────────────────────────
    # "I'm dead" is common British slang for shock/disbelief.  When another
    # player's confirmed death is announced within _SELF_DECL_REACTION_WINDOW_S
    # of a self-declaration, the self-declaration is almost certainly a reaction
    # ("I'm dead, I can't believe Nilesy died!") rather than a ghost status
    # update.  Only non-self-declaration signals with conf >= threshold count as
    # "confirmed death announcements" for this check.
    other_deaths: list[tuple[float, str]] = [
        (ct, opname)
        for opname, cands in raw_candidates.items()
        for ct, cconf, chint, _, _ in cands
        if chint != "self_declaration" and cconf >= _SELF_DECL_REACTION_MIN_CONF
    ]
    for pname in list(raw_candidates.keys()):
        filtered: list = []
        for cand in raw_candidates[pname]:
            ct, cconf, chint, csrc, cspk = cand
            if chint == "self_declaration":
                is_reaction = any(
                    opname != pname and abs(ct - ot) <= _SELF_DECL_REACTION_WINDOW_S
                    for ot, opname in other_deaths
                )
                if is_reaction:
                    continue  # suppress: likely shocked reaction to another death
            filtered.append(cand)
        raw_candidates[pname] = filtered

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

            # Apply context-based confidence adjustments (requires context_segments.csv).
            # Morning context: death announcements at Day start after Night have a high
            # prior probability of being real night-death announcements from the ST.
            # Execution context: execution-type signals during active vote sequences are
            # corroborated by the timing evidence already baked into the VoteSequence.
            if ctx_segs:
                ctx_mode, _ = _context_at(t, ctx_segs)
                if ctx_mode == "morning_result_announcement" and hint in (
                    "night_death", "uncertain_death"
                ):
                    conf = min(1.0, conf + _CONTEXT_MORNING_CONF_BOOST)
                elif ctx_mode == "execution_window" and hint == "execution":
                    conf = min(1.0, conf + _CONTEXT_EXECUTION_CONF_BOOST)

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

    # ── Night-target events: dedup + reconcile + write ────────────────────────
    # Dedup: for the same (source_speaker, target_player), collapse records that
    # are within _NT_DEDUP_WINDOW_S of each other — keep highest confidence.
    nte_deduped: list[dict] = []
    for rec in sorted(night_target_records, key=lambda r: r["timestamp_start"]):
        matched_existing = None
        for existing in nte_deduped:
            if (
                existing["source_speaker"] == rec["source_speaker"]
                and existing["target_player"] == rec["target_player"]
                and abs(existing["timestamp_start"] - rec["timestamp_start"]) <= _NT_DEDUP_WINDOW_S
            ):
                matched_existing = existing
                break
        if matched_existing is None:
            nte_deduped.append(rec)
        elif rec["confidence"] > matched_existing["confidence"]:
            # Replace lower-confidence duplicate with higher-confidence version
            nte_deduped[nte_deduped.index(matched_existing)] = rec

    # Reconcile each targeting record against confirmed deaths.
    # Outcome categories (per task spec):
    #   matched_actual_death      — target player confirmed dead after this event
    #   did_not_match_actual_death — target survived; someone else died that night
    #   no_confirmed_death         — no death detected in the look-ahead window
    #   unknown                    — cannot determine (e.g. death before targeting)
    # Two death lookups for reconciliation.  Only night_death events are used for
    # positive matches: executions are day-phase events and must never be linked
    # to a night targeting record.  all_death_lookup is used only for the edge
    # case where the target died before the targeting event was recorded.
    night_death_lookup: dict[str, float] = {
        d["player_name"]: d["timestamp_start"]
        for d in final_deaths
        if d["event_type"] == "night_death"
    }
    all_death_lookup: dict[str, float] = {
        d["player_name"]: d["timestamp_start"] for d in final_deaths
    }
    for rec in nte_deduped:
        t_evt = rec["timestamp_start"]
        target = rec["target_player"]
        target_nd_t = night_death_lookup.get(target)   # night-kill timestamp, if any
        target_any_t = all_death_lookup.get(target)    # any-cause death, if any

        if (target_nd_t is not None
                and t_evt < target_nd_t <= t_evt + _NIGHT_TARGET_LINK_WINDOW_S):
            # Night-kill of the target within the night window — direct match.
            rec["outcome_relation"] = "matched_actual_death"
            rec["linked_death_player"] = target
            rec["linked_death_timestamp"] = round(target_nd_t, 2)
        elif target_any_t is not None and target_any_t <= t_evt:
            # Target was already dead before this targeting event — flag as unknown.
            rec["outcome_relation"] = "unknown"
            rec["linked_death_player"] = target
            rec["linked_death_timestamp"] = round(target_any_t, 2)
        else:
            # No matching night-kill of the target within the window.
            # Check for other night-kills in the same narrow window.
            # Records that target != victim without evidence of why are kept as-is;
            # no mechanic (protection / redirect / bounce) is inferred from mismatch.
            other_nd = [
                d for d in final_deaths
                if d["player_name"] != target
                and d["event_type"] == "night_death"
                and t_evt < d["timestamp_start"] <= t_evt + _NIGHT_TARGET_LINK_WINDOW_S
            ]
            if other_nd:
                actual = min(other_nd, key=lambda d: d["timestamp_start"])
                rec["outcome_relation"] = "did_not_match_actual_death"
                rec["linked_death_player"] = actual["player_name"]
                rec["linked_death_timestamp"] = round(actual["timestamp_start"], 2)
            else:
                rec["outcome_relation"] = "no_confirmed_death"
                rec["linked_death_player"] = ""
                rec["linked_death_timestamp"] = ""

    # Write night_target_events.csv (always — even if empty, so downstream can
    # reliably detect N4 coverage vs. a missing file).
    nte_path = out_dir / "night_target_events.csv"
    nte_fields = [
        "timestamp_start", "source_speaker", "target_player", "target_role_if_any",
        "source_text", "confidence", "evidence_type",
        "candidate_actor_alignment", "actor_hint",
        "linked_death_player", "linked_death_timestamp", "outcome_relation",
    ]
    with nte_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=nte_fields)
        w.writeheader()
        w.writerows(nte_deduped)
    print(f"  Wrote {len(nte_deduped)} night-target events -> {nte_path.name}")
    if nte_deduped:
        for rec in nte_deduped:
            print(
                f"    {rec['evidence_type']:14} t={rec['timestamp_start']:.0f}s"
                f"  actor={rec['actor_hint'] or rec['source_speaker']}"
                f"  -> {rec['target_player']}  [{rec['outcome_relation']}]"
            )

    # ── Name resolution debug artifact (optional) ─────────────────────────────
    # Emitted only when at least one variant or alias was discovered.
    # Useful for auditing what the normalization layer matched/rejected.
    if name_debug_records:
        debug_path = out_dir / "name_resolution_debug.csv"
        debug_fields = ["video_id", "raw_mention", "normalized_name", "confidence", "method", "candidate_pool"]
        for rec in name_debug_records:
            rec["video_id"] = video_id
            rec.setdefault("candidate_pool", len(players))
        with debug_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=debug_fields)
            w.writeheader()
            w.writerows(name_debug_records)
        print(f"  Wrote {len(name_debug_records)} resolution records -> {debug_path.name}")

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
