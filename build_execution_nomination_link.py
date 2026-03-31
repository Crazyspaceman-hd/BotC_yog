"""build_execution_nomination_link.py — N10: Nomination–episode link.

Joins N9 nomination_events.csv with N6 execution_episodes.csv to produce one
row per execution episode describing which nomination event (if any) anchors
that episode's vote window.

This is a narrow, conservative join — it does NOT recompute or replace
execution_episodes.csv.  It produces an inspectable side-artifact that
downstream consumers (analytics, N8 enrichment, reporting) can use to:

  - Fill in likely_target context for episodes that N6 could not resolve
    (because N5 had no nomination_reference event for that window)
  - Confirm or cross-check existing N6 targets against explicit nomination evidence
  - Track who nominated whom and with what evidence quality

Join key:  vote_window_start (exact float match — both artifacts derive from the
           same VoteSequence rows in day_events.csv, so timestamps are identical).

Nomination ranking per window:
  When multiple nominations exist within one window, select the best by:
    1. Does nominee match episode execution_result_player?   (strongest signal)
    2. Evidence source priority: fused > n5_nomination_reference > transcript_speech_act
    3. Confidence value (descending)

nomination_link_method values:
  direct_match_result  — selected nomination's nominee == execution_result_player
  single               — exactly one nomination in window (no ambiguity)
  highest_confidence   — multiple nominations; winner clear; no result to compare
  ambiguous_multi      — multiple nominations, tied in priority; emits best-available
                         but flags ambiguity explicitly; consumer should inspect
  none                 — no nominations found for this window

Output always includes one row per execution episode (including no-nomination
episodes), so coverage is fully visible.

Output:
    outputs/<id>/execution_nomination_link.csv

Usage:
    python build_execution_nomination_link.py <video_id>
    python build_execution_nomination_link.py <video_id> --force
    python build_execution_nomination_link.py --all [--force]
"""

from __future__ import annotations

import argparse
import csv
import json
import traceback
from pathlib import Path

from pipeline_utils import load_player_aliases, load_roster

# ── Paths ───────────────────────────────────────────────────────────────────────

OUTPUTS_DIR = Path("outputs")
PLAYLIST_PATH = Path("playlist.json")

# ── Constants ───────────────────────────────────────────────────────────────────

# Floating-point tolerance for vote_window_start matching.
# Both artifacts derive from the same day_events.csv timestamps, so these
# should be identical; a small epsilon guards against float-repr drift.
_JOIN_EPSILON: float = 0.5

# Source priority for nomination ranking (higher = more trusted).
_SOURCE_PRIORITY: dict[str, int] = {
    "fused": 3,
    "n5_nomination_reference": 2,
    "transcript_speech_act": 1,
}

# ── Output schema ────────────────────────────────────────────────────────────────

_FIELDNAMES = [
    # Episode identification (join key + context)
    "vote_window_start",
    "vote_window_end",
    "round",
    # Nomination link
    "linked_nominee",
    "linked_nominee_role",
    "linked_nominator",
    "linked_nomination_timestamp",
    "linked_nomination_source",
    "linked_nomination_confidence",
    "nomination_link_method",
    # Episode context (carried from N6 for easy inspection without a join)
    "episode_likely_target_player",
    "episode_execution_result_player",
    # Agreement flags
    "nominee_agrees_with_episode_target",
    "nominee_matches_result",
    # Window-level counts
    "nomination_count_in_window",
]

# ── Data loaders ─────────────────────────────────────────────────────────────────


def _load_execution_episodes(out_dir: Path) -> list[dict]:
    """Load execution_episodes.csv (N6 output)."""
    p = out_dir / "execution_episodes.csv"
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _load_nomination_events(out_dir: Path) -> list[dict]:
    """Load nomination_events.csv (N9 output)."""
    p = out_dir / "nomination_events.csv"
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ── Nomination index ─────────────────────────────────────────────────────────────


def _index_nominations(nominations: list[dict]) -> dict[float, list[dict]]:
    """Return {vote_window_start: [nomination_rows]} for fast lookup."""
    index: dict[float, list[dict]] = {}
    for nom in nominations:
        try:
            ws = float(nom.get("vote_window_start", 0))
        except ValueError:
            continue
        index.setdefault(ws, []).append(nom)
    return index


