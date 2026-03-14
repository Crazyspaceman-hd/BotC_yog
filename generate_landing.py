"""generate_landing.py — Build a standalone HTML landing page from botc.db.

Reads botc.db, computes the same stats shown on the explore_public.py Home tab,
and writes a self-contained landing.html with all data embedded as JSON.

Usage:
    python generate_landing.py              # writes landing.html next to this script
    python generate_landing.py --open       # write + open in default browser
    python generate_landing.py --db PATH    # custom DB path
    python generate_landing.py --out PATH   # custom output path
    python generate_landing.py --explorer-url http://host:8501
"""

import argparse
import json
import sqlite3
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# ── reuse shared helpers ───────────────────────────────────────────────────────
from botc_ui import _team, _role_type

DB_PATH  = Path("botc.db")
OUT_PATH = Path("landing.html")
EXPLORER_URL_DEFAULT = "http://localhost:8501"


# ── data helpers ──────────────────────────────────────────────────────────────

def _load(db: Path) -> pd.DataFrame:
    """Load all lies joined with video titles."""
    con = sqlite3.connect(str(db))
    df = pd.read_sql_query(
        "SELECT l.video_id, v.title AS video_title, v.winner, "
        "l.player_name, l.actual_role, l.claimed_role, l.verdict "
        "FROM lies l JOIN videos v ON l.video_id = v.id",
        con,
    )
    con.close()
    # Normalise role strings to Title Case (handles legacy lowercase DB data)
    for col in ("actual_role", "claimed_role"):
        df[col] = df[col].fillna("").str.title()
    return df


def _load_videos(db: Path) -> pd.DataFrame:
    """Load video list for the Recent Games section."""
    con = sqlite3.connect(str(db))
    df = pd.read_sql_query(
        "SELECT v.id, v.title, v.winner, "
        "COUNT(l.rowid) AS total_claims, "
        "SUM(CASE WHEN l.verdict='LIE' THEN 1 ELSE 0 END) AS lie_count "
        "FROM videos v "
        "LEFT JOIN lies l ON l.video_id = v.id "
        "WHERE v.id IN (SELECT DISTINCT video_id FROM lies) "
        "GROUP BY v.id, v.title, v.winner "
        "ORDER BY v.rowid DESC "
        "LIMIT 12",
        con,
    )
    con.close()
    return df


def _load_wins(db: Path) -> dict:
    """Return {good: N, evil: N} total win counts from videos table."""
    con = sqlite3.connect(str(db))
    df  = pd.read_sql_query(
        "SELECT winner, COUNT(*) AS n FROM videos GROUP BY winner", con
    )
    con.close()
    good = int(df.loc[df["winner"] == "Good",  "n"].sum()) if not df.empty else 0
    evil = int(df.loc[df["winner"] == "Evil",  "n"].sum()) if not df.empty else 0
    return {"good": good, "evil": evil}


def _load_words_per_game(db: Path) -> dict[str, float]:
    """Return {player_name: avg_words_per_game} from segments + speaker_map."""
    con = sqlite3.connect(str(db))
    try:
        wpg_df = pd.read_sql_query(
            """
            SELECT sm.name, s.video_id, s.text
              FROM segments s
              JOIN speaker_map sm
                ON s.video_id = sm.video_id AND s.speaker = sm.speaker
             WHERE sm.name IS NOT NULL
               AND sm.name != 'Storyteller'
            """,
            con,
        )
    finally:
        con.close()
    if wpg_df.empty:
        return {}
    wpg_df["words"] = wpg_df["text"].fillna("").apply(lambda t: len(t.split()))
    per_game = wpg_df.groupby(["name", "video_id"])["words"].sum().reset_index()
    avg = per_game.groupby("name")["words"].mean().round(1)
    return avg.to_dict()


def _load_words_per_team(db: Path) -> dict[str, dict]:
    """Return {player_name: {good: avg_wpg, evil: avg_wpg}} split by team."""
    con = sqlite3.connect(str(db))
    try:
        segs = pd.read_sql_query(
            """SELECT sm.name, s.video_id, s.text
                 FROM segments s
                 JOIN speaker_map sm
                   ON s.video_id = sm.video_id AND s.speaker = sm.speaker
                WHERE sm.name IS NOT NULL AND sm.name != 'Storyteller'""",
            con,
        )
        teams_raw = pd.read_sql_query(
            """SELECT DISTINCT player_name, video_id, actual_role
                 FROM lies
                WHERE player_name IS NOT NULL
                  AND player_name != 'Storyteller'
                  AND player_name NOT LIKE 'speaker_%'""",
            con,
        )
    finally:
        con.close()
    if segs.empty or teams_raw.empty:
        return {}
    teams_raw["team"] = teams_raw["actual_role"].map(_team)
    teams_raw = teams_raw[teams_raw["team"].isin(["Good", "Evil"])].drop_duplicates(
        subset=["player_name", "video_id"]
    )
    segs["words"] = segs["text"].fillna("").apply(lambda t: len(t.split()))
    per_game = segs.groupby(["name", "video_id"])["words"].sum().reset_index()
    merged = per_game.merge(
        teams_raw[["player_name", "video_id", "team"]].rename(columns={"player_name": "name"}),
        on=["name", "video_id"],
        how="inner",
    )
    result: dict[str, dict] = {}
    for (name, team), grp in merged.groupby(["name", "team"]):
        result.setdefault(name, {})[team.lower()] = round(float(grp["words"].mean()), 1)
    return result


def _load_game_records(db: Path) -> dict:
    """Return record-breaking game stats: most lies, highest lie-rate, most claims."""
    con = sqlite3.connect(str(db))
    try:
        df = pd.read_sql_query(
            """SELECT v.id, v.title, v.winner,
                      COUNT(l.rowid) AS total_claims,
                      SUM(CASE WHEN l.verdict='LIE' THEN 1 ELSE 0 END) AS lie_count
                 FROM videos v
                 JOIN lies l ON l.video_id = v.id
                GROUP BY v.id, v.title, v.winner""",
            con,
        )
    finally:
        con.close()
    empty = {"most_lies": None, "most_deceptive": None, "most_claims": None}
    if df.empty:
        return empty
    df["lie_rate"] = (df["lie_count"] / df["total_claims"]).fillna(0)

    def _row(r):
        return {
            "id":       str(r["id"]),
            "title":    str(r["title"]),
            "winner":   str(r["winner"] or ""),
            "claims":   int(r["total_claims"]),
            "lies":     int(r["lie_count"] or 0),
            "lie_rate": round(float(r["lie_rate"]), 3),
        }

    qual = df[df["total_claims"] >= 10]
    return {
        "most_lies":      _row(df.sort_values("lie_count",    ascending=False).iloc[0]),
        "most_deceptive": _row(qual.sort_values("lie_rate",   ascending=False).iloc[0]) if not qual.empty else None,
        "most_claims":    _row(df.sort_values("total_claims", ascending=False).iloc[0]),
    }


