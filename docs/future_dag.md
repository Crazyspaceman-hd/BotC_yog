# BotC_yog — Future DAG: Accuracy Improvements

> **Status:** planned, NOT yet implemented.
> These nodes extend the pipeline defined in `pipeline_dag.md`.
> Do not implement until the current DAG is stable and fully validated.

---

## M. speaker.day_boundary_detection

| Field | Value |
|-------|-------|
| **Purpose** | Detect transitions between Night / Day / Nomination phases and tag each segment with its phase |
| **Slot in DAG** | After D (merge), before G (analyze) |
| **Per-video?** | Yes |
| **Inputs** | `segments.csv` or `segments_patched.csv`, Storyteller speech patterns, audio energy features |
| **Outputs** | `outputs/<id>/phase_labels.csv` (columns: start, end, phase = Night/Day/Nomination/Execution) |
| **Validation** | Manual spot-check of 5 videos; confirm day count matches known game records |
| **Notes** | Phase labels would let `analyze_roles.py` distinguish "Day claims" from "Night whispers". Also enables per-day lie statistics. Likely requires: (a) Storyteller segment detection (speaker_0), (b) silence/energy gap detection, (c) heuristic keyword triggers ("it is now day", "nominations are open"). |

---

## N. speaker.accuracy_improvement

| Field | Value |
|-------|-------|
| **Purpose** | Improve diarization quality for videos where multiple speakers are unlinked or confused |
| **Slot in DAG** | Replaces / supplements C (diarize) for specific videos |
| **Per-video?** | Yes (applied selectively) |
| **Inputs** | `audio_16k.wav`, optional speaker reference samples from the intro |
| **Outputs** | Improved `diarization.rttm` |
| **Validation** | Compare linked-speaker counts before/after; `validate.py` unlinked_speakers check should improve |
| **Approaches** | (a) Speaker-conditioned diarization using intro audio clips as reference embeddings. (b) Post-processing: merge short segments from the same speaker. (c) Pyannote v3 as a second-pass alternative to NeMo. |
| **Blockers** | Requires `.venv_botc` GPU environment; NeMo or Pyannote v3 API changes |

---

## O. content.vote_extraction

| Field | Value |
|-------|-------|
| **Purpose** | Detect and record nomination + vote events (who was nominated, who voted to execute, outcome) |
| **Slot in DAG** | After G (analyze), before J (build_db) — writes per-video vote artifact |
| **Per-video?** | Yes |
| **Inputs** | `segments_patched.csv` (or `segments.csv`) with phase labels from M |
| **Outputs** | `outputs/<id>/votes.json` (nominations, voters, executed player, time) |
| **Validation** | Vote counts should match game records; executed player should match known outcomes |
| **DB impact** | New `votes` table in `botc.db`; `build_db.py` extended to ingest |
| **Notes** | High false-positive risk if phase labels are missing. Patterns to detect: "I nominate [player]", "I vote yes/no", "X has been executed". |

---

## P. content.role_change_detection

| Field | Value |
|-------|-------|
| **Purpose** | Detect mid-game role changes (e.g. Snake Charmer swap, Barber swap, Engineer transform) |
| **Slot in DAG** | After G (analyze) — annotates the lie_analysis with role-change events |
| **Per-video?** | Yes |
| **Inputs** | `segments_patched.csv`, `intro_roster.json`, `lie_analysis.csv`, `roster_overrides.json` |
| **Outputs** | `outputs/<id>/role_changes.json` (player, old_role, new_role, timestamp, evidence) |
| **Validation** | Cross-check against known game mechanics; roles that can change: Snake Charmer (swap), Barber (swap post-death), Engineer (new demon), Cannibal (dead player's role) |
| **DB impact** | New `role_changes` table; `lies` table extended with optional `post_change_role` column |
| **Notes** | Requires `analyze_roles.py` to support updating a player's `actual_role` mid-game. Currently the role is assumed static for the whole game. |

---

## Q. data.playlist_auto_update

| Field | Value |
|-------|-------|
| **Purpose** | Automatically detect new videos in the YouTube playlist and add them to `playlist.json` |
| **Slot in DAG** | Runs before Phase 1 (per-video extraction); triggered on a schedule |
| **Per-video?** | No — cross-video / global |
| **Inputs** | YouTube playlist URL (cached in `playlist.json`) |
| **Outputs** | Updated `playlist.json` with new video entries at `status: pending` |
| **Validation** | New video IDs appear in `playlist.json`; no existing entries overwritten |
| **Command** | `python fetch_playlist.py` (already implemented — just needs scheduling) |
| **Notes** | Could be a GitHub Actions cron job that runs `fetch_playlist.py` + `validate.py` weekly. |

---

## Integration order (when all future nodes are ready)

```
A download → B transcribe → C diarize* → N speaker_accuracy?
                                          ↓
                           D merge → M day_boundary_detection
                                     ↓
                E patch → F scrape → G analyze → O vote_extraction
                                                 P role_change_detection
                                                 ↓
                H auto_assign → I fix_rosters (manual) → J build_db
                                                           ↓
                                              K botc_ui (library)
                                              L generate_landing
                                              Q auto playlist update
```

`*` N (speaker_accuracy) runs conditionally — only for videos with many unlinked speakers.
