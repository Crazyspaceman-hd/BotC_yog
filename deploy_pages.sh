#!/usr/bin/env bash
# deploy_pages.sh — Regenerate landing.html and push to gh-pages branch.
#
# Usage:
#   bash deploy_pages.sh              # regenerate + deploy
#   bash deploy_pages.sh --dry-run    # regenerate only, skip git operations
#
# The script always returns you to the branch you started on, and uses
# git stash/pop to preserve any uncommitted local changes (e.g. botc.db).

set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  [[ "$arg" == "--dry-run" ]] && DRY_RUN=1
done

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
TMPFILE=$(mktemp /tmp/botc_index_XXXXXX.html)
STASHED=0

cleanup() {
  rm -f "$TMPFILE"
  # Make sure we always get back to the original branch
  if [[ "$(git rev-parse --abbrev-ref HEAD)" != "$CURRENT_BRANCH" ]]; then
    echo "[deploy] Returning to $CURRENT_BRANCH ..."
    # No -f here: we rely on the stash below to avoid conflicts
    git checkout "$CURRENT_BRANCH" 2>/dev/null || git checkout -f "$CURRENT_BRANCH"
  fi
  # Restore any local changes we stashed before the branch switch
  if [[ $STASHED -eq 1 ]]; then
    echo "[deploy] Restoring stashed local changes ..."
    git stash pop
  fi
}
trap cleanup EXIT

# ── 1. Regenerate ─────────────────────────────────────────────────────────────
echo "[deploy] Regenerating landing.html ..."
python generate_landing.py
cp landing.html "$TMPFILE"
echo "[deploy] Saved to $TMPFILE"

if [[ $DRY_RUN -eq 1 ]]; then
  echo "[deploy] --dry-run: skipping git operations."
  exit 0
fi

# ── 2. Stash uncommitted tracked-file changes so branch switch stays clean ────
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "[deploy] Stashing local changes before branch switch ..."
  git stash push -m "deploy-pages: auto-stash" --include-untracked
  STASHED=1
fi

# ── 3. Switch to gh-pages ─────────────────────────────────────────────────────
echo "[deploy] Switching to gh-pages ..."
if git show-ref --verify --quiet refs/heads/gh-pages; then
  git checkout gh-pages
else
  # No local branch yet — fetch from remote or create orphan
  if git ls-remote --exit-code --heads origin gh-pages > /dev/null 2>&1; then
    git fetch origin gh-pages
    git checkout -b gh-pages origin/gh-pages
  else
    git checkout --orphan gh-pages
    git rm -rf --cached . > /dev/null 2>&1 || true
  fi
fi

# ── 4. Update index.html ──────────────────────────────────────────────────────
cp "$TMPFILE" index.html
git add index.html

# Only commit if something actually changed
if git diff --cached --quiet; then
  echo "[deploy] index.html unchanged — nothing to commit."
else
  TIMESTAMP=$(date -u "+%Y-%m-%d %H:%M UTC")
  git commit -m "Deploy: update landing page ($TIMESTAMP)"
  echo "[deploy] Committed."
fi

# ── 5. Push ───────────────────────────────────────────────────────────────────
git push origin gh-pages
echo "[deploy] Pushed to origin/gh-pages."
echo "[deploy] Live at: https://crazyspaceman-hd.github.io/BotC_yog/"