def _compute_stats(df: pd.DataFrame, exclude_goblin: bool = False) -> dict:
    """Compute all stats needed by the landing page."""
    if df.empty:
        return _empty_stats()

    if exclude_goblin:
        gob = (
            (df["actual_role"].str.lower() == "goblin") |
            (df["claimed_role"].str.lower() == "goblin")
        )
        df = df[~gob].copy()
        if df.empty:
            return _empty_stats()

    df["team"] = df["actual_role"].map(_team)

    # ── summary ───────────────────────────────────────────────────────────────
    n_games   = int(df["video_id"].nunique())
    n_players = int(
        df[~df["player_name"].str.match(r"^speaker_\d+$", na=False)
           & df["player_name"].notna()]["player_name"].nunique()
    )
    n_claims  = len(df)
    n_lies    = int((df["verdict"] == "LIE").sum())
    n_true    = int((df["verdict"] == "TRUE").sum())
    n_hm      = int((df["verdict"] == "HONEST MISTAKE").sum())
    n_unver   = int((df["verdict"] == "UNVERIFIED").sum())

    # ── per-player stats (mirror explore_public home tab logic) ───────────────
    named = df[
        df["player_name"].notna() &
        ~df["player_name"].str.match(r"^speaker_\d+$", na=False) &
        (df["player_name"] != "Storyteller")
    ].copy()

    game_teams = named.drop_duplicates(subset=["player_name", "video_id"])[
        ["player_name", "video_id", "team"]
    ]
    pa_games = (
        game_teams.groupby("player_name")
        .agg(
            games  =("video_id", "nunique"),
            evil_g =("team",  lambda x: (x == "Evil").sum()),
            good_g =("team",  lambda x: (x == "Good").sum()),
        )
        .reset_index()
    )
    pa_claims = (
        named.groupby("player_name")
        .agg(
            claims =("verdict", "count"),
            lies   =("verdict", lambda x: (x == "LIE").sum()),
            true_  =("verdict", lambda x: (x == "TRUE").sum()),
        )
        .reset_index()
    )
    pa = pa_games.merge(pa_claims, on="player_name", how="left").fillna(0)
    pa["lie_rate"]  = pa["lies"]   / pa["claims"].replace(0, float("nan"))
    pa["evil_rate"] = pa["evil_g"] / pa["games"].replace(0, float("nan"))
    pa["good_rate"] = pa["good_g"] / pa["games"].replace(0, float("nan"))

    # ── superlatives ──────────────────────────────────────────────────────────
    # Only players with more than 5 games
    qual  = pa[pa["games"] > 5]
    multi = pa[pa["games"] > 5]

    def _top(frame, col, asc=False):
        s = frame.dropna(subset=[col]).sort_values(col, ascending=asc)
        return s.iloc[0].to_dict() if not s.empty else None

    most_evil    = _top(multi, "evil_rate")
    most_good    = _top(multi, "good_rate")
    biggest_liar = _top(qual,  "lie_rate")
    most_honest  = _top(qual,  "lie_rate", asc=True)
    most_games   = _top(pa,    "games")

    # Good player who lied most (only among players with >5 games)
    eligible_names = set(multi["player_name"])
    good_lies = (
        named[
            (named["team"] == "Good") &
            (named["verdict"] == "LIE") &
            (named["player_name"].isin(eligible_names))
        ]
        .groupby("player_name").size()
        .reset_index(name="good_lies")
        .sort_values("good_lies", ascending=False)
    )
    traitor = good_lies.iloc[0].to_dict() if not good_lies.empty else None

    # ── top liars chart data (top 10 by lie count) ────────────────────────────
    top_liars_df = (
        qual.sort_values("lies", ascending=False).head(10)
    )
    top_liars = [
        {
            "name":     row["player_name"],
            "lies":     int(row["lies"]),
            "lie_rate": round(float(row["lie_rate"]), 3),
        }
        for _, row in top_liars_df.iterrows()
    ]

    # ── roles most often faked (per-game count, normalises multi-claim games) ──
    fake_src = df[
        df["claimed_role"].notna() &
        (df["claimed_role"].str.lower() != "unknown")
    ]
    # Count distinct games (video_id) in which the role was lied about / claimed honestly
    fake_lies = (
        fake_src[fake_src["verdict"] == "LIE"]
        .groupby("claimed_role")["video_id"]
        .nunique()
        .reset_index(name="games_faked")
    )
    fake_honest = (
        fake_src[fake_src["verdict"] == "TRUE"]
        .groupby("claimed_role")["video_id"]
        .nunique()
        .reset_index(name="games_honest")
    )
    fake_df = fake_lies.merge(fake_honest, on="claimed_role", how="outer").fillna(0)
    fake_df["games_faked"]  = fake_df["games_faked"].astype(int)
    fake_df["games_honest"] = fake_df["games_honest"].astype(int)
    fake_df["team"] = fake_df["claimed_role"].map(_team)
    fake_df = fake_df[fake_df["games_faked"] >= 2].sort_values("games_faked", ascending=False)
    most_faked_role = (
        {"role":        str(fake_df.iloc[0]["claimed_role"]),
         "team":        str(fake_df.iloc[0]["team"]),
         "games_faked": int(fake_df.iloc[0]["games_faked"])}
        if not fake_df.empty else None
    )
    fake_roles = fake_df.apply(lambda r: {
        "role":         r["claimed_role"],
        "team":         r["team"],
        "games_faked":  int(r["games_faked"]),
        "games_honest": int(r["games_honest"]),
    }, axis=1).tolist()

    # ── role win rates (best performing role per team) ────────────────────────
    rw = df[
        df["actual_role"].notna() &
        (df["actual_role"].str.lower() != "unknown") &
        df["winner"].notna()
    ].drop_duplicates(subset=["actual_role", "video_id"])[
        ["actual_role", "video_id", "winner", "team"]
    ].copy()
    rw["won"] = rw["winner"] == rw["team"]
    rw_grp = (
        rw.groupby("actual_role")
        .agg(
            total_games=("video_id", "nunique"),
            wins       =("won",      "sum"),
            team       =("team",     lambda x: x.mode().iloc[0] if not x.empty else "Good"),
        )
        .reset_index()
    )
    rw_grp = rw_grp[rw_grp["total_games"] >= 3].copy()
    rw_grp["win_rate"] = rw_grp["wins"] / rw_grp["total_games"]

    def _best_role(team_val):
        sub = rw_grp[rw_grp["team"] == team_val].dropna(subset=["win_rate"])
        if sub.empty:
            return None
        row = sub.sort_values("win_rate", ascending=False).iloc[0]
        return {
            "role":        str(row["actual_role"]),
            "wins":        int(row["wins"]),
            "total_games": int(row["total_games"]),
            "win_rate":    round(float(row["win_rate"]), 3),
        }

    best_evil_role = _best_role("Evil")
    best_good_role = _best_role("Good")

    # ── role breakdown (per-game counts, min 2 games) ────────────────────────
    _rb = df[df["actual_role"].notna() & ~df["actual_role"].str.lower().isin(["", "unknown"])].copy()

    # distinct games where this role appeared as actual_role
    _role_games = _rb.groupby("actual_role")["video_id"].nunique().rename("games")

    # distinct games where the holder of this role told a lie
    _role_faked = (
        _rb[_rb["verdict"] == "LIE"]
        .groupby("actual_role")["video_id"].nunique().rename("games_faked_in")
    )

    # most common team per role
    _role_team = (
        _rb.groupby("actual_role")["team"]
        .agg(lambda x: x.mode().iloc[0] if not x.empty else "Good")
        .rename("team")
    )

    # games where someone NOT holding this role claimed it (bluffs / cover stories)
    _other = df[
        df["claimed_role"].notna() &
        ~df["claimed_role"].str.lower().isin(["", "unknown"]) &
        (df["claimed_role"] != df["actual_role"])
    ]
    _role_claimed_other = (
        _other.groupby("claimed_role")["video_id"].nunique()
        .rename("games_claimed_by_other")
        .rename_axis("actual_role")
    )

    # games where role was present but nobody (holder or anyone else) ever claimed it
    _games_with_role    = _rb.groupby("actual_role")["video_id"].apply(set)
    _claimed_all        = df[df["claimed_role"].notna() & (df["claimed_role"] != "")]
    _games_role_claimed = _claimed_all.groupby("claimed_role")["video_id"].apply(set).rename_axis("actual_role")

    role_df = pd.concat([_role_games, _role_faked, _role_team], axis=1).reset_index()
    role_df = role_df.merge(
        _role_claimed_other.reset_index(), on="actual_role", how="left"
    )
    role_df["games_not_claimed"] = role_df["actual_role"].map(
        lambda r: len(_games_with_role.get(r, set()) - _games_role_claimed.get(r, set()))
    )
    role_df[["games_faked_in", "games_claimed_by_other"]] = (
        role_df[["games_faked_in", "games_claimed_by_other"]].fillna(0).astype(int)
    )

    # ── Cover split: Evil claimants vs Good claimants ─────────────────────────
    # Non-holders who claimed role X, tagged by their own actual team.
    _other_team = df[
        df["claimed_role"].notna() &
        ~df["claimed_role"].str.lower().isin(["", "unknown"]) &
        (df["claimed_role"] != df["actual_role"])
    ].copy()
    _other_team["claimant_team"] = _other_team["actual_role"].map(_team)
    _role_cover_evil = (
        _other_team[_other_team["claimant_team"] == "Evil"]
        .groupby("claimed_role")["video_id"].nunique()
        .rename("games_cover_evil").rename_axis("actual_role")
    )
    _role_cover_good = (
        _other_team[_other_team["claimant_team"] == "Good"]
        .groupby("claimed_role")["video_id"].nunique()
        .rename("games_cover_good").rename_axis("actual_role")
    )
    role_df = role_df.merge(_role_cover_evil.reset_index(), on="actual_role", how="left")
    role_df = role_df.merge(_role_cover_good.reset_index(), on="actual_role", how="left")
    role_df[["games_cover_evil", "games_cover_good"]] = (
        role_df[["games_cover_evil", "games_cover_good"]].fillna(0).astype(int)
    )

    # ── Phantom claims: role claimed in a game where it wasn't actually in play ─
    _appeared_per_game  = df.groupby("actual_role")["video_id"].apply(set)
    _all_claimed_per_game = (
        df[df["claimed_role"].notna() & (df["claimed_role"] != "")]
        .groupby("claimed_role")["video_id"].apply(set)
        .rename_axis("actual_role")
    )
    role_df["games_phantom"] = role_df["actual_role"].map(
        lambda r: len(_all_claimed_per_game.get(r, set()) - _appeared_per_game.get(r, set()))
    ).fillna(0).astype(int)

    role_df = role_df[role_df["games"] >= 2].copy()
    role_df["lie_rate"] = (
        role_df["games_faked_in"] / role_df["games"].replace(0, float("nan"))
    )
    role_df = role_df.sort_values("lie_rate", ascending=False)

    # ── Type + wins lookups ───────────────────────────────────────────────────
    role_df["type"] = role_df["actual_role"].map(_role_type)

    _wr_lookup: dict[str, float] = {
        str(row["actual_role"]): round(float(row["win_rate"]), 3)
        for _, row in rw_grp.iterrows()
        if pd.notna(row.get("win_rate"))
    }
    _wins_lookup: dict[str, int] = {
        str(row["actual_role"]): int(row["wins"])
        for _, row in rw_grp.iterrows()
    }

    # ── Most oblivious outsider: highest (games_not_claimed / games) rate ─────
    _out_df = role_df[role_df["type"] == "Outsider"].copy()
    _out_df["hidden_rate"] = (
        _out_df["games_not_claimed"] / _out_df["games"].replace(0, float("nan"))
    )
    most_oblivious_outsider: dict | None = None
    if not _out_df.empty and _out_df["hidden_rate"].notna().any():
        _top_out = _out_df.dropna(subset=["hidden_rate"]).sort_values(
            "hidden_rate", ascending=False
        ).iloc[0]
        most_oblivious_outsider = {
            "role":             str(_top_out["actual_role"]),
            "games":            int(_top_out["games"]),
            "games_not_claimed": int(_top_out["games_not_claimed"]),
            "hidden_rate":      round(float(_top_out["hidden_rate"]), 3),
        }

    roles = role_df.apply(lambda r: {
        "role":                   r["actual_role"],
        "team":                   r["team"],
        "type":                   r["type"],
        "games":                  int(r["games"]),
        "wins":                   _wins_lookup.get(r["actual_role"], 0),
        "games_faked_in":         int(r["games_faked_in"]),
        "games_claimed_by_other": int(r["games_claimed_by_other"]),
        "games_cover_evil":       int(r["games_cover_evil"]),
        "games_cover_good":       int(r["games_cover_good"]),
        "games_not_claimed":      int(r["games_not_claimed"]),
        "games_phantom":          int(r["games_phantom"]),
        "lie_rate":               round(float(r["lie_rate"]), 3) if pd.notna(r["lie_rate"]) else 0.0,
        "win_rate":               _wr_lookup.get(r["actual_role"]),
    }, axis=1).tolist()

    # ── all-player table (for the leaderboard section) ────────────────────────
    players = (
        pa.sort_values("lies", ascending=False)
        .apply(lambda r: {
            "name":      r["player_name"],
            "games":     int(r["games"]),
            "claims":    int(r["claims"]),
            "lies":      int(r["lies"]),
            "true_":     int(r["true_"]),
            "lie_rate":  round(float(r["lie_rate"]), 3) if pd.notna(r["lie_rate"]) else None,
            "evil_rate": round(float(r["evil_rate"]), 3) if pd.notna(r["evil_rate"]) else None,
        }, axis=1)
        .tolist()
    )

    def _clean(s):
        """Normalise a row-dict: floats → rounded, numpy ints → int, rest → str."""
        if s is None:
            return None
        out = {}
        for k, v in s.items():
            if isinstance(v, float):
                out[k] = round(v, 3)
            elif isinstance(v, bool):
                out[k] = v
            elif hasattr(v, "item"):          # numpy scalar
                out[k] = v.item()
            elif isinstance(v, int):
                out[k] = v
            else:
                out[k] = str(v)
        return out

    return {
        "summary": {
            "games":    n_games,
            "players":  n_players,
            "claims":   n_claims,
            "lies":     n_lies,
            "true":     n_true,
            "hm":       n_hm,
            "unverified": n_unver,
        },
        "superlatives": {
            "most_evil":               _clean(most_evil),
            "most_good":               _clean(most_good),
            "biggest_liar":            _clean(biggest_liar),
            "most_honest":             _clean(most_honest),
            "traitor":                 _clean(traitor),
            "most_games":              _clean(most_games),
            "most_faked_role":         most_faked_role,
            "best_evil_role":          best_evil_role,
            "best_good_role":          best_good_role,
            "most_oblivious_outsider": most_oblivious_outsider,
        },
        "top_liars":   top_liars,
        "fake_roles":  fake_roles,
        "roles":       roles,
        "players":     players,
    }


