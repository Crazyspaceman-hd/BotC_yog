# Dataset Snapshot

> **Last updated:** 2026-03-07
> Run `python validate.py` for the current end-state check.

---

## Pipeline coverage

| Status | Count |
|--------|-------|
| analyzed (full pipeline complete) | 52 |
| pending / members-only / skip | ~10 |

DB as of last rebuild: **183 roles, 322 roster rows, 834 lies, 81 871 segments**

---

## Enrichment coverage (N1/N2/N3)

| Enrichment | Coverage | Notes |
|------------|----------|-------|
| N1 speaker_consistency | 52 / 52 | 0 failures |
| N2 phase_detection | 52 / 52 | 2 weak (Intro+Day only) |
| N3 claim_extraction | 52 / 52 | 3 364 total claims, 0 empty |

**N2 weak videos** (low ST keyword density — check roster linkage first):
- `OPqWyO7h-wM` — 1 unlinked speaker
- `0wGTes2sqmE` — 2 unlinked speakers; winner missing

---

## Known data gaps (manual action required)

| Video | Issue | Action |
|-------|-------|--------|
| `0wGTes2sqmE` | 2 unlinked speakers; winner missing | Watch video; fix in `fix_rosters.py`; set winner |
| `ggM9BH__xtU` | Blind game; winner unknown | Watch video; set winner in `playlist.json` |
| `DzTk6kSIg-M` | 4 unlinked speakers | `streamlit run fix_rosters.py` |
| `IUO3Xz1kNkc` | 4 unlinked speakers | `streamlit run fix_rosters.py` |
| `OPqWyO7h-wM` | 1 unlinked speaker | `streamlit run fix_rosters.py` |
| `QbzFmlScLSA` | 3 unlinked speakers | `streamlit run fix_rosters.py` |
| `DAb9sq5ku2k` | Bonus format; only 4/14 claims verified | Low priority; manual `roster_overrides.json` |
| `z79AJOPoNi4`, `OaAUvM4SAkg` | Members-only; no audio | Download with membership cookies |
| `DbF9CPOueTI` | Never processed | `python run_pipeline.py DbF9CPOueTI` |

---

## Active branches

| Branch | Purpose |
|--------|---------|
| `develop` | Integration; merge features here |
| `feat/nlp-enrichment` | N1/N2/N3 enrichment work (frozen, pending PR) |
| `feat/speaker-linking-and-roster-tools` | Merged to main |
