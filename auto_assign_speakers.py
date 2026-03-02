"""auto_assign_speakers.py — Heuristic speaker auto-assignment

Reads the full transcript for each unlinked speaker and applies pattern-matching
heuristics to propose player->speaker mappings.

Heuristics (in priority order):
  1. Storyteller detection  — speaks near t=0, uses intro/phase phrases  -> CERTAIN
  2. Role self-declaration  — "I am/I'm the [role]" matches exactly one
                              roster player's actual or believed role      -> HIGH
  3. Process of elimination — N-1 speakers assigned, 1 unassigned player  -> MEDIUM

Usage:
  python auto_assign_speakers.py              # dry run, print report
  python auto_assign_speakers.py --apply      # write HIGH/CERTAIN assignments
  python auto_assign_speakers.py --apply --include-medium   # also write MEDIUM
  python auto_assign_speakers.py --video <id> # single video only
  python auto_assign_speakers.py --out report.json  # save full report to JSON
"""

import argparse
import csv
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────

OUTPUTS_DIR = Path("outputs")
PLAYLIST    = Path("playlist.json")
ROLES_TXT   = Path("roles.txt")

# ─── Constants ────────────────────────────────────────────────────────────────

CERTAIN = "CERTAIN"
HIGH    = "HIGH"
MEDIUM  = "MEDIUM"
LOW     = "LOW"

_CONFIDENCE_ORDER = {CERTAIN: 4, HIGH: 3, MEDIUM: 2, LOW: 1}

# Role aliases (same as elsewhere in the pipeline)
_ROLE_ALIASES: dict[str, str] = {
    "evil santa al-madikhia": "al-hadikhia",
    "al-madikhia":            "al-hadikhia",
    "evil santa":             "al-hadikhia",
}

# ─── Storyteller phrase detection ────────────────────────────────────────────
# Any single match from STRONG -> CERTAIN confidence.
# Three or more WEAK matches at t < 120s -> CERTAIN.

_ST_STRONG: list[str] = [
    "welcome to blood on the clocktower",
    "welcome to blood on the clock tower",
    "welcome one and all",
    "everyone close your eyes",
    "everyone please close your eyes",
    "all players close your eyes",
    "good morning everyone",          # Storyteller wakes the town
    "the town wakes up",
    "it is now day",
    "it is now night",
    "night falls",
    "time for nominations",
    "nominations are open",
    "we are going to start",
    "we are going to begin",
    "let's start the game",
    "let's begin the game",
    "your role is",                   # telling a player their role in setup
    "you are the storyteller",
    "i am the storyteller",
]

_ST_WEAK: list[str] = [
    "blood on the clocktower",
    "blood on the clock tower",
    "clocktower",
    "good morning",
    "good evening",
    "the demon",
    "the minion",
    "wake up",
    "go to sleep",
    "open your eyes",
    "you may now open",
    "you are good",
    "you are evil",
    "nominate",
    "execute",
]


def _lc(s: str) -> str:
    return s.lower()


def _storyteller_score(lines: list[tuple[float, str]]) -> tuple[float, list[str]]:
    """Return (confidence_0_to_1, evidence_strings)."""
    strong_hits: list[str] = []
    weak_hits:   list[str] = []
    early_weak   = 0

    for t, txt in lines:
        lctxt = _lc(txt)
        for phrase in _ST_STRONG:
            if phrase in lctxt:
                strong_hits.append(f"'{txt[:100]}' at {t:.1f}s (strong phrase: '{phrase}')")
        for phrase in _ST_WEAK:
            if phrase in lctxt:
                weak_hits.append(f"'{txt[:80]}' at {t:.1f}s (weak: '{phrase}')")
                if t < 120:
                    early_weak += 1

    if strong_hits:
        return 1.0, strong_hits[:3]
    if early_weak >= 3:
        return 1.0, weak_hits[:3]
    if early_weak >= 2:
        return 0.85, weak_hits[:2]
    if weak_hits:
        return 0.5, weak_hits[:1]
    return 0.0, []


# ─── Role declaration extraction ─────────────────────────────────────────────
# Captures: "I am the X", "I'm the X", "I am a X", "I'm a X", "I am X"

_ROLE_CLAIM_RE = re.compile(
    r"\bi(?:'m| am)\s+(?:actually\s+)?(?:the\s+|a\s+)?([a-z][a-z '\-]{2,30}?)(?:\.|,|\s*$|\s+(?:and|but|so|which|that|in|on|because|it|i))",
    re.IGNORECASE,
)