def _load_recent_games(db: Path) -> list[dict]:
    df = _load_videos(db)
    return df.apply(lambda r: {
        "id":          r["id"],
        "title":       r["title"],
        "winner":      r["winner"] or "",
        "claims":      int(r["total_claims"]),
        "lies":        int(r["lie_count"] or 0),
    }, axis=1).tolist()


def _empty_stats() -> dict:
    return {
        "summary": {"games": 0, "players": 0, "claims": 0,
                    "lies": 0, "true": 0, "hm": 0, "unverified": 0},
        "wins": {"good": 0, "evil": 0},
        "superlatives": {k: None for k in
                         ["most_evil", "most_good", "biggest_liar",
                          "most_honest", "traitor", "most_games",
                          "most_talkative", "most_faked_role",
                          "best_evil_role", "best_good_role",
                          "most_oblivious_outsider"]},
        "top_liars":      [],
        "fake_roles":     [],
        "roles":          [],
        "players":        [],
        "voice_analysis": [],
        "game_records":   {"most_lies": None, "most_deceptive": None, "most_claims": None},
    }


# ── HTML template ─────────────────────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">
<title>🏰 BotC · Yogscast Lie Tracker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
:root {{
  --bg:        #0f0f1a;
  --surface:   #161625;
  --surface2:  #1e1e35;
  --border:    #2a2a45;
  --text:      #e2e8f0;
  --muted:     #8892a4;
  --evil:      #dc2626;
  --evil-dim:  #7f1d1d;
  --good:      #2563eb;
  --good-dim:  #1e3a8a;
  --gold:      #f59e0b;
  --green:     #16a34a;
  --radius:    12px;
  --shadow:    0 4px 24px rgba(0,0,0,.5);
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

body {{
  background: var(--bg);
  color: var(--text);
  font-family: system-ui, -apple-system, sans-serif;
  font-size: 15px;
  line-height: 1.6;
}}

a {{ color: var(--gold); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}

/* ── layout ── */
.container {{ max-width: 1100px; margin: 0 auto; padding: 0 20px; }}

/* ── header / hero ── */
header {{
  background: linear-gradient(160deg, #12122a 0%, #1a0a0a 100%);
  border-bottom: 1px solid var(--border);
  padding: 48px 20px 40px;
  text-align: center;
}}
.hero-title {{
  font-size: clamp(2rem, 5vw, 3.2rem);
  font-weight: 800;
  letter-spacing: -0.02em;
  background: linear-gradient(135deg, #fbbf24 0%, #f87171 60%, #a78bfa 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  margin-bottom: 8px;
}}
.hero-sub {{
  color: var(--muted);
  font-size: 1.05rem;
  margin-bottom: 32px;
}}
.stat-row {{
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 12px;
  margin-bottom: 28px;
}}
.stat-bubble {{
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 10px 22px;
  text-align: center;
  min-width: 110px;
}}
.stat-bubble .num {{
  font-size: 1.6rem;
  font-weight: 700;
  line-height: 1.1;
  color: var(--gold);
}}
.stat-bubble .lbl {{
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: .05em;
  color: var(--muted);
  margin-top: 2px;
}}

.btn-explore {{
  display: inline-block;
  background: linear-gradient(135deg, #7c3aed, #dc2626);
  color: #fff !important;
  font-weight: 700;
  font-size: 0.95rem;
  padding: 12px 32px;
  border-radius: 999px;
  text-decoration: none !important;
  letter-spacing: .03em;
  box-shadow: 0 0 20px rgba(124,58,237,.4);
  transition: transform .15s, box-shadow .15s;
}}
.btn-explore:hover {{
  transform: translateY(-2px);
  box-shadow: 0 0 30px rgba(124,58,237,.6);
}}

/* ── sections ── */
section {{
  padding: 48px 0 16px;
}}
.section-title {{
  font-size: 1.1rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .07em;
  color: var(--muted);
  margin-bottom: 20px;
  display: flex;
  align-items: center;
  gap: 8px;
}}
.section-title::after {{
  content: "";
  flex: 1;
  height: 1px;
  background: var(--border);
}}

/* ── superlative cards ── */
.sup-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 14px;
  margin-bottom: 32px;
}}
.sup-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px 16px;
  transition: transform .15s, box-shadow .15s;
}}
.sup-card:hover {{
  transform: translateY(-3px);
  box-shadow: var(--shadow);
}}
.sup-card .icon {{ font-size: 1.8rem; margin-bottom: 6px; }}
.sup-card .label {{
  font-size: 0.68rem;
  text-transform: uppercase;
  letter-spacing: .07em;
  color: var(--muted);
  margin-bottom: 4px;
}}
.sup-card .player {{
  font-size: 1.05rem;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.sup-card .detail {{
  font-size: 0.8rem;
  color: var(--muted);
  margin-top: 4px;
}}

/* ── charts row ── */
.charts-row {{
  display: grid;
  grid-template-columns: 1fr 2fr;
  gap: 20px;
  margin-bottom: 32px;
}}
@media (max-width: 700px) {{
  .charts-row {{ grid-template-columns: 1fr; }}
}}
.chart-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
}}
.chart-card h3 {{
  font-size: 0.82rem;
  text-transform: uppercase;
  letter-spacing: .06em;
  color: var(--muted);
  margin-bottom: 16px;
}}
.chart-wrap {{
  position: relative;
  width: 100%;
}}

/* ── roles table ── */
.table-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  margin-bottom: 32px;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.88rem;
}}
thead th {{
  background: var(--surface2);
  padding: 10px 14px;
  text-align: left;
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: .05em;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
}}
tbody tr {{
  border-bottom: 1px solid var(--border);
  transition: background .1s;
}}
tbody tr:last-child {{ border-bottom: none; }}
tbody tr:hover {{ background: var(--surface2); }}
td {{
  padding: 9px 14px;
  vertical-align: middle;
}}
.team-evil {{
  color: #fca5a5;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .05em;
}}
.team-good {{
  color: #93c5fd;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .05em;
}}
.rate-bar-wrap {{
  display: flex;
  align-items: center;
  gap: 8px;
}}
.rate-bar {{
  flex: 1;
  height: 6px;
  background: var(--border);
  border-radius: 999px;
  overflow: hidden;
  min-width: 60px;
}}
.rate-bar-fill {{
  height: 100%;
  border-radius: 999px;
  background: var(--evil);
}}

