# BotC_yog — Pipeline DAG

> **Last updated:** 2026-03-19
> **State:** covers all scripts present on `feat/player-status-tracking` as of 2026-03-19; N0–N7 are optional enrichment nodes; player-name normalization via `normalize_player()` added to `pipeline_utils.py`

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
| **Enrichment** | N0–N7 optional nodes | per-video, non-blocking |

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
                   |
                   └─► [N2b. context.segment_labelling]  ← optional, writes context_segments.csv

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
                   ├─► [N4. content.player_status_tracking] ← optional, reads context_segments.csv (N2b), writes player_status.csv + death_events.csv + night_target_events.csv
                   |        |
                   |        └─► [N5. content.execution_context] ← optional, reads context_segments.csv (N2b), writes execution_context_events.csv
                   |                  |
                   |                  └─► [N6. content.execution_episodes] ← optional, reads day_events.csv (N2) + execution_context_events.csv (N5), writes execution_episodes.csv
                   |                             |
                   |                             └─► [N7. content.execution_claim_context] ← optional, reads execution_episodes.csv (N6) + claims.csv (N3), writes execution_claim_context.csv
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

**N0/N1/N2/N2b/N3/N4/N5/N6/N7 are optional enrichment nodes.** They do not affect playlist.json status, do not block any existing step, and are not required by J (build_db). N0 writes directly to `botc.db` (frame_scan table); N1–N7 and N2b write new artifact files alongside existing outputs.

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

**Death events** — produced by N4 (player_status_tracking / `extract_player_status.py`):

| Status / Event | Description |
|---------------|-------------|
| `alive` | Player is alive (initial state from intro_roster; also transitions back from unknown) |
| `dead` | Player has died (from execution, night kill, or uncertain cause) |
| `unknown` | Status cannot be determined from available signals |
| `execution` | Death by nomination vote (associated with a preceding VoteSequence day event) |
| `night_death` | Death by night action (demon kill, poisoner, etc.) |
| `uncertain_death` | Death confirmed but cause not classifiable from signals |

N4 consumes phase_labels (N2) and context_segments (N2b) for temporal context and cause
classification. Visual corroboration from frame_scan (N0) provides a confidence boost
when the header bar is visible at the time of the death signal.
Conservative policy: false negatives preferred over false positives — only
emit a death event when confidence ≥ 0.45.

**Public execution events** — produced by N5 (execution_context / `extract_execution_context.py`):

| Event Type | Description |
|------------|-------------|
| `public_kill_pressure` | Player explicitly pushed for another player's execution ("we should kill X", "vote for X", "X should die") |
| `nomination_reference` | Player explicitly nominated another by name ("I nominate X", "nominating X") |
| `execution_result_narration` | ST (or confirmed narrator) announcing execution outcome ("Goodbye X", "X was executed", "X, last words") |
| `execution_opposition` | Speaker defending a player against execution push ("don't kill X", "X is innocent") |

N5 consumes context_segments (N2b) to gate patterns strictly to public Day context
(`public_day_discussion` or `execution_window` audience_scope). This prevents "kill X"
language in private Night or StorytellerInterruption segments (where a demon selects
their night target) from leaking into public execution-pressure records. N4 captures
private kill intent; N5 captures public execution pressure — the two are disjoint.
Conservative policy: false negatives preferred — `_MIN_CONF = 0.60` (higher than N4's
0.45, reflecting noisier public-day language).

**Execution episodes** — produced by N6 (execution_episodes / `build_execution_episodes.py`):

| Field | Description |
|-------|-------------|
| `likely_target_player` | Inferred from nomination_reference and public_kill_pressure signals in a ±300 s window around the vote; blank if ambiguous |
| `execution_result_player` | From execution_result_narration after the vote window (up to +300 s); independent of inferred target |
| `nomination_speakers` | Players who made nomination_reference events in the episode window |
| `pressure_speakers` | Players who applied public_kill_pressure in the episode window |
| `opposition_speakers` | Players who defended the target against execution |
| `confidence` | Aggregated from VoteSequence confidence + nomination/pressure/result evidence |

