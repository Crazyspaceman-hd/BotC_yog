#!/usr/bin/env bash
# scripts/run_all.sh — Authoritative run sequence for BotC_yog.
#
# Executes the full pipeline in dependency order:
#   1. Per-video extraction  (steps A-G via run_pipeline.py)
#   2. Cross-video curation  (auto_assign_speakers, then manual fix_rosters gate)
#   3. Database rebuild      (build_db.py)
#   4. UI / public refresh   (generate_landing.py + optional deploy)
#
# Usage:
#   bash scripts/run_all.sh                  # full run, all pending videos
#   bash scripts/run_all.sh --steps "transcribe merge patch scrape analyze"
#   bash scripts/run_all.sh --force          # re-run even if outputs exist
#   bash scripts/run_all.sh --skip-curation  # skip auto_assign step (data already curated)
#   bash scripts/run_all.sh --skip-deploy    # skip gh-pages push
#   bash scripts/run_all.sh --video <id>     # single video + curation + rebuild + refresh
#
# Prerequisites:
#   - .venv activated (main pipeline, Streamlit UI, build_db)
#   - .venv_botc activated separately BEFORE running the diarize step
#     (or exclude 'diarize' from --steps if already done in .venv_botc)
#
# Validation:
#   Run 'python validate.py' after this script to confirm the repo end-state.

set -euo pipefail

# ── Parse arguments ────────────────────────────────────────────────────────────
STEPS="download transcribe diarize merge patch scrape analyze"
FORCE_FLAG=""
SKIP_CURATION=0
SKIP_DEPLOY=0
VIDEO_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --steps)       STEPS="$2";        shift 2 ;;
    --force)       FORCE_FLAG="--force"; shift ;;
    --skip-curation) SKIP_CURATION=1; shift ;;
    --skip-deploy) SKIP_DEPLOY=1;     shift ;;
    --video)       VIDEO_ID="$2";     shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# ── Helpers ────────────────────────────────────────────────────────────────────
log() { echo "[run_all] $*"; }
die() { echo "[run_all] ERROR: $*" >&2; exit 1; }

[[ -f playlist.json ]] || die "playlist.json not found — run 'python fetch_playlist.py' first"

# ── Phase 1: Per-video extraction ──────────────────────────────────────────────
log "=== Phase 1: Per-video extraction ==="
log "Steps: $STEPS"

if [[ -n "$VIDEO_ID" ]]; then
  log "Processing single video: $VIDEO_ID"
  # shellcheck disable=SC2086
  python run_pipeline.py "$VIDEO_ID" --steps $STEPS $FORCE_FLAG
else
  log "Processing all pending videos..."
  # shellcheck disable=SC2086
  python run_pipeline.py --all --steps $STEPS $FORCE_FLAG
fi

# ── Phase 2: Cross-video curation ─────────────────────────────────────────────
if [[ $SKIP_CURATION -eq 0 ]]; then
  log ""
  log "=== Phase 2: Cross-video curation ==="

  log "Running auto_assign_speakers (dry run first)..."
  python auto_assign_speakers.py

  log ""
  log ">>> MANUAL GATE <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<"
  log "Review the speaker assignment proposals above."
  log "  - To apply CERTAIN + HIGH assignments and continue:"
  log "      python auto_assign_speakers.py --apply"
  log "  - To also apply MEDIUM (process-of-elimination):"
  log "      python auto_assign_speakers.py --apply --include-medium"
  log "  - For remaining unlinked speakers, run:"
  log "      streamlit run fix_rosters.py"
  log "  Then re-run this script with --skip-curation once all issues are fixed."
  log ">>><<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<"
  log ""
  log "Pausing — press Enter to continue with current state, or Ctrl+C to stop."
  read -r

  log "Re-running analyze_roles.py --all after curation..."
  python analyze_roles.py --all
else
  log "(Skipping curation step — using existing roster_overrides.json files)"
fi

# ── Phase 3: Database rebuild ─────────────────────────────────────────────────
log ""
log "=== Phase 3: Database rebuild ==="
python build_db.py
log "botc.db rebuilt."

# ── Phase 4: UI / public refresh ──────────────────────────────────────────────
log ""
log "=== Phase 4: UI / public refresh ==="
python generate_landing.py
log "landing.html regenerated."

if [[ $SKIP_DEPLOY -eq 0 ]]; then
  log "Deploying to gh-pages..."
  bash deploy_pages.sh
else
  log "(Skipping deploy — run 'bash deploy_pages.sh' manually when ready)"
fi

# ── Validation ────────────────────────────────────────────────────────────────
log ""
log "=== Validation ==="
python validate.py

log ""
log "=== Done ==="
