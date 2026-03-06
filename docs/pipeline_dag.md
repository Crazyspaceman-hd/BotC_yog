# BotC_yog — Pipeline DAG

> **Last updated:** 2026-03-06
> **State:** covers all scripts present in the repo as of commit `7cf11b9`

---

## Overview

The system has three layers:

| Layer | Scripts | Scope |
|-------|---------|-------|
| **Extraction** | `run_pipeline.py` (orchestrates A–G) | per-video |
| **Curation** | `auto_assign_speakers.py`, `fix_rosters.py` | cross-video |
| **Publish** | `build_db.py`, `generate_landing.py`, `deploy_pages.sh` | global |

Canonical state is stored in:
- `outputs/<video_id>/` — per-video artifact files (gitignored)
- `playlist.json` — video registry + processing status (committed)
- `player_aliases.json` — canonical player name map (committed)
- `botc.db` — SQLite database rebuilt from all outputs (committed)
- `gh-pages` branch `index.html` — static landing page (deployed via `deploy_pages.sh`)

---

## Dependency Graph

```
A. video.download
│
├─► B. video.transcribe
│
├─► C. video.diarize
│
B+C ──► D. video.merge
│
D ──► E. video.patch      (optional but recommended)
│
│   F. video.scrape       (independent of B/C/D — reads video.mp4 directly)
│
E+F ──► H. curation.auto_assign_speakers   (cross-video, reads E+F output)
         │
         └─► (human review via I. curation.fix_rosters if needed)
              │
E/F+overrides ──► G. video.analyze
                   │
                   ├─► J. data.build_db       (cross-video, reads all G outputs)
                   │
                   ├─► L. ui.generate_landing (reads J output)
                   │
                   └─► K. ui.botc_ui          (library — no direct execution)
```

**Notes:**
- `F. video.scrape` can run after `A. video.download` (only needs `video.mp4`).
  In practice it is grouped with the per-video pipeline steps.
- `H. curation.auto_assign_speakers` is **optional** — if all speakers are
  already linked in `roster_overrides.json`, skip it.
- `I. curation.fix_rosters` is a **manual human step** — only needed when
  `auto_assign_speakers` leaves unlinked speakers or OCR issues exist.
- `K. ui.botc_ui` is a **shared library** (imported by `explore.py`,
  `explore_public.py`, `fix_rosters.py`, `build_db.py`, `generate_landing.py`).
  It is never executed directly.

---

## Node Definitions

### A. video.download

| Field | Value |
|-------|-------|
| **Purpose** | Download audio + video from YouTube; resample audio for diarization |
| **Script** | `run_pipeline.py` (step: `download`) internally uses `yt-dlp` + `ffmpeg` |
| **Per-video?** | ✓ Yes |
| **Inputs** | YouTube URL (from `playlist.json` or constructed from video ID) |
| **Outputs** | `outputs/<id>/audio.wav` (44.1 kHz stereo), `outputs/<id>/audio_16k.wav` (16 kHz mono), `outputs/<id>/video.mp4` |
| **Command** | `python run_pipeline.py <video_id> --steps download` |
| **Acceptance** | `audio.wav`, `audio_16k.wav`, `video.mp4` all exist and are non-zero |
| **Downstream** | B, C, F |
| **Prereqs** | `yt-dlp` on PATH; `ffmpeg` on PATH or `faster-whisper`+`soundfile` installed as fallback |

---

### B. video.transcribe

| Field | Value |
|-------|-------|
| **Purpose** | Transcribe audio to timestamped text segments via faster-whisper |
| **Script** | `transcribe.py` |
| **Per-video?** | ✓ Yes |
| **Inputs** | `outputs/<id>/audio.wav` |
| **Outputs** | `outputs/<id>/whisper_segments.jsonl` |
| **Command** | `python run_pipeline.py <video_id> --steps transcribe` |
| **Acceptance** | `whisper_segments.jsonl` is non-zero and contains valid JSONL |
| **Downstream** | D |
| **Prereqs** | `faster-whisper` installed; CUDA GPU recommended; model `medium` by default |
| **Notes** | Windows: nvidia DLLs registered automatically before import |

