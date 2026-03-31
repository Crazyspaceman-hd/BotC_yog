#!/usr/bin/env python3
"""
build_nomination_execution_outcomes.py — nomination/execution analysis mart
============================================================================
Joins nomination events (N9) with execution episodes (N6), execution
nomination link (N10), execution claim context (N7), and claimed role
outcomes (N8) to produce a single analysis-ready CSV.

One row per nomination event.  All source artifacts are treated as optional
with graceful fallback to blank values so partial coverage does not drop rows.

Answers questions from docs/project_charter.md:
  - which claimed roles attract execution pressure / are nominated most
  - who nominates whom, and what role did the nominator actually have
  - does being Evil change nomination behaviour
  - which nominated players survive vs get executed

Output:
  marts/nomination_execution_outcomes.csv   (default; cross-video mart)

Input artifacts per video (all optional):
  outputs/<id>/nomination_events.csv         N9 — one row per nomination
  outputs/<id>/execution_nomination_link.csv N10 — one row per vote window
  outputs/<id>/execution_episodes.csv        N6 — one row per episode
  outputs/<id>/execution_claim_context.csv   N7 — one row per episode
  outputs/<id>/claimed_role_outcomes.csv     N8 — one row per player

Join paths:
  N9 → N10 on (vote_window_start, round)   — LEFT join; 1-N10 per window
  N10 → N6 on (vote_window_start, round)   — LEFT join; denormalised into N9
  N10 → N7 on (vote_window_start, round)   — LEFT join; claim context
  N8 joined by (video_id, player_name)     — nominee and nominator separately

Cardinality:
  N9 can have multiple nominations per vote window.  Each gets its own row
  even though N10/N6/N7 all have exactly one row per window.  The derived
  per-row fields (this_nominee_is_linked, this_nominee_agrees_with_episode_target,
  this_nominee_matches_execution_result) distinguish which nomination in a
  contested window was the one the episode-level artifacts resolved to.

analysis_confidence_band derivation:
  "high"      fused evidence + linked (not none) + this nomination is the
              linked nominee + confidence >= 0.85
  "medium"    fused or n5_nomination_reference + linked + confidence >= 0.70
  "low"       any link exists but lower evidence strength or confidence
  "unlinked"  no episode link (nomination_link_method == "none" or missing)

Ambiguity is preserved:
  - nomination_evidence_source, nomination_confidence, nomination_link_method
    are passed through verbatim so downstream queries can filter by trust level
  - nomination_count_in_window makes contested windows visible
  - this_nominee_is_linked=false rows exist and are valid; they represent real
    nominations in contested windows that were not the episode's primary link
"""

import argparse
import csv
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO = Path(__file__).parent
OUTPUTS = REPO / "outputs"
MARTS = REPO / "marts"

