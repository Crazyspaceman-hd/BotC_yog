"""detect_phases.py — N2: Game-phase boundary detection (state-machine edition).

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
   Triggers are split into storyteller-only (st_triggers) and all-speaker
   (all_triggers, non-ST downweighted by STORYTELLER_DOWNSCALE).
3. Run a sequential state machine over all segment boundaries:
   a. Maintain current_phase and phase_start_t.
   b. At each interval midpoint, score all phases via _score_window.
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
   g. Intro stability: within INTRO_CUTOFF_S only st_triggers are used;
      the machine stays in Intro unless strong ST evidence fires for a
      legal next phase.
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
import re
from collections import Counter
from pathlib import Path

# ── Tuning constants ───────────────────────────────────────────────────────────
INTRO_CUTOFF_S        = 330.0   # intro region ends here (unless strong ST signal)
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
# in-phase evidence keeps cur_sc elevated.  INERTIA_BONUS_EXIT allows the
# exit signal to win over mild staying evidence without eliminating inertia
# entirely.
INERTIA_BONUS_EXIT = 0.02

# Phase pairs for which INERTIA_BONUS_EXIT applies when best candidate has a
# strong cue.  These cover the sticky boundaries where in-phase evidence
# commonly competes with legitimate exit announcements.
_EXIT_INERTIA_PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("Night",      "Day"),
    ("Night",      "Nomination"),
    ("Nomination", "Execution"),
    ("Execution",  "Night"),
    ("Execution",  "Day"),
})

# Transitions that require strong evidence to fire.  Weak cues (player
# chatter, incidental phrases) will not trigger these even if they
# accumulate above MIN_SCORE_WEAK.  This prevents in-phase ST commentary
# (e.g. "good morning" addressed to a player mid-Nomination) from
# prematurely resetting a phase.  Strong cues such as "welcome back to
# town" or "no one died" still trigger them normally.
_STRONG_ONLY_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("Nomination", "Day"),   # needs explicit ST day-boundary announcement
})

# Soft timeout for the Nomination phase.  If we have been in Nomination for
# longer than MAX_NOMINATION_S with no fresh nomination evidence in the last
# NOMINATION_REFRESH_S, suppress Nomination from the candidate pool so the
# machine can naturally transition away (handles long recap / outro tails).
MAX_NOMINATION_S     = 480.0   # 8 minutes
NOMINATION_REFRESH_S = 120.0   # 2-minute freshness window

# ── Signal table ───────────────────────────────────────────────────────────────
# Tuple: (compiled_regex, phase, weight, storyteller_only, strong)
#
# strong=True  — explicit, unambiguous phase-boundary ANNOUNCEMENT
#                (e.g. "everyone close your eyes", "it is now night")
#                These fire at MIN_SCORE_STRONG.
#
# strong=False — incidental / reference phrase that can appear in player
#                DISCUSSION as well as real announcements
#                (e.g. "night one", "good morning", "i nominate")
#                These fire at the higher MIN_SCORE_WEAK bar.
#
# st=True      — storyteller-dominant; non-ST speakers get weight * STORYTELLER_DOWNSCALE.
#                Within the intro window, only ST triggers are evaluated at all.
_SIGNALS: list[tuple[re.Pattern, str, float, bool, bool]] = []


def _sig(pat: str, phase: str, w: float = 1.0,
         st: bool = False, strong: bool = False) -> None:
    _SIGNALS.append((re.compile(pat, re.I), phase, w, st, strong))


# ── Night ──────────────────────────────────────────────────────────────────────
# Strong: unambiguous state-transition announcements (ST class A/B cues)
_sig(r"\bit(?:'s| is) now night\b",                      "Night", 1.0, st=True,  strong=True)
_sig(r"\beveryone (?:close|shut) your eyes\b",           "Night", 0.9, st=True,  strong=True)
_sig(r"\bclose your eyes\b",                             "Night", 0.8, st=True,  strong=True)
_sig(r"\bnight (?:begins|starts|falls?|time)\b",         "Night", 0.8, st=True,  strong=True)
_sig(r"\bsun(?:set|down)\b",                             "Night", 0.7, st=True,  strong=True)
_sig(r"\bgo to sleep\b",                                 "Night", 0.8, st=True,  strong=True)
# Weak: reference phrases (players mention these during day discussion)
_sig(r"\bnight (?:one|two|three|four|five|six|seven|eight|nine|\d+)\b",
                                                          "Night", 0.6, st=True,  strong=False)
_sig(r"\bgood night\b",                                  "Night", 0.4, st=True,  strong=False)

# ── Day ────────────────────────────────────────────────────────────────────────
# Strong: unambiguous state-transition announcements + day-start resolutions
_sig(r"\bit(?:'s| is) now day\b",                        "Day",   1.0, st=True,  strong=True)
_sig(r"\beveryone (?:open|opens?) your eyes\b",          "Day",   0.9, st=True,  strong=True)
_sig(r"\bday (?:begins|starts|time)\b",                  "Day",   0.8, st=True,  strong=True)
_sig(r"\bthe sun rises?\b",                              "Day",   0.8, st=True,  strong=True)
_sig(r"\bsunrise\b",                                     "Day",   0.7, st=True,  strong=True)
_sig(r"\bno(?:body| one) (?:died|was killed|was murdered)\b",
                                                          "Day",   0.9, st=True,  strong=True)
_sig(r"\b(?:someone|a player) (?:died|was found dead|was killed)\b",
                                                          "Day",   0.8, st=True,  strong=True)
# Weak: reference phrases
_sig(r"\bday (?:one|two|three|four|five|six|seven|eight|nine|\d+)\b",
                                                          "Day",   0.6, st=True,  strong=False)
_sig(r"\brise and shine\b",                              "Day",   0.8, st=True,  strong=True)
_sig(r"\bgood morning\b",                                "Day",   0.5, st=True,  strong=False)
_sig(r"\bwake up\b",                                     "Day",   0.4, st=True,  strong=False)
# Phase-exit cues: mark the Night → Day boundary specifically.
# "welcome back to town" is the canonical Yogscast BotC day-start phrase (ST).
# "last night" and "overnight" accumulate from players discussing night outcomes
# and fire once multiple speakers mention them within the context window.
# "the night is over" and "dawn breaks" are any-speaker explicit announcements.
_sig(r"\bwelcome back to town\b",                        "Day",   0.9, st=True,  strong=True)
_sig(r"\bthe night(?:'s| is| was) over\b",               "Day",   0.9, st=False, strong=True)
_sig(r"\bdawn (?:breaks?|has (?:come|broken))\b",        "Day",   0.8, st=True,  strong=True)
_sig(r"\bit(?:'s| is) (?:now )?(?:the )?morning\b",      "Day",   0.8, st=True,  strong=True)
_sig(r"\blast night\b",                                  "Day",   0.35, st=False, strong=False)
_sig(r"\bovernight\b",                                   "Day",   0.30, st=False, strong=False)

# ── Nomination ─────────────────────────────────────────────────────────────────
# Strong: ST procedural cues
_sig(r"\bnominations?(?: are| is)? open\b",              "Nomination", 1.0, st=True,  strong=True)
_sig(r"\bvote(?:s)?(?: are| is)? open\b",                "Nomination", 0.9, st=True,  strong=True)
_sig(r"\btime to (?:vote|nominate)\b",                   "Nomination", 0.8, st=True,  strong=True)
_sig(r"\bwho (?:do you |would you )?nominate\b",         "Nomination", 0.7, st=True,  strong=True)
# Weak: player-initiated (valid but require accumulation)
_sig(r"\bi nominate\b",                                  "Nomination", 0.6, st=False, strong=False)
_sig(r"\bput(?:ting)? (?:\w+ )?on the block\b",          "Nomination", 0.5, st=False, strong=False)

# ── Execution ──────────────────────────────────────────────────────────────────
# All strong: these phrases only appear during / immediately after an execution
_sig(r"\bhas been executed\b",                           "Execution", 1.0, st=True,  strong=True)
_sig(r"\byou(?:'ve| have) been executed\b",              "Execution", 1.0, st=True,  strong=True)
_sig(r"\bwas executed\b",                                "Execution", 0.9, st=True,  strong=True)
_sig(r"\bexecution (?:is )?complete\b",                  "Execution", 0.9, st=True,  strong=True)
_sig(r"\bput to death\b",                                "Execution", 0.8, st=True,  strong=True)
_sig(r"\bthe vote (?:passed|carries)\b",                 "Execution", 0.7, st=True,  strong=True)
_sig(r"\bis now dead\b",                                 "Execution", 0.8, st=True,  strong=True)
# Phase-exit cues: mark Nomination → Execution boundary from any speaker.
# Players commonly announce the vote outcome before the ST formalises it.
_sig(r"\bvoted (?:out|off)\b",                           "Execution", 0.8, st=False, strong=True)
_sig(r"\bgets? (?:executed|the axe)\b",                  "Execution", 0.8, st=True,  strong=True)


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


def _collect_triggers(
    rows: list[dict],
    st_id: str | None,
) -> tuple[
    list[tuple[float, str, float, str, bool]],  # st_triggers
    list[tuple[float, str, float, str, bool]],  # all_triggers
]:
    """Scan segments and return two trigger lists.

    st_triggers  — only signals from the identified storyteller speaker, at
                   full weight.  Used exclusively in the intro window.

    all_triggers — signals from every speaker; non-ST speakers are downweighted
                   by STORYTELLER_DOWNSCALE for ST-only signal patterns.

    Each trigger tuple: (timestamp, phase, eff_weight, evidence_text, strong)
    """
    st_triggers:  list[tuple[float, str, float, str, bool]] = []
    all_triggers: list[tuple[float, str, float, str, bool]] = []
    for row in rows:
        t     = float(row["start"])
        text  = row["text"]
        spk   = row["speaker"]
        is_st = (spk == st_id) if st_id else False
        for pat, phase, w, st_only, strong in _SIGNALS:
            if pat.search(text):
                eff_w = w if (not st_only or is_st) else w * STORYTELLER_DOWNSCALE
                tup   = (t, phase, eff_w, text[:80].replace("\n", " "), strong)
                all_triggers.append(tup)
                if is_st:
                    st_triggers.append(tup)
    return st_triggers, all_triggers


def _score_window(
    t: float,
    triggers: list[tuple[float, str, float, str, bool]],
) -> dict[str, tuple[float, bool, str]]:
    """Accumulate trigger scores within CONTEXT_S of t.

    Returns {phase: (raw_score, has_strong_cue, best_evidence_text)}.

    Does NOT apply MIN_SCORE thresholds — the state machine decides whether
    the score is sufficient to act on.  Caller is responsible for checking
    MIN_SCORE_STRONG vs MIN_SCORE_WEAK based on has_strong_cue.
    """
    scores:  dict[str, float] = {}
    strongs: dict[str, bool]  = {}
    evids:   dict[str, str]   = {}
    for trig_t, phase, w, evid, strong in triggers:
        dt = abs(t - trig_t)
        if dt <= CONTEXT_S:
            eff = w * (1.0 - dt / CONTEXT_S)
            scores[phase] = scores.get(phase, 0.0) + eff
            if strong and not strongs.get(phase):
                strongs[phase] = True
            if eff > 0.05 and phase not in evids:
                evids[phase] = evid
    return {
        ph: (scores[ph], strongs.get(ph, False), evids.get(ph, ""))
        for ph in scores
    }


# ── State machine ──────────────────────────────────────────────────────────────

def _apply_state_machine(
    rows:         list[dict],
    st_triggers:  list[tuple[float, str, float, str, bool]],
    all_triggers: list[tuple[float, str, float, str, bool]],
) -> list[tuple[float, float, str, float, str]]:
    """Sequentially label intervals using a transition-gated state machine.

    Key behaviours
    --------------
    Intro stability
        Within INTRO_CUTOFF_S only st_triggers are evaluated.  Player chatter
        ("night one" in role announcements) cannot end Intro.  Past the cutoff
        the machine silently promotes Intro to Day.

    Transition gating
        Only phases in ALLOWED_TRANSITIONS[current_phase] are considered as
        switch candidates.  Illegal jumps (e.g. Night→Execution, Night→
        Nomination) are ignored entirely.

    Inertia
        The current phase receives INERTIA_BONUS added to its raw evidence
        score.  A candidate must beat (current_score + INERTIA_BONUS) to
        trigger a switch.  When no active evidence supports the current phase
        (raw score=0) the effective bar is just INERTIA_BONUS (0.15), so a
        single strong cue still fires cleanly.

    Nomination timeout
        After MAX_NOMINATION_S in Nomination with no fresh evidence in the
        last NOMINATION_REFRESH_S window, Nomination is suppressed from the
        candidate pool.  This handles long recap/outro tails where "i nominate"
        references dried up after the real nominations ended.

    Low-ST fallback
        With few ST cues the machine stays in broad Day/Night blocks rather
        than churning on noisy accumulated player references.
    """
    boundaries = sorted(
        {float(r["start"]) for r in rows} | {float(r["end"]) for r in rows}
    )

    current_phase = "Intro"
    phase_start_t = 0.0
    result: list[tuple[float, float, str, float, str]] = []

    for i in range(len(boundaries) - 1):
        t0   = boundaries[i]
        t1   = boundaries[i + 1]
        tmid = (t0 + t1) / 2.0

        # ── Intro past cutoff: silently promote to Day ────────────────────────
        # Once INTRO_CUTOFF_S has elapsed and we are still in Intro, assume Day
        # has begun.  This ensures a sane baseline even with zero ST cues.
        if current_phase == "Intro" and t0 >= INTRO_CUTOFF_S:
            current_phase = "Day"
            phase_start_t = t0

        # ── Select trigger pool ───────────────────────────────────────────────
        triggers = st_triggers if tmid < INTRO_CUTOFF_S else all_triggers

        # ── Score all phases at this midpoint ─────────────────────────────────
        scores = _score_window(tmid, triggers)

        # ── Nomination timeout suppression ────────────────────────────────────
        if (current_phase == "Nomination"
                and (tmid - phase_start_t) > MAX_NOMINATION_S):
            fresh = any(
                True
                for trig in all_triggers
                if trig[1] == "Nomination"
                and (tmid - NOMINATION_REFRESH_S) <= trig[0] <= tmid
            )
            if not fresh:
                scores.pop("Nomination", None)

        # ── Intro window: fully locked until INTRO_CUTOFF_S ──────────────────
        # Intro is treated as a fixed block; no phase transitions are evaluated
        # within it.  This prevents both player chatter ("night one" during
        # role announcements) AND storyteller Night-0 setup phrases ("go to
        # sleep" before Day 1) from fragmenting or prematurely ending Intro.
        # Phase detection only begins once INTRO_CUTOFF_S has elapsed and the
        # machine silently promotes Intro → Day above.
        if tmid < INTRO_CUTOFF_S and current_phase == "Intro":
            result.append((t0, t1, "Intro", 0.9, "before intro cutoff"))
            continue

        # ── Post-intro state machine ──────────────────────────────────────────
        # Find the best legal candidate phase from ALLOWED_TRANSITIONS.
        allowed_nexts = ALLOWED_TRANSITIONS.get(current_phase, [])
        best_cand   = None
        best_sc     = 0.0
        best_ev     = ""
        best_strong = False   # whether the best candidate has a strong cue
        for ph in allowed_nexts:
            if ph not in scores:
                continue
            sc, has_strong, ev = scores[ph]
            min_s = MIN_SCORE_STRONG if has_strong else MIN_SCORE_WEAK
            if sc >= min_s and sc > best_sc:
                best_cand, best_sc, best_ev, best_strong = ph, sc, ev, has_strong

        # Enforce strong-only constraint on certain transitions.
        # Some phase exits (e.g. Nomination→Day) must not be triggered by weak
        # cues alone (e.g. ST saying "good morning" to a player mid-Nomination).
        # Only a STRONG cue — an unambiguous boundary announcement such as
        # "welcome back to town" or "nobody died" — may fire these transitions.
        if (best_cand is not None
                and not best_strong
                and (current_phase, best_cand) in _STRONG_ONLY_TRANSITIONS):
            best_cand   = None
            best_sc     = 0.0
            best_ev     = ""
            best_strong = False

        # Current phase score (raw, before inertia bonus).
        cur_sc, _cur_strong, cur_ev = scores.get(current_phase, (0.0, False, ""))

        # Choose the inertia bonus.
        # For known phase-exit boundaries where a STRONG candidate exists,
        # use INERTIA_BONUS_EXIT (≈0) so that nearby in-phase evidence
        # (e.g. Night cues from ongoing resolution) cannot suppress an
        # obvious exit announcement (e.g. "welcome back to town" = Day start).
        # For all other cases keep INERTIA_BONUS to prevent noisy flips.
        is_exit_boundary = (
            best_strong
            and best_cand is not None
            and (current_phase, best_cand) in _EXIT_INERTIA_PAIRS
        )
        inertia = cur_sc + (INERTIA_BONUS_EXIT if is_exit_boundary else INERTIA_BONUS)

        if best_cand is not None and best_sc > inertia:
            # ── Switch ────────────────────────────────────────────────────────
            current_phase = best_cand
            phase_start_t = t0
            total_sc = sum(s for s, _, _ in scores.values()) or 1e-9
            conf = round(min(best_sc / total_sc, 1.0), 3)
            result.append((t0, t1, current_phase, conf, best_ev))
        else:
            # ── Stay ──────────────────────────────────────────────────────────
            if cur_sc > 0.0:
                total_sc = sum(s for s, _, _ in scores.values()) or 1e-9
                conf = round(min(cur_sc / total_sc, 1.0), 3)
                evid = cur_ev
            else:
                phase_age = tmid - phase_start_t
                conf = max(0.05, 0.6 - phase_age * CONFIDENCE_DECAY_S)
                evid = f"inferred from previous {current_phase}"
            result.append((t0, t1, current_phase, round(conf, 3), evid))

    return result


# ── Post-processing ────────────────────────────────────────────────────────────

def _merge_short(
    intervals: list[tuple[float, float, str, float, str]],
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


def _collapse(
    intervals: list[tuple[float, float, str, float, str]],
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

    rows = _load_segments(out_dir)
    if not rows:
        print("  [SKIP] No segments found.")
        return

    total_s = max(float(r["end"]) for r in rows)
    print(f"  Duration: {total_s:.0f}s ({total_s / 60:.1f} min)")

    st_id = _find_storyteller(rows)
    print(f"  Storyteller speaker: {st_id}")

    st_triggers, all_triggers = _collect_triggers(rows, st_id)
    print(f"  Keyword triggers: {len(all_triggers)} total"
          f"  ({len(st_triggers)} from storyteller)")
    if all_triggers:
        phase_hits  = Counter(t[1] for t in all_triggers)
        strong_hits = Counter(t[1] for t in all_triggers if t[4])
        st_phase    = Counter(t[1] for t in st_triggers)
        print(f"  Trigger breakdown (all):   {dict(phase_hits)}")
        print(f"  Strong cues only:          {dict(strong_hits)}")
        print(f"  Storyteller triggers:      {dict(st_phase)}")

    ivs = _apply_state_machine(rows, st_triggers, all_triggers)
    ivs = _collapse(ivs)
    ivs = _merge_short(ivs, MIN_PHASE_S)
    ivs = _collapse(ivs)

    phase_summary = Counter(iv[2] for iv in ivs)
    print(f"  Output intervals: {len(ivs)}  phases: {dict(phase_summary)}")

    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["start", "end", "phase", "confidence", "evidence"])
        w.writeheader()
        for s, e, ph, cf, ev in ivs:
            w.writerow({
                "start": round(s, 3), "end": round(e, 3),
                "phase": ph, "confidence": cf, "evidence": ev,
            })

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
