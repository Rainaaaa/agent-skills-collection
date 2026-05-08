#!/usr/bin/env bash
# Dispatch to a pipeline script.
#
#   docker run --rm agentskills-collection \
#       skills_labels_collection/crawl_lists.py --workers 4
#
# The first arg is interpreted as a path relative to /app. If it ends in
# `.py`, we hand it to python; otherwise we exec it (so you can run plain
# shell commands like `docker run ... bash -lc '...'`).
set -euo pipefail

if [ "$#" -eq 0 ]; then
  set -- --help
fi

case "$1" in
  --help|-h)
    cat <<'EOF'
AgentSkills-collection container

Stage 1 — SkillsMP labels:
    skills_labels_collection/crawl_lists.py     [--workers N]
    skills_labels_collection/crawl_details.py   [--workers N]
    skills_labels_collection/merge_metadata.py

Stage 2 — GitHub metadata:
    skills_repos/fetch_metadata.py              --repo_map <path>

Stage 3 — GitHub archive download:
    skills_repos/download_repos.py              --repo_map <path>

Mount your work area at /app/output to persist results between runs:

    docker run --rm \
        -v $(pwd)/output:/app/output \
        agentskills-collection \
        skills_repos/fetch_metadata.py --help

For non-Python entry points (bash, etc.), pass the binary as the first arg:

    docker run --rm -it agentskills-collection bash
EOF
    exit 0
    ;;
esac

# If the first arg is a Python script under /app, run it with python.
script="$1"; shift
if [[ "$script" == *.py ]]; then
  if [ -f "/app/$script" ]; then
    cd "/app/$(dirname "$script")"
    exec python -u "$(basename "$script")" "$@"
  fi
  if [ -f "$script" ]; then
    exec python -u "$script" "$@"
  fi
  echo "[entrypoint] python script not found: $script" >&2
  exit 64
fi

# Otherwise treat the args as a verbatim command (bash, sh, ls, etc.).
exec "$script" "$@"