def _find_nominations_for_window(
    ws: float,
    index: dict[float, list[dict]],
) -> list[dict]:
    """Return nominations for a given vote_window_start (with epsilon tolerance)."""
    if ws in index:
        return index[ws]
    # Epsilon scan (rare — only needed if float repr drifts).
    for key, noms in index.items():
        if abs(key - ws) <= _JOIN_EPSILON:
            return noms
    return []


# ── Nomination ranking ────────────────────────────────────────────────────────────


def _rank_key(nom: dict, result_player: str) -> tuple:
    """Sort key for a nomination candidate (higher = better).

    Priority:
      1. Nominee matches execution_result_player (strongest confirmation).
      2. Evidence source priority (fused > n5_nomination_reference > transcript).
      3. Confidence value.
    """
    nominee = nom.get("nominee", "").strip().lower()
    result_lower = result_player.strip().lower() if result_player else ""
    matches_result = 1 if (result_lower and nominee == result_lower) else 0
    source_pri = _SOURCE_PRIORITY.get(nom.get("evidence_source", ""), 0)
    try:
        conf = float(nom.get("confidence", 0))
    except ValueError:
        conf = 0.0
    return (matches_result, source_pri, conf)


def _select_best_nomination(
    candidates: list[dict],
    result_player: str,
    roster: dict[str, str],
) -> tuple[dict | None, str]:
    """Select the best nomination from candidates and determine link method.

    Returns (best_nomination_or_None, nomination_link_method).
    """
    if not candidates:
        return None, "none"

    if len(candidates) == 1:
        return candidates[0], "single"

    # Multiple candidates: rank them.
    ranked = sorted(candidates, key=lambda n: _rank_key(n, result_player), reverse=True)
    best = ranked[0]
    second = ranked[1]

    best_key = _rank_key(best, result_player)
    second_key = _rank_key(second, result_player)

    # Best matches execution result player.
    if best_key[0] == 1:  # matches_result
        return best, "direct_match_result"

    # If all top-ranked candidates agree on the same nominee, it's unambiguous
    # (multiple events from different evidence sources, or duplicate transcript hits).
    best_nominee = best.get("nominee", "").strip().lower()
    if all(n.get("nominee", "").strip().lower() == best_nominee for n in ranked):
        return best, "highest_confidence"

    # Best clearly wins on source priority or confidence.
    if best_key[:2] > second_key[:2]:  # strictly better source tier
        return best, "highest_confidence"
    if best_key[1] == second_key[1] and best_key[2] > second_key[2]:  # same source, higher conf
        return best, "highest_confidence"

    # Tied nominees disagree — genuinely ambiguous.
    return best, "ambiguous_multi"


# ── Per-video join ────────────────────────────────────────────────────────────────