N6 produces one row per Day-phase VoteSequence event, aggregating N5 public signals
and N2 vote windows into a single inspectable artifact per execution episode.
`likely_target_player` and `execution_result_player` are stored separately:
one is inferred from pre-vote town discussion, the other confirmed from result narration.
These can differ (town pushes player A but executes player B via a different nomination),
which is meaningful game data. Conservative: target is left blank when evidence is
ambiguous (competing candidates within _AMBIGUITY_RATIO=1.4 and _AMBIGUITY_GAP=0.20).

**Execution claim context** — produced by N7 (execution_claim_context / `build_execution_claim_context.py`):

| Field | Description |
|-------|-------------|
| `target_claimed_role` | The role the likely_target_player was publicly claiming near the vote; blank if no qualifying claim exists |
| `result_claimed_role` | The role the execution_result_player was claiming when executed; blank if no qualifying claim exists |
| `*_claim_confidence` | Confidence of the selected claim (from N3 claims.csv) |
| `*_claim_type` | Event type of the selected claim (`role_claim`) |
| `*_claim_text` | First 100 chars of the claim utterance (for inspection) |
| `*_claim_source_timestamp` | When the claim was made (seconds from start) |
| `*_claim_recency_s` | Seconds between claim and vote_window_start (staleness measure) |
| `*_claim_stale` | `true` if recency > 5400 s — flag only; stale claims are still included |
| `*_claim_match_status` | `lie` (verified_lie==true), `truthful` (claimed == actual), or `unverified` |
| `*_claim_speaker` | Player who made the claim (always the player themselves for role_claim) |

N7 joins each N6 episode row with N3 claims.csv using a conservative per-player lookup.
Claim selection policy: most recent role_claim with confidence ≥ 0.35 before vote_window_end,
sorted by confidence DESC → intro-phase tier → recency DESC. If no qualifying claim exists,
all claim fields are left blank (false negative preferred over forced guess).
target and result player claims are resolved independently.

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
| Player status (N4) | `outputs/<id>/player_status.csv` | No (gitignored) | Via N4 (`extract_player_status.py`) |
| Death events (N4) | `outputs/<id>/death_events.csv` | No (gitignored) | Via N4 (`extract_player_status.py`) |
| Night target events (N4) | `outputs/<id>/night_target_events.csv` | No (gitignored) | Via N4 (`extract_player_status.py`) |
| Context segments (N2b) | `outputs/<id>/context_segments.csv` | No (gitignored) | Via N2b (`generate_context_segments.py`) |
| Execution context events (N5) | `outputs/<id>/execution_context_events.csv` | No (gitignored) | Via N5 (`extract_execution_context.py`) |
| Execution episodes (N6) | `outputs/<id>/execution_episodes.csv` | No (gitignored) | Via N6 (`build_execution_episodes.py`) |
| Execution claim context (N7) | `outputs/<id>/execution_claim_context.csv` | No (gitignored) | Via N7 (`build_execution_claim_context.py`) |
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

## N2b. context.segment_labelling  [optional enrichment]

| Field | Value |
|-------|-------|
| **Purpose** | Synthesize fine-grained interpretation context for each time interval; bridge N2 phase labels and N4 death/target extraction; make interpretive decisions explicit and inspectable |
| **Script** | `generate_context_segments.py` |
| **Per-video?** | Yes |
| **Slot** | After N2 (phase_detection) — reads `phase_labels.csv` + `day_events.csv` |
| **Inputs** | `outputs/<id>/phase_labels.csv` (N2, required); `outputs/<id>/day_events.csv` (N2, optional) |
| **Outputs** | `outputs/<id>/context_segments.csv` — one row per time interval (columns: timestamp_start, timestamp_end, broad_phase, context_mode, speaker_type, audience_scope, confidence, source) |
| **Command** | `python generate_context_segments.py <video_id>` \| `python generate_context_segments.py --all` |
| **Acceptance** | `context_segments.csv` exists; rows cover full video duration from phase_labels; `context_mode` values are one of the documented set |
| **Downstream** | N4 (`extract_player_status.py`) — consumes for night-target scope and confidence adjustments |
| **Notes** | Non-destructive: no other artifacts touched. Absence does not break N4 — N4 falls back to raw `_phase_at()` lookup. Context modes: `intro_meta`, `night_private_action`, `storyteller_narration`, `morning_result_announcement`, `public_day_discussion`, `execution_window`, `ambiguous`. Day phases are sub-divided by VoteSequence and StorytellerInterruption events. Morning window (first 600 s of a Day following a Night) labelled `morning_result_announcement`. Contract: `docs/context_contract.md`. |

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

