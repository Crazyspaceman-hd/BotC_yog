"""pipeline_utils.py — Shared helpers used across pipeline steps.

Imported by: merge_segments.py, patch_transcript.py, analyze_roles.py,
             auto_assign_speakers.py, fix_rosters.py, build_db.py,
             validate.py, extract_player_status.py,
             extract_execution_context.py, build_execution_episodes.py,
             build_execution_claim_context.py, build_claimed_role_outcomes.py
"""

import csv
import json
import re
from pathlib import Path

# ── Playlist helpers ───────────────────────────────────────────────────────────

PLAYLIST_JSON = Path("playlist.json")


def load_playlist_entries() -> list[dict]:
    """Load playlist.json entries.

    Handles both list-root and ``{"entries": [...]}`` wrapper formats.
    Returns an empty list if the file is absent or unparseable.
    """
    if not PLAYLIST_JSON.exists():
        return []
    try:
        raw = json.loads(PLAYLIST_JSON.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else raw.get("entries", [])
    except Exception:
        return []


def load_blind_vids() -> frozenset[str]:
    """Return frozenset of video IDs marked as 'blind' in playlist.json."""
    return frozenset(e["id"] for e in load_playlist_entries() if e.get("blind"))


# ── Player alias helpers ───────────────────────────────────────────────────────

PLAYER_ALIASES_FILE = Path("player_aliases.json")


def load_player_aliases() -> dict[str, str]:
    """Load {alias: canonical_name} mapping. Returns {} if file absent."""
    if not PLAYER_ALIASES_FILE.exists():
        return {}
    try:
        return json.loads(PLAYER_ALIASES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def normalize_player(name: str) -> str:
    """Separator-collapsed lookup key for player names.

    Lowercases and removes all whitespace, hyphens, underscores and dots so
    that OCR / transcript variants that differ only in separators collapse to
    the same key before alias lookup.

    Examples:
        'RT Game'  -> 'rtgame'
        'rt-game'  -> 'rtgame'
        'rt_game'  -> 'rtgame'
        'RTGame'   -> 'rtgame'
        'Nilesy'   -> 'nilesy'
        'MsCupcakes' -> 'mscupcakes'
    """
    s = str(name).strip().lower()
    return re.sub(r"[\s\-_\.]+", "", s)


def resolve_player_name(name: str, aliases: dict[str, str]) -> str:
    """Return canonical player name, applying alias if present.

    Resolution order:
      1. Exact key match in aliases.
      2. Case-insensitive (``name.lower()``) match.
      3. Separator-collapsed normalized match via ``normalize_player()``.
      4. Return *name* unchanged.

    Using normalize_player() on both sides means that 'RT Game', 'rt-game',
    'rt_game', and 'RTGame' all resolve to the same canonical name as long
    as any one of those forms is listed as an alias key.
    """
    if name in aliases:
        return aliases[name]
    lower = name.lower()
    if lower in aliases:
        return aliases[lower]
    # Normalized fallback: strip separators and compare both sides.
    key = normalize_player(name)
    for alias, canonical in aliases.items():
        if normalize_player(alias) == key:
            return canonical
    return name


def parse_rttm(path: Path) -> list[tuple[float, float, str]]:
    """Parse a NeMo RTTM file into (start, end, speaker_id) triples."""
    turns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if parts[0] != "SPEAKER":
            continue
        start = float(parts[3])
        dur   = float(parts[4])
        spk   = parts[7]
        turns.append((start, start + dur, spk))
    return turns


def best_speaker(a: float, b: float,
                 turns: list[tuple[float, float, str]]) -> str:
    """Return the speaker with the greatest overlap in segment [a, b]."""
    best_spk, best_ov = "UNKNOWN", 0.0
    for s, e, spk in turns:
        ov = max(0.0, min(b, e) - max(a, s))
        if ov > best_ov:
            best_spk, best_ov = spk, ov
    return best_spk


# ── Role name normalization ────────────────────────────────────────────────────

def normalize_role(role: str) -> str:
    """Canonical comparison form: lowercase, spaces (no underscores).

    Use this for all role comparisons inside the pipeline so that
    'plague_doctor', 'Plague Doctor', and 'plague doctor' all compare equal.
    """
    return str(role).strip().lower().replace("_", " ")


def display_role(role: str) -> str:
    """Canonical storage/display form: Title Case, spaces.

    Use this when writing role names to CSV files or the database so that
    'plague_doctor' → 'Plague Doctor' consistently everywhere.
    """
    return normalize_role(role).title()


# ── Enrichment-node shared helpers ────────────────────────────────────────────

def ts_to_seconds(ts: str) -> float:
    """Parse a timestamp string to float seconds.

    Accepts 'MM:SS.ss' (claims.csv format) or a plain float-string.
    Returns 0.0 on parse failure.
    """
    ts = ts.strip()
    if ":" in ts:
        parts = ts.split(":")
        try:
            return float(parts[0]) * 60.0 + float(parts[1])
        except (IndexError, ValueError):
            pass
    try:
        return float(ts)
    except ValueError:
        return 0.0


def read_csv(path: Path) -> list[dict]:
    """Read a CSV file and return rows as a list of dicts.

    Returns [] if the file does not exist.
    """
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_roster(out_dir: Path, aliases: dict) -> dict[str, str]:
    """Return {canonical_player_name: actual_role} for a video output directory.

    Merges intro_roster.json (preferred source) with roster_overrides.json.
    Storyteller entries are excluded from overrides.
    Keys are canonical player names produced by resolve_player_name().
    Used by N4 (extract_player_status), N5 (extract_execution_context),
    and N6 (build_execution_episodes).
    """
    roster: dict[str, str] = {}

    intro = out_dir / "intro_roster.json"
    if intro.exists():
        try:
            data = json.loads(intro.read_text(encoding="utf-8"))
            for player in data.get("players", []):
                raw = player.get("name", "").strip()
                role = player.get("actual_role", "").strip()
                if raw:
                    roster[resolve_player_name(raw, aliases)] = role
        except Exception:
            pass

    overrides = out_dir / "roster_overrides.json"
    if overrides.exists():
        try:
            ov = json.loads(overrides.read_text(encoding="utf-8"))
            for speaker_id, info in ov.items():
                raw = info.get("name", "").strip()
                role = info.get("actual_role", "").strip()
                if raw and raw.lower() not in ("storyteller", speaker_id.lower()):
                    roster.setdefault(resolve_player_name(raw, aliases), role)
        except Exception:
            pass

    return roster