# False-positive filters — common phrases that match the regex but aren't roles
_NOT_ROLES = {
    "not", "sure", "going", "thinking", "worried", "trying", "just",
    "good", "evil", "the only", "right", "wrong", "dead", "alive",
    "pretty", "basically", "actually", "kind of", "sort of",
    "telling the truth", "lying", "bluffing",
}


def _extract_role_claims(lines: list[tuple[float, str]]) -> list[tuple[str, float, str]]:
    """Return [(claimed_role_lc, timestamp, original_text), ...]."""
    claims = []
    for t, txt in lines:
        for m in _ROLE_CLAIM_RE.finditer(txt):
            raw = m.group(1).strip().lower()
            if raw and raw not in _NOT_ROLES and len(raw) >= 3:
                claims.append((raw, t, txt))
    return claims


# ─── Role fuzzy matching ──────────────────────────────────────────────────────


def _load_known_roles() -> list[str]:
    if not ROLES_TXT.exists():
        return []
    return [ln.strip().lower() for ln in ROLES_TXT.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _fuzzy_role_match(claimed: str, known_roles: list[str], threshold: float = 0.80) -> str:
    """Return best-matching canonical role name, or '' if below threshold."""
    claimed_lc = claimed.lower().strip()

    # Exact match first
    if claimed_lc in known_roles:
        return claimed_lc

    # Alias check
    if claimed_lc in _ROLE_ALIASES:
        return _ROLE_ALIASES[claimed_lc]

    # Fuzzy
    best_score = 0.0
    best_role  = ""
    for role in known_roles:
        score = SequenceMatcher(None, claimed_lc, role).ratio()
        if score > best_score:
            best_score = score
            best_role  = role
    return best_role if best_score >= threshold else ""


def _match_claim_to_player(
    claimed_role:       str,
    unassigned_players: list[dict],
    all_players:        list[dict],
    known_roles:        list[str],
) -> tuple[dict | None, str, float]:
    """
    Try to match a claimed role to a player.

    Returns:
        (player_dict, evidence_str, confidence_0_to_1)
        or (None, reason_str, 0.0) if no match or ambiguous.
    """
    canonical = _fuzzy_role_match(claimed_role, known_roles)
    if not canonical:
        return None, f"Claimed role '{claimed_role}' not recognised", 0.0

    # Check unassigned players (actual_role and believed_role)
    matches = []
    for p in unassigned_players:
        actual  = _ROLE_ALIASES.get(p.get("actual_role",   "").lower(), p.get("actual_role",   "").lower())
        believed = _ROLE_ALIASES.get(p.get("believed_role", "").lower(), p.get("believed_role", "").lower())

        actual_match   = _fuzzy_role_match(canonical, [actual],   0.88)
        believed_match = _fuzzy_role_match(canonical, [believed], 0.88)

        if actual_match:
            matches.append((p, "actual_role",   0.90))
        elif believed_match:
            # Speaker claims their BELIEVED role (e.g. Marionette thinks they're Empath)
            matches.append((p, "believed_role", 0.88))

    if len(matches) == 1:
        p, match_field, conf = matches[0]
        field_note = "" if match_field == "actual_role" else " (believed role — likely deceived)"
        return p, f"Claimed '{claimed_role}' -> {p['name']} ({match_field}={p[match_field]}){field_note}", conf

    if len(matches) > 1:
        names = [m[0]["name"] for m in matches]
        return None, f"Claimed role '{canonical}' is ambiguous: matches {names}", 0.0

    # Check if the role belongs to an already-assigned player (lying / diarization split)
    for p in all_players:
        actual   = _ROLE_ALIASES.get(p.get("actual_role",   "").lower(), p.get("actual_role",   "").lower())
        believed = _ROLE_ALIASES.get(p.get("believed_role", "").lower(), p.get("believed_role", "").lower())
        if _fuzzy_role_match(canonical, [actual], 0.88) or _fuzzy_role_match(canonical, [believed], 0.88):
            return None, (
                f"Claimed role '{canonical}' belongs to {p['name']} who is already assigned — "
                f"likely a diarization split or bluff"
            ), 0.0

    return None, f"Claimed role '{canonical}' not found among unassigned players", 0.0


# ─── I/O helpers ─────────────────────────────────────────────────────────────


def _load_playlist() -> list[dict]:
    if not PLAYLIST.exists():
        return []
    try:
        return json.loads(PLAYLIST.read_text(encoding="utf-8")).get("entries", [])
    except Exception:
        return []


def _load_overrides(vid: str) -> dict:
    f = OUTPUTS_DIR / vid / "roster_overrides.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_overrides(vid: str, data: dict) -> None:
    f = OUTPUTS_DIR / vid / "roster_overrides.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_intro_roster(vid: str) -> list[dict]:
    f = OUTPUTS_DIR / vid / "intro_roster.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8")).get("players", [])
        except Exception:
            pass
    return []