## N4. content.player_status_tracking  [optional enrichment]

| Field | Value |
|-------|-------|
| **Purpose** | Detect player deaths from transcript + visual signals; emit per-player status transitions and classified death events |
| **Script** | `extract_player_status.py` |
| **Per-video?** | Yes |
| **Slot** | After G (analyze) — reads roster data, segments, and optional N2 phase labels + N0 frame scan |
| **Inputs** | `segments_patched.csv` (fallback: `segments.csv`), `intro_roster.json`, `roster_overrides.json` (optional), `phase_labels.csv` (N2, optional), `day_events.csv` (N2, optional), `context_segments.csv` (N2b, optional), `frame_scan` rows from `botc.db` (N0, optional) |
| **Outputs** | `outputs/<id>/player_status.csv` (per-player status transitions), `outputs/<id>/death_events.csv` (one row per death), `outputs/<id>/night_target_events.csv` (one row per intended kill target), `outputs/<id>/name_resolution_debug.csv` (optional — emitted when ≥1 variant found) |
| **Command** | `python extract_player_status.py <video_id>` \| `python extract_player_status.py --all` |
| **Acceptance** | Both CSVs exist; `death_events.csv` `event_type` column contains only `{execution, night_death, uncertain_death}`; no rows with confidence below threshold |
| **Notes** | Signal priority: visual (frame_scan `header_visible`) > ST transcript > general transcript. Storyteller detected as highest word-count speaker in first 330 s (mirrors N2). ST excluded from self-declaration path. Conservative: `_MIN_CONF=0.45`; prefers false negatives. Death cause classification uses N2 VoteSequence lookback (120 s) and Day-phase start window (600 s). `--force` flag to overwrite existing output. **Transcript name normalization** (added 2026-03-15): per-video ASR variant discovery — alias variants from `player_aliases.json` and fuzzy variants (difflib `SequenceMatcher`, threshold 0.78, min length 5 chars for both token and player key) are added as regex patterns at `_VARIANT_CONF_SCALE=0.95`. Possessives and short-name ambiguities are filtered. Self-declaration false positives suppressed via 60 s reaction window. Short player names (≤4 chars) use alias-only matching. **Night target events** (added 2026-03-15): kill-intent patterns detected during Night phase are recorded separately from confirmed deaths in `night_target_events.csv`. Fields include `source_speaker`, `target_player`, `evidence_type` (`named_intent` or `split_intent`), `candidate_actor_alignment` (Evil/Good/unknown from per-video roster), and `outcome_relation` (`matched_actual_death` / `did_not_match_actual_death` / `no_confirmed_death` / `unknown`). An intended target NEVER creates a confirmed death by itself. This enables "targeting behaviour" analysis (project charter). **Reconciliation policy** (tightened 2026-03-18): intended-target records are only linked to confirmed deaths if (a) the death is a `night_death` event (not an execution) and (b) it falls within 600 s of the targeting event. Executions are never linked. No mechanic (protection / redirect / bounce) is inferred from target ≠ victim — `did_not_match_actual_death` only asserts that the target survived and a different night-kill occurred in the same window. |

---

## N5. content.execution_context  [optional enrichment]

