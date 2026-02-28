"""explore.py - Streamlit UI for exploring BotC transcript data (full editor).

This app can edit rosters, save overrides, and trigger re-analysis.
For a publish-safe read-only version use explore_public.py.

Usage:
    streamlit run explore.py
"""

import json
import subprocess
from pathlib import Path

import pandas as pd
import streamlit as st

DEFAULT_VIDEO_ID = "lF96Jd3Eaeg"

# ── colour palettes ───────────────────────────────────────────────────────────

_SPK_PALETTE = [
    "#dbeafe", "#fef9c3", "#dcfce7", "#ffe4e6",
    "#e0f2fe", "#fde8d0", "#ede9fe", "#d1fae5", "#fee2e2",
]

VERDICT_BG = {
    "TRUE":           "#dcfce7",
    "HONEST MISTAKE": "#fef9c3",
    "LIE":            "#ffe4e6",
    "UNVERIFIED":     "#e5e7eb",
}

VERDICT_ICON = {
    "TRUE": "✓", "HONEST MISTAKE": "~", "LIE": "✗", "UNVERIFIED": "?",
}

_STATUS_BG = {
    "analyzed":    "#dcfce7",
    "patched":     "#d1fae5",
    "merged":      "#fef9c3",
    "diarized":    "#fde8d0",
    "transcribed": "#e0f2fe",
    "downloaded":  "#dbeafe",
    "pending":     "#e5e7eb",
}

# Known evil roles in BotC (demons + minions) — used to split good/evil stats
_EVIL_ROLES = {
    # Demons
    "imp", "ojo", "vigormortis", "no dashii", "vortox", "fang gu",
    "al-hadikhia", "lil' monsta", "lil monsta", "pukka", "po", "lleech",
    "shabaloth", "zombuul", "legion", "riot",
    # Minions
    "poisoner", "spy", "scarlet woman", "baron", "godfather", "assassin",
    "devil's advocate", "devils advocate", "evil twin", "witch", "cerenovus",
    "pit-hag", "pit hag", "fearmonger", "marionette", "organ grinder",
    "mezepheles", "harpy",
}


def _team(role: str) -> str:
    return "Evil" if str(role).strip().lower() in _EVIL_ROLES else "Good"


# Source-badge styles: (icon, background)
_SOURCE_STYLE = {
    "manual":           ("🟢", "#dcfce7"),
    "scraped":          ("📷", "#e0f2fe"),
    "auto":             ("🟡", "#fef9c3"),
    "scraped_unlinked": ("⚠️",  "#fde8d0"),
    "unlinked":         ("🔴", "#ffe4e6"),
    "system":           ("🎭", "#f0f0f0"),
}


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
    out_dir = Path(f"outputs/{video_id}")
    for name in ("segments_patched.csv", "segments.csv"):
        p = out_dir / name
        if p.exists():
            return pd.read_csv(p)
    return pd.DataFrame(columns=["start", "end", "speaker", "text"])


@st.cache_data
def _load_lies(video_id: str) -> pd.DataFrame:
    p = Path(f"outputs/{video_id}") / "lie_analysis.csv"
    return pd.read_csv(p) if p.exists() else pd.DataFrame()


def _load_overrides(video_id: str) -> dict:
    f = Path(f"outputs/{video_id}") / "roster_overrides.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {}


def _save_overrides(video_id: str, data: dict) -> None:
    f = Path(f"outputs/{video_id}") / "roster_overrides.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@st.cache_data
def _load_intro_roster(video_id: str) -> dict:
    """Load scraped intro_roster.json produced by scrape_intro.py."""
    f = Path(f"outputs/{video_id}") / "intro_roster.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


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
    turns:   list[tuple[float, float, str]],
    window:  float = 30.0,
    min_dur: float = 20.0,
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


