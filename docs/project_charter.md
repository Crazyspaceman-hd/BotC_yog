# BotC_yog Project Charter

## Purpose

BotC_yog exists to build a structured, inspectable dataset from Yogscast Blood on the Clocktower games so we can answer a specific set of analysis questions about:

- speech
- deception
- targeting
- survival
- execution pressure
- rumor and sentiment flow
- alignment and outcome patterns

This project is not trying to extract every possible signal from every video.

The project should be judged by whether it improves our ability to answer the analysis questions below in a reliable, reproducible way.

## AI-friendly document rules

This document is written primarily for AI-assisted development.

Follow these rules when using or updating it:

1. Treat this document as a strategy document, not a low-level implementation spec.
2. Prefer explicit definitions over implied intent.
3. Prefer stable analysis goals over opportunistic extraction work.
4. Do not expand scope unless the new work clearly improves one of the core analysis goals.
5. If a proposed feature does not map to a listed analysis goal, it should usually be delayed.
6. When this document conflicts with implementation habits from older threads, prefer this document.
7. Keep future edits short, explicit, and easy for an AI agent to follow.
8. When changing project direction, update this document before or alongside implementation.

## North star

Build a reliable enough structured BotC dataset to answer questions about deception, speech, targeting, survival, and information flow.

Not the north star:

- extract everything possible from the videos
- solve every rare edge case before core analytics are stable
- build large complex systems that are weakly connected to downstream analysis

## Primary analysis goals

### Player behavior
- Who talks the most?
- Who talks the least?
- Who lies the most?
- Who lies the least?
- Does being evil affect how much a player speaks?
- Does being evil affect how often a player lies?

### Role behavior
- Do certain roles dominate conversations?
- Do certain roles produce more lies?
- Are some roles more likely to be executed?
- Are some roles more likely to survive?

### Survival and execution strategy
- Is there a go-to claimed role that good players gravitate toward when dying?
- Is there a go-to claimed role that evil players gravitate toward when surviving?
- Is there a go-to claimed role that good players gravitate toward when surviving?
- Which claimed roles get executed most often?
- Which actual roles get killed most often?

### Outcome and alignment
- What is the good vs evil win/loss record?
- Which behaviors correlate with survival?
- Which behaviors correlate with winning?

### Targeting behavior
- What are popular targets for demons, by player and by role?
- What are popular targets for minions, by player and by role?
- How often does intended target differ from actual victim?

### Social dynamics
- How do rumors spread among the town?
- Who starts accusations?
- Who amplifies accusations?
- Who defends others?
- How does sentiment shift over the course of the game?

## Canonical analysis definitions

These definitions should stay stable unless there is a clear reason to change them.

### Talks the most / least
Primary measures:
- total speaking time
- total segment count
- share of total game speech

Secondary cuts:
- by phase
- by day
- by alignment
- by role

### Lies the most / least
Primary measures:
- lie count
- lies per game
- lies per minute spoken
- lies per claim

Important:
- always distinguish total lies from normalized lie rate

### Go-to role to die / survive
Analyze this first by:
- claimed role
- public role perception

Then compare against:
- actual hidden role

Reason:
In-game decisions are based on public information, not post-game truth.

### Death
Death must be represented as both:
- a status transition
- an event with timing and cause or context

### Targeting
Keep intended target and actual victim separate whenever possible.

### Rumor spread
A rumor is not only an accusation. It can include:
- a role claim repeated by others
- a suspicion repeated by others
- a narrative about a player that spreads across multiple speakers

## In scope

- speaker-linked transcript analysis
- speaking-time analytics
- claim extraction
- lie extraction
- phase and day-event tracking
- player status tracking
- death-event tracking
- target-intent extraction
- rumor and sentiment propagation analysis
- role, alignment, and outcome analysis
- inspectable intermediate artifacts
- reproducible DB ingest

## Out of scope for now

- perfect reconstruction of every hidden action
- full open-world transcript repair
- large ML-heavy systems without clear payoff
- extracting every rare mechanic before core analytics are stable
- broad architecture rewrites that do not unlock one of the primary analysis goals

## Current capability map

This section should describe what the pipeline already supports at a high level.

### Strong or usable now
These areas are already useful for analysis:

- speaker mapping and speaking-time measurement
- lie and claim extraction
- broad phase detection
- day-event detection
- player status tracking
- death-event extraction
- transcript name normalization
- winner and alignment outcome tracking

### Meaningfully incomplete
These areas still block major analysis questions:

- nomination extraction
- vote extraction
- target-intent extraction
- rumor propagation graph
- claimed-role vs actual-role survival comparison
- finer death-cause refinement in ambiguous cases

## Highest-value remaining gaps

### Gap 1: nominations and votes
Why it matters:
- unlocks execution strategy analysis
- unlocks town pressure analysis
- unlocks claimed-role-to-die analysis
- identifies who pushes eliminations

Questions blocked:
- who nominates whom
- who the town tries to kill
- which claimed roles attract execution pressure
- whether evil drives specific execution patterns

Priority:
- highest

### Gap 2: target intent
Why it matters:
- needed for demon and minion target preference analysis
- needed to separate intended kill from actual death
- needed for protection and redirect analysis

Questions blocked:
- popular demon targets
- popular minion targets
- role-based target preferences
- target vs victim divergence

Priority:
- very high

### Gap 3: rumor and sentiment propagation
Why it matters:
- directly tied to a core analysis goal
- needed for social graph analysis

Questions blocked:
- who starts rumors
- who spreads rumors
- how suspicion moves through town
- whether evil-originated rumors spread differently