/* ── recent games grid ── */
.games-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
  margin-bottom: 32px;
}}
.game-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  transition: transform .15s, box-shadow .15s;
  display: flex;
  flex-direction: column;
  gap: 8px;
}}
.game-card:hover {{
  transform: translateY(-2px);
  box-shadow: var(--shadow);
}}
.game-title {{
  font-size: 0.88rem;
  font-weight: 600;
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}}
.game-meta {{
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}}
.badge {{
  font-size: 0.7rem;
  font-weight: 700;
  letter-spacing: .04em;
  padding: 2px 9px;
  border-radius: 999px;
  text-transform: uppercase;
}}
.badge-evil   {{ background: var(--evil-dim);  color: #fca5a5; }}
.badge-good   {{ background: var(--good-dim);  color: #93c5fd; }}
.badge-none   {{ background: var(--surface2);  color: var(--muted); }}
.game-stats {{
  font-size: 0.78rem;
  color: var(--muted);
}}
.game-link {{
  font-size: 0.75rem;
  color: var(--gold);
}}

/* ── footer ── */
footer {{
  border-top: 1px solid var(--border);
  padding: 24px 20px;
  text-align: center;
  color: var(--muted);
  font-size: 0.82rem;
}}
footer a {{ color: var(--gold); }}

/* ── empty state ── */
.empty {{
  text-align: center;
  padding: 48px;
  color: var(--muted);
  font-size: 1rem;
}}

/* ── roles split grid ── */
.roles-row {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 32px;
}}
@media (max-width: 700px) {{
  .roles-row {{ grid-template-columns: 1fr; }}
}}
.roles-team-hdr {{
  padding: 10px 14px;
  font-size: 0.82rem;
  font-weight: 700;
  letter-spacing: .04em;
  border-bottom: 1px solid var(--border);
}}
.roles-evil-hdr    {{ background: #200d0d; color: #fca5a5; }}
.roles-good-hdr    {{ background: #0a1120; color: #93c5fd; }}
.roles-demon-hdr   {{ background: #1a0808; color: #f87171; }}
.roles-minion-hdr  {{ background: #1a0e07; color: #fdba74; }}
.roles-outsider-hdr {{ background: #081318; color: #67e8f9; }}
.roles-townsfolk-hdr {{ background: #0a1120; color: #93c5fd; }}

/* ── Evil vs Good wins scoreboard ── */
.wins-board {{
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 28px;
  margin: 28px auto 12px;
  max-width: 520px;
}}
.wins-side {{
  text-align: center;
  flex: 1;
}}
.wins-label {{
  font-size: 0.68rem;
  text-transform: uppercase;
  letter-spacing: .12em;
  font-weight: 700;
  margin-bottom: 4px;
}}
.wins-good .wins-label {{ color: #93c5fd; }}
.wins-evil .wins-label {{ color: #fca5a5; }}
.wins-num {{
  font-size: clamp(3.5rem, 9vw, 5.5rem);
  font-weight: 900;
  line-height: 1;
}}
.wins-good .wins-num {{
  color: #60a5fa;
  text-shadow: 0 0 40px rgba(96,165,250,.45);
}}
.wins-evil .wins-num {{
  color: #f87171;
  text-shadow: 0 0 40px rgba(248,113,113,.45);
}}
.wins-sub {{
  font-size: 0.72rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .08em;
  margin-top: 2px;
}}
.wins-vs {{
  font-size: 1.1rem;
  font-weight: 700;
  color: var(--muted);
  flex: 0 0 auto;
  padding: 0 4px;
}}
.wins-bar-wrap {{
  max-width: 520px;
  margin: 0 auto 28px;
  height: 7px;
  border-radius: 999px;
  overflow: hidden;
  background: var(--border);
  display: flex;
}}
.wins-bar-good {{
  height: 100%;
  background: linear-gradient(90deg, #1e40af, #3b82f6);
  transition: width .8s cubic-bezier(.4,0,.2,1);
}}
.wins-bar-evil {{
  height: 100%;
  background: linear-gradient(90deg, #dc2626, #b91c1c);
  transition: width .8s cubic-bezier(.4,0,.2,1);
}}

/* ── game records grid ── */
.game-records-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
  margin-bottom: 32px;
}}
.record-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px 16px;
  transition: transform .15s, box-shadow .15s;
}}
.record-card:hover {{
  transform: translateY(-2px);
  box-shadow: var(--shadow);
}}
.record-card .icon {{ font-size: 1.6rem; margin-bottom: 6px; }}
.record-card .label {{
  font-size: 0.68rem;
  text-transform: uppercase;
  letter-spacing: .07em;
  color: var(--muted);
  margin-bottom: 4px;
}}
.record-card .record-title {{
  font-size: 0.88rem;
  font-weight: 600;
  line-height: 1.35;
  margin-bottom: 6px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}}
.record-card .record-stat {{ font-size: 0.78rem; color: var(--muted); }}

/* ── show-more / show-less row ── */
.showmore-row td {{
  text-align: center;
  padding: 10px !important;
  background: var(--surface) !important;
}}
.showmore-btn {{
  background: none;
  border: 1px solid var(--border);
  color: var(--gold);
  font-size: 0.78rem;
  padding: 5px 18px;
  border-radius: 999px;
  cursor: pointer;
  transition: background .1s;
}}
.showmore-btn:hover {{ background: var(--surface2); }}

/* ── info button & modal ──────────────────────────────────────────────────── */
.btn-info {{
  background: none;
  border: 1px solid var(--border);
  color: var(--muted);
  font-size: 0.75rem;
  padding: 4px 14px;
  border-radius: 999px;
  cursor: pointer;
  transition: border-color .15s, color .15s;
  margin-top: 10px;
  display: inline-block;
}}
.btn-info:hover {{ border-color: var(--gold); color: var(--gold); }}

#botc-info-overlay {{
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,.72);
  align-items: center;
  justify-content: center;
  z-index: 10000;
  padding: 20px;
}}
#botc-info-modal {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  max-width: 560px;
  width: 100%;
  padding: 28px 32px;
  position: relative;
  max-height: 82vh;
  overflow-y: auto;
  box-shadow: 0 8px 40px rgba(0,0,0,.6);
}}
#botc-info-modal h2 {{
  font-size: 1.1rem;
  font-weight: 700;
  margin-bottom: 16px;
  background: linear-gradient(135deg, #fbbf24 0%, #f87171 60%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}}
#botc-info-modal p {{
  font-size: 0.88rem;
  line-height: 1.75;
  color: var(--text);
  margin-bottom: 12px;
}}
#botc-info-modal p:last-child {{ margin-bottom: 0; }}
#botc-info-modal strong {{ color: var(--gold); font-weight: 700; }}
.info-close {{
  position: absolute;
  top: 14px; right: 14px;
  background: none;
  border: 1px solid var(--border);
  color: var(--muted);
  width: 28px; height: 28px;
  border-radius: 50%;
  cursor: pointer;
  font-size: 0.9rem;
  line-height: 26px;
  text-align: center;
  transition: border-color .1s, color .1s;
  padding: 0;
}}
.info-close:hover {{ border-color: var(--evil); color: var(--evil); }}

body.mc .btn-info {{
  font-family: 'Press Start 2P', monospace;
  font-size: 0.5rem;
  border-radius: 0;
  border: 2px solid #5a5a5a;
  padding: 8px 16px;
}}
body.mc .btn-info:hover {{ border-color: #ffaa00; color: #ffaa00; }}
body.mc #botc-info-modal {{
  background: #3c3c3c;
  border: 4px solid #1a1a1a;
  border-radius: 0;
  box-shadow: inset 2px 2px 0 #6a6a6a, inset -2px -2px 0 #2a2a2a, 6px 6px 0 #000;
}}
body.mc #botc-info-modal h2 {{
  font-family: 'Press Start 2P', monospace;
  background: transparent;
  -webkit-background-clip: unset;
  background-clip: unset;
  -webkit-text-fill-color: #fcfc00;
  color: #fcfc00;
  text-shadow: 2px 2px 0 #5a4a00;
  font-size: 0.72rem;
  letter-spacing: 0.04em;
}}
body.mc #botc-info-modal p {{
  font-family: 'Press Start 2P', monospace;
  font-size: 0.52rem;
  line-height: 2.1;
  color: #e0e0e0;
}}
body.mc #botc-info-modal strong {{ color: #ffaa00; }}
body.mc .info-close {{
  font-family: 'Press Start 2P', monospace;
  border-radius: 0;
  border: 2px solid #5a5a5a;
  box-shadow: inset -1px -1px 0 #2d2d2d, inset 1px 1px 0 #9a9a9a;
  width: 32px; height: 32px;
  line-height: 28px;
}}



/* ── MINECRAFT THEME ─────────────────────────────────────────────────────── */
body.mc {{
  --bg:       #1a1a1a;
  --surface:  #3c3c3c;
  --surface2: #2b2b2b;
  --border:   #1a1a1a;
  --text:     #ffffff;
  --muted:    #aaaaaa;
  --evil:     #ff5555;
  --evil-dim: #5a0000;
  --good:     #55ff55;
  --good-dim: #005500;
  --gold:     #ffaa00;
  --green:    #55ff55;
  --radius:   0px;
  background-color: #6b6b6b;
  background-image:
    repeating-linear-gradient(0deg,  transparent, transparent 15px, rgba(0,0,0,.28) 15px, rgba(0,0,0,.28) 16px),
    repeating-linear-gradient(90deg, transparent, transparent 15px, rgba(0,0,0,.28) 15px, rgba(0,0,0,.28) 16px);
  font-family: 'Press Start 2P', monospace;
  image-rendering: pixelated;
}}
body.mc a {{ color: #ffaa00; }}
body.mc header {{
  background: #1d1d1d;
  border-bottom: none;
  padding-bottom: 52px;
  position: relative;
}}
body.mc header::after {{
  content: '';
  position: absolute;
  bottom: 0; left: 0; right: 0;
  height: 14px;
  background: linear-gradient(180deg, #5d9734 0%, #5d9734 50%, #876440 50%);
}}
body.mc .hero-title {{
  font-family: 'Press Start 2P', monospace;
  background: transparent !important;
  -webkit-background-clip: unset !important;
  background-clip: unset !important;
  -webkit-text-fill-color: #fcfc00;
  color: #fcfc00;
  text-shadow: 4px 4px 0 #5a4a00;
  font-size: clamp(1rem, 2.5vw, 1.8rem);
  letter-spacing: 0.04em;
}}
body.mc .hero-sub {{
  font-family: 'Press Start 2P', monospace;
  color: #aaaaaa;
  font-size: 0.55rem;
  letter-spacing: 0.08em;
}}
body.mc .wins-board {{ gap: 20px; }}
body.mc .wins-label {{ font-family: 'Press Start 2P', monospace; font-size: 0.5rem; letter-spacing: 0.1em; }}
body.mc .wins-num   {{ font-family: 'Press Start 2P', monospace !important; font-size: clamp(2rem, 5vw, 3.5rem) !important; }}
body.mc .wins-sub   {{ font-family: 'Press Start 2P', monospace; font-size: 0.45rem; letter-spacing: 0.08em; }}
body.mc .wins-vs    {{ font-family: 'Press Start 2P', monospace; font-size: 0.9rem; }}
body.mc .wins-bar-wrap {{ border-radius: 0; height: 10px; }}
body.mc .wins-bar-good {{ background: linear-gradient(90deg, #33aa33, #55ff55); }}
body.mc .wins-bar-evil {{ background: linear-gradient(90deg, #aa3333, #ff5555); }}
body.mc .stat-bubble {{
  background: #3c3c3c;
  border: 2px solid #1a1a1a;
  border-radius: 0;
  box-shadow: inset 1px 1px 0 #6a6a6a, inset -1px -1px 0 #2a2a2a;
}}
body.mc .stat-bubble .num {{ font-family: 'Press Start 2P', monospace; font-size: 1.1rem; color: #ffaa00; }}
body.mc .stat-bubble .lbl {{ font-family: 'Press Start 2P', monospace; font-size: 0.45rem; letter-spacing: .04em; }}
body.mc .btn-explore {{
  background: #7f7f7f !important;
  border-radius: 0 !important;
  box-shadow: inset -4px -4px 0 #2d2d2d, inset 4px 4px 0 #cfcfcf !important;
  text-shadow: 2px 2px #3f3f3f;
  font-family: 'Press Start 2P', monospace;
  font-size: 0.6rem;
  letter-spacing: 0.08em;
  transition: background .1s;
}}
body.mc .btn-explore:hover {{
  background: #8888cc !important;
  box-shadow: inset -4px -4px 0 #2a2a7a, inset 4px 4px 0 #bbbbff !important;
  transform: none;
}}
body.mc .section-title {{ font-family: 'Press Start 2P', monospace; color: #ffaa00; font-size: 0.72rem; }}
body.mc .section-title::after {{ background: #5a5a5a; height: 2px; }}
body.mc .sup-card {{
  background: #3c3c3c !important;
  border: 2px solid #1a1a1a !important;
  border-radius: 0;
  box-shadow: inset 1px 1px 0 #5a5a5a, inset -1px -1px 0 #2a2a2a;
}}
body.mc .sup-card:hover {{ transform: none; box-shadow: inset 1px 1px 0 #5a5a5a, inset -1px -1px 0 #2a2a2a; }}
body.mc .sup-card .icon  {{ font-size: 1.4rem; }}
body.mc .sup-card .label {{ font-family: 'Press Start 2P', monospace; font-size: 0.45rem; letter-spacing: 0.05em; }}
body.mc .sup-card .player {{ font-family: 'Press Start 2P', monospace; font-size: 0.7rem; color: #ffaa00; }}
body.mc .sup-card .detail {{ font-family: 'Press Start 2P', monospace; font-size: 0.5rem; }}
body.mc .chart-card {{
  background: #3c3c3c;
  border: 2px solid #1a1a1a;
  border-radius: 0;
  box-shadow: inset 1px 1px 0 #5a5a5a, inset -1px -1px 0 #2a2a2a;
}}
body.mc .chart-card h3 {{ font-family: 'Press Start 2P', monospace; font-size: 0.55rem; color: #aaaaaa; letter-spacing: 0.05em; }}
body.mc .table-card {{
  border: 2px solid #1a1a1a;
  border-radius: 0;
  box-shadow: inset 1px 1px 0 #5a5a5a;
}}
body.mc thead th {{
  background: #2b2b2b;
  font-family: 'Press Start 2P', monospace;
  font-size: 0.45rem;
  color: #aaaaaa;
  letter-spacing: 0.04em;
  border-bottom: 2px solid #1a1a1a;
}}
body.mc tbody tr {{ border-bottom-color: #2b2b2b; }}
body.mc tbody tr:hover {{ background: #4a4a4a; }}
body.mc td {{ font-family: 'Press Start 2P', monospace; font-size: 0.58rem; line-height: 1.8; }}
body.mc .team-evil {{ color: #ff8888; font-family: 'Press Start 2P', monospace; font-size: 0.48rem; }}
body.mc .team-good {{ color: #88ff88; font-family: 'Press Start 2P', monospace; font-size: 0.48rem; }}
body.mc .rate-bar {{ border-radius: 0; background: #1a1a1a; }}
body.mc .rate-bar-fill {{ border-radius: 0; }}
body.mc .badge {{ border-radius: 0; font-family: 'Press Start 2P', monospace; font-size: 0.45rem; }}
body.mc .badge-evil {{ background: #5a0000; color: #ff8888; }}
body.mc .badge-good {{ background: #005500; color: #88ff88; }}
body.mc .roles-evil-hdr     {{ background: #3d0000 !important; color: #ff8888 !important; font-family: 'Press Start 2P', monospace; font-size: 0.6rem; }}
body.mc .roles-good-hdr     {{ background: #003d00 !important; color: #88ff88 !important; font-family: 'Press Start 2P', monospace; font-size: 0.6rem; }}
body.mc .roles-faked-hdr    {{ background: #1a0030 !important; color: #cc88ff !important; font-family: 'Press Start 2P', monospace; font-size: 0.55rem; }}
body.mc .roles-demon-hdr    {{ background: #3d0000 !important; color: #ff8888 !important; font-family: 'Press Start 2P', monospace; font-size: 0.6rem; }}
body.mc .roles-minion-hdr   {{ background: #2b1800 !important; color: #ffaa44 !important; font-family: 'Press Start 2P', monospace; font-size: 0.6rem; }}
body.mc .roles-outsider-hdr {{ background: #001820 !important; color: #44ddff !important; font-family: 'Press Start 2P', monospace; font-size: 0.6rem; }}
body.mc .roles-townsfolk-hdr {{ background: #003d00 !important; color: #88ff88 !important; font-family: 'Press Start 2P', monospace; font-size: 0.6rem; }}
body.mc .game-card {{
  background: #3c3c3c;
  border: 2px solid #1a1a1a;
  border-radius: 0;
  box-shadow: inset 1px 1px 0 #5a5a5a, inset -1px -1px 0 #2a2a2a;
}}
body.mc .game-card:hover {{ transform: none; box-shadow: inset 1px 1px 0 #7a7a7a; }}
body.mc .game-title {{ font-family: 'Press Start 2P', monospace; font-size: 0.6rem; line-height: 1.8; }}
body.mc .game-stats {{ font-family: 'Press Start 2P', monospace; font-size: 0.5rem; }}
body.mc .game-link  {{ font-family: 'Press Start 2P', monospace; font-size: 0.5rem; color: #ffaa00; }}
body.mc footer {{
  background: #2b2b2b;
  border-top: 2px solid #1a1a1a;
  font-family: 'Press Start 2P', monospace;
  font-size: 0.5rem;
  line-height: 2;
}}
body.mc footer a {{ color: #ffaa00; }}

/* ── MC overrides: new elements ── */
body.mc .record-card {{
  background: #3c3c3c;
  border: 2px solid #1a1a1a;
  border-radius: 0;
  box-shadow: inset 1px 1px 0 #5a5a5a, inset -1px -1px 0 #2a2a2a;
}}
body.mc .record-card:hover {{ transform: none; box-shadow: inset 1px 1px 0 #5a5a5a, inset -1px -1px 0 #2a2a2a; }}
body.mc .record-card .label {{ font-family: 'Press Start 2P', monospace; font-size: 0.45rem; }}
body.mc .record-card .record-title {{ font-family: 'Press Start 2P', monospace; font-size: 0.55rem; line-height: 1.8; }}
body.mc .record-card .record-stat {{ font-family: 'Press Start 2P', monospace; font-size: 0.5rem; }}
body.mc .showmore-row td {{ background: #3c3c3c !important; }}
body.mc .showmore-btn {{ font-family: 'Press Start 2P', monospace; font-size: 0.5rem; border-radius: 0; border-color: #5a5a5a; color: #ffaa00; }}
/* ── theme toggle button ──────────────────────────────────────────────────── */
#theme-toggle {{
  position: fixed;
  top: 12px;
  right: 12px;
  z-index: 9999;
  background: #312a5a;
  border: 1px solid #2a2a45;
  color: #e2e8f0;
  font-size: 0.8rem;
  font-weight: 700;
  padding: 8px 14px;
  border-radius: 8px;
  cursor: pointer;
  transition: background .15s, transform .1s;
  letter-spacing: .02em;
  line-height: 1;
}}
#theme-toggle:hover {{ background: #4a3a7a; transform: translateY(-1px); }}
body.mc #theme-toggle {{
  background: #7f7f7f;
  border: none;
  box-shadow: inset -3px -3px 0 #2d2d2d, inset 3px 3px 0 #cfcfcf;
  color: #fff;
  text-shadow: 2px 2px #3f3f3f;
  font-family: 'Press Start 2P', monospace;
  border-radius: 0;
  font-size: 0.55rem;
  padding: 10px 14px;
  letter-spacing: 0.05em;
  transition: background .1s;
  transform: none;
}}
body.mc #theme-toggle:hover {{
  background: #8888cc;
  box-shadow: inset -3px -3px 0 #2a2a7a, inset 3px 3px 0 #bbbbff;
  transform: none;
}}
body.mc #theme-toggle:active {{
  box-shadow: inset 3px 3px 0 #2d2d2d, inset -3px -3px 0 #cfcfcf;
}}
</style>
</head>
<body>

<button id="theme-toggle" onclick="toggleTheme()">⛏ MC Mode</button>

<header>
  <div class="hero-title">🏰 Blood on the Clocktower</div>
  <div class="hero-sub">Yogscast &middot; Automated lie detection across every game</div>
  <button class="btn-info" onclick="document.getElementById('botc-info-overlay').style.display='flex'">❓ What is Blood on the Clocktower?</button>

  <div class="wins-board" id="wins-board"></div>
  <div class="wins-bar-wrap" id="wins-bar-wrap">
    <div class="wins-bar-good" id="wbar-good"></div>
    <div class="wins-bar-evil" id="wbar-evil"></div>
  </div>

  <div class="stat-row" id="stat-row"></div>
</header>

<main class="container">

  <section>
    <div class="section-title"><span>🏆</span> Superlatives</div>
    <div class="sup-grid" id="sup-grid"></div>
  </section>

  <section>
    <div class="section-title"><span>📜</span> Game Records</div>
    <div class="game-records-grid" id="game-records-grid"></div>
  </section>

  <section>
    <div class="section-title"><span>📊</span> Lie Breakdown</div>
    <div class="charts-row">
      <div class="chart-card">
        <h3>Verdict split</h3>
        <div class="chart-wrap" style="max-width:240px;margin:0 auto;">
          <canvas id="donut-chart"></canvas>
        </div>
      </div>
      <div class="chart-card">
        <h3>Top liars by lie count</h3>
        <div class="chart-wrap">
          <canvas id="bar-chart"></canvas>
        </div>
      </div>
    </div>
  </section>

  <section>
    <div class="section-title"><span>🎭</span> Role Breakdown <span style="font-size:.8rem;font-weight:400;margin-left:4px;">(min 2 games)</span></div>

    <div class="table-card" style="margin-bottom:20px">
      <div class="roles-team-hdr roles-faked-hdr" style="background:#1a1430;color:#c4b5fd;border-bottom:1px solid var(--border)">🎭 Roles Most Often Faked <span style="font-size:.78rem;font-weight:400;opacity:.7">(min 2 games · sorted by games used as cover)</span></div>
      <table>
        <thead>
          <tr>
            <th>Claimed Role</th>
            <th>Team</th>
            <th>Games as Cover</th>
            <th>Games Honest</th>
          </tr>
        </thead>
        <tbody id="fake-roles-body"></tbody>
      </table>
    </div>

    <div class="roles-row" style="margin-bottom:16px">
      <div class="table-card">
        <div class="roles-team-hdr roles-demon-hdr">👹 Demon <span style="font-size:.78rem;font-weight:400;opacity:.7">(sorted by games played)</span></div>
        <table>
          <thead><tr><th>Role</th><th>Appeared</th><th>Won</th><th>Win rate</th></tr></thead>
          <tbody id="demon-roles-body"></tbody>
        </table>
      </div>
      <div class="table-card">
        <div class="roles-team-hdr roles-minion-hdr">😈 Minion <span style="font-size:.78rem;font-weight:400;opacity:.7">(sorted by games played)</span></div>
        <table>
          <thead><tr><th>Role</th><th>Appeared</th><th>Won</th><th>Lie rate</th></tr></thead>
          <tbody id="minion-roles-body"></tbody>
        </table>
      </div>
    </div>
    <div class="roles-row">
      <div class="table-card">
        <div class="roles-team-hdr roles-outsider-hdr">🤷 Outsider <span style="font-size:.78rem;font-weight:400;opacity:.7">(sorted by evil cover usage)</span></div>
        <table>
          <thead><tr><th>Role</th><th>Games</th><th>Evil cover</th><th>Good cover</th></tr></thead>
          <tbody id="outsider-roles-body"></tbody>
        </table>
      </div>
      <div class="table-card">
        <div class="roles-team-hdr roles-townsfolk-hdr">🕵️ Townsfolk <span style="font-size:.78rem;font-weight:400;opacity:.7">(sorted by evil cover usage)</span></div>
        <table>
          <thead><tr><th>Role</th><th>Games</th><th>Evil cover</th><th>Good cover</th><th>Not in play</th></tr></thead>
          <tbody id="townsfolk-roles-body"></tbody>
        </table>
      </div>
    </div>
  </section>

  <section>
    <div class="section-title"><span>🗣️</span> Voice Analysis <span style="font-size:.8rem;font-weight:400;margin-left:4px;">(avg words spoken per game · sorted by evil/good ratio)</span></div>
    <div class="table-card">
      <table>
        <thead>
          <tr>
            <th>Player</th>
            <th style="color:#93c5fd">Good WPG</th>
            <th style="color:#fca5a5">Evil WPG</th>
            <th>E/G Ratio</th>
          </tr>
        </thead>
        <tbody id="voice-body"></tbody>
      </table>
    </div>
  </section>

  <section>
    <div class="section-title"><span>🎮</span> Recent Games</div>
    <div class="games-grid" id="games-grid"></div>
  </section>

  <div style="text-align:center;padding:40px 0 24px">
    <a class="btn-explore" href="{explorer_url}" target="_blank">▶&nbsp; Open Full Explorer</a>
  </div>

</main>

<!-- BotC info modal -->
<div id="botc-info-overlay" onclick="if(event.target===this)closeBotcInfo()">
  <div id="botc-info-modal" role="dialog" aria-modal="true" aria-label="What is Blood on the Clocktower?">
    <button class="info-close" onclick="closeBotcInfo()" aria-label="Close">✕</button>
    <h2>🏰 Blood on the Clocktower</h2>
    <p><strong>Blood on the Clocktower</strong> is a social deduction game for 5–20 players, played in person. Think Mafia or Werewolf, but with a dedicated Storyteller, full information during setup, and the ability to win even from the grave.</p>
    <p>Players are split into two teams: <strong>Good</strong> (Townsfolk &amp; Outsiders) and <strong>Evil</strong> (Minions &amp; the Demon). Each player is dealt a secret role card with a unique ability. Good wins by executing the Demon; Evil wins if the Demon survives until only two players remain.</p>
    <p>Every night the Demon and Minions wake silently to scheme, while Townsfolk may receive clues from the Storyteller. Every <strong>day</strong>, players openly debate, accuse, and vote to execute one suspect — then night falls again.</p>
    <p><strong>Role-claiming</strong> is central to strategy. Good players share information; Evil players lie about who they are. This tracker automatically flags whenever a player's stated role doesn't match their actual role card — that's a lie!</p>
    <p>The Yogscast play on their charity streams, usually with 10–15 players. The games you see here have all been automatically transcribed and analysed.</p>
  </div>
</div>

<footer>
  Generated {generated_at} &middot;
  <a href="{explorer_url}" target="_blank">▶ Open full explorer</a>
</footer>

<script>
const DATA    = {data_json};
const GAMES   = {games_json};
const EXPLORER = "{explorer_url}";


// ── theme globals & helpers ───────────────────────────────────────────────────
let _donutChart = null;
let _barChart   = null;


function updateChartTheme() {{
  const isMC  = document.body.classList.contains('mc');
  const gridC = isMC ? '#4a4a4a' : '#2a2a45';
  const txX   = isMC ? '#aaaaaa' : '#8892a4';
  const txY   = isMC ? '#ffffff' : '#e2e8f0';
  const brdC  = isMC ? '#1a1a1a' : '#161625';
  const legC  = isMC ? '#aaaaaa' : '#8892a4';
  if (_donutChart) {{
    _donutChart.data.datasets[0].borderColor = brdC;
    _donutChart.options.plugins.legend.labels.color = legC;
    _donutChart.update();
  }}
  if (_barChart) {{
    _barChart.options.scales.x.grid.color  = gridC;
    _barChart.options.scales.x.ticks.color = txX;
    _barChart.options.scales.y.ticks.color = txY;
    _barChart.update();
  }}
}}

// ── BotC info modal ───────────────────────────────────────────────────────────
function closeBotcInfo() {{
  document.getElementById('botc-info-overlay').style.display = 'none';
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') closeBotcInfo();
}});

function toggleTheme() {{
  const isMC = document.body.classList.toggle('mc');
  localStorage.setItem('botc-theme', isMC ? 'mc' : 'classic');
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = isMC ? '🌙 Classic' : '⛏ MC Mode';
  const sub = document.querySelector('.hero-sub');
  if (sub) sub.textContent = isMC
    ? '⛏  Yogscast  ·  Auto-lie detection across every game  ⛏'
    : 'Yogscast · Automated lie detection across every game';
  updateChartTheme();
}}

// ── wins scoreboard ────────────────────────────────────────────────────────────
function renderWins() {{
  const w = DATA.wins || {{good: 0, evil: 0}};
  const board = document.getElementById("wins-board");
  if (board) {{
    board.innerHTML =
      `<div class="wins-side wins-good">
         <div class="wins-label">Good</div>
         <div class="wins-num">${{w.good}}</div>
         <div class="wins-sub">wins</div>
       </div>
       <div class="wins-vs">VS</div>
       <div class="wins-side wins-evil">
         <div class="wins-label">Evil</div>
         <div class="wins-num">${{w.evil}}</div>
         <div class="wins-sub">wins</div>
       </div>`;
  }}
  const total = (w.good + w.evil) || 1;
  const goodPct = (w.good / total * 100).toFixed(2);
  const evilPct = (w.evil / total * 100).toFixed(2);
  const bg = document.getElementById("wbar-good");
  const be = document.getElementById("wbar-evil");
  if (bg) bg.style.width = goodPct + "%";
  if (be) be.style.width = evilPct + "%";
}}
renderWins();

// ── stat bubbles ──────────────────────────────────────────────────────────────
function renderStatBubbles() {{
  const s = DATA.summary;
  const lie_rate = s.claims > 0 ? (s.lies / s.claims * 100).toFixed(1) + "%" : "—";
  const items = [
    [s.games,   "Games"],
    [s.players, "Players"],
    [s.claims,  "Role Claims"],
    [s.lies,    "Lies Told"],
    [s.true,    "True Claims"],
    [lie_rate,  "Lie Rate"],
  ];
  const row = document.getElementById("stat-row");
  row.innerHTML = '';
  items.forEach(([num, lbl]) => {{
    row.innerHTML +=
      `<div class="stat-bubble"><div class="num">${{num}}</div><div class="lbl">${{lbl}}</div></div>`;
  }});
}}
renderStatBubbles();

// ── superlatives ──────────────────────────────────────────────────────────────
function renderSuperlatives() {{
  const sup = DATA.superlatives;
  const cards = [
    {{
      key: "most_evil", icon: "😈", label: "Most Often Evil",
      name:   s => s.player_name,
      detail: s => `Evil ${{s.evil_g}}/${{s.games}} games (${{(s.evil_rate*100).toFixed(0)}}%)`,
      bg: "#2a1215",
    }},
    {{
      key: "most_good", icon: "😇", label: "Most Often Good",
      name:   s => s.player_name,
      detail: s => `Good ${{s.good_g}}/${{s.games}} games (${{(s.good_rate*100).toFixed(0)}}%)`,
      bg: "#0e1f2a",
    }},
    {{
      key: "biggest_liar", icon: "🤥", label: "Biggest Liar",
      name:   s => s.player_name,
      detail: s => `${{s.lies}} lies · ${{(s.lie_rate*100).toFixed(0)}}% lie rate`,
      bg: "#2a1215",
    }},
    {{
      key: "most_honest", icon: "🌟", label: "Most Honest",
      name:   s => s.player_name,
      detail: s => `${{s.true_}} true · ${{(s.lie_rate*100).toFixed(0)}}% lie rate`,
      bg: "#0e2a1f",
    }},
    {{
      key: "traitor", icon: "🗡️", label: "Lied to Their Town",
      name:   s => s.player_name,
      detail: s => `${{s.good_lies}} lies while playing Good`,
      bg: "#2a200e",
    }},
    {{
      key: "most_games", icon: "🎮", label: "Most Games Played",
      name:   s => s.player_name,
      detail: s => `${{s.games}} appearances`,
      bg: "#1a1a2e",
    }},
    {{
      key: "most_talkative", icon: "🗣️", label: "Most Talkative",
      name:   s => s.player_name,
      detail: s => `~${{Math.round(s.wpg).toLocaleString()}} words/game avg`,
      bg: "#1a2a1a",
    }},
    {{
      key: "most_faked_role", icon: "🪪", label: "Favourite Cover Story",
      name:   s => s.role,
      detail: s => `Used as cover in ${{s.games_faked}} game${{s.games_faked !== 1 ? 's' : ''}}`,
      bg: "#1a1430",
    }},
    {{
      key: "best_evil_role", icon: "☠️", label: "Most Successful Evil Role",
      name:   s => s.role,
      detail: s => `${{(s.win_rate*100).toFixed(0)}}% win rate (${{s.wins}}/${{s.total_games}} games)`,
      bg: "#2a1215",
    }},
    {{
      key: "best_good_role", icon: "🛡️", label: "Most Successful Good Role",
      name:   s => s.role,
      detail: s => `${{(s.win_rate*100).toFixed(0)}}% win rate (${{s.wins}}/${{s.total_games}} games)`,
      bg: "#0e2a1f",
    }},
    {{
      key: "most_oblivious_outsider", icon: "🫣", label: "Most Often Hidden Outsider",
      name:   s => s.role,
      detail: s => `Never revealed in ${{s.games_not_claimed}}/${{s.games}} games (${{(s.hidden_rate*100).toFixed(0)}}%)`,
      bg: "#081318",
    }},
  ];

  const grid = document.getElementById("sup-grid");
  grid.innerHTML = '';
  cards.forEach(c => {{
    const data = sup[c.key];
    const html = data
      ? `<div class="icon">${{c.icon}}</div>
         <div class="label">${{c.label}}</div>
         <div class="player">${{c.name(data)}}</div>
         <div class="detail">${{c.detail(data)}}</div>`
      : `<div class="icon">${{c.icon}}</div>
         <div class="label">${{c.label}}</div>
         <div class="player" style="color:var(--muted)">—</div>`;
    grid.innerHTML +=
      `<div class="sup-card" style="border-color:${{c.bg === "#2a1215" ? "#5a1f1f" :
        c.bg === "#0e1f2a" ? "#1e3a5a" : "var(--border)"}};background:${{c.bg}}">${{html}}</div>`;
  }});
}}
renderSuperlatives();

// ── doughnut chart ────────────────────────────────────────────────────────────
function renderDonut() {{
  const s = DATA.summary;
  if (_donutChart) {{
    _donutChart.data.datasets[0].data = [s.lies, s.true, s.unverified, s.hm];
    _donutChart.options.plugins.tooltip.callbacks.label =
      ctx => ` ${{ctx.label}}: ${{ctx.parsed}} (${{(ctx.parsed/(s.claims||1)*100).toFixed(1)}}%)`;
    _donutChart.update();
    return;
  }}
  _donutChart = new Chart(document.getElementById("donut-chart"), {{
    type: "doughnut",
    data: {{
      labels: ["LIE", "TRUE", "UNVERIFIED", "HONEST MISTAKE"],
      datasets: [{{
        data: [s.lies, s.true, s.unverified, s.hm],
        backgroundColor: ["#dc2626", "#16a34a", "#4b5563", "#d97706"],
        borderColor: "#161625",
        borderWidth: 3,
        hoverOffset: 8,
      }}],
    }},
    options: {{
      plugins: {{
        legend: {{
          position: "bottom",
          labels: {{
            color: "#8892a4",
            font: {{ size: 11 }},
            padding: 12,
          }},
        }},
        tooltip: {{
          callbacks: {{
            label: ctx => ` ${{ctx.label}}: ${{ctx.parsed}} (${{(ctx.parsed/(s.claims||1)*100).toFixed(1)}}%)`,
          }},
        }},
      }},
      cutout: "68%",
    }},
  }});
}}
renderDonut();

// ── horizontal bar chart (top liars) ──────────────────────────────────────────
function renderBarChart() {{
  const liars = DATA.top_liars;
  if (!liars.length) return;
  const labels = liars.map(x => x.name);
  const lies   = liars.map(x => x.lies);
  const rates  = liars.map(x => x.lie_rate);
  const colors = rates.map(r => {{
    const t = Math.min(r, 1);
    return `rgba(220, ${{Math.round(38 + (1-t)*140)}}, ${{Math.round(38 + (1-t)*100)}}, 0.85)`;
  }});
  if (_barChart) {{
    _barChart.data.labels = labels;
    _barChart.data.datasets[0].data = lies;
    _barChart.data.datasets[0].backgroundColor = colors;
    // Rebuild tooltip closure with new rates
    _barChart.options.plugins.tooltip.callbacks.label =
      ctx => ` ${{ctx.parsed.x}} lies · ${{(rates[ctx.dataIndex]*100).toFixed(0)}}% lie rate`;
    _barChart.update();
    return;
  }}
  _barChart = new Chart(document.getElementById("bar-chart"), {{
    type: "bar",
    data: {{
      labels,
      datasets: [{{
        label: "Lies told",
        data: lies,
        backgroundColor: colors,
        borderRadius: 6,
      }}],
    }},
    options: {{
      indexAxis: "y",
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          callbacks: {{
            label: ctx => ` ${{ctx.parsed.x}} lies · ${{(rates[ctx.dataIndex]*100).toFixed(0)}}% lie rate`,
          }},
        }},
      }},
      scales: {{
        x: {{
          grid:  {{ color: "#2a2a45" }},
          ticks: {{ color: "#8892a4", font: {{ size: 11 }} }},
        }},
        y: {{
          grid:  {{ display: false }},
          ticks: {{ color: "#e2e8f0", font: {{ size: 12 }} }},
        }},
      }},
    }},
  }});
}}
renderBarChart();

// ── most-faked roles table (collapsible) ──────────────────────────────────────
function renderFakeRoles() {{
  const LIMIT = 10;
  let expanded = false;
  const tbody = document.getElementById("fake-roles-body");

  function render() {{
    const rows = DATA.fake_roles || [];
    if (!rows.length) {{
      tbody.innerHTML = `<tr><td colspan="4" class="empty">No lie data yet.</td></tr>`;
      return;
    }}
    tbody.innerHTML = '';
    (expanded ? rows : rows.slice(0, LIMIT)).forEach(r => {{
      tbody.innerHTML +=
        `<tr>
          <td style="font-weight:600">${{r.role}}</td>
          <td><span class="team-${{r.team.toLowerCase()}}">${{r.team}}</span></td>
          <td>${{r.games_faked}}</td>
          <td>${{r.games_honest}}</td>
        </tr>`;
    }});
    if (rows.length > LIMIT) {{
      const tr = document.createElement('tr');
      tr.className = 'showmore-row';
      const td = document.createElement('td'); td.colSpan = 4;
      const btn = document.createElement('button'); btn.className = 'showmore-btn';
      btn.textContent = expanded ? '▲ Show fewer' : `▼ Show all ${{rows.length}} roles`;
      btn.onclick = () => {{ expanded = !expanded; render(); }};
      td.appendChild(btn); tr.appendChild(td); tbody.appendChild(tr);
    }}
  }}
  render();
}}
renderFakeRoles();

// ── roles tables (split by type: Demon / Minion / Outsider / Townsfolk) ──────
function renderRoles() {{
  // Generic collapsible table helper
  // cols: array of {{fn(r) → html string}} descriptors
  // sorted: pre-sorted array
  function makeTable(tbodyId, sorted, cols, barColor) {{
    const LIMIT = 10;
    let expanded = false;
    const tbody = document.getElementById(tbodyId);
    const ncols = cols.length;
    function render() {{
      if (!sorted.length) {{
        tbody.innerHTML = `<tr><td colspan="${{ncols}}" class="empty" style="padding:20px">—</td></tr>`;
        return;
      }}
      tbody.innerHTML = '';
      (expanded ? sorted : sorted.slice(0, LIMIT)).forEach(r => {{
        const cells = cols.map(c => `<td>${{c.fn(r)}}</td>`).join('');
        tbody.innerHTML += `<tr>${{cells}}</tr>`;
      }});
      if (sorted.length > LIMIT) {{
        const tr = document.createElement('tr');
        tr.className = 'showmore-row';
        const td = document.createElement('td'); td.colSpan = ncols;
        const btn = document.createElement('button'); btn.className = 'showmore-btn';
        btn.textContent = expanded ? '▲ Show fewer' : `▼ Show all ${{sorted.length}} roles`;
        btn.onclick = () => {{ expanded = !expanded; render(); }};
        td.appendChild(btn); tr.appendChild(td); tbody.appendChild(tr);
      }}
    }}
    render();
  }}

  function rateBar(pct, color) {{
    return `<div class="rate-bar-wrap">
      <div class="rate-bar"><div class="rate-bar-fill" style="width:${{pct}}%;background:${{color}}"></div></div>
      <span style="font-size:.8rem;min-width:34px">${{pct}}%</span>
    </div>`;
  }}

  // ── Demon: Role | Appeared | Won | Win rate ─────────────────────────────────
  // Lie rate omitted — Demons always lie; win rate is the meaningful signal.
  const demons = [...DATA.roles.filter(r => r.type === "Demon")]
    .sort((a, b) => b.games - a.games);
  makeTable("demon-roles-body", demons, [
    {{ fn: r => `<span style="font-weight:600">${{r.role}}</span>` }},
    {{ fn: r => r.games }},
    {{ fn: r => r.wins != null ? r.wins : '—' }},
    {{ fn: r => r.win_rate != null
                 ? rateBar((r.win_rate * 100).toFixed(0), "#dc2626")
                 : `<span style="color:var(--muted)">—</span>` }},
  ]);

  // ── Minion: Role | Appeared | Won | Lie rate ────────────────────────────────
  // "Won" = games where Evil won while this Minion type was in play.
  // "Times killed by Demon" not yet tracked in pipeline data.
  const minions = [...DATA.roles.filter(r => r.type === "Minion")]
    .sort((a, b) => b.games - a.games);
  makeTable("minion-roles-body", minions, [
    {{ fn: r => `<span style="font-weight:600">${{r.role}}</span>` }},
    {{ fn: r => r.games }},
    {{ fn: r => r.wins != null ? r.wins : '—' }},
    {{ fn: r => rateBar((r.lie_rate * 100).toFixed(0), "#f97316") }},
  ]);

  // ── Outsider: Role | Games | Evil cover | Good cover ────────────────────────
  // Evil players love bluffing Outsider roles (Drunk, Butler, Recluse…).
  // Good cover = a non-holder Good player claimed this role (e.g. coordination bluff).
  // "Never claimed" moved to Superlatives as most_oblivious_outsider.
  const outsiders = [...DATA.roles.filter(r => r.type === "Outsider")]
    .sort((a, b) => b.games_cover_evil - a.games_cover_evil);
  makeTable("outsider-roles-body", outsiders, [
    {{ fn: r => `<span style="font-weight:600">${{r.role}}</span>` }},
    {{ fn: r => r.games }},
    {{ fn: r => r.games_cover_evil
                 ? `<span style="color:#fca5a5">${{r.games_cover_evil}}</span>` : '—' }},
    {{ fn: r => r.games_cover_good
                 ? `<span style="color:#93c5fd">${{r.games_cover_good}}</span>` : '—' }},
  ]);

  // ── Townsfolk: Role | Games | Evil cover | Good cover | Not in play ──────────
  // Sorted by Evil cover desc so the juiciest bluff targets float to the top.
  // "Not in play" = games where role was claimed but nobody actually held it.
  const townsfolk = [...DATA.roles.filter(r => r.type === "Townsfolk")]
    .sort((a, b) => b.games_cover_evil - a.games_cover_evil);
  makeTable("townsfolk-roles-body", townsfolk, [
    {{ fn: r => `<span style="font-weight:600">${{r.role}}</span>` }},
    {{ fn: r => r.games }},
    {{ fn: r => r.games_cover_evil
                 ? `<span style="color:#fca5a5">${{r.games_cover_evil}}</span>` : '—' }},
    {{ fn: r => r.games_cover_good
                 ? `<span style="color:#93c5fd">${{r.games_cover_good}}</span>` : '—' }},
    {{ fn: r => r.games_phantom
                 ? `<span style="color:#c4b5fd;font-weight:600">${{r.games_phantom}}</span>` : '—' }},
  ]);
}}
renderRoles();

// ── games grid ────────────────────────────────────────────────────────────────
(function () {{
  const grid = document.getElementById("games-grid");
  if (!GAMES.length) {{
    grid.innerHTML = `<div class="empty">No games analyzed yet.</div>`;
    return;
  }}
  GAMES.forEach(g => {{
    const wCls   = g.winner === "Evil" ? "badge-evil" : g.winner === "Good" ? "badge-good" : "badge-none";
    const wLabel = g.winner || "Unknown";
    const ytUrl  = `https://www.youtube.com/watch?v=${{g.id}}`;
    const lieRate = g.claims > 0 ? (g.lies / g.claims * 100).toFixed(0) + "% lie rate" : "";
    grid.innerHTML +=
      `<div class="game-card">
        <div class="game-title">${{g.title}}</div>
        <div class="game-meta">
          <span class="badge ${{wCls}}">${{wLabel}} wins</span>
          <span class="game-stats">${{g.lies}} lie${{g.lies!==1?"s":""}} · ${{g.claims}} claim${{g.claims!==1?"s":""}}</span>
        </div>
        <a class="game-link" href="${{ytUrl}}" target="_blank">▶ Watch on YouTube →</a>
      </div>`;
  }});
}})();

// ── game records ─────────────────────────────────────────────────────────────
function renderGameRecords() {{
  const rec  = DATA.game_records || {{}};
  const grid = document.getElementById('game-records-grid');
  if (!grid) return;
  grid.innerHTML = '';
  const cards = [
    {{ key: 'most_lies',      icon: '🤥', label: 'Most Lies in One Game',
       stat: r => `${{r.lies}} lies · ${{(r.lie_rate*100).toFixed(0)}}% lie rate`, bg: '#2a1215' }},
    {{ key: 'most_deceptive', icon: '🎭', label: 'Most Deceptive Game',
       stat: r => `${{(r.lie_rate*100).toFixed(0)}}% lie rate (${{r.lies}}/${{r.claims}} claims)`, bg: '#1a1430' }},
    {{ key: 'most_claims',    icon: '📋', label: 'Most Role Claims Made',
       stat: r => `${{r.claims}} claims · ${{r.lies}} lies`, bg: '#0e1f2a' }},
  ];
  cards.forEach(c => {{
    const data = rec[c.key];
    if (!data) return;
    const ytUrl = `https://www.youtube.com/watch?v=${{data.id}}`;
    const wCls  = data.winner === 'Evil' ? 'badge-evil' : data.winner === 'Good' ? 'badge-good' : 'badge-none';
    grid.innerHTML +=
      `<div class="record-card" style="background:${{c.bg}}">
        <div class="icon">${{c.icon}}</div>
        <div class="label">${{c.label}}</div>
        <div class="record-title">${{data.title}}</div>
        <div class="record-stat">${{c.stat(data)}}</div>
        <div style="margin-top:8px;display:flex;align-items:center;gap:8px">
          <span class="badge ${{wCls}}">${{data.winner || 'Unknown'}} wins</span>
          <a href="${{ytUrl}}" target="_blank" style="font-size:.75rem">▶ Watch →</a>
        </div>
      </div>`;
  }});
}}
renderGameRecords();

// ── voice analysis table ──────────────────────────────────────────────────────
function renderVoiceAnalysis() {{
  const rows  = DATA.voice_analysis || [];
  const tbody = document.getElementById('voice-body');
  if (!tbody) return;
  tbody.innerHTML = '';
  if (!rows.length) {{
    tbody.innerHTML = `<tr><td colspan="4" class="empty" style="padding:32px">Not enough data yet (need ≥4 games on each team).</td></tr>`;
    return;
  }}
  rows.forEach(r => {{
    const ratio = r.ratio;
    const rc = ratio >= 1.2 ? '#fca5a5' : ratio <= 0.8 ? '#93c5fd' : 'var(--muted)';
    const rl = ratio >= 1.2 ? '↑ more chatty as Evil'
             : ratio <= 0.8 ? '↓ more chatty as Good'
             : '≈ roughly equal';
    tbody.innerHTML +=
      `<tr>
        <td style="font-weight:600">${{r.name}}</td>
        <td style="color:#93c5fd">${{r.good_wpg.toLocaleString()}}</td>
        <td style="color:#fca5a5">${{r.evil_wpg.toLocaleString()}}</td>
        <td>
          <span style="font-weight:700;color:${{rc}}">${{ratio.toFixed(2)}}×</span>
          <span style="font-size:.72rem;color:var(--muted);margin-left:5px">${{rl}}</span>
        </td>
      </tr>`;
  }});
}}
renderVoiceAnalysis();

// ── master re-render ─────────────────────────────────────────────────────────
function renderAll() {{
  renderWins();
  renderStatBubbles();
  renderSuperlatives();
  renderDonut();
  renderBarChart();
  renderFakeRoles();
  renderRoles();
  renderGameRecords();
  renderVoiceAnalysis();
}}

// ── restore saved theme ───────────────────────────────────────────────────────
(function () {{
  if (localStorage.getItem('botc-theme') === 'mc') {{
    document.body.classList.add('mc');
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = '🌙 Classic';
    const sub = document.querySelector('.hero-sub');
    if (sub) sub.textContent = '⛏  Yogscast  ·  Auto-lie detection across every game  ⛏';
    updateChartTheme();
  }}
}})();
</script>
</body>
</html>
"""


def build(db: Path, out: Path, explorer_url: str) -> None:
    """Read db, compute stats, write HTML."""
    if not db.exists():
        print(f"[warn] {db} not found — writing empty-state page.")
        stats    = _empty_stats()
        games: list[dict] = []
    else:
        df       = _load(db)
        stats    = _compute_stats(df)
        games    = _load_recent_games(db)

    # ── shared auxiliary data (not goblin-sensitive) ───────────────────────────
    if db.exists():
        wins         = _load_wins(db)
        wpg          = _load_words_per_game(db)
        wpg_team     = _load_words_per_team(db)
        game_records = _load_game_records(db)
    else:
        wins         = {"good": 0, "evil": 0}
        wpg          = {}
        wpg_team     = {}
        game_records = {"most_lies": None, "most_deceptive": None, "most_claims": None}

    def _augment(s: dict) -> None:
        """Attach wins, WPG, voice-analysis and game records to a stats dict."""
        s["wins"] = wins
        # Override n_games: use videos-table winner count (good+evil) so the
        # total is always consistent with the scoreboard.  The lies-table count
        # is lower because some games have zero detectable role claims.
        s["summary"]["games"] = wins["good"] + wins["evil"]
        for p in s.get("players", []):
            p["wpg"] = round(wpg.get(p["name"], 0.0), 1)
        eligible = [p for p in s.get("players", []) if p["games"] > 5 and p["name"] in wpg]
        if eligible:
            top = max(eligible, key=lambda p: wpg.get(p["name"], 0))
            s["superlatives"]["most_talkative"] = {
                "player_name": top["name"],
                "wpg":         round(wpg[top["name"]], 0),
                "games":       top["games"],
            }
        else:
            s["superlatives"]["most_talkative"] = None
        s["game_records"] = game_records
        for p in s.get("players", []):
            pt = wpg_team.get(p["name"], {})
            p["good_wpg"] = round(pt.get("good", 0.0), 1)
            p["evil_wpg"] = round(pt.get("evil", 0.0), 1)
        voice_rows = []
        for p in s.get("players", []):
            gw, ew = p.get("good_wpg", 0.0), p.get("evil_wpg", 0.0)
            if gw > 0 and ew > 0 and p["games"] >= 4:
                voice_rows.append({
                    "name":     p["name"],
                    "good_wpg": gw,
                    "evil_wpg": ew,
                    "ratio":    round(ew / gw, 2),
                    "games":    p["games"],
                })
        voice_rows.sort(key=lambda x: x["ratio"], reverse=True)
        s["voice_analysis"] = voice_rows

    _augment(stats)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = _HTML.format(
        explorer_url  = explorer_url,
        generated_at  = generated_at,
        data_json     = json.dumps(stats,    ensure_ascii=False, indent=None),
        games_json    = json.dumps(games,    ensure_ascii=False, indent=None),
    )
    out.write_text(html, encoding="utf-8")
    print(f"[OK] Wrote {out}  ({len(html):,} bytes)")


def main() -> None:
    p = argparse.ArgumentParser(description="Generate BotC landing page from botc.db")
    p.add_argument("--db",           default=str(DB_PATH),  help="Path to botc.db")
    p.add_argument("--out",          default=str(OUT_PATH), help="Output HTML path")
    p.add_argument("--explorer-url", default=EXPLORER_URL_DEFAULT,
                   help="URL of the Streamlit explorer (default: http://localhost:8501)")
    p.add_argument("--open", action="store_true", help="Open landing.html in browser after writing")
    args = p.parse_args()

    build(Path(args.db), Path(args.out), args.explorer_url)

    if args.open:
        webbrowser.open(Path(args.out).resolve().as_uri())


if __name__ == "__main__":
    main()
