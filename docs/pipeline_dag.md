# BotC_yog — Pipeline DAG

> **Last updated:** 2026-03-14
> **State:** covers all scripts present on `develop` as of 2026-03-14; N0–N3 are optional enrichment nodes; player-name normalization via `normalize_player()` added to `pipeline_utils.py`

---

## Overview

The system has five layers:

| Layer | Scripts | Scope |
|-------|---------|-------|
| **Registry** | `fetch_playlist.py` | global, periodic |
| **Extraction** | `run_pipeline.py` (orchestrates A–G) | per-video |
| **Curation** | `auto_assign_speakers.py`, `fix_rosters.py` | cross-video |
| **Publish** | `build_db.py`, `generate_landing.py`, `deploy_pages.sh` | global |
| **Validation** | `validate.py` | global, run after any phase |

Canonical state is stored in:
- `playlist.json` — video registry + processing status + per-game flags (committed)
- `outputs/<video_id>/` — per-video artifact files (gitignored)
- `player_aliases.json` — canonical player name map (committed)
- `botc.db` — SQLite database rebuilt from all outputs (committed)
- `gh-pages` branch `index.html` — static landing page (deployed via `deploy_pages.sh`)

---

## Per-Video State Machine

Each video in `playlist.json` has a `status` field. Status is **derived from filesystem artifacts** by `fetch_playlist.compute_status()` and refreshed after each pipeline step.

| Status | Meaning | Artifact present |
|--------|---------|-----------------|
| `pending` | Not yet started (or download failed) | none |
| `downloaded` | Audio and video downloaded | `audio.wav` |
| `transcribed` | Whisper transcription complete | `whisper_segments.jsonl` |
| `diarized` | NeMo diarization complete | `diarization.rttm` |
| `merged` | Transcript + speaker labels combined | `segments.csv` |
| `patched` | Transcript fuzzy-patched | `segments_patched.csv` |
| `analyzed` | Lie detection complete | `lie_analysis.csv` |

Additional per-game flags (not derived from artifacts — set manually or by pipeline):

| Flag | Meaning |
|------|---------|
| `members_only: true` | Requires YouTube membership to download; skipped by `--all` |
| `skip: true` | Not a game video (meta content); skipped by `--all` |
| `blind: true` | Roles hidden from players; no intro roster; lie analysis writes empty CSV |
| `needs_manual_st: true` | Storyteller is not `speaker_0`; requires manual `roster_overrides.json` |
| `winner: "Good"\|"Evil"\|null` | Game outcome (set manually from transcript) |
| `num_speakers: N` | Override oracle speaker count for diarization |

**Resume behaviour:** Because `status` is derived from artifacts, interrupted runs resume safely — the pipeline checks artifact existence before running each step. If a step is interrupted mid-write, re-running with `--force` will overwrite the partial output.

---

## Dependency Graph

```
A.0 playlist.fetch     (global, periodic)
     |
     v
A. video.download
|
├─► B. video.transcribe
|
├─► C. video.diarize
|        |
|        └─► [N1. speaker.episode_consistency]   ← optional, writes segments_consistent.csv
|
B+C ──► D. video.merge
         |
         v
         E. video.patch      (optional but recommended)
         |
         └─► [N2. speaker.day_boundary_detection] ← optional, writes phase_labels.csv + day_events.csv

A ──► F. video.scrape       (independent of B/C/D — reads video.mp4 directly)
      |
      └─► [N0. video.scan_frames]               ← optional, writes frame_scan rows to botc.db

E+F ──► H. curation.auto_assign_speakers   (cross-video, reads E+F output)
         |
         └─► (MANUAL GATE: I. curation.fix_rosters if issues remain)
              |
E/F+overrides ──► G. video.analyze
                   |
                   ├─► [N3. content.claim_propagation] ← optional, writes claims.csv + claim_graph.json
                   |
                   v
                   J. data.build_db       (cross-video, reads all G outputs)
                   |
                   ├─► L. ui.generate_landing
                   |        |
                   |        └─► deploy_pages.sh
                   |
                   └─► K. ui.botc_ui (library — no direct execution)

                   (any phase) ──► M. validate.end_state
```