def _fmt_ts(sec: float) -> str:
    m, s = int(sec // 60), sec % 60
    return f"{m:02d}:{s:05.2f}"


# ── page setup ────────────────────────────────────────────────────────────────

st.set_page_config(page_title="BotC Explorer", layout="wide", page_icon="🏰")
st.title("🏰  BotC Transcript Explorer")


# ── game selector (sidebar, first block) ──────────────────────────────────────

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

        entry  = next((e for e in playlist if e["id"] == video_id), {})
        status = entry.get("status", "unknown")
        bg     = _STATUS_BG.get(status, "#e5e7eb")
        st.markdown(
            f'Pipeline status: <span style="background:{bg}; color:#111; '
            f'padding:2px 8px; border-radius:4px; font-size:13px;">{status}</span>',
            unsafe_allow_html=True,
        )
    else:
        video_id = DEFAULT_VIDEO_ID
        st.info("No `playlist.json` — using default game.\n"
                "Run `fetch_playlist.py` to enable the game selector.")

    st.divider()


# ── load data for selected game ───────────────────────────────────────────────

segs       = _load_segments(video_id)
lies       = _load_lies(video_id)
overrides  = _load_overrides(video_id)
intro_data = _load_intro_roster(video_id)

_roles_file = Path(f"outputs/{video_id}/roles.txt")
if not _roles_file.exists():
    _roles_file = Path("roles.txt")
players  = _load_vocab("players.txt")
roles    = _load_vocab(str(_roles_file))
roles_lc = {r.lower(): r for r in roles}

all_spks  = sorted(segs["speaker"].unique().tolist()) if not segs.empty else []
spk_color = {s: _SPK_PALETTE[i % len(_SPK_PALETTE)] for i, s in enumerate(all_spks)}
# Storyteller gets a distinct neutral colour
if "speaker_0" in spk_color:
    spk_color["speaker_0"] = "#f0f0f0"

overrides_file = Path(f"outputs/{video_id}") / "roster_overrides.json"

# Scraped lookup: {lowercase name -> player dict}
scraped_lookup: dict[str, dict] = {}
for _p in intro_data.get("players", []):
    _n = (_p.get("name") or "").lower()
    if _n:
        scraped_lookup[_n] = _p


# ── sidebar data stats (second block) ─────────────────────────────────────────

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
        ts_scraped = intro_data.get("scraped_at", "")
        st.write(f"**Scraped intro:** {n_scraped} player(s) 📷")
        if ts_scraped:
            st.caption(f"Scraped: {ts_scraped}")
    st.write(f"**Overrides saved:** {len(overrides)}")
    st.divider()
    st.caption(
        "Edit the Roster tab to fix unlinked speakers, "
        "then click **Save & Re-analyze** to refresh."
    )


# ── build speaker label map ───────────────────────────────────────────────────

def _spk_label(spk: str) -> str:
    """Return 'Name (role)' if known, else 'speaker_N'."""
    if spk == "speaker_0":
        return "Storyteller"
    ov = overrides.get(spk)
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

# ── tab-jump helper (JS click is the only way without key= support) ───────────
if st.session_state.pop("_goto_fix_tab", False):
    import streamlit.components.v1 as _stc
    _stc.html(
        """<script>
        (function(){
          function click_tab(){
            var btns = window.parent.document.querySelectorAll('button[data-baseweb="tab"]');
            if(btns && btns.length > 5){ btns[5].click(); }
            else { setTimeout(click_tab, 50); }
          }
          setTimeout(click_tab, 80);
        })();
        </script>""",
        height=0,
    )

tab_roster, tab_tx, tab_lies_tab, tab_stats, tab_aggregate, tab_fix = st.tabs(
    ["🎭  Roster", "📜  Transcript", "🔍  Lie Analysis", "📊  Stats", "🌐  All Games", "🔧  Fix Rosters"]
)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — ROSTER
# ══════════════════════════════════════════════════════════════════════════════

with tab_roster:
    st.markdown(
        "Assign each speaker ID to the correct player and role.  "
        "**🔴 Red** = unlinked &nbsp; **🟡 Yellow** = auto-detected &nbsp; "
        "**🟢 Green** = manually saved &nbsp; **📷 Blue** = scraped from video &nbsp; "
        "**🎭 Grey** = Storyteller (system)"
    )

    # 1. Seed from lie_analysis (auto-detected)
    auto: dict[str, dict] = {}
    if not lies.empty:
        for _, r in lies.drop_duplicates("speaker").iterrows():
            name = r["player_name"] if r["player_name"] != r["speaker"] else ""
            auto[r["speaker"]] = {
                "name":          name,
                "believed_role": str(r.get("believed_role") or "").lower(),
                "actual_role":   str(r.get("actual_role")   or "").lower(),
                "source":        "auto" if name else "unlinked",
            }

    # 2. Fill in any speakers missing from lie_analysis
    for spk in all_spks:
        if spk not in auto:
            if spk == "speaker_0":
                auto[spk] = {
                    "name":          "Storyteller",
                    "believed_role": "storyteller",
                    "actual_role":   "storyteller",
                    "source":        "system",
                }
            else:
                auto[spk] = {
                    "name":          "",
                    "believed_role": "",
                    "actual_role":   "",
                    "source":        "unlinked",
                }

    # 3. Overlay saved overrides
    for spk, ov in overrides.items():
        if ov.get("name"):
            auto.setdefault(spk, {"name": "", "believed_role": "",
                                  "actual_role": "", "source": "unlinked"})
            auto[spk].update(ov)
            auto[spk]["source"] = "manual"

    # 4. Mark scraped entries (only for auto-detected, not manual)
    for spk, info in auto.items():
        if info["source"] == "auto":
            name_lc = (info.get("name") or "").lower()
            if name_lc in scraped_lookup:
                info["source"] = "scraped"

    df_ros = pd.DataFrame(
        [{"speaker": spk, **info} for spk, info in sorted(auto.items())]
    )[["speaker", "name", "believed_role", "actual_role", "source"]]

    # Read-only coloured overview
    def _style_src(row):
        src = row["source"]
        if src == "system":
            bg = "#f0f0f0"
        elif src == "manual":
            bg = "#dcfce7"
        elif src == "scraped":
            bg = "#e0f2fe"
        elif src == "auto":
            bg = "#fef9c3"
        else:
            bg = "#ffe4e6"
        return [f"background-color: {bg}; color: #111"] * len(row)

    # Source legend
    st.markdown(
        " &nbsp; ".join(
            f'<span style="background:{v[1]}; color:#111; padding:2px 8px; '
            f'border-radius:4px; font-size:12px;">{v[0]} {k}</span>'
            for k, v in _SOURCE_STYLE.items()
        ),
        unsafe_allow_html=True,
    )
    st.write("")

    st.dataframe(
        df_ros.style.apply(_style_src, axis=1),
        use_container_width=True,
        hide_index=True,
    )

    # Show scraped intro data if available
    if scraped_lookup:
        with st.expander(
            f"📷 Scraped intro data ({len(scraped_lookup)} entries) "
            f"— {intro_data.get('scraped_at', 'unknown time')}"
        ):
            scraped_rows = []
            for _p in intro_data.get("players", []):
                deception = (
                    "⚠️ Deceived"
                    if _p.get("actual_role") != _p.get("believed_role")
                    else "✓ Honest"
                )
                scraped_rows.append({
                    "Name":          _p.get("name", ""),
                    "Believed Role": _p.get("believed_role", ""),
                    "Actual Role":   _p.get("actual_role", ""),
                    "Detected at":   f"{_p.get('frame_time', 0):.1f}s",
                    "Status":        deception,
                })
            st.dataframe(
                pd.DataFrame(scraped_rows),
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                "Re-run `scrape_intro.py --force` to refresh this data. "
                "Use the editor below to correct any OCR mistakes."
            )

    st.divider()
    st.subheader("Edit assignments")

    # Lift role values to title case to match dropdown options
    edit_df = df_ros[["speaker", "name", "believed_role", "actual_role"]].copy()
    edit_df["believed_role"] = edit_df["believed_role"].map(
        lambda v: roles_lc.get(str(v).lower(), str(v) if v else "")
    )
    edit_df["actual_role"] = edit_df["actual_role"].map(
        lambda v: roles_lc.get(str(v).lower(), str(v) if v else "")
    )

    edited = st.data_editor(
        edit_df,
        column_config={
            "speaker": st.column_config.TextColumn(
                "Speaker", disabled=True, width="small"
            ),
            "name": st.column_config.SelectboxColumn(
                "Player Name", options=[""] + players, width="medium"
            ),
            "believed_role": st.column_config.SelectboxColumn(
                "Believed Role", options=[""] + roles, width="medium"
            ),
            "actual_role": st.column_config.SelectboxColumn(
                "Actual Role", options=[""] + roles, width="medium"
            ),
        },
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        key="roster_editor",
    )

    c_save, c_rerun, _ = st.columns([1, 1.6, 4])

    if c_save.button("💾  Save", use_container_width=True):
        out = {
            row["speaker"]: {
                "name":          row["name"] or "",
                "believed_role": (row["believed_role"] or "").lower(),
                "actual_role":   (row["actual_role"] or "").lower(),
            }
            for _, row in edited.iterrows()
        }
        _save_overrides(video_id, out)
        st.success(f"Saved {len(out)} entries to `{overrides_file}`")
        st.cache_data.clear()
        st.rerun()

    if c_rerun.button("🔄  Save & Re-analyze", use_container_width=True):
        out = {
            row["speaker"]: {
                "name":          row["name"] or "",
                "believed_role": (row["believed_role"] or "").lower(),
                "actual_role":   (row["actual_role"] or "").lower(),
            }
            for _, row in edited.iterrows()
        }
        _save_overrides(video_id, out)
        with st.spinner("Running analyze_roles.py…"):
            result = subprocess.run(
                ["python", "analyze_roles.py", video_id],
                capture_output=True, text=True,
            )
        if result.returncode == 0:
            st.success("Re-analysis complete!")
        else:
            st.error("analyze_roles.py returned an error — see output below.")
        with st.expander("Script output"):
            st.code(result.stdout + result.stderr)
        st.cache_data.clear()
        st.rerun()


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

    # Legend strip
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

    # Chat-style bubbles
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
        st.warning("No `lie_analysis.csv` found — run `analyze_roles.py` first.")
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
        return f"background-color: {VERDICT_BG.get(str(val), '#fff')}; color: #111"

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
                bg    = VERDICT_BG.get(row["verdict"], "#fff")
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
        ov = overrides.get(spk)
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
        }).style.background_gradient(subset=["Total words"], cmap="Blues"),
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    st.subheader("Speaking time over the game")
    st.caption("Each bar = words spoken in a 5-minute window.")

    df_game = df_w[df_w["speaker"] != "speaker_0"].copy()
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
        "Detected by sliding a 30-second window across the diarization timeline "
        "and grouping periods where the same speakers are active together."
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
            return f"background-color: {TYPE_COLORS.get(val, '#fff')}; color: #111"

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

