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
    resolve_player_name,
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
            for p in data.get("players", []):
                raw_name = p.get("name", "").strip()
                role_raw = p.get("actual_role", "").strip()
                if not raw_name:
                    continue
                if raw_name.lower() in _STORYTELLER_NAMES:
                    continue
                display = resolve_player_name(raw_name, aliases)
                norm = normalize_player(display)
                roster[norm] = (display, role_raw)
        except Exception:
            pass

    overrides = out_dir / "roster_overrides.json"
    if overrides.exists():
        try:
            ov = json.loads(overrides.read_text(encoding="utf-8"))
            for spk, info in ov.items():
                raw_name = info.get("name", "").strip()
                role_raw = info.get("actual_role", "").strip()
                if not raw_name:
                    continue
                if raw_name.lower() in _STORYTELLER_NAMES:
                    continue
                if raw_name.lower() == spk.lower():
                    continue  # unresolved speaker placeholder
                display = resolve_player_name(raw_name, aliases)
                norm = normalize_player(display)
                roster.setdefault(norm, (display, role_raw))
        except Exception:
            pass

    return roster


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _norm_name(name: str, aliases: dict) -> str:
    """Normalize a player name to a stable lookup key."""
    return normalize_player(resolve_player_name(name.strip(), aliases))


def _ts_to_seconds(ts: str) -> float:
    """Parse 'MM:SS.ss' or float-string timestamp to float seconds."""
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


def _display_claimed(raw: str) -> str:
    """Apply display_role() to a claimed_role string from claims.csv."""
    return display_role(raw) if raw.strip() else ""


# ── Per-video processing ──────────────────────────────────────────────────────

