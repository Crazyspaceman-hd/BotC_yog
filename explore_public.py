"""explore_public.py - Read-only BotC transcript viewer (safe for GitHub Pages).

This is the publishing-safe version of explore.py.
  - All data-editing, saving, and re-analysis features are REMOVED.
  - No subprocess calls; no file writes.
  - Safe to deploy as a public Streamlit app (streamlit.io, GitHub Codespaces, etc.)

Usage:
    streamlit run explore_public.py
"""

import json
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from botc_ui import (
    _SPK_PALETTE, VERDICT_BG, VERDICT_ICON, _STATUS_BG,
    _SOURCE_STYLE, _EVIL_ROLES, _team,
    _REDS_HIGH, _BLUES_HIGH,
)

DEFAULT_VIDEO_ID = "lF96Jd3Eaeg"
DB_PATH = Path("botc.db")

# Dark text for light/pastel backgrounds (used in all fixed-colour style fns)
_DARK_TXT   = "color: #111"
# White + shadow text only for dark/saturated gradient cells
_LIGHT_TXT  = "color: white; text-shadow: 0 0 3px #000, 0 0 3px #000"


def _txt_for_bg(r: int, g: int, b: int) -> str:
    """Return a legible CSS text-color for the given RGB background.

    Uses the standard luminance formula: dark text on light backgrounds,
    white (+ shadow) text on dark/saturated backgrounds.
    """
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return _DARK_TXT if lum > 140 else _LIGHT_TXT


def _color_scale(s, high=_REDS_HIGH):
    """Gradient from white → high colour; no matplotlib required.

    Text colour switches automatically so it stays legible across the range.
    """
    mn, mx = s.min(), s.max()
    def _cell(v):
        t = (v - mn) / (mx - mn) if mx > mn else 0.0
        r = int(255 + t * (high[0] - 255))
        g = int(255 + t * (high[1] - 255))
        b = int(255 + t * (high[2] - 255))
        return f"background-color: rgb({r},{g},{b}); {_txt_for_bg(r, g, b)}"
    return s.apply(_cell)


# ── playlist helpers ──────────────────────────────────────────────────────────

def load_playlist() -> list[dict]:
    p = Path("playlist.json")
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("entries", [])
    except Exception:
        return []


# ── data helpers ──────────────────────────────────────────────────────────────

@st.cache_data
def _load_segments(video_id: str) -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame(columns=["start", "end", "speaker", "text"])
    con = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        "SELECT start, end, speaker, text FROM segments "
        "WHERE video_id=? ORDER BY start",
        con, params=(video_id,),
    )
    con.close()
    return df if not df.empty else pd.DataFrame(columns=["start", "end", "speaker", "text"])


@st.cache_data
def _load_lies(video_id: str) -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    con = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        "SELECT timestamp, speaker, player_name, team, actual_role, "
        "believed_role, claimed_role, verdict, text "
        "FROM lies WHERE video_id=?",
        con, params=(video_id,),
    )
    con.close()
    return df


def _load_overrides(video_id: str) -> dict:
    f = Path(f"outputs/{video_id}") / "roster_overrides.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {}


@st.cache_data
def _load_speaker_map(video_id: str) -> dict:
    """Load manual speaker-to-player assignments from the DB (speaker_map table).

    Returns {speaker_id: {name, actual_role, believed_role}}.
    Falls back to empty dict if the table doesn't exist yet.
    """
    if not DB_PATH.exists():
        return {}
    try:
        con = sqlite3.connect(str(DB_PATH))
        rows = con.execute(
            "SELECT speaker, name, actual_role, believed_role "
            "FROM speaker_map WHERE video_id=?",
            (video_id,),
        ).fetchall()
        con.close()
        return {
            r[0]: {"name": r[1], "actual_role": r[2], "believed_role": r[3]}
            for r in rows if r[1]
        }
    except Exception:
        return {}


@st.cache_data
def _load_intro_roster(video_id: str) -> dict:
    """Load scraped intro roster from the database."""
    if not DB_PATH.exists():
        return {}
    con = sqlite3.connect(str(DB_PATH))
    rows = con.execute(
        "SELECT player_name, actual_role, believed_role, frame_time "
        "FROM roster WHERE video_id=? ORDER BY frame_time",
        (video_id,),
    ).fetchall()
    con.close()
    if not rows:
        return {}
    return {
        "players": [
            {
                "name":          r[0],
                "actual_role":   r[1],
                "believed_role": r[2],
                "frame_time":    r[3],
            }
            for r in rows
        ]
    }


@st.cache_data
def _load_rttm(video_id: str) -> list[tuple[float, float, str]]:
    rttm_file = Path(f"outputs/{video_id}") / "diarization.rttm"
    if not rttm_file.exists():
        return []
    turns = []
    for line in rttm_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        p = line.split()
        if p[0] != "SPEAKER":
            continue
        turns.append((float(p[3]), float(p[3]) + float(p[4]), p[7]))
    return turns


def detect_conversations(
    turns:    list[tuple[float, float, str]],
    window:   float = 30.0,
    min_dur:  float = 20.0,
) -> list[dict]:
    if not turns:
        return []
    max_t  = max(e for _, e, _ in turns)
    blocks = []
    t = 0.0
    while t < max_t:
        wend   = min(t + window, max_t)
        active = {
            spk for s, e, spk in turns
            if max(0.0, min(e, wend) - max(s, t)) > 1.5
        }
        non_st = active - {"speaker_0"}
        if not non_st:
            ctype = "Narration"
        elif len(non_st) <= 2:
            ctype = "Private"
        elif len(non_st) <= 4:
            ctype = "Small group"
        else:
            ctype = "Town"
        blocks.append({
            "start": t, "end": wend,
            "participants": sorted(non_st), "type": ctype,
        })
        t += window

    merged: list[dict] = []
    for b in blocks:
        if merged and merged[-1]["participants"] == b["participants"]:
            merged[-1]["end"] = b["end"]
        else:
            merged.append(dict(b))
    return [b for b in merged if b["end"] - b["start"] >= min_dur]


def _load_vocab(fn: str) -> list[str]:
    p = Path(fn)
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


