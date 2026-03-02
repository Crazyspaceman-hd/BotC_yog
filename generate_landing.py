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
from botc_ui import _team

DB_PATH  = Path("botc.db")
OUT_PATH = Path("landing.html")
EXPLORER_URL_DEFAULT = "http://localhost:8501"


# ── data helpers ──────────────────────────────────────────────────────────────

def _load(db: Path) -> pd.DataFrame:
    """Load all lies joined with video titles."""
    con = sqlite3.connect(str(db))
    df = pd.read_sql_query(
        "SELECT l.video_id, v.title AS video_title, v.winner, "
        "l.player_name, l.actual_role, l.verdict "
        "FROM lies l JOIN videos v ON l.video_id = v.id",
        con,
    )
    con.close()
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


def _compute_stats(df: pd.DataFrame) -> dict:
    """Compute all stats needed by the landing page."""
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

    # ── role breakdown (sorted by lie rate, min 3 claims) ─────────────────────
    role_df = (
        df[df["actual_role"].notna() & (df["actual_role"].str.lower() != "unknown")]
        .groupby("actual_role")
        .agg(
            claims =("verdict", "count"),
            lies   =("verdict", lambda x: (x == "LIE").sum()),
            team   =("team",    lambda x: x.mode().iloc[0] if not x.empty else "Good"),
        )
        .reset_index()
    )
    role_df = role_df[role_df["claims"] >= 3].copy()
    role_df["lie_rate"] = role_df["lies"] / role_df["claims"].replace(0, float("nan"))
    role_df = role_df.sort_values("lie_rate", ascending=False)
    roles = role_df.apply(lambda r: {
        "role":     r["actual_role"],
        "team":     r["team"],
        "claims":   int(r["claims"]),
        "lies":     int(r["lies"]),
        "lie_rate": round(float(r["lie_rate"]), 3),
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
            "most_evil":    _clean(most_evil),
            "most_good":    _clean(most_good),
            "biggest_liar": _clean(biggest_liar),
            "most_honest":  _clean(most_honest),
            "traitor":      _clean(traitor),
            "most_games":   _clean(most_games),
        },
        "top_liars": top_liars,
        "roles":     roles,
        "players":   players,
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
                          "most_talkative"]},
        "top_liars": [],
        "roles":     [],
        "players":   [],
    }


# ── HTML template ─────────────────────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
</style>
</head>
<body>

<header>
  <div class="hero-title">🏰 Blood on the Clocktower</div>
  <div class="hero-sub">Yogscast &middot; Automated lie detection across every game</div>

  <div class="wins-board" id="wins-board"></div>
  <div class="wins-bar-wrap" id="wins-bar-wrap">
    <div class="wins-bar-good" id="wbar-good"></div>
    <div class="wins-bar-evil" id="wbar-evil"></div>
  </div>

  <div class="stat-row" id="stat-row"></div>
  <a class="btn-explore" href="{explorer_url}" target="_blank">▶&nbsp; Open Full Explorer</a>
</header>

<main class="container">

  <section>
    <div class="section-title"><span>🏆</span> Superlatives</div>
    <div class="sup-grid" id="sup-grid"></div>
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
    <div class="section-title"><span>🎭</span> Role Breakdown <span style="font-size:.8rem;font-weight:400;margin-left:4px;">(min 3 claims)</span></div>
    <div class="table-card">
      <table id="roles-table">
        <thead>
          <tr>
            <th>Role</th>
            <th>Team</th>
            <th>Claims</th>
            <th>Lies</th>
            <th>Lie rate</th>
          </tr>
        </thead>
        <tbody id="roles-body"></tbody>
      </table>
    </div>
  </section>

  <section>
    <div class="section-title"><span>🎮</span> Recent Games</div>
    <div class="games-grid" id="games-grid"></div>
  </section>

</main>

<footer>
  Generated {generated_at} &middot;
  <a href="{explorer_url}" target="_blank">▶ Open full explorer</a>
</footer>

<script>
const DATA   = {data_json};
const GAMES  = {games_json};
const EXPLORER = "{explorer_url}";

