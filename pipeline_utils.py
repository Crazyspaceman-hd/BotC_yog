"""pipeline_utils.py — Shared helpers used across pipeline steps.

Imported by: merge_segments.py, patch_transcript.py, analyze_roles.py,
             auto_assign_speakers.py, fix_rosters.py, build_db.py
"""

import json
from pathlib import Path

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


def resolve_player_name(name: str, aliases: dict[str, str]) -> str:
    """Return canonical player name, applying alias if present."""
    return aliases.get(name, aliases.get(name.lower(), name))


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