@st.cache_data
def _load_all_lies(entries: tuple) -> pd.DataFrame:
    """Load lie_analysis.csv from every analyzed video and concatenate."""
    frames = []
    for vid, title in entries:
        p = Path(f"outputs/{vid}/lie_analysis.csv")
        if p.exists():
            df = pd.read_csv(p)
            df["video_id"]    = vid
            df["video_title"] = title
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


with tab_aggregate:
    _entries = tuple(
        (e["id"], e.get("title", e["id"]))
        for e in (playlist if playlist else [{"id": video_id, "title": video_id}])
    )
    all_lies = _load_all_lies(_entries)

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
                return ["background-color:#ffe4e6; color:#111"] * len(row)
            if w == "Good":
                return ["background-color:#dcfce7; color:#111"] * len(row)
            return [""] * len(row)

        st.dataframe(
            game_agg.style
                .background_gradient(subset=["Lies"], cmap="Reds")
                .apply(_style_winner, axis=1),
            use_container_width=True,
            hide_index=True,
        )

        st.divider()

        # ── Player leaderboard across all games ───────────────────────────────
        st.subheader("Player leaderboard — all games")

        player_agg = (
            all_lies[all_lies["player_name"].notna()]
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
            player_agg.style.background_gradient(subset=["Lies"], cmap="Reds"),
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

        all_players = sorted(all_lies["player_name"].dropna().unique())
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
                        roles_played.style.background_gradient(
                            subset=["Lies"], cmap="Reds"
                        ),
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
                return [f"background-color: {bg}; color: #111"] * len(row)

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


# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — FIX ROSTERS  (slideshow of all intro_roster issues across every game)
# ══════════════════════════════════════════════════════════════════════════════

import re as _re

_OUTPUTS_DIR = Path("outputs")

# ── role aliases: themed / Christmas / Jingle Jam names → canonical BotC names ─
# Add any game-specific pseudonyms here so they're never flagged as unknown roles.
_ROLE_ALIASES: dict[str, str] = {
    # Christmas / "Evil Santa" game (oN0pdPoVGhY)
    "evil santa al-madikhia":   "al-hadikhia",
    "al-madikhia":              "al-hadikhia",
    "evil santa":               "al-hadikhia",
    # add further aliases as new themed games appear
}

# Known canonical role names (used to catch OCR reading a role label as a player name,
# e.g. "GRANDMOTHER" or "UNDERTAKER" appearing in the player_name field)
_KNOWN_ROLES: frozenset[str] = frozenset({
    "imp", "ojo", "vigormortis", "no dashii", "vortox", "fang gu",
    "al-hadikhia", "lil monsta", "pukka", "po", "lleech", "shabaloth",
    "zombuul", "legion", "riot", "lord of typhon",
    "poisoner", "spy", "scarlet woman", "baron", "godfather", "assassin",
    "devil's advocate", "evil twin", "witch", "cerenovus", "pit-hag",
    "fearmonger", "marionette", "organ grinder", "mezepheles", "harpy",
    "washerwoman", "librarian", "investigator", "chef", "empath",
    "fortune teller", "undertaker", "monk", "ravenkeeper", "virgin",
    "slayer", "soldier", "mayor", "butler", "drunk", "recluse", "saint",
    "grandmother", "amnesiac", "noble", "lunatic", "fisherman", "clockmaker",
    "dreamer", "seamstress", "philosopher", "artist", "juggler", "sage",
    "mathematician", "flower girl", "town crier", "oracle", "savant",
    "balloonist", "cannibal", "magician", "knight", "nightwatchman",
    "pixie", "damsel", "heretic", "farmer", "acrobat", "innkeeper",
    "gambler", "gossip", "chambermaid", "exorcist", "professor",
    "necromancer", "huntsman", "poppy grower", "boomdandy", "ogre",
    "puzzlemaster", "wizard", "steward", "king", "lycanthrope",
    "snake charmer", "cult leader", "klutz", "banshee", "alchemist",
    "princess", "courtier", "fool", "sweetheart", "tinker", "pacifist",
    "vizier", "boffin", "alsaahir", "general", "minion", "demon",
    "traveller", "traveler", "barista", "fiddler", "bishop", "bureaucrat",
})

# Roles that can legitimately receive a false token (told they are a different role).
# Only these three should ever have believed_role != actual_role in the data.
_DECEIVABLE_ROLES: frozenset[str] = frozenset({"drunk", "lunatic", "marionette"})

# Garbled-name triggers:
#   1. Contains a digit (e.g. "4 UNDEL")
#   2. Starts with a single letter then a space (e.g. "T TOM", "T ALEX")
#   3. Is itself a known role name (OCR read the role label instead of the player name)
# We deliberately do NOT flag all-caps names (BARRY, ROSS, CRAIG are valid).
_DIGIT_RE     = _re.compile(r"\d")
_SINGLE_LTR_RE = _re.compile(r"^\w\s")   # "T TOM", "T ALEX"


def _is_garbled(name: str) -> bool:
    n = name.strip()
    if not n:
        return False
    if _DIGIT_RE.search(n):           # has a digit
        return True
    if _SINGLE_LTR_RE.match(n):       # single-letter prefix
        return True
    if n.lower() in _KNOWN_ROLES:     # OCR read a role label as the player name
        return True
    return False


_ISSUE_LABELS = {
    "duplicate_role":      ("🔁", "#fde8d0", "Duplicate role"),
    "unknown_role":        ("❓", "#e5e7eb", "Unknown / blank role"),
    "garbled_name":        ("⚠️",  "#fef9c3", "Garbled player name"),
    "wrong_believed_role": ("🎭", "#ddd6fe", "Wrong believed role (OCR artifact)"),
}


@st.cache_data
def _all_roster_issues(entries_key: tuple) -> list[dict]:
    """Scan every intro_roster.json for problems.
    entries_key is a tuple of (video_id, title, members) for cache keying.
    Role aliases are applied before checking so themed names are not flagged.
    """
    from collections import defaultdict

    issues: list[dict] = []
    seen:   set[tuple] = set()

    for vid, title, members in entries_key:
        roster_path = _OUTPUTS_DIR / vid / "intro_roster.json"
        if not roster_path.exists():
            continue
        try:
            players = json.loads(
                roster_path.read_text(encoding="utf-8")
            ).get("players", [])
        except Exception:
            continue

        # Normalise roles through the alias map
        for p in players:
            for field in ("actual_role", "believed_role"):
                raw = (p.get(field) or "").lower().strip()
                p[field] = _ROLE_ALIASES.get(raw, raw)

        # Per-video role counts (after alias normalisation)
        role_counts:  dict[str, int]  = defaultdict(int)
        role_players: dict[str, list] = defaultdict(list)
        for p in players:
            r = p.get("actual_role", "")
            n = p.get("name", "")
            if r and r != "unknown":
                role_counts[r] += 1
                role_players[r].append(n)

        for p in players:
            name   = p.get("name", "")
            actual = p.get("actual_role", "")
            bel    = p.get("believed_role", "")
            ft     = float(p.get("frame_time", 0))
            key    = (vid, name, actual, ft)
            if key in seen:
                continue

            # 1. Duplicate role
            if actual and actual != "unknown" and role_counts[actual] > 1:
                conflicts = [x for x in role_players[actual] if x != name]
                issues.append({
                    "video_id": vid, "title": title, "members": members,
                    "player_name": name, "actual_role": actual,
                    "believed_role": bel, "frame_time": ft,
                    "issue_type": "duplicate_role",
                    "conflict_players": conflicts,
                })
                seen.add(key)
                continue

            # 2. Unknown / blank role (after alias resolution)
            if not actual or actual == "unknown":
                issues.append({
                    "video_id": vid, "title": title, "members": members,
                    "player_name": name, "actual_role": actual,
                    "believed_role": bel, "frame_time": ft,
                    "issue_type": "unknown_role",
                    "conflict_players": [],
                })
                seen.add(key)
                continue

            # 3. Garbled player name
            if _is_garbled(name):
                issues.append({
                    "video_id": vid, "title": title, "members": members,
                    "player_name": name, "actual_role": actual,
                    "believed_role": bel, "frame_time": ft,
                    "issue_type": "garbled_name",
                    "conflict_players": [],
                })
                seen.add(key)
                continue

            # 4. Wrong believed role — OCR picked up a second visible role on screen.
            #    Only Drunk / Lunatic / Marionette can legitimately differ; for all
            #    other roles a mismatch is an OCR artifact that should be corrected.
            if (bel and bel != actual
                    and actual not in _DECEIVABLE_ROLES):
                issues.append({
                    "video_id": vid, "title": title, "members": members,
                    "player_name": name, "actual_role": actual,
                    "believed_role": bel, "frame_time": ft,
                    "issue_type": "wrong_believed_role",
                    "conflict_players": [],
                })
                seen.add(key)

    return issues


def _first_line_for(video_id: str, player_name: str, frame_time: float = 0.0) -> str:
    """Return the first spoken line for a player.

    Strategy:
      1. Look up the speaker in lie_analysis.csv (case-insensitive name match).
      2. Find their earliest segment.
      3. Fallback: if no speaker found, show transcript context around frame_time
         (the moment the player's role card appeared in the intro).
    """
    lie_csv = _OUTPUTS_DIR / video_id / "lie_analysis.csv"
    speaker = None

    if lie_csv.exists():
        try:
            lies_df = pd.read_csv(lie_csv)
            name_lc = player_name.lower().strip()
            rows = lies_df[lies_df["player_name"].str.lower().str.strip() == name_lc]
            if not rows.empty:
                speaker = rows.iloc[0]["speaker"]
        except Exception:
            pass

    segs = _load_segments(video_id)
    if segs.empty:
        return ""

    if speaker:
        spk_segs = segs[segs["speaker"] == speaker].sort_values("start")
        if not spk_segs.empty:
            return str(spk_segs.iloc[0]["text"])

    # Fallback: show non-storyteller transcript context around the frame_time
    if frame_time > 0:
        window = segs[
            (segs["start"] >= max(0, frame_time - 5))
            & (segs["start"] <= frame_time + 30)
            & (segs["speaker"] != "speaker_0")
        ].sort_values("start")
        if not window.empty:
            row = window.iloc[0]
            return f"[~{int(frame_time)}s] {row['text']}"

    return ""


with tab_fix:
    _fix_entries = tuple(
        (e["id"], e.get("title", e["id"]), e.get("members", False))
        for e in (playlist if playlist else [])
    )
    all_issues = _all_roster_issues(_fix_entries)

    if not all_issues:
        st.success("No roster issues found — everything looks clean! 🎉")
        st.stop()

    # ── filters ───────────────────────────────────────────────────────────────
    def _want_fix_tab():
        st.session_state["_goto_fix_tab"] = True

    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        filter_type = st.multiselect(
            "Issue type",
            ["duplicate_role", "unknown_role", "garbled_name"],
            default=["duplicate_role", "unknown_role", "garbled_name"],
            format_func=lambda x: _ISSUE_LABELS[x][2],
            key="fix_type_filter",
            on_change=_want_fix_tab,
        )
    with col_f2:
        filter_members = st.radio(
            "Game type", ["All", "Main only", "Members only"],
            horizontal=True, key="fix_mbr_filter",
            on_change=_want_fix_tab,
        )
    with col_f3:
        filter_vid = st.text_input("Filter by video ID / title", "",
                                   key="fix_vid_filter",
                                   on_change=_want_fix_tab)

    filtered = [
        iss for iss in all_issues
        if iss["issue_type"] in filter_type
        and (filter_members == "All"
             or (filter_members == "Members only" and iss["members"])
             or (filter_members == "Main only"    and not iss["members"]))
        and (not filter_vid
             or filter_vid.lower() in iss["video_id"].lower()
             or filter_vid.lower() in iss["title"].lower())
    ]

    if not filtered:
        st.info("No issues match the current filters.")
        st.stop()

    st.divider()

    # ── session-state slide index ─────────────────────────────────────────────
    if "fix_idx" not in st.session_state:
        st.session_state["fix_idx"] = 0
    idx = min(int(st.session_state["fix_idx"]), len(filtered) - 1)
    st.session_state["fix_idx"] = idx

    # ── navigation bar ────────────────────────────────────────────────────────
    col_prev, col_prog, col_next = st.columns([1, 6, 1])
    with col_prev:
        if st.button("◀ Prev", disabled=(idx == 0), key="fix_prev"):
            st.session_state["fix_idx"] = idx - 1
            st.session_state["_goto_fix_tab"] = True
    with col_next:
        if st.button("Next ▶", disabled=(idx == len(filtered) - 1), key="fix_next"):
            st.session_state["fix_idx"] = idx + 1
            st.session_state["_goto_fix_tab"] = True

    # Re-read idx after possible button update (no st.rerun() — avoids tab reset)
    idx = min(int(st.session_state["fix_idx"]), len(filtered) - 1)

    with col_prog:
        st.progress((idx + 1) / len(filtered),
                    text=f"Issue **{idx + 1}** of **{len(filtered)}**")

    # ── current slide ─────────────────────────────────────────────────────────
    iss    = filtered[idx]
    icon, bg, label = _ISSUE_LABELS[iss["issue_type"]]
    vid    = iss["video_id"]
    title  = iss["title"]
    mbr    = iss["members"]
    name   = iss["player_name"]
    actual = iss["actual_role"]
    bel    = iss["believed_role"]
    ft     = iss["frame_time"]

    mbr_chip = (
        '<span style="background:#7c3aed; color:#fff; padding:2px 8px; '
        'border-radius:4px; font-size:12px; margin-right:6px;">🔒 Members</span>'
        if mbr else
        '<span style="background:#e5e7eb; color:#444; padding:2px 8px; '
        'border-radius:4px; font-size:12px; margin-right:6px;">📺 Main</span>'
    )
    issue_chip = (
        f'<span style="background:{bg}; color:#111; padding:2px 8px; '
        f'border-radius:4px; font-size:12px;">{icon} {label}</span>'
    )
    st.markdown(f'<div style="margin-bottom:6px;">{mbr_chip}{issue_chip}</div>',
                unsafe_allow_html=True)
    st.markdown(f'### [{title}](https://www.youtube.com/watch?v={vid})')
    st.caption(f"Video ID: `{vid}`")
    st.divider()

    col_card, col_edit = st.columns([3, 2])

    with col_card:
        # Conflict banner
        if iss["issue_type"] == "duplicate_role":
            st.markdown(
                f'<div style="background:{bg}; color:#111; border-left:4px solid #f97316; '
                f'padding:10px 14px; border-radius:6px; margin-bottom:10px;">'
                f'<strong>{icon} Role conflict</strong><br>'
                f'<code>{actual}</code> is also assigned to: '
                f'<strong>{", ".join(iss["conflict_players"])}</strong>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Player card
        deceived = (actual in _DECEIVABLE_ROLES and bel and actual != bel)
        st.markdown(
            f'<div style="background:#f8fafc; color:#111; border:1px solid #e2e8f0; '
            f'border-radius:8px; padding:14px 18px;">'
            f'<div style="font-size:1.2em; font-weight:700; margin-bottom:6px;">👤 {name}</div>'
            f'<div>Actual role: &nbsp;<strong>{actual or "—"}</strong></div>'
            f'<div>Believed role: &nbsp;<strong>{bel or "—"}</strong>'
            + (' &nbsp;⚠️ <em>(deceived)</em>' if deceived else '') +
            f'</div>'
            f'<div style="color:#666; font-size:0.85em; margin-top:6px;">'
            f'Detected at: {ft:.1f}s &nbsp;|&nbsp; '
            f'<a href="https://www.youtube.com/watch?v={vid}&t={int(ft)}s" '
            f'target="_blank">▶ Jump to timestamp</a>'
            f'</div></div>',
            unsafe_allow_html=True,
        )

        # First spoken line (or context fallback)
        st.markdown("**First spoken line in this game:**")
        first_line = _first_line_for(vid, name, ft)
        if first_line:
            is_context = first_line.startswith("[~")
            line_color = "#555" if is_context else "#0c4a6e"
            border_color = "#94a3b8" if is_context else "#0ea5e9"
            bg_color = "#f8fafc" if is_context else "#f0f9ff"
            st.markdown(
                f'<div style="background:{bg_color}; color:{line_color}; '
                f'border-left:3px solid {border_color}; '
                f'padding:8px 12px; border-radius:4px; font-style:italic; margin-top:4px;">'
                f'"{first_line[:280]}"'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            st.caption("*(no transcript line found — player likely not in lie analysis)*")

        # Conflict players' first lines
        if iss["issue_type"] == "duplicate_role":
            for cp in iss["conflict_players"]:
                cp_line = _first_line_for(vid, cp, ft)
                st.markdown(
                    f'<div style="background:#fff7ed; color:#7c2d12; '
                    f'border-left:3px solid #f97316; '
                    f'padding:8px 12px; border-radius:4px; margin-top:8px;">'
                    f'<strong>{cp}</strong><br>'
                    f'<em>'
                    + (f'"{cp_line[:200]}"' if cp_line else "(no transcript line)") +
                    f'</em></div>',
                    unsafe_allow_html=True,
                )

    # ── edit form ─────────────────────────────────────────────────────────────
    with col_edit:
        st.markdown("**✏️ Fix this entry**")
        roster_path = _OUTPUTS_DIR / vid / "intro_roster.json"

        with st.form(key=f"fix_{idx}_{vid}_{name}"):
            new_name   = st.text_input("Player name", value=name)
            new_actual = st.text_input("Actual role", value=actual)
            new_bel    = st.text_input(
                "Believed role (blank = same as actual)",
                value=bel if bel != actual else "",
            )
            remove    = st.checkbox("Remove this entry entirely")
            submitted = st.form_submit_button("💾 Save & next")

        if submitted:
            data    = json.loads(roster_path.read_text(encoding="utf-8"))
            players = data.get("players", [])

            if remove:
                players = [
                    p for p in players
                    if not (
                        p.get("name", "") == name
                        and (p.get("actual_role") or "").lower() == actual
                        and abs(float(p.get("frame_time", 0)) - ft) < 1.0
                    )
                ]
            else:
                matched = False
                for p in players:
                    if (
                        p.get("name", "") == name
                        and (p.get("actual_role") or "").lower() == actual
                        and abs(float(p.get("frame_time", 0)) - ft) < 1.0
                    ):
                        p["name"]          = new_name.strip()
                        p["actual_role"]   = new_actual.strip().lower()
                        p["believed_role"] = (
                            new_bel.strip().lower()
                            if new_bel.strip() else new_actual.strip().lower()
                        )
                        matched = True
                if not matched:
                    st.error(f"Could not find entry to update (name={name!r}, role={actual!r}, t={ft})")

            data["players"] = players
            roster_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            _all_roster_issues.clear()
            _load_intro_roster.clear()
            # Advance index — no st.rerun() needed; form submit triggers rerun automatically
            next_idx = min(idx + 1, len(filtered) - 1)
            st.session_state["fix_idx"] = next_idx
            st.session_state["_goto_fix_tab"] = True
            st.success(
                f"✅ Saved!  "
                f"Run `python analyze_roles.py {vid}` then `python build_db.py` to update the DB."
            )

    st.divider()

    # ── summary table ─────────────────────────────────────────────────────────
    with st.expander(f"📋 All {len(filtered)} issues (current filter)", expanded=False):
        summary_rows = []
        for i, iss2 in enumerate(filtered):
            ico, _, lbl = _ISSUE_LABELS[iss2["issue_type"]]
            summary_rows.append({
                "#":       i + 1,
                "Type":    f"{ico} {lbl}",
                "Game":    iss2["title"][:50],
                "Members": "🔒" if iss2["members"] else "",
                "Player":  iss2["player_name"],
                "Role":    iss2["actual_role"],
                "Conflict": ", ".join(iss2["conflict_players"]),
            })
        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True,
                     hide_index=True)

