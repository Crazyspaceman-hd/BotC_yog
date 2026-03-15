# Dataset Snapshot

> **Last updated:** 2026-03-14 (post-N2 cleanup)
> Run `python validate.py` for the current end-state check.

---

## Pipeline coverage

| Status | Count |
|--------|-------|
| analyzed (full pipeline complete) | 54 |
| members-only (no audio available) | 6 |
| blind game (no reliable intro roster) | 3 |
| skip (non-game content) | 1 |

DB as of last rebuild: **54 videos, 183 roles, 299 roster rows, 895 lies, 30 542 segments**

Winner set: 48 / 54.  Missing winner: 6 (includes blind games and 3 confirmed gaps below).

Roster vs spreadsheet discrepancies: **115** (down from 128 pre-batch; majority are OCR coverage gaps, not wrong roles).

---

## Enrichment coverage (N0/N1/N2/N3)

| Enrichment | Coverage | Notes |
|------------|----------|-------|
| N0 scan_frames | 48 / 53 | 5 missing (members-only / no video) |
| N1 speaker_consistency | 52 / 53 | 1 missing |
| N2 phase_detection | 53 / 53 | all covered |
| N3 claim_extraction | 53 / 53 | 3 431 total claims |

*(53 = total processable; 1 video is skip-flagged)*

---

## Known data gaps (manual action required)

| Video | Issue | Action |
|-------|-------|--------|
| `0wGTes2sqmE` | 2 unlinked speakers; winner missing | Watch video; fix in `fix_rosters.py`; set winner |
| `DbF9CPOueTI` | Winner missing | Watch video; set winner in `playlist.json` |
| `OaAUvM4SAkg` | Winner missing | Watch video; set winner in `playlist.json` |
| `ggM9BH__xtU` | Blind game; winner unknown; no intro roster | Watch video; set winner in `playlist.json` |
| `OPqWyO7h-wM` | 1 unlinked speaker | `streamlit run fix_rosters.py` |
| `QbzFmlScLSA` | 3 unlinked speakers | `streamlit run fix_rosters.py` |
| `DzTk6kSIg-M` | 4 unlinked speakers | `streamlit run fix_rosters.py` |
| `IUO3Xz1kNkc` | 4 unlinked speakers | `streamlit run fix_rosters.py` |
| `DAb9sq5ku2k` | Bonus format; only 4/14 claims verified | Low priority; manual `roster_overrides.json` |
| `d2M-N5iABRo`, `z79AJOPoNi4` | Members-only; no audio | Download with valid membership cookies |
| `OYTaTtjk3ac` | **Tutorial/meta video** (skip=True); not a real game — no player roster possible, speaker linking is N/A. OCR shows one intro card (Sophie=Tor from a demo). Diarization ran but outputs are not meaningful for game analysis. | No action needed; validate.py excludes skip=True from speaker-link checks. |

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
| `main` | Stable; PR #8 open develop→main | awaiting merge |