// ── wins scoreboard ────────────────────────────────────────────────────────────
(function () {{
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
}})();

// ── stat bubbles ──────────────────────────────────────────────────────────────
(function () {{
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
  items.forEach(([num, lbl]) => {{
    row.innerHTML +=
      `<div class="stat-bubble"><div class="num">${{num}}</div><div class="lbl">${{lbl}}</div></div>`;
  }});
}})();

// ── superlatives ──────────────────────────────────────────────────────────────
(function () {{
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
  ];

  const grid = document.getElementById("sup-grid");
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
}})();

// ── doughnut chart ────────────────────────────────────────────────────────────
(function () {{
  const s = DATA.summary;
  new Chart(document.getElementById("donut-chart"), {{
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
}})();

// ── horizontal bar chart (top liars) ──────────────────────────────────────────
(function () {{
  const liars = DATA.top_liars;
  if (!liars.length) return;
  const labels = liars.map(x => x.name);
  const lies   = liars.map(x => x.lies);
  const rates  = liars.map(x => x.lie_rate);
  // Color each bar by lie_rate: low = amber, high = red
  const colors = rates.map(r => {{
    const t = Math.min(r, 1);
    const red = Math.round(220 + t * (220 - 220));
    return `rgba(220, ${{Math.round(38 + (1-t)*140)}}, ${{Math.round(38 + (1-t)*100)}}, 0.85)`;
  }});

  new Chart(document.getElementById("bar-chart"), {{
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
}})();

// ── roles table ───────────────────────────────────────────────────────────────
(function () {{
  const tbody = document.getElementById("roles-body");
  DATA.roles.forEach(r => {{
    const pct = (r.lie_rate * 100).toFixed(0);
    tbody.innerHTML +=
      `<tr>
        <td style="font-weight:600">${{r.role}}</td>
        <td><span class="team-${{r.team.toLowerCase()}}">${{r.team}}</span></td>
        <td>${{r.claims}}</td>
        <td>${{r.lies}}</td>
        <td>
          <div class="rate-bar-wrap">
            <div class="rate-bar"><div class="rate-bar-fill" style="width:${{pct}}%"></div></div>
            <span style="font-size:.8rem;min-width:34px">${{pct}}%</span>
          </div>
        </td>
      </tr>`;
  }});
  if (!DATA.roles.length) {{
    tbody.innerHTML = `<tr><td colspan="5" class="empty">No role data yet.</td></tr>`;
  }}
}})();

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
</script>
</body>
</html>
"""


def build(db: Path, out: Path, explorer_url: str) -> None:
    """Read db, compute stats, write HTML."""
    if not db.exists():
        print(f"[warn] {db} not found — writing empty-state page.")
        stats = _empty_stats()
        games: list[dict] = []
    else:
        df    = _load(db)
        stats = _compute_stats(df)
        games = _load_recent_games(db)

    # ── augment stats with wins + words-per-game ──────────────────────────────
    if db.exists():
        wins = _load_wins(db)
        wpg  = _load_words_per_game(db)
    else:
        wins = {"good": 0, "evil": 0}
        wpg  = {}

    stats["wins"] = wins

    # Attach per-player avg words/game
    for p in stats.get("players", []):
        p["wpg"] = round(wpg.get(p["name"], 0.0), 1)

    # Most-talkative superlative (players with >5 games only)
    eligible = [p for p in stats.get("players", []) if p["games"] > 5 and p["name"] in wpg]
    if eligible:
        top = max(eligible, key=lambda p: wpg.get(p["name"], 0))
        stats["superlatives"]["most_talkative"] = {
            "player_name": top["name"],
            "wpg":         round(wpg[top["name"]], 0),
            "games":       top["games"],
        }
    else:
        stats["superlatives"]["most_talkative"] = None

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = _HTML.format(
        explorer_url  = explorer_url,
        generated_at  = generated_at,
        data_json     = json.dumps(stats,  ensure_ascii=False, indent=None),
        games_json    = json.dumps(games,  ensure_ascii=False, indent=None),
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
