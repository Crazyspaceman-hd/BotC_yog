#!/usr/bin/env bash
# deploy_pages.sh — Regenerate landing.html and push to gh-pages branch.
#
# Usage:
#   bash deploy_pages.sh              # regenerate + deploy
#   bash deploy_pages.sh --dry-run    # regenerate only, skip git operations
#
# The script always returns you to the branch you started on.

set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  [[ "$arg" == "--dry-run" ]] && DRY_RUN=1
done

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
TMPFILE=$(mktemp /tmp/botc_index_XXXXXX.html)

cleanup() {
  rm -f "$TMPFILE"
  # Make sure we always get back to the original branch
  if [[ "$(git rev-parse --abbrev-ref HEAD)" != "$CURRENT_BRANCH" ]]; then
    echo "[deploy] Returning to $CURRENT_BRANCH ..."
    git checkout -f "$CURRENT_BRANCH"
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

# ── 2. Switch to gh-pages ─────────────────────────────────────────────────────
echo "[deploy] Switching to gh-pages ..."
if git show-ref --verify --quiet refs/heads/gh-pages; then
  # Local branch exists — check it out (force-overwrite untracked)
  git checkout -f gh-pages
else
  # No local branch yet — fetch from remote or create orphan
  if git ls-remote --exit-code --heads origin gh-pages > /dev/null 2>&1; then
    git fetch origin gh-pages
    git checkout -f -b gh-pages origin/gh-pages
  else
    git checkout --orphan gh-pages
    git rm -rf --cached . > /dev/null 2>&1 || true
  fi
fi

# ── 3. Update index.html ──────────────────────────────────────────────────────
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

# ── 4. Push ───────────────────────────────────────────────────────────────────
git push origin gh-pages
echo "[deploy] Pushed to origin/gh-pages."
echo "[deploy] Live at: https://crazyspaceman-hd.github.io/BotC_yog/"
