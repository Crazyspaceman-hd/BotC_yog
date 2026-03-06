"""extract_claims.py — N3: Claim / suspicion propagation tracking.

Extracts conversational events from the transcript:

    role_claim   — "I am the [role]" / "I'm the [role]" / "my role is..."
    accusation   — "I think [player] is evil/demon/lying"
    suspicion    — "I'm suspicious of [player]" / "I suspect [player]"
    agreement    — "I agree with [player]" / "I trust [player]"
    challenge    — "I don't believe [player]" / "[player] is lying"

Each event is assigned a unique ID, a timestamp, speaker, target (if any),
the matched text, the game phase (from phase_labels.csv if present), and a
confidence score.

Writes:
    outputs/<video_id>/claims.csv        — one row per event
    outputs/<video_id>/claim_graph.json  — nodes + edges (echo/support/challenge)

The lie_analysis.csv is cross-referenced: any role_claim by a player whose
actual role differs gets a `verified_lie` flag.

Usage:
    python extract_claims.py <video_id>
    python extract_claims.py <video_id> --force
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import uuid
from collections import defaultdict
from pathlib import Path

from pipeline_utils import normalize_role, load_player_aliases, resolve_player_name

# ── Event patterns ─────────────────────────────────────────────────────────────
# Each: (event_type, compiled_regex, confidence, capture_groups_meaning)
#   Group 'role'   -> matched role string (for role_claim)
#   Group 'target' -> matched player/target name (for other types)

_ROLE_CLAIM_PATS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"\b(?:i am|i'm|im)\s+(?:the|an?)\s+(?P<role>[a-z][a-z\s]{2,24})", re.I), 0.85),
    (re.compile(r"\bmy role is\s+(?:the\s+)?(?P<role>[a-z][a-z\s]{2,24})", re.I), 0.90),
    (re.compile(r"\bas\s+(?:the|a)\s+(?P<role>[a-z][a-z\s]{2,24})\b", re.I), 0.60),
    (re.compile(r"\b(?:playing|play)\s+(?:the|a)\s+(?P<role>[a-z][a-z\s]{2,24})", re.I), 0.70),
]

# Target-bearing patterns: look for known player names near the keyword
_ACCUSATION_PATS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"\b(?P<target>\w+)\s+is\s+(?:the\s+)?(?:evil|demon|liar|lying|not\s+good)", re.I), 0.80),
    (re.compile(r"\bi (?:accuse|nominate)\s+(?P<target>\w+)", re.I), 0.85),
    (re.compile(r"\bi think\s+(?P<target>\w+)\s+is\s+(?:evil|lying|suspicious|bad|the demon)", re.I), 0.75),
    (re.compile(r"\b(?P<target>\w+)\s+(?:is|seems?)\s+(?:very\s+)?suspicious", re.I), 0.65),
    (re.compile(r"\bkill\s+(?P<target>\w+)", re.I), 0.70),
    (re.compile(r"\bexecute\s+(?P<target>\w+)", re.I), 0.75),
]

_SUSPICION_PATS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"\bi(?:'m| am) (?:suspicious|unsure|not sure) of\s+(?P<target>\w+)", re.I), 0.80),
    (re.compile(r"\bi suspect\s+(?P<target>\w+)", re.I), 0.85),
    (re.compile(r"\b(?P<target>\w+)\s+seems? (?:sus|off|wrong|weird)", re.I), 0.65),
    (re.compile(r"\bnot sure about\s+(?P<target>\w+)", re.I), 0.60),
    (re.compile(r"\bdon'?t trust\s+(?P<target>\w+)", re.I), 0.70),
]

_AGREEMENT_PATS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"\bi agree with\s+(?P<target>\w+)", re.I), 0.85),
    (re.compile(r"\bi (?:believe|trust)\s+(?P<target>\w+)", re.I), 0.80),
    (re.compile(r"\b(?P<target>\w+) (?:is|seems?) (?:good|trustworthy|telling the truth|honest)", re.I), 0.65),
    (re.compile(r"\bsame as\s+(?P<target>\w+)", re.I), 0.55),
]

_CHALLENGE_PATS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"\bi (?:don'?t|do not) believe\s+(?P<target>\w+)", re.I), 0.85),
    (re.compile(r"\b(?P<target>\w+)\s+is\s+(?:lying|a liar|not telling the truth)", re.I), 0.85),
    (re.compile(r"\bi (?:doubt|question)\s+(?P<target>\w+)", re.I), 0.75),
    (re.compile(r"\bthat(?:'s| is) (?:not true|a lie|false)[^.]*(?P<target>\w+)", re.I), 0.65),
]

_ALL_PATS = [
    ("accusation", _ACCUSATION_PATS),
    ("suspicion",  _SUSPICION_PATS),
    ("agreement",  _AGREEMENT_PATS),
    ("challenge",  _CHALLENGE_PATS),
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_segments(out_dir: Path) -> list[dict]:
    for name in ("segments_consistent.csv", "segments_patched.csv", "segments.csv"):
        p = out_dir / name
        if p.exists():
            with p.open(encoding="utf-8", newline="") as f:
                return list(csv.DictReader(f))
    raise FileNotFoundError(f"No segment CSV in {out_dir}")


def _load_roster(out_dir: Path) -> dict[str, str]:
    """Return {player_name_lower: actual_role_normalised} from roster sources.

    roster_overrides.json format: {speaker_id: {name, actual_role, believed_role}}
    intro_roster.json format:     {players: [{name, actual_role, believed_role}]}
    """
    roster: dict[str, str] = {}

    # Prefer roster_overrides.json (speaker-keyed dict)
    p = out_dir / "roster_overrides.json"
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for _spk, info in data.items():
                if not isinstance(info, dict):
                    continue
                pname = (info.get("name") or "").strip()
                role  = info.get("actual_role") or info.get("believed_role") or ""
                if pname and role:
                    roster[pname.lower()] = normalize_role(role)
        if roster:
            return roster

    # Fallback: intro_roster.json (players list)
    p = out_dir / "intro_roster.json"
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        for pl in data.get("players", []):
            pname = (pl.get("name") or "").strip()
            role  = pl.get("actual_role") or pl.get("believed_role") or ""
            if pname and role:
                roster[pname.lower()] = normalize_role(role)
    return roster


def _load_speaker_map(out_dir: Path) -> dict[str, str]:
    """Return {speaker_id: player_name} from roster_overrides.json."""
    spk_map: dict[str, str] = {}
    p = out_dir / "roster_overrides.json"
    if not p.exists():
        return spk_map
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return spk_map
    for spk, info in data.items():
        if isinstance(info, dict):
            pname = (info.get("name") or "").strip()
            if pname:
                spk_map[spk] = pname
    return spk_map


def _load_phase_labels(out_dir: Path) -> list[dict]:
    p = out_dir / "phase_labels.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _phase_at(t: float, labels: list[dict]) -> str:
    for row in labels:
        if float(row["start"]) <= t <= float(row["end"]):
            return row["phase"]
    return "Unknown"


def _load_known_lies(out_dir: Path) -> set[tuple[str, str]]:
    """(player_name_lower, normalised_claimed_role) pairs confirmed as LIE."""
    lies: set[tuple[str, str]] = set()
    p = out_dir / "lie_analysis.csv"
    if not p.exists():
        return lies
    with p.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("verdict") == "LIE":
                player = (row.get("player_name") or "").lower()
                role   = normalize_role(row.get("claimed_role") or "")
                if player and role:
                    lies.add((player, role))
    return lies


def _load_known_roles(video_id: str) -> set[str]:
    """All normalised role names from roles.txt."""
    p = Path("roles.txt")
    if not p.exists():
        return set()
    return {normalize_role(line) for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()}


def _clean_target(raw: str, known_players: set[str]) -> str | None:
    """Resolve a raw matched word to a known player name (case-insensitive)."""
    if not raw:
        return None
    raw_low = raw.strip().lower()
    for pl in known_players:
        if pl.lower() == raw_low:
            return pl
    return None


# ── Core extraction ────────────────────────────────────────────────────────────

def _extract(video_id: str,
             rows: list[dict],
             roster: dict[str, str],
             speaker_map: dict[str, str],
             phase_labels: list[dict],
             known_lies: set[tuple[str, str]],
             known_roles: set[str],
             ) -> list[dict]:
    known_players = set(roster.keys())  # lower-cased
    events: list[dict] = []
    aliases = load_player_aliases()

    for row in rows:
        t     = float(row["start"])
        spk   = row["speaker"]
        text  = row["text"]
        pname = speaker_map.get(spk, spk)  # speaker_id -> player name (or keep ID)
        phase = _phase_at(t, phase_labels) if phase_labels else "Unknown"

        mm, ss = divmod(int(t), 60)
        ts_str = f"{mm:02d}:{ss:02d}.{int((t % 1) * 100):02d}"

        # ── role_claim ────────────────────────────────────────────────────────
        for pat, base_conf in _ROLE_CLAIM_PATS:
            m = pat.search(text)
            if not m:
                continue
            role_raw = m.group("role").strip().rstrip(".,!?")
            role_norm = normalize_role(role_raw)
            # Only register if the claimed role is plausibly a real BotC role
            if len(role_norm) < 3 or role_norm in {
                "player", "person", "one", "role", "good", "evil", "demon"}:
                continue
            if known_roles and role_norm not in known_roles:
                base_conf *= 0.5  # unknown role name -> lower confidence

            pname_low = pname.lower()
            actual_role = roster.get(pname_low, "")
            verified_lie = (pname_low, role_norm) in known_lies

            events.append({
                "event_id":       str(uuid.uuid4())[:8],
                "timestamp_start": ts_str,
                "speaker":         spk,
                "player_name":     pname,
                "target_player":   "",
                "event_type":      "role_claim",
                "claim_text":      text[:120].replace("\n", " "),
                "claimed_role":    role_norm,
                "actual_role":     actual_role,
                "verified_lie":    str(verified_lie).lower(),
                "phase":           phase,
                "confidence":      round(base_conf, 3),
            })
            break  # one role_claim per segment

        # ── target-bearing events ─────────────────────────────────────────────
        for etype, pats in _ALL_PATS:
            for pat, base_conf in pats:
                m = pat.search(text)
                if not m:
                    continue
                try:
                    raw_target = m.group("target")
                except IndexError:
                    raw_target = ""
                target = _clean_target(raw_target, known_players)
                events.append({
                    "event_id":        str(uuid.uuid4())[:8],
                    "timestamp_start": ts_str,
                    "speaker":         spk,
                    "player_name":     pname,
                    "target_player":   target or raw_target,
                    "event_type":      etype,
                    "claim_text":      text[:120].replace("\n", " "),
                    "claimed_role":    "",
                    "actual_role":     "",
                    "verified_lie":    "false",
                    "phase":           phase,
                    "confidence":      round(base_conf, 3),
                })
                break  # first matching pattern wins per type per segment

    return events


# ── Graph builder ──────────────────────────────────────────────────────────────

def _build_graph(events: list[dict]) -> dict:
    """
    Nodes: each event.
    Edges:
      - echo:      same claimed_role within 120s -> echo / support
      - challenge: a challenge event targets the speaker of an earlier role_claim
      - agreement: an agreement event targets the speaker of an earlier claim
    """
    nodes = [{"id": e["event_id"], **{k: v for k, v in e.items()
                                       if k != "event_id"}} for e in events]
    edges = []

    def _ts_to_s(ts: str) -> float:
        try:
            mm, rest = ts.split(":")
            ss, cc   = rest.split(".")
            return int(mm) * 60 + int(ss) + int(cc) / 100
        except Exception:
            return 0.0

    # Index role_claims by player for fast lookup
    claim_by_player: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e["event_type"] == "role_claim":
            claim_by_player[e["player_name"].lower()].append(e)

    for e in events:
        et = _ts_to_s(e["timestamp_start"])
        etype = e["event_type"]

        if etype == "role_claim":
            # Echo: same role claimed by another player within 120s window
            for other in events:
                if (other["event_id"] != e["event_id"]
                        and other["event_type"] == "role_claim"
                        and other["claimed_role"] == e["claimed_role"]
                        and abs(_ts_to_s(other["timestamp_start"]) - et) < 120
                        and other["player_name"] != e["player_name"]):
                    edges.append({"source": e["event_id"],
                                  "target": other["event_id"],
                                  "relation": "echo"})

        elif etype in ("challenge", "agreement"):
            # Link to the most recent role_claim by the target player
            target_low = (e.get("target_player") or "").lower()
            candidates = [c for c in claim_by_player.get(target_low, [])
                          if _ts_to_s(c["timestamp_start"]) < et]
            if candidates:
                closest = max(candidates,
                              key=lambda c: _ts_to_s(c["timestamp_start"]))
                relation = "support" if etype == "agreement" else "challenge"
                edges.append({"source": e["event_id"],
                              "target": closest["event_id"],
                              "relation": relation})

    return {"nodes": nodes, "edges": edges}


# ── Entry point ────────────────────────────────────────────────────────────────

def main(video_id: str, force: bool = False) -> None:
    out_dir   = Path("outputs") / video_id
    csv_path  = out_dir / "claims.csv"
    json_path = out_dir / "claim_graph.json"

    if csv_path.exists() and json_path.exists() and not force:
        print(f"  [SKIP] claims.csv + claim_graph.json already exist for {video_id}")
        return

    print(f"\n=== extract_claims.py — {video_id} ===")

    rows         = _load_segments(out_dir)
    roster       = _load_roster(out_dir)
    speaker_map  = _load_speaker_map(out_dir)
    phase_labels = _load_phase_labels(out_dir)
    known_lies   = _load_known_lies(out_dir)
    known_roles  = _load_known_roles(video_id)

    print(f"  Segments: {len(rows)}  Roster players: {len(roster)}")
    print(f"  Speaker map: {speaker_map}")
    print(f"  Phase labels: {len(phase_labels)} intervals")
    print(f"  Known lies cross-ref: {len(known_lies)}")

    events = _extract(video_id, rows, roster, speaker_map,
                      phase_labels, known_lies, known_roles)

    from collections import Counter
    by_type: Counter = Counter(e["event_type"] for e in events)
    print(f"\n  Events extracted: {len(events)}  breakdown: {dict(by_type)}")

    # Write claims.csv
    fieldnames = ["event_id", "timestamp_start", "speaker", "player_name",
                  "target_player", "event_type", "claim_text", "claimed_role",
                  "actual_role", "verified_lie", "phase", "confidence"]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for e in events:
            w.writerow({k: e.get(k, "") for k in fieldnames})
    print(f"  Wrote {len(events)} rows -> {csv_path}")

    # Write claim_graph.json
    graph = _build_graph(events)
    json_path.write_text(
        json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Wrote {len(graph['nodes'])} nodes, {len(graph['edges'])} edges"
          f" -> {json_path}")

    # Sample
    if events:
        print("\n  Sample events (first 6):")
        for e in events[:6]:
            print(f"    [{e['timestamp_start']}] {e['player_name']:12s} "
                  f"{e['event_type']:12s} conf={e['confidence']}  "
                  f"\"{e['claim_text'][:55]}\"")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Claim / suspicion propagation tracking (N3)")
    ap.add_argument("video_id", help="YouTube video ID")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing output files")
    args = ap.parse_args()
    main(args.video_id, force=args.force)