**N0/N1/N2/N3 are optional enrichment nodes.** They do not affect playlist.json status, do not block any existing step, and are not required by J (build_db). N0 writes directly to `botc.db` (frame_scan table); N1–N3 write new artifact files alongside existing outputs.

**Notes:**
- `F. video.scrape` can run after `A. video.download` (only needs `video.mp4`).
  In practice it is grouped with the per-video pipeline steps.
- `H. curation.auto_assign_speakers` is **optional** — if all speakers are
  already linked in `roster_overrides.json`, skip it.
- `I. curation.fix_rosters` is a **MANUAL GATE** — only needed when
  `auto_assign_speakers` leaves unlinked speakers or OCR issues exist.
- `K. ui.botc_ui` is a **shared library** (imported by `explore.py`,
  `explore_public.py`, `fix_rosters.py`, `build_db.py`, `generate_landing.py`).
  It is never executed directly.
- `M. validate.end_state` should be run after any phase to confirm no artifacts
  were left behind.

---

## Game Phases vs. Events

The pipeline distinguishes between **phases** (broad temporal regions of a game) and
**events** (discrete occurrences within those regions).

**Broad phases** — produced by N2 (phase_detection / `detect_phases.py`), written to `phase_labels.csv`:

| Phase | Description |
|-------|-------------|
| `Intro` | Pre-game: players announce roles; Storyteller assigns night abilities |
| `Night` | Players close eyes; Storyteller resolves night actions privately |
| `Day` | All open discussion: group talk, nominations, voting, execution announcements |

> **Design note:** `Nomination` and `Execution` are no longer top-level phases.
> Everything visible to all players (group discussion, votes, ST announcements)
> is labelled `Day`.  Fine-grained game events within Day are captured in
> `day_events.csv` (see below).

**Day-scoped events** — also produced by N2, written to `day_events.csv`:

| Event | Description | Primary evidence |
|-------|-------------|-----------------|
| `NominationStart` | Nominations open or a nomination is made | `frame_scan.votes_visible` (conf 1.0) or ST keyword (conf 0.9) or player keyword (conf 0.6) |
| `VoteSequence` | The full voting window for a nomination | `frame_scan.votes_visible` window start→end |
| `ExecutionAnnouncement` | ST announces execution outcome (death or pardon) | Keyword regex in ST speech |
| `StorytellerInterruption` | ST speaks 1:1 with a player mid-Day (structural signal) | 2-speaker window during Day phase |
| `DayEnd` | Day→Night boundary (derived from phase transition) | Phase label boundary |

**Claim events** — produced by N3 (claim_extraction / `extract_claims.py`):

| Event | Typical phase | Description |
|-------|--------------|-------------|
| `role_claim` | Day / Night | A player claims (or implies) a role |
| `accusation` | Day | A player accuses another of being Evil |
| `suspicion` | Day | A player expresses suspicion about another |
| `agreement` | Day | A player supports another's claim or accusation |
| `challenge` | Day | A player disputes a claim |

Phase labels from N2 are consumed by N3 to provide temporal context for event
extraction (e.g. a role claim during a Night phase has different weight than
one during Day).

---

## Node Definitions

### A.0 playlist.fetch

| Field | Value |
|-------|-------|
| **Purpose** | Refresh `playlist.json` from the YouTube playlist; detect new videos; preserve curated flags |
| **Script** | `fetch_playlist.py` |
| **Per-video?** | No — global |
| **Inputs** | YouTube playlist URL (cached in `playlist.json` after first run) |
| **Outputs** | Updated `playlist.json` — new entries added as `status: pending`; existing curated flags preserved |
| **Command** | `python fetch_playlist.py` |
| **Acceptance** | New videos appear as `pending`; no existing `winner`/`blind`/`members_only` flags overwritten |
| **Notes** | Run periodically (e.g. weekly) to pick up new uploads. Members-only videos that yt-dlp cannot see without auth are preserved from existing entries. New videos that fail download are auto-flagged `members_only: true` by `run_pipeline.py`. |

