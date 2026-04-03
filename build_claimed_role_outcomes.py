#!/usr/bin/env python3
"""
build_claimed_role_outcomes.py — N8 optional enrichment
========================================================
One row per player per video, summarising claimed-role context and outcomes.

Answers questions from docs/project_charter.md:
  - go-to claimed role to die / survive
  - which claimed roles attract execution pressure
  - how claimed role differs from actual role and outcome

Input artifacts (all optional where noted):
  execution_claim_context.csv (N7, preferred for claim-at-key-moment fields)
  execution_episodes.csv      (N6, for pressure / execution counts)
  claims.csv                  (N3, for first / last claim)
  death_events.csv            (N4, for death type / timing)
  intro_roster.json           (F, for actual role)
  roster_overrides.json       (H, supplements intro_roster)
  playlist.json               (root, for winner + video duration)

Output:
  outputs/<video_id>/claimed_role_outcomes.csv

Claim selection policy (conservative, explicit):
  last_claimed_role       — highest-confidence role_claim (ties: most recent)
                            before the end of the game; conf >= _CLAIM_MIN_CONF
  first_claimed_role      — earliest role_claim with conf >= _CLAIM_MIN_CONF
  claimed_role_at_pressure — best-confidence non-blank target_claimed_role from
                             N7 rows where player is the likely_target_player
  claimed_role_at_execution — result_claimed_role from N7 rows where player is
                              the execution_result_player
  If no qualifying claim exists, the field is left blank. Claims are never forced.

Staleness:
  last_claim_stale = "true" if the last_claimed_role was made more than
  _CLAIM_STALE_S seconds before the end of the video (or death event if
  the player died). This captures Intro-only declarations on long games.

Survival:
  survived = "true" if no confirmed death event for the player (conservative:
  absence of a death event does NOT guarantee survival, just that N4 did not
  detect a death; prefer "true" over false positive "false").
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from botc_ui import _team
from pipeline_utils import (
    display_role,
    load_player_aliases,
    normalize_player,
    normalize_role,
    read_csv,
    resolve_player_name,
    ts_to_seconds,
)

# ── Constants ────────────────────────────────────────────────────────────────

_CLAIM_MIN_CONF = 0.35    # mirrors N7; below this a claim is too weak to use
_CLAIM_STALE_S = 5400.0   # 90 minutes; claim made > this before game end = stale
_OUT_FILE = "claimed_role_outcomes.csv"
_STORYTELLER_NAMES = {"storyteller", "st"}

_FIELDNAMES = [
    # Identity
    "video_id",
    "player_name",
    "actual_role",
    "alignment",
    "winner",
    # Outcome
    "survived",
    "died",
    "was_executed",
    "death_type",
    "death_timestamp",
    "execution_timestamp",
    "survived_to_end",
    # General claims (from N3)
    "first_claimed_role",
    "last_claimed_role",
    "last_claim_confidence",
    "last_claim_stale",
    "claim_count",
    "lied_about_role",
    # Claims at key moments (from N7)
    "claimed_role_at_pressure",
    "target_claim_match_status",
    "claimed_role_at_execution",
    "result_claim_match_status",
    # N6 episode counts
    "pressure_episode_count",
    "targeted_in_n6",
    "execution_episode_count",
    "executed_in_n6",
]


# ── Roster loading ────────────────────────────────────────────────────────────

def _load_roster(out_dir: Path, aliases: dict) -> dict[str, tuple[str, str]]:
    """Return {norm_key: (display_name, actual_role_raw)} for all non-ST players.

    Merges intro_roster.json and roster_overrides.json.
    intro_roster takes precedence for role; overrides fill in extra players.
    Storyteller entries are excluded.
    """
    # norm_key -> (display_name, actual_role_raw)
    roster: dict[str, tuple[str, str]] = {}

    intro = out_dir / "intro_roster.json"
    if intro.exists():
        try:
            data = json.loads(intro.read_text(encoding="utf-8"))
            for player_entry in data.get("players", []):
                raw_name = player_entry.get("name", "").strip()
                role_raw = player_entry.get("actual_role", "").strip()
                if not raw_name:
                    continue
                if raw_name.lower() in _STORYTELLER_NAMES:
                    continue
                display_name = resolve_player_name(raw_name, aliases)
                player_key = normalize_player(display_name)
                roster[player_key] = (display_name, role_raw)
        except Exception:
            pass

    overrides = out_dir / "roster_overrides.json"
    if overrides.exists():
        try:
            ov = json.loads(overrides.read_text(encoding="utf-8"))
            for speaker_id, player_info in ov.items():
                raw_name = player_info.get("name", "").strip()
                role_raw = player_info.get("actual_role", "").strip()
                if not raw_name:
                    continue
                if raw_name.lower() in _STORYTELLER_NAMES:
                    continue
                if raw_name.lower() == speaker_id.lower():
                    continue  # unresolved speaker placeholder
                display_name = resolve_player_name(raw_name, aliases)
                player_key = normalize_player(display_name)
                roster.setdefault(player_key, (display_name, role_raw))
        except Exception:
            pass

    return roster


# ── Helpers ───────────────────────────────────────────────────────────────────

def player_key_from_name(name: str, aliases: dict) -> str:
    """Normalize a player name to a stable, case-insensitive lookup key.

    Combines alias resolution and separator-collapsed lowercasing so that
    'Lewis Brindley', 'lewis brindley', and 'lewisbrindley' all map to the same key.
    """
    return normalize_player(resolve_player_name(name.strip(), aliases))


def _display_claimed(raw: str) -> str:
    """Apply display_role() to a claimed_role string from claims.csv."""
    return display_role(raw) if raw.strip() else ""


# ── Per-video processing ──────────────────────────────────────────────────────

def build_outcome_rows_for_video(
    video_id: str,
    out_dir: Path,
    winner: str,
    duration: float,
    aliases: dict,
) -> list[dict]:
    """Return one outcome row per player for this video. Returns [] on failure."""

    # 1. Player roster — actual role for each player
    roster = _load_roster(out_dir, aliases)
    if not roster:
        return []

    # 2. Death events → {player_key: death_event_row}  (earliest death only per player)
    earliest_death_by_player: dict[str, dict] = {}
    for death_event_row in read_csv(out_dir / "death_events.csv"):
        name = death_event_row.get("player_name", "").strip()
        if not name:
            continue
        player_key = player_key_from_name(name, aliases)
        if player_key not in earliest_death_by_player:
            earliest_death_by_player[player_key] = death_event_row

    # 3. Role claims → {player_key: [claim_rows sorted by timestamp ascending]}
    # Keep only role_claim events with conf >= threshold and a non-blank claimed_role.
    claims_by_player: dict[str, list[dict]] = defaultdict(list)
    for claim_row in read_csv(out_dir / "claims.csv"):
        if claim_row.get("event_type") != "role_claim":
            continue
        if not claim_row.get("claimed_role", "").strip():
            continue
        try:
            claim_conf = float(claim_row.get("confidence", 0))
        except ValueError:
            claim_conf = 0.0
        if claim_conf < _CLAIM_MIN_CONF:
            continue
        name = claim_row.get("player_name", "").strip()
        if not name:
            continue
        player_key = player_key_from_name(name, aliases)
        claim_row["_ts"] = ts_to_seconds(claim_row.get("timestamp_start", "0"))
        claim_row["_conf"] = claim_conf
        claims_by_player[player_key].append(claim_row)

    # Sort each player's claims by timestamp ascending
    for player_key in claims_by_player:
        claims_by_player[player_key].sort(key=lambda claim_row: claim_row["_ts"])

    # 4. N6 episode counts → per-player pressure / execution episode counts
    pressure_episode_count_by_player: dict[str, int] = defaultdict(int)
    execution_episode_count_by_player: dict[str, int] = defaultdict(int)
    for episode_row in read_csv(out_dir / "execution_episodes.csv"):
        target_name = episode_row.get("likely_target_player", "").strip()
        result_name = episode_row.get("execution_result_player", "").strip()
        if target_name:
            pressure_episode_count_by_player[player_key_from_name(target_name, aliases)] += 1
        if result_name:
            execution_episode_count_by_player[player_key_from_name(result_name, aliases)] += 1

    # 5. N7 claim-at-key-moment → per-player best claim context row
    #    claimed_role_at_pressure: when player was likely_target_player.
    #      Pick the N7 row with highest target_claim_confidence; tie: most recent vote.
    #    claimed_role_at_execution: when player was execution_result_player.
    #      Take first non-blank (typically only one per player).
    best_pressure_claim_by_player: dict[str, dict] = {}   # player_key -> best N7 target-side row
    best_execution_claim_by_player: dict[str, dict] = {}  # player_key -> best N7 result-side row

    for claim_context_row in read_csv(out_dir / "execution_claim_context.csv"):
        target_name = claim_context_row.get("likely_target_player", "").strip()
        result_name = claim_context_row.get("execution_result_player", "").strip()
        target_claimed_role = claim_context_row.get("target_claimed_role", "").strip()
        result_claimed_role = claim_context_row.get("result_claimed_role", "").strip()

        if target_name and target_claimed_role:
            player_key = player_key_from_name(target_name, aliases)
            try:
                target_claim_conf = float(claim_context_row.get("target_claim_confidence", 0) or 0)
            except ValueError:
                target_claim_conf = 0.0
            try:
                vote_window_start = float(claim_context_row.get("vote_window_start", 0) or 0)
            except ValueError:
                vote_window_start = 0.0
            prev_best = best_pressure_claim_by_player.get(player_key)
            if prev_best is None:
                best_pressure_claim_by_player[player_key] = claim_context_row
            else:
                try:
                    prev_conf = float(prev_best.get("target_claim_confidence", 0) or 0)
                except ValueError:
                    prev_conf = 0.0
                try:
                    prev_vote_start = float(prev_best.get("vote_window_start", 0) or 0)
                except ValueError:
                    prev_vote_start = 0.0
                # Prefer higher confidence; tie: most recent vote window
                if target_claim_conf > prev_conf or (target_claim_conf == prev_conf and vote_window_start > prev_vote_start):
                    best_pressure_claim_by_player[player_key] = claim_context_row

        if result_name and result_claimed_role:
            player_key = player_key_from_name(result_name, aliases)
            if player_key not in best_execution_claim_by_player:
                best_execution_claim_by_player[player_key] = claim_context_row

    # 6. Assemble one outcome row per player
    outcome_rows: list[dict] = []

    for player_key, (display_name, actual_role_raw) in roster.items():
        normalized_actual_role = normalize_role(actual_role_raw)
        display_actual_role = display_role(actual_role_raw) if actual_role_raw else ""

        # Alignment — via botc_ui._team(); never hardcoded
        alignment = ""
        if normalized_actual_role:
            team = _team(normalized_actual_role)
            if team:
                alignment = team   # "Good" or "Evil"

        # Outcome fields
        death_event = earliest_death_by_player.get(player_key)
        died = "true" if death_event else "false"
        survived = "false" if death_event else "true"
        survived_to_end = survived
        death_type = death_event["event_type"] if death_event else ""
        was_executed = (
            "true" if (death_event and death_event["event_type"] == "execution") else "false"
        )
        death_timestamp = death_event["timestamp_start"] if death_event else ""
        execution_timestamp = death_timestamp if was_executed == "true" else ""

        # General claim fields (from N3)
        player_claim_rows = claims_by_player.get(player_key, [])
        claim_count = len(player_claim_rows)
        first_claimed_role_raw = player_claim_rows[0]["claimed_role"].strip() if player_claim_rows else ""
        # highest confidence claim, tie-break on most recent
        highest_conf_claim = (
            max(player_claim_rows, key=lambda claim_row: (claim_row["_conf"], claim_row["_ts"]))
            if player_claim_rows
            else None
        )
        last_claimed_role_raw = highest_conf_claim["claimed_role"].strip() if highest_conf_claim else ""
        last_claim_conf = round(highest_conf_claim["_conf"], 3) if highest_conf_claim else ""
        lied_about_role = (
            "true"
            if any(claim_row.get("verified_lie", "").lower() == "true" for claim_row in player_claim_rows)
            else "false"
        )

        # Staleness: last claim relative to death or game end
        if highest_conf_claim:
            staleness_reference_t = (
                float(death_timestamp) if death_timestamp else (duration if duration > 0 else 9999.0)
            )
            claim_recency_s = staleness_reference_t - highest_conf_claim["_ts"]
            last_claim_stale = "true" if claim_recency_s > _CLAIM_STALE_S else "false"
        else:
            last_claim_stale = ""

        # N6 pressure and execution episode counts
        n_pressure_episodes = pressure_episode_count_by_player.get(player_key, 0)
        n_execution_episodes = execution_episode_count_by_player.get(player_key, 0)

        # N7 claims at key moments
        best_pressure_claim_row = best_pressure_claim_by_player.get(player_key)
        best_execution_claim_row = best_execution_claim_by_player.get(player_key)

        claimed_at_pressure_raw = (
            best_pressure_claim_row.get("target_claimed_role", "").strip()
            if best_pressure_claim_row else ""
        )
        pressure_claim_match = (
            best_pressure_claim_row.get("target_claim_match_status", "").strip()
            if best_pressure_claim_row else ""
        )
        claimed_at_execution_raw = (
            best_execution_claim_row.get("result_claimed_role", "").strip()
            if best_execution_claim_row else ""
        )
        execution_claim_match = (
            best_execution_claim_row.get("result_claim_match_status", "").strip()
            if best_execution_claim_row else ""
        )

        outcome_rows.append(
            {
                "video_id": video_id,
                "player_name": display_name,
                "actual_role": display_actual_role,
                "alignment": alignment,
                "winner": winner or "",
                "survived": survived,
                "died": died,
                "was_executed": was_executed,
                "death_type": death_type,
                "death_timestamp": death_timestamp,
                "execution_timestamp": execution_timestamp,
                "survived_to_end": survived_to_end,
                "first_claimed_role": _display_claimed(first_claimed_role_raw),
                "last_claimed_role": _display_claimed(last_claimed_role_raw),
                "last_claim_confidence": last_claim_conf,
                "last_claim_stale": last_claim_stale,
                "claim_count": claim_count,
                "lied_about_role": lied_about_role,
                "claimed_role_at_pressure": _display_claimed(claimed_at_pressure_raw),
                "target_claim_match_status": pressure_claim_match,
                "claimed_role_at_execution": _display_claimed(claimed_at_execution_raw),
                "result_claim_match_status": execution_claim_match,
                "pressure_episode_count": n_pressure_episodes,
                "targeted_in_n6": "true" if n_pressure_episodes > 0 else "false",
                "execution_episode_count": n_execution_episodes,
                "executed_in_n6": "true" if n_execution_episodes > 0 else "false",
            }
        )

    return outcome_rows


# ── I/O ───────────────────────────────────────────────────────────────────────

def write_outcome_rows(outcome_rows: list[dict], out_path: Path) -> None:
    """Write the per-player outcome rows to a CSV file."""
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(outcome_rows)


def load_playlist_index() -> dict[str, dict]:
    """Return {video_id: playlist_entry} for all entries in playlist.json."""
    p = Path("playlist.json")
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {e["id"]: e for e in data.get("entries", [])}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Build claimed_role_outcomes.csv per video (N8)"
    )
    ap.add_argument("video_id", nargs="?", help="Single video ID to process")
    ap.add_argument(
        "--all", action="store_true", help="Process all eligible videos"
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing claimed_role_outcomes.csv",
    )
    return ap.parse_args()


def _eligible_videos(playlist: dict[str, dict], all_mode: bool, single_id: str | None):
    """Yield (video_id, entry) for eligible videos."""
    if single_id:
        if single_id in playlist:
            yield single_id, playlist[single_id]
        else:
            print(f"WARNING: {single_id!r} not found in playlist.json", file=sys.stderr)
        return
    if all_mode:
        for vid, entry in playlist.items():
            if entry.get("members", False):
                continue
            out_dir = Path("outputs") / vid
            if not (out_dir / "execution_episodes.csv").exists():
                continue
            yield vid, entry


def main() -> None:
    args = _parse_args()
    if not args.video_id and not args.all:
        print("Usage: python build_claimed_role_outcomes.py <video_id> [--force]")
        print("       python build_claimed_role_outcomes.py --all [--force]")
        sys.exit(1)

    playlist = load_playlist_index()
    aliases = load_player_aliases()

    to_process = list(_eligible_videos(playlist, args.all, args.video_id))
    print(f"Processing {len(to_process)} videos ...")

    total_player_rows = 0
    skipped = 0
    errors = 0

    for video_id, playlist_entry in to_process:
        out_dir = Path("outputs") / video_id
        out_path = out_dir / _OUT_FILE

        if out_path.exists() and not args.force:
            print(f"  {video_id}: exists, skipping (use --force to overwrite)")
            skipped += 1
            continue

        winner = playlist_entry.get("winner") or ""
        duration = float(playlist_entry.get("duration") or 0)

        try:
            outcome_rows = build_outcome_rows_for_video(video_id, out_dir, winner, duration, aliases)
        except Exception as exc:
            print(f"  {video_id}: ERROR — {exc}", file=sys.stderr)
            errors += 1
            continue

        if not outcome_rows:
            print(f"  {video_id}: no roster, skipping")
            skipped += 1
            continue

        write_outcome_rows(outcome_rows, out_path)
        total_player_rows += len(outcome_rows)

        # Summary stats
        survived_count = sum(1 for r in outcome_rows if r["survived"] == "true")
        pressured_count = sum(1 for r in outcome_rows if r["targeted_in_n6"] == "true")
        executed_count = sum(1 for r in outcome_rows if r["executed_in_n6"] == "true")
        claimed_count = sum(1 for r in outcome_rows if r["last_claimed_role"])
        liar_count = sum(1 for r in outcome_rows if r["lied_about_role"] == "true")

        print(
            f"  {video_id}: {len(outcome_rows)} players  "
            f"survived={survived_count}  pressured={pressured_count}  "
            f"executed={executed_count}  with_last_claim={claimed_count}  "
            f"liars={liar_count}"
        )

        # Show interesting examples
        for outcome_row in outcome_rows:
            if outcome_row["claimed_role_at_execution"]:
                print(
                    f"    claimed_at_exec:    {outcome_row['player_name']} "
                    f"claimed={outcome_row['claimed_role_at_execution']} "
                    f"actual={outcome_row['actual_role']} "
                    f"match={outcome_row['result_claim_match_status']}"
                )
            if outcome_row["claimed_role_at_pressure"] and outcome_row["targeted_in_n6"] == "true":
                print(
                    f"    claimed_at_pressure: {outcome_row['player_name']} "
                    f"claimed={outcome_row['claimed_role_at_pressure']} "
                    f"actual={outcome_row['actual_role']} "
                    f"survived={outcome_row['survived']} "
                    f"match={outcome_row['target_claim_match_status']}"
                )

    print(
        f"\nDone: {len(to_process)} videos processed, "
        f"{total_player_rows} player rows written, "
        f"{skipped} skipped, {errors} errors."
    )


if __name__ == "__main__":
    main()