def _load_full_transcripts(vid: str) -> dict[str, list[tuple[float, str]]]:
    """Return {speaker_id: [(timestamp, text), ...]} sorted by timestamp."""
    result: dict[str, list[tuple[float, str]]] = {}
    for fname in ("segments_patched.csv", "segments.csv"):
        p = OUTPUTS_DIR / vid / fname
        if not p.exists():
            continue
        try:
            with p.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    spk = row.get("speaker", "")
                    if not spk or spk in ("speaker_0", "UNKNOWN"):
                        continue
                    t   = float(row.get("start", 0))
                    txt = row.get("text", "").strip()
                    if txt:
                        result.setdefault(spk, []).append((t, txt))
            break
        except Exception:
            continue
    # Sort each speaker's lines by timestamp
    for spk in result:
        result[spk].sort(key=lambda x: x[0])
    return result


# ─── Core analysis per video ─────────────────────────────────────────────────


def analyse_video(
    vid:         str,
    title:       str,
    known_roles: list[str],
) -> dict:
    """
    Return a report dict for one video:
      {
        "video_id", "title",
        "assignments": [assignment_dict, ...],
        "skipped":     [skip_dict, ...],
        "eliminated":  [elim_dict, ...],   # process-of-elimination proposals
      }
    """
    overrides = _load_overrides(vid)
    # Already-assigned speakers (have a real name, not just self-reference)
    assigned_spk: set[str] = {
        k for k, v in overrides.items()
        if v.get("name") and v["name"] not in (k, "")
    }
    assigned_names_lc: set[str] = {
        v["name"].lower() for v in overrides.values()
        if v.get("name")
    }

    intro_players = _load_intro_roster(vid)
    # Apply aliases to intro roles
    for p in intro_players:
        for field in ("actual_role", "believed_role"):
            raw = p.get(field, "").lower()
            p[field] = _ROLE_ALIASES.get(raw, raw)

    unassigned_players = [
        p for p in intro_players
        if p.get("name") and p["name"].lower() not in assigned_names_lc
    ]

    transcripts = _load_full_transcripts(vid)

    # Speakers present in segments but not yet assigned
    unlinked_spk = [
        spk for spk in sorted(transcripts.keys())
        if spk not in assigned_spk
    ]

    if not unlinked_spk:
        return {"video_id": vid, "title": title,
                "assignments": [], "skipped": [], "eliminated": []}

    assignments: list[dict] = []
    skipped:     list[dict] = []

    # Track what we've matched so far (for process-of-elimination)
    newly_assigned_names: set[str] = set()

    # ── Pass 1: Storyteller + role declarations ───────────────────────────────
    for spk in unlinked_spk:
        lines = transcripts[spk]
        result: dict | None = None

        # 1a. Storyteller detection
        st_conf, st_evidence = _storyteller_score(lines)
        if st_conf >= 0.85:
            result = {
                "speaker_id":           spk,
                "proposed_name":        "Storyteller",
                "proposed_actual_role": "storyteller",
                "proposed_believed_role": "storyteller",
                "confidence":           CERTAIN,
                "evidence":             st_evidence,
            }

        # 1b. Role declaration — only if not already identified as Storyteller
        if result is None:
            claims = _extract_role_claims(lines)
            # Deduplicate by role
            seen_claims: dict[str, tuple[str, float, str]] = {}
            for raw_role, t, txt in claims:
                if raw_role not in seen_claims:
                    seen_claims[raw_role] = (raw_role, t, txt)
            unique_claims = list(seen_claims.values())

            for raw_role, t, txt in unique_claims:
                # Skip if the claim is clearly the Storyteller's "your role is X" phrase
                if "your role is" in txt.lower() or "you are the" in txt.lower():
                    continue

                player, evidence_str, conf = _match_claim_to_player(
                    raw_role, unassigned_players, intro_players, known_roles
                )

                if player and conf >= 0.85:
                    result = {
                        "speaker_id":             spk,
                        "proposed_name":          player["name"],
                        "proposed_actual_role":   player.get("actual_role", ""),
                        "proposed_believed_role": player.get("believed_role", ""),
                        "confidence":             HIGH,
                        "evidence":               [
                            f"Says '{txt[:120]}' at {t:.1f}s",
                            evidence_str,
                        ],
                    }
                    break  # Take first good match

        if result:
            assignments.append(result)
            if result["proposed_name"] != "Storyteller":
                newly_assigned_names.add(result["proposed_name"].lower())
                # Remove matched player from unassigned for subsequent speakers
                unassigned_players = [
                    p for p in unassigned_players
                    if p["name"].lower() != result["proposed_name"].lower()
                ]
        else:
            skipped.append({
                "speaker_id":     spk,
                "reason":         "No confident match found",
                "sample_lines":   [txt[:120] for _, txt in lines[:3]],
                "n_lines":        len(lines),
                "first_t":        lines[0][0] if lines else 0.0,
            })

    # ── Pass 2: Process of elimination ───────────────────────────────────────
    # Recalculate who is still unlinked / unassigned after pass 1
    assigned_in_pass1 = {a["speaker_id"] for a in assignments}
    remaining_unlinked = [s for s in unlinked_spk if s not in assigned_in_pass1]
    # Storytellers don't consume an intro player
    remaining_unassigned = unassigned_players  # already shrunk during pass 1

    eliminated: list[dict] = []

    if len(remaining_unlinked) == 1 and len(remaining_unassigned) == 1:
        spk    = remaining_unlinked[0]
        player = remaining_unassigned[0]
        lines  = transcripts[spk]
        eliminated.append({
            "speaker_id":             spk,
            "proposed_name":          player["name"],
            "proposed_actual_role":   player.get("actual_role", ""),
            "proposed_believed_role": player.get("believed_role", ""),
            "confidence":             MEDIUM,
            "evidence":               [
                f"Process of elimination: only remaining unlinked speaker "
                f"and only remaining unassigned player ({player['name']}, "
                f"{player.get('actual_role', '?')})",
            ],
        })
        # Move from skipped to eliminated
        skipped = [s for s in skipped if s["speaker_id"] != spk]

    return {
        "video_id":    vid,
        "title":       title,
        "assignments": assignments,
        "skipped":     skipped,
        "eliminated":  eliminated,
    }


