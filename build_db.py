"""build_db.py — Build (or rebuild) botc.db from all pipeline outputs.

Run this after any analyze_roles.py / scrape_intro.py run to sync the
database with the latest CSV / JSON files.

Usage:
    python build_db.py [--db PATH]       default: botc.db
"""

import argparse
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from pipeline_utils import load_player_aliases, resolve_player_name

_PLAYER_ALIASES: dict[str, str] = load_player_aliases()

# ── Evil-role lookup (mirrors explore_public.py) ──────────────────────────────
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


def team_for(role: str) -> str:
    return "Evil" if str(role).strip().lower() in _EVIL_ROLES else "Good"


# ── DDL ───────────────────────────────────────────────────────────────────────
DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS videos (
    id           TEXT PRIMARY KEY,
    title        TEXT,
    duration     INTEGER,
    url          TEXT,
    winner       TEXT,           -- 'Good', 'Evil', or NULL
    members      INTEGER DEFAULT 0  -- 1 = members/bonus game
);

CREATE TABLE IF NOT EXISTS lies (
    rowid        INTEGER PRIMARY KEY,
    video_id     TEXT NOT NULL REFERENCES videos(id),
    timestamp    TEXT,           -- "MM:SS.ss"
    speaker      TEXT,           -- "speaker_X"
    player_name  TEXT,
    team         TEXT,           -- 'Good' or 'Evil' (from actual_role)
    actual_role  TEXT,
    believed_role TEXT,
    claimed_role TEXT,
    verdict      TEXT,           -- TRUE | LIE | HONEST MISTAKE | UNVERIFIED
    text         TEXT
);
CREATE INDEX IF NOT EXISTS idx_lies_vid  ON lies(video_id);
CREATE INDEX IF NOT EXISTS idx_lies_pn   ON lies(player_name);
CREATE INDEX IF NOT EXISTS idx_lies_verd ON lies(verdict);

CREATE TABLE IF NOT EXISTS segments (
    rowid        INTEGER PRIMARY KEY,
    video_id     TEXT NOT NULL REFERENCES videos(id),
    start        REAL,
    end          REAL,
    speaker      TEXT,
    text         TEXT
);
CREATE INDEX IF NOT EXISTS idx_seg_vid ON segments(video_id);

-- FTS for segment text (joined so we can filter by video_id too)
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text,
    content=segments,
    content_rowid=rowid
);

CREATE TABLE IF NOT EXISTS roster (
    rowid        INTEGER PRIMARY KEY,
    video_id     TEXT NOT NULL REFERENCES videos(id),
    player_name  TEXT,
    actual_role  TEXT,
    believed_role TEXT,
    frame_time   REAL
);
CREATE INDEX IF NOT EXISTS idx_roster_vid ON roster(video_id);
CREATE INDEX IF NOT EXISTS idx_roster_pn  ON roster(player_name);

