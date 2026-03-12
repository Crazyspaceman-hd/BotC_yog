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

from pipeline_utils import load_player_aliases, resolve_player_name, display_role
from botc_ui import _team, _ROLES as _ROLE_DATA

_PLAYER_ALIASES: dict[str, str] = load_player_aliases()


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

-- Canonical role registry — single source of truth for team + type.
-- Seeded from botc_ui._ROLES on every build_db run.
-- Use this table to JOIN against lies/roster instead of hardcoding team logic.
CREATE TABLE IF NOT EXISTS roles (
    name  TEXT PRIMARY KEY,  -- display name e.g. "Plague Doctor"
    team  TEXT NOT NULL,     -- 'Good' or 'Evil'
    type  TEXT NOT NULL      -- 'Townsfolk' | 'Outsider' | 'Minion' | 'Demon' | 'Traveller' | 'Fabled'
);

-- N2: game-phase boundaries (from detect_phases.py)
CREATE TABLE IF NOT EXISTS phase_labels (
    rowid      INTEGER PRIMARY KEY,
    video_id   TEXT NOT NULL REFERENCES videos(id),
    start      REAL,
    end        REAL,
    phase      TEXT,        -- 'Intro' | 'Night' | 'Day' | 'Nomination' | 'Execution'
    confidence REAL,
    evidence   TEXT
);
CREATE INDEX IF NOT EXISTS idx_phase_vid ON phase_labels(video_id);

-- N3: role claims, accusations, suspicions (from extract_claims.py)
CREATE TABLE IF NOT EXISTS claims (
    rowid           INTEGER PRIMARY KEY,
    video_id        TEXT NOT NULL REFERENCES videos(id),
    event_id        TEXT,
    timestamp_start TEXT,
    speaker         TEXT,
    player_name     TEXT,
    target_player   TEXT,
    event_type      TEXT,   -- 'role_claim' | 'accusation' | 'suspicion' | 'agreement' | 'challenge'
    claim_text      TEXT,
    claimed_role    TEXT,
    actual_role     TEXT,
    verified_lie    INTEGER DEFAULT 0,  -- 0 = false, 1 = true
    phase           TEXT,
    confidence      REAL
);
CREATE INDEX IF NOT EXISTS idx_claims_vid  ON claims(video_id);
CREATE INDEX IF NOT EXISTS idx_claims_type ON claims(event_type);
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

    # ── roles (canonical registry — re-seeded on every run) ───────────────────
    con.execute("DELETE FROM roles")
    con.executemany(
        "INSERT INTO roles(name, team, type) VALUES (?, ?, ?)",
        [(display_role(name), team, rtype) for name, (team, rtype) in _ROLE_DATA.items()],
    )
    print(f"  {con.execute('SELECT COUNT(*) FROM roles').fetchone()[0]} rows in roles")

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
                        _team(r.get("actual_role", "")),
                        display_role(r.get("actual_role", "")),
                        display_role(r.get("believed_role", "")),
                        display_role(r.get("claimed_role", "")),
                        r.get("verdict", ""),
                        r.get("text", ""),
                    )
                    for r in rows
                ],
            )
            lies_total += len(rows)

        # ── segments (prefer most-processed version) ──────────────────────────
        seg_csv = next(
            (out / n for n in ("segments_consistent.csv", "segments_patched.csv", "segments.csv")
             if (out / n).exists()),
            None,
        )
        if seg_csv is not None:
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
                            display_role(p.get("actual_role", "")),
                            display_role(p.get("believed_role", "")),
                            p.get("frame_time", 0.0),
                        )
                        for p in players
                    ],
                )
                roster_total += len(players)
            except Exception as exc:
                print(f"  [WARN] roster {vid}: {exc}")

    # ── phase_labels (N2 — from detect_phases.py) ────────────────────────────
    phase_total = 0
    for e in entries:
        vid = e["id"]
        phase_csv = Path("outputs") / vid / "phase_labels.csv"
        if phase_csv.exists():
            con.execute("DELETE FROM phase_labels WHERE video_id = ?", (vid,))
            rows = read_csv(phase_csv)
            con.executemany(
                "INSERT INTO phase_labels(video_id, start, end, phase, confidence, evidence) "
                "VALUES (?,?,?,?,?,?)",
                [
                    (vid, float(r.get("start", 0)), float(r.get("end", 0)),
                     r.get("phase", ""), float(r.get("confidence", 0)), r.get("evidence", ""))
                    for r in rows
                ],
            )
            phase_total += len(rows)

    # ── claims (N3 — from extract_claims.py) ─────────────────────────────────
    claims_total = 0
    for e in entries:
        vid = e["id"]
        claims_csv = Path("outputs") / vid / "claims.csv"
        if claims_csv.exists():
            con.execute("DELETE FROM claims WHERE video_id = ?", (vid,))
            rows = read_csv(claims_csv)
            con.executemany(
                "INSERT INTO claims(video_id, event_id, timestamp_start, speaker, player_name, "
                "target_player, event_type, claim_text, claimed_role, actual_role, "
                "verified_lie, phase, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        vid,
                        r.get("event_id", ""),
                        r.get("timestamp_start", ""),
                        r.get("speaker", ""),
                        resolve_player_name(r.get("player_name", ""), _PLAYER_ALIASES),
                        resolve_player_name(r.get("target_player", ""), _PLAYER_ALIASES),
                        r.get("event_type", ""),
                        r.get("claim_text", ""),
                        display_role(r.get("claimed_role", "")),
                        display_role(r.get("actual_role", "")),
                        1 if str(r.get("verified_lie", "false")).lower() == "true" else 0,
                        r.get("phase", ""),
                        float(r.get("confidence", 0)),
                    )
                    for r in rows
                ],
            )
            claims_total += len(rows)

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
                 display_role(ov.get("actual_role", "")), display_role(ov.get("believed_role", "")))
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
        f"roster_players={roster_total:,}   speaker_map={spkmap_total:,}\n"
        f"  phase_labels={phase_total:,}   claims={claims_total:,}"
    )
    print(f"  Built at: {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build botc.db from pipeline outputs")
    ap.add_argument("--db", default="botc.db", help="Output SQLite database path")
    args = ap.parse_args()
    build(Path(args.db))
