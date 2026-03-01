"""analyze_roles.py - Link speakers to players+roles, then detect lies.

Reads:
    players.txt
    roles.txt (per-game override or global fallback)
    outputs/<VIDEO_ID>/segments_patched.csv  (falls back to segments.csv)
    outputs/<VIDEO_ID>/diarization.rttm
    outputs/<VIDEO_ID>/intro_roster.json     (scraped from video — optional)
    outputs/<VIDEO_ID>/roster_overrides.json (manual edits from explore.py)

Writes:
    outputs/<VIDEO_ID>/lie_analysis.csv

Roster priority (highest → lowest):
    1. Manual overrides (roster_overrides.json)     — human-corrected
    2. Scraped intro    (intro_roster.json)          — OCR from video UI
    3. Auto-detected    (transcript intro patterns)  — NLP heuristics

Role changes during game are detected and tracked.  Verdict evaluation uses
the player's role AT THE TIME of each claim, not just their final role.

Usage:
    python analyze_roles.py [video_id]
"""

import argparse
import csv
import json
import re
from pathlib import Path

from pipeline_utils import parse_rttm

INTRO_CUTOFF   = 300.0        # seconds — used to build the initial roster
STORYTELLER_ID = "speaker_0"  # diarization always assigns lead voice to spk_0


# ── helpers ──────────────────────────────────────────────────────────────────