---

### A. video.download

| Field | Value |
|-------|-------|
| **Purpose** | Download audio + video from YouTube; resample audio for diarization |
| **Script** | `run_pipeline.py` (step: `download`) internally uses `yt-dlp` + PyAV fallback |
| **Per-video?** | Yes |
| **Inputs** | YouTube URL (from `playlist.json`) |
| **Outputs** | `outputs/<id>/audio.wav` (44.1 kHz stereo), `outputs/<id>/audio_16k.wav` (16 kHz mono), `outputs/<id>/video.mp4` |
| **Command** | `python run_pipeline.py <video_id> --steps download` |
| **Acceptance** | `audio.wav`, `audio_16k.wav`, `video.mp4` all exist and are non-zero |
| **Downstream** | B, C, F |
| **Prereqs** | `yt-dlp` on PATH; `ffmpeg` OR `faster-whisper`+`soundfile` for PyAV fallback; `cookies.txt` in project root for members-only |
| **Failure modes** | `members_only: true` auto-set if download returns a membership gate error. If a `.webm` exists but `audio.wav` is missing, the conversion step was interrupted — re-run with `--force`. |

---

### B. video.transcribe

| Field | Value |
|-------|-------|
| **Purpose** | Transcribe audio to timestamped text segments via faster-whisper |
| **Script** | `transcribe.py` |
| **Per-video?** | Yes |
| **Inputs** | `outputs/<id>/audio.wav` |
| **Outputs** | `outputs/<id>/whisper_segments.jsonl` |
| **Command** | `python run_pipeline.py <video_id> --steps transcribe` |
| **Acceptance** | `whisper_segments.jsonl` is non-zero and contains valid JSONL |
| **Downstream** | D |
| **Prereqs** | `faster-whisper` installed; CUDA GPU recommended; model `medium` by default |
| **Notes** | Windows: nvidia DLLs registered automatically before import. `vad_filter=True` removes silence. To bulk re-transcribe: `python retranscribe_all.py`. |

---

### C. video.diarize

| Field | Value |
|-------|-------|
| **Purpose** | Label each audio segment with a speaker ID using NeMo TitaNet |
| **Script** | `diarize_nemo.py` |
| **Per-video?** | Yes |
| **Inputs** | `outputs/<id>/audio_16k.wav` (must be 16 kHz mono) |
| **Outputs** | `outputs/<id>/diarization.rttm` |
| **Command** | `python run_pipeline.py <video_id> --steps diarize` |
| **Acceptance** | `diarization.rttm` is non-zero; `SPEAKER` lines present |
| **Downstream** | D |
| **Prereqs** | NeMo installed in `.venv`; CUDA GPU strongly recommended |
| **Notes** | Speaker count resolved: (1) `intro_roster.json` player count +1, (2) `playlist.json` `num_speakers`, (3) default 9. `oracle_num_speakers=True` always enabled. Independent of transcription — re-diarizing does not require re-transcribing. |

---

### D. video.merge

| Field | Value |
|-------|-------|
| **Purpose** | Combine Whisper transcription with NeMo speaker labels into a single CSV |
| **Script** | `merge_segments.py` |
| **Per-video?** | Yes |
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
| **Per-video?** | Yes |
| **Inputs** | `segments.csv`, `players.txt`, `roles.txt` |
| **Outputs** | `outputs/<id>/segments_patched.csv` |
| **Command** | `python run_pipeline.py <video_id> --steps patch` |
| **Acceptance** | `segments_patched.csv` is non-zero |
| **Downstream** | G (preferred over segments.csv), H |
| **Notes** | If `segments_patched.csv` absent, G falls back to `segments.csv`. Missing patch is **not** a blocker but reduces lie-detection accuracy. |

---

### F. video.scrape