def _process_video(
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

    # 2. Death events → {norm_name: row}
    death_by_player: dict[str, dict] = {}
    for row in _read_csv(out_dir / "death_events.csv"):
        name = row.get("player_name", "").strip()
        if not name:
            continue
        norm = _norm_name(name, aliases)
        if norm not in death_by_player:           # keep earliest death only
            death_by_player[norm] = row

    # 3. Role claims → {norm_name: [sorted rows]}
    # Keep only role_claim events with conf >= threshold and a non-blank claimed_role.
    claims_by_player: dict[str, list[dict]] = defaultdict(list)
    for row in _read_csv(out_dir / "claims.csv"):
        if row.get("event_type") != "role_claim":
            continue
        if not row.get("claimed_role", "").strip():
            continue
        try:
            conf = float(row.get("confidence", 0))
        except ValueError:
            conf = 0.0
        if conf < _CLAIM_MIN_CONF:
            continue
        name = row.get("player_name", "").strip()
        if not name:
            continue
        norm = _norm_name(name, aliases)
        row["_ts"] = _ts_to_seconds(row.get("timestamp_start", "0"))
        row["_conf"] = conf
        claims_by_player[norm].append(row)

    # Sort each player's claims by timestamp ascending
    for norm in claims_by_player:
        claims_by_player[norm].sort(key=lambda r: r["_ts"])

    # 4. N6 episode counts → per-player pressure / execution count
    pressure_count: dict[str, int] = defaultdict(int)
    execution_count: dict[str, int] = defaultdict(int)
    for row in _read_csv(out_dir / "execution_episodes.csv"):
        target = row.get("likely_target_player", "").strip()
        result = row.get("execution_result_player", "").strip()
        if target:
            pressure_count[_norm_name(target, aliases)] += 1
        if result:
            execution_count[_norm_name(result, aliases)] += 1

    # 5. N7 claim-at-key-moment → per-player best claim row
    #    claimed_role_at_pressure: when player was likely_target_player.
    #      Pick the N7 row with highest target_claim_confidence; tie: most recent vote.
    #    claimed_role_at_execution: when player was execution_result_player.
    #      Take first non-blank (typically only one per player).
    pressure_claim: dict[str, dict] = {}   # norm -> best N7 row (target side)
    execution_claim: dict[str, dict] = {}  # norm -> best N7 row (result side)

    for row in _read_csv(out_dir / "execution_claim_context.csv"):
        target = row.get("likely_target_player", "").strip()
        result = row.get("execution_result_player", "").strip()
        tclaimed = row.get("target_claimed_role", "").strip()
        rclaimed = row.get("result_claimed_role", "").strip()

        if target and tclaimed:
            norm = _norm_name(target, aliases)
            try:
                tconf = float(row.get("target_claim_confidence", 0) or 0)
            except ValueError:
                tconf = 0.0
            try:
                vws = float(row.get("vote_window_start", 0) or 0)
            except ValueError:
                vws = 0.0
            prev = pressure_claim.get(norm)
            if prev is None:
                pressure_claim[norm] = row
            else:
                try:
                    prev_conf = float(prev.get("target_claim_confidence", 0) or 0)
                except ValueError:
                    prev_conf = 0.0
                try:
                    prev_vws = float(prev.get("vote_window_start", 0) or 0)
                except ValueError:
                    prev_vws = 0.0
                # Prefer higher confidence; tie: most recent vote window
                if tconf > prev_conf or (tconf == prev_conf and vws > prev_vws):
                    pressure_claim[norm] = row

        if result and rclaimed:
            norm = _norm_name(result, aliases)
            if norm not in execution_claim:
                execution_claim[norm] = row

    # 6. Assemble one row per player
    rows: list[dict] = []

    for norm_key, (display_name, actual_role_raw) in roster.items():
        actual_role_norm = normalize_role(actual_role_raw)
        actual_role_disp = display_role(actual_role_raw) if actual_role_raw else ""

        # Alignment — via botc_ui._team(); never hardcoded
        alignment = ""
        if actual_role_norm:
            team = _team(actual_role_norm)
            if team:
                alignment = team   # "Good" or "Evil"

        # Outcome
        death_row = death_by_player.get(norm_key)
        died = "true" if death_row else "false"
        survived = "false" if death_row else "true"
        survived_to_end = survived
        death_type = death_row["event_type"] if death_row else ""
        was_executed = (
            "true" if (death_row and death_row["event_type"] == "execution") else "false"
        )
        death_ts = death_row["timestamp_start"] if death_row else ""
        exec_ts = death_ts if was_executed == "true" else ""

        # General claims
        player_claims = claims_by_player.get(norm_key, [])
        claim_count = len(player_claims)
        first_claimed_raw = player_claims[0]["claimed_role"].strip() if player_claims else ""
        # last = highest confidence, tie-break on most recent
        best_last = (
            max(player_claims, key=lambda r: (r["_conf"], r["_ts"]))
            if player_claims
            else None
        )
        last_claimed_raw = best_last["claimed_role"].strip() if best_last else ""
        last_claim_conf = round(best_last["_conf"], 3) if best_last else ""
        lied = (
            "true"
            if any(r.get("verified_lie", "").lower() == "true" for r in player_claims)
            else "false"
        )

        # Staleness: last claim relative to death or game end
        if best_last:
            reference_t = (
                float(death_ts) if death_ts else (duration if duration > 0 else 9999.0)
            )
            recency = reference_t - best_last["_ts"]
            last_claim_stale = "true" if recency > _CLAIM_STALE_S else "false"
        else:
            last_claim_stale = ""

        # N6 counts
        pcount = pressure_count.get(norm_key, 0)
        ecount = execution_count.get(norm_key, 0)

        # N7 claims at key moments
        pclaim_row = pressure_claim.get(norm_key)
        eclaim_row = execution_claim.get(norm_key)

        claimed_at_pressure_raw = (
            pclaim_row.get("target_claimed_role", "").strip() if pclaim_row else ""
        )
        tgt_match = (
            pclaim_row.get("target_claim_match_status", "").strip() if pclaim_row else ""
        )
        claimed_at_execution_raw = (
            eclaim_row.get("result_claimed_role", "").strip() if eclaim_row else ""
        )
        res_match = (
            eclaim_row.get("result_claim_match_status", "").strip() if eclaim_row else ""
        )

        rows.append(
            {
                "video_id": video_id,
                "player_name": display_name,
                "actual_role": actual_role_disp,
                "alignment": alignment,
                "winner": winner or "",
                "survived": survived,
                "died": died,
                "was_executed": was_executed,
                "death_type": death_type,
                "death_timestamp": death_ts,
                "execution_timestamp": exec_ts,
                "survived_to_end": survived_to_end,
                "first_claimed_role": _display_claimed(first_claimed_raw),
                "last_claimed_role": _display_claimed(last_claimed_raw),
                "last_claim_confidence": last_claim_conf,
                "last_claim_stale": last_claim_stale,
                "claim_count": claim_count,
                "lied_about_role": lied,
                "claimed_role_at_pressure": _display_claimed(claimed_at_pressure_raw),
                "target_claim_match_status": tgt_match,
                "claimed_role_at_execution": _display_claimed(claimed_at_execution_raw),
                "result_claim_match_status": res_match,
                "pressure_episode_count": pcount,
                "targeted_in_n6": "true" if pcount > 0 else "false",
                "execution_episode_count": ecount,
                "executed_in_n6": "true" if ecount > 0 else "false",
            }
        )

    return rows


# ── I/O ───────────────────────────────────────────────────────────────────────

def _write_csv(rows: list[dict], out_path: Path) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _load_playlist() -> dict[str, dict]:
    """Return {video_id: {winner, duration, members, ...}}."""
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

    playlist = _load_playlist()
    aliases = load_player_aliases()

    to_process = list(_eligible_videos(playlist, args.all, args.video_id))
    print(f"Processing {len(to_process)} videos ...")

    total_rows = 0
    skipped = 0
    errors = 0

    for video_id, entry in to_process:
        out_dir = Path("outputs") / video_id
        out_path = out_dir / _OUT_FILE

        if out_path.exists() and not args.force:
            print(f"  {video_id}: exists, skipping (use --force to overwrite)")
            skipped += 1
            continue

        winner = entry.get("winner") or ""
        duration = float(entry.get("duration") or 0)

        try:
            rows = _process_video(video_id, out_dir, winner, duration, aliases)
        except Exception as exc:
            print(f"  {video_id}: ERROR — {exc}", file=sys.stderr)
            errors += 1
            continue

        if not rows:
            print(f"  {video_id}: no roster, skipping")
            skipped += 1
            continue

        _write_csv(rows, out_path)
        total_rows += len(rows)

        # Summary stats
        survived_cnt = sum(1 for r in rows if r["survived"] == "true")
        pressured_cnt = sum(1 for r in rows if r["targeted_in_n6"] == "true")
        executed_cnt = sum(1 for r in rows if r["executed_in_n6"] == "true")
        claimed_cnt = sum(1 for r in rows if r["last_claimed_role"])
        liars = sum(1 for r in rows if r["lied_about_role"] == "true")

        print(
            f"  {video_id}: {len(rows)} players  "
            f"survived={survived_cnt}  pressured={pressured_cnt}  "
            f"executed={executed_cnt}  with_last_claim={claimed_cnt}  "
            f"liars={liars}"
        )

        # Show interesting examples
        for r in rows:
            if r["claimed_role_at_execution"]:
                print(
                    f"    claimed_at_exec:    {r['player_name']} "
                    f"claimed={r['claimed_role_at_execution']} "
                    f"actual={r['actual_role']} "
                    f"match={r['result_claim_match_status']}"
                )
            if r["claimed_role_at_pressure"] and r["targeted_in_n6"] == "true":
                print(
                    f"    claimed_at_pressure: {r['player_name']} "
                    f"claimed={r['claimed_role_at_pressure']} "
                    f"actual={r['actual_role']} "
                    f"survived={r['survived']} "
                    f"match={r['target_claim_match_status']}"
                )

    print(
        f"\nDone: {len(to_process)} videos processed, "
        f"{total_rows} player rows written, "
        f"{skipped} skipped, {errors} errors."
    )


if __name__ == "__main__":
    main()