Priority:
- high

### Gap 4: claimed-role vs actual-role comparison layer
Why it matters:
- critical for survival and execution bluff analysis
- needed for go-to-role-to-die and survive questions

Questions blocked:
- which claimed roles act as scapegoats
- which claimed roles protect survival
- how bluffing changes survival odds

Priority:
- high

### Gap 5: finer kill-cause refinement
Why it matters:
- current death timing is useful
- exact cause is still coarse in some cases

Questions blocked:
- some special-kill analyses
- some role-specific targeting conclusions

Priority:
- medium

## Roadmap

### Phase 1: stabilize core analytics base
Goal:
- ensure the current pipeline is reliable enough for player, role, and alignment analysis

Deliverables:
- stable speaker mapping
- stable lie and claim extraction
- stable phase and day-event extraction
- stable player status and death-event extraction
- transcript name normalization integrated
- docs and validation clean

Success criteria:
- dataset builds cleanly
- validation stays green
- no major contract drift
- core analytics tables are trustworthy

Status:
- mostly complete

### Phase 2: execution and pressure tracking
Goal:
- capture how the town chooses kills

Deliverables:
- nomination extraction
- vote sequence linkage to nominees
- execution-target dataset
- claimed-role-at-execution dataset

Unlocks:
- go-to claimed role to die
- who gets pushed most
- who nominates whom
- execution pressure by alignment and role

Priority:
- highest next step

### Phase 3: night target intent
Goal:
- capture intended night actions separately from actual outcomes

Deliverables:
- target-intent extraction from transcript and context
- target vs actual victim reconciliation
- confidence tagging for intended actions
- conservative blocked and uncertain handling

Unlocks:
- demon target preferences
- minion target preferences
- role-based target patterns
- protection and redirect effects

Priority:
- very high

### Phase 4: rumor and sentiment graph
Goal:
- model information flow through the town

Deliverables:
- accusation edges
- defense edges
- repeated-claim propagation
- rumor-origin tracking
- sentiment timeline per player

Unlocks:
- who starts rumors
- who spreads rumors
- who defends whom
- how consensus forms

Priority:
- high

### Phase 5: claimed-role survival analysis
Goal:
- compare public claims to outcomes

Deliverables:
- claimed-role timeline
- claim persistence and contradiction tracking
- survival and execution rates by claimed role
- actual-role comparison layer

Unlocks:
- go-to role to survive
- bluff effectiveness
- good vs evil public-role strategies

Priority:
- high

### Phase 6: refinement and scale-up
Goal:
- improve accuracy and expand coverage across all processable videos

Deliverables:
- broader rollout of newer nodes
- edge-case review loops
- alias/support growth from real misses
- targeted validation on ambiguous cases

Unlocks:
- more trustworthy aggregate stats
- less bias from partial coverage

Priority:
- ongoing

## Recommended extraction priorities

If only three major pieces of work happen next, they should be:

1. nomination and vote extraction
2. night target-intent extraction
3. rumor propagation graph

These three unlock the most value relative to the analysis goals.

## Success metrics

The project is succeeding if it can produce trustworthy answers to questions like:

### Behavior
- top 10 talkers by total speaking time
- bottom 10 talkers by total speaking time
- top 10 liars by lies per game
- lie rate by alignment

### Role and alignment
- speaking volume by role
- speaking volume by alignment
- lie rate by role
- good vs evil win rate

### Survival and pressure
- most commonly executed claimed roles
- most commonly surviving claimed roles
- actual roles most often killed at night
- players most often targeted by executions

### Social flow
- top rumor starters
- top rumor spreaders
- most common accusation pairs
- most defended players

If these are easy to compute and reasonably trustworthy, the project is aligned.

## Decision rules for future work

Use these rules before starting major extraction work.

### Keep
Do the work if it clearly improves one of:
- talk analysis
- lie analysis
- survival or execution analysis
- target analysis
- rumor or sentiment analysis

### Delay
Delay the work if it is:
- technically interesting but weakly tied to the analysis goals
- mostly about rare edge cases that do not unlock core questions
- complexity without a clear downstream payoff

### Preferred implementation style
Prefer:
- additive artifacts
- conservative inference
- per-video closed-world reasoning
- inspectable intermediate outputs
- validation-backed rollout
- small, explicit contracts

Avoid:
- silent heuristics with unclear confidence
- broad global matching when video-local reasoning is possible
- heavy manual maintenance unless it clearly pays off
- new complexity without a mapped analysis benefit

## Source-of-truth expectations

This document defines strategic intent.

Other docs define operational details:
- `AGENTS.md` for repo and branch rules
- `docs/agent_rules.md` for implementation constraints
- `docs/pipeline_dag.md` for pipeline architecture
- `docs/dataset_snapshot.md` for current coverage and known gaps
- `docs/dev_commands.md` for approved execution patterns

If implementation changes project direction, update this charter and the relevant operational docs together.

## Change protocol

When adding a new extraction feature, update these explicitly:

1. what analysis question it supports
2. what artifact it writes
3. what DB tables it affects
4. what validation expectations change
5. what limitations remain

If a new feature cannot answer item 1 clearly, it probably should not be prioritized.

## Short version for AI agents

If you are an AI agent working in this repo, optimize for this order:

1. stable data that answers core analysis questions
2. conservative and inspectable extraction
3. additive artifacts with clean validation
4. narrow fixes for real bottlenecks
5. avoid broad complexity unless it clearly unlocks analysis value