| Field | Value |
|-------|-------|
| **Purpose** | Extract public kill pressure, nomination references, and execution-result narration from the game transcript, gated strictly to public Day context via the context layer (N2b) |
| **Script** | `extract_execution_context.py` |
| **Per-video?** | Yes |
| **Slot** | After G (analyze) and N2b (context labelling) — reads `segments_patched.csv` + `context_segments.csv` |
| **Inputs** | `segments_patched.csv` (fallback: `segments_consistent.csv`, `segments.csv`), `intro_roster.json`, `roster_overrides.json` (optional), `phase_labels.csv` (N2, optional), `context_segments.csv` (N2b, optional) |
| **Outputs** | `outputs/<id>/execution_context_events.csv` — one row per execution-context event (columns: timestamp_start, speaker, source_player_name, target_player, target_role_if_any, event_type, confidence, context_mode, phase, source_text) |
| **Command** | `python extract_execution_context.py <video_id>` \| `python extract_execution_context.py --all` |
| **Acceptance** | `execution_context_events.csv` exists (empty CSV with header is valid if no qualifying events); `event_type` column contains only `{public_kill_pressure, nomination_reference, execution_result_narration, execution_opposition}`; no events in private_like context |
| **Downstream** | Future vote-reconstruction and nomination-graph analytics (not yet implemented) |
| **Notes** | **Context gate (strict):** `public_kill_pressure`, `nomination_reference`, and `execution_opposition` fire ONLY when `audience_scope == "public"` AND `phase == "Day"` AND `context_mode in {public_day_discussion, execution_window}`. This prevents Night-phase and StorytellerInterruption private-like segments from producing day-pressure events. `execution_result_narration` fires in any Day context and is boosted when spoken by the Storyteller speaker AND in `execution_window` or `morning_result_announcement` context. **N4/N5 disjoint guarantee:** N4 captures private kill intent (Night + private_like); N5 captures public execution pressure (public Day only) — the audience_scope gate in context_segments.csv is the enforcing boundary. Conservative: `_MIN_CONF = 0.60`. Same alias + fuzzy name-variant discovery as N4 (`_FUZZY_NAME_THRESHOLD = 0.78`). Deduplication: same (speaker, target_player, event_type) within 30 s → keep highest confidence. `--force` flag to overwrite existing output. **Batch run across 47 videos:** 245 total events (nomination_reference=114, public_kill_pressure=113, execution_result_narration=9, execution_opposition=9). |

---

## N6. content.execution_episodes  [optional enrichment]

| Field | Value |
|-------|-------|
| **Purpose** | Aggregate N2 VoteSequence windows and N5 execution-context events into one row per execution episode; describe likely target, who drove the conversation, and whether result narration confirms the executed player |
| **Script** | `build_execution_episodes.py` |
| **Per-video?** | Yes |
| **Slot** | After N5 (execution context) — reads `execution_context_events.csv` + `day_events.csv` |
| **Inputs** | `day_events.csv` (N2, required), `execution_context_events.csv` (N5, optional), `phase_labels.csv` (N2, optional — used for Night-phase filtering), `intro_roster.json`, `roster_overrides.json` (optional) |
| **Outputs** | `outputs/<id>/execution_episodes.csv` — one row per Day-phase VoteSequence event (columns: timestamp_start, timestamp_end, phase, round, vote_window_start, vote_window_end, likely_target_player, likely_target_role_if_any, evidence_count, supporting_event_types, confidence, nomination_speakers, pressure_speakers, opposition_speakers, execution_result_player, matched_vote_sequence, source_timestamps) |
| **Command** | `python build_execution_episodes.py <video_id>` \| `python build_execution_episodes.py --all` |
| **Acceptance** | `execution_episodes.csv` exists; one row per Day-phase VoteSequence; `likely_target_player` blank when evidence is ambiguous or absent; `execution_result_player` independent of inferred target |
| **Downstream** | Future vote-reconstruction, execution-strategy analysis, claimed-role-at-execution dataset |
| **Notes** | **Conservative scoring:** candidate target scored from pre-vote N5 events using weighted sum (`nomination × 1.0`, `kill_pressure × 0.7`, `opposition × 0.3`). Target resolved only if top candidate exceeds second by ratio ≥ 1.4 AND gap ≥ 0.20 — otherwise `likely_target_player = ""`. **Night-phase filtering:** VoteSequences where both start and end fall in Night are excluded (frame-scan misdetections); VoteSequences that straddle Night→Day boundaries are included if either endpoint falls in Day. **Lookback:** 300 s before vote window start for nomination/pressure evidence. **Result lookahead:** 300 s after vote window end for execution_result_narration confirmation. `likely_target_player` and `execution_result_player` are stored separately — they can differ (town discusses one target but executes another via a different nomination); this divergence is real game signal. Episodes with zero N5 evidence are still emitted (conf ≈ 0.40) so vote coverage is always visible. **Batch results (47 videos):** 311 episodes — resolved targets: 97/311 (31%); result confirmed: 9/311; confirmed matches (target == result): 1/311. |

