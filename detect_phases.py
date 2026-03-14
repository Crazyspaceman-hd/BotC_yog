"""detect_phases.py — N2 (phase_detection): Game-phase boundary detection (state-machine edition).

Reads segments_consistent.csv (or segments_patched.csv / segments.csv) and
produces phase_labels.csv labelling each interval of the episode as one of:

    Intro        — pre-game introductions (players announce their roles)
    Night        — storyteller resolves night actions
    Day          — open discussion phase
    Nomination   — nomination / voting window
    Execution    — execution announcement

Algorithm
---------
1. Identify the Storyteller speaker: dominant talker in first INTRO_CUTOFF_S
   seconds (word-count weighted).
2. Scan every segment for keyword patterns.  Signals are classified as
   strong (explicit state-change phrases, e.g. "everyone close your eyes")
   or weak (incidental references, e.g. "night one" in player discussion).
   Triggers are split into storyteller-only (storyteller_triggers) and
   all-speaker (all_triggers, non-ST downweighted by STORYTELLER_DOWNSCALE).
   Phase-conditional signals carry an allowed_source_phases set; they only
   contribute to scoring when current_phase is in that set.
3. Run a sequential state machine over all segment boundaries:
   a. Maintain current_phase and phase_start_t.
   b. At each interval midpoint, filter active triggers for the current
      source phase, then score all candidate phases via _score_window.
   c. Transition gating: only ALLOWED_TRANSITIONS from the current phase
      are eligible candidate next phases.
   d. Inertia: the current phase receives an INERTIA_BONUS on top of its
      raw evidence score.  A candidate must beat (current_score +
      INERTIA_BONUS) to trigger a switch — biasing toward staying.
   e. MIN_SCORE thresholds: strong cues require MIN_SCORE_STRONG, weak
      cues require MIN_SCORE_WEAK.  A candidate below its threshold is
      ignored even if it is the top scorer.
   f. Nomination timeout: if in Nomination for > MAX_NOMINATION_S with no
      fresh evidence in the last NOMINATION_REFRESH_S window, suppress
      Nomination so the machine can transition away.
   g. Intro stability: within INTRO_CUTOFF_S only storyteller_triggers are
      used; the machine stays in Intro until INTRO_CUTOFF_S elapses and
      silently promotes to Day.
4. Collapse consecutive same-phase intervals.
5. Absorb islands shorter than MIN_PHASE_S into their longer neighbour.
6. Final collapse pass.

Output columns: start, end, phase, confidence, evidence

Usage:
    python detect_phases.py <video_id>
    python detect_phases.py <video_id> --force
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

# ── Tuning constants ───────────────────────────────────────────────────────────
INTRO_CUTOFF_S        = 330.0   # intro region ends here
MIN_PHASE_S           = 30.0    # minimum island duration before absorbing
CONTEXT_S             = 20.0    # keyword context window (±seconds)
CONFIDENCE_DECAY_S    = 0.003   # confidence drop per second since last active signal
STORYTELLER_DOWNSCALE = 0.2     # weight multiplier for non-ST speakers on ST-only
                                 # signals; kept low to limit player-chatter phase drift

# Minimum absolute accumulated score for a phase to be a valid candidate.
# Strong cues (explicit announcements) set a lower bar; weak cues (reference
# phrases in discussion) must accumulate to overcome the higher threshold.
MIN_SCORE_STRONG = 0.20   # one nearby strong cue is sufficient
MIN_SCORE_WEAK   = 0.40   # weak cues must accumulate

# ── State machine constants ────────────────────────────────────────────────────
# Legal next phases from each current phase.  A candidate not in the
# allowed list is ignored even if it has a high evidence score.
# This encodes the standard BotC game arc:
#   Intro → Day → Nomination → Execution (opt) → Night → Day → ...
#
# Night → Nomination is also allowed as a shortcut for games where the Day
# between Night and Nominations is too brief to generate detectable evidence
# (e.g. the storyteller jumps straight from morning-reveal into nominations).
ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    "Intro":      ["Day", "Night"],
    "Day":        ["Nomination", "Night"],
    "Nomination": ["Execution", "Night", "Day"],   # Day: nomination may fail / resume
    "Execution":  ["Night", "Day"],
    "Night":      ["Day", "Nomination"],
}

# Raw-score bonus added to the current-phase score when comparing against
# a switch candidate.  A switch occurs only when:
#   candidate_score > current_score + INERTIA_BONUS
# This prevents single-event flips on noisy or low-evidence intervals.
INERTIA_BONUS = 0.15

# Reduced inertia bonus for phase-exit boundaries: when a STRONG cue targets
# a well-known phase boundary (Night→Day, Nomination→Execution, etc.) the
# regular INERTIA_BONUS can suppress an obvious exit signal if nearby
# in-phase evidence keeps the current score elevated.  INERTIA_BONUS_EXIT
# allows the exit signal to win over mild staying evidence without
# eliminating inertia entirely.
INERTIA_BONUS_EXIT = 0.02

# Phase pairs for which INERTIA_BONUS_EXIT applies when the best candidate
# has a strong cue.  These cover the sticky boundaries where in-phase
# evidence commonly competes with legitimate exit announcements.
_EXIT_INERTIA_PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("Night",      "Day"),
    ("Night",      "Nomination"),
    ("Nomination", "Execution"),
    ("Execution",  "Night"),
    ("Execution",  "Day"),
})

# Transitions that require at least one STRONG cue to fire.  Weak cues
# (player chatter, incidental phrases) will not trigger these even if they
# accumulate above MIN_SCORE_WEAK.  This prevents in-phase ST commentary
# (e.g. "good morning" addressed to a player mid-Nomination) from
# prematurely resetting a phase.
# Note: most morning/wakeup cues are already excluded from Nomination by their
# allowed_source_phases; this guard catches any remaining weak cues.
_STRONG_ONLY_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("Nomination", "Day"),   # needs an explicit ST day-boundary announcement
})

# Soft timeout for the Nomination phase.  If we have been in Nomination for
# longer than MAX_NOMINATION_S with no fresh nomination evidence in the last
# NOMINATION_REFRESH_S, suppress Nomination from the candidate pool so the
# machine can naturally transition away (handles long recap / outro tails).
MAX_NOMINATION_S     = 480.0   # 8 minutes
NOMINATION_REFRESH_S = 120.0   # 2-minute freshness window

# ── Structural (speaker-pattern) signal constants ─────────────────────────────
# Structural triggers are generated from speaker-count patterns in the segment
# data and added to all_triggers alongside keyword triggers.  They provide a
# phase signal even when the storyteller uses non-standard phrasing, eliminating
# the "no keywords → pure inertia" failure mode.
#
# Sampling math (CONTEXT_S=20, STRUCT_STEP_S=15):
#   At any interval_mid the 3 nearest structural samples (at t-15, t, t+15)
#   contribute weight × (0.25 + 1.0 + 0.25) = weight × 1.5 after decay.
#   Night: 0.30 × 1.5 = 0.45  ≥  MIN_SCORE_WEAK (0.40) — fires after ~30s
#   Day:   0.25 × 1.5 = 0.375 — combines with keyword Day signals to exceed 0.40
#   A single isolated structural trigger (0.30) stays below MIN_SCORE_WEAK and
#   cannot flip the machine alone; MIN_PHASE_S=30s absorbs any remaining islands.
STRUCT_STEP_S  = 15.0   # sampling interval for structural trigger generation (s)
STRUCT_NIGHT_W = 0.30   # Night trigger weight: ST + 1 player window
STRUCT_DAY_W   = 0.25   # Day trigger weight: ≥2 non-ST speakers in window

# ── Cue-cluster constants ──────────────────────────────────────────────────────
# When a _FROM_NIGHT_ONLY restricted Day cue (e.g. "welcome back to town") co-
# occurs with explicit death-confirmation language during Nomination, the
# combination strongly implies an execution just resolved and Day has resumed.
# Injecting CUE_CLUSTER_BONUS into Day (marked strong) lets the restricted cue
# still drive Nomination→Day without relaxing the source-phase restriction that
# prevents the false-fire regression in DzTk6kSIg-M.
CUE_CLUSTER_BONUS = 0.30

# ── Boundary-sharpening constants ─────────────────────────────────────────────
# When sharpening Night/Day boundary timestamps we search this far either side
# of the NLP-estimated boundary for a matching header-visibility transition.
SHARPEN_SEARCH_S = 90.0

# Storyteller phrases that mark the exact start of Night (header goes invisible).
_NIGHT_START_RE: re.Pattern = re.compile(
    r"\b(?:"
    r"close your eyes"
    r"|everyone close"
    r"|go to sleep"
    r"|night (?:one|two|three|four|five|six|seven|eight|nine|ten|\d+) begins"
    r"|night falls"
    r"|shut your eyes"
    r"|time to sleep"
    r")\b",
    re.I,
)

# Storyteller phrases that mark the exact start of Day (header reappears).
_DAY_START_RE: re.Pattern = re.compile(
    r"\b(?:"
    r"good morning"
    r"|open your eyes"
    r"|welcome back to (?:the )?town"
    r"|sun rises"
    r"|day (?:one|two|three|four|five|six|seven|eight|nine|ten|\d+) begins"
    r"|wake up"
    r")\b",
    re.I,
)

_DEATH_LANGUAGE_RE: re.Pattern = re.compile(
    r"\b(?:"
    r"you died"
    r"|(?:he|she|they|it)(?:'s| is| was) (?:now )?dead"
    r"|i(?:'m| am) (?:now )?dead"
    r"|(?:has|have) been (?:killed|executed)"
    r"|there(?:'s| is| was| has been) a death"
    r")\b",
    re.I,
)

# ── Signal table ───────────────────────────────────────────────────────────────
# Each entry is a tuple:
#   (pattern, target_phase, weight, storyteller_only, strong, allowed_source_phases)
#
# target_phase          — which game phase this signal votes for
#
# weight                — base score contribution (linearly decayed by distance
#                         within CONTEXT_S; non-ST speakers on ST-only signals
#                         are further multiplied by STORYTELLER_DOWNSCALE)
#
# storyteller_only      — if True, non-ST speakers receive weight × STORYTELLER_DOWNSCALE.
#                         Only ST triggers are evaluated inside the intro window.
#
# strong                — if True, an explicit, unambiguous phase-boundary ANNOUNCEMENT.
#                         Fires at MIN_SCORE_STRONG (low bar; one nearby cue suffices).
#                         If False, an incidental / discussion-phase reference that
#                         fires at the higher MIN_SCORE_WEAK (requires accumulation).
#
# allowed_source_phases — optional frozenset of current-phase values from which
#                         this signal is permitted to contribute.  When the machine
#                         is NOT in one of these phases the signal is excluded from
#                         the active trigger pool before scoring.
#                         None = fires from any source phase (default behaviour).
#                         Use this for cues that are unambiguous on one boundary
#                         but ambiguous on another (e.g. "welcome back to town"
#                         marks Night→Day correctly but is also a casual greeting
#                         mid-Nomination in some episodes).

# Convenience constants for common source-phase restrictions:
_FROM_NIGHT_ONLY: frozenset[str] = frozenset({"Night", "Intro"})
# ^ Morning/wake cues: only meaningful when exiting Night (or Intro Night-0 setup).
#   Excluded when the machine is already in Day, Nomination, or Execution.

_FROM_NOMINATION_ONLY: frozenset[str] = frozenset({"Nomination"})
# ^ Vote-outcome cues: only arise immediately after a Nomination vote resolves.

_SIGNALS: list[tuple[re.Pattern, str, float, bool, bool, frozenset[str] | None]] = []


def _sig(
    pattern: str,
    target_phase: str,
    weight: float = 1.0,
    storyteller_only: bool = False,
    strong: bool = False,
    allowed_source_phases: frozenset[str] | None = None,
) -> None:
    _SIGNALS.append((
        re.compile(pattern, re.I),
        target_phase,
        weight,
        storyteller_only,
        strong,
        allowed_source_phases,
    ))


# ── Night ──────────────────────────────────────────────────────────────────────
# Strong: unambiguous state-transition announcements (ST class A/B cues)
_sig(r"\bit(?:'s| is) now night\b",                      "Night", 1.0, storyteller_only=True,  strong=True)
_sig(r"\beveryone (?:close|shut) your eyes\b",           "Night", 0.9, storyteller_only=True,  strong=True)
_sig(r"\bclose your eyes\b",                             "Night", 0.8, storyteller_only=True,  strong=True)
_sig(r"\bnight (?:begins|starts|falls?|time)\b",         "Night", 0.8, storyteller_only=True,  strong=True)
_sig(r"\bsun(?:set|down)\b",                             "Night", 0.7, storyteller_only=True,  strong=True)
_sig(r"\bgo to sleep\b",                                 "Night", 0.8, storyteller_only=True,  strong=True)
# Weak: reference phrases (players mention these during day discussion)
_sig(r"\bnight (?:one|two|three|four|five|six|seven|eight|nine|\d+)\b",
                                                          "Night", 0.6, storyteller_only=True,  strong=False)
_sig(r"\bgood night\b",                                  "Night", 0.4, storyteller_only=True,  strong=False)

# ── Day ────────────────────────────────────────────────────────────────────────
# Strong, unrestricted: these explicit announcements are unambiguous across all
# source phases — they can legitimately drive Day from Nomination (failed vote)
# or Execution (game continues) as well as from Night.
_sig(r"\bit(?:'s| is) now day\b",                        "Day",   1.0, storyteller_only=True,  strong=True)
_sig(r"\bday (?:begins|starts|time)\b",                  "Day",   0.8, storyteller_only=True,  strong=True)
_sig(r"\bthe sun rises?\b",                              "Day",   0.8, storyteller_only=True,  strong=True)
_sig(r"\bsunrise\b",                                     "Day",   0.7, storyteller_only=True,  strong=True)
_sig(r"\bno(?:body| one) (?:died|was killed|was murdered)\b",
                                                          "Day",   0.9, storyteller_only=True,  strong=True)
_sig(r"\b(?:someone|a player) (?:died|was found dead|was killed)\b",
                                                          "Day",   0.8, storyteller_only=True,  strong=True)
# Weak: reference phrases
_sig(r"\bday (?:one|two|three|four|five|six|seven|eight|nine|\d+)\b",
                                                          "Day",   0.6, storyteller_only=True,  strong=False)
_sig(r"\bgood morning\b",                                "Day",   0.5, storyteller_only=True,  strong=False)
_sig(r"\bwake up\b",                                     "Day",   0.4, storyteller_only=True,  strong=False)
# Strong, night-exit only: morning/wakeup phrases that are unambiguous as a
# Night→Day boundary marker, but are also used as casual greetings mid-Nomination
# or mid-Execution in some episodes.  Restricting them to _FROM_NIGHT_ONLY
# prevents a Nomination-phase "welcome back to town" from erroneously driving
# a Nomination→Day transition (regression seen in DzTk6kSIg-M).
_sig(r"\beveryone (?:open|opens?) your eyes\b",          "Day",   0.9, storyteller_only=True,  strong=True,
     allowed_source_phases=_FROM_NIGHT_ONLY)
_sig(r"\brise and shine\b",                              "Day",   0.8, storyteller_only=True,  strong=True,
     allowed_source_phases=_FROM_NIGHT_ONLY)
_sig(r"\bwelcome back to town\b",                        "Day",   0.9, storyteller_only=True,  strong=True,
     allowed_source_phases=_FROM_NIGHT_ONLY)
_sig(r"\bthe night(?:'s| is| was) over\b",               "Day",   0.9, storyteller_only=False, strong=True,
     allowed_source_phases=_FROM_NIGHT_ONLY)
_sig(r"\bdawn (?:breaks?|has (?:come|broken))\b",        "Day",   0.8, storyteller_only=True,  strong=True,
     allowed_source_phases=_FROM_NIGHT_ONLY)
_sig(r"\bit(?:'s| is) (?:now )?(?:the )?morning\b",      "Day",   0.8, storyteller_only=True,  strong=True,
     allowed_source_phases=_FROM_NIGHT_ONLY)
# Weak accumulation signals: players referencing the outcome of the previous night.
# Unrestricted — these appear naturally in Day and Nomination discussion alike.
_sig(r"\blast night\b",                                  "Day",   0.35, storyteller_only=False, strong=False)
_sig(r"\bovernight\b",                                   "Day",   0.30, storyteller_only=False, strong=False)

# ── Nomination ─────────────────────────────────────────────────────────────────
# Strong: ST procedural cues
_sig(r"\bnominations?(?: are| is)? open\b",              "Nomination", 1.0, storyteller_only=True,  strong=True)
_sig(r"\bvote(?:s)?(?: are| is)? open\b",                "Nomination", 0.9, storyteller_only=True,  strong=True)
_sig(r"\btime to (?:vote|nominate)\b",                   "Nomination", 0.8, storyteller_only=True,  strong=True)
_sig(r"\bwho (?:do you |would you )?nominate\b",         "Nomination", 0.7, storyteller_only=True,  strong=True)
# Weak: player-initiated (valid but require accumulation)
_sig(r"\bi nominate\b",                                  "Nomination", 0.6, storyteller_only=False, strong=False)
_sig(r"\bput(?:ting)? (?:\w+ )?on the block\b",          "Nomination", 0.5, storyteller_only=False, strong=False)

# ── Execution ──────────────────────────────────────────────────────────────────
# Core verdict / confirmation phrases: unambiguous regardless of source phase.
_sig(r"\bhas been executed\b",                           "Execution", 1.0, storyteller_only=True,  strong=True)
_sig(r"\byou(?:'ve| have) been executed\b",              "Execution", 1.0, storyteller_only=True,  strong=True)
_sig(r"\bwas executed\b",                                "Execution", 0.9, storyteller_only=True,  strong=True)
_sig(r"\bexecution (?:is )?complete\b",                  "Execution", 0.9, storyteller_only=True,  strong=True)
_sig(r"\bput to death\b",                                "Execution", 0.8, storyteller_only=True,  strong=True)
_sig(r"\bthe vote (?:passed|carries)\b",                 "Execution", 0.7, storyteller_only=True,  strong=True)
_sig(r"\bis now dead\b",                                 "Execution", 0.8, storyteller_only=True,  strong=True)
# Vote-outcome cues, nomination-exit only: these phrases could plausibly appear
# in post-game discussion from phases other than Nomination; restricting them
# to _FROM_NOMINATION_ONLY avoids spurious Execution islands outside vote windows.
_sig(r"\bvoted (?:out|off)\b",                           "Execution", 0.8, storyteller_only=False, strong=True,
     allowed_source_phases=_FROM_NOMINATION_ONLY)
_sig(r"\bgets? (?:executed|the axe)\b",                  "Execution", 0.8, storyteller_only=True,  strong=True,
     allowed_source_phases=_FROM_NOMINATION_ONLY)


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
    word_counts: Counter = Counter()
    for row in rows:
        if float(row["start"]) >= INTRO_CUTOFF_S:
            break
        word_counts[row["speaker"]] += len(row["text"].split())
    if not word_counts:
        return None
    return word_counts.most_common(1)[0][0]


# Trigger tuple: (timestamp, target_phase, effective_weight, evidence_text,
#                 is_strong, allowed_source_phases)
_Trigger = tuple[float, str, float, str, bool, frozenset[str] | None]


def _collect_triggers(
    rows: list[dict],
    storyteller_id: str | None,
) -> tuple[list[_Trigger], list[_Trigger]]:
    """Scan segments and return two trigger lists.

    storyteller_triggers — only signals from the identified storyteller speaker,
                           at full weight.  Used exclusively in the intro window.

    all_triggers         — signals from every speaker; non-ST speakers are
                           downweighted by STORYTELLER_DOWNSCALE for ST-only
                           signal patterns.

    Each trigger: (timestamp, target_phase, effective_weight, evidence_text,
                   is_strong, allowed_source_phases)
    """
    storyteller_triggers: list[_Trigger] = []
    all_triggers:         list[_Trigger] = []
    for row in rows:
        segment_time   = float(row["start"])
        segment_text   = row["text"]
        speaker_id     = row["speaker"]
        is_storyteller = (speaker_id == storyteller_id) if storyteller_id else False
        for pattern, target_phase, weight, storyteller_only, is_strong, allowed_source_phases \
                in _SIGNALS:
            if pattern.search(segment_text):
                effective_weight = (
                    weight if (not storyteller_only or is_storyteller)
                    else weight * STORYTELLER_DOWNSCALE
                )
                trigger: _Trigger = (
                    segment_time,
                    target_phase,
                    effective_weight,
                    segment_text[:80].replace("\n", " "),
                    is_strong,
                    allowed_source_phases,
                )
                all_triggers.append(trigger)
                if is_storyteller:
                    storyteller_triggers.append(trigger)
    return storyteller_triggers, all_triggers


def _score_window(
    midpoint_t: float,
    triggers: list[_Trigger],
) -> dict[str, tuple[float, bool, str]]:
    """Accumulate trigger scores within CONTEXT_S of midpoint_t.

    Returns {target_phase: (raw_score, has_strong_cue, best_evidence_text)}.

    Does NOT apply MIN_SCORE thresholds — the state machine decides whether
    the accumulated score is sufficient to act on.  The caller is responsible
    for checking MIN_SCORE_STRONG vs MIN_SCORE_WEAK based on has_strong_cue.

    Triggers are expected to be pre-filtered by the caller for source-phase
    compatibility (allowed_source_phases); this function scores what it receives.
    """
    phase_scores:    dict[str, float] = {}
    phase_has_strong: dict[str, bool]  = {}
    phase_evidence:  dict[str, str]   = {}
    for trigger_time, target_phase, weight, evidence_text, is_strong, *_ in triggers:
        time_delta = abs(midpoint_t - trigger_time)
        if time_delta <= CONTEXT_S:
            decayed_score = weight * (1.0 - time_delta / CONTEXT_S)
            phase_scores[target_phase] = (
                phase_scores.get(target_phase, 0.0) + decayed_score
            )
            if is_strong and not phase_has_strong.get(target_phase):
                phase_has_strong[target_phase] = True
            if decayed_score > 0.05 and target_phase not in phase_evidence:
                phase_evidence[target_phase] = evidence_text
    return {
        phase: (
            phase_scores[phase],
            phase_has_strong.get(phase, False),
            phase_evidence.get(phase, ""),
        )
        for phase in phase_scores
    }


# ── Structural speaker-pattern helpers ────────────────────────────────────────

def _speakers_in_window(
    t: float,
    rows: list[dict],
    half_window_s: float,
) -> set[str]:
    """Unique speaker IDs whose segment overlaps [t - half_window, t + half_window]."""
    return {
        row["speaker"]
        for row in rows
        if float(row["start"]) < t + half_window_s
        and float(row["end"]) > t - half_window_s
    }


def _structural_triggers(
    rows: list[dict],
    storyteller_id: str | None,
    video_duration_s: float,
) -> list[_Trigger]:
    """Generate phase-vote triggers from speaker-count patterns in segment data.

    Sampled every STRUCT_STEP_S seconds starting from t=0.  Triggers land in
    all_triggers (not storyteller_triggers) so they are automatically suppressed
    within the intro window by the `raw_triggers` selection in the state machine.

    Two patterns are detected:

      Night  — exactly 2 speakers active (ST + 1 player, 1:1 consultation).
               Sustained Night phases accumulate well above MIN_SCORE_WEAK;
               brief mid-Day 1:1 chat (<30 s) scores below the threshold and
               is absorbed by MIN_PHASE_S even if it briefly wins.

      Day    — ≥2 non-ST speakers active (group discussion / side convos).
               Combines with keyword Day signals; on its own (0.375) is just
               below MIN_SCORE_WEAK, so a minimal Day keyword cue is still
               needed to confirm the transition.

    Nomination/Execution are not targeted here; both phases look like large
    multi-speaker windows and are better distinguished by keyword signals.
    """
    if not storyteller_id:
        return []
    triggers: list[_Trigger] = []
    t = 0.0
    while t <= video_duration_s:
        active   = _speakers_in_window(t, rows, STRUCT_STEP_S)
        n_total  = len(active)
        st_here  = storyteller_id in active
        n_non_st = n_total - (1 if st_here else 0)

        if n_total == 2 and st_here:
            triggers.append((
                t, "Night", STRUCT_NIGHT_W,
                "structural: 2-speaker window (ST + 1 player)",
                False, None,
            ))

        if n_non_st >= 2:
            triggers.append((
                t, "Day", STRUCT_DAY_W,
                f"structural: {n_non_st} non-ST speakers in window",
                False, None,
            ))

        t += STRUCT_STEP_S
    return triggers


# ── State machine ──────────────────────────────────────────────────────────────

def _apply_state_machine(
    rows:                 list[dict],
    storyteller_triggers: list[_Trigger],
    all_triggers:         list[_Trigger],
) -> list[tuple[float, float, str, float, str]]:
    """Sequentially label intervals using a transition-gated state machine.

    Key behaviours
    --------------
    Intro stability
        Within INTRO_CUTOFF_S only storyteller_triggers are evaluated.  Player
        chatter ("night one" in role announcements) cannot end Intro.  Past the
        cutoff the machine silently promotes Intro to Day.

    Phase-conditional signal filtering
        Before scoring each interval, triggers whose allowed_source_phases does
        not include current_phase are excluded.  This prevents ambiguous cues
        (e.g. "welcome back to town" used as a casual mid-Nomination greeting)
        from influencing scoring in phases where they are not meaningful.

    Transition gating
        Only phases in ALLOWED_TRANSITIONS[current_phase] are considered as
        switch candidates.  Illegal jumps (e.g. Night→Execution) are ignored.

    Inertia
        The current phase receives INERTIA_BONUS added to its raw evidence
        score.  A candidate must beat (current_score + INERTIA_BONUS) to
        trigger a switch.  When no active evidence supports the current phase
        (raw score = 0) the effective bar is just INERTIA_BONUS (0.15), so a
        single strong cue still fires cleanly.

    Nomination timeout
        After MAX_NOMINATION_S in Nomination with no fresh evidence in the
        last NOMINATION_REFRESH_S window, Nomination is suppressed from the
        candidate pool.  This handles long recap/outro tails where nomination
        references dried up after the real nominations ended.

    Low-ST fallback
        With few ST cues the machine stays in broad Day/Night blocks rather
        than churning on noisy accumulated player references.
    """
    all_boundaries = sorted(
        {float(r["start"]) for r in rows} | {float(r["end"]) for r in rows}
    )

    current_phase = "Intro"
    phase_start_t = 0.0
    labeled_intervals: list[tuple[float, float, str, float, str]] = []

    for i in range(len(all_boundaries) - 1):
        seg_start    = all_boundaries[i]
        seg_end      = all_boundaries[i + 1]
        interval_mid = (seg_start + seg_end) / 2.0

        # ── Intro past cutoff: silently promote to Day ────────────────────────
        # Once INTRO_CUTOFF_S has elapsed and we are still in Intro, assume Day
        # has begun.  This ensures a sane baseline even with zero ST cues.
        if current_phase == "Intro" and seg_start >= INTRO_CUTOFF_S:
            current_phase = "Day"
            phase_start_t = seg_start

        # ── Select trigger pool ───────────────────────────────────────────────
        # Inside the intro window only the storyteller's triggers are used so
        # that player role-announcement chatter cannot perturb phase detection
        # before the game has formally started.
        raw_triggers = (
            storyteller_triggers if interval_mid < INTRO_CUTOFF_S else all_triggers
        )

        # ── Filter for source-phase compatibility ─────────────────────────────
        # Exclude triggers whose allowed_source_phases does not include the
        # current phase.  This is the phase-conditional signal mechanism: a cue
        # that is only meaningful on a specific boundary (e.g. "welcome back to
        # town" as a Night→Day marker) is suppressed when the machine is in a
        # phase where the cue is ambiguous (e.g. Nomination).
        active_triggers = [
            trig for trig in raw_triggers
            if trig[5] is None or current_phase in trig[5]
        ]

        # ── Score all phases at this midpoint ─────────────────────────────────
        phase_scores = _score_window(interval_mid, active_triggers)

        # ── Nomination timeout suppression ────────────────────────────────────
        if (current_phase == "Nomination"
                and (interval_mid - phase_start_t) > MAX_NOMINATION_S):
            nomination_is_fresh = any(
                True
                for trig in all_triggers
                if trig[1] == "Nomination"
                and (interval_mid - NOMINATION_REFRESH_S) <= trig[0] <= interval_mid
            )
            if not nomination_is_fresh:
                phase_scores.pop("Nomination", None)

        # ── Cue-cluster: restricted Day cue + death reveal → Nomination→Day ──
        # "welcome back to town" et al. are _FROM_NIGHT_ONLY to block the
        # DzTk6kSIg-M false-fire (casual mid-Nomination use with no death).
        # When the phrase co-occurs with death-confirmation language (e.g.
        # "you died" / "I'm dead") the combination marks a genuine execution
        # reveal; inject CUE_CLUSTER_BONUS so Day can still win (e.g. _EGJi3wg0bE).
        if current_phase == "Nomination":
            restricted_day_cue_nearby = any(
                trig[1] == "Day"
                and trig[5] is not None
                and current_phase not in trig[5]
                and abs(interval_mid - trig[0]) <= CONTEXT_S
                for trig in raw_triggers
            )
            if restricted_day_cue_nearby:
                death_reveal_nearby = any(
                    _DEATH_LANGUAGE_RE.search(row["text"])
                    for row in rows
                    if abs(interval_mid - float(row["start"])) <= CONTEXT_S
                )
                if death_reveal_nearby:
                    existing_sc, _, existing_ev = phase_scores.get(
                        "Day", (0.0, False, "")
                    )
                    phase_scores["Day"] = (
                        existing_sc + CUE_CLUSTER_BONUS,
                        True,
                        existing_ev or "co-occurrence: day-start cue + death reveal",
                    )

        # ── Intro window: fully locked until INTRO_CUTOFF_S ──────────────────
        # Intro is treated as a fixed block; no phase transitions are evaluated
        # within it.  This prevents both player chatter ("night one" during
        # role announcements) AND storyteller Night-0 setup phrases ("go to
        # sleep" before Day 1) from fragmenting or prematurely ending Intro.
        # Phase detection only begins once INTRO_CUTOFF_S has elapsed and the
        # machine silently promotes Intro → Day above.
        if interval_mid < INTRO_CUTOFF_S and current_phase == "Intro":
            labeled_intervals.append(
                (seg_start, seg_end, "Intro", 0.9, "before intro cutoff")
            )
            continue

        # ── Post-intro state machine ──────────────────────────────────────────
        # Find the best legal candidate phase from ALLOWED_TRANSITIONS.
        candidate_phases         = ALLOWED_TRANSITIONS.get(current_phase, [])
        best_candidate_phase     = None
        best_candidate_score     = 0.0
        best_candidate_evidence  = ""
        best_candidate_is_strong = False
        for candidate_phase in candidate_phases:
            if candidate_phase not in phase_scores:
                continue
            cand_score, cand_has_strong, cand_evidence = phase_scores[candidate_phase]
            min_score = MIN_SCORE_STRONG if cand_has_strong else MIN_SCORE_WEAK
            if cand_score >= min_score and cand_score > best_candidate_score:
                best_candidate_phase     = candidate_phase
                best_candidate_score     = cand_score
                best_candidate_evidence  = cand_evidence
                best_candidate_is_strong = cand_has_strong

        # Enforce strong-only constraint on certain transitions.
        # Some phase exits (e.g. Nomination→Day) must not be triggered by weak
        # cues alone (e.g. ST saying "good morning" to a player mid-Nomination).
        # Only a STRONG cue — an unambiguous boundary announcement such as
        # "nobody died" — may fire these transitions.  Most morning/wakeup cues
        # are already excluded from Nomination by their allowed_source_phases;
        # this guard catches any remaining weak cues that slip through.
        if (best_candidate_phase is not None
                and not best_candidate_is_strong
                and (current_phase, best_candidate_phase) in _STRONG_ONLY_TRANSITIONS):
            best_candidate_phase     = None
            best_candidate_score     = 0.0
            best_candidate_evidence  = ""
            best_candidate_is_strong = False

        # Current phase score (raw, before inertia bonus).
        current_phase_score, _, current_phase_evidence = phase_scores.get(
            current_phase, (0.0, False, "")
        )

        # Choose the inertia bonus.
        # For known phase-exit boundaries where a STRONG candidate exists,
        # use INERTIA_BONUS_EXIT (≈0) so that nearby in-phase evidence
        # (e.g. Night cues from ongoing resolution) cannot suppress an
        # obvious exit announcement (e.g. "welcome back to town" = Day start).
        # For all other cases keep INERTIA_BONUS to prevent noisy flips.
        is_exit_boundary = (
            best_candidate_is_strong
            and best_candidate_phase is not None
            and (current_phase, best_candidate_phase) in _EXIT_INERTIA_PAIRS
        )
        inertia = current_phase_score + (
            INERTIA_BONUS_EXIT if is_exit_boundary else INERTIA_BONUS
        )

        if best_candidate_phase is not None and best_candidate_score > inertia:
            # ── Switch ────────────────────────────────────────────────────────
            current_phase = best_candidate_phase
            phase_start_t = seg_start
            total_score   = sum(s for s, _, _ in phase_scores.values()) or 1e-9
            confidence    = round(min(best_candidate_score / total_score, 1.0), 3)
            labeled_intervals.append(
                (seg_start, seg_end, current_phase, confidence, best_candidate_evidence)
            )
        else:
            # ── Stay ──────────────────────────────────────────────────────────
            if current_phase_score > 0.0:
                total_score = sum(s for s, _, _ in phase_scores.values()) or 1e-9
                confidence  = round(min(current_phase_score / total_score, 1.0), 3)
                evidence    = current_phase_evidence
            else:
                phase_age  = interval_mid - phase_start_t
                confidence = max(0.05, 0.6 - phase_age * CONFIDENCE_DECAY_S)
                evidence   = f"inferred from previous {current_phase}"
            labeled_intervals.append(
                (seg_start, seg_end, current_phase, round(confidence, 3), evidence)
            )

    return labeled_intervals


# ── Post-processing ────────────────────────────────────────────────────────────

def _merge_short(
    phase_intervals: list[tuple[float, float, str, float, str]],
    min_duration_s: float,
) -> list[tuple[float, float, str, float, str]]:
    """Absorb islands shorter than min_duration_s into their longer neighbour."""
    changed = True
    result  = list(phase_intervals)
    while changed:
        changed = False
        merged: list[tuple] = []
        i = 0
        while i < len(result):
            iv_start, iv_end, iv_phase, iv_conf, iv_ev = result[i]
            if (iv_end - iv_start) < min_duration_s and len(result) > 1:
                prev_dur = (merged[-1][1] - merged[-1][0]) if merged else -1
                next_dur = (
                    (result[i + 1][1] - result[i + 1][0])
                    if i + 1 < len(result) else -1
                )
                if merged and prev_dur >= next_dur:
                    prev_start, prev_end, prev_phase, prev_conf, prev_ev = merged.pop()
                    merged.append((prev_start, iv_end, prev_phase, prev_conf, prev_ev))
                elif i + 1 < len(result):
                    next_start, next_end, next_phase, next_conf, next_ev = result[i + 1]
                    merged.append((iv_start, next_end, next_phase, next_conf, next_ev))
                    i += 1
                else:
                    merged.append(result[i])
                changed = True
            else:
                merged.append(result[i])
            i += 1
        result = merged
    return result


def _collapse(
    phase_intervals: list[tuple[float, float, str, float, str]],
) -> list[tuple[float, float, str, float, str]]:
    """Merge consecutive same-phase intervals."""
    if not phase_intervals:
        return phase_intervals
    out = [phase_intervals[0]]
    for iv_start, iv_end, iv_phase, iv_conf, iv_ev in phase_intervals[1:]:
        prev_start, prev_end, prev_phase, prev_conf, prev_ev = out[-1]
        if iv_phase == prev_phase:
            out[-1] = (
                prev_start, iv_end, prev_phase,
                round((prev_conf + iv_conf) / 2, 3),
                prev_ev,
            )
        else:
            out.append((iv_start, iv_end, iv_phase, iv_conf, iv_ev))
    return out


# ── Visual ground-truth helpers (frame_scan.json) ─────────────────────────────

def _load_votes_windows(out_dir: Path, interval_s: float = 30.0) -> list[tuple[float, float]]:
    """Return merged time windows (start, end) where votes_visible=True.

    Each frame-scan sample represents ~interval_s seconds.  Consecutive True
    samples are merged into one window; a gap of ≤ interval_s between True
    samples is also bridged (one missed frame does not split a nomination run).
    Returns [] if frame_scan.json is absent or has no votes_visible frames.
    """
    fscan = out_dir / "frame_scan.json"
    if not fscan.exists():
        return []
    data = json.loads(fscan.read_text(encoding="utf-8"))
    interval_s = float(data.get("sample_interval_s", interval_s))
    votes_times = sorted(
        float(f["t"]) for f in data.get("frames", []) if f.get("votes_visible")
    )
    if not votes_times:
        return []

    half = interval_s / 2.0
    # Bridge gaps ≤ 3 samples (90 s at default 30 s interval).
    # The votes table can disappear briefly mid-nomination (inventory, camera
    # pan, etc.) without the nomination actually ending.  Genuine separate
    # nominations in BotC are always separated by several minutes of discussion,
    # so anything within 90 s is the same event.
    bridge = interval_s * 3
    windows: list[list[float]] = [[votes_times[0] - half, votes_times[0] + half]]
    for t in votes_times[1:]:
        w_start = t - half
        w_end   = t + half
        if w_start <= windows[-1][1] + bridge:
            windows[-1][1] = max(windows[-1][1], w_end)
        else:
            windows.append([w_start, w_end])
    return [(max(0.0, s), e) for s, e in windows]


def _apply_votes_overrides(
    labeled_intervals: list[tuple[float, float, str, float, str]],
    votes_windows: list[tuple[float, float]],
) -> list[tuple[float, float, str, float, str]]:
    """Hard-stamp intervals overlapping votes_windows as Nomination (conf=1.0).

    Each overlapping interval is split into up to three pieces:
      [pre-window part]  original phase
      [window overlap]   Nomination, confidence=1.0
      [post-window part] original phase
    Applied after all NLP post-processing so _merge_short cannot absorb them.
    """
    if not votes_windows:
        return labeled_intervals

    result: list[tuple[float, float, str, float, str]] = []
    for iv_start, iv_end, iv_phase, iv_conf, iv_ev in labeled_intervals:
        overlaps = [
            (max(ws, iv_start), min(we, iv_end))
            for ws, we in votes_windows
            if ws < iv_end and we > iv_start
        ]
        if not overlaps:
            result.append((iv_start, iv_end, iv_phase, iv_conf, iv_ev))
            continue

        cursor = iv_start
        for ov_start, ov_end in sorted(overlaps):
            if cursor < ov_start:
                result.append((cursor, ov_start, iv_phase, iv_conf, iv_ev))
            result.append((
                ov_start, ov_end,
                "Nomination", 1.0, "frame_scan: votes table visible",
            ))
            cursor = ov_end
        if cursor < iv_end:
            result.append((cursor, iv_end, iv_phase, iv_conf, iv_ev))

    return result


# ── Boundary sharpening (frame_scan header + ST keywords) ─────────────────────

def _load_frame_scan(out_dir: Path) -> list[dict]:
    """Return frame_scan samples sorted by time, or [] if absent."""
    fscan = out_dir / "frame_scan.json"
    if not fscan.exists():
        return []
    data = json.loads(fscan.read_text(encoding="utf-8"))
    return sorted(data.get("frames", []), key=lambda f: float(f["t"]))


def _header_transitions(samples: list[dict]) -> list[dict]:
    """Return header-visibility transitions from sorted frame_scan samples.

    Each entry: {type, t_low, t_high} where [t_low, t_high] is the 30-second
    window in which the transition occurred.

    type='night_start' — header went True → False (game went to Night)
    type='day_start'   — header went False → True (game returned to Day)
    """
    transitions = []
    for i in range(1, len(samples)):
        prev_hdr = bool(samples[i - 1].get("header_visible"))
        curr_hdr = bool(samples[i].get("header_visible"))
        t_low    = float(samples[i - 1]["t"])
        t_high   = float(samples[i]["t"])
        if prev_hdr and not curr_hdr:
            transitions.append({"type": "night_start", "t_low": t_low, "t_high": t_high})
        elif not prev_hdr and curr_hdr:
            transitions.append({"type": "day_start",   "t_low": t_low, "t_high": t_high})
    return transitions


def _sharpen_boundaries(
    labeled_intervals: list[tuple[float, float, str, float, str]],
    frame_scan_samples: list[dict],
    rows: list[dict],
    storyteller_id: str,
    search_s: float = SHARPEN_SEARCH_S,
) -> list[tuple[float, float, str, float, str]]:
    """Sharpen Night/Day boundary timestamps using header transitions + ST keywords.

    For each Night or Day interval start:
      1. Find the nearest header-visibility transition of the right type within
         ±search_s of the NLP-estimated boundary.
      2. Within that 30-second transition window (± a 30 s slack), search for a
         Storyteller segment matching the phase-appropriate keyword regex.
      3. If a keyword is found, use its timestamp as the precise boundary.
         Otherwise fall back to the midpoint of the transition window.

    Adjusts both the end of the preceding interval and the start of the current
    one so the total timeline remains gapless.  Returns a new interval list.
    """
    if not frame_scan_samples:
        return labeled_intervals

    transitions = _header_transitions(frame_scan_samples)
    if not transitions:
        return labeled_intervals

    # Storyteller segments only — we search these for keyword timestamps.
    st_segs = sorted(
        (r for r in rows if r.get("speaker") == storyteller_id),
        key=lambda r: float(r["start"]),
    )

    def _keyword_t(
        pattern: re.Pattern,
        t_low: float,
        t_high: float,
        slack: float = 30.0,
    ) -> float | None:
        lo, hi = t_low - slack, t_high + slack
        for seg in st_segs:
            t = float(seg["start"])
            if t < lo:
                continue
            if t > hi:
                break
            if pattern.search(seg.get("text", "")):
                return t
        return None

    ivs = list(labeled_intervals)
    for i in range(1, len(ivs)):
        _, _, prev_phase, prev_conf, prev_ev = ivs[i - 1]
        curr_start, curr_end, curr_phase, curr_conf, curr_ev = ivs[i]

        # Only sharpen the start of Night and Day intervals.
        if curr_phase == "Night":
            trans_type = "night_start"
            kw_pattern = _NIGHT_START_RE
        elif curr_phase == "Day":
            trans_type = "day_start"
            kw_pattern = _DAY_START_RE
        else:
            continue

        # Find the nearest matching header transition within ±search_s.
        best_trans = min(
            (t for t in transitions if t["type"] == trans_type),
            key=lambda t: abs((t["t_low"] + t["t_high"]) / 2 - curr_start),
            default=None,
        )
        if best_trans is None:
            continue
        mid = (best_trans["t_low"] + best_trans["t_high"]) / 2.0
        if abs(mid - curr_start) > search_s:
            continue  # nearest transition is too far away

        # Try to find a precise Storyteller keyword within the window.
        kw_t = _keyword_t(kw_pattern, best_trans["t_low"], best_trans["t_high"])
        if kw_t is not None:
            new_t   = kw_t
            new_ev  = curr_ev + f" (sharpened: ST keyword at {kw_t:.1f}s)"
        else:
            new_t   = mid
            new_ev  = (
                curr_ev
                + f" (sharpened: header {best_trans['t_low']:.0f}–{best_trans['t_high']:.0f}s)"
            )

        # Clamp: must stay within the bounds of both adjacent intervals.
        prev_start = ivs[i - 1][0]
        new_t = max(prev_start + 1.0, min(new_t, curr_end - 1.0))

        ivs[i - 1] = (ivs[i - 1][0], new_t, prev_phase, prev_conf, prev_ev)
        ivs[i]     = (new_t, curr_end, curr_phase, curr_conf, new_ev)

    return ivs


# ── Round numbering ────────────────────────────────────────────────────────────

def _number_rounds(
    labeled_intervals: list[tuple[float, float, str, float, str]],
) -> list[tuple[float, float, str, float, str, int]]:
    """Assign game round numbers and return 6-tuples (..., round).

    A round groups one Day (possibly interrupted by Nominations) and its
    following Night together under the same number.  The round increments
    only when a Day phase begins after a Night — NOT on every Day interval
    (because Nomination→Day resumes the same game day, not a new one).

    Intro is always round 0.

    Example:
        Intro          round=0
        Day            round=1   ← first Day starts round 1
        Nomination     round=1   ← same game day
        Day            round=1   ← nominations over, day resumes
        Nomination     round=1
        Night          round=1   ← Night 1 belongs to round 1
        Day            round=2   ← Night ended → new round
        Nomination     round=2
        Night          round=2   ← Night 2 belongs to round 2
    """
    current_round = 0
    came_from_night = False   # True while the last non-Intro phase was Night
    result: list[tuple[float, float, str, float, str, int]] = []
    for iv_start, iv_end, phase, conf, ev in labeled_intervals:
        if phase == "Intro":
            rnd = 0
        elif phase == "Day":
            # New round only on the FIRST Day or when arriving from a Night.
            if current_round == 0 or came_from_night:
                current_round += 1
            came_from_night = False
            rnd = current_round
        elif phase == "Night":
            came_from_night = True
            rnd = current_round   # Night belongs to the same round as its Day
        else:  # Nomination, Execution — part of the current day
            came_from_night = False
            rnd = current_round
        result.append((iv_start, iv_end, phase, conf, ev, rnd))
    return result


# ── Entry point ────────────────────────────────────────────────────────────────

def main(video_id: str, force: bool = False) -> None:
    out_dir  = Path("outputs") / video_id
    out_path = out_dir / "phase_labels.csv"

    if out_path.exists() and not force:
        print(f"  [SKIP] phase_labels.csv already exists for {video_id}")
        return

    print(f"\n=== detect_phases.py — {video_id} ===")

    rows = _load_segments(out_dir)
    if not rows:
        print("  [SKIP] No segments found.")
        return

    total_s = max(float(r["end"]) for r in rows)
    print(f"  Duration: {total_s:.0f}s ({total_s / 60:.1f} min)")

    storyteller_id = _find_storyteller(rows)
    print(f"  Storyteller speaker: {storyteller_id}")

    storyteller_triggers, all_triggers = _collect_triggers(rows, storyteller_id)

    # Structural triggers: speaker-count patterns from the segment data.
    # Added to all_triggers (not storyteller_triggers) so they are suppressed
    # inside the intro window by the state machine's raw_triggers selection.
    structural = _structural_triggers(rows, storyteller_id, total_s)
    all_triggers = all_triggers + structural
    print(
        f"  Keyword triggers: {len(all_triggers) - len(structural)} total"
        f"  ({len(storyteller_triggers)} from storyteller)"
    )
    print(
        f"  Structural triggers: {len(structural)}"
        f"  (Night: {sum(1 for t in structural if t[1] == 'Night')},"
        f"  Day: {sum(1 for t in structural if t[1] == 'Day')})"
    )
    if all_triggers:
        all_phase_hits         = Counter(t[1] for t in all_triggers)
        strong_phase_hits      = Counter(t[1] for t in all_triggers if t[4])
        storyteller_phase_hits = Counter(t[1] for t in storyteller_triggers)
        print(f"  Trigger breakdown (all):   {dict(all_phase_hits)}")
        print(f"  Strong cues only:          {dict(strong_phase_hits)}")
        print(f"  Storyteller triggers:      {dict(storyteller_phase_hits)}")

    labeled_intervals = _apply_state_machine(rows, storyteller_triggers, all_triggers)
    labeled_intervals = _collapse(labeled_intervals)
    labeled_intervals = _merge_short(labeled_intervals, MIN_PHASE_S)
    labeled_intervals = _collapse(labeled_intervals)

    # ── Visual ground-truth override (N0 frame_scan) ──────────────────────────
    # votes_visible=True windows from scan_frames.py are hard Nomination ground
    # truth.  Applied after NLP post-processing so _merge_short cannot absorb
    # them; a final _collapse merges any adjacent Nomination intervals.
    frame_scan_samples = _load_frame_scan(out_dir)
    votes_windows = _load_votes_windows(out_dir)
    if votes_windows:
        print(
            f"  Visual override: {len(votes_windows)} votes-visible window(s) from frame_scan.json"
        )
        labeled_intervals = _apply_votes_overrides(labeled_intervals, votes_windows)
        labeled_intervals = _collapse(labeled_intervals)
    else:
        print("  Visual override: no frame_scan.json (or no votes_visible frames) — NLP only")

    # ── Boundary sharpening (header transitions + ST keywords) ────────────────
    # Refines Night/Day start timestamps from ±60 s NLP accuracy to the exact
    # second using the frame_scan header-visibility flip and the Storyteller's
    # "close your eyes" / "good morning" cue word.
    n_transitions = len(_header_transitions(frame_scan_samples))
    if frame_scan_samples and n_transitions:
        labeled_intervals = _sharpen_boundaries(
            labeled_intervals, frame_scan_samples, rows, storyteller_id
        )
        print(f"  Boundary sharpening: {n_transitions} header transition(s) available")
    else:
        print("  Boundary sharpening: skipped (no frame_scan header transitions)")

    # ── Round numbering ───────────────────────────────────────────────────────
    numbered = _number_rounds(labeled_intervals)

    phase_summary = Counter(iv[2] for iv in numbered)
    print(f"  Output intervals: {len(numbered)}  phases: {dict(phase_summary)}")

    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["start", "end", "phase", "round", "confidence", "evidence"]
        )
        writer.writeheader()
        for iv_start, iv_end, iv_phase, iv_conf, iv_ev, iv_round in numbered:
            writer.writerow({
                "start": round(iv_start, 3), "end": round(iv_end, 3),
                "phase": iv_phase, "round": iv_round,
                "confidence": iv_conf, "evidence": iv_ev,
            })

    print(f"\n  Wrote {len(numbered)} rows -> {out_path}")
    print("\n  Sample (first 10 rows):")
    for iv in numbered[:10]:
        iv_start, iv_end, iv_phase, iv_conf, iv_ev, iv_round = iv
        print(
            f"    {iv_start:8.1f}s - {iv_end:8.1f}s"
            f"  {iv_phase:12s}  rnd={iv_round}  conf={iv_conf:.2f}  \"{iv_ev[:50]}\""
        )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Game-phase boundary detection (N2 / phase_detection)")
    ap.add_argument("video_id", help="YouTube video ID")
    ap.add_argument("--force", action="store_true", help="Overwrite existing output")
    args = ap.parse_args()
    main(args.video_id, force=args.force)