# ─── Apply assignments ────────────────────────────────────────────────────────


def apply_report(
    report:          list[dict],
    include_medium:  bool = False,
    dry_run:         bool = True,
) -> tuple[int, int]:
    """Write approved assignments to roster_overrides.json files.

    Returns (n_written, n_skipped).
    """
    written = 0
    skipped = 0

    for vid_report in report:
        vid         = vid_report["video_id"]
        proposals   = list(vid_report["assignments"]) + (
            vid_report["eliminated"] if include_medium else []
        )

        if not proposals:
            continue

        ov_data = _load_overrides(vid)
        changed = False

        for prop in proposals:
            conf  = prop["confidence"]
            spk   = prop["speaker_id"]
            name  = prop["proposed_name"]
            act   = prop["proposed_actual_role"]
            bel   = prop["proposed_believed_role"]

            if _CONFIDENCE_ORDER.get(conf, 0) < _CONFIDENCE_ORDER[HIGH]:
                skipped += 1
                continue
            if conf == MEDIUM and not include_medium:
                skipped += 1
                continue

            if dry_run:
                written += 1
                continue

            ov_data[spk] = {
                "name":          name,
                "actual_role":   act,
                "believed_role": bel,
            }
            changed = True
            written += 1

        if changed and not dry_run:
            _save_overrides(vid, ov_data)

    return written, skipped


# ─── Pretty printing ──────────────────────────────────────────────────────────

_CONF_COLOUR = {
    CERTAIN: "\033[92m",  # green
    HIGH:    "\033[94m",  # blue
    MEDIUM:  "\033[93m",  # yellow
    LOW:     "\033[91m",  # red
}
_RESET = "\033[0m"


def _conf_str(conf: str) -> str:
    return f"{_CONF_COLOUR.get(conf, '')}{conf}{_RESET}"