# ---------------------------------------------------------------------------
# Output columns (order is the contract)
# ---------------------------------------------------------------------------
FIELDNAMES = [
    # Provenance
    "video_id",
    "round",
    "vote_window_start",
    "vote_window_end",
    "nomination_timestamp",
    # Nomination identity
    "nominee",
    "nominator",
    "nomination_evidence_source",
    "nomination_confidence",
    "nomination_source_text",
    # Episode link (from N10)
    "nomination_link_method",
    "nomination_count_in_window",
    "this_nominee_is_linked",
    "this_nominee_agrees_with_episode_target",
    "this_nominee_matches_execution_result",
    # Execution episode (from N6 via N10)
    "episode_likely_target_player",
    "episode_execution_result_player",
    "episode_confidence",
    "episode_supporting_events",
    # Claim context (from N7 — resolved to this nominee where possible)
    "nominee_claimed_role_at_execution",
    "nominee_claim_confidence",
    "nominee_claim_match_status",
    # Nominee role outcomes (from N8)
    "nominee_actual_role",
    "nominee_alignment",
    "nominee_winner",
    "nominee_survived",
    "nominee_was_executed",
    "nominee_lied_about_role",
    # Nominator role outcomes (from N8)
    "nominator_actual_role",
    "nominator_alignment",
    # Derived quality signal
    "analysis_confidence_band",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fkey(vote_window_start: str, round_: str) -> tuple:
    """Normalised float+int join key for vote window lookups."""
    try:
        return (round(float(vote_window_start), 3), int(float(round_)))
    except (ValueError, TypeError):
        return (None, None)


def _load_csv_indexed(path: Path, key_fn) -> dict:
    """Read a CSV and return a dict keyed by key_fn(row).

    If a key collision occurs the last row wins (safe for 1-per-window files).
    Returns {} if path does not exist.
    """
    if not path.exists():
        return {}
    idx = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            k = key_fn(row)
            if k is not None:
                idx[k] = row
    return idx


def _load_csv_multi_indexed(path: Path, key_fn) -> dict:
    """Read a CSV and return a dict keyed by key_fn(row) → list of rows.

    Returns {} if path does not exist.
    """
    if not path.exists():
        return {}
    idx: dict = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            k = key_fn(row)
            if k is not None:
                idx.setdefault(k, []).append(row)
    return idx


def _player_key(video_id: str, player_name: str) -> tuple:
    return (video_id, player_name.strip())


def _confidence_band(
    nomination_evidence_source: str,
    nomination_link_method: str,
    nomination_confidence: str,
    this_nominee_is_linked: bool,
) -> str:
    """Derive a confidence band for downstream filtering."""
    method = nomination_link_method or "none"
    if method == "none":
        return "unlinked"
    try:
        conf = float(nomination_confidence)
    except (ValueError, TypeError):
        conf = 0.0
    src = nomination_evidence_source or ""
    if (
        src == "fused"
        and this_nominee_is_linked
        and conf >= 0.85
    ):
        return "high"
    if src in {"fused", "n5_nomination_reference"} and conf >= 0.70:
        return "medium"
    return "low"


def _bool_str(val: str) -> str:
    """Normalise boolean-ish strings to 'true'/'false'/'' for consistency."""
    if val is None:
        return ""
    v = str(val).strip().lower()
    if v in {"true", "1", "yes"}:
        return "true"
    if v in {"false", "0", "no"}:
        return "false"
    return ""


def _process_video(video_id: str, out_rows: list, verbose: bool) -> dict:
    """Process one video directory and append mart rows to out_rows.

    Returns a stats dict with keys: nominations, linked, unlinked, no_n8_nominee.
    """
    vdir = OUTPUTS / video_id
    stats = {"nominations": 0, "linked": 0, "unlinked": 0, "no_n8_nominee": 0}

    # --- N9: nomination events (source rows) ---
    nom_path = vdir / "nomination_events.csv"
    if not nom_path.exists():
        return stats  # nothing to do

    # --- N10: execution nomination link (one row per vote window) ---
    enl_idx = _load_csv_indexed(
        vdir / "execution_nomination_link.csv",
        lambda r: _fkey(r.get("vote_window_start", ""), r.get("round", "")),
    )

    # --- N6: execution episodes (one row per episode) ---
    ep_idx = _load_csv_indexed(
        vdir / "execution_episodes.csv",
        lambda r: _fkey(r.get("vote_window_start", ""), r.get("round", "")),
    )

    # --- N7: execution claim context (one row per episode) ---
    ecc_idx = _load_csv_indexed(
        vdir / "execution_claim_context.csv",
        lambda r: _fkey(r.get("vote_window_start", ""), r.get("round", "")),
    )

    # --- N8: claimed role outcomes (one row per player per video) ---
    cro_path = vdir / "claimed_role_outcomes.csv"
    cro_idx: dict = {}
    if cro_path.exists():
        with cro_path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pname = row.get("player_name", "").strip()
                if pname:
                    cro_idx[pname] = row

    # --- Stream through N9 rows ---
    with nom_path.open(newline="", encoding="utf-8") as fh:
        for nom in csv.DictReader(fh):
            stats["nominations"] += 1

            nominee = nom.get("nominee", "").strip()
            nominator = nom.get("nominator", "").strip()
            nom_src = nom.get("evidence_source", "")
            nom_conf = nom.get("confidence", "")
            vws = nom.get("vote_window_start", "")
            vwe = nom.get("vote_window_end", "")
            rnd = nom.get("round", "")
            fk = _fkey(vws, rnd)

            # ---- N10 join ----
            enl = enl_idx.get(fk, {})
            link_method = enl.get("nomination_link_method", "") or "none"
            nom_count = enl.get("nomination_count_in_window", "")
            linked_nominee = (enl.get("linked_nominee") or "").strip()
            ep_likely_target = (enl.get("episode_likely_target_player") or "").strip()
            ep_exec_result = (enl.get("episode_execution_result_player") or "").strip()

            # Per-row derived from N10 data
            this_is_linked = bool(linked_nominee and nominee == linked_nominee)
            this_agrees_ep = bool(ep_likely_target and nominee == ep_likely_target)
            this_matches_result = bool(ep_exec_result and nominee == ep_exec_result)

            if link_method != "none":
                stats["linked"] += 1
            else:
                stats["unlinked"] += 1

            # ---- N6 join (episode-level extras) ----
            ep = ep_idx.get(fk, {})
            ep_conf = ep.get("confidence", "")
            ep_events = ep.get("supporting_event_types", "")

            # ---- N7 join (claim context, resolved to this nominee) ----
            ecc = ecc_idx.get(fk, {})
            n7_target_player = (ecc.get("likely_target_player") or "").strip()
            n7_result_player = (ecc.get("execution_result_player") or "").strip()

            # Resolve which claim fields apply to this specific nominee
            if nominee and nominee == n7_target_player:
                nom_claimed_role = ecc.get("target_claimed_role", "")
                nom_claim_conf = ecc.get("target_claim_confidence", "")
                nom_claim_status = ecc.get("target_claim_match_status", "")
            elif nominee and nominee == n7_result_player:
                nom_claimed_role = ecc.get("result_claimed_role", "")
                nom_claim_conf = ecc.get("result_claim_confidence", "")
                nom_claim_status = ecc.get("result_claim_match_status", "")
            else:
                nom_claimed_role = ""
                nom_claim_conf = ""
                nom_claim_status = ""

            # ---- N8 join (nominee) ----
            cro_nom = cro_idx.get(nominee, {})
            if not cro_nom and nominee:
                stats["no_n8_nominee"] += 1
            nom_actual_role = cro_nom.get("actual_role", "")
            nom_alignment = cro_nom.get("alignment", "")
            nom_winner = cro_nom.get("winner", "")
            nom_survived = _bool_str(cro_nom.get("survived", ""))
            nom_was_executed = _bool_str(cro_nom.get("was_executed", ""))
            nom_lied = _bool_str(cro_nom.get("lied_about_role", ""))

            # ---- N8 join (nominator) ----
            cro_ntor = cro_idx.get(nominator, {})
            ntor_actual_role = cro_ntor.get("actual_role", "")
            ntor_alignment = cro_ntor.get("alignment", "")

            # ---- Derived: analysis_confidence_band ----
            band = _confidence_band(nom_src, link_method, nom_conf, this_is_linked)

            out_rows.append({
                "video_id": video_id,
                "round": rnd,
                "vote_window_start": vws,
                "vote_window_end": vwe,
                "nomination_timestamp": nom.get("timestamp_start", ""),
                "nominee": nominee,
                "nominator": nominator,
                "nomination_evidence_source": nom_src,
                "nomination_confidence": nom_conf,
                "nomination_source_text": nom.get("source_text", ""),
                "nomination_link_method": link_method,
                "nomination_count_in_window": nom_count,
                "this_nominee_is_linked": _bool_str(str(this_is_linked)),
                "this_nominee_agrees_with_episode_target": _bool_str(str(this_agrees_ep)),
                "this_nominee_matches_execution_result": _bool_str(str(this_matches_result)),
                "episode_likely_target_player": ep_likely_target,
                "episode_execution_result_player": ep_exec_result,
                "episode_confidence": ep_conf,
                "episode_supporting_events": ep_events,
                "nominee_claimed_role_at_execution": nom_claimed_role,
                "nominee_claim_confidence": nom_claim_conf,
                "nominee_claim_match_status": nom_claim_status,
                "nominee_actual_role": nom_actual_role,
                "nominee_alignment": nom_alignment,
                "nominee_winner": nom_winner,
                "nominee_survived": nom_survived,
                "nominee_was_executed": nom_was_executed,
                "nominee_lied_about_role": nom_lied,
                "nominator_actual_role": ntor_actual_role,
                "nominator_alignment": ntor_alignment,
                "analysis_confidence_band": band,
            })

    if verbose:
        print(
            f"  {video_id}: {stats['nominations']} nominations, "
            f"{stats['linked']} linked, {stats['unlinked']} unlinked, "
            f"{stats['no_n8_nominee']} missing N8 nominee"
        )
    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build nomination_execution_outcomes.csv mart"
    )
    parser.add_argument(
        "--video-id",
        metavar="VIDEO_ID",
        help="Process only this video (for testing)",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=str(MARTS / "nomination_execution_outcomes.csv"),
        help="Output CSV path (default: marts/nomination_execution_outcomes.csv)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress per-video progress output"
    )
    args = parser.parse_args()

    verbose = not args.quiet
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Collect video IDs to process
    if args.video_id:
        video_ids = [args.video_id]
    else:
        video_ids = sorted(
            d.name for d in OUTPUTS.iterdir()
            if d.is_dir() and (d / "nomination_events.csv").exists()
        )

    if not video_ids:
        print("No nomination_events.csv files found under outputs/.", file=sys.stderr)
        sys.exit(1)

    if verbose:
        print(f"Building mart from {len(video_ids)} video(s)...")

    out_rows: list = []
    total_stats = {
        "nominations": 0,
        "linked": 0,
        "unlinked": 0,
        "no_n8_nominee": 0,
        "videos": 0,
    }

    for vid in video_ids:
        stats = _process_video(vid, out_rows, verbose=verbose)
        total_stats["nominations"] += stats["nominations"]
        total_stats["linked"] += stats["linked"]
        total_stats["unlinked"] += stats["unlinked"]
        total_stats["no_n8_nominee"] += stats["no_n8_nominee"]
        total_stats["videos"] += 1

    # Write output
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(out_rows)

    if verbose:
        link_pct = (
            round(100 * total_stats["linked"] / total_stats["nominations"])
            if total_stats["nominations"]
            else 0
        )
        print()
        print("=" * 60)
        print(f"Mart written to: {out_path}")
        print(f"  Videos processed : {total_stats['videos']}")
        print(f"  Total rows        : {total_stats['nominations']}")
        print(f"  Linked to episode : {total_stats['linked']} ({link_pct}%)")
        print(f"  Unlinked          : {total_stats['unlinked']}")
        print(f"  Missing N8 nominee: {total_stats['no_n8_nominee']}")

        # Quick band breakdown
        bands: dict = {}
        for row in out_rows:
            b = row.get("analysis_confidence_band", "")
            bands[b] = bands.get(b, 0) + 1
        print()
        print("  analysis_confidence_band breakdown:")
        for band in ("high", "medium", "low", "unlinked"):
            n = bands.get(band, 0)
            print(f"    {band:10s}: {n}")
        print("=" * 60)


if __name__ == "__main__":
    main()