| Field | Value |
|-------|-------|
| **Purpose** | OCR the intro UI overlay to extract player name → role assignments |
| **Script** | `scrape_intro.py` |
| **Per-video?** | Yes |
| **Inputs** | `outputs/<id>/video.mp4` |
| **Outputs** | `outputs/<id>/intro_roster.json` |
| **Command** | `python run_pipeline.py <video_id> --steps scrape` |
| **Acceptance** | `intro_roster.json` exists, is valid JSON, `players` array is non-empty |
| **Downstream** | G, H |
| **Prereqs** | `easyocr` or `pytesseract` installed; OpenCV |
| **Flags** | `--force` re-scrapes (skips `manual_entry` files); `--force-manual` also overwrites `manual_entry` files — use with caution |
| **Notes** | OCR pipeline: left-panel-only rule; HSV masking for both blue (Good) and red (Evil) nameplates; tight nameplate blob selection (height ≥30% crop + leftmost); coverage check in fuzzy match; 4 s name-persistence window. Protected manual rosters: `tf_LO5NKKUU`, `HQlYPDUfM4Q` (`source: manual_entry`). Non-standard: `DAb9sq5ku2k` (Bonus format — DO NOT re-scrape). Blind games return empty result (expected). |

---

### G. video.analyze

| Field | Value |
|-------|-------|
| **Purpose** | Cross-reference role claims in transcript against roster — detect lies |
| **Script** | `analyze_roles.py` |
| **Per-video?** | Yes (also supports `--all` global rerun) |
| **Inputs** | `segments_patched.csv` (fallback: `segments.csv`), `intro_roster.json`, `roster_overrides.json` (if present) |
| **Outputs** | `outputs/<id>/lie_analysis.csv` |
| **Command** | `python run_pipeline.py <video_id> --steps analyze` OR `python analyze_roles.py --all` |
| **Acceptance** | `lie_analysis.csv` exists with valid CSV header. Header-only (0 data rows) is acceptable for blind games or games with no detectable role claims. |
| **Downstream** | J |
| **Notes** | Role names written normalized (lowercase, spaces) via `normalize_role()`. `build_db.py` applies `display_role()` (Title Case) on ingest. Missing/incomplete `roster_overrides.json` produces UNVERIFIED verdicts. |

---

### H. curation.auto_assign_speakers

| Field | Value |
|-------|-------|
| **Purpose** | Heuristically propose speaker→player name mappings; write `roster_overrides.json` |
| **Script** | `auto_assign_speakers.py` |
| **Per-video?** | Cross-video |
| **Inputs** | `segments_patched.csv` (or `segments.csv`), `intro_roster.json`, existing `roster_overrides.json`, `roles.txt` |
| **Outputs** | `outputs/<id>/roster_overrides.json` (updated) |
| **Command** | `python auto_assign_speakers.py` (dry run) \| `python auto_assign_speakers.py --apply` |
| **Acceptance** | All CERTAIN/HIGH confidence speakers assigned. Remaining unlinked → go to I. |
| **Downstream** | G (must re-run analyze after applying) |
| **Notes** | Heuristics: (1) Storyteller detection, (2) role self-declaration, (3) process of elimination. Skipped for blind games. |

---

### I. curation.fix_rosters (MANUAL GATE)

| Field | Value |
|-------|-------|
| **Purpose** | Interactive Streamlit tool to fix remaining OCR and speaker-linking issues |
| **Script** | `fix_rosters.py` |
| **Per-video?** | Cross-video |
| **Inputs** | `segments.csv/segments_patched.csv`, `intro_roster.json`, `roster_overrides.json`, `players.txt`, `roles.txt` |
| **Outputs** | `outputs/<id>/roster_overrides.json` (updated) |
| **Command** | `streamlit run fix_rosters.py` |
| **Acceptance** | No remaining unlinked-speaker, duplicate-role, unknown-role, or garbled-name issues |
| **Downstream** | G (must re-run analyze after fixing) |
| **Notes** | **Human intervention required.** Cannot be automated. After fixing, re-run G then J. This is the primary manual gate in the pipeline. |

---

### J. data.build_db

