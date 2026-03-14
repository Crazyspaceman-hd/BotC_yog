"""fix_rosters.py — Standalone BotC Roster Fix Tool

Purpose-built for working through roster data quality issues efficiently.

Handles four issue types:
  🔗 Unlinked speaker  — speaker_X ID present in segments.csv but not mapped to
                         any player name in roster_overrides.json
  🔁 Duplicate role    — two players in intro_roster.json share the same role
  ❓ Unknown / blank   — a player's role could not be read by the OCR scraper
  ⚠️  Garbled name     — the player name field contains OCR garbage

Key UX design:
  • Unlinked speakers are shown ONE GAME AT A TIME — all speaker_X IDs for
    a video appear together so you can assign the whole cast in one interaction.
  • OCR issues (duplicate / unknown / garbled) are shown one entry at a time.

Usage:
    streamlit run fix_rosters.py
"""

import csv
import json
import re as _re
from collections import defaultdict
from pathlib import Path

import pandas as pd
import streamlit as st

from botc_ui import _EVIL_ROLES
from pipeline_utils import load_player_aliases, resolve_player_name

_PLAYER_ALIASES: dict[str, str] = load_player_aliases()

# ─── Paths ────────────────────────────────────────────────────────────────────

OUTPUTS_DIR = Path("outputs")
PLAYLIST    = Path("playlist.json")
PLAYERS_TXT = Path("players.txt")
ROLES_TXT   = Path("roles.txt")

# ─── Constants ────────────────────────────────────────────────────────────────

_STORYTELLER = "🎙️ Storyteller"
_SKIP        = "— skip for now —"

ISSUE_META: dict[str, tuple[str, str, str]] = {
    "unlinked_speaker": ("🔗", "#ffe4e6", "Unlinked speaker"),
    "duplicate_role":   ("🔁", "#fde8d0", "Duplicate role"),
    "unknown_role":     ("❓", "#e5e7eb", "Unknown / blank role"),
    "garbled_name":     ("⚠️",  "#fef9c3", "Garbled player name"),
}

# Role aliases: themed / Christmas names → canonical BotC names
_ROLE_ALIASES: dict[str, str] = {
    "evil santa al-madikhia": "al-hadikhia",
    "al-madikhia":            "al-hadikhia",
    "evil santa":             "al-hadikhia",
}

# Roles that can legitimately receive a false token
_DECEIVABLE_ROLES: frozenset[str] = frozenset({"drunk", "lunatic", "marionette"})

# Garbled-name detection patterns (mirrored from explore.py)
_DIGIT_RE      = _re.compile(r"\d")
_SINGLE_LTR_RE = _re.compile(r"^\w\s")   # "T TOM", "T ALEX"

# Canonical role names (for detecting OCR-read role labels in name field)
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
    "huntsman", "poppy grower", "boomdandy", "ogre", "puzzlemaster",
    "wizard", "steward", "king", "lycanthrope", "snake charmer",
    "cult leader", "klutz", "banshee", "alchemist", "princess", "courtier",
    "fool", "sweetheart", "tinker", "pacifist", "vizier", "boffin",
    "alsaahir", "general", "minion", "demon", "traveller", "traveler",
    "barista", "fiddler", "bishop", "bureaucrat",
})

# ─── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="BotC Roster Fixer",
    page_icon="🔧",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Data loaders (cached) ────────────────────────────────────────────────────


@st.cache_data
def load_players() -> list[str]:
    if not PLAYERS_TXT.exists():
        return []
    seen: set[str] = set()
    result: list[str] = []
    for ln in PLAYERS_TXT.read_text(encoding="utf-8").splitlines():
        name = ln.strip()
        if not name:
            continue
        canonical = resolve_player_name(name, _PLAYER_ALIASES)
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


