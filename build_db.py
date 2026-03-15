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
    rowid              INTEGER PRIMARY KEY,
    video_id           TEXT NOT NULL REFERENCES videos(id),
    player_name        TEXT,
    actual_role        TEXT,   -- final role at end of game
    initial_actual_role TEXT,  -- role at game start (may differ if role changed)
    believed_role      TEXT,
    role_changed       INTEGER DEFAULT 0,  -- 1 if player changed roles during game
    frame_time         REAL
);
CREATE INDEX IF NOT EXISTS idx_roster_vid ON roster(video_id);
CREATE INDEX IF NOT EXISTS idx_roster_pn  ON roster(player_name);

-- Role changes detected mid-game (from analyze_roles.py / manual entry)
CREATE TABLE IF NOT EXISTS role_changes (
    rowid         INTEGER PRIMARY KEY,
    video_id      TEXT NOT NULL REFERENCES videos(id),
    player_name   TEXT,
    previous_role TEXT,
    new_role      TEXT,
    change_time_s REAL,   -- seconds into the video
    source        TEXT    -- 'nlp' | 'manual'
);
CREATE INDEX IF NOT EXISTS idx_rchg_vid ON role_changes(video_id);

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

-- N2: broad game-phase boundaries (from detect_phases.py)
-- Phases: 'Intro' | 'Night' | 'Day'
-- Nomination and Execution are no longer top-level phases; see day_events.
CREATE TABLE IF NOT EXISTS phase_labels (
    rowid      INTEGER PRIMARY KEY,
    video_id   TEXT NOT NULL REFERENCES videos(id),
    start      REAL,
    end        REAL,
    phase      TEXT,        -- 'Intro' | 'Night' | 'Day'
    round      INTEGER DEFAULT 0,  -- game round (Day 1=1, Night 1=1, Day 2=2 …)
    confidence REAL,
    evidence   TEXT
);
CREATE INDEX IF NOT EXISTS idx_phase_vid ON phase_labels(video_id);

-- N2b: day-scoped events (from detect_phases.py)
-- Events within Day: NominationStart, VoteSequence, ExecutionAnnouncement,
--                    StorytellerInterruption, DayEnd
CREATE TABLE IF NOT EXISTS day_events (
    rowid      INTEGER PRIMARY KEY,
    video_id   TEXT NOT NULL REFERENCES videos(id),
    start      REAL,
    end        REAL,
    round      INTEGER DEFAULT 0,
    event      TEXT,        -- see event types above
    confidence REAL,
    evidence   TEXT
);
CREATE INDEX IF NOT EXISTS idx_devents_vid ON day_events(video_id);

-- N0: visual frame scan (from scan_frames.py) — header bar + votes table visibility
CREATE TABLE IF NOT EXISTS frame_scan (
    rowid      INTEGER PRIMARY KEY,
    video_id   TEXT NOT NULL REFERENCES videos(id),
    t          REAL,            -- timestamp (seconds)
    header_visible INTEGER,     -- 0/1 — player role-bar visible at top
    votes_visible  INTEGER      -- 0/1 — nomination voting table visible
);
CREATE INDEX IF NOT EXISTS idx_fscan_vid ON frame_scan(video_id);

-- N4: player status transitions (from extract_player_status.py)
-- One row per status change; t=0 row marks alive at game start.
CREATE TABLE IF NOT EXISTS player_status (
    rowid           INTEGER PRIMARY KEY,
    video_id        TEXT NOT NULL REFERENCES videos(id),
    timestamp_start REAL,
    player_name     TEXT,
    prior_status    TEXT,   -- 'alive' | 'dead' | 'unknown'
    status          TEXT,   -- 'alive' | 'dead' | 'unknown'
    source          TEXT,   -- 'intro_roster' | 'transcript' | 'visual'
    confidence      REAL
);
CREATE INDEX IF NOT EXISTS idx_pstatus_vid ON player_status(video_id);

-- N4: death events (from extract_player_status.py)
-- One row per detected death with cause classification.
CREATE TABLE IF NOT EXISTS death_events (
    rowid                INTEGER PRIMARY KEY,
    video_id             TEXT NOT NULL REFERENCES videos(id),
    timestamp_start      REAL,
    player_name          TEXT,
    event_type           TEXT,   -- 'execution' | 'night_death' | 'uncertain_death'
    source               TEXT,
    confidence           REAL,
    phase                TEXT,
    source_text          TEXT,
    inferred_round       INTEGER DEFAULT 0,
    storyteller_anchored INTEGER DEFAULT 0,
    header_visible       INTEGER DEFAULT 0,
    linked_status_change INTEGER DEFAULT 0,
    cause_confidence     REAL,
    night_target_evidence INTEGER DEFAULT 0  -- 1 if demon chose this player during preceding Night
);
CREATE INDEX IF NOT EXISTS idx_deathe_vid ON death_events(video_id);

