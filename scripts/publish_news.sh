#!/usr/bin/env bash
# Run only in the disposable Actions checkout, after collecting news.
set -euo pipefail

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

commit_news() {
  if git diff --quiet -- news.json; then
    echo "No new articles."
    exit 0
  fi
  git add news.json
  git commit -m "chore: update AI security headlines"
}

commit_news
for attempt in 1 2 3; do
  if git push origin HEAD:main; then
    exit 0
  fi
  if [ "$attempt" -eq 3 ]; then
    echo "Could not publish news after three attempts. Check the push errors above."
    exit 1
  fi
  echo "Refreshing main before retry $((attempt + 1))."
  git fetch origin main
  if ! git rebase origin/main; then
    # A concurrent edit to news.json needs fresh selection against the new archive.
    # This checkout is disposable; preserve the remote history and collect again.
    git rebase --abort
    git reset --hard origin/main
    python scripts/update_news.py
    commit_news
  fi
done