def print_report(report: list[dict], include_medium: bool = False) -> None:
    total_proposals = 0
    total_skipped   = 0

    for vr in report:
        proposals = list(vr["assignments"])
        if include_medium:
            proposals += vr["eliminated"]
        else:
            # Still show eliminations in output but mark as pending
            pass

        if not proposals and not vr["skipped"] and not vr["eliminated"]:
            continue

        print(f"\n{'-'*72}")
        print(f"  {vr['title'][:65]}")
        print(f"  {vr['video_id']}")
        print(f"{'-'*72}")

        for p in proposals:
            conf = p["confidence"]
            print(
                f"  {_conf_str(conf):20}  {p['speaker_id']:12}  ->  "
                f"{p['proposed_name']} ({p['proposed_actual_role']})"
            )
            for ev in p.get("evidence", [])[:2]:
                print(f"      {ev[:100]}")
            total_proposals += 1

        for p in vr["eliminated"]:
            conf = p["confidence"]
            marker = "  ✓ would apply" if include_medium else "  (pending --include-medium)"
            print(
                f"  {_conf_str(conf):20}  {p['speaker_id']:12}  ->  "
                f"{p['proposed_name']} ({p['proposed_actual_role']}){marker}"
            )
            for ev in p.get("evidence", [])[:1]:
                print(f"      {ev[:100]}")

        for s in vr["skipped"]:
            print(f"  {'SKIP':20}  {s['speaker_id']:12}     {s['reason'][:60]}")
            for ln in s["sample_lines"][:2]:
                print(f"      \"{ln[:90]}\"")
            total_skipped += 1

    print(f"\n{'='*72}")
    certain_high = sum(
        1 for vr in report
        for p in list(vr["assignments"]) + (vr["eliminated"] if include_medium else [])
        if _CONFIDENCE_ORDER.get(p["confidence"], 0) >= _CONFIDENCE_ORDER[HIGH]
    )
    elim_count = sum(len(vr["eliminated"]) for vr in report)
    print(f"  CERTAIN/HIGH  : {certain_high} assignments ready to apply")
    if not include_medium:
        print(f"  MEDIUM        : {elim_count} process-of-elimination (add --include-medium)")
    print(f"  SKIPPED       : {total_skipped} speakers need manual review in fix_rosters.py")
    print(f"{'='*72}")


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Heuristic speaker auto-assignment for BotC transcripts."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Write CERTAIN and HIGH confidence assignments to roster_overrides.json",
    )
    parser.add_argument(
        "--include-medium", action="store_true",
        help="Also apply MEDIUM confidence (process-of-elimination) assignments",
    )
    parser.add_argument(
        "--video", metavar="VIDEO_ID",
        help="Process a single video ID instead of the whole playlist",
    )
    parser.add_argument(
        "--out", metavar="PATH",
        help="Write full report JSON to this path",
    )
    args = parser.parse_args()

    known_roles = _load_known_roles()

    # Build video list
    if args.video:
        entries = [(args.video, args.video, False)]
        # Try to get title from playlist
        for e in _load_playlist():
            if e["id"] == args.video:
                entries = [(e["id"], e.get("title", e["id"]), e.get("members", False))]
                break
    else:
        playlist = _load_playlist()
        if not playlist:
            print("ERROR: playlist.json not found. Run fetch_playlist.py first.")
            return
        entries = [(e["id"], e.get("title", e["id"]), e.get("members", False))
                   for e in playlist]

    # Only process videos that have segments + unlinked speakers
    processable = []
    for vid, title, _ in entries:
        segs = OUTPUTS_DIR / vid / "segments_patched.csv"
        if not segs.exists():
            segs = OUTPUTS_DIR / vid / "segments.csv"
        if segs.exists():
            processable.append((vid, title))

    if not processable:
        print("No videos with transcripts found.")
        return

    print(f"Scanning {len(processable)} video(s)...")

    report: list[dict] = []
    for vid, title in processable:
        vr = analyse_video(vid, title, known_roles)
        if vr["assignments"] or vr["skipped"] or vr["eliminated"]:
            report.append(vr)

    if not report:
        print("No unlinked speakers found — all speakers are already assigned.")
        return

    print_report(report, include_medium=args.include_medium)

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nReport written to: {args.out}")

    if args.apply:
        dry = False
        mode = "CERTAIN + HIGH"
        if args.include_medium:
            mode += " + MEDIUM"
        print(f"\nApplying {mode} assignments...")
        n_written, n_skipped = apply_report(
            report, include_medium=args.include_medium, dry_run=dry
        )
        print(f"  Written : {n_written}")
        print(f"  Skipped : {n_skipped} (below threshold)")
        print("\nRun next:")
        print("  python analyze_roles.py --all")
        print("  python build_db.py")
        print("  python generate_landing.py")
    else:
        n_would, _ = apply_report(report, include_medium=args.include_medium, dry_run=True)
        if n_would:
            print(f"\nDry run — add --apply to write {n_would} assignment(s).")


if __name__ == "__main__":
    main()
