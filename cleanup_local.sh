#!/usr/bin/env bash
# Optional local cleanup — wipes crawled outputs, logs, caches, and the
# deprecated skills_collection/ tree. Idempotent; safe to re-run.
#
# Run from anywhere — the script resolves its own location:
#   bash cleanup_local.sh
#
# Or with a dry run first:
#   DRYRUN=1 bash cleanup_local.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

run() {
  if [ "${DRYRUN:-0}" = "1" ]; then
    echo "[dry-run] $*"
  else
    echo "[run] $*"
    "$@"
  fi
}

cd "$ROOT"

# 1) Deprecated API crawler (kept under DEPRECATED.md but no longer maintained)
[ -d skills_collection ] && run rm -rf skills_collection

# 2) Crawled output directories — large, reproducible from the scripts
for d in metadata_collection/output skills_repos/output \
         metadata_collection/log    skills_repos/log    \
         metadata_collection/logs   skills_repos/logs; do
  [ -e "$d" ] && run rm -rf "$d"
done

# 3) Python caches + scratch
find . -name __pycache__   -type d -prune -exec rm -rf {} + 2>/dev/null || true
find . -name .ipynb_checkpoints -type d -prune -exec rm -rf {} + 2>/dev/null || true
find . -name '*.pyc' -delete 2>/dev/null || true
find . -name '*.bak' -delete 2>/dev/null || true
find . -name '*.tmp' -delete 2>/dev/null || true
find . -name '_backup_v1' -prune -exec rm -rf {} + 2>/dev/null || true
find . -name 'Untitled-*.ipynb' -delete 2>/dev/null || true

# 4) Smoke-test scratch files
rm -f metadata_collection/pw_minimal.py     2>/dev/null || true
rm -f metadata_collection/_pw_node_probe.py 2>/dev/null || true
rm -f metadata_collection/test.py           2>/dev/null || true

# 5) Real secrets (real tokens.json is gitignored anyway, but wipe to be safe)
[ -f metadata_collection/tokens.json ] && run rm -f metadata_collection/tokens.json
[ -f skills_repos/tokens.json ]             && run rm -f skills_repos/tokens.json

echo "[done] cleanup complete."