---

### C. video.diarize

| Field | Value |
|-------|-------|
| **Purpose** | Label each audio segment with a speaker ID using NeMo |
| **Script** | `diarize_nemo.py` |
| **Per-video?** | ✓ Yes |
| **Inputs** | `outputs/<id>/audio_16k.wav` (must be 16 kHz mono!) |
| **Outputs** | `outputs/<id>/diarization.rttm` |
| **Command** | `python run_pipeline.py <video_id> --steps diarize` |
| **Acceptance** | `diarization.rttm` is non-zero; `SPEAKER` lines present |
| **Downstream** | D |
| **Prereqs** | NeMo `.venv_botc` environment activated; CUDA GPU strongly recommended |
| **Notes** | Speaker count resolved from: (1) `intro_roster.json` player count +1, (2) `playlist.json` `num_speakers` field, (3) default 9. `oracle_num_speakers=True` enabled. |

---

### D. video.merge

| Field | Value |
|-------|-------|
| **Purpose** | Combine Whisper transcription with NeMo speaker labels into a single CSV |
| **Script** | `merge_segments.py` |
| **Per-video?** | ✓ Yes |
| **Inputs** | `whisper_segments.jsonl`, `diarization.rttm` |
| **Outputs** | `outputs/<id>/segments.csv` (columns: start, end, speaker, text) |
| **Command** | `python run_pipeline.py <video_id> --steps merge` |
| **Acceptance** | `segments.csv` is non-zero and has rows |
| **Downstream** | E |

---

### E. video.patch

| Field | Value |
|-------|-------|
| **Purpose** | Fuzzy-match player names/roles in transcript to fix Whisper transcription errors |
| **Script** | `patch_transcript.py` |
| **Per-video?** | ✓ Yes |
| **Inputs** | `segments.csv`, `players.txt`, `roles.txt` |
| **Outputs** | `outputs/<id>/segments_patched.csv` |
| **Command** | `python run_pipeline.py <video_id> --steps patch` |
| **Acceptance** | `segments_patched.csv` is non-zero |
| **Downstream** | G (preferred over segments.csv), H |
| **Notes** | If `segments_patched.csv` is absent, G falls back to `segments.csv`. Missing patch is **not** a blocker but reduces lie-detection accuracy. |

---

### F. video.scrape

| Field | Value |
|-------|-------|
| **Purpose** | OCR the intro UI overlay to extract player name → role assignments |
| **Script** | `scrape_intro.py` |
| **Per-video?** | ✓ Yes |
| **Inputs** | `outputs/<id>/video.mp4` (also tries .webm, .mkv, .avi) |
| **Outputs** | `outputs/<id>/intro_roster.json` |
| **Command** | `python run_pipeline.py <video_id> --steps scrape` |
| **Acceptance** | `intro_roster.json` exists, is valid JSON, `players` array is non-empty |
| **Downstream** | G, H |
| **Prereqs** | `easyocr` (GPU) or `pytesseract` installed |
| **Notes** | OCR quality varies. Output must be reviewed for: duplicate roles, unknown roles, garbled names. Use `fix_rosters.py` or manual edit of `intro_roster.json` + `roster_overrides.json` to repair. **Blind games** have no reliable intro overlay — `intro_roster.json` will be empty or missing. |

---

### G. video.analyze

| Field | Value |
|-------|-------|
| **Purpose** | Cross-reference role claims in transcript against roster → detect lies |
| **Script** | `analyze_roles.py` |
| **Per-video?** | ✓ Yes |
| **Inputs** | `segments_patched.csv` (fallback: `segments.csv`), `intro_roster.json`, `roster_overrides.json` (if present) |
| **Outputs** | `outputs/<id>/lie_analysis.csv` |
| **Command** | `python run_pipeline.py <video_id> --steps analyze` OR `python analyze_roles.py --all` |
| **Acceptance** | `lie_analysis.csv` exists and has a valid CSV header. Header-only (0 data rows) is acceptable for games with no detectable role claims. |
| **Downstream** | J |
| **Notes** | Role names written in normalized form (lowercase, spaces) via `normalize_role()`. `build_db.py` applies `display_role()` (Title Case) on ingest. A missing or incomplete `roster_overrides.json` will produce many UNVERIFIED verdicts. |

