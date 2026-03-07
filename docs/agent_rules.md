# Agent Rules

Rules for AI agents (Claude, Copilot, etc.) working in this repo.
Follow these before writing or proposing any change.

---

## Always read first
1. `docs/pipeline_dag.md` — authoritative pipeline architecture
2. `docs/dev_commands.md` — correct command forms
3. `AGENTS.md` — repo overview and never-do list

## Role and team data
- **Never hardcode** role names, team assignments, or evil/good sets
- Use `botc_ui._team(role_name)` for team lookup
- Use `pipeline_utils.normalize_role()` for comparisons (returns lowercase)
- Use `pipeline_utils.display_role()` for storage/display (returns Title Case)
- Add new roles in `botc_ui._ROLES` only — `build_db.py` seeds the DB automatically

## Code conventions
- Speaker 0 is always the Storyteller (`STORYTELLER_ID = "speaker_0"`)
- Role names in DB/JSON: Title Case with spaces (`"Snake Charmer"`)
- Role comparisons in code: always via `normalize_role()` (lowercase)
- Enrichment node aliases: N1 = speaker_consistency, N2 = phase_detection, N3 = claim_extraction

## Safe to do without asking
- Read any file
- Run `python validate.py`
- Run `python detect_phases.py <id>`, `python speaker_consistency.py <id>`, `python extract_claims.py <id>`
- Run `python analyze_roles.py --all` after a curation change
- Run `python build_db.py` after any output change
- Add documentation files

## Ask before doing
- Any change to `botc_ui.py` or `pipeline_utils.py`
- Schema changes to `botc.db`
- Changes to `playlist.json` flags (`winner`, `blind`, `members_only`, `skip`)
- Running `deploy_pages.sh` or pushing to `gh-pages`
- Renaming or deleting any pipeline script

## Never do
- Push directly to `main`
- Delete files from `outputs/`
- Skip `validate.py` after a pipeline rebuild
- Hardcode role→team mappings
