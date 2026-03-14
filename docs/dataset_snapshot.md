# Dataset Snapshot

> **Last updated:** 2026-03-14
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
| `d2M-N5iABRo`, `OYTaTtjk3ac`, `z79AJOPoNi4` | Members-only; no audio | Download with valid membership cookies |

**Protected manual rosters** (do not re-scrape with `--force-manual`):
- `tf_LO5NKKUU` — `source: manual_entry`; 6 players curated by hand
- `HQlYPDUfM4Q` — `source: manual_entry`; 6 players curated by hand

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