def extract(video_id: str, force: bool = False) -> bool:
    """Build execution_nomination_link.csv for one video.

    Returns True on success, False on skip.
    """
    out_dir = OUTPUTS_DIR / video_id
    out_path = out_dir / "execution_nomination_link.csv"

    if out_path.exists() and not force:
        return False

    aliases = load_player_aliases()
    roster = load_roster(out_dir, aliases)

    episodes = _load_execution_episodes(out_dir)
    if not episodes:
        print(f"  SKIP {video_id}: no execution_episodes.csv")
        return False

    nominations = _load_nomination_events(out_dir)
    nom_index = _index_nominations(nominations)

    print(
        f"  Episodes: {len(episodes)}"
        f"  |  Nominations available: {len(nominations)}"
        f"  |  Unique nomination windows: {len(nom_index)}"
    )

    output_rows: list[dict] = []

    for ep in episodes:
        try:
            ws = float(ep.get("vote_window_start", 0))
            we = float(ep.get("vote_window_end", ws))
        except ValueError:
            continue

        round_num = ep.get("round", "")
        ep_target = ep.get("likely_target_player", "").strip()
        ep_result = ep.get("execution_result_player", "").strip()

        candidates = _find_nominations_for_window(ws, nom_index)
        best_nom, link_method = _select_best_nomination(candidates, ep_result, roster)

        if best_nom:
            linked_nominee = best_nom.get("nominee", "").strip()
            linked_nominator = best_nom.get("nominator", "").strip()
            try:
                linked_ts = float(best_nom.get("timestamp_start", 0))
            except ValueError:
                linked_ts = 0.0
            linked_source = best_nom.get("evidence_source", "")
            try:
                linked_conf = float(best_nom.get("confidence", 0))
            except ValueError:
                linked_conf = 0.0
            linked_role = roster.get(linked_nominee, "")

            agrees_with_target = ""
            if linked_nominee and ep_target:
                agrees_with_target = "true" if linked_nominee.lower() == ep_target.lower() else "false"

            matches_result = ""
            if linked_nominee and ep_result:
                matches_result = "true" if linked_nominee.lower() == ep_result.lower() else "false"
        else:
            linked_nominee = ""
            linked_nominator = ""
            linked_ts = ""
            linked_source = ""
            linked_conf = ""
            linked_role = ""
            agrees_with_target = ""
            matches_result = ""

        output_rows.append(
            {
                "vote_window_start": ws,
                "vote_window_end": we,
                "round": round_num,
                "linked_nominee": linked_nominee,
                "linked_nominee_role": linked_role,
                "linked_nominator": linked_nominator,
                "linked_nomination_timestamp": round(linked_ts, 2) if isinstance(linked_ts, float) else "",
                "linked_nomination_source": linked_source,
                "linked_nomination_confidence": round(linked_conf, 3) if isinstance(linked_conf, float) else "",
                "nomination_link_method": link_method,
                "episode_likely_target_player": ep_target,
                "episode_execution_result_player": ep_result,
                "nominee_agrees_with_episode_target": agrees_with_target,
                "nominee_matches_result": matches_result,
                "nomination_count_in_window": len(candidates),
            }
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(output_rows)

    # ── Summary ──────────────────────────────────────────────────────────────────

    linked_count = sum(1 for r in output_rows if r["linked_nominee"])
    unresolved_gain = sum(
        1 for r in output_rows
        if r["linked_nominee"] and not r["episode_likely_target_player"]
    )
    confirmed_count = sum(
        1 for r in output_rows
        if r["nominee_agrees_with_episode_target"] == "true"
    )
    disagree_count = sum(
        1 for r in output_rows
        if r["nominee_agrees_with_episode_target"] == "false"
    )
    result_match_count = sum(
        1 for r in output_rows
        if r["nominee_matches_result"] == "true"
    )
    method_counts: dict[str, int] = {}
    for r in output_rows:
        m = r["nomination_link_method"]
        method_counts[m] = method_counts.get(m, 0) + 1

    print(f"  Wrote {len(output_rows)} rows -> {out_path.name}")
    print(f"    Linked (any method):          {linked_count}/{len(output_rows)}")
    print(f"    New target context (was blank): {unresolved_gain}")
    print(f"    Agrees with episode target:   {confirmed_count}")
    print(f"    Disagrees with episode target:{disagree_count}")
    print(f"    Nominee matches result player:{result_match_count}")
    print(f"    Link methods: {method_counts}")

    if output_rows:
        print("  Sample rows:")
        shown = 0
        for r in output_rows:
            if shown >= 6:
                break
            if not r["linked_nominee"] and shown > 2:
                continue
            print(
                f"    round={r['round']}  ws={r['vote_window_start']:.0f}s"
                f"  nominee={r['linked_nominee'] or '—'}"
                f"  nominator={r['linked_nominator'] or '—'}"
                f"  method={r['nomination_link_method']}"
                f"  ep_target={r['episode_likely_target_player'] or '—'}"
                f"  agrees={r['nominee_agrees_with_episode_target'] or '—'}"
            )
            shown += 1

    return True


# ── Batch helpers ─────────────────────────────────────────────────────────────────


def _all_video_ids() -> list[str]:
    """Return all eligible video IDs with execution_episodes.csv."""
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


# ── CLI ────────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="N10: Join N9 nominations with N6 execution episodes."
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
                if extract(vid, force=args.force):
                    done += 1
                else:
                    skipped += 1
            except Exception as exc:
                print(f"  ERROR: {exc}")
                traceback.print_exc()
                errors += 1
        print(f"\nDone: {done} extracted, {skipped} skipped, {errors} errors.")
    elif args.video_id:
        print(f"=== build_execution_nomination_link.py  {args.video_id} ===")
        try:
            extract(args.video_id, force=args.force)
        except Exception as exc:
            print(f"ERROR: {exc}")
            raise
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
