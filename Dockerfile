# syntax=docker/dockerfile:1.7
#
# agent-skills-collection — single image covering all three pipeline stages.
#
#   Stage 1: metadata_collection (needs Playwright + Chromium)
#   Stage 2: skills_repos/fetch_metadata.py
#   Stage 3: skills_repos/download_repos.py
#
# Build:
#   docker build -t agentskills-collection .
#
# Run a stage (mount your output dir so artifacts survive container restarts):
#   docker run --rm -v $(pwd)/output:/app/output \
#       agentskills-collection metadata_collection/crawl_lists.py --workers 4
#
# Or with docker-compose (see docker-compose.yml):
#   docker compose run --rm labels   crawl_lists.py --workers 4
#   docker compose run --rm repos    fetch_metadata.py --repo_map …
#   docker compose run --rm download download_repos.py --repo_map …

# Pin to the official Playwright image so Chromium + system fonts + ICU + the
# right glibc are already installed and tested together. v1.45 matches the
# requirements.txt floor.
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Default output / log roots inside the image — override at run time
    # by mounting a host directory at /app/output.
    AGENTSKILLS_OUTPUT_ROOT=/app/output

WORKDIR /app

# Python dependencies first (cached layer).
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Pipeline source.
COPY metadata_collection /app/metadata_collection
COPY skills_repos             /app/skills_repos
COPY README.md                /app/README.md

# Entry point dispatches to whichever stage script you ask for.
# Usage:
#   docker run --rm agentskills-collection skills_repos/fetch_metadata.py --help
#   docker run --rm agentskills-collection metadata_collection/crawl_lists.py --help
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--help"]
