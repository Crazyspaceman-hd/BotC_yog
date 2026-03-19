"""build_execution_claim_context.py — N7: Execution claim context.

Joins N6 execution episodes with N3 role claims to describe what role each
player was publicly claiming near the time they were nominated or executed.

Helps answer:
  - What was the likely_target_player claiming at the time of execution pressure?
  - What was the execution_result_player claiming when they were executed?
  - When does the town pressure converge on a publicly-known claimed role?
  - When does town pressure target one claimed role but execute someone else?

NOT full belief-state reconstruction.  NOT a complete claim timeline.
A narrow, conservative join that looks up the most recent plausible role claim
for each relevant player in an execution episode.

Claim selection policy:
  1. Filter to event_type == "role_claim" with non-empty claimed_role.
  2. Restrict to claims timestamped <= vote_window_end (before/during the vote).
  3. Among qualifying claims, rank by: most recent timestamp first,
     then higher confidence as tiebreaker.
  4. Require confidence >= _CLAIM_MIN_CONF (0.35).
     This is intentionally low to avoid blank-filling due to N3 quality variance.
  5. Report claim_recency_s = vote_window_start - claim_timestamp.
     A "stale" indicator is set when recency > _CLAIM_STALE_S (3600 s).
     Stale claims are NOT rejected — they are included with the staleness flag
     so the consumer can decide whether to trust them.
     The concern is forced inference: we report what existed; we do not guess.
  6. If no qualifying claim exists: leave all claim fields blank (false negative
     preferred over a forced guess).
  7. likely_target_player and execution_result_player are handled independently.
     If they resolve to the same player, both claim sets may point to the same
     underlying claim event.

Output:
    outputs/<id>/execution_claim_context.csv

Usage:
    python build_execution_claim_context.py <video_id>
    python build_execution_claim_context.py <video_id> --force
    python build_execution_claim_context.py --all [--force]
"""

from __future__ import annotations

import argparse
import csv
import json
import traceback
from pathlib import Path

from pipeline_utils import load_player_aliases, resolve_player_name

# ── Paths ──────────────────────────────────────────────────────────────────────

OUTPUTS_DIR = Path("outputs")
PLAYLIST_PATH = Path("playlist.json")

# ── Constants ──────────────────────────────────────────────────────────────────

# Minimum claim confidence to consider.  Very low — N3 typically returns 0.425
# for partial extractions and 0.85 for explicit hard claims.  Setting this at
# 0.35 accepts almost all extractable claims while excluding near-zero noise.
_CLAIM_MIN_CONF: float = 0.35

# Staleness threshold: claims older than this (in seconds before vote_window_start)
# are flagged as potentially stale.  Not rejected — just flagged.  90 minutes
# exceeds typical game duration so most claims clear this; it catches true edge cases.
_CLAIM_STALE_S: float = 5400.0

# Output fieldnames per task spec + implementation additions.
_FIELDNAMES = [
    # Episode identification
    "timestamp_start",
    "timestamp_end",
    "vote_window_start",
    "vote_window_end",
    "round",
    # Players
    "likely_target_player",
    "execution_result_player",
    # Target claim
    "target_claimed_role",
    "target_claim_confidence",
    "target_claim_type",
    "target_claim_text",
    "target_claim_source_timestamp",
    "target_claim_recency_s",
    "target_claim_stale",
    "target_claim_match_status",
    "target_claim_speaker",
    # Result claim
    "result_claimed_role",
    "result_claim_confidence",
    "result_claim_type",
    "result_claim_text",
    "result_claim_source_timestamp",
    "result_claim_recency_s",
    "result_claim_stale",
    "result_claim_match_status",
    "result_claim_speaker",
    # Provenance
    "source",
]

# ── Timestamp helpers ──────────────────────────────────────────────────────────


def _ts_to_seconds(ts: str) -> float:
    """Convert claims.csv timestamp ('MM:SS.ss') to float seconds.

    Accepts MM:SS.ss or plain float-seconds strings.
    """
    ts = ts.strip()
    if ":" in ts:
        parts = ts.split(":")
        try:
            minutes = float(parts[0])
            seconds = float(parts[1])
            return minutes * 60.0 + seconds
        except (IndexError, ValueError):
            pass
    try:
        return float(ts)
    except ValueError:
        return 0.0


# ── Data loaders ───────────────────────────────────────────────────────────────


def _load_episodes(out_dir: Path) -> list[dict]:
    p = out_dir / "execution_episodes.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return rows