-- N4: intended night-kill targeting events (from extract_player_status.py)
-- Records intended kill targets separately from confirmed deaths.
-- Intended target ≠ actual victim when protection, redirect, or bounce occurs.
CREATE TABLE IF NOT EXISTS night_target_events (
    rowid                    INTEGER PRIMARY KEY,
    video_id                 TEXT NOT NULL REFERENCES videos(id),
    timestamp_start          REAL,
    source_speaker           TEXT,       -- diarization speaker_id
    target_player            TEXT,       -- intended target (canonical player name)
    target_role_if_any       TEXT,       -- role of target from roster (may be empty)
    source_text              TEXT,       -- transcript evidence
    confidence               REAL,
    evidence_type            TEXT,       -- 'named_intent' | 'split_intent'
    candidate_actor_alignment TEXT,      -- 'Evil' | 'Good' | 'unknown' (best-effort)
    actor_hint               TEXT,       -- canonical player name of the actor
    linked_death_player      TEXT,       -- actual victim (may differ from target)
    linked_death_timestamp   REAL,       -- timestamp of actual victim's death
    outcome_relation         TEXT        -- 'matched_actual_death' | 'did_not_match_actual_death'
                                         --   | 'no_confirmed_death' | 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_nte_vid    ON night_target_events(video_id);
CREATE INDEX IF NOT EXISTS idx_nte_target ON night_target_events(target_player);
CREATE INDEX IF NOT EXISTS idx_nte_actor  ON night_target_events(actor_hint);

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

    # ── schema migrations (add columns that may be absent from older DBs) ─────
    _migrations = [
        "ALTER TABLE roster ADD COLUMN initial_actual_role TEXT",
        "ALTER TABLE roster ADD COLUMN role_changed INTEGER DEFAULT 0",
        "ALTER TABLE phase_labels ADD COLUMN round INTEGER DEFAULT 0",
        "ALTER TABLE death_events ADD COLUMN night_target_evidence INTEGER DEFAULT 0",
    ]
    for _sql in _migrations:
        try:
            con.execute(_sql)
        except sqlite3.OperationalError:
            pass  # column already exists

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
                    "initial_actual_role, believed_role, role_changed, frame_time) "
                    "VALUES (?,?,?,?,?,?,?)",
                    [
                        (
                            vid,
                            resolve_player_name(p.get("name", ""), _PLAYER_ALIASES),
                            display_role(p.get("actual_role", "")),
                            display_role(p.get("initial_actual_role") or p.get("actual_role", "")),
                            display_role(p.get("believed_role", "")),
                            1 if p.get("role_history") else 0,
                            p.get("frame_time", 0.0),
                        )
                        for p in players
                    ],
                )
                roster_total += len(players)
            except Exception as exc:
                print(f"  [WARN] roster {vid}: {exc}")

    # ── role_changes (from analyze_roles.py) ─────────────────────────────────
    rchg_total = 0
    for e in entries:
        vid = e["id"]
        rchg_json = Path("outputs") / vid / "role_changes.json"
        if rchg_json.exists():
            try:
                changes = json.loads(rchg_json.read_text(encoding="utf-8"))
                con.execute("DELETE FROM role_changes WHERE video_id = ?", (vid,))
                con.executemany(
                    "INSERT INTO role_changes(video_id, player_name, previous_role, "
                    "new_role, change_time_s, source) VALUES (?,?,?,?,?,?)",
                    [
                        (
                            vid,
                            c.get("player_name", ""),
                            display_role(c.get("previous_role", "")),
                            display_role(c.get("new_role", "")),
                            float(c.get("time", 0)),
                            c.get("source", "nlp"),
                        )
                        for c in changes
                    ],
                )
                rchg_total += len(changes)
            except Exception as exc:
                print(f"  [WARN] role_changes {vid}: {exc}")

    # ── phase_labels (N2 — from detect_phases.py) ────────────────────────────
    phase_total = 0
    for e in entries:
        vid = e["id"]
        phase_csv = Path("outputs") / vid / "phase_labels.csv"
        if phase_csv.exists():
            con.execute("DELETE FROM phase_labels WHERE video_id = ?", (vid,))
            rows = read_csv(phase_csv)
            con.executemany(
                "INSERT INTO phase_labels(video_id, start, end, phase, round, confidence, evidence) "
                "VALUES (?,?,?,?,?,?,?)",
                [
                    (vid, float(r.get("start", 0)), float(r.get("end", 0)),
                     r.get("phase", ""), int(r.get("round", 0)),
                     float(r.get("confidence", 0)), r.get("evidence", ""))
                    for r in rows
                ],
            )
            phase_total += len(rows)

    # ── day_events (N2b — from detect_phases.py) ─────────────────────────────
    devents_total = 0
    for e in entries:
        vid = e["id"]
        devents_csv = Path("outputs") / vid / "day_events.csv"
        if devents_csv.exists():
            con.execute("DELETE FROM day_events WHERE video_id = ?", (vid,))
            rows = read_csv(devents_csv)
            con.executemany(
                "INSERT INTO day_events(video_id, start, end, round, event, confidence, evidence) "
                "VALUES (?,?,?,?,?,?,?)",
                [
                    (vid, float(r.get("start", 0)), float(r.get("end", 0)),
                     int(r.get("round", 0)), r.get("event", ""),
                     float(r.get("confidence", 0)), r.get("evidence", ""))
                    for r in rows
                ],
            )
            devents_total += len(rows)

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

    # ── player_status (N4 — from extract_player_status.py) ───────────────
    pstatus_total = 0
    for e in entries:
        vid = e["id"]
        pstatus_csv = Path("outputs") / vid / "player_status.csv"
        if pstatus_csv.exists():
            con.execute("DELETE FROM player_status WHERE video_id = ?", (vid,))
            rows = read_csv(pstatus_csv)
            con.executemany(
                "INSERT INTO player_status(video_id, timestamp_start, player_name, "
                "prior_status, status, source, confidence) VALUES (?,?,?,?,?,?,?)",
                [
                    (vid, float(r.get("timestamp_start", 0)), r.get("player_name", ""),
                     r.get("prior_status", ""), r.get("status", ""),
                     r.get("source", ""), float(r.get("confidence", 0)))
                    for r in rows
                ],
            )
            pstatus_total += len(rows)

    # ── death_events (N4 — from extract_player_status.py) ────────────────
    deathe_total = 0
    for e in entries:
        vid = e["id"]
        deathe_csv = Path("outputs") / vid / "death_events.csv"
        if deathe_csv.exists():
            con.execute("DELETE FROM death_events WHERE video_id = ?", (vid,))
            rows = read_csv(deathe_csv)
            con.executemany(
                "INSERT INTO death_events(video_id, timestamp_start, player_name, "
                "event_type, source, confidence, phase, source_text, inferred_round, "
                "storyteller_anchored, header_visible, linked_status_change, cause_confidence, "
                "night_target_evidence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        vid,
                        float(r.get("timestamp_start", 0)),
                        r.get("player_name", ""),
                        r.get("event_type", ""),
                        r.get("source", ""),
                        float(r.get("confidence", 0)),
                        r.get("phase", ""),
                        r.get("source_text", ""),
                        int(r.get("inferred_round", 0)),
                        int(r.get("storyteller_anchored", 0)),
                        int(r.get("header_visible", 0)),
                        int(r.get("linked_status_change", 0)),
                        float(r.get("cause_confidence", 0)),
                        int(r.get("night_target_evidence", 0)),
                    )
                    for r in rows
                ],
            )
            deathe_total += len(rows)

    # ── night_target_events (N4 — from extract_player_status.py) ────────────
    nte_total = 0
    for e in entries:
        vid = e["id"]
        nte_csv = Path("outputs") / vid / "night_target_events.csv"
        if nte_csv.exists():
            con.execute("DELETE FROM night_target_events WHERE video_id = ?", (vid,))
            rows = read_csv(nte_csv)
            con.executemany(
                "INSERT INTO night_target_events(video_id, timestamp_start, source_speaker, "
                "target_player, target_role_if_any, source_text, confidence, evidence_type, "
                "candidate_actor_alignment, actor_hint, linked_death_player, "
                "linked_death_timestamp, outcome_relation) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        vid,
                        float(r.get("timestamp_start", 0)),
                        r.get("source_speaker", ""),
                        r.get("target_player", ""),
                        r.get("target_role_if_any", ""),
                        r.get("source_text", ""),
                        float(r.get("confidence", 0)),
                        r.get("evidence_type", ""),
                        r.get("candidate_actor_alignment", ""),
                        r.get("actor_hint", ""),
                        r.get("linked_death_player", ""),
                        float(r.get("linked_death_timestamp", 0) or 0),
                        r.get("outcome_relation", ""),
                    )
                    for r in rows
                ],
            )
            nte_total += len(rows)

    # ── frame_scan (N0 — from scan_frames.py) ────────────────────────────────
    fscan_total = 0
    for e in entries:
        vid = e["id"]
        fscan_json = Path("outputs") / vid / "frame_scan.json"
        if fscan_json.exists():
            try:
                data = json.loads(fscan_json.read_text(encoding="utf-8"))
                con.execute("DELETE FROM frame_scan WHERE video_id = ?", (vid,))
                frames = data.get("frames", [])
                con.executemany(
                    "INSERT INTO frame_scan(video_id, t, header_visible, votes_visible) "
                    "VALUES (?,?,?,?)",
                    [
                        (vid, f["t"], 1 if f["header_visible"] else 0,
                         1 if f["votes_visible"] else 0)
                        for f in frames
                    ],
                )
                fscan_total += len(frames)
            except Exception as exc:
                print(f"  [WARN] frame_scan {vid}: {exc}")

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
                if isinstance(ov, dict) and ov.get("name")   # only store entries with a name assigned
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
        f"  phase_labels={phase_total:,}   day_events={devents_total:,}   "
        f"claims={claims_total:,}   frame_scan={fscan_total:,}   role_changes={rchg_total:,}\n"
        f"  player_status={pstatus_total:,}   death_events={deathe_total:,}   "
        f"night_target_events={nte_total:,}"
    )
    print(f"  Built at: {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build botc.db from pipeline outputs")
    ap.add_argument("--db", default="botc.db", help="Output SQLite database path")
    args = ap.parse_args()
    build(Path(args.db))
