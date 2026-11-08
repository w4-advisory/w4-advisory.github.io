#!/usr/bin/env bash
# Run only in the disposable Actions checkout, after collecting news.
set -euo pipefail

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

commit_news() {
  git add -- news.json news-seen.json assets/news/
  if git diff --cached --quiet; then
    echo "No article or preview updates."
    exit 0
  fi
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
  if ! git rebase origin/main || ! python scripts/update_news.py --validate-only; then
    # Concurrent news/image edits need fresh selection against the new archive.
    # This checkout is disposable; preserve the remote history and collect again.
    git rebase --abort 2>/dev/null || true
    git reset --hard origin/main
    python scripts/update_news.py
    python scripts/update_news.py --validate-only
    commit_news
  fi
done
