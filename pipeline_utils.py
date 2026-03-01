"""pipeline_utils.py — Shared helpers used across pipeline steps.

Imported by: merge_segments.py, patch_transcript.py, analyze_roles.py
"""

from pathlib import Path


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