---

## N7. content.execution_claim_context  [optional enrichment]

| Field | Value |
|-------|-------|
| **Purpose** | Join N6 execution episodes with N3 role claims to describe what role each player was publicly claiming near the time they were nominated or executed |
| **Script** | `build_execution_claim_context.py` |
| **Per-video?** | Yes |
| **Slot** | After N6 (execution_episodes) and N3 (claim_extraction) — reads `execution_episodes.csv` + `claims.csv` |
| **Inputs** | `execution_episodes.csv` (N6, required), `claims.csv` (N3, optional — all claim fields blank if absent) |
| **Outputs** | `outputs/<id>/execution_claim_context.csv` — one row per execution episode (all N6 episode fields plus target/result claim fields; see Game Phases section above) |
| **Command** | `python build_execution_claim_context.py <video_id>` \| `python build_execution_claim_context.py --all` |
| **Acceptance** | `execution_claim_context.csv` exists with one row per N6 episode; blank claim fields when no qualifying claim found (not a failure); `target_claim_match_status` and `result_claim_match_status` contain only `{lie, truthful, unverified, ""}` |
| **Notes** | **Claim selection:** `event_type == role_claim`, `confidence >= 0.35` (`_CLAIM_MIN_CONF`), `timestamp_seconds <= vote_window_end`. Sort priority: confidence DESC → intro-phase tier DESC → recency DESC. This prioritises explicit hard claims (conf 0.85) over garbled N3 fragments (conf 0.425), and Intro-phase reveals over in-game declarations. **Staleness:** `claim_recency_s = vote_window_start - claim_timestamp`. Claims older than 5400 s (`_CLAIM_STALE_S`) are flagged `claim_stale=true` but still included — the consumer decides whether to filter them. **False-negative policy:** blank is always preferred over a forced guess. No claim interpolation, no belief-state reconstruction. **Player matching:** exact case-insensitive match on `player_name` from claims.csv — videos with incomplete roster linking (e.g. `speaker_0`/`speaker_1` as player_name) produce zero resolved claims; this is expected conservative behaviour. **Batch results (47 videos):** 311 episodes, target_claimed_role resolved: 52/311 (17%), result_claimed_role resolved: 3/311 (1%), lies detected: 17 episodes. |

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
| `generate_context_segments.py` | N2b optional enrichment: context labelling from N2 outputs → `context_segments.csv` |
| `extract_claims.py` | N3 optional enrichment: role-claim / accusation / suspicion extraction |
| `extract_player_status.py` | N4 optional enrichment: player death / status tracking |
| `extract_execution_context.py` | N5 optional enrichment: public execution-context event extraction → `execution_context_events.csv` |
| `build_execution_episodes.py` | N6 optional enrichment: execution-episode aggregation from N2 vote windows + N5 events → `execution_episodes.csv` |
| `build_execution_claim_context.py` | N7 optional enrichment: claimed-role-at-execution join from N6 episodes + N3 claims → `execution_claim_context.csv` |

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