def _load_claims(out_dir: Path) -> list[dict]:
    """Load claims.csv, parse timestamps to seconds, sort ascending by time."""
    p = out_dir / "claims.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["timestamp_seconds"] = _ts_to_seconds(r.get("timestamp_start", "0"))
    rows.sort(key=lambda r: r["timestamp_seconds"])
    return rows


# ── Claim selection ────────────────────────────────────────────────────────────


def _best_role_claim(
    player: str,
    claims: list[dict],
    before_t: float,
) -> dict | None:
    """Return the best qualifying role claim for player before before_t.

    Selection policy:
      - event_type == "role_claim"
      - player_name matches player (case-insensitive)
      - timestamp_seconds <= before_t
      - claimed_role is non-empty
      - confidence >= _CLAIM_MIN_CONF
    Among qualifying claims: most recent first; tie-break by confidence descending.
    Returns None if no qualifying claim exists.
    """
    if not player:
        return None

    player_lower = player.lower().strip()
    candidates = []
    for c in claims:
        if c.get("event_type") != "role_claim":
            continue
        if c.get("timestamp_seconds", 0) > before_t:
            continue
        pname = c.get("player_name", "").lower().strip()
        if pname != player_lower:
            continue
        if not c.get("claimed_role", "").strip():
            continue
        try:
            conf = float(c.get("confidence", 0))
        except ValueError:
            conf = 0.0
        if conf < _CLAIM_MIN_CONF:
            continue
        candidates.append((c, conf))

    if not candidates:
        return None

    # Sort priority:
    #   1. Higher confidence first — explicit hard claims (0.85) over garbled
    #      fragments (0.425), regardless of recency.
    #   2. Intro-phase claims second — explicit role reveals under controlled
    #      conditions; tie-break within the same confidence tier.
    #   3. Most recent timestamp last — prefer recency when all else is equal.
    # Rationale: the town's belief about a player's role is primarily driven by
    # their clearest public declaration, not their most recent garbled utterance.
    candidates.sort(
        key=lambda x: (
            x[1],                                                   # conf DESC
            1 if x[0].get("phase", "").lower() == "intro" else 0,  # intro tier DESC
            x[0]["timestamp_seconds"],                              # recency DESC
        ),
        reverse=True,
    )
    return candidates[0][0]


def _claim_match_status(claim: dict) -> str:
    """Determine whether the claim is a verified lie, truthful, or unverified."""
    vl = str(claim.get("verified_lie", "")).strip().lower()
    if vl == "true":
        return "lie"
    actual = (claim.get("actual_role") or "").strip()
    claimed = (claim.get("claimed_role") or "").strip().lower()
    if actual:
        if actual.lower() in claimed or claimed in actual.lower():
            return "truthful"
        return "unverified"
    return "unverified"


def _fill_claim_fields(
    prefix: str,
    claim: dict | None,
    vote_window_start: float,
) -> dict:
    """Build the claim-field dict for a given prefix ('target_' or 'result_').

    Returns a dict with all _FIELDNAMES fields for this prefix, blank if no claim.
    """
    p = prefix
    if claim is None:
        return {
            f"{p}claimed_role": "",
            f"{p}claim_confidence": "",
            f"{p}claim_type": "",
            f"{p}claim_text": "",
            f"{p}claim_source_timestamp": "",
            f"{p}claim_recency_s": "",
            f"{p}claim_stale": "",
            f"{p}claim_match_status": "",
            f"{p}claim_speaker": "",
        }

    ts = claim.get("timestamp_seconds", 0.0)
    recency = vote_window_start - ts
    stale = recency > _CLAIM_STALE_S

    return {
        f"{p}claimed_role": claim.get("claimed_role", "").strip(),
        f"{p}claim_confidence": claim.get("confidence", ""),
        f"{p}claim_type": claim.get("event_type", ""),
        f"{p}claim_text": claim.get("claim_text", "").strip()[:100],
        f"{p}claim_source_timestamp": round(ts, 2),
        f"{p}claim_recency_s": round(recency, 1),
        f"{p}claim_stale": "true" if stale else "false",
        f"{p}claim_match_status": _claim_match_status(claim),
        f"{p}claim_speaker": claim.get("player_name", "").strip(),
    }


# ── Per-video extraction ───────────────────────────────────────────────────────