---

### H. curation.auto_assign_speakers

| Field | Value |
|-------|-------|
| **Purpose** | Heuristically propose speaker→player name mappings; write to `roster_overrides.json` |
| **Script** | `auto_assign_speakers.py` |
| **Per-video?** | Cross-video (runs across all videos with transcripts) |
| **Inputs** | `segments_patched.csv` (or `segments.csv`), `intro_roster.json`, `roster_overrides.json` (existing), `roles.txt` |
| **Outputs** | `outputs/<id>/roster_overrides.json` (updated for assigned speakers) |
| **Command** | `python auto_assign_speakers.py` (dry run) \| `python auto_assign_speakers.py --apply` |
| **Acceptance** | After `--apply`: all CERTAIN/HIGH confidence speakers are assigned. Remaining unlinked speakers go to `fix_rosters.py`. |
| **Downstream** | G (must re-run analyze after applying) |
| **Notes** | Heuristics: (1) Storyteller detection by intro phrases, (2) role self-declaration matching intro_roster, (3) process of elimination. Skipped for `blind` games. |

---

### I. curation.fix_rosters (MANUAL STEP)

| Field | Value |
|-------|-------|
| **Purpose** | Interactive Streamlit tool to fix remaining OCR and speaker-linking issues |
| **Script** | `fix_rosters.py` |
| **Per-video?** | Cross-video (reviews all unresolved issues) |
| **Inputs** | `segments.csv/segments_patched.csv`, `intro_roster.json`, `roster_overrides.json`, `players.txt`, `roles.txt` |
| **Outputs** | `outputs/<id>/roster_overrides.json` (updated), may also edit `intro_roster.json` |
| **Command** | `streamlit run fix_rosters.py` |
| **Acceptance** | No remaining "unlinked speaker", "duplicate role", "unknown role", or "garbled name" issues |
| **Downstream** | G (must re-run analyze after fixing) |
| **Notes** | **Human intervention required.** Cannot be automated. Fixing an issue updates `roster_overrides.json`; downstream G must be re-run. This is a **manual gate** in the pipeline. |

---

### J. data.build_db

| Field | Value |
|-------|-------|
| **Purpose** | Rebuild `botc.db` from all pipeline outputs; seed the canonical `roles` table |
| **Script** | `build_db.py` |
| **Per-video?** | No — cross-video, processes all videos in `playlist.json` |
| **Inputs** | `playlist.json`, `player_aliases.json`, all `outputs/<id>/lie_analysis.csv`, `segments_patched.csv`, `intro_roster.json`, `roster_overrides.json` |
| **Outputs** | `botc.db` |
| **Command** | `python build_db.py` |
| **Acceptance** | `botc.db` exists; `lies`, `segments`, `roster`, `speaker_map`, `roles`, `videos` tables all have expected row counts; no errors in output |
| **Downstream** | K (library), L |
| **Notes** | Fully reproducible from committed artifacts. Run after **any** change to lie_analysis, intro_roster, or roster_overrides. Role names normalized to Title Case on ingest via `display_role()`. |

---

### K. ui.botc_ui (LIBRARY — NOT EXECUTED)

| Field | Value |
|-------|-------|
| **Purpose** | Shared display constants, role registry (`_ROLES`), and helpers for all UI scripts |
| **Script** | `botc_ui.py` |
| **Per-video?** | N/A — library |
| **Inputs** | Imported by `explore.py`, `explore_public.py`, `fix_rosters.py`, `build_db.py`, `generate_landing.py` |
| **Outputs** | Provides: `_ROLES`, `_EVIL_ROLES` (derived frozenset), `_team()`, `VERDICT_BG`, `VERDICT_ICON`, `_SPK_PALETTE`, `_SOURCE_STYLE`, `_STATUS_BG` |
| **Command** | Not executed directly |
| **Notes** | **Single source of truth for role→team/type mapping.** Adding a new BotC role: add one entry to `_ROLES` dict; `build_db.py` will seed the DB `roles` table automatically. `_EVIL_ROLES` is a derived frozenset for backward compatibility. |

