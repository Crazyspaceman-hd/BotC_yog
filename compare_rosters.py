"""
compare_rosters.py  --  diff our DB roster against the external spreadsheet data.

Spreadsheet data lives in sheet_data.csv (fetched once from the public Google Sheet).
Columns: Episode, Player, Role, Alignment, Type, Winner, Public

Run:  python compare_rosters.py
"""
import csv
import sqlite3
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path


# ── Load spreadsheet data ──────────────────────────────────────────────────────

def load_sheet(path: str = "sheet_data.csv"):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows

SHEET_ROWS = load_sheet()

# Spreadsheet uses short/alternate names that differ from our canonical player names.
# Map sheet name (lowercase) -> our canonical name.
SHEET_NAME_ALIASES = {
    "tom":   "Tom C",    # spreadsheet uses "Tom"; our intro cards show "Tom C"
    "smith": "Smithy",   # spreadsheet uses "Smith"; intro cards show "Smithy"
    "alex":  "Alex",     # some entries are "ALEX" in the sheet; canonical is "Alex"
}


# ── DB query ───────────────────────────────────────────────────────────────────

con = sqlite3.connect("botc.db")
con.row_factory = sqlite3.Row

db_rows = con.execute("""
    SELECT v.title, v.id as video_id, r.player_name as player, r.actual_role
    FROM roster r
    JOIN videos v ON r.video_id = v.id
    WHERE r.actual_role NOT IN ('unknown','Storyteller')
      AND r.player_name IS NOT NULL
    ORDER BY v.title, r.player_name
""").fetchall()

# Build lookup: normalised_title -> {player_lower -> actual_role}
db_by_title  = defaultdict(dict)
db_title_map = {}  # normalised -> original


def normalise(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\s*[-|]\s*(blood on the clocktower.*|bonus.*)", "", s)
    s = re.sub(r"[!?']", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


for r in db_rows:
    nt = normalise(r["title"])
    db_by_title[nt][r["player"].lower()] = r["actual_role"]
    db_title_map[nt] = r["title"]

db_norm_titles_set = set(db_by_title.keys())

# Build video_id -> normalised_title for override lookups.
# Use all videos (not just those with roster entries) so overrides work for
# videos that have an intro_roster.json but haven't been DB-built yet.
all_videos = con.execute("SELECT id, title FROM videos").fetchall()
vid_to_nt = {v["id"]: normalise(v["title"]) for v in all_videos}
# Also ensure db_title_map covers all videos (even those with 0 roster rows)
for v in all_videos:
    nt = normalise(v["title"])
    if nt not in db_title_map:
        db_title_map[nt] = v["title"]

# Include ALL video titles in fuzzy search, even those with 0 roster rows.
# Without this, videos that exist in the DB but have no roster yet are
# invisible to fuzzy matching and always score against the wrong title.
db_norm_titles = list(db_norm_titles_set | set(vid_to_nt.values()))

# Explicit overrides for episodes whose titles are too similar to other DB titles
# to fuzzy-match correctly.  Maps sheet episode title -> video_id.
SHEET_TITLE_OVERRIDES = {
    "Lewis is finally a player in Blood on the Clocktower in Minecraft!": "tf_LO5NKKUU",
    "This is how we record Blood on the Clocktower in Minecraft!":        "OYTaTtjk3ac",
    "Nilesy's annoying wish in Blood on the Clocktower in Minecraft!":    "HQlYPDUfM4Q",
    "Who's who? Another blind Blood on the Clocktower Bonus!":            "ggM9BH__xtU",
    "No one knows their roles! (Game 1)":                                 "d2M-N5iABRo",
    "No one knows their roles! (Game 2)":                                 "d2M-N5iABRo",
}


def best_db_match(sheet_ep: str):
    # Explicit override wins unconditionally
    if sheet_ep in SHEET_TITLE_OVERRIDES:
        vid = SHEET_TITLE_OVERRIDES[sheet_ep]
        nt  = vid_to_nt.get(vid)
        if nt:
            return nt, 1.0
    n = normalise(sheet_ep)
    # Exact normalised match
    if n in db_by_title:
        return n, 1.0
    best, best_score = None, 0.0
    for nt in db_norm_titles:
        score = SequenceMatcher(None, n, nt).ratio()
        if score > best_score:
            best_score, best = score, nt
    return best, best_score


# ── Match episodes ─────────────────────────────────────────────────────────────

sheet_episodes = sorted({r["Episode"] for r in SHEET_ROWS})
matched   = {}   # sheet_ep -> db_norm_title
unmatched = []

print("=== Episode matching ===")
for ep in sheet_episodes:
    nt, score = best_db_match(ep)
    if score < 0.60:
        unmatched.append(ep)
        print(f"  NO MATCH ({score:.2f}): {ep!r}")
    else:
        matched[ep] = nt
        if score < 0.90:
            print(f"  LOW ({score:.2f}): {ep!r}")
            print(f"          -> {db_title_map.get(nt, nt)!r}")

print(f"\nMatched {len(matched)}/{len(sheet_episodes)} episodes"
      f"  ({len(unmatched)} unmatched)\n")


# ── Run diff ───────────────────────────────────────────────────────────────────

def resolve_sheet_name(name: str) -> str:
    """Map spreadsheet player name to our canonical name."""
    return SHEET_NAME_ALIASES.get(name.lower(), name)

sheet_per_ep = defaultdict(dict)   # sheet_ep -> {canonical_player_lower -> role}
for r in SHEET_ROWS:
    canonical = resolve_sheet_name(r["Player"]).lower()
    sheet_per_ep[r["Episode"]][canonical] = r["Role"]

discrepancies = []

# Sheet entries vs DB
for r in SHEET_ROWS:
    ep     = r["Episode"]
    player = resolve_sheet_name(r["Player"])
    s_role = r["Role"]
    nt = matched.get(ep)
    if not nt:
        continue
    db_role = db_by_title[nt].get(player.lower())
    if db_role is None:
        discrepancies.append((ep, player, s_role, "(MISSING FROM DB)"))
    elif s_role.lower().strip() != db_role.lower().strip():
        discrepancies.append((ep, player, s_role, db_role))

# DB entries not in sheet (for matched games only)
for ep, nt in matched.items():
    for player_lower, db_role in db_by_title[nt].items():
        if player_lower not in sheet_per_ep[ep] and player_lower != "seat 7":
            discrepancies.append((ep, player_lower.title(), "(not in sheet)", db_role))

# ── Report ─────────────────────────────────────────────────────────────────────

print("=== Role discrepancies ===")
if not discrepancies:
    print("  ✓ No discrepancies found!")
else:
    by_ep = defaultdict(list)
    for ep, player, s_r, db_r in discrepancies:
        by_ep[ep].append((player, s_r, db_r))
    for ep in sorted(by_ep):
        db_title = db_title_map.get(matched.get(ep, ""), "?")
        print(f"\n  [{ep}]  ->  DB: {db_title}")
        for player, s_r, db_r in sorted(by_ep[ep]):
            print(f"    {player:<14}  sheet={s_r:<28}  db={db_r}")

print(f"\nTotal discrepancies: {len(discrepancies)}")

# ── Episodes only in DB (not in sheet) ────────────────────────────────────────
matched_db_titles = set(matched.values())
db_only = [db_title_map[nt] for nt in db_norm_titles if nt not in matched_db_titles]
if db_only:
    print(f"\n=== In DB but not in spreadsheet ({len(db_only)}) ===")
    for t in sorted(db_only):
        print(f"  {t}")
