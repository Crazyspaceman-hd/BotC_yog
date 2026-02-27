"""patch_transcript.py - Fix name-spelling errors in whisper_segments.jsonl

Reads players.txt and roles.txt, scans the first ~300s of the transcript for
approximate matches to known player names, builds a substitution map, then
produces corrected files:

  outputs/<VIDEO_ID>/whisper_segments_patched.jsonl
  outputs/<VIDEO_ID>/segments_patched.csv

Usage:
    python patch_transcript.py [video_id]
"""

import argparse
import json
import re
import difflib
from pathlib import Path

import pandas as pd

INTRO_CUTOFF = 300.0   # seconds - only scan this window for name mismatches
CERTAIN_THRESHOLD   = 0.85   # auto-apply substitution
UNCERTAIN_THRESHOLD = 0.70   # flag for user review, still apply


# -- helpers -----------------------------------------------------------------

def load_lines(path: Path) -> list[str]:
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def parse_rttm(path: Path) -> list[tuple[float, float, str]]:
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


def best_speaker(a: float, b: float, turns: list) -> str:
    best_spk, best_ov = "UNKNOWN", 0.0
    for s, e, spk in turns:
        ov = max(0.0, min(b, e) - max(a, s))
        if ov > best_ov:
            best_spk, best_ov = spk, ov
    return best_spk


def _player_parts(players: list[str]) -> set[str]:
    """Lowercase set of every individual word that appears in any player name."""
    parts: set[str] = set()
    for p in players:
        for word in p.split():
            parts.add(word.lower())
    return parts


def best_fuzzy(token_lc: str, players: list[str], threshold: float):
    """
    Find the closest player (or player name-part) to `token_lc`.
    Returns (canonical_player_name, ratio) or (None, best_ratio).
    """
    best_name, best_ratio = None, 0.0
    for player in players:
        for part in player.split():
            ratio = difflib.SequenceMatcher(None, token_lc, part.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_name = player
    if best_ratio >= threshold:
        return best_name, best_ratio
    return None, best_ratio


# -- main --------------------------------------------------------------------

def main(video_id: str = "lF96Jd3Eaeg") -> None:
    print("=== patch_transcript.py ===\n")

    out_dir     = Path(f"outputs/{video_id}")
    whisper_in  = out_dir / "whisper_segments.jsonl"
    whisper_out = out_dir / "whisper_segments_patched.jsonl"
    rttm        = out_dir / "diarization.rttm"
    csv_out     = out_dir / "segments_patched.csv"

    # 1 -- load vocabulary
    players = load_lines(Path("players.txt"))
    # Per-game roles override; fall back to global roles.txt
    roles_file = out_dir / "roles.txt"
    if not roles_file.exists():
        roles_file = Path("roles.txt")
    roles = load_lines(roles_file)
    print(f"Loaded {len(players)} players, {len(roles)} roles")

    # All known-correct lowercase name parts (no substitution needed)
    exact_parts = _player_parts(players)

    # 2 -- load whisper segments
    segments: list[dict] = []
    for line in whisper_in.read_text(encoding="utf-8").splitlines():
        if line.strip():
            segments.append(json.loads(line))
    print(f"Loaded {len(segments)} whisper segments\n")

    # 3 -- collect candidate tokens from intro window
    print(f"Scanning intro (first {INTRO_CUTOFF:.0f}s) for name mismatches ...")

    # Gather every capitalised word of 3+ characters from the intro.
    # We keep only the first cased occurrence of each distinct lowercase form.
    token_occurrences: dict[str, str] = {}   # lower -> first cased form
    for seg in segments:
        if seg["start"] > INTRO_CUTOFF:
            break
        for w in re.findall(r"[A-Za-z']{3,}", seg["text"]):
            lc = w.lower()
            if w[0].isupper() and lc not in token_occurrences:
                token_occurrences[lc] = w

    # 4 -- build substitution map
    sub_map: dict[str, str] = {}   # original_token -> replacement_name

    for lc, orig in sorted(token_occurrences.items()):
        # Already an exact match to a known player-name part -> keep as-is
        if lc in exact_parts:
            continue

        match, ratio = best_fuzzy(lc, players, threshold=UNCERTAIN_THRESHOLD)
        if match is None:
            continue

        # Don't create a trivial self-substitution
        if orig.lower() == match.lower():
            continue

        # If match is multi-word, only substitute if we're matching the right part
        # (avoid mapping "The" -> "Alex the Rambler")
        match_parts_lc = [p.lower() for p in match.split()]
        if len(match.split()) > 1:
            best_part_ratio = max(
                difflib.SequenceMatcher(None, lc, p).ratio()
                for p in match_parts_lc
            )
            # Only accept if this token is clearly one of the name's parts
            if best_part_ratio < UNCERTAIN_THRESHOLD:
                continue

        sub_map[orig] = match
        label = "[AUTO]   " if ratio >= CERTAIN_THRESHOLD else "[REVIEW] "
        note  = "" if ratio >= CERTAIN_THRESHOLD else "  <- please verify!"
        print(f"  {label} '{orig}' -> '{match}'  (similarity {ratio:.2f}){note}")

    if not sub_map:
        print("  (no substitutions found)")
    print(f"\n{len(sub_map)} substitution(s) queued.\n")

    # 5 -- apply substitutions to every segment
    if sub_map:
        pattern = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in sub_map) + r")\b"
        )
        def replacer(m: re.Match) -> str:
            return sub_map[m.group(1)]
    else:
        pattern = None

    patched: list[dict] = []
    n_changed = 0
    for seg in segments:
        text = seg["text"]
        new_text = pattern.sub(replacer, text) if pattern else text
        if new_text != text:
            n_changed += 1
        patched.append({"start": seg["start"], "end": seg["end"], "text": new_text})

    # 6 -- write patched JSONL
    with whisper_out.open("w", encoding="utf-8") as f:
        for seg in patched:
            f.write(json.dumps(seg, ensure_ascii=False) + "\n")
    print(f"Wrote {len(patched)} segments ({n_changed} changed) -> {whisper_out}\n")

    # 7 -- re-merge with diarization
    print("Re-merging with diarization ...")
    turns = parse_rttm(rttm)
    rows = []
    for seg in patched:
        spk = best_speaker(seg["start"], seg["end"], turns)
        rows.append({
            "start":   seg["start"],
            "end":     seg["end"],
            "speaker": spk,
            "text":    seg["text"],
        })

    df = pd.DataFrame(rows)
    df.to_csv(csv_out, index=False)
    print(f"Wrote {len(df)} rows -> {csv_out}")
    print(df.head(10).to_string(index=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Patch transcript name-spelling errors")
    ap.add_argument("video_id", nargs="?", default="lF96Jd3Eaeg",
                    help="YouTube video ID")
    main(ap.parse_args().video_id)