-- Manual speaker-to-player assignments (from roster_overrides.json)
CREATE TABLE IF NOT EXISTS speaker_map (
    rowid        INTEGER PRIMARY KEY,
    video_id     TEXT NOT NULL REFERENCES videos(id),
    speaker      TEXT NOT NULL,   -- "speaker_X"
    name         TEXT,
    actual_role  TEXT,
    believed_role TEXT
);
CREATE INDEX IF NOT EXISTS idx_spkmap_vid ON speaker_map(video_id);
"""


# ── helpers ───────────────────────────────────────────────────────────────────

def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ── main ──────────────────────────────────────────────────────────────────────

def build(db_path: Path) -> None:
    playlist_path = Path("playlist.json")
    if not playlist_path.exists():
        raise FileNotFoundError("playlist.json not found — run fetch_playlist.py first")

    raw  = json.loads(playlist_path.read_text(encoding="utf-8"))
    entries = raw if isinstance(raw, list) else raw.get("entries", [])

    con = sqlite3.connect(db_path)
    con.executescript(DDL)

    # ── videos ────────────────────────────────────────────────────────────────
    print(f"Loading {len(entries)} videos …")
    con.executemany(
        "INSERT OR REPLACE INTO videos(id, title, duration, url, winner, members) "
        "VALUES (:id, :title, :duration, :url, :winner, :members)",
        [
            {
                "id":       e["id"],
                "title":    e.get("title", ""),
                "duration": e.get("duration"),
                "url":      e.get("url", ""),
                "winner":   e.get("winner"),
                "members":  1 if e.get("members") else 0,
            }
            for e in entries
        ],
    )
    print(f"  {con.execute('SELECT COUNT(*) FROM videos').fetchone()[0]} rows in videos")

    # ── lies, segments, roster ────────────────────────────────────────────────
    lies_total = segs_total = roster_total = 0

    for e in entries:
        vid = e["id"]
        out = Path("outputs") / vid

        # ── lies ──────────────────────────────────────────────────────────────
        lies_csv = out / "lie_analysis.csv"
        if lies_csv.exists():
            con.execute("DELETE FROM lies WHERE video_id = ?", (vid,))
            rows = read_csv(lies_csv)
            con.executemany(
                "INSERT INTO lies(video_id, timestamp, speaker, player_name, "
                "team, actual_role, believed_role, claimed_role, verdict, text) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        vid,
                        r.get("timestamp", ""),
                        r.get("speaker", ""),
                        resolve_player_name(r.get("player_name", ""), _PLAYER_ALIASES),
                        team_for(r.get("actual_role", "")),
                        r.get("actual_role", "").title(),
                        r.get("believed_role", "").title(),
                        r.get("claimed_role", "").title(),
                        r.get("verdict", ""),
                        r.get("text", ""),
                    )
                    for r in rows
                ],
            )
            lies_total += len(rows)

        # ── segments ──────────────────────────────────────────────────────────
        seg_csv = out / "segments_patched.csv"
        if not seg_csv.exists():
            seg_csv = out / "segments.csv"
        if seg_csv.exists():
            con.execute("DELETE FROM segments WHERE video_id = ?", (vid,))
            rows = read_csv(seg_csv)
            con.executemany(
                "INSERT INTO segments(video_id, start, end, speaker, text) "
                "VALUES (?,?,?,?,?)",
                [
                    (
                        vid,
                        float(r.get("start", 0)),
                        float(r.get("end", 0)),
                        r.get("speaker", ""),
                        r.get("text", ""),
                    )
                    for r in rows
                ],
            )
            segs_total += len(rows)

        # ── roster ────────────────────────────────────────────────────────────
        roster_json = out / "intro_roster.json"
        if roster_json.exists():
            try:
                data = json.loads(roster_json.read_text(encoding="utf-8"))
                con.execute("DELETE FROM roster WHERE video_id = ?", (vid,))
                players = data.get("players", [])
                con.executemany(
                    "INSERT INTO roster(video_id, player_name, actual_role, "
                    "believed_role, frame_time) VALUES (?,?,?,?,?)",
                    [
                        (
                            vid,
                            resolve_player_name(p.get("name", ""), _PLAYER_ALIASES),
                            p.get("actual_role", "").title(),
                            p.get("believed_role", "").title(),
                            p.get("frame_time", 0.0),
                        )
                        for p in players
                    ],
                )
                roster_total += len(players)
            except Exception as exc:
                print(f"  [WARN] roster {vid}: {exc}")

    # ── speaker_map (from roster_overrides.json) ──────────────────────────────
    spkmap_total = 0
    for e in entries:
        vid = e["id"]
        overrides_json = Path("outputs") / vid / "roster_overrides.json"
        if not overrides_json.exists():
            continue
        try:
            overrides = json.loads(overrides_json.read_text(encoding="utf-8"))
            con.execute("DELETE FROM speaker_map WHERE video_id = ?", (vid,))
            rows = [
                (vid, spk,
                 resolve_player_name(ov.get("name", ""), _PLAYER_ALIASES),
                 ov.get("actual_role", "").title(), ov.get("believed_role", "").title())
                for spk, ov in overrides.items()
                if ov.get("name")   # only store entries with a name assigned
            ]
            con.executemany(
                "INSERT INTO speaker_map(video_id, speaker, name, actual_role, believed_role) "
                "VALUES (?,?,?,?,?)",
                rows,
            )
            spkmap_total += len(rows)
        except Exception as exc:
            print(f"  [WARN] speaker_map {vid}: {exc}")

    # ── rebuild FTS index ─────────────────────────────────────────────────────
    print("Rebuilding FTS index …")
    con.execute("INSERT INTO segments_fts(segments_fts) VALUES('rebuild')")

    con.commit()
    con.close()

    size_kb = db_path.stat().st_size // 1024
    print(
        f"\nDone. Built {db_path}  ({size_kb:,} KB)\n"
        f"  lies={lies_total:,}   segments={segs_total:,}   "
        f"roster_players={roster_total:,}   speaker_map={spkmap_total:,}"
    )
    print(f"  Built at: {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build botc.db from pipeline outputs")
    ap.add_argument("--db", default="botc.db", help="Output SQLite database path")
    args = ap.parse_args()
    build(Path(args.db))
