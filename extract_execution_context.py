"""extract_execution_context.py — N5: Public execution-context event extraction.

Extracts public kill pressure, nomination references, and execution-result narration
from the game transcript using the context layer (N2b: context_segments.csv) to
strictly gate patterns to appropriate public-day contexts.

Key separation from N4 (night_target_events.csv):
    N4 captures PRIVATE kill intent: demon selecting a night target.
        Gate: Night phase OR private_like context (StorytellerInterruption).
    N5 captures PUBLIC execution pressure: town discussing who to execute today.
        Gate: public audience_scope in Day phase ONLY.
    This separation is possible because context_segments.csv tags each interval
    with audience_scope (public vs. private_like).

Supported event types:
    public_kill_pressure       — player explicitly pushed for execution in public
                                  ("we should kill X", "vote for X", "X should die")
    nomination_reference       — player explicitly nominated by name
                                  ("I nominate X", "nominating X")
    execution_result_narration — ST (or confirmed narrator) announcing execution outcome
                                  ("Goodbye X", "X was executed", "X, last words")
    execution_opposition       — speaker defending a player against execution push
                                  ("don't kill X", "X is innocent", "leave X alone")

Context gate (strict):
    public_kill_pressure, nomination_reference, execution_opposition:
        → ONLY when audience_scope == "public" AND phase == "Day"
          AND context_mode in {public_day_discussion, execution_window}
        → Never fires during Night phase, private_like context, or intro phase
    execution_result_narration:
        → Fires during any Day phase; boosted when ST-anchored AND in
          execution_window / morning_result_announcement context
    Fallback (no context_segments.csv): requires raw phase == "Day"
      and reverts to public_day_discussion assumption for Day segments.

Conservative policy: _MIN_CONF = 0.60; prefer false negatives.

Output:
    outputs/<id>/execution_context_events.csv

Usage:
    python extract_execution_context.py <video_id>
    python extract_execution_context.py <video_id> --force
    python extract_execution_context.py --all [--force]
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
from collections import defaultdict
from pathlib import Path

from pipeline_utils import load_player_aliases, normalize_player, resolve_player_name

# ── Paths ──────────────────────────────────────────────────────────────────────

OUTPUTS_DIR = Path("outputs")
PLAYLIST_PATH = Path("playlist.json")

# ── Constants ──────────────────────────────────────────────────────────────────

# Conservative minimum confidence — higher than N4 (0.45) since public discussion
# has more noise than private night dialogue.
_MIN_CONF: float = 0.60

# Confidence scale for alias/fuzzy name variants (slight penalty vs. canonical).
_VARIANT_CONF_SCALE: float = 0.95

# Fuzzy name matching (mirrors N4 thresholds).
_FUZZY_NAME_THRESHOLD: float = 0.78
_FUZZY_MIN_TOKEN_LEN: int = 4

# Storyteller auto-detection window (mirrors N2/N4).
_INTRO_CUTOFF_S: float = 330.0

# Confidence boost when execution result is announced by the Storyteller speaker.
_ST_ANCHOR_BOOST: float = 0.08

# Confidence boost for result narration in an execution or morning context.
_CONTEXT_RESULT_BOOST: float = 0.05

# Deduplication window: same (speaker, target, event_type) within this window
# → keep only the highest-confidence record.
_DEDUP_WINDOW_S: float = 30.0

# Context modes that allow public kill-pressure and nomination patterns.
# EXCLUDES private_like (night_private_action, storyteller_narration) and intro.
_PUBLIC_DAY_CONTEXTS = frozenset({
    "public_day_discussion",
    "execution_window",
})

# Context modes where execution-result narration is expected.
_RESULT_NARRATION_CONTEXTS = frozenset({
    "execution_window",
    "morning_result_announcement",
    "storyteller_narration",
    "public_day_discussion",
})

# Output fieldnames (per task spec + context).
_FIELDNAMES = [
    "timestamp_start",
    "speaker",
    "source_player_name",
    "target_player",
    "target_role_if_any",
    "event_type",
    "confidence",
    "context_mode",
    "phase",
    "source_text",
]

# ── Pattern templates ──────────────────────────────────────────────────────────
# All use {name} as the player-name placeholder (re.escape'd at build time).
# Ordered from most-specific (highest confidence) to broadest.

# Explicit nomination of a player by name.
_NOMINATION_TEMPLATES: list[tuple[str, float]] = [
    # "I nominate X" / "I'd like to nominate X" / "I want to nominate X"
    (r"\bi\s+(?:would\s+like\s+to\s+|want\s+to\s+)?nominate\s+{name}\b", 0.85),
    # "nominate X" / "nominating X" (direct imperative or progressive)
    (r"\bnominat(?:e|ing)\s+{name}\b", 0.80),
    # "my nomination is X" / "my nomination goes to X" / "my nomination would be X"
    (r"\bmy\s+nomination\s+(?:is|goes\s+to|would\s+be)\s+{name}\b", 0.82),
    # "X is nominated" / "X has been nominated" (announcement of existing nomination)
    (r"\b{name}\s+(?:is|has\s+been)\s+nominated\b", 0.75),
    # "put X on the block" (colloquial BotC nomination language)
    (r"\bput\s+{name}\s+on\s+the\s+block\b", 0.72),
]

# Public kill / execution pressure directed at a specific player.
# Context gate (public_day_discussion or execution_window) prevents
# this from firing on demon night-selection dialogue.
_KILL_PRESSURE_TEMPLATES: list[tuple[str, float]] = [
    # "we should kill/execute/get rid of X"
    (r"\b(?:we\s+should|we\s+need\s+to)\s+(?:kill|execute|get\s+rid\s+of)\s+{name}\b", 0.80),
    # "let's kill/execute X" / "let's get rid of X"
    (r"\blet'?s\s+(?:kill|execute|get\s+rid\s+of)\s+{name}\b", 0.80),
    # "X should die / go / be executed / be killed"
    (r"\b{name}\s+should\s+(?:die|go|be\s+executed|be\s+killed)\b", 0.75),
    # "X needs to die / go"
    (r"\b{name}\s+needs\s+to\s+(?:die|go)\b", 0.72),
    # "I'm voting for X" / "I'll vote for X" (explicit vote direction)
    (r"\b(?:i'?m|i'?ll|i\s+am)\s+voting\s+(?:for\s+)?{name}\b", 0.75),
    # "vote for X" / "vote X" (imperative, in public context)
    (r"\bvote\s+(?:for\s+)?{name}\b", 0.68),
    # "get rid of X" (with no execute/kill verb)
    (r"\bget\s+rid\s+of\s+{name}\b", 0.70),
    # "execute X" (imperative in public day — safe with context gate)
    (r"\bexecute\s+{name}\b", 0.65),
    # "X should be dead"
    (r"\b{name}\s+should\s+be\s+dead\b", 0.68),
    # "X is the demon / definitely evil / clearly evil" (accusation → execution push)
    # Lower confidence: accusation is evidence of push, not push itself.
    (r"\b{name}\s+is\s+(?:the\s+demon|definitely\s+evil|clearly\s+evil)\b", 0.62),
]

# Execution result narration: ST announcing who was executed / goodbye ceremony.
# Gets confidence boost when spoken by Storyteller AND in execution context.
_EXECUTION_RESULT_TEMPLATES: list[tuple[str, float]] = [
    # "<Name> was/has been executed" — most explicit
    (r"\b{name}(?:'s?)?\s+(?:was|has\s+been|is)\s+executed\b", 0.90),
    (r"\bexecuted\s+{name}\b", 0.85),
    # "Goodbye X" / "X, last words" / "last words, X" — execution ceremony phrasing
    (r"\bgoodbye.{{0,30}}{name}\b", 0.82),
    (r"\b{name}[,\s]+(?:any\s+)?last\s+words\b", 0.85),
    (r"\blast\s+words.{{0,20}}{name}\b", 0.85),
    # "X dies today / at dawn"
    (r"\b{name}\s+(?:is\s+executed|dies\s+today|dies\s+at\s+dawn)\b", 0.78),
    # "we're/I'm executing X"
    (r"\b(?:we'?re|i'?m|we\s+are|i\s+am)\s+executing\s+{name}\b", 0.80),
]

# Defending a player against execution pressure (opposition to a kill push).
_EXECUTION_OPPOSITION_TEMPLATES: list[tuple[str, float]] = [
    # "don't kill / execute X"
    (r"\bdon'?t\s+(?:kill|execute)\s+{name}\b", 0.78),
    # "don't vote for X" / "don't vote X"
    (r"\bdon'?t\s+vote\s+(?:for\s+)?{name}\b", 0.78),
    # "we should not kill / execute X" / "we shouldn't kill X"
    (r"\bwe\s+should(?:n'?t|\s+not)\s+(?:kill|execute)\s+{name}\b", 0.78),
    # "X is innocent"
    (r"\b{name}\s+is\s+innocent\b", 0.68),
    # "leave X alone" / "spare X"
    (r"\b(?:leave|spare)\s+{name}\b", 0.65),
    # "I'd keep / save / protect X"
    (r"\bi'?d\s+(?:keep|save|protect)\s+{name}\b", 0.65),
    # "X is not evil / not the demon"
    (r"\b{name}\s+is\s+not\s+(?:evil|the\s+demon|a\s+minion)\b", 0.65),
]


# ── Data loaders ───────────────────────────────────────────────────────────────


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
                raw = p.get("name", "").strip()
                role = p.get("actual_role", "").strip()
                if raw:
                    roster[resolve_player_name(raw, aliases)] = role
        except Exception:
            pass
    overrides = out_dir / "roster_overrides.json"
    if overrides.exists():
        try:
            ov = json.loads(overrides.read_text(encoding="utf-8"))
            for spk, info in ov.items():
                raw = info.get("name", "").strip()
                role = info.get("actual_role", "").strip()
                if raw and raw.lower() not in ("storyteller", spk.lower()):
                    roster.setdefault(resolve_player_name(raw, aliases), role)
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
                raw = info.get("name", "").strip()
                if raw:
                    result[spk] = resolve_player_name(raw, aliases)
        except Exception:
            pass
    return result


def _load_phase_labels(out_dir: Path) -> list[dict]:
    p = out_dir / "phase_labels.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _load_context_segments(out_dir: Path) -> list[dict]:
    """Return context_segments.csv rows sorted ascending by timestamp_start."""
    p = out_dir / "context_segments.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    rows.sort(key=lambda r: float(r.get("timestamp_start", 0)))
    return rows


# ── Context helpers ────────────────────────────────────────────────────────────


def _phase_at(t: float, labels: list[dict]) -> str:
    for row in reversed(labels):
        if float(row["start"]) <= t:
            return row.get("phase", "Unknown")
    return "Unknown"


def _context_at(t: float, ctx_segs: list[dict]) -> tuple[str, str]:
    """Return (context_mode, audience_scope) for timestamp t.

    Mirrors N4's _context_at — rows must be sorted ascending by timestamp_start.
    Returns ("ambiguous", "ambiguous") when ctx_segs is absent or t precedes all rows.
    """
    mode = "ambiguous"
    scope = "ambiguous"
    for row in ctx_segs:
        try:
            if float(row["timestamp_start"]) <= t:
                mode = row.get("context_mode", "ambiguous")
                scope = row.get("audience_scope", "ambiguous")
            else:
                break
        except (KeyError, ValueError):
            continue
    return mode, scope


# ── Name variant discovery ─────────────────────────────────────────────────────


def _discover_name_variants(
    players: list[str],
    segs: list[dict],
    aliases: dict[str, str],
) -> dict[str, set[str]]:
    """Discover alias and ASR fuzzy variant spellings for each player name.

    Two stages:
      Stage 1 — alias: use player_aliases.json to find known ASR/OCR variants.
      Stage 2 — fuzzy: scan transcript tokens with difflib.SequenceMatcher.

    Returns {canonical_player_name: {variant_str, ...}}.
    Mirrors N4's _discover_name_variants (standalone copy — N5 is not a library).
    """
    variants: dict[str, set[str]] = {p: set() for p in players}

    # Stage 1: alias-backed variants
    for alias_str, canon in aliases.items():
        matched = next((p for p in players if p.lower() == canon.lower()), None)
        if matched:
            variants[matched].add(alias_str)

    # Stage 2: fuzzy transcript token scan
    canonical_lowers: set[str] = set()
    for p in players:
        canonical_lowers.add(p.lower())
        parts = p.split()
        if len(parts) > 1:
            canonical_lowers.add(parts[0].lower())

    unique_tokens: set[str] = set()
    for row in segs:
        text = (row.get("text") or "").strip()
        for tok in re.findall(r"[A-Za-z']+", text):
            if len(tok) >= _FUZZY_MIN_TOKEN_LEN and tok.lower() not in canonical_lowers:
                unique_tokens.add(tok)

    for tok in sorted(unique_tokens):
        tok_lower = tok.lower()
        # Skip possessives of canonical names — patterns already handle (?:'s?) suffixes.
        if tok_lower.endswith("'s") and tok_lower[:-2] in canonical_lowers:
            continue
        if len(tok_lower) < 5:
            continue

        scores: list[tuple[float, str]] = []
        for p in players:
            p_key = normalize_player(p.split()[0] if " " in p else p)
            if len(p_key) < 5:
                continue  # short names — alias-only; fuzzy too noisy
            r = difflib.SequenceMatcher(None, tok_lower, normalize_player(p)).ratio()
            parts = p.split()
            if len(parts) > 1:
                r = max(r, difflib.SequenceMatcher(None, tok_lower, parts[0].lower()).ratio())
            scores.append((r, p))
        if not scores:
            continue
        scores.sort(reverse=True)

        best_r, best_p = scores[0]
        if best_r < _FUZZY_NAME_THRESHOLD:
            continue
        # Reject if second-best is within 0.05 and also above threshold (ambiguous)
        if (
            len(scores) > 1
            and scores[1][0] >= _FUZZY_NAME_THRESHOLD * 0.92
            and (best_r - scores[1][0]) < 0.05
        ):
            continue
        variants[best_p].add(tok)

    return variants


# ── Pattern builder ────────────────────────────────────────────────────────────


def _build_patterns(
    player_name: str,
    templates: list[tuple[str, float]],
    extra_variants: set[str] | None = None,
) -> list[tuple[re.Pattern, float]]:
    """Compile (regex, confidence) pairs for one player across all templates.

    Canonical name and first-name shorthand are both matched at confidence 1.0.
    Extra variants (aliases, fuzzy) at _VARIANT_CONF_SCALE penalty.
    """
    results: list[tuple[re.Pattern, float]] = []
    tokens = player_name.strip().split()
    name_variants: list[tuple[str, float]] = [(re.escape(player_name), 1.0)]
    if len(tokens) > 1:
        name_variants.append((re.escape(tokens[0]), 1.0))  # first-name shorthand
    if extra_variants:
        for ev in extra_variants:
            ev = ev.strip()
            if ev and ev.lower() != player_name.lower():
                name_variants.append((re.escape(ev), _VARIANT_CONF_SCALE))

    for variant_esc, scale in name_variants:
        for template, base_conf in templates:
            try:
                pat = re.compile(template.format(name=variant_esc), re.I)
                results.append((pat, base_conf * scale))
            except re.error:
                pass

    return results


# ── Event helpers ──────────────────────────────────────────────────────────────


def _make_event(
    *,
    t: float,
    speaker: str,
    source_player: str,
    target: str,
    roster: dict,
    event_type: str,
    conf: float,
    ctx_mode: str,
    phase: str,
    text: str,
) -> dict:
    return {
        "timestamp_start": round(t, 3),
        "speaker": speaker,
        "source_player_name": source_player,
        "target_player": target,
        "target_role_if_any": roster.get(target, ""),
        "event_type": event_type,
        "confidence": round(conf, 3),
        "context_mode": ctx_mode,
        "phase": phase,
        "source_text": text[:120],
    }


def _dedup_events(events: list[dict]) -> list[dict]:
    """Collapse same (speaker, target_player, event_type) within _DEDUP_WINDOW_S.

    When duplicates exist, keeps the highest-confidence record.
    Input must be sorted ascending by timestamp_start.
    """
    deduped: list[dict] = []
    for evt in events:
        matched_idx: int | None = None
        for idx, existing in enumerate(deduped):
            if (
                existing["speaker"] == evt["speaker"]
                and existing["target_player"] == evt["target_player"]
                and existing["event_type"] == evt["event_type"]
                and abs(existing["timestamp_start"] - evt["timestamp_start"]) <= _DEDUP_WINDOW_S
            ):
                matched_idx = idx
                break
        if matched_idx is None:
            deduped.append(evt)
        elif evt["confidence"] > deduped[matched_idx]["confidence"]:
            deduped[matched_idx] = evt  # replace with higher-confidence version

    return sorted(deduped, key=lambda e: e["timestamp_start"])


# ── Per-video extraction ───────────────────────────────────────────────────────


def extract(video_id: str, force: bool = False) -> bool:
    """Extract execution_context_events.csv for one video.

    Returns True on success, False on skip (output exists and not force).
    """
    out_dir = OUTPUTS_DIR / video_id
    out_path = out_dir / "execution_context_events.csv"

    if out_path.exists() and not force:
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
    ctx_segs = _load_context_segments(out_dir)

    # Detect Storyteller speaker(s): dominant talker in first _INTRO_CUTOFF_S seconds.
    # Mirrors N2/N4 detection — ensures consistent ST identification.
    word_counts: dict[str, int] = defaultdict(int)
    for row in segs:
        if float(row.get("start", 0)) <= _INTRO_CUTOFF_S:
            spk = row.get("speaker", "")
            word_counts[spk] += len((row.get("text") or "").split())
    st_speakers: set[str] = set()
    if word_counts:
        max_words = max(word_counts.values())
        for spk, wc in word_counts.items():
            if wc == max_words:
                st_speakers.add(spk)
    for spk, canon in speaker_map.items():
        if canon.lower() == "storyteller":
            st_speakers.add(spk)

    players = sorted(roster.keys())
    print(f"  Players ({len(players)}): {', '.join(players)}")
    print(f"  ST speakers: {st_speakers}")
    print(f"  Context segments loaded: {len(ctx_segs)} rows")

    # Discover name variants (alias + fuzzy) to extend pattern matching.
    asr_variants = _discover_name_variants(players, segs, aliases)
    total_variants = sum(len(v) for v in asr_variants.values())
    if total_variants:
        print(f"  Name variants: {total_variants}")
        for p, vs in sorted(asr_variants.items()):
            if vs:
                print(f"    {p}: {sorted(vs)}")

    # Build compiled patterns per player per event type.
    nomination_pats: dict[str, list[tuple[re.Pattern, float]]] = {}
    kill_pressure_pats: dict[str, list[tuple[re.Pattern, float]]] = {}
    result_narration_pats: dict[str, list[tuple[re.Pattern, float]]] = {}
    opposition_pats: dict[str, list[tuple[re.Pattern, float]]] = {}

    for pname in players:
        vs = asr_variants.get(pname)
        nomination_pats[pname] = _build_patterns(pname, _NOMINATION_TEMPLATES, vs)
        kill_pressure_pats[pname] = _build_patterns(pname, _KILL_PRESSURE_TEMPLATES, vs)
        result_narration_pats[pname] = _build_patterns(pname, _EXECUTION_RESULT_TEMPLATES, vs)
        opposition_pats[pname] = _build_patterns(pname, _EXECUTION_OPPOSITION_TEMPLATES, vs)

    events: list[dict] = []

    # ── Scan transcript ─────────────────────────────────────────────────────────
    for row in segs:
        t = float(row.get("start", 0))
        text = (row.get("text") or "").strip()
        speaker = row.get("speaker", "")
        if not text:
            continue

        # Determine phase and context at this timestamp.
        phase = _phase_at(t, phase_labels)
        if ctx_segs:
            ctx_mode, audience_scope = _context_at(t, ctx_segs)
        else:
            # Fallback without context_segments: require Day phase; assume public.
            if phase != "Day":
                continue
            ctx_mode = "public_day_discussion"
            audience_scope = "public"

        is_st = speaker in st_speakers
        source_player = speaker_map.get(speaker, "")

        # ── Context gates ───────────────────────────────────────────────────────
        #
        # in_public_day: gate for kill-pressure, nomination, opposition patterns.
        #   STRICT: must be public audience_scope in Day phase with a public context_mode.
        #   This prevents ANY private-like or Night-phase segment from producing
        #   public-pressure events, ensuring N4 night-targets and N5 day-pressure
        #   are disjoint.
        in_public_day = (
            phase == "Day"
            and audience_scope == "public"
            and ctx_mode in _PUBLIC_DAY_CONTEXTS
        )

        # in_result_context: gate for execution result narration.
        #   Less strict: result narration can be in any Day phase context since
        #   ST may announce outcomes at various moments.
        in_result_context = (
            phase == "Day"
            and ctx_mode in _RESULT_NARRATION_CONTEXTS
        )

        # Skip if this segment doesn't qualify for any pattern type.
        if not in_public_day and not in_result_context:
            continue

        for pname in players:
            # Nomination reference: players nominate — exclude ST.
            # ST says things like "we're going to nominate X for execution" as narration,
            # but actual nominations come from players.
            if in_public_day and not is_st:
                for pat, conf in nomination_pats[pname]:
                    if pat.search(text):
                        events.append(_make_event(
                            t=t, speaker=speaker, source_player=source_player,
                            target=pname, roster=roster,
                            event_type="nomination_reference",
                            conf=conf, ctx_mode=ctx_mode, phase=phase, text=text,
                        ))
                        break  # one nomination event per player per segment

            # Public kill pressure: both players and ST (ST may echo town decision).
            if in_public_day:
                for pat, conf in kill_pressure_pats[pname]:
                    if pat.search(text):
                        events.append(_make_event(
                            t=t, speaker=speaker, source_player=source_player,
                            target=pname, roster=roster,
                            event_type="public_kill_pressure",
                            conf=conf, ctx_mode=ctx_mode, phase=phase, text=text,
                        ))
                        break

            # Execution opposition: players defend — exclude ST (ST is neutral arbiter).
            if in_public_day and not is_st:
                for pat, conf in opposition_pats[pname]:
                    if pat.search(text):
                        events.append(_make_event(
                            t=t, speaker=speaker, source_player=source_player,
                            target=pname, roster=roster,
                            event_type="execution_opposition",
                            conf=conf, ctx_mode=ctx_mode, phase=phase, text=text,
                        ))
                        break

            # Execution result narration: fire in any result context.
            # ST-anchor and execution_window/morning context each add a confidence boost.
            if in_result_context:
                for pat, conf in result_narration_pats[pname]:
                    if pat.search(text):
                        if is_st:
                            conf = min(1.0, conf + _ST_ANCHOR_BOOST)
                        if ctx_mode in ("execution_window", "morning_result_announcement"):
                            conf = min(1.0, conf + _CONTEXT_RESULT_BOOST)
                        events.append(_make_event(
                            t=t, speaker=speaker, source_player=source_player,
                            target=pname, roster=roster,
                            event_type="execution_result_narration",
                            conf=conf, ctx_mode=ctx_mode, phase=phase, text=text,
                        ))
                        break

    # Apply confidence threshold, then deduplicate.
    events = [e for e in events if e["confidence"] >= _MIN_CONF]
    events.sort(key=lambda e: e["timestamp_start"])
    deduped = _dedup_events(events)

    # Write output (always — even if empty, so downstream can detect N5 coverage).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(deduped)

    # Summary
    type_counts: dict[str, int] = {}
    for e in deduped:
        type_counts[e["event_type"]] = type_counts.get(e["event_type"], 0) + 1

    print(f"  Wrote {len(deduped)} events -> {out_path.name}")
    for etype, cnt in sorted(type_counts.items()):
        print(f"    {etype}: {cnt}")

    # Print representative examples for manual inspection
    if deduped:
        print("  Examples:")
        shown: dict[str, int] = {}
        for e in deduped:
            etype = e["event_type"]
            if shown.get(etype, 0) >= 2:
                continue
            shown[etype] = shown.get(etype, 0) + 1
            src = e["source_player_name"] or e["speaker"]
            print(
                f"    [{etype}] t={e['timestamp_start']:.0f}s  "
                f"{src} -> {e['target_player']}  "
                f"ctx={e['context_mode']}  conf={e['confidence']}"
            )
            print(f"      \"{e['source_text'][:80]}\"")

    return True


# ── Batch helpers ──────────────────────────────────────────────────────────────


def _all_video_ids() -> list[str]:
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
            and (OUTPUTS_DIR / e["id"] / "segments_patched.csv").exists()
        ]
    except Exception:
        return []


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="N5: Extract public execution-context events from BotC transcripts."
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
                errors += 1
        print(f"\nDone: {done} extracted, {skipped} skipped, {errors} errors.")
    elif args.video_id:
        print(f"=== extract_execution_context.py  {args.video_id} ===")
        try:
            extract(args.video_id, force=args.force)
        except Exception as exc:
            print(f"ERROR: {exc}")
            raise
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