| Field | Value |
|-------|-------|
| **Purpose** | Rebuild `botc.db` from all pipeline outputs; seed canonical `roles` table |
| **Script** | `build_db.py` |
| **Per-video?** | No — cross-video |
| **Inputs** | `playlist.json`, `player_aliases.json`, all `outputs/<id>/lie_analysis.csv`, `segments_patched.csv`, `intro_roster.json`, `roster_overrides.json` |
| **Outputs** | `botc.db` |
| **Command** | `python build_db.py` |
| **Acceptance** | `botc.db` exists; all tables have expected row counts; no errors |
| **Downstream** | K (library), L |
| **Notes** | Fully reproducible from committed artifacts. Run after any change to lie_analysis, intro_roster, or roster_overrides. |

---

### K. ui.botc_ui (LIBRARY)

| Field | Value |
|-------|-------|
| **Purpose** | Shared display constants, role registry (`_ROLES`), helpers for all UI scripts |
| **Script** | `botc_ui.py` |
| **Per-video?** | N/A — library |
| **Outputs** | Provides: `_ROLES`, `_EVIL_ROLES` (derived frozenset, compat), `_team()`, colour palettes, verdict styles |
| **Command** | Not executed directly |
| **Notes** | **Single source of truth for role→team/type mapping.** Add new roles here; `build_db.py` seeds the `roles` table automatically. |

---

### L. ui.generate_landing

| Field | Value |
|-------|-------|
| **Purpose** | Generate self-contained HTML landing page from `botc.db` |
| **Script** | `generate_landing.py` |
| **Per-video?** | No — global |
| **Inputs** | `botc.db` |
| **Outputs** | `landing.html` (gitignored) |
| **Command** | `python generate_landing.py` |
| **Acceptance** | `landing.html` exists and is non-zero |
| **Downstream** | `deploy_pages.sh` |
| **Notes** | `landing.html` gitignored. Live page on `gh-pages` branch. Deploy: `bash deploy_pages.sh`. |

---

### M. validate.end_state

| Field | Value |
|-------|-------|
| **Purpose** | Cross-check all pipeline outputs for completeness, consistency, and correctness |
| **Script** | `validate.py` |
| **Per-video?** | No — global (supports `--video <id>` for single-video check) |
| **Inputs** | `playlist.json`, `botc.db`, `outputs/*/` artifact files |
| **Outputs** | Console report; exit code 0 (pass/warn) or 1 (fail / --strict) |
| **Command** | `python validate.py` \| `python validate.py --strict` \| `python validate.py --json` |
| **Checks (28)** | prerequisites, DB schema/counts, role normalization, playlist sync, per-video artifacts, unlinked speakers, winner coverage, ghost dirs, pending processable, partial downloads, UI source, duplicate role sets, enrichment artifact coverage |
| **Pass/Warn/Fail** | PASS = check clean. WARN = data gap or manual action needed (non-blocking). FAIL = structural/code error (blocking). INFO = expected/acceptable state (blind games, optional files). |

---

## Authoritative Orchestration Commands

### Per-video path (new or failed video)
```bash
# Full pipeline for one video:
python run_pipeline.py <video_id>

# Specific steps only:
python run_pipeline.py <video_id> --steps transcribe merge patch analyze

# Force re-run (ignore existing outputs):
python run_pipeline.py <video_id> --steps transcribe merge patch analyze --force

# All pending public non-skip videos:
python run_pipeline.py --all
```

### Dataset refresh path (after curation changes)
```bash
# Automated portion:
python scripts/run_dataset_refresh.py

# Or manually step by step:
python auto_assign_speakers.py --apply     # heuristic speaker linking
streamlit run fix_rosters.py              # MANUAL GATE: fix remaining issues
python analyze_roles.py --all             # re-run analysis after curation
python build_db.py                        # rebuild DB
python generate_landing.py               # rebuild landing page
bash deploy_pages.sh                     # deploy (optional)
python validate.py                        # confirm end state
```

### Full run (new videos + refresh):
```bash
bash scripts/run_all.sh
```

---

## Canonical Artifacts