def load_lines(path: Path) -> list[str]:
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def fmt_ts(sec: float) -> str:
    m = int(sec // 60)
    s = sec - 60 * m
    return f"{m:02d}:{s:05.2f}"


def load_segments(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            rows.append({
                "start":   float(row["start"]),
                "end":     float(row["end"]),
                "speaker": row["speaker"],
                "text":    row["text"],
            })
    return rows


def speaker_at(t: float, turns: list[tuple[float, float, str]]) -> str:
    for s, e, spk in turns:
        if s <= t < e:
            return spk
    return "UNKNOWN"


def make_role_re(roles: list[str]) -> re.Pattern:
    sorted_roles = sorted(roles, key=len, reverse=True)
    return re.compile(
        r"\b(" + "|".join(re.escape(r) for r in sorted_roles) + r")\b",
        re.IGNORECASE,
    )


def make_role_claim_re(roles: list[str]) -> re.Pattern:
    """
    Matches first-person role claims:
        I am (the|a|of the) <role>
        I'm (the|a) <role>
        my role (today) is (the|a) <role>
        I claim (the|a) <role>
        I am playing (the|a) <role>
    """
    rp = "|".join(re.escape(r) for r in sorted(roles, key=len, reverse=True))
    return re.compile(
        r"(?:"
        r"I(?:'m| am)\s+(?:the|a|of the)\s*(?P<r1>" + rp + r")"
        r"|my role(?:\s+today)?\s+is\s+(?:the|a)?\s*(?P<r2>" + rp + r")"
        r"|I\s+claim\s+(?:the|a)?\s*(?P<r3>" + rp + r")"
        r"|I(?:'m| am)\s+playing\s+(?:the|a)\s+(?P<r4>" + rp + r")"
        r")",
        re.IGNORECASE,
    )


# ── name-finding helpers ──────────────────────────────────────────────────────

def find_intro_name(text: str, players: list[str]) -> str | None:
    for player in players:
        first = player.split()[0]
        pats = [
            rf"\b{re.escape(player)}\s+here\b",
            rf"\bmy name is\s+{re.escape(player)}\b",
            rf"\bI(?:'m| am)\s+{re.escape(first)}\b(?:\s|[,!.]|$)",
            rf"\bHello\s+{re.escape(first)}\b",
            rf"\bHi\s+(?:I am|I'm)\s+{re.escape(first)}\b",
            rf"\bguys\s+{re.escape(first)}\s+here\b",
        ]
        for pat in pats:
            if re.search(pat, text, re.IGNORECASE):
                return player

    m = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+here\b", text)
    if m:
        return m.group(1)
    lc = text.lower()
    pos = lc.find("my name is ")
    if pos >= 0:
        m = re.match(r"([A-Z][a-z']+(?:\s+[A-Z][a-z']+)*)",
                     text[pos + len("my name is "):])
        if m:
            return m.group(1)
    m = re.search(r"\bI(?:'m| am)\s+([A-Z][a-z]+)\b(?:\s|[,!.]|$)", text)
    if m:
        candidate = m.group(1)
        if candidate.lower() not in {
            "the", "a", "an", "going", "playing", "not", "just", "also",
            "happy", "glad", "sorry", "afraid", "pretty", "kind", "good",
            "bad", "here", "there", "your", "ready", "sure", "okay",
        }:
            return candidate
    return None


def find_intro_role(text: str, role_claim_re: re.Pattern) -> str | None:
    m = role_claim_re.search(text)
    if not m:
        return None
    for v in m.groupdict().values():
        if v:
            return v.lower()
    return None


# ── roster builder (transcript-based) ────────────────────────────────────────

def build_roster(
    segments:   list[dict],
    players:    list[str],
    roles:      list[str],
    rttm_turns: list[tuple[float, float, str]],
) -> dict[str, dict]:
    """
    Scan intro segments for self-introduction patterns.
    Returns {speaker_id: {name, believed_role, actual_role,
                          initial_actual_role, role_history, source}}.
    """
    intro = [s for s in segments if s["start"] <= INTRO_CUTOFF]
    role_claim_re = make_role_claim_re(roles)
    roster: dict[str, dict] = {}
    assigned: set[str] = set()

    for i, seg in enumerate(intro):
        text    = seg["text"]
        speaker = seg["speaker"]

        name = find_intro_name(text, players)
        role = find_intro_role(text, role_claim_re)

        if name and role is None and i + 1 < len(intro):
            role = find_intro_role(text + " " + intro[i + 1]["text"], role_claim_re)
        if role and name is None and i > 0:
            name = find_intro_name(intro[i - 1]["text"] + " " + text, players)

        if not (name and role):
            continue

        actual_speaker = speaker
        if speaker == STORYTELLER_ID:
            for j in range(i + 1, min(i + 4, len(intro))):
                if intro[j]["speaker"] != STORYTELLER_ID:
                    actual_speaker = intro[j]["speaker"]
                    break

        if actual_speaker in assigned or actual_speaker == STORYTELLER_ID:
            continue

        roster[actual_speaker] = {
            "name":               name,
            "believed_role":      role,
            "actual_role":        role,
            "initial_actual_role": role,
            "role_history":       [],
            "source":             "auto",
        }
        assigned.add(actual_speaker)

    return roster


# ── intro-timing speaker linker ───────────────────────────────────────────────

def link_speakers_from_intro_timing(
    scraped:     dict[str, dict],         # {name_lower: {name, frame_time, ...}}
    rttm_turns:  list[tuple[float, float, str]],
    existing:    dict[str, dict],         # already-linked roster {speaker_id: info}
    st_id:       str  = STORYTELLER_ID,
    win_before:  float = 25.0,            # seconds before card appears
    win_after:   float = 5.0,             # seconds after card appears
    min_coverage: float = 0.40,           # fraction of window the speaker must occupy
) -> dict[str, str]:
    """
    For each scraped player card (sorted by frame_time), find the diarization
    speaker that dominates the audio window just before the card appears on
    screen.  Players record solo intro monologues which are edited into the
    video; their card typically appears mid-monologue, so we look [ft-25, ft+5].

    Returns {speaker_id: player_name} for newly-inferred links only.
    Already-linked speakers are excluded so manual overrides always win.
    """
    linked = set(existing.keys()) | {st_id}
    result: dict[str, str] = {}
    already_named = {info.get("name", "").lower() for info in existing.values()}

    players_by_time = sorted(
        [(info["frame_time"], name_lc, info)
         for name_lc, info in scraped.items()
         if info.get("frame_time", 0) > 0],
        key=lambda x: x[0],
    )

    for frame_time, name_lc, info in players_by_time:
        name = info["name"]
        if name_lc in already_named or name_lc in {v.lower() for v in result.values()}:
            continue

        win_s = frame_time - win_before
        win_e = frame_time + win_after
        win_len = win_e - win_s

        # Accumulate speaking time per unlinked speaker within the window
        time_in_win: dict[str, float] = {}
        for s, e, spk in rttm_turns:
            if spk in linked or spk in result:
                continue
            overlap = max(0.0, min(e, win_e) - max(s, win_s))
            if overlap > 0:
                time_in_win[spk] = time_in_win.get(spk, 0.0) + overlap

        if not time_in_win:
            continue

        best_spk = max(time_in_win, key=time_in_win.__getitem__)
        coverage = time_in_win[best_spk] / win_len if win_len > 0 else 0

        if coverage < min_coverage:
            continue  # not confident enough

        result[best_spk] = name
        linked.add(best_spk)
        already_named.add(name_lc)
        print(f"  [intro_timing] {name:20s}  ->  {best_spk}"
              f"  (coverage {coverage:.0%})")

    return result


# ── scraped intro roster integration ─────────────────────────────────────────

def load_intro_roster(out_dir: Path) -> dict[str, dict]:
    """
    Load intro_roster.json produced by scrape_intro.py.
    Returns {lowercase_name: {name, believed_role, actual_role, frame_time}}.
    """
    intro_file = out_dir / "intro_roster.json"
    if not intro_file.exists():
        return {}
    try:
        data = json.loads(intro_file.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [WARNING] Could not read intro_roster.json: {exc}")
        return {}

    result: dict[str, dict] = {}
    for p in data.get("players", []):
        name = (p.get("name") or "").strip()
        if name:
            result[name.lower()] = {
                "name":          name,
                "believed_role": p.get("believed_role", "unknown").lower(),
                "actual_role":   p.get("actual_role",   "unknown").lower(),
                "frame_time":    p.get("frame_time", 0.0),
            }
    return result


def apply_intro_roster(roster: dict[str, dict], scraped: dict[str, dict]) -> None:
    """
    Update believed_role / actual_role in *roster* using scraped data.
    The speaker_id → name mapping stays from transcript analysis; only roles
    (and the name casing) are updated where a scraped record matches.
    """
    for spk, info in roster.items():
        name_lc = (info.get("name") or "").lower()
        hit = scraped.get(name_lc)
        if hit:
            info["believed_role"]       = hit["believed_role"]
            info["actual_role"]         = hit["actual_role"]
            info["initial_actual_role"] = hit["actual_role"]
            info["source"]              = "scraped"
            print(f"  [scraped] {info['name']}  actual: {hit['actual_role']}"
                  + (f"  (believed: {hit['believed_role']})"
                     if hit['actual_role'] != hit['believed_role'] else ""))


# ── storyteller corrections (mid-game announcements) ─────────────────────────

def apply_storyteller_corrections(
    segments: list[dict],
    roster:   dict[str, dict],
    roles:    list[str],
) -> None:
    """
    Parse ALL segments for patterns like:
        "<Name> is actually the <Role>"
        "<Name> of course is the <Role>"
    Updates actual_role (and initial_actual_role if no role_history yet).
    """
    name_to_spk = {info["name"].lower(): spk for spk, info in roster.items()
                   if info.get("name")}
    rp = "|".join(re.escape(r) for r in sorted(roles, key=len, reverse=True))
    correction_pats = [
        re.compile(r"(\w+)\s+is\s+actually\s+(?:the|a)\s+(" + rp + r")\b",
                   re.IGNORECASE),
        re.compile(r"(\w+)\s+of\s+course\s+is\s+(?:the|a)\s+(" + rp + r")\b",
                   re.IGNORECASE),
    ]

    for seg in segments:
        for pat in correction_pats:
            for m in pat.finditer(seg["text"]):
                name = m.group(1).lower()
                role = m.group(2).lower()
                spk  = name_to_spk.get(name)
                if not spk:
                    continue
                old = roster[spk]["actual_role"]
                if old != role:
                    roster[spk]["actual_role"]         = role
                    roster[spk]["initial_actual_role"] = role
                    print(f"  [storyteller] {roster[spk]['name']} is actually "
                          f"the {role}  (was: {old})")


# ── role-change detection (mid-game) ─────────────────────────────────────────

def detect_role_changes(
    segments: list[dict],
    roster:   dict[str, dict],
    roles:    list[str],
) -> None:
    """
    Detect patterns like "[Name] you are now the [Role]" or
    "[Name] is now the [Role]" occurring AFTER the intro cutoff.
    Appends to roster[spk]["role_history"] and updates actual_role to latest.
    """
    name_to_spk = {info["name"].lower(): spk for spk, info in roster.items()
                   if info.get("name")}
    rp = "|".join(re.escape(r) for r in sorted(roles, key=len, reverse=True))

    change_pats = [
        re.compile(
            r"(\w[\w\s]{0,20}?)\s*,?\s*you(?:'re| are) now (?:the|a)\s*(" + rp + r")\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"(\w+)\s+is now (?:the|a)\s*(" + rp + r")\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"(\w+)\s+(?:has become|became) (?:the|a)\s*(" + rp + r")\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"(\w+)(?:'s)?\s+role\s+(?:has )?changed to (?:the|a)?\s*(" + rp + r")\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"(\w+)\s+you(?:'re| are)\s+(?:the|a)\s*(" + rp + r")\s+"
            r"(?:from now|tonight|for the rest)\b",
            re.IGNORECASE,
        ),
    ]

    for seg in segments:
        if seg["start"] <= INTRO_CUTOFF:
            continue
        for pat in change_pats:
            for m in pat.finditer(seg["text"]):
                name_raw = m.group(1).strip().lower()
                new_role = m.group(2).lower()

                # Accept full name or first token
                spk = name_to_spk.get(name_raw)
                if not spk:
                    # Try first-word match
                    for stored_name, s in name_to_spk.items():
                        if stored_name.split()[0] == name_raw:
                            spk = s
                            break
                if not spk:
                    continue

                old_role = roster[spk]["actual_role"]
                if old_role == new_role:
                    continue

                roster[spk]["role_history"].append({
                    "time":             seg["start"],
                    "previous_role":    old_role,
                    "actual_role":      new_role,
                })
                roster[spk]["actual_role"] = new_role
                print(f"  [role change] {roster[spk]['name']}: "
                      f"{old_role} -> {new_role}  at t={fmt_ts(seg['start'])}")


# ── role-at-time helper ───────────────────────────────────────────────────────

def get_role_at(speaker: str, claim_time: float, roster: dict) -> tuple[str, str]:
    """
    Return (believed_role, actual_role) for *speaker* at *claim_time*.
    Role history is replayed chronologically so mid-game changes are respected.
    """
    info = roster.get(speaker)
    if not info:
        return "unknown", "unknown"

    believed = info.get("believed_role", "unknown")
    # Start from initial role, then walk forward through history
    actual = info.get("initial_actual_role", info.get("actual_role", "unknown"))

    for change in sorted(info.get("role_history", []), key=lambda c: c["time"]):
        if change["time"] <= claim_time:
            actual = change["actual_role"]
        else:
            break

    return believed, actual


# ── winner detection ──────────────────────────────────────────────────────────

def detect_winner(segments: list[dict]) -> str | None:
    """
    Scan the final 40 % of transcript segments for win-announcement phrases.
    Returns "Good", "Evil", or None if not detectable.

    Two detection phases:
      1. Direct pattern matching on individual late segments (weighted).
      2. Consecutive-pair matching for cross-segment phrases like
         "Rydian won" / "with the good team" (split across 2 segments).

    Pattern weights:
      4 = explicit direct declaration  ("evil has won", "you won, good team")
      3 = strong contextual evidence   ("slayed the demon", "good victory")
      2 = moderate evidence            ("on behalf of the evil team")
      1 = weak / consolation phrase    ("congratulations, evil/good team")

    Ties or zero evidence → None.
    """
    if not segments:
        return None

    import re as _re

    max_t  = max(s["start"] for s in segments)
    cutoff = max_t * 0.60        # search last 40 %

    # ── pattern table  (regex, team, weight) ──────────────────────────────────
    _PATTERNS: list[tuple[str, str, int]] = [
        # ── Evil ──────────────────────────────────────────────────────────────
        ("evil team win",                    "Evil", 3),
        ("evil team has won",                "Evil", 4),
        ("evil team have won",               "Evil", 4),
        ("evil team won",                    "Evil", 4),
        ("evil has won",                     "Evil", 4),   # "evil has won"
        ("evil team victory",                "Evil", 3),
        ("evil have won",                    "Evil", 4),
        ("evil won",                         "Evil", 3),
        # require 'that' to avoid "tell you MY evil team is…" false-positives
        ("tell you that (?:the )?evil",      "Evil", 4),
        ("well done.*evil team",             "Evil", 2),
        ("congratulations.*evil team",       "Evil", 1),   # may be consolation
        ("on behalf of the evil",            "Evil", 2),   # winner taunting
        ("managed to win.*evil",             "Evil", 3),   # "managed to win for the evil team"
        ("for the evil team",                "Evil", 2),   # "a difficult game for the evil team"
        ("evil team one\\b",                 "Evil", 2),   # Whisper OCR of "evil team won"
        # ── Good ──────────────────────────────────────────────────────────────
        ("good team win",                    "Good", 3),
        ("good team has won",                "Good", 4),
        ("good team have won",               "Good", 4),
        ("good team won",                    "Good", 4),
        ("good team victory",                "Good", 3),
        ("good wins",                        "Good", 1),   # low weight: too generic alone
        ("good have won",                    "Good", 4),
        ("good won",                         "Good", 3),
        # require 'that' to avoid "tell you boys … good" false-positives
        ("tell you that (?:the )?good",      "Good", 4),
        ("you won.*good",                    "Good", 4),   # "You won, good team"
        ("won.*good team",                   "Good", 4),   # "won with the good team"
        ("congrats.*good team",              "Good", 3),   # "congrats good team"
        ("good team.*congrats",              "Good", 3),
        ("town wins",                        "Good", 3),
        ("town has won",                     "Good", 3),
        ("town have won",                    "Good", 3),
        ("townsfolk win",                    "Good", 3),
        ("townsfolk won",                    "Good", 3),
        ("townsfolk have won",               "Good", 3),
        ("well done.*good team",             "Good", 2),
        ("congratulations.*good team",       "Good", 1),   # may be consolation
        ("congratulations.*town",            "Good", 2),
        ("good victory",                     "Good", 2),   # "A good victory"
        ("to the good team",                 "Good", 2),   # "Congratulations. To the good team."
        ("killed the demon",                 "Good", 3),   # slayer / execution kills demon
        ("slayed the demon",                 "Good", 4),   # Slayer ability fires
        ("slew the demon",                   "Good", 3),
        ("hang the d[ij]",                   "Good", 3),   # Atheist game: "hang the DJ/DeeJay"
        ("on behalf of the good",            "Good", 2),
    ]

    compiled = [
        (_re.compile(p, _re.IGNORECASE), team, w)
        for p, team, w in _PATTERNS
    ]

    scores: dict[str, int] = {"Good": 0, "Evil": 0}

    # ── Phase 1: individual late segments ─────────────────────────────────────
    late_segs = [s for s in segments if s["start"] >= cutoff]
    for seg in late_segs:
        txt = seg.get("text", "")
        for pat, team, weight in compiled:
            if pat.search(txt):
                scores[team] += weight

    # ── Phase 2: consecutive pairs (catches cross-segment phrases) ────────────
    # Only check "won ... team" patterns – the main cross-segment split case.
    # (Broader patterns like "tell you that…evil" are intentionally NOT applied
    # here to avoid false-positives from paired segments whose combined text
    # matches "tell you that the [team] is going to LOSE".)
    _cross_pats: list[tuple[_re.Pattern, str]] = [
        (_re.compile(r"won.{0,80}good team",  _re.IGNORECASE), "Good"),
        (_re.compile(r"won.{0,80}evil team",  _re.IGNORECASE), "Evil"),
        (_re.compile(r"good team.{0,80}won",  _re.IGNORECASE), "Good"),
        (_re.compile(r"evil team.{0,80}won",  _re.IGNORECASE), "Evil"),
    ]
    for i in range(len(late_segs) - 1):
        pair_txt = late_segs[i]["text"] + " " + late_segs[i + 1]["text"]
        for pat, team in _cross_pats:
            if pat.search(pair_txt):
                scores[team] += 3

    if scores["Evil"] == scores["Good"]:
        return None
    return "Evil" if scores["Evil"] > scores["Good"] else "Good"


# ── lie detection ─────────────────────────────────────────────────────────────

def scan_for_lies(
    segments: list[dict],
    roster:   dict[str, dict],
    roles:    list[str],
) -> list[dict]:
    """
    Scan ALL game segments (after intro) for first-person role claims.
    Verdict evaluation uses the player's role AT the time of each claim.
    """
    role_claim_re = make_role_claim_re(roles)
    records = []

    for seg in segments:
        if seg["start"] <= INTRO_CUTOFF:
            continue
        speaker = seg["speaker"]
        if speaker == STORYTELLER_ID:
            continue

        info = roster.get(speaker)
        player_name = info["name"]   if info else speaker
        believed_at, actual_at = get_role_at(speaker, seg["start"], roster)

        for m in role_claim_re.finditer(seg["text"]):
            claimed = next(v for v in m.groupdict().values() if v).lower()

            if actual_at == "unknown":
                verdict = "UNVERIFIED"
            elif claimed == actual_at:
                verdict = "TRUE"
            elif claimed == believed_at and believed_at != actual_at:
                verdict = "HONEST MISTAKE"
            else:
                verdict = "LIE"

            records.append({
                "timestamp":    fmt_ts(seg["start"]),
                "speaker":      speaker,
                "player_name":  player_name,
                "actual_role":  actual_at,
                "believed_role": believed_at,
                "claimed_role": claimed,
                "verdict":      verdict,
                "text":         seg["text"],
            })

    return records


# ── main ─────────────────────────────────────────────────────────────────────

def main(video_id: str = "lF96Jd3Eaeg") -> None:
    print("=== analyze_roles.py ===\n")

    out_dir = Path(f"outputs/{video_id}")

    _patched = out_dir / "segments_patched.csv"
    _orig    = out_dir / "segments.csv"
    segments_csv = _patched if _patched.exists() else _orig

    rttm    = out_dir / "diarization.rttm"
    lie_csv = out_dir / "lie_analysis.csv"

    print(f"Segments: {segments_csv}")

    players = load_lines(Path("players.txt"))
    roles_file = out_dir / "roles.txt"
    if not roles_file.exists():
        roles_file = Path("roles.txt")
    roles = load_lines(roles_file)
    print(f"Loaded {len(players)} players, {len(roles)} roles\n")

    segments   = load_segments(segments_csv)
    rttm_turns = parse_rttm(rttm) if rttm.exists() else []
    print(f"Loaded {len(segments)} segments\n")

    # ── A: build transcript-based roster ──────────────────────────────────────
    print("Building intro roster from transcript ...")
    roster = build_roster(segments, players, roles, rttm_turns)

    # ── A1: intro-timing fallback ─────────────────────────────────────────────
    # For speakers not yet linked by NLP, use the frame_time of each player's
    # intro card to infer which speaker they are.  Works well when each player
    # records a solo monologue that is edited into the video (common format).
    scraped = load_intro_roster(out_dir)
    if scraped and rttm_turns:
        timing_links = link_speakers_from_intro_timing(scraped, rttm_turns, roster)
        for spk, name in timing_links.items():
            if spk not in roster:
                # Find role from scraped data
                info = scraped.get(name.lower(), {})
                roster[spk] = {
                    "name":               name,
                    "believed_role":      (info.get("believed_role") or "unknown").lower(),
                    "actual_role":        (info.get("actual_role")   or "unknown").lower(),
                    "initial_actual_role": (info.get("actual_role")  or "unknown").lower(),
                    "role_history":       [],
                    "source":             "auto_timing",
                }

    # Update roles for NLP-linked speakers using scraped OCR data (more accurate
    # than transcript-guessed roles, especially for deception like Drunk/Marionette).
    # Must run BEFORE manual overrides so the manual values still win.
    if scraped:
        apply_intro_roster(roster, scraped)

    # ── A2: apply manual overrides from explore.py (name→speaker linkage) ───────
    # Applied LAST so manual values always override auto/scraped guesses.
    overrides_file = out_dir / "roster_overrides.json"
    if overrides_file.exists():
        overrides = json.loads(overrides_file.read_text(encoding="utf-8"))
        n_applied = 0
        for spk, info in overrides.items():
            name = info.get("name", "")
            if not name or name == spk:
                continue
            roster[spk] = {
                "name":               name,
                "believed_role":      (info.get("believed_role") or "unknown").lower(),
                "actual_role":        (info.get("actual_role") or
                                       info.get("believed_role") or "unknown").lower(),
                "initial_actual_role": (info.get("actual_role") or
                                        info.get("believed_role") or "unknown").lower(),
                "role_history":       [],
                "source":             "manual",
            }
            n_applied += 1
        if n_applied:
            print(f"\nApplied {n_applied} manual override(s) from {overrides_file}")

    # ── Add Storyteller as a named entity ─────────────────────────────────────
    roster[STORYTELLER_ID] = {
        "name":               "Storyteller",
        "believed_role":      "storyteller",
        "actual_role":        "storyteller",
        "initial_actual_role": "storyteller",
        "role_history":       [],
        "source":             "system",
        "is_storyteller":     True,
    }

    # ── Print roster ──────────────────────────────────────────────────────────
    print("\n=== Roster ===")
    for spk in sorted(roster):
        info = roster[spk]
        if info.get("is_storyteller"):
            print(f"  {spk:12s}  {'Storyteller':20s}  [Storyteller]")
            continue
        diff = (f"  <- believed {info['believed_role']}"
                if info["actual_role"] != info["believed_role"] else "")
        src  = f"  [{info.get('source', '?')}]"
        print(f"  {spk:12s}  {info['name']:20s}  "
              f"actual: {info['actual_role']}{diff}{src}")

    # ── B: storyteller corrections (e.g. "Ben is actually the Drunk") ─────────
    print("\nApplying storyteller corrections ...")
    apply_storyteller_corrections(segments, roster, roles)

    # ── B2: detect mid-game role changes ──────────────────────────────────────
    print("\nDetecting mid-game role changes ...")
    detect_role_changes(segments, roster, roles)

    # Print role changes if any
    changed = [
        (spk, info) for spk, info in roster.items()
        if info.get("role_history") and not info.get("is_storyteller")
    ]
    if changed:
        print(f"\n  {len(changed)} player(s) changed roles during the game:")
        for spk, info in changed:
            for ch in info["role_history"]:
                print(f"    {info['name']}  {ch['previous_role']} -> "
                      f"{ch['actual_role']}  at {fmt_ts(ch['time'])}")

    # ── C: scan game segments for role claims ─────────────────────────────────
    print("\nScanning game segments for role claims ...")
    records = scan_for_lies(segments, roster, roles)
    print(f"Found {len(records)} role claim(s) after the intro.\n")

    # ── D: write output CSV ───────────────────────────────────────────────────
    lie_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp", "speaker", "player_name",
        "actual_role", "believed_role", "claimed_role", "verdict", "text",
    ]
    with lie_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
    print(f"Wrote {len(records)} rows -> {lie_csv}\n")

    # ── E: detect winning team and save to playlist.json ─────────────────────
    winner = detect_winner(segments)
    print(f"Winning team detected: {winner or '(unknown)'}")

    playlist_path = Path("playlist.json")
    if playlist_path.exists():
        playlist_data = json.loads(playlist_path.read_text(encoding="utf-8"))
        entries = playlist_data if isinstance(playlist_data, list) else \
                  playlist_data.get("entries", [])
        for e in entries:
            if e.get("id") == video_id:
                e["winner"] = winner
                break
        playlist_path.write_text(
            json.dumps(playlist_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Saved winner='{winner}' to playlist.json\n")

    # ── G: console summary grouped by player ─────────────────────────────────
    print("=== Lie Analysis Summary ===\n")
    by_player: dict[str, list[dict]] = {}
    for r in records:
        by_player.setdefault(r["player_name"], []).append(r)

    if not by_player:
        print("  (no role claims detected in game segments)")
        return

    for player, claims in sorted(by_player.items()):
        info = next((v for v in roster.values()
                     if v.get("name") == player), None)
        role_str = (
            f"actual: {info['actual_role']}"
            + (f" / believed: {info['believed_role']}"
               if info and info["believed_role"] != info["actual_role"] else "")
            if info else "role unknown"
        )
        # Note any role changes
        changes = info.get("role_history", []) if info else []
        if changes:
            role_str += f"  ({len(changes)} role change(s) during game)"
        print(f"[{player}  —  {role_str}]")

        for c in claims:
            icon = {"TRUE": "[+]", "HONEST MISTAKE": "~",
                    "LIE": "[x]", "UNVERIFIED": "?"}.get(c["verdict"], "?")
            print(f"  {icon} {c['timestamp']}  "
                  f"claimed '{c['claimed_role']}'  ->  {c['verdict']}")
            print(f"       \"{c['text'][:120].replace(chr(10), ' ')}\"")
        print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Analyze roles and detect lies")
    ap.add_argument("video_id", nargs="?", default=None,
                    help="YouTube video ID (omit with --all to process every game)")
    ap.add_argument("--all", action="store_true",
                    help="Process all games that have segments + diarization output")
    args = ap.parse_args()

    if args.all:
        outputs = Path("outputs")
        video_ids = sorted(
            d.name for d in outputs.iterdir()
            if d.is_dir()
            and (d / "diarization.rttm").exists()
            and ((d / "segments_patched.csv").exists() or (d / "segments.csv").exists())
        )
        if not video_ids:
            print("No processable games found under outputs/")
        else:
            print(f"Processing {len(video_ids)} games...\n")
            errors = []
            for vid in video_ids:
                print(f"\n{'='*60}")
                print(f"  {vid}")
                print(f"{'='*60}")
                try:
                    main(vid)
                except Exception as exc:
                    print(f"  [ERROR] {exc}")
                    errors.append((vid, exc))
            print(f"\nDone. {len(video_ids) - len(errors)} OK, {len(errors)} errors.")
            for vid, exc in errors:
                print(f"  {vid}: {exc}")
    else:
        main(args.video_id or "lF96Jd3Eaeg")
