# Context Segments — Artifact Contract

> **Last updated:** 2026-03-18
> **Status:** v1 — produced by `generate_context_segments.py` (N2b), consumed by N4

---

## Purpose

`context_segments.csv` is a shared, inspectable artifact that maps time intervals
in a BotC game video to structured interpretation context.  It is produced by
`generate_context_segments.py` (conceptually node N2b — runs after N2) and consumed
by downstream enrichment nodes, primarily N4 (`extract_player_status.py`).

The artifact makes interpretive decisions explicit and inspectable.  Rather than each
extraction node reimplementing "is this segment during night?" or "is this a private
demon–ST conversation?" independently, `context_segments.csv` provides a single
shared answer per time interval that every consumer can query.

---

## Output file

```
outputs/<video_id>/context_segments.csv
```

Non-destructive: if `phase_labels.csv` is absent for a video, the output will be
empty.  The absence of `context_segments.csv` does not block any other node —
consumers fall back gracefully to raw phase labels.

---

## Columns

| Column | Type | Values | Description |
|--------|------|---------|-------------|
| `timestamp_start` | float | seconds | Interval start (inclusive) |
| `timestamp_end` | float | seconds | Interval end (exclusive) |
| `broad_phase` | str | `intro` \| `night` \| `day` \| `unknown` | Broad game phase from N2 phase_labels |
| `context_mode` | str | see table below | Fine-grained interpretation mode for this interval |
| `speaker_type` | str | `storyteller` \| `player` \| `mixed` \| `unknown` | Who is expected to speak in this interval |
| `audience_scope` | str | `public` \| `private_like` \| `ambiguous` | Who hears the speech in this interval |
| `confidence` | float | 0.0–1.0 | Confidence in this row's classification |
| `source` | str | see table below | Primary signal that produced this row |

---

## `context_mode` values

| Value | Broad phase | `audience_scope` | Description |
|-------|-------------|------------------|-------------|
| `intro_meta` | intro | public | Pre-game role introductions |
| `night_private_action` | night | private_like | Night phase — ST resolves kill and other night actions privately |
| `storyteller_narration` | night or day | private_like | ST speaking 1:1 with one player (StorytellerInterruption during Day, or Night with only ST audible) |
| `morning_result_announcement` | day | public | Day-start window after a Night phase: ST announcing last night's death |
| `public_day_discussion` | day | public | Open group discussion, nominations, debate |
| `execution_window` | day | public | Active or just-completed vote/execution sequence (VoteSequence event) |
| `ambiguous` | any | ambiguous | Insufficient signal to classify reliably |

**Contract for consumers:** unknown `context_mode` values that appear in future
versions should be treated as `ambiguous`.  Do not raise an error on unknown values.

---

## `source` values

| Value | Description |
|-------|-------------|
| `phase_label` | Directly derived from a `phase_labels.csv` row |
| `day_event:VoteSequence` | Refined by a `VoteSequence` row in `day_events.csv` |
| `day_event:StorytellerInterruption` | Refined by a `StorytellerInterruption` row in `day_events.csv` |
| `day_event:morning_window` | First `MORNING_WINDOW_S` seconds of a Day phase after a Night phase |

---

## Coverage

Rows cover the full video duration from the first to the last `phase_labels.csv`
interval.  Row boundaries align exactly with `phase_labels.csv` boundaries plus
`day_events.csv` event boundaries within Day phases.

Within a Day phase, sub-intervals for detected events (VoteSequence,
StorytellerInterruption) take priority over the base Day classification.  Priority
order (highest to lowest) when intervals overlap:

1. `execution_window` (VoteSequence)
2. `storyteller_narration` (StorytellerInterruption)
3. `morning_result_announcement` (morning window after Night)
4. `public_day_discussion` (default)

---

## Inputs required

| Input | Location | Required? |
|-------|----------|-----------|
| `phase_labels.csv` | `outputs/<id>/phase_labels.csv` | Yes — without it the output will be empty |
| `day_events.csv` | `outputs/<id>/day_events.csv` | Optional — absence means no event-level sub-intervals |

---

## Consumers

### N4 — `extract_player_status.py`

Uses `context_segments.csv` for two improvements over raw phase-label lookup:

1. **Night-target pattern scope**: In addition to Night phase segments,
   night-target patterns (kill-intent) now also fire during `private_like` intervals
   within Day (i.e., `StorytellerInterruption` events where the demon whispers
   their kill choice to the ST at end of Day).  This catches real targeting dialogue
   that currently falls in a Day-labelled segment.

2. **Death signal confidence adjustment**: Death signals during
   `morning_result_announcement` receive a small confidence boost (the morning is
   when the ST announces last night's victim — high prior probability that a death
   mention is real).  Execution-type signals during `execution_window` receive a
   corresponding boost.

Fallback: if `context_segments.csv` is absent, N4 falls back to raw
`_phase_at()` / `_has_vote_sequence_before()` lookups — identical to pre-v1 behaviour.

---

## Stability guarantees

- Column set: stable in v1; new columns may be added later with defaults
- `context_mode` enum: may grow; consumers must handle unknown values as `ambiguous`
- Row count: O(N_phases + N_day_events) per video — typically 20–100 rows
- All timestamps in seconds (float), consistent with other pipeline CSVs