---

### L. ui.generate_landing

| Field | Value |
|-------|-------|
| **Purpose** | Generate a self-contained HTML landing page from `botc.db` |
| **Script** | `generate_landing.py` |
| **Per-video?** | No — global |
| **Inputs** | `botc.db` |
| **Outputs** | `landing.html` (gitignored) |
| **Command** | `python generate_landing.py` |
| **Acceptance** | `landing.html` exists and is non-zero |
| **Downstream** | `deploy_pages.sh` (copies to `gh-pages` branch as `index.html`) |
| **Notes** | `landing.html` is gitignored. The live page is on the `gh-pages` branch. Deploy with `bash deploy_pages.sh`. |

---

## Canonical Artifacts

| Artifact | Location | Committed? | Reproducible? |
|----------|----------|------------|---------------|
| Playlist metadata | `playlist.json` | ✓ | Via `fetch_playlist.py` (requires YouTube access) |
| Player name aliases | `player_aliases.json` | ✓ | Manual — maintained by hand |
| SQLite database | `botc.db` | ✓ | Fully: `python build_db.py` from committed outputs |
| Transcription | `outputs/<id>/whisper_segments.jsonl` | ✗ (gitignored) | Via step B |
| Speaker labels | `outputs/<id>/diarization.rttm` | ✗ (gitignored) | Via step C (GPU + NeMo) |
| Merged segments | `outputs/<id>/segments.csv` | ✗ (gitignored) | Via step D |
| Patched segments | `outputs/<id>/segments_patched.csv` | ✗ (gitignored) | Via step E |
| Intro roster (OCR) | `outputs/<id>/intro_roster.json` | ✗ (gitignored) | Via step F (OCR — may need manual fix) |
| Speaker overrides | `outputs/<id>/roster_overrides.json` | ✗ (gitignored) | Via steps H+I |
| Lie analysis | `outputs/<id>/lie_analysis.csv` | ✗ (gitignored) | Via step G |
| Landing page HTML | `landing.html` | ✗ (gitignored) | Via step L |
| Live landing page | `gh-pages:index.html` | ✓ (separate branch) | Via `deploy_pages.sh` |

---

## Scripts Not in the DAG (Support / Utility)

| Script | Role |
|--------|------|
| `fetch_playlist.py` | Bootstraps / refreshes `playlist.json`; run once before first pipeline run, then periodically to add new videos |
| `calibrate_scraper.py` | Visual tuning tool for OCR crop regions in `scrape_intro.py`; not in the pipeline flow |
| `explore.py` | Full Streamlit editor (read/write) — for manual inspection and roster editing outside of `fix_rosters.py` |
| `explore_public.py` | Read-only Streamlit viewer — the public-facing analysis UI |
| `deploy_pages.sh` | Bash script: runs `generate_landing.py`, switches to `gh-pages`, commits and pushes `index.html` |
| `validate.py` | _(see docs/validate.py)_ Checks repo end-state against expected completeness |

---

## Known Data Gaps (as of 2026-03-06)

| Video ID | Issue | Blocked By |
|----------|-------|------------|
| `DbF9CPOueTI` | Never processed (pending) | Manual pipeline run required |
| `0wGTes2sqmE` | Analyzed but 2 speakers unlinked; no lies detected; **winner missing** | Manual: watch video to determine winner |
| `ggM9BH__xtU` | Blind game — no intro roster; **winner missing** | Manual: watch video to determine winner |
| 3 other NULL-winner videos | Winner not yet manually determined | Manual review |
| `DAb9sq5ku2k` | Members-only bonus; intro_roster has only 4 players (partial OCR) | OCR quality issue; may need `fix_rosters.py` |
| `nPAdvl7pyS` | Empty ghost directory (not in playlist.json) | Safe to delete: `rmdir outputs/nPAdvl7pyS` |
