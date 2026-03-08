"""validate.py — End-state validation for BotC_yog.

Checks the repo's data completeness and consistency against the pipeline DAG.
Run after any full pipeline run or build_db.py to get a pass/fail report.

Usage:
    python validate.py              # full check, all videos
    python validate.py --video <id> # single video only
    python validate.py --strict     # fail on data gaps (missing winners, etc.)
    python validate.py --json       # output as JSON
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from pipeline_utils import load_playlist_entries

# ── Config ────────────────────────────────────────────────────────────────────

OUTPUTS_DIR   = Path("outputs")
PLAYLIST_JSON = Path("playlist.json")
DB_PATH       = Path("botc.db")

# Files expected for a fully-processed video (relative to outputs/<id>/)
_REQUIRED_PIPELINE = [
    ("audio.wav",                "A.download"),
    ("whisper_segments.jsonl",   "B.transcribe"),
    ("diarization.rttm",         "C.diarize"),
    ("segments.csv",             "D.merge"),
    ("intro_roster.json",        "F.scrape"),
    ("lie_analysis.csv",         "G.analyze"),
]

# Files that are strongly recommended but not always present
_OPTIONAL_PIPELINE = [
    ("segments_patched.csv",   "E.patch"),
    ("roster_overrides.json",  "H/I.curation"),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, status: str, check: str, detail: str, video_id: str = "") -> None:
        self.items.append({
            "status":   status,
            "check":    check,
            "video_id": video_id,
            "detail":   detail,
        })

    def print(self) -> None:
        status_order = {FAIL: 0, WARN: 1, PASS: 2, INFO: 3}
        grouped: dict[str, list[dict]] = {}
        for item in self.items:
            grouped.setdefault(item["check"], []).append(item)

        counts = {PASS: 0, WARN: 0, FAIL: 0, INFO: 0}
        for check, items in grouped.items():
            worst = min(items, key=lambda x: status_order[x["status"]])
            icon = {"PASS": "OK", "WARN": "!! ", "FAIL": "XX ", "INFO": "-- "}[worst["status"]]
            print(f"  {icon} [{worst['status']:4}] {check}")
            # Only show individual failures / warnings (not all passes)
            for it in items:
                if it["status"] in (FAIL, WARN, INFO):
                    vid = f" ({it['video_id']})" if it["video_id"] else ""
                    print(f"         {it['detail']}{vid}")
            counts[worst["status"]] += 1

        print()
        print(f"  Checks: {len(grouped)}  PASS={counts[PASS]}  WARN={counts[WARN]}  FAIL={counts[FAIL]}")

    def as_json(self) -> str:
        return json.dumps(self.items, indent=2, ensure_ascii=False)

    def has_failures(self) -> bool:
        return any(i["status"] == FAIL for i in self.items)

    def has_warnings(self) -> bool:
        return any(i["status"] == WARN for i in self.items)


# ── Checks ────────────────────────────────────────────────────────────────────

def check_prerequisites(rpt: Report) -> None:
    """Core files that must exist for the repo to function."""
    for path, label in [
        (PLAYLIST_JSON,          "playlist.json"),
        (Path("player_aliases.json"), "player_aliases.json"),
        (Path("players.txt"),    "players.txt"),
        (Path("roles.txt"),      "roles.txt"),
        (Path("botc_ui.py"),     "botc_ui.py"),
        (Path("pipeline_utils.py"), "pipeline_utils.py"),
        (DB_PATH,                "botc.db"),
    ]:
        if path.exists():
            rpt.add(PASS, f"prereq:{label}", f"{path} exists")
        else:
            rpt.add(FAIL, f"prereq:{label}", f"{path} MISSING")


def check_db_importable(rpt: Report) -> None:
    """botc.db is valid SQLite and has all expected tables."""
    if not DB_PATH.exists():
        rpt.add(FAIL, "db:schema", "botc.db missing")
        return

    try:
        con = sqlite3.connect(str(DB_PATH))
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        required = {"videos", "lies", "segments", "roster", "speaker_map", "roles"}
        missing = required - tables
        if missing:
            rpt.add(FAIL, "db:schema", f"Missing tables: {sorted(missing)}")
        else:
            rpt.add(PASS, "db:schema", f"All required tables present ({len(tables)} total)")

        for t in sorted(required & tables):
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            if n == 0:
                rpt.add(WARN, f"db:row_count:{t}", f"Table '{t}' is empty")
            else:
                rpt.add(PASS, f"db:row_count:{t}", f"{t}: {n:,} rows")

        con.close()
    except Exception as exc:
        rpt.add(FAIL, "db:schema", f"DB error: {exc}")


def check_role_normalization(rpt: Report) -> None:
    """All role names in DB are Title Case (display form)."""
    if not DB_PATH.exists():
        return
    try:
        con = sqlite3.connect(str(DB_PATH))
        # Check lies table for non-title-case role names
        bad_actual = con.execute(
            "SELECT DISTINCT actual_role FROM lies "
            "WHERE actual_role != '' AND actual_role != UPPER(SUBSTR(actual_role,1,1)) || LOWER(SUBSTR(actual_role,2))"
            " AND actual_role NOT GLOB '*[a-z]*' LIMIT 20"
        ).fetchall()
        # Simpler: check for any lowercase-starting non-empty role
        bad_roles = con.execute(
            "SELECT DISTINCT actual_role FROM lies "
            "WHERE length(actual_role) > 0 "
            "AND actual_role = lower(actual_role) "
            "LIMIT 20"
        ).fetchall()
        if bad_roles:
            rpt.add(WARN, "roles:normalization",
                    f"{len(bad_roles)} lowercase actual_role values in lies table: "
                    f"{[r[0] for r in bad_roles[:5]]}")
        else:
            rpt.add(PASS, "roles:normalization",
                    "All actual_role values in lies table appear Title-Cased")
        con.close()
    except Exception as exc:
        rpt.add(WARN, "roles:normalization", f"Could not check: {exc}")


def check_db_reproducible(rpt: Report) -> None:
    """build_db.py can be imported and run without errors (dry validation)."""
    try:
        import build_db  # noqa: F401
        rpt.add(PASS, "db:reproducible", "build_db.py imports cleanly")
    except Exception as exc:
        rpt.add(FAIL, "db:reproducible", f"build_db.py import error: {exc}")


def check_playlist_sync(rpt: Report) -> None:
    """playlist.json status fields match actual output files."""
    entries = load_playlist_entries()
    if not entries:
        rpt.add(FAIL, "playlist:sync", "playlist.json missing or empty")
        return

    from fetch_playlist import compute_status
    mismatches = 0
    for e in entries:
        vid = e["id"]
        actual = compute_status(vid)
        declared = e.get("status", "pending")
        if actual != declared:
            rpt.add(WARN, "playlist:sync",
                    f"status mismatch: declared='{declared}' actual='{actual}'",
                    video_id=vid)
            mismatches += 1
    if mismatches == 0:
        rpt.add(PASS, "playlist:sync",
                f"All {len(entries)} playlist entries have correct status")


def check_per_video_artifacts(rpt: Report, video_ids: list[str] | None = None) -> None:
    """Required output files exist for processed videos."""
    entries = load_playlist_entries()
    if not entries:
        return

    target_vids = set(video_ids) if video_ids else None

    analyzed = [
        e for e in entries
        if e.get("status") == "analyzed"
        and not e.get("members_only")  # members-only may legitimately miss some files
        and (target_vids is None or e["id"] in target_vids)
    ]

    missing_required = 0
    missing_optional = 0
    for e in analyzed:
        vid = e["id"]
        d   = OUTPUTS_DIR / vid
        for fname, step in _REQUIRED_PIPELINE:
            p = d / fname
            if not p.exists():
                rpt.add(FAIL, f"artifact:{step}", f"{fname} missing", video_id=vid)
                missing_required += 1
            elif p.stat().st_size == 0:
                rpt.add(WARN, f"artifact:{step}", f"{fname} is 0 bytes", video_id=vid)
        for fname, step in _OPTIONAL_PIPELINE:
            p = d / fname
            if not p.exists():
                rpt.add(INFO, f"artifact:{step}", f"{fname} absent (optional)", video_id=vid)
                missing_optional += 1

    if missing_required == 0:
        rpt.add(PASS, "artifact:required",
                f"All required artifacts present for {len(analyzed)} analyzed videos")
    if missing_optional == 0:
        rpt.add(PASS, "artifact:optional",
                f"All optional artifacts present for {len(analyzed)} analyzed videos")


def check_unlinked_speakers(rpt: Report) -> None:
    """No analyzed non-blind video has unlinked speakers (speaker_X with no override).

    Blind games are expected to have no speaker links (players don't know their
    roles; intro roster is unavailable) and are downgraded to INFO.
    """
    if not DB_PATH.exists():
        return
    # Build set of blind video IDs so we can classify differently
    blind_vids: set[str] = {
        e["id"] for e in load_playlist_entries()
        if e.get("blind") or e.get("members_only")
    }
    try:
        con = sqlite3.connect(str(DB_PATH))
        # Speakers in segments that have NO entry in speaker_map
        unlinked = con.execute("""
            SELECT s.video_id, s.speaker, COUNT(*) AS n_segs
            FROM segments s
            LEFT JOIN speaker_map sm
                ON sm.video_id = s.video_id AND sm.speaker = s.speaker
            WHERE sm.speaker IS NULL
              AND s.speaker NOT IN ('speaker_0', 'UNKNOWN')
              AND s.speaker LIKE 'speaker_%'
            GROUP BY s.video_id, s.speaker
            ORDER BY s.video_id, CAST(REPLACE(s.speaker, 'speaker_', '') AS INTEGER)
        """).fetchall()
        warn_count = 0
        for vid, spk, n in unlinked:
            if vid in blind_vids:
                rpt.add(INFO, "curation:unlinked_speakers",
                        f"{spk} unlinked ({n} segs) - blind/members game, expected",
                        video_id=vid)
            else:
                rpt.add(WARN, "curation:unlinked_speakers",
                        f"{spk} unlinked ({n} segments)", video_id=vid)
                warn_count += 1
        if warn_count == 0 and not unlinked:
            rpt.add(PASS, "curation:unlinked_speakers",
                    "All non-Storyteller speakers are linked to a player")
        elif warn_count == 0:
            rpt.add(PASS, "curation:unlinked_speakers",
                    "All unlinked speakers are in blind/members games (expected)")
        con.close()
    except Exception as exc:
        rpt.add(WARN, "curation:unlinked_speakers", f"Could not check: {exc}")


def check_winner_coverage(rpt: Report) -> None:
    """All analyzed non-blind, non-members-only videos have a winner set."""
    entries = load_playlist_entries()
    if not entries:
        return

    # Videos that are fully analyzed, not members, not blind
    should_have_winner = [
        e for e in entries
        if e.get("status") == "analyzed"
        and not e.get("members_only")
        and not e.get("blind")
    ]
    missing_winner = []
    if DB_PATH.exists():
        try:
            con = sqlite3.connect(str(DB_PATH))
            no_winner_ids = {
                row[0] for row in
                con.execute("SELECT id FROM videos WHERE winner IS NULL").fetchall()
            }
            con.close()
            missing_winner = [
                e for e in should_have_winner if e["id"] in no_winner_ids
            ]
        except Exception:
            pass

    if missing_winner:
        for e in missing_winner:
            rpt.add(WARN, "data:winner_coverage",
                    f"winner not set: {e.get('title','')[:50]}", video_id=e["id"])
    else:
        rpt.add(PASS, "data:winner_coverage",
                f"All {len(should_have_winner)} analyzed non-blind videos have a winner")


def check_ghost_directories(rpt: Report) -> None:
    """No output directories for videos not in playlist.json."""
    entries = load_playlist_entries()
    playlist_ids = {e["id"] for e in entries}
    if not OUTPUTS_DIR.exists():
        return
    ghosts = []
    for d in OUTPUTS_DIR.iterdir():
        if d.is_dir() and d.name not in playlist_ids:
            if not any(d.iterdir()):
                rpt.add(WARN, "repo:ghost_dirs",
                        f"Empty ghost directory not in playlist.json: outputs/{d.name}/")
            else:
                rpt.add(WARN, "repo:ghost_dirs",
                        f"Directory not in playlist.json: outputs/{d.name}/ (has files)")
            ghosts.append(d.name)
    if not ghosts:
        rpt.add(PASS, "repo:ghost_dirs", "No untracked output directories found")


def check_ui_source(rpt: Report) -> None:
    """explore_public.py and generate_landing.py both read from botc.db."""
    for script in ("explore_public.py", "generate_landing.py"):
        p = Path(script)
        if not p.exists():
            rpt.add(FAIL, f"ui:source:{script}", f"{script} missing")
            continue
        src = p.read_text(encoding="utf-8")
        if "botc.db" in src or 'DB_PATH' in src or 'sqlite3' in src:
            rpt.add(PASS, f"ui:source:{script}", f"{script} reads from botc.db")
        else:
            rpt.add(WARN, f"ui:source:{script}",
                    f"{script} does not appear to read from botc.db")


def check_no_duplicate_pipeline_paths(rpt: Report) -> None:
    """Detect if any script bypasses the canonical pipeline path."""
    # Old pattern: hardcoded _EVIL_ROLES sets (replaced by botc_ui._ROLES)
    # New pattern: scripts should use normalize_role/display_role, not .title()
    import_audit = []
    for script in Path(".").glob("*.py"):
        if script.name == __file__.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]:
            continue  # skip self
        src = script.read_text(encoding="utf-8", errors="replace")
        if "_EVIL_ROLES" in src and script.name != "botc_ui.py":
            # Check it's using the imported one, not defining its own
            if "_EVIL_ROLES = {" in src or "_EVIL_ROLES = frozenset" in src:
                import_audit.append(f"{script.name}: defines its own _EVIL_ROLES set")
    if import_audit:
        for note in import_audit:
            rpt.add(WARN, "code:duplicate_roles_set", note)
    else:
        rpt.add(PASS, "code:duplicate_roles_set",
                "No script defines its own _EVIL_ROLES (all use botc_ui)")


def check_pending_processable(rpt: Report) -> None:
    """Surface public non-skip videos that are pending but could be processed now."""
    entries = load_playlist_entries()
    if not entries:
        return
    processable = [
        e for e in entries
        if not e.get("skip")
        and not e.get("members_only")
        and e.get("status", "pending") != "analyzed"
    ]
    if processable:
        for e in processable:
            rpt.add(INFO, "pipeline:pending_processable",
                    f"status={e.get('status','pending')} - run: python run_pipeline.py {e['id']}",
                    video_id=e["id"])
    else:
        rpt.add(PASS, "pipeline:pending_processable",
                "All public non-skip videos are fully analyzed")


def check_partial_downloads(rpt: Report) -> None:
    """Detect videos with a webm/raw download but no converted audio.wav."""
    entries = load_playlist_entries()
    if not entries:
        return
    raw_exts = ("*.webm", "*.m4a", "*.mp3", "*.mkv")
    partial = []
    for e in entries:
        if e.get("skip"):
            continue
        vid = e["id"]
        out = OUTPUTS_DIR / vid
        if not out.exists():
            continue
        has_wav = (out / "audio.wav").exists()
        has_raw = any(list(out.glob(ext)) for ext in raw_exts)
        if has_raw and not has_wav:
            raw_files = [f.name for ext in raw_exts for f in out.glob(ext)]
            partial.append((vid, raw_files))
    if partial:
        for vid, files in partial:
            rpt.add(WARN, "pipeline:partial_download",
                    f"Raw download present {files} but audio.wav missing - re-run download step",
                    video_id=vid)
    else:
        rpt.add(PASS, "pipeline:partial_download",
                "No partial downloads detected (all raw files have been converted)")


# ── N1/N2/N3 Enrichment checks (INFO only — enrichment is always optional) ────

def check_enrichment_artifacts(rpt: Report,
                               video_ids: list[str] | None = None) -> None:
    """Report presence/validity of optional N1/N2/N3 enrichment artifacts.

    These are always INFO-level — missing enrichment files never cause WARN or FAIL.
    Only run for 'analyzed' videos where the enrichment could have been generated.
    """
    import csv as _csv

    entries = load_playlist_entries()
    if not entries:
        return

    analyzed = [e["id"] for e in entries
                if e.get("status") == "analyzed" and not e.get("skip")]
    targets  = [v for v in analyzed
                if video_ids is None or v in video_ids]

    n1_present = n2_valid = n3_valid = 0
    n1_total   = len(targets)
    issues: list[str] = []

    for vid in targets:
        out = OUTPUTS_DIR / vid

        # N1: segments_consistent.csv
        p_cons = out / "segments_consistent.csv"
        if p_cons.exists():
            n1_present += 1

        # N2: phase_labels.csv — check header and phase values
        p_phase = out / "phase_labels.csv"
        if p_phase.exists():
            try:
                rows = list(_csv.DictReader(p_phase.open(encoding="utf-8")))
                valid_phases = {"Intro", "Night", "Day", "Nomination", "Execution", "Unknown"}
                bad = [r["phase"] for r in rows if r.get("phase") not in valid_phases]
                if bad:
                    issues.append(f"{vid}: phase_labels.csv has unrecognised phases: {bad[:3]}")
                else:
                    n2_valid += 1
            except Exception as exc:
                issues.append(f"{vid}: phase_labels.csv unreadable — {exc}")

        # N3: claims.csv + claim_graph.json
        p_claims = out / "claims.csv"
        p_graph  = out / "claim_graph.json"
        if p_claims.exists() and p_graph.exists():
            try:
                rows = list(_csv.DictReader(p_claims.open(encoding="utf-8")))
                _ = json.loads(p_graph.read_text(encoding="utf-8"))
                n3_valid += 1
            except Exception as exc:
                issues.append(f"{vid}: claims artifact unreadable — {exc}")

    summary = (f"N1 consistency: {n1_present}/{n1_total}  "
               f"N2 phases: {n2_valid}/{n1_total}  "
               f"N3 claims: {n3_valid}/{n1_total}")
    rpt.add(INFO, "enrichment:artifacts", summary)

    for issue in issues:
        rpt.add(WARN, "enrichment:artifacts", issue)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Validate BotC_yog repo end-state")
    ap.add_argument("--video", metavar="ID", help="Validate a single video ID only")
    ap.add_argument("--strict", action="store_true",
                    help="Exit non-zero on warnings too (not just failures)")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="Output results as JSON")
    args = ap.parse_args()

    rpt = Report()

    print("BotC_yog - Validation Report")
    print("=" * 60)

    # Run all checks
    check_prerequisites(rpt)
    check_db_importable(rpt)
    check_db_schema_and_counts = check_db_importable  # already done
    check_db_reproducible(rpt)
    check_role_normalization(rpt)
    check_playlist_sync(rpt)
    check_per_video_artifacts(rpt, [args.video] if args.video else None)
    check_unlinked_speakers(rpt)
    check_winner_coverage(rpt)
    check_ghost_directories(rpt)
    check_pending_processable(rpt)
    check_partial_downloads(rpt)
    check_ui_source(rpt)
    check_no_duplicate_pipeline_paths(rpt)
    check_enrichment_artifacts(rpt, [args.video] if args.video else None)

    print()
    if args.as_json:
        print(rpt.as_json())
    else:
        rpt.print()

    print("=" * 60)
    if rpt.has_failures():
        print("RESULT: FAIL -- see XX items above")
        sys.exit(1)
    elif rpt.has_warnings() and args.strict:
        print("RESULT: WARN (--strict mode) -- see !! items above")
        sys.exit(1)
    elif rpt.has_warnings():
        print("RESULT: WARN -- see !! items above (run with --strict to treat as failure)")
        sys.exit(0)
    else:
        print("RESULT: PASS")
        sys.exit(0)


if __name__ == "__main__":
    main()