@st.cache_data
def load_roles() -> list[str]:
    if not ROLES_TXT.exists():
        return []
    return [
        ln.strip()
        for ln in ROLES_TXT.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


@st.cache_data
def load_playlist() -> list[dict]:
    if not PLAYLIST.exists():
        return []
    try:
        return json.loads(PLAYLIST.read_text(encoding="utf-8")).get("entries", [])
    except Exception:
        return []


@st.cache_data
def load_segments(vid: str) -> pd.DataFrame:
    for name in ("segments_patched.csv", "segments.csv"):
        p = OUTPUTS_DIR / vid / name
        if p.exists():
            return pd.read_csv(p)
    return pd.DataFrame(columns=["start", "end", "speaker", "text"])


def load_overrides(vid: str) -> dict:
    f = OUTPUTS_DIR / vid / "roster_overrides.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_overrides(vid: str, data: dict) -> None:
    f = OUTPUTS_DIR / vid / "roster_overrides.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def save_intro_roster(vid: str, data: dict) -> None:
    f = OUTPUTS_DIR / vid / "intro_roster.json"
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ─── Issue scanning ───────────────────────────────────────────────────────────


def _is_garbled(name: str) -> bool:
    if not name:
        return True
    lc = name.strip().lower()
    if _DIGIT_RE.search(name):
        return True
    if _SINGLE_LTR_RE.match(name):
        return True
    if lc in _KNOWN_ROLES:
        return True
    return False


@st.cache_data
def all_issues(entries_key: tuple) -> list[dict]:
    """Scan every video's output for roster quality issues."""
    issues: list[dict] = []
    seen:   set[tuple] = set()

    # ── 1–3. OCR issues from intro_roster.json ────────────────────────────────
    for vid, title, members in entries_key:
        roster_path = OUTPUTS_DIR / vid / "intro_roster.json"
        if not roster_path.exists():
            continue
        try:
            players = json.loads(roster_path.read_text(encoding="utf-8")).get("players", [])
        except Exception:
            continue

        # Load confirmed_duplicates from roster_overrides so intentional
        # multi-player roles (e.g. two Amnesiacs) don't keep surfacing here.
        overrides_path = OUTPUTS_DIR / vid / "roster_overrides.json"
        confirmed_dups: set[str] = set()
        if overrides_path.exists():
            try:
                ov = json.loads(overrides_path.read_text(encoding="utf-8"))
                confirmed_dups = {
                    r.lower().strip()
                    for r in ov.get("confirmed_duplicates", [])
                }
            except Exception:
                pass

        for p in players:
            for field in ("actual_role", "believed_role"):
                raw = (p.get(field) or "").lower().strip()
                p[field] = _ROLE_ALIASES.get(raw, raw)

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

            if actual and actual != "unknown" and role_counts[actual] > 1 \
                    and actual not in confirmed_dups:
                conflicts = [x for x in role_players[actual] if x != name]
                issues.append({
                    "video_id": vid, "title": title, "members": members,
                    "player_name": name, "actual_role": actual,
                    "believed_role": bel, "frame_time": ft,
                    "issue_type": "duplicate_role",
                    "conflict_players": conflicts,
                    "speaker_id": None, "sample_lines": [], "known_players": [],
                })
                seen.add(key)
                continue

            if not actual or actual == "unknown":
                issues.append({
                    "video_id": vid, "title": title, "members": members,
                    "player_name": name, "actual_role": actual,
                    "believed_role": bel, "frame_time": ft,
                    "issue_type": "unknown_role",
                    "conflict_players": [],
                    "speaker_id": None, "sample_lines": [], "known_players": [],
                })
                seen.add(key)
                continue

            if _is_garbled(name):
                issues.append({
                    "video_id": vid, "title": title, "members": members,
                    "player_name": name, "actual_role": actual,
                    "believed_role": bel, "frame_time": ft,
                    "issue_type": "garbled_name",
                    "conflict_players": [],
                    "speaker_id": None, "sample_lines": [], "known_players": [],
                })
                seen.add(key)

    # ── 4. Unlinked speakers from segments.csv ────────────────────────────────
    for vid, title, members in entries_key:
        segs_path = OUTPUTS_DIR / vid / "segments_patched.csv"
        if not segs_path.exists():
            segs_path = OUTPUTS_DIR / vid / "segments.csv"
        if not segs_path.exists():
            continue

        ov_path   = OUTPUTS_DIR / vid / "roster_overrides.json"
        overrides: dict = {}
        if ov_path.exists():
            try:
                ov_raw    = json.loads(ov_path.read_text(encoding="utf-8"))
                overrides = {
                    k: v for k, v in ov_raw.items()
                    if v.get("name") and v["name"] != k and v["name"] != ""
                }
            except Exception:
                pass

        # All intro players (not just unassigned) — used for the batch dropdown
        all_intro_players: list[dict] = []
        intro_path = OUTPUTS_DIR / vid / "intro_roster.json"
        if intro_path.exists():
            try:
                all_intro_players = json.loads(
                    intro_path.read_text(encoding="utf-8")
                ).get("players", [])
            except Exception:
                pass

        # Intro players not yet assigned to any speaker
        assigned_lc = {v["name"].lower() for v in overrides.values()}
        unassigned_intro = [
            p for p in all_intro_players
            if p.get("name") and p["name"].lower() not in assigned_lc
        ]

        first_ts:    dict[str, float] = {}
        sample_lines: dict[str, list] = {}
        try:
            with segs_path.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    spk = row.get("speaker", "")
                    if not spk or spk in ("speaker_0", "UNKNOWN"):
                        continue
                    if spk in overrides:
                        continue
                    t = float(row.get("start", 0))
                    if spk not in first_ts:
                        first_ts[spk] = t
                        sample_lines[spk] = []
                    if len(sample_lines[spk]) < 4:
                        sample_lines[spk].append((t, row.get("text", "")))
        except Exception:
            continue

        for spk in sorted(first_ts.keys()):
            key = (vid, spk, "unlinked_speaker")
            if key in seen:
                continue
            seen.add(key)
            issues.append({
                "video_id": vid, "title": title, "members": members,
                "player_name": spk,
                "actual_role": "", "believed_role": "",
                "frame_time": first_ts[spk],
                "issue_type": "unlinked_speaker",
                "conflict_players": [],
                "speaker_id": spk,
                "sample_lines": sample_lines.get(spk, []),
                # All unassigned intro players for this video (same list per video)
                "known_players": unassigned_intro,
            })

    return issues


# ─── Build "effective items" list ─────────────────────────────────────────────
# Unlinked speakers are collapsed one-per-video into a "batch" item so the
# user assigns a whole game's cast in a single interaction.
# OCR issues remain one-per-entry.

def build_effective_items(filtered: list[dict]) -> list[dict]:
    """Collapse per-video unlinked speakers into batch items."""
    items: list[dict] = []
    seen_unlinked_vids: set[str] = set()

    for iss in filtered:
        if iss["issue_type"] != "unlinked_speaker":
            items.append({"kind": "ocr", "issue": iss})
            continue

        vid = iss["video_id"]
        if vid in seen_unlinked_vids:
            continue   # already added a batch item for this video
        seen_unlinked_vids.add(vid)

        # Gather ALL unlinked speakers for this video from the full filtered list
        batch = [i for i in filtered if i["issue_type"] == "unlinked_speaker"
                 and i["video_id"] == vid]
        items.append({
            "kind":    "unlinked_batch",
            "video_id": vid,
            "title":   iss["title"],
            "members": iss["members"],
            "speakers": batch,          # list of unlinked-speaker issue dicts
        })

    return items


# ─── Display helpers ──────────────────────────────────────────────────────────


def _yt_url(vid: str, t: float, offset: int = 3) -> str:
    return f"https://www.youtube.com/watch?v={vid}&t={max(0, int(t) - offset)}s"


def _team_chip(role: str) -> str:
    if not role:
        return ""
    is_evil = role.strip().lower() in _EVIL_ROLES
    color   = "#dc2626" if is_evil else "#2563eb"
    label   = "Evil" if is_evil else "Good"
    return (
        f'<span style="background:{color}; color:#fff; padding:1px 7px; '
        f'border-radius:3px; font-size:11px; margin-left:6px; '
        f'vertical-align:middle;">{label}</span>'
    )


def _first_line_for_player(vid: str, player_name: str) -> tuple[float, str]:
    lie_csv = OUTPUTS_DIR / vid / "lie_analysis.csv"
    spk_id  = None
    if lie_csv.exists():
        try:
            ldf    = pd.read_csv(lie_csv)
            name_lc = player_name.lower().strip()
            rows   = ldf[ldf["player_name"].str.lower().str.strip() == name_lc]
            if not rows.empty:
                spk_id = rows.iloc[0]["speaker"]
        except Exception:
            pass
    if spk_id:
        segs = load_segments(vid)
        if not segs.empty and "speaker" in segs.columns:
            rows2 = segs[segs["speaker"] == spk_id].sort_values("start")
            if not rows2.empty:
                r = rows2.iloc[0]
                return float(r["start"]), str(r["text"])
    return 0.0, ""


# ─── App ──────────────────────────────────────────────────────────────────────

all_players = load_players()
all_roles   = load_roles()
roles_lc    = {r.lower(): r for r in all_roles}

playlist    = load_playlist()
entries_key = tuple(
    (e["id"], e.get("title", e["id"]), e.get("members", False))
    for e in playlist
) if playlist else ()

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 🔧 BotC Roster Fixer")
    st.caption("Fix missing or incorrect roster data.")
    st.divider()

    st.markdown("**Issue types**")
    filter_unlinked  = st.checkbox("🔗 Unlinked speakers",    value=True, key="f_unlinked")
    filter_duplicate = st.checkbox("🔁 Duplicate roles",      value=True, key="f_dup")
    filter_unknown   = st.checkbox("❓ Unknown / blank role",  value=True, key="f_unk")
    filter_garbled   = st.checkbox("⚠️ Garbled player name",   value=True, key="f_garb")

    st.divider()
    filter_members = st.radio(
        "Game type",
        ["All games", "Main channel only", "Members only"],
        index=0, key="f_mbr",
    )

    st.divider()
    filter_text = st.text_input("Search title / video ID", "", key="f_txt")

    st.divider()
    if st.button("🔄 Rescan issues", use_container_width=True):
        all_issues.clear()
        load_segments.clear()
        st.rerun()

    st.divider()
    st.markdown("**After fixing, sync the database:**")
    st.code(
        "python analyze_roles.py --all\n"
        "python build_db.py\n"
        "python generate_landing.py",
        language="bash",
    )

# ── Guard ─────────────────────────────────────────────────────────────────────

if not entries_key:
    st.error(
        "**No playlist loaded.**  "
        "Run `python fetch_playlist.py` to build `playlist.json` first."
    )
    st.stop()

# ── Build issue list ──────────────────────────────────────────────────────────

issues = all_issues(entries_key)

type_filter: set[str] = set()
if filter_unlinked:  type_filter.add("unlinked_speaker")
if filter_duplicate: type_filter.add("duplicate_role")
if filter_unknown:   type_filter.add("unknown_role")
if filter_garbled:   type_filter.add("garbled_name")

filtered: list[dict] = [
    iss for iss in issues
    if iss["issue_type"] in type_filter
    and (
        filter_members == "All games"
        or (filter_members == "Members only"      and     iss["members"])
        or (filter_members == "Main channel only" and not iss["members"])
    )
    and (
        not filter_text
        or filter_text.lower() in iss["video_id"].lower()
        or filter_text.lower() in iss["title"].lower()
    )
]

# Collapse unlinked speakers by game
items = build_effective_items(filtered)

# ── Done / empty states ───────────────────────────────────────────────────────

if not type_filter:
    st.info("Select at least one issue type in the sidebar to get started.")
    st.stop()

if not items:
    st.success("## ✅ All done!")
    st.markdown(
        f"No issues found matching current filters "
        f"({len(issues)} scanned across {len(entries_key)} videos)."
    )
    st.balloons()
    st.markdown(
        "**Sync the database:**\n"
        "```bash\npython analyze_roles.py --all\n"
        "python build_db.py\npython generate_landing.py\n```"
    )
    st.stop()

# ── Session state ─────────────────────────────────────────────────────────────

if "fix_idx" not in st.session_state:
    st.session_state["fix_idx"] = 0

idx   = max(0, min(int(st.session_state["fix_idx"]), len(items) - 1))
st.session_state["fix_idx"] = idx
total = len(items)

# Count underlying issues for the progress label
total_raw = len(filtered)
done_raw  = sum(
    len(it["speakers"]) if it["kind"] == "unlinked_batch" else 1
    for it in items[:idx]
)
current_raw = (
    len(items[idx]["speakers"]) if items[idx]["kind"] == "unlinked_batch" else 1
)

# ── Progress bar + navigation ─────────────────────────────────────────────────

col_prev, col_prog, col_next = st.columns([1, 8, 1])

with col_prev:
    if st.button("◀", disabled=(idx == 0), use_container_width=True, help="Previous"):
        st.session_state["fix_idx"] = idx - 1
        st.rerun()

with col_next:
    if st.button("▶", disabled=(idx >= total - 1), use_container_width=True, help="Next"):
        st.session_state["fix_idx"] = idx + 1
        st.rerun()

with col_prog:
    pct = idx / total if total > 1 else 0.0
    label_detail = (
        f"  ({current_raw} speaker{'s' if current_raw > 1 else ''})"
        if items[idx]["kind"] == "unlinked_batch" and current_raw > 1
        else ""
    )
    st.progress(
        pct,
        text=(
            f"**{idx + 1} of {total}** items remaining{label_detail}"
            f" &nbsp;·&nbsp; {total_raw} underlying issues"
        ),
    )

# ── Current item ──────────────────────────────────────────────────────────────

item = items[idx]

# ══════════════════════════════════════════════════════════════════════════════
# UNLINKED BATCH — all speakers for one game on one page
# ══════════════════════════════════════════════════════════════════════════════

if item["kind"] == "unlinked_batch":
    vid      = item["video_id"]
    title    = item["title"]
    mbr      = item["members"]
    speakers = item["speakers"]          # list of unlinked-speaker issue dicts

    # All unassigned intro players for this video (same across all speakers)
    known_players: list[dict] = speakers[0].get("known_players", []) if speakers else []
    player_role_map: dict[str, dict] = {
        p["name"]: p for p in known_players if p.get("name")
    }

    # Header
    mbr_html = (
        '<span style="background:#7c3aed; color:#fff; padding:2px 9px; '
        'border-radius:4px; font-size:12px; margin-right:6px;">🔒 Members</span>'
        if mbr else
        '<span style="background:#e5e7eb; color:#444; padding:2px 9px; '
        'border-radius:4px; font-size:12px; margin-right:6px;">📺 Main channel</span>'
    )
    n_spk = len(speakers)
    n_unassigned = len(known_players)
    st.markdown(
        f'<div style="margin-bottom:6px;">{mbr_html}'
        f'<span style="background:#ffe4e6; color:#111; padding:2px 9px; '
        f'border-radius:4px; font-size:12px;">🔗 {n_spk} unlinked speaker'
        f'{"s" if n_spk > 1 else ""}</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown(f"### [{title}](https://www.youtube.com/watch?v={vid})")
    st.caption(f"Video ID: `{vid}`")

    # Blind-game warning
    blind_vids = {e["id"] for e in playlist if e.get("blind")}
    if vid in blind_vids:
        st.warning(
            "**BLIND GAME** — players didn't know their roles during this game. "
            "Role auto-fill is not available, but speaker → player assignment "
            "via Storyteller detection and manual linking still works.",
            icon="🙈",
        )

    # Context bar: intro roster summary
    if known_players:
        intro_names = ", ".join(p["name"] for p in known_players)
        st.info(
            f"Intro OCR found **{n_unassigned}** unassigned player"
            f"{'s' if n_unassigned != 1 else ''} in this game: {intro_names}",
            icon="📋",
        )
    else:
        st.warning(
            "No intro roster data for this game — you'll need to identify "
            "speakers from the transcript lines alone.",
            icon="⚠️",
        )

    st.divider()

    # ── One card per speaker ───────────────────────────────────────────────────
    # Build a unique key namespace per (video, render)
    batch_key = f"{vid}__{idx}"

    # Collect all assignments before rendering so we can save together
    # (Streamlit renders widgets top-to-bottom; we read their values after)

    for spk_idx, spk_iss in enumerate(speakers):
        spk_id       = spk_iss["speaker_id"]
        sample_lines = spk_iss.get("sample_lines", [])
        ft           = spk_iss["frame_time"]
        spk_key      = f"{batch_key}__{spk_id}"

        col_info, col_form = st.columns([3, 2], gap="large")

        with col_info:
            yt_ts = _yt_url(vid, ft)
            # Compact speaker header
            st.markdown(
                f'<div style="background:#f8fafc; border:1px solid #e2e8f0; '
                f'border-radius:8px; padding:10px 16px; margin-bottom:8px;">'
                f'<span style="font-weight:700; font-size:1.05em;">🎙️ <code>{spk_id}</code></span>'
                f' &nbsp;<span style="color:#64748b; font-size:0.88em;">'
                f'first at {ft:.1f}s &nbsp;'
                f'<a href="{yt_ts}" target="_blank">▶ jump</a></span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            if sample_lines:
                for t_line, txt in sample_lines:
                    ts_str  = f"{int(t_line // 60):02d}:{t_line % 60:05.2f}"
                    yt_line = _yt_url(vid, t_line, offset=2)
                    st.markdown(
                        f'<div style="background:#f0f9ff; color:#0c4a6e; '
                        f'border-left:3px solid #0ea5e9; padding:5px 10px; '
                        f'border-radius:4px; font-style:italic; '
                        f'margin-top:4px; font-size:0.9em;">'
                        f'<a href="{yt_line}" target="_blank" '
                        f'style="color:#64748b; font-style:normal; text-decoration:none;">'
                        f'⏱ {ts_str}</a>'
                        f' &nbsp;"<span>{txt[:220]}</span>"'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
            else:
                st.caption("*(no transcript lines)*")

        with col_form:
            player_opts = [_SKIP, _STORYTELLER] + sorted(all_players)
            chosen = st.selectbox(
                f"Who is {spk_id}?",
                options=player_opts,
                key=f"player_{spk_key}",
            )

            intro_hint_actual = ""
            intro_hint_bel    = ""
            if chosen not in (_SKIP, _STORYTELLER) and chosen in player_role_map:
                p_info            = player_role_map[chosen]
                intro_hint_actual = (p_info.get("actual_role")   or "").lower()
                intro_hint_bel    = (p_info.get("believed_role") or "").lower()

            if chosen == _STORYTELLER:
                st.caption("Storyteller — won't affect lie statistics.")
            elif chosen != _SKIP:
                if intro_hint_actual:
                    hint = f"Intro card: **{chosen}** → `{intro_hint_actual}`"
                    if intro_hint_bel and intro_hint_bel != intro_hint_actual:
                        hint += f" (believed: `{intro_hint_bel}`)"
                    st.caption(hint)

                role_opts    = [""] + all_roles
                role_default = 0
                if intro_hint_actual:
                    canonical = roles_lc.get(intro_hint_actual, "")
                    if canonical in all_roles:
                        role_default = all_roles.index(canonical) + 1

                st.selectbox(
                    "Actual role",
                    options=role_opts,
                    index=role_default,
                    key=f"role_{spk_key}_{chosen}",
                )

                bel_opts    = ["Same as actual"] + all_roles
                bel_default = 0
                if intro_hint_bel and intro_hint_bel != intro_hint_actual:
                    canonical_bel = roles_lc.get(intro_hint_bel, "")
                    if canonical_bel in all_roles:
                        bel_default = all_roles.index(canonical_bel) + 1
                st.selectbox(
                    "Believed role",
                    options=bel_opts,
                    index=bel_default,
                    key=f"bel_{spk_key}_{chosen}",
                    help="Only if given a false token (Drunk, Lunatic, etc.)",
                )

        if spk_idx < len(speakers) - 1:
            st.markdown(
                '<hr style="border:none; border-top:1px solid #e2e8f0; margin:16px 0;">',
                unsafe_allow_html=True,
            )

    # ── Save all / skip ───────────────────────────────────────────────────────
    st.divider()
    col_sv, col_sk, col_info2 = st.columns([2, 2, 6])

    with col_sv:
        save_all = st.button(
            "💾 Save all & next",
            use_container_width=True,
            key=f"save_batch_{batch_key}",
            type="primary",
        )
    with col_sk:
        skip_all = st.button(
            "⏭ Skip this game",
            use_container_width=True,
            key=f"skip_batch_{batch_key}",
        )
    with col_info2:
        st.caption(
            "Speakers left on **— skip for now —** will remain unlinked. "
            "Only speakers with a player selected will be saved."
        )

    if save_all:
        ov_data = load_overrides(vid)
        saved   = 0
        for spk_iss in speakers:
            spk_id  = spk_iss["speaker_id"]
            spk_key = f"{batch_key}__{spk_id}"

            chosen_val = st.session_state.get(f"player_{spk_key}", _SKIP)
            if chosen_val == _SKIP:
                continue

            if chosen_val == _STORYTELLER:
                ov_data[spk_id] = {
                    "name": "Storyteller",
                    "actual_role": "storyteller",
                    "believed_role": "storyteller",
                }
                saved += 1
            else:
                role_val = st.session_state.get(f"role_{spk_key}_{chosen_val}", "")
                bel_val  = st.session_state.get(f"bel_{spk_key}_{chosen_val}", "Same as actual")
                actual_out = role_val.strip().lower() if role_val else ""
                bel_out    = (
                    actual_out
                    if bel_val == "Same as actual"
                    else bel_val.strip().lower()
                )
                if chosen_val and actual_out:
                    ov_data[spk_id] = {
                        "name":          chosen_val,
                        "actual_role":   actual_out,
                        "believed_role": bel_out,
                    }
                    saved += 1

        if saved:
            save_overrides(vid, ov_data)
            all_issues.clear()
            st.toast(f"✅ Saved {saved} of {len(speakers)} speakers for this game", icon="💾")
        else:
            st.toast("Nothing to save — all speakers were skipped.", icon="ℹ️")

        # Always advance; remaining unlinked speakers will re-appear on rescan
        st.session_state["fix_idx"] = idx
        st.rerun()

    if skip_all:
        st.session_state["fix_idx"] = min(idx + 1, total - 1)
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# OCR ISSUES: duplicate_role / unknown_role / garbled_name
# ══════════════════════════════════════════════════════════════════════════════

else:
    iss    = item["issue"]
    itype  = iss["issue_type"]
    icon, bg, label = ISSUE_META[itype]

    vid    = iss["video_id"]
    title  = iss["title"]
    mbr    = iss["members"]
    name   = iss["player_name"]
    actual = iss["actual_role"]
    bel    = iss["believed_role"]
    ft     = iss["frame_time"]

    issue_key = f"{vid}__{name}__{idx}"

    is_dup    = itype == "duplicate_role"
    conflicts = iss.get("conflict_players", [])

    # Header
    mbr_html = (
        '<span style="background:#7c3aed; color:#fff; padding:2px 9px; '
        'border-radius:4px; font-size:12px; margin-right:6px;">🔒 Members</span>'
        if mbr else
        '<span style="background:#e5e7eb; color:#444; padding:2px 9px; '
        'border-radius:4px; font-size:12px; margin-right:6px;">📺 Main channel</span>'
    )
    type_html = (
        f'<span style="background:{bg}; color:#111; padding:2px 9px; '
        f'border-radius:4px; font-size:12px;">{icon} {label}</span>'
    )
    st.markdown(
        f'<div style="margin-bottom:6px;">{mbr_html}{type_html}</div>',
        unsafe_allow_html=True,
    )
    st.markdown(f"### [{title}](https://www.youtube.com/watch?v={vid})")
    st.caption(f"Video ID: `{vid}`")
    st.divider()

    col_info, col_form = st.columns([3, 2], gap="large")

    # ── Info panel ────────────────────────────────────────────────────────────
    with col_info:
        if is_dup and conflicts:
            st.markdown(
                f'<div style="background:#fde8d0; border-left:4px solid #f97316; '
                f'padding:10px 14px; border-radius:6px; margin-bottom:12px;">'
                f'<strong>🔁 Role conflict</strong><br>'
                f'Role <code>{actual}</code> is also assigned to: '
                f'<strong>{", ".join(conflicts)}</strong>'
                f'</div>',
                unsafe_allow_html=True,
            )

        yt_ts    = _yt_url(vid, ft)
        deceived = (actual in _DECEIVABLE_ROLES and bel and actual != bel)
        st.markdown(
            f'<div style="background:#f8fafc; border:1px solid #e2e8f0; '
            f'border-radius:8px; padding:14px 18px; margin-bottom:12px;">'
            f'<div style="font-size:1.15em; font-weight:700; margin-bottom:6px;">'
            f'👤 {name}</div>'
            f'<div>Actual role: &nbsp;<strong>{actual or "—"}</strong>'
            + _team_chip(actual) +
            f'</div>'
            f'<div>Believed role: &nbsp;<strong>{bel or "—"}</strong>'
            + (' &nbsp;⚠️ <em>(deceived)</em>' if deceived else '') +
            f'</div>'
            f'<div style="color:#64748b; font-size:0.9em; margin-top:8px;">'
            f'Detected at <strong>{ft:.1f}s</strong>'
            f' &nbsp;|&nbsp; '
            f'<a href="{yt_ts}" target="_blank">▶ Jump to video</a>'
            f'</div></div>',
            unsafe_allow_html=True,
        )

        fl_ts, fl_txt = _first_line_for_player(vid, name)
        if fl_txt:
            ts_str  = f"{int(fl_ts // 60):02d}:{fl_ts % 60:05.2f}"
            yt_line = _yt_url(vid, fl_ts, offset=2)
            st.markdown("**First spoken line:**")
            st.markdown(
                f'<div style="background:#f0f9ff; color:#0c4a6e; '
                f'border-left:3px solid #0ea5e9; padding:8px 12px; '
                f'border-radius:4px; font-style:italic; font-size:0.92em;">'
                f'<a href="{yt_line}" target="_blank" '
                f'style="color:#64748b; font-style:normal; text-decoration:none;">'
                f'⏱ {ts_str}</a>'
                f' &nbsp;"<span>{fl_txt[:280]}</span>"'
                f'</div>',
                unsafe_allow_html=True,
            )

        if is_dup:
            for cp in conflicts:
                cp_ts, cp_txt = _first_line_for_player(vid, cp)
                yt_cp = _yt_url(vid, cp_ts, offset=2) if cp_ts else "#"
                ts_cp = f"{int(cp_ts//60):02d}:{cp_ts%60:05.2f}" if cp_ts else "—"
                st.markdown(
                    f'<div style="background:#fff7ed; color:#7c2d12; '
                    f'border-left:3px solid #f97316; '
                    f'padding:8px 12px; border-radius:4px; margin-top:8px;">'
                    f'<strong>{cp}</strong> also has <code>{actual}</code><br>'
                    f'<a href="{yt_cp}" target="_blank" '
                    f'style="color:#92400e; font-size:0.9em; text-decoration:none;">'
                    f'⏱ {ts_cp}</a> &nbsp;'
                    f'<em>'
                    + (f'"{cp_txt[:200]}"' if cp_txt else "(no transcript line)")
                    + '</em></div>',
                    unsafe_allow_html=True,
                )

    # ── Form panel ────────────────────────────────────────────────────────────
    with col_form:
        st.markdown("**✏️ Fix this entry**")

        name_opts = ["(keep current name)"] + sorted(all_players)
        name_idx  = 0
        for i, n in enumerate(name_opts):
            if n.lower() == name.lower():
                name_idx = i
                break

        new_name_sel = st.selectbox(
            "Player name", options=name_opts, index=name_idx,
            key=f"name_{issue_key}",
        )

        role_opts = [""] + all_roles
        role_idx  = 0
        if actual:
            canonical = roles_lc.get(actual.lower(), "")
            if canonical in all_roles:
                role_idx = all_roles.index(canonical) + 1

        new_actual_sel = st.selectbox(
            "Actual role", options=role_opts, index=role_idx,
            key=f"actual_{issue_key}",
        )

        bel_opts = ["Same as actual"] + all_roles
        bel_idx  = 0
        if bel and bel != actual:
            canonical_bel = roles_lc.get(bel.lower(), "")
            if canonical_bel in all_roles:
                bel_idx = all_roles.index(canonical_bel) + 1

        new_bel_sel = st.selectbox(
            "Believed role", options=bel_opts, index=bel_idx,
            key=f"bel_{issue_key}",
            help="Only if given a false token.",
        )

        remove = st.checkbox("Remove this entry entirely", key=f"remove_{issue_key}")

        st.markdown("")
        col_sv2, col_sk2 = st.columns(2)
        with col_sv2:
            save_btn2 = st.button(
                "💾 Save & Next", use_container_width=True,
                key=f"save2_{issue_key}", type="primary",
            )
        with col_sk2:
            skip_btn2 = st.button(
                "⏭ Skip", use_container_width=True,
                key=f"skip2_{issue_key}",
            )

        if save_btn2:
            roster_path = OUTPUTS_DIR / vid / "intro_roster.json"
            if not roster_path.exists():
                st.error(f"`intro_roster.json` not found for `{vid}`.")
            else:
                data    = json.loads(roster_path.read_text(encoding="utf-8"))
                players = data.get("players", [])

                if remove:
                    players = [
                        p for p in players
                        if not (
                            p.get("name", "").lower() == name.lower()
                            and (p.get("actual_role") or "").lower() == actual.lower()
                            and abs(float(p.get("frame_time", 0)) - ft) < 1.0
                        )
                    ]
                else:
                    fixed_name   = (
                        name if new_name_sel == "(keep current name)" else new_name_sel
                    )
                    fixed_actual = (
                        new_actual_sel.strip().lower() if new_actual_sel else actual
                    )
                    fixed_bel = (
                        fixed_actual
                        if new_bel_sel == "Same as actual"
                        else new_bel_sel.strip().lower()
                    )
                    matched = False
                    for p in players:
                        if (
                            p.get("name", "").lower() == name.lower()
                            and (p.get("actual_role") or "").lower() == actual.lower()
                            and abs(float(p.get("frame_time", 0)) - ft) < 1.0
                        ):
                            p["name"]          = fixed_name
                            p["actual_role"]   = fixed_actual
                            p["believed_role"] = fixed_bel
                            matched = True
                    if not matched:
                        st.error(
                            f"Could not find entry "
                            f"(name={name!r}, role={actual!r}, t={ft:.1f})"
                        )
                        st.stop()

                data["players"] = players
                save_intro_roster(vid, data)
                all_issues.clear()
                load_segments.clear()
                action = "Removed" if remove else "Saved"
                st.toast(f"✅ {action} {name} ({actual})", icon="💾")
                st.rerun()

        if skip_btn2:
            st.session_state["fix_idx"] = min(idx + 1, total - 1)
            st.rerun()

# ── Issue summary table ───────────────────────────────────────────────────────

st.divider()
with st.expander(f"📋 All {len(items)} items in current filter ({total_raw} underlying issues)", expanded=False):
    rows = []
    for i, it in enumerate(items):
        if it["kind"] == "unlinked_batch":
            spk_ids = ", ".join(s["speaker_id"] for s in it["speakers"])
            rows.append({
                "#":      i + 1,
                "Type":   f"🔗 {len(it['speakers'])} unlinked speakers",
                "Game":   it["title"][:55],
                "Members": "🔒" if it["members"] else "📺",
                "Detail": spk_ids[:80],
            })
        else:
            iss2 = it["issue"]
            ico2, _, lbl2 = ISSUE_META[iss2["issue_type"]]
            rows.append({
                "#":      i + 1,
                "Type":   f"{ico2} {lbl2}",
                "Game":   iss2["title"][:55],
                "Members": "🔒" if iss2["members"] else "📺",
                "Detail": f'{iss2["player_name"]} / {iss2["actual_role"] or "—"}',
            })
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
        column_config={"#": st.column_config.NumberColumn(width="small")},
    )
