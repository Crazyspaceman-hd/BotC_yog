# Dataset Snapshot

> **Last updated:** 2026-03-15 (feat/player-status-tracking: N4 night target events)
> Run `python validate.py` for the current end-state check.

---

## Pipeline coverage

| Status | Count |
|--------|-------|
| analyzed (full pipeline complete) | 54 |
| members-only (no audio available) | 5 |
| blind game (no reliable intro roster) | 3 |
| skip (non-game content) | 1 |

DB as of last rebuild: **54 videos, 183 roles, 299 roster rows, 895 lies, 30 542 segments**
(`speaker_map`: 396 entries; `day_events`: 1 792 rows; `death_events`: 186 rows; `night_target_events`: 29 rows)

Winner set: 51 / 54.  Missing winner: 3 (all structural — 1 skip/tutorial, 1 members-only, 1 blind game).

Roster vs spreadsheet discrepancies: **115** (down from 128 pre-batch; majority are OCR coverage gaps, not wrong roles).

---

## Enrichment coverage (N0/N1/N2/N3/N4)

| Enrichment | Coverage | Notes |
|------------|----------|-------|
| N0 scan_frames | 48 / 53 | 5 missing (members-only / no video) |
| N1 speaker_consistency | 52 / 53 | 1 missing |
| N2 phase_detection | 53 / 53 | all covered |
| N3 claim_extraction | 53 / 53 | 3 431 total claims |
| N4 player_status | 50 / 53 | night target events + transcript name normalization added 2026-03-15; 3 missing = blind/members/empty roster |

*(53 = total processable; 1 video is skip-flagged)*

---

## Known data gaps (manual action required)

| Video | Issue | Action |
|-------|-------|--------|
| `0wGTes2sqmE` | 2 unlinked speakers | Watch video; identify speakers; fix in `fix_rosters.py`; run `build_db.py` |
| `ggM9BH__xtU` | Blind game; winner unknown; no intro roster | Watch video; set winner in `playlist.json` |
| `OPqWyO7h-wM` | 1 unlinked speaker (speaker_4, 25 segs — Duncan or Rythian) | Watch video; fix in `fix_rosters.py`; run `build_db.py` |
| `QbzFmlScLSA` | 3 unlinked speakers (Yogs Staff game, non-regular cast) | Watch video; identify cast; fix in `fix_rosters.py`; run `build_db.py` |
| `DAb9sq5ku2k` | Bonus format; only 4/14 claims verified | Low priority; manual `roster_overrides.json` |
| `d2M-N5iABRo`, `z79AJOPoNi4` | Members-only; no audio | Download with valid membership cookies |
| `OYTaTtjk3ac` | **Tutorial/meta video** (skip=True); not a real game — no player roster possible, speaker linking is N/A. Diarization ran but outputs are not meaningful for game analysis. | No action needed; validate.py excludes skip=True from speaker-link checks. |

**Protected manual rosters** (do not re-scrape with `--force-manual`):
- `tf_LO5NKKUU` — `source: manual_entry`; 6 players curated by hand
- `HQlYPDUfM4Q` — `source: manual_entry`; 6 players curated by hand

---

## Speaker-linking notes

### fix_rosters.py workflow
`fix_rosters.py` writes directly to `outputs/<id>/roster_overrides.json` on save.
The Streamlit UI reflects saved data immediately on reload.
However, **`validate.py` reads the DB** — so `build_db.py` must be run after saving in
`fix_rosters.py` before validation will reflect the change.
Correct sequence: `streamlit run fix_rosters.py` → save → `python build_db.py` → `python validate.py`.

### Unlinked speaker classification
Investigated unlinked speakers in `QbzFmlScLSA`, `nPAdvl7pySg`, `OPqWyO7h-wM` (2026-03-14):

- **Not overlap-clusters**: All unlinked speakers in `QbzFmlScLSA` (spk 3/4/5) and `nPAdvl7pySg`
  span the full video duration. They are real individual players, not diarization artefacts.
  Identification requires watching the video.
- **`QbzFmlScLSA`**: "Yogs Staff" game with non-regular cast. `speaker_3` mentions Craig and Sarah;
  `speaker_4` is addressed as Alex; speaker identities otherwise unknown without watching.
  Currently only Matt (spk_1) and CRAIG (spk_2) are linked; 3 speakers and full roster remain open.
- **`OPqWyO7h-wM`**: `speaker_4` has only 25 segments (via consistent.csv); remaining unlinked
  player is Duncan or Rythian. Requires video watch to confirm.
- **`nPAdvl7pySg`**: Blind game — speaker linking is expected to be incomplete.

---

## Active branches

| Branch | Purpose | State |
|--------|---------|-------|
| `develop` | Integration; merge features here | current working branch |
| `feat/curation-closeout` | Curation tooling improvements | in progress |
| `feat/nlp-enrichment` | N1/N2/N3 enrichment (merged to develop) | frozen |
| `feat/player-normalization` | `normalize_player()` + alias expansion (merged to develop) | merged |
| `feat/speaker-linking-and-roster-tools` | Speaker linking / roster tools (merged via PR #8) | frozen |
| `feat/player-status-tracking` | Player death/status tracking (N4 node) | in progress |
| `main` | Stable; PR #8 open develop→main | awaiting merge |