| Artifact | Location | Committed? | Reproducible? |
|----------|----------|------------|---------------|
| Playlist metadata + flags | `playlist.json` | Yes | Via `fetch_playlist.py` (needs YouTube access) |
| Player name aliases | `player_aliases.json` | Yes | Manual — maintained by hand |
| SQLite database | `botc.db` | Yes | Fully: `python build_db.py` |
| Transcription | `outputs/<id>/whisper_segments.jsonl` | No (gitignored) | Via step B |
| Speaker labels | `outputs/<id>/diarization.rttm` | No (gitignored) | Via step C (GPU + NeMo) |
| Merged segments | `outputs/<id>/segments.csv` | No (gitignored) | Via step D |
| Patched segments | `outputs/<id>/segments_patched.csv` | No (gitignored) | Via step E |
| Intro roster (OCR) | `outputs/<id>/intro_roster.json` | No (gitignored) | Via step F (may need manual fix) |
| Speaker overrides | `outputs/<id>/roster_overrides.json` | No (gitignored) | Via steps H+I |
| Lie analysis | `outputs/<id>/lie_analysis.csv` | No (gitignored) | Via step G |
| Landing page HTML | `landing.html` | No (gitignored) | Via step L |
| Live landing page | `gh-pages:index.html` | Yes (separate branch) | Via `deploy_pages.sh` |

---

## N0. video.scan_frames  [optional enrichment]

| Field | Value |
|-------|-------|
| **Purpose** | Extract visual game-phase signals from video frames using HSV colour thresholding; writes results to `botc.db` `frame_scan` table |
| **Script** | `scan_frames.py` |
| **Per-video?** | Yes |
| **Slot** | After A (download) — reads `video.mp4` directly |
| **Inputs** | `outputs/<id>/video.mp4` |
| **Outputs** | Rows in `botc.db`.`frame_scan` (timestamp, signal_type, confidence) |
| **Command** | `python scan_frames.py <video_id>` |
| **Acceptance** | `frame_scan` rows present for video; no errors |
| **Notes** | Detects two persistent UI elements (scoreboard, town-square background) via normalised crop regions (resolution-independent). Non-destructive: existing outputs untouched. Coverage: 48/53 processable videos. |

---

## N1. speaker.episode_consistency  [optional enrichment]

| Field | Value |
|-------|-------|
| **Purpose** | Reduce intra-episode diarization fragmentation by smoothing short speaker flips and A/B/A patterns; does NOT build cross-episode voice identities |
| **Script** | `speaker_consistency.py` |
| **Per-video?** | Yes |
| **Slot** | After C (diarize), before D (merge) — reads RTTM + raw segments |
| **Inputs** | `diarization.rttm`, `segments.csv` (or `segments_patched.csv`) |
| **Outputs** | `outputs/<id>/segments_consistent.csv` |
| **Command** | `python speaker_consistency.py <video_id>` |
| **Acceptance** | `segments_consistent.csv` exists with ≤ row count of input (merges reduce rows) |
| **Notes** | Non-destructive: original `segments.csv` / `diarization.rttm` untouched. Reports before/after flip-count. Controlled by `--min-flip-s` and `--context-s` CLI flags. |

---

## N2. speaker.day_boundary_detection  [optional enrichment]

| Field | Value |
|-------|-------|
| **Purpose** | Label each segment with its broad game phase (Intro / Night / Day) and detect day-scoped events (nominations, votes, executions) |
| **Script** | `detect_phases.py` |
| **Per-video?** | Yes |
| **Slot** | After E (patch) — reads `segments_patched.csv` |
| **Inputs** | `segments_patched.csv` (fallback: `segments_consistent.csv`, `segments.csv`); `frame_scan` rows from `botc.db` (optional, for high-confidence event anchors) |
| **Outputs** | `outputs/<id>/phase_labels.csv` (columns: start, end, phase, round, confidence, evidence); `outputs/<id>/day_events.csv` (columns: start, end, round, event, confidence, evidence) |
| **Command** | `python detect_phases.py <video_id>` |
| **Acceptance** | `phase_labels.csv` exists, rows cover full episode duration, `phase` column is one of `{Intro, Night, Day, Unknown}`; `day_events.csv` exists (may be empty if no nominations detected) |
| **Notes** | Purely text-based + speaker-count structural signals; no raw audio features. Storyteller auto-detected by word-count dominance in first 330 s. Low-confidence regions labelled `Unknown` then forward-filled. Structural triggers: ST+1-player 2-speaker window → Night evidence; ≥2 non-ST speakers → Day evidence. `--force` flag to overwrite. Legacy `phase_labels.csv` files containing `Nomination`/`Execution` produce a WARN in `validate.py` — re-run N2 to upgrade. |