@st.cache_data
def _load_all_lies() -> pd.DataFrame:
    """Load all lie analysis data from the database."""
    if not DB_PATH.exists():
        return pd.DataFrame()
    con = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        "SELECT l.video_id, v.title AS video_title, "
        "l.timestamp, l.speaker, l.player_name, l.team, "
        "l.actual_role, l.believed_role, l.claimed_role, l.verdict, l.text "
        "FROM lies l JOIN videos v ON l.video_id = v.id",
        con,
    )
    con.close()
    return df


def _fmt_ts(sec: float) -> str:
    m, s = int(sec // 60), sec % 60
    return f"{m:02d}:{s:05.2f}"


# ── page setup ────────────────────────────────────────────────────────────────

st.set_page_config(page_title="BotC Viewer", layout="wide", page_icon="🏰")

# Read-only banner
st.markdown(
    '<div style="background:#dbeafe; color:#1e3a8a; padding:8px 16px; '
    'border-radius:6px; margin-bottom:12px; font-size:13px;">'
    '👁️ <strong>Read-Only View</strong> — data browsing only. '
    'To edit rosters or re-run analysis use <code>explore.py</code>.'
    '</div>',
    unsafe_allow_html=True,
)

st.title("🏰  BotC Transcript Viewer")


# ── game selector (sidebar) ───────────────────────────────────────────────────

with st.sidebar:
    playlist = load_playlist()

    if playlist:
        game_options = {
            f"{e['title']} ({e['id']})": e["id"]
            for e in playlist
        }
        ids         = [e["id"] for e in playlist]
        default_idx = ids.index(DEFAULT_VIDEO_ID) if DEFAULT_VIDEO_ID in ids else 0

        sel = st.selectbox(
            "Game",
            list(game_options.keys()),
            index=default_idx,
            key="game_sel",
        )
        video_id = game_options[sel]

        entry   = next((e for e in playlist if e["id"] == video_id), {})
        status  = entry.get("status", "unknown")
        bg      = _STATUS_BG.get(status, "#e5e7eb")
        is_mbr  = entry.get("members", False)
        mbr_html = (
            ' &nbsp;<span style="background:#7c3aed; color:#fff; '
            'padding:2px 8px; border-radius:4px; font-size:12px;">🔒 Members</span>'
            if is_mbr else ""
        )
        st.markdown(
            f'Pipeline status: <span style="background:{bg}; color:#111; '
            f'padding:2px 8px; border-radius:4px; font-size:13px;">{status}</span>'
            f'{mbr_html}',
            unsafe_allow_html=True,
        )
    else:
        video_id = DEFAULT_VIDEO_ID
        st.info("No `playlist.json` — using default game.")

    st.divider()


# ── load data ─────────────────────────────────────────────────────────────────

segs         = _load_segments(video_id)
lies         = _load_lies(video_id)
overrides    = _load_overrides(video_id)
speaker_map  = _load_speaker_map(video_id)
intro_data   = _load_intro_roster(video_id)

_roles_file = Path(f"outputs/{video_id}/roles.txt")
if not _roles_file.exists():
    _roles_file = Path("roles.txt")
roles_lc = {
    r.lower(): r
    for r in _load_vocab(str(_roles_file))
}

all_spks  = sorted(segs["speaker"].unique().tolist()) if not segs.empty else []
spk_color = {s: _SPK_PALETTE[i % len(_SPK_PALETTE)] for i, s in enumerate(all_spks)}

# Storyteller gets a distinct neutral colour
if "speaker_0" in spk_color:
    spk_color["speaker_0"] = "#f0f0f0"


# ── sidebar data stats ────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Data")
    st.markdown(f"**Video:** [{video_id}](https://www.youtube.com/watch?v={video_id})")
    st.write(f"**Segments:** {len(segs):,}")
    if not lies.empty:
        st.write(f"**Role claims:** {len(lies)}")
        vc = lies["verdict"].value_counts()
        for v in ("LIE", "HONEST MISTAKE", "TRUE", "UNVERIFIED"):
            n = vc.get(v, 0)
            if n:
                st.write(f"  - {VERDICT_ICON.get(v, '')} {v}: **{n}**")
    if intro_data:
        n_scraped = len(intro_data.get("players", []))
        st.write(f"**Scraped intro:** {n_scraped} player(s) 📷")
    _n_manual = len(overrides) or len(speaker_map)
    st.write(f"**Manual assignments:** {_n_manual}")
    st.divider()
    st.caption(
        "To fix rosters or re-run analysis, use **explore.py** "
        "(the full editor app)."
    )


# ── speaker label map ─────────────────────────────────────────────────────────

def _spk_label(spk: str) -> str:
    if spk == "speaker_0":
        return "Storyteller"
    ov = overrides.get(spk) or speaker_map.get(spk)
    if ov and ov.get("name"):
        role = ov.get("actual_role", "")
        return f"{ov['name']}" + (f"  ({role})" if role else "")
    if not lies.empty:
        rows = lies[lies["speaker"] == spk]
        if not rows.empty and rows.iloc[0]["player_name"] != spk:
            r    = rows.iloc[0]
            role = r.get("actual_role", "")
            return f"{r['player_name']}" + (f"  ({role})" if role else "")
    return spk


spk_labels = {s: _spk_label(s) for s in all_spks}


# ══════════════════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════════════════

tab_home, tab_roster, tab_tx, tab_lies_tab, tab_stats, tab_aggregate = st.tabs(
    ["🏠  Home", "🎭  Roster", "📜  Transcript", "🔍  Lie Analysis", "📊  Stats", "🌐  All Games"]
)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 0 — HOME  (landing page with aggregate superlatives)
# ══════════════════════════════════════════════════════════════════════════════

with tab_home:
    _all = _load_all_lies()

    st.markdown(
        '<h1 style="text-align:center; margin-bottom:4px;">🏰 Blood on the Clocktower</h1>'
        '<p style="text-align:center; color:#666; font-size:1.1em; margin-top:0;">'
        'Yogscast · Automated lie detection across every game</p>',
        unsafe_allow_html=True,
    )
    st.divider()

    if _all.empty:
        st.info("No analyzed games yet. Run `analyze_roles.py` on some videos first.")
    else:
        _all["team"] = _all["actual_role"].map(_team)

        # ── Top-level numbers ─────────────────────────────────────────────────
        _n_games   = int(_all["video_id"].nunique())
        _n_players = int(_all["player_name"].nunique())
        _n_claims  = len(_all)
        _n_lies    = int((_all["verdict"] == "LIE").sum())
        _n_true    = int((_all["verdict"] == "TRUE").sum())

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("🎮 Games",        _n_games)
        c2.metric("🧑 Players",      _n_players)
        c3.metric("💬 Role claims",  _n_claims)
        c4.metric("✗ Lies told",     _n_lies)
        c5.metric("✓ Honest claims", _n_true)

        st.divider()

        # ── Superlatives row ──────────────────────────────────────────────────
        st.subheader("🏆 Superlatives")

        def _stat_card(col, icon: str, title: str, name: str, detail: str,
                       bg: str = "#f8fafc") -> None:
            col.markdown(
                f'<div style="background:{bg}; border-radius:10px; padding:14px 16px; '
                f'height:130px;">'
                f'<div style="font-size:1.6em;">{icon}</div>'
                f'<div style="font-size:0.75em; color:#666; text-transform:uppercase; '
                f'letter-spacing:0.05em;">{title}</div>'
                f'<div style="font-size:1.15em; font-weight:700; margin:2px 0;">{name}</div>'
                f'<div style="font-size:0.85em; color:#555;">{detail}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Per-player aggregates — filter out unlinked speaker_X placeholders and Storyteller
        _named = _all[
            _all["player_name"].notna() &
            ~_all["player_name"].str.match(r"^speaker_\d+$", na=False) &
            (_all["player_name"] != "Storyteller")
        ]

        # Count evil/good games (one row per player×game, not per claim)
        _game_teams = _named.drop_duplicates(subset=["player_name", "video_id"])[
            ["player_name", "video_id", "team"]
        ]
        _pa_games = (
            _game_teams.groupby("player_name")
            .agg(
                games  =("video_id", "nunique"),
                evil_g =("team",     lambda x: (x == "Evil").sum()),
                good_g =("team",     lambda x: (x == "Good").sum()),
            )
            .reset_index()
        )

        # Count claims / lies / true from all rows
        _pa_claims = (
            _named.groupby("player_name")
            .agg(
                claims =("verdict", "count"),
                lies   =("verdict", lambda x: (x == "LIE").sum()),
                true_  =("verdict", lambda x: (x == "TRUE").sum()),
            )
            .reset_index()
        )

        _pa = _pa_games.merge(_pa_claims, on="player_name", how="left")
        _pa["lie_rate"]  = _pa["lies"]   / _pa["claims"].replace(0, float("nan"))
        _pa["evil_rate"] = _pa["evil_g"] / _pa["games"].replace(0, float("nan"))
        _pa["good_rate"] = _pa["good_g"] / _pa["games"].replace(0, float("nan"))

        # Superlative selectors (min 2 claims / 2 games for rate stats)
        _qual = _pa[_pa["claims"] >= 2]
        _multi = _pa[_pa["games"] >= 2]

        def _top(df, col, asc=False):
            s = df.sort_values(col, ascending=asc)
            return s.iloc[0] if not s.empty else None

        _most_evil  = _top(_multi, "evil_rate")
        _most_good  = _top(_multi, "good_rate")
        _biggest_liar = _top(_qual, "lie_rate")
        _most_honest  = _top(_qual[_qual["lie_rate"] >= 0], "lie_rate", asc=True)
        # "Lied to teammates" = Good player with most LIE verdicts (betrayed own town)
        _good_lies = (
            _named[(_named["team"] == "Good") & (_named["verdict"] == "LIE")]
            .groupby("player_name")
            .size()
            .reset_index(name="good_lies")
            .sort_values("good_lies", ascending=False)
        )
        _traitor = _good_lies.iloc[0] if not _good_lies.empty else None

        row1 = st.columns(3)
        row2 = st.columns(3)

        if _most_evil is not None:
            _stat_card(row1[0], "😈", "Most often Evil",
                       _most_evil["player_name"],
                       f"Evil in {_most_evil['evil_g']}/{_most_evil['games']} games "
                       f"({_most_evil['evil_rate']:.0%})",
                       "#ffe4e6")
        if _most_good is not None:
            _stat_card(row1[1], "😇", "Most often Good",
                       _most_good["player_name"],
                       f"Good in {_most_good['good_g']}/{_most_good['games']} games "
                       f"({_most_good['good_rate']:.0%})",
                       "#dcfce7")
        if _biggest_liar is not None:
            _stat_card(row1[2], "🤥", "Biggest Liar",
                       _biggest_liar["player_name"],
                       f"{_biggest_liar['lies']} lies · "
                       f"{_biggest_liar['lie_rate']:.0%} lie rate",
                       "#fde8d0")
        if _most_honest is not None:
            _stat_card(row2[0], "🌟", "Most Honest",
                       _most_honest["player_name"],
                       f"{_most_honest['true_']} true · "
                       f"{_most_honest['lie_rate']:.0%} lie rate",
                       "#e0f2fe")
        if _traitor is not None:
            _stat_card(row2[1], "🗡️", "Lied to Their Town",
                       _traitor["player_name"],
                       f"{_traitor['good_lies']} lies while playing Good",
                       "#fef9c3")

        # Most appearances
        _appearances = _pa.sort_values("games", ascending=False)
        if not _appearances.empty:
            _top_app = _appearances.iloc[0]
            _stat_card(row2[2], "🎭", "Most Games Played",
                       _top_app["player_name"],
                       f"Appeared in {_top_app['games']} / {_n_games} games",
                       "#ede9fe")

        st.divider()

        # ── Player appearances breakdown ──────────────────────────────────────
        st.subheader("🎮 Player appearances")

        _app_df = (
            _pa[["player_name", "games", "evil_g", "good_g", "lies", "lie_rate"]]
            .sort_values("games", ascending=False)
            .rename(columns={
                "player_name": "Player",
                "games":       "Games",
                "evil_g":      "As Evil",
                "good_g":      "As Good",
                "lies":        "Total Lies",
            })
        )
        _app_df["Lie rate"] = _app_df["lie_rate"].map(
            lambda v: f"{v:.0%}" if pd.notna(v) else "—"
        ).values
        _app_df = _app_df.drop(columns="lie_rate")

        st.bar_chart(_app_df.set_index("Player")["Games"])

        def _style_app(row):
            if row["As Evil"] > row["As Good"]:
                return [f"background-color:#ffe4e6; {_DARK_TXT}"] * len(row)
            elif row["As Good"] > row["As Evil"]:
                return [f"background-color:#dcfce7; {_DARK_TXT}"] * len(row)
            return [""] * len(row)

        st.dataframe(
            _app_df.style.apply(_style_app, axis=1),
            use_container_width=True,
            hide_index=True,
        )

        st.divider()

        # ── Recent games ──────────────────────────────────────────────────────
        st.subheader("📺 Games in this dataset")
        for e in (playlist if playlist else []):
            vid  = e.get("id", "")
            title = e.get("title", vid)
            status = e.get("status", "")
            bg = _STATUS_BG.get(status, "#e5e7eb")
            mbr_chip = (
                '<span style="background:#7c3aed; color:#fff; padding:2px 6px; '
                'border-radius:4px; font-size:11px; white-space:nowrap;">🔒 Members</span>'
                if e.get("members") else ""
            )
            st.markdown(
                f'<div style="display:flex; align-items:center; gap:10px; '
                f'padding:6px 0; border-bottom:1px solid #eee;">'
                f'<span style="background:{bg}; color:#111; padding:2px 7px; '
                f'border-radius:4px; font-size:12px; white-space:nowrap;">{status}</span>'
                f'{mbr_chip}'
                f'<a href="https://www.youtube.com/watch?v={vid}" target="_blank" '
                f'style="color:#1d4ed8; text-decoration:none;">{title}</a>'
                f'</div>',
                unsafe_allow_html=True,
            )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — ROSTER  (read-only)
# ══════════════════════════════════════════════════════════════════════════════

with tab_roster:
    st.markdown(
        "Player roster — all data from the database.  "
        "**🔴 Red** = unlinked speaker &nbsp; **🟡 Yellow** = auto-detected &nbsp; "
        "**🟢 Green** = manually assigned &nbsp; **📷 Blue** = scraped from video intro"
    )

    # Build a unified player-centric roster from DB tables only.
    # Priority: roster (ground-truth roles) → speaker_map (manual linking) → lies (auto)
    players: dict[str, dict] = {}

    # 1. Seed from roster DB table (scraped video intro — ground truth for names + roles)
    for p in intro_data.get("players", []):
        name_raw = (p.get("name") or "").strip()
        key      = name_raw.lower()
        if key:
            players[key] = {
                "name":          name_raw,
                "believed_role": (p.get("believed_role") or "").lower(),
                "actual_role":   (p.get("actual_role")   or "").lower(),
                "speaker":       "",
                "source":        "scraped",
            }

    # 2. Link speakers to players via speaker_map (manual DB assignments)
    for spk, sm in speaker_map.items():
        name_raw = (sm.get("name") or "").strip()
        key      = name_raw.lower()
        if not key:
            continue
        if key in players:
            players[key]["speaker"] = spk
            players[key]["source"]  = "manual"
            # Fill roles from speaker_map only if roster didn't supply them
            if not players[key]["believed_role"]:
                players[key]["believed_role"] = (sm.get("believed_role") or "").lower()
            if not players[key]["actual_role"]:
                players[key]["actual_role"] = (sm.get("actual_role") or "").lower()
        else:
            # In speaker_map but not in scraped roster — add as manual entry
            players[key] = {
                "name":          name_raw,
                "believed_role": (sm.get("believed_role") or "").lower(),
                "actual_role":   (sm.get("actual_role")   or "").lower(),
                "speaker":       spk,
                "source":        "manual",
            }

    # 3. Link remaining speakers via lies auto-detection
    if not lies.empty:
        for _, r in lies.drop_duplicates("speaker").iterrows():
            spk  = r["speaker"]
            name = r["player_name"] if r["player_name"] != r["speaker"] else ""
            if not name:
                continue
            key = name.lower()
            if key in players:
                # Roster entry exists — just fill in the speaker link if missing
                if not players[key]["speaker"]:
                    players[key]["speaker"] = spk
            else:
                # Auto-detected player not in roster or speaker_map
                players[key] = {
                    "name":          name,
                    "believed_role": str(r.get("believed_role") or "").lower(),
                    "actual_role":   str(r.get("actual_role")   or "").lower(),
                    "speaker":       spk,
                    "source":        "auto",
                }

    # 4. Add Storyteller
    players["_storyteller"] = {
        "name": "Storyteller", "believed_role": "storyteller",
        "actual_role": "storyteller", "speaker": "speaker_0", "source": "system",
    }

    # 5. Collect unlinked speakers (in transcript but no player matched)
    linked_spks = {v["speaker"] for v in players.values() if v["speaker"]}
    unlinked_spks = [spk for spk in all_spks if spk not in linked_spks]

    # Sort helper: system (speaker_0) first, linked players by speaker number,
    # unlinked scraped players alphabetically at the end
    def _player_sort(v: dict) -> tuple:
        if v["source"] == "system":
            return (0, -1, "")
        spk = v.get("speaker", "")
        try:
            n = int(spk.split("_")[1])
        except Exception:
            n = 999
        return (1 if spk else 2, n, v["name"].lower())

    # Only named players go in the main table
    named_players = [v for v in players.values() if v.get("name")]

    df_ros = pd.DataFrame([
        {
            "speaker":       v["speaker"] or "—",
            "name":          v["name"],
            "believed_role": v["believed_role"],
            "actual_role":   v["actual_role"],
            "deceived":      (
                "⚠️ Yes"
                if (v["actual_role"] and v["believed_role"]
                    and v["actual_role"] != v["believed_role"]
                    and v["source"] != "system")
                else ""
            ),
            "source":        v["source"],
        }
        for v in sorted(named_players, key=_player_sort)
    ])[["speaker", "name", "believed_role", "actual_role", "deceived", "source"]]

    def _style_src(row):
        src = row["source"]
        if src == "system":
            bg = "#f0f0f0"
        elif src == "manual":
            bg = "#dcfce7"
        elif src == "scraped":
            bg = "#e0f2fe"
        elif src in ("auto", "auto_timing"):
            bg = "#fef9c3"
        else:
            bg = "#ffe4e6"
        return [f"background-color: {bg}; {_DARK_TXT}"] * len(row)

    # Source legend
    st.markdown(
        " &nbsp; ".join(
            f'<span style="background:{v[1]}; color:#111; padding:2px 8px; '
            f'border-radius:4px; font-size:12px;">{v[0]} {k}</span>'
            for k, v in _SOURCE_STYLE.items()
        ) + ' &nbsp; <span style="background:#f0f0f0; color:#111; padding:2px 8px; '
            'border-radius:4px; font-size:12px;">🎭 system</span>',
        unsafe_allow_html=True,
    )
    st.write("")

    st.dataframe(
        df_ros.style.apply(_style_src, axis=1),
        use_container_width=True,
        hide_index=True,
    )

    # Unlinked speakers — collapsed, secondary info
    if unlinked_spks:
        with st.expander(
            f"⚠️ {len(unlinked_spks)} speaker(s) not yet identified "
            f"({', '.join(unlinked_spks)}) — fix in explore.py"
        ):
            st.caption(
                "These transcript speakers have no player assigned. "
                "Open **explore.py → Fix Rosters** tab to link them."
            )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — TRANSCRIPT
# ══════════════════════════════════════════════════════════════════════════════

with tab_tx:
    f1, f2, f3 = st.columns([2, 2, 3])

    spk_sel = f1.multiselect(
        "Speaker",
        options=all_spks,
        format_func=lambda s: f"{s}  [{spk_labels.get(s, s)}]",
    )
    dur     = float(segs["end"].max()) if not segs.empty else 600.0
    t_range = f2.slider(
        "Time range (min)", 0.0, dur / 60, (0.0, dur / 60), step=0.5,
    )
    search  = f3.text_input("Search text", placeholder="search transcript…")

    view = segs.copy()
    if spk_sel:
        view = view[view["speaker"].isin(spk_sel)]
    view = view[
        (view["start"] / 60 >= t_range[0]) & (view["start"] / 60 <= t_range[1])
    ]
    if search:
        view = view[view["text"].str.contains(search, case=False, na=False)]

    st.caption(f"Showing **{len(view):,}** of {len(segs):,} segments")

    PAGE    = 60
    n_pages = max(1, (len(view) - 1) // PAGE + 1)
    page_no = int(
        st.number_input("Page", min_value=1, max_value=n_pages,
                        value=1, step=1, key="tx_page")
    ) - 1
    chunk = view.iloc[page_no * PAGE: (page_no + 1) * PAGE]

    legend_parts = []
    for spk in chunk["speaker"].unique():
        col = spk_color.get(spk, "#eee")
        lbl = spk_labels.get(spk, spk)
        legend_parts.append(
            f'<span style="background:{col}; padding:2px 8px; border-radius:4px; '
            f'margin-right:6px; font-size:12px;">{lbl}</span>'
        )
    st.markdown(
        '<div style="margin-bottom:8px;">' + "".join(legend_parts) + "</div>",
        unsafe_allow_html=True,
    )

    parts    = ['<div style="font-family:system-ui,sans-serif; font-size:14px; '
                'line-height:1.5;">']
    prev_spk = None
    for _, row in chunk.iterrows():
        spk    = row["speaker"]
        col    = spk_color.get(spk, "#f5f5f5")
        lbl    = spk_labels.get(spk, spk)
        ts     = _fmt_ts(row["start"])
        txt    = (str(row["text"])
                  .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        indent = "0" if spk == "speaker_0" else "20px"

        if spk != prev_spk:
            parts.append(
                f'<div style="margin:10px 0 2px {indent}; font-size:11px; '
                f'color:#555; font-weight:700;">{lbl}&ensp;&middot;&ensp;{ts}</div>'
            )
        else:
            parts.append(
                f'<div style="margin-left:{indent}; font-size:10px; '
                f'color:#aaa; margin-bottom:1px;">{ts}</div>'
            )
        parts.append(
            f'<div style="background:{col}; color:#111; margin:1px 0 1px {indent}; '
            f'padding:5px 10px; border-radius:8px; '
            f'border-left:3px solid rgba(0,0,0,0.08);">{txt}</div>'
        )
        prev_spk = spk

    parts.append("</div>")
    st.html("".join(parts))


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — LIE ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

with tab_lies_tab:
    if lies.empty:
        st.warning("No lie analysis data found for this game — run `analyze_roles.py` first.")
        st.stop()

    vc = lies["verdict"].value_counts()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("TRUE",           vc.get("TRUE",           0))
    m2.metric("HONEST MISTAKE", vc.get("HONEST MISTAKE", 0))
    m3.metric("LIE",            vc.get("LIE",            0))
    m4.metric("UNVERIFIED",     vc.get("UNVERIFIED",     0))

    st.divider()

    f1, f2 = st.columns(2)
    p_sel  = f1.multiselect("Player", sorted(lies["player_name"].unique()))
    v_sel  = f2.multiselect(
        "Verdict",
        options=sorted(lies["verdict"].unique()),
        default=sorted(lies["verdict"].unique()),
    )

    view = lies.copy()
    if p_sel:
        view = view[view["player_name"].isin(p_sel)]
    if v_sel:
        view = view[view["verdict"].isin(v_sel)]

    st.caption(f"Showing **{len(view)}** of {len(lies)} claims")

    display_cols = [
        "timestamp", "player_name", "actual_role",
        "believed_role", "claimed_role", "verdict",
    ]

    def _color_verdict(val: str) -> str:
        return f"background-color: {VERDICT_BG.get(str(val), '#fff')}; {_DARK_TXT}"

    st.dataframe(
        view[display_cols].style.map(_color_verdict, subset=["verdict"]),
        use_container_width=True,
        hide_index=True,
        height=360,
    )

    with st.expander("Show full text for filtered claims"):
        for _, row in view.iterrows():
            bg   = VERDICT_BG.get(row["verdict"], "#fff")
            icon = VERDICT_ICON.get(row["verdict"], "?")
            st.markdown(
                f'<div style="background:{bg}; padding:8px 12px; '
                f'border-radius:6px; margin:4px 0;">'
                f'<strong>{row["timestamp"]}</strong> &nbsp; '
                f'<em>{row["player_name"]}</em> claims '
                f'<strong>{row["claimed_role"]}</strong> &nbsp; {icon} '
                f'<span style="font-weight:700">{row["verdict"]}</span><br>'
                f'<span style="color:#555; font-size:0.9em;">'
                f'{str(row["text"])[:300]}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.divider()
    st.subheader("Per-player breakdown")

    for player in sorted(lies["player_name"].unique()):
        p_rows = lies[lies["player_name"] == player]
        pc     = p_rows["verdict"].value_counts()
        n_lie  = pc.get("LIE",            0)
        n_hm   = pc.get("HONEST MISTAKE", 0)
        n_true = pc.get("TRUE",           0)
        icon   = "🔴" if n_lie else ("🟡" if n_hm else "🟢")

        with st.expander(
            f"{icon}  {player}  —  {len(p_rows)} claim(s)  "
            f"[ {n_lie} lies · {n_true} true · {n_hm} honest mistakes ]"
        ):
            for _, row in p_rows.iterrows():
                bg   = VERDICT_BG.get(row["verdict"], "#fff")
                icon2 = VERDICT_ICON.get(row["verdict"], "?")
                st.markdown(
                    f'<div style="background:{bg}; padding:8px 12px; '
                    f'border-radius:6px; margin:4px 0;">'
                    f'<strong>{row["timestamp"]}</strong>'
                    f'&ensp;claimed <em>{row["claimed_role"]}</em>'
                    f'&ensp;{icon2} <strong>{row["verdict"]}</strong><br>'
                    f'<span style="color:#555; font-size:0.9em;">'
                    f'{str(row["text"])[:300]}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — STATS
# ══════════════════════════════════════════════════════════════════════════════

with tab_stats:
    if segs.empty:
        st.warning("No segment data found.")
        st.stop()

    def _player_for(spk: str) -> str:
        if spk == "speaker_0":
            return "Storyteller"
        ov = overrides.get(spk) or speaker_map.get(spk)
        if ov and ov.get("name"):
            return ov["name"]
        if not lies.empty:
            rows = lies[lies["speaker"] == spk]
            if not rows.empty and rows.iloc[0]["player_name"] != spk:
                return rows.iloc[0]["player_name"]
        return spk

    df_w = segs.copy()
    df_w["player"]     = df_w["speaker"].map(_player_for)
    df_w["word_count"] = df_w["text"].str.split().str.len().fillna(0).astype(int)
    df_w["duration"]   = (df_w["end"] - df_w["start"]).round(1)

    agg = (
        df_w.groupby("player")
        .agg(
            words    =("word_count", "sum"),
            segments =("word_count", "count"),
            minutes  =("duration",   "sum"),
        )
        .reset_index()
        .sort_values("words", ascending=False)
    )
    agg["minutes"]       = (agg["minutes"] / 60).round(1)
    agg["words_per_min"] = (
        agg["words"] / agg["minutes"].replace(0, float("nan"))
    ).round(1)

    st.subheader("Words spoken per player")
    st.bar_chart(agg.set_index("player")["words"])
    st.divider()

    st.dataframe(
        agg.rename(columns={
            "player":       "Player",
            "words":        "Total words",
            "segments":     "Segments",
            "minutes":      "Speaking time (min)",
            "words_per_min": "Words / min",
        }).style.apply(_color_scale, subset=["Total words"], high=_BLUES_HIGH),
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    st.subheader("Speaking time over the game")
    st.caption("Each bar = words spoken in a 5-minute window.")

    df_game   = df_w[df_w["speaker"] != "speaker_0"].copy()
    df_game["bucket"] = ((df_game["start"] // 300) * 5).astype(int)
    pivot = (
        df_game.groupby(["bucket", "player"])["word_count"]
        .sum()
        .reset_index()
        .pivot(index="bucket", columns="player", values="word_count")
        .fillna(0)
    )
    pivot.index = pivot.index.map(lambda m: f"{m}m")
    st.bar_chart(pivot)

    st.divider()
    st.subheader("Conversation breakdown")
    st.caption(
        "Detected by sliding a 30-second window across the diarization timeline."
    )

    TYPE_COLORS = {
        "Town":        "#fef9c3",
        "Small group": "#dcfce7",
        "Private":     "#dbeafe",
        "Narration":   "#e5e7eb",
    }

    rttm_turns = _load_rttm(video_id)
    convs      = detect_conversations(rttm_turns)

    if not convs:
        st.info("No RTTM file found — run `diarize_nemo.py` first.")
    else:
        type_counts: dict[str, int] = {}
        for c in convs:
            type_counts[c["type"]] = type_counts.get(c["type"], 0) + 1
        chips = "  &nbsp;|&nbsp;  ".join(
            f'<span style="background:{TYPE_COLORS.get(t, "#eee")}; color:#111; '
            f'padding:2px 8px; border-radius:4px;">{t}: {n}</span>'
            for t, n in sorted(type_counts.items())
        )
        st.markdown(chips, unsafe_allow_html=True)
        st.write("")

        conv_rows = []
        for c in convs:
            names = [spk_labels.get(p, p) for p in c["participants"]]
            mask  = (df_w["start"] >= c["start"]) & (df_w["start"] < c["end"])
            conv_rows.append({
                "Start":        _fmt_ts(c["start"]),
                "End":          _fmt_ts(c["end"]),
                "Duration":     f"{(c['end'] - c['start']) / 60:.1f} min",
                "Type":         c["type"],
                "Participants": ", ".join(names) if names else "—",
                "Words":        int(df_w.loc[mask, "word_count"].sum()),
                "Segments":     int(mask.sum()),
            })

        conv_df = pd.DataFrame(conv_rows)

        def _color_type(val: str) -> str:
            return f"background-color: {TYPE_COLORS.get(val, '#fff')}; {_DARK_TXT}"

        st.dataframe(
            conv_df.style.map(_color_type, subset=["Type"]),
            use_container_width=True,
            hide_index=True,
            height=520,
        )

        st.divider()
        st.subheader("Visual timeline")
        st.caption("Each block = one detected conversation. Hover for details.")

        total_dur = max(c["end"] for c in convs)
        bar_parts = [
            '<div style="display:flex; width:100%; height:40px; '
            'border-radius:6px; overflow:hidden;">'
        ]
        for c in convs:
            pct   = (c["end"] - c["start"]) / total_dur * 100
            col   = TYPE_COLORS.get(c["type"], "#eee")
            names = ", ".join(spk_labels.get(p, p) for p in c["participants"]) or "—"
            tip   = f'{_fmt_ts(c["start"])} – {c["type"]}: {names}'
            bar_parts.append(
                f'<div title="{tip}" style="width:{pct:.2f}%; background:{col}; '
                f'border-right:1px solid rgba(0,0,0,0.1);"></div>'
            )
        bar_parts.append("</div>")

        legend = "  ".join(
            f'<span style="background:{col}; color:#111; padding:2px 8px; '
            f'border-radius:4px; font-size:12px;">{t}</span>'
            for t, col in TYPE_COLORS.items()
        )
        bar_parts.append(f'<div style="margin-top:6px;">{legend}</div>')
        st.html("".join(bar_parts))


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — ALL GAMES (AGGREGATE)
# ══════════════════════════════════════════════════════════════════════════════

with tab_aggregate:
    all_lies = _load_all_lies()

    if all_lies.empty:
        st.info("No analyzed games found yet. Run `analyze_roles.py` on some videos first.")
    else:
        n_games = int(all_lies["video_id"].nunique())
        total   = len(all_lies)
        n_true  = int((all_lies["verdict"] == "TRUE").sum())
        n_lie   = int((all_lies["verdict"] == "LIE").sum())
        n_hm    = int((all_lies["verdict"] == "HONEST MISTAKE").sum())
        n_unv   = int((all_lies["verdict"] == "UNVERIFIED").sum())

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Games analyzed",   n_games)
        m2.metric("Total claims",     total)
        m3.metric("✓ TRUE",           n_true)
        m4.metric("✗ LIE",            n_lie)
        m5.metric("~ Honest Mistake", n_hm)

        st.divider()

        # ── Per-game summary ──────────────────────────────────────────────────
        st.subheader("Per-game summary")

        # Build winner lookup from playlist.json
        _winner_map = {
            e["id"]: e.get("winner")
            for e in (playlist if playlist else [])
        }

        game_agg = (
            all_lies.groupby(["video_id", "video_title"])
            .agg(
                Claims          =("verdict", "count"),
                Lies            =("verdict", lambda x: (x == "LIE").sum()),
                TRUE            =("verdict", lambda x: (x == "TRUE").sum()),
                Honest_Mistakes =("verdict", lambda x: (x == "HONEST MISTAKE").sum()),
                Unverified      =("verdict", lambda x: (x == "UNVERIFIED").sum()),
            )
            .reset_index()
            .rename(columns={"video_title": "Game", "video_id": "ID",
                              "Honest_Mistakes": "Honest Mistakes"})
            .sort_values("Lies", ascending=False)
        )
        game_agg["Winner"] = game_agg["ID"].map(_winner_map).fillna("—")
        game_agg = game_agg.drop(columns="ID")

        def _style_winner(row):
            w = row.get("Winner", "")
            if w == "Evil":
                return [f"background-color:#ffe4e6; {_DARK_TXT}"] * len(row)
            if w == "Good":
                return [f"background-color:#dcfce7; {_DARK_TXT}"] * len(row)
            return [""] * len(row)

        st.dataframe(
            game_agg.style
                .apply(_color_scale, subset=["Lies"])
                .apply(_style_winner, axis=1),
            use_container_width=True,
            hide_index=True,
        )

        st.divider()

        # ── Player leaderboard across all games ───────────────────────────────
        st.subheader("Player leaderboard — all games")

        player_agg = (
            all_lies[
                all_lies["player_name"].notna() &
                ~all_lies["player_name"].str.match(r"^speaker_\d+$", na=False)
            ]
            .groupby("player_name")
            .agg(
                Games           =("video_id", "nunique"),
                Claims          =("verdict", "count"),
                Lies            =("verdict", lambda x: (x == "LIE").sum()),
                TRUE            =("verdict", lambda x: (x == "TRUE").sum()),
                Honest_Mistakes =("verdict", lambda x: (x == "HONEST MISTAKE").sum()),
            )
            .reset_index()
            .rename(columns={"player_name": "Player",
                              "Honest_Mistakes": "Honest Mistakes"})
            .sort_values("Lies", ascending=False)
        )
        player_agg["Lie rate"] = (
            player_agg["Lies"] / player_agg["Claims"].replace(0, float("nan"))
        ).map(lambda v: f"{v:.0%}" if pd.notna(v) else "—")

        st.bar_chart(player_agg.set_index("Player")["Lies"])
        st.dataframe(
            player_agg.style.apply(_color_scale, subset=["Lies"]),
            use_container_width=True,
            hide_index=True,
        )

        st.divider()

        # ── Most claimed roles ────────────────────────────────────────────────
        st.subheader("Most claimed roles")

        col_a, col_b = st.columns(2)

        role_counts = (
            all_lies["claimed_role"]
            .value_counts()
            .reset_index()
            .rename(columns={"claimed_role": "Role", "count": "Times claimed"})
            .head(20)
        )
        col_a.bar_chart(role_counts.set_index("Role")["Times claimed"])

        # ── Most deceptive actual roles ───────────────────────────────────────
        col_b.subheader("Most deceptive actual roles")
        col_b.caption("Roles with the most LIE verdicts across all games.")

        role_lies = (
            all_lies[all_lies["verdict"] == "LIE"]
            .groupby("actual_role")
            .size()
            .reset_index(name="Lies")
            .sort_values("Lies", ascending=False)
            .head(15)
        )
        col_b.bar_chart(role_lies.set_index("actual_role")["Lies"])

        st.divider()

        # ── Player deep-dive ──────────────────────────────────────────────────
        st.subheader("🔍 Player deep-dive")

        _named_mask = (
            all_lies["player_name"].notna() &
            ~all_lies["player_name"].str.match(r"^speaker_\d+$", na=False)
        )
        all_players = sorted(all_lies.loc[_named_mask, "player_name"].unique())
        sel_player  = st.selectbox("Select player", all_players, key="player_deepdive")

        p_df = all_lies[all_lies["player_name"] == sel_player].copy()
        p_df["team"] = p_df["actual_role"].map(_team)

        if p_df.empty:
            st.info(f"No claim data found for {sel_player}.")
        else:
            p_total  = len(p_df)
            p_lies   = int((p_df["verdict"] == "LIE").sum())
            p_true   = int((p_df["verdict"] == "TRUE").sum())
            p_hm     = int((p_df["verdict"] == "HONEST MISTAKE").sum())
            p_games  = int(p_df["video_id"].nunique())
            p_lie_rt = f"{p_lies / p_total:.0%}" if p_total else "—"

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Games",       p_games)
            m2.metric("Claims",      p_total)
            m3.metric("✓ TRUE",      p_true)
            m4.metric("✗ Lie rate",  p_lie_rt)
            m5.metric("~ Honest M.", p_hm)

            st.divider()

            # Evil vs Good breakdown
            col_evil, col_good = st.columns(2)
            for col, team, icon, color in [
                (col_evil, "Evil", "😈", "#ffe4e6"),
                (col_good, "Good", "😇", "#dcfce7"),
            ]:
                t_df     = p_df[p_df["team"] == team]
                t_games  = int(t_df["video_id"].nunique())
                t_claims = len(t_df)
                t_lies   = int((t_df["verdict"] == "LIE").sum())
                t_true   = int((t_df["verdict"] == "TRUE").sum())
                t_hm     = int((t_df["verdict"] == "HONEST MISTAKE").sum())
                t_lie_rt = f"{t_lies / t_claims:.0%}" if t_claims else "—"

                col.markdown(
                    f'<div style="background:{color}; padding:12px 16px; '
                    f'border-radius:8px; margin-bottom:8px;">'
                    f'<strong>{icon} As {team}</strong> ({t_games} game(s))<br>'
                    f'Claims: {t_claims} &nbsp;|&nbsp; '
                    f'✗ Lies: {t_lies} &nbsp;|&nbsp; '
                    f'✓ True: {t_true} &nbsp;|&nbsp; '
                    f'~ Honest: {t_hm}<br>'
                    f'<strong>Lie rate: {t_lie_rt}</strong>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                if not t_df.empty:
                    roles_played = (
                        t_df.groupby("actual_role")
                        .agg(
                            Games  =("video_id", "nunique"),
                            Claims =("verdict", "count"),
                            Lies   =("verdict", lambda x: (x == "LIE").sum()),
                            TRUE   =("verdict", lambda x: (x == "TRUE").sum()),
                        )
                        .reset_index()
                        .rename(columns={"actual_role": "Role"})
                        .sort_values("Lies", ascending=False)
                    )
                    roles_played["Lie rate"] = (
                        roles_played["Lies"]
                        / roles_played["Claims"].replace(0, float("nan"))
                    ).map(lambda v: f"{v:.0%}" if pd.notna(v) else "—")
                    col.dataframe(
                        roles_played.style.apply(_color_scale, subset=["Lies"]),
                        use_container_width=True,
                        hide_index=True,
                    )

            st.divider()

            # Per-game history
            st.subheader("Per-game history")
            game_hist = (
                p_df.groupby(["video_id", "video_title", "actual_role", "team"])
                .agg(
                    Claims =("verdict", "count"),
                    Lies   =("verdict", lambda x: (x == "LIE").sum()),
                    TRUE   =("verdict", lambda x: (x == "TRUE").sum()),
                    HM     =("verdict", lambda x: (x == "HONEST MISTAKE").sum()),
                )
                .reset_index()
                .rename(columns={
                    "video_title": "Game",
                    "actual_role": "Role",
                    "team":        "Team",
                    "HM":          "Honest M.",
                })
                .drop(columns="video_id")
            )
            game_hist["Lie rate"] = (
                game_hist["Lies"]
                / game_hist["Claims"].replace(0, float("nan"))
            ).map(lambda v: f"{v:.0%}" if pd.notna(v) else "—")

            def _style_team(row):
                bg = "#ffe4e6" if row["Team"] == "Evil" else "#dcfce7"
                return [f"background-color: {bg}; {_DARK_TXT}"] * len(row)

            st.dataframe(
                game_hist.style.apply(_style_team, axis=1),
                use_container_width=True,
                hide_index=True,
            )

            # All claims expander
            with st.expander(f"All {p_total} claims by {sel_player}"):
                for _, row in p_df.sort_values("video_title").iterrows():
                    bg   = VERDICT_BG.get(row["verdict"], "#fff")
                    icon = VERDICT_ICON.get(row["verdict"], "?")
                    team_badge = (
                        '<span style="background:#ffe4e6; color:#111; padding:1px 6px; '
                        'border-radius:4px; font-size:11px; margin-right:4px;">😈 Evil</span>'
                        if row["team"] == "Evil" else
                        '<span style="background:#dcfce7; color:#111; padding:1px 6px; '
                        'border-radius:4px; font-size:11px; margin-right:4px;">😇 Good</span>'
                    )
                    st.markdown(
                        f'<div style="background:{bg}; padding:8px 12px; '
                        f'border-radius:6px; margin:4px 0;">'
                        f'{team_badge}'
                        f'<strong>{row["timestamp"]}</strong> in '
                        f'<em>{row["video_title"]}</em> '
                        f'(as {row["actual_role"]}) &nbsp; claimed '
                        f'<strong>{row["claimed_role"]}</strong> &nbsp;'
                        f'{icon} <strong>{row["verdict"]}</strong><br>'
                        f'<span style="color:#555; font-size:0.9em;">'
                        f'{str(row["text"])[:300]}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

