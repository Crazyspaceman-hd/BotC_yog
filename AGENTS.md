# AGENTS.md — Repo guidance for AI agents

## What this repo is
Automated pipeline for downloading, transcribing, diarizing, and analysing
Yogscast Blood on the Clocktower (BotC) YouTube videos.  See `docs/pipeline_dag.md`
for the full system architecture.

## Key files
| File | Role |
|------|------|
| `docs/pipeline_dag.md` | **Authoritative** pipeline reference — read before making changes |
| `docs/dev_commands.md` | Quick command reference |
| `docs/agent_rules.md` | Rules for agents working in this repo |
| `docs/dataset_snapshot.md` | Current dataset state and known gaps |
| `botc_ui.py` | Single source of truth for role data — never hardcode roles or teams |
| `pipeline_utils.py` | Shared helpers — use `normalize_role()`, `display_role()`, `resolve_player_name()` |
| `build_db.py` | Rebuilds `botc.db` from outputs — run after any data change |
| `validate.py` | End-state validator — run after any pipeline phase |

## Never do without being asked
- Modify `botc.db` schema
- Delete or overwrite files in `outputs/`
- Push to `main` or `gh-pages`
- Run `deploy_pages.sh`
- Change `playlist.json` winner/blind/members_only flags
- Rename pipeline scripts

## Branch conventions
- `develop` — integration branch; merge features here before `main`
- `feat/*` — feature branches; open PR to `develop` or `main`
- `gh-pages` — static landing page only; managed by `deploy_pages.sh`