---

## N3. content.claim_propagation  [optional enrichment]

| Field | Value |
|-------|-------|
| **Purpose** | Extract conversational events (role claims, accusations, suspicions, agreements, challenges) with timestamps, speakers, and targets; produce a claim relationship graph |
| **Script** | `extract_claims.py` |
| **Per-video?** | Yes |
| **Slot** | After G (analyze) — reads `lie_analysis.csv`, `segments_patched.csv`, roster data, optional `phase_labels.csv` |
| **Inputs** | `segments_patched.csv`, `intro_roster.json`, `roster_overrides.json` (optional), `phase_labels.csv` (optional), `lie_analysis.csv` |
| **Outputs** | `outputs/<id>/claims.csv`, `outputs/<id>/claim_graph.json` |
| **Command** | `python extract_claims.py <video_id>` |
| **Acceptance** | Both output files exist; `claims.csv` header valid; `claim_graph.json` is valid JSON |
| **Notes** | Event types: `role_claim`, `accusation`, `suspicion`, `agreement`, `challenge`. Graph nodes = claim events; edges = echo/support/challenge relationships. `lie_analysis.csv` rows are cross-referenced to mark verified lies. Blind games produce few or zero events (expected). |

---

## Scripts Not in the DAG (Support / Utility)

| Script | Role |
|--------|------|
| `calibrate_scraper.py` | Visual tuning tool for OCR crop regions in `scrape_intro.py` |
| `compare_rosters.py` | Diffs DB roster against the episode spreadsheet; fuzzy title matching |
| `retranscribe_all.py` | Bulk re-transcribe all eligible videos (e.g. after model upgrade) |
| `batch_transcribe.py` | Batch orchestration for retranscription runs with progress tracking |
| `batch_downstream.py` | Batch orchestration for downstream (merge/patch/analyze) processing |
| `explore.py` | Full Streamlit editor (read/write) — local only |
| `explore_public.py` | Read-only Streamlit viewer — public-facing analysis UI |
| `validate.py` | End-state validator — run after any pipeline phase |
| `speaker_consistency.py` | N1 optional enrichment: smooths diarization fragmentation |
| `detect_phases.py` | N2 optional enrichment: game-phase boundary detection |
| `extract_claims.py` | N3 optional enrichment: role-claim / accusation / suspicion extraction |

---

## Known Data Gaps (as of 2026-03-14)

| Video ID | Issue | Type | Path Forward |
|----------|-------|------|-------------|
| `0wGTes2sqmE` | 2 unlinked speakers; winner missing | manual | Watch video; set winner in `playlist.json`; run `fix_rosters.py` |
| `DbF9CPOueTI` | Winner missing | manual | Watch video; set winner in `playlist.json` |
| `OaAUvM4SAkg` | Winner missing | manual | Watch video; set winner in `playlist.json` |
| `ggM9BH__xtU` | Blind game; winner unknown; no intro roster | manual | Watch video; set winner in `playlist.json` |
| `OPqWyO7h-wM` | 1 unlinked speaker | manual | `streamlit run fix_rosters.py` |
| `QbzFmlScLSA` | 3 unlinked speakers | manual | `streamlit run fix_rosters.py` |
| `DzTk6kSIg-M` | 4 unlinked speakers | manual | `streamlit run fix_rosters.py` |
| `IUO3Xz1kNkc` | 4 unlinked speakers | manual | `streamlit run fix_rosters.py` |
| `DAb9sq5ku2k` | Bonus format; only 4/14 claims verified | data/manual | Manual `roster_overrides.json` — low priority |
| `d2M-N5iABRo`, `OYTaTtjk3ac`, `z79AJOPoNi4` | Members-only; no audio | access | Download with valid membership cookies |
