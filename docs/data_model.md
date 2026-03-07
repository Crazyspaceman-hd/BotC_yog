# BotC_yog — Data Model

> **Last updated:** 2026-03-07
> This document defines the conceptual model that underlies the pipeline.
> It separates **entities** (stable objects), **states** (mutable conditions),
> **events** (discrete occurrences), and **derived enrichments** (algorithm output).
> Use it as a reference when designing new pipeline nodes or DB tables.

---

## Entities

The permanent, identity-bearing objects in the domain.

| Entity | Description | Primary key |
|--------|-------------|-------------|
| **game** | A single recorded BotC game (one YouTube video) | `video_id` |
| **player** | A named participant, identified across games | canonical name (`player_aliases.json`) |
| **role** | A BotC role definition (e.g. Imp, Mayor, Washerwoman) | `roles.name` (Title Case) |
| **speaker_label** | A diarization-assigned ID (e.g. `speaker_0`) — technical artifact, not a person | `(video_id, speaker_id)` |

`speaker_label` maps to `player` via `roster_overrides.json` + `intro_roster.json`.
`speaker_0` is always the Storyteller (`STORYTELLER_ID = "speaker_0"`).

---

## States

Per-player mutable conditions that hold during a segment of the game.
States change in response to Events.

| State | Applies to | Values | Notes |
|-------|-----------|--------|-------|
| **role** | player | any `roles.name` value | Set at game start; may change mid-game (e.g. Snake Charmer swap) |
| **alignment** | player | Good / Evil | Derived from `role` via `botc_ui._team()`; never hardcoded |
| **alive** | player | alive / dead | Changes on death events |
| **poisoned** | player | bool | Set by Poisoner night action; clears at next night end |
| **drunk** | player | bool | Set by Drunk self-state; permanent until death |
| **harpied** | player | bool | Set by Harpy night action |
| **pixie** | player | bool | Pixie player may inherit a Townsfolk role on that player's death |

States are not currently tracked segment-by-segment in the DB.
They are inferred from `intro_roster.json` and `lie_analysis.csv` at query time.

---

## Events

Discrete observable occurrences anchored to a timestamp and a speaker.

| Event | Source | Notes |
|-------|--------|-------|
| **phase_transition** | `detect_phases.py` (N2 / phase_detection) | Intro → Night → Day → Nomination → Execution arc |
| **utterance** | `segments_patched.csv` | Every transcribed speech segment with speaker label |
| **storyteller_announcement** | `analyze_roles.py` | ST declares death, night result, role reveal |
| **nomination** | `extract_claims.py` (N3 / claim_extraction) | Player nominates another for execution |
| **vote** | `extract_claims.py` | Player casts yes/no vote on a nomination |
| **execution** | `extract_claims.py` | ST announces execution outcome |
| **night_action** | *(not yet extracted)* | Player wakes and performs role ability |
| **death** | `analyze_roles.py` + `extract_claims.py` | Player dies via night-kill or execution |

---

## Derived Enrichments

Outputs produced by automated analysis of raw events and utterances.
These are best-effort; accuracy depends on transcript quality and curation state.

| Enrichment | Script | Output artifact |
|------------|--------|-----------------|
| **speaker_consistency** | `speaker_consistency.py` (N1) | `segments_consistent.csv` |
| **phase_detection** | `detect_phases.py` (N2) | `phase_labels.csv` |
| **claim_events** | `extract_claims.py` (N3) | `claims.csv` |
| **claim_graph** | `extract_claims.py` (N3) | `claim_graph.json` |
| **lie_detection** | `analyze_roles.py` | `lie_analysis.csv` |

`claim_graph` nodes are claim_events; edges are echo / support / challenge relationships.
Future work: belief propagation over the claim_graph using phase labels as context windows.

---

## Naming Conventions

| Concept | Convention | Example |
|---------|-----------|---------|
| Role names in DB / JSON | Title Case, spaces | `"Imp"`, `"Snake Charmer"` |
| Role comparisons in code | `normalize_role()` → lowercase, spaces | `"snake charmer"` |
| Role display | `display_role()` → Title Case | `"Snake Charmer"` |
| Team lookup | `botc_ui._team(role_name)` | returns `"Evil"` or `"Good"` |
| Enrichment node aliases | N1 = speaker_consistency, N2 = phase_detection, N3 = claim_extraction | |