def extract(video_id: str, force: bool = False) -> bool:
    """Build execution_claim_context.csv for one video.

    Returns True on success, False on skip (no episodes or output exists and !force).
    """
    out_dir = OUTPUTS_DIR / video_id
    out_path = out_dir / "execution_claim_context.csv"

    if out_path.exists() and not force:
        return False

    aliases = load_player_aliases()

    episodes = _load_episodes(out_dir)
    if not episodes:
        print(f"  SKIP {video_id}: no execution_episodes.csv")
        return False

    claims = _load_claims(out_dir)
    if not claims:
        print(f"  NOTE {video_id}: no claims.csv — target_claimed_role fields will be blank")

    print(
        f"  Episodes: {len(episodes)}"
        f"  |  Role claims available: {sum(1 for c in claims if c.get('event_type') == 'role_claim')}"
    )

    rows: list[dict] = []

    for ep in episodes:
        vs_start = float(ep.get("vote_window_start", 0))
        vs_end = float(ep.get("vote_window_end", 0))
        ep_start = float(ep.get("timestamp_start", vs_start))
        ep_end = float(ep.get("timestamp_end", vs_end))

        target = ep.get("likely_target_player", "").strip()
        result = ep.get("execution_result_player", "").strip()

        # Claim lookup cutoff: vote_window_end (include in-vote claims).
        # This captures last-minute claims made during the vote window itself.
        before_t = vs_end

        # Look up best role claim for each player.
        target_claim = _best_role_claim(target, claims, before_t) if target else None
        result_claim = _best_role_claim(result, claims, before_t) if result else None

        row: dict = {
            "timestamp_start": ep_start,
            "timestamp_end": ep_end,
            "vote_window_start": vs_start,
            "vote_window_end": vs_end,
            "round": ep.get("round", ""),
            "likely_target_player": target,
            "execution_result_player": result,
            "source": "N3_claims_join",
        }
        row.update(_fill_claim_fields("target_", target_claim, vs_start))
        row.update(_fill_claim_fields("result_", result_claim, vs_start))
        rows.append(row)

    # Write output — even if all claims are blank, so coverage is visible.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    # Summary.
    target_resolved = sum(1 for r in rows if r.get("target_claimed_role"))
    result_resolved = sum(1 for r in rows if r.get("result_claimed_role"))
    both_resolved = sum(
        1 for r in rows
        if r.get("target_claimed_role") and r.get("result_claimed_role")
    )
    same_player = sum(
        1 for r in rows
        if r.get("likely_target_player")
        and r.get("execution_result_player")
        and r["likely_target_player"].lower() == r["execution_result_player"].lower()
    )
    lies = sum(
        1 for r in rows
        if "lie" in (r.get("target_claim_match_status") or "")
        or "lie" in (r.get("result_claim_match_status") or "")
    )

    print(f"  Wrote {len(rows)} rows -> {out_path.name}")
    print(f"    Target claim resolved:  {target_resolved}/{len(rows)}")
    print(f"    Result claim resolved:  {result_resolved}/{len(rows)}")
    print(f"    Both resolved:          {both_resolved}")
    print(f"    Target == result (same player): {same_player}")
    print(f"    Detected lies in claims: {lies}")

    # Print representative examples.
    if any(r.get("target_claimed_role") or r.get("result_claimed_role") for r in rows):
        print("  Examples:")
        shown = 0
        for r in rows:
            if shown >= 4:
                break
            tgt = r.get("likely_target_player") or ""
            res = r.get("execution_result_player") or ""
            tcr = r.get("target_claimed_role") or ""
            rcr = r.get("result_claimed_role") or ""
            if not (tcr or rcr):
                continue
            print(
                f"    vote={r['vote_window_start']:.0f}-{r['vote_window_end']:.0f}s  "
                f"target={tgt or '(none)'}->claim={tcr or '(blank)'}  "
                f"result={res or '(none)'}->claim={rcr or '(blank)'}"
            )
            shown += 1

    return True


# ── Batch helpers ──────────────────────────────────────────────────────────────


def _all_video_ids() -> list[str]:
    """Return all eligible video IDs (analyzed, public, with execution_episodes.csv)."""
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
            and (OUTPUTS_DIR / e["id"] / "execution_episodes.csv").exists()
        ]
    except Exception:
        return []


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="N7: Join N6 execution episodes with N3 claims for claimed-role-at-execution context."
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
                traceback.print_exc()
                errors += 1
        print(f"\nDone: {done} extracted, {skipped} skipped, {errors} errors.")
    elif args.video_id:
        print(f"=== build_execution_claim_context.py  {args.video_id} ===")
        try:
            extract(args.video_id, force=args.force)
        except Exception as exc:
            print(f"ERROR: {exc}")
            raise
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
