# AgentSkills-collection

End-to-end crawler for the AgentSkills-OSS dataset. Three independent stages
turn the live [SkillsMP](https://skillsmp.com) site into a deduplicated,
labeled, license-aware local catalog of agent skills with their backing
GitHub repositories.

```
                 ┌──────────────────────────────────────┐
Stage 1          │  metadata_collection/           │
SkillsMP         │  Playwright crawl of L1 list pages   │
crawl            │  + L2 detail pages → labels + meta   │
                 └────────────────┬─────────────────────┘
                                  │ repo_map.json
                                  │ skillsmp_metadata.jsonl
                                  ▼
                 ┌──────────────────────────────────────┐
Stage 2          │  skills_repos/fetch_metadata.py      │
GitHub API       │  → license, default_branch, stars,   │
metadata         │     archived/fork flags, topics, …   │
                 └────────────────┬─────────────────────┘
                                  │ github_metadata.jsonl
                                  ▼
                 ┌──────────────────────────────────────┐
Stage 3          │  skills_repos/download_repos.py      │
GitHub archive   │  → tar.gz per repo, per-skill        │
download         │     package views, license-filtered  │
                 └──────────────────────────────────────┘
```

Each stage is a standalone CLI with its own `runtime_config.json`,
checkpoint, and append-only outputs. They are **chained by file paths**, not
by Python imports, so any stage can be re-run, replaced, or fed an
alternate input source independently.

## Layout

```
AgentSkills-collection/
├── README.md                          # this file
│
├── skills_collection/                 # ⚠ DEPRECATED — API-only legacy crawler
│   └── DEPRECATED.md
│
├── metadata_collection/          # Stage 1 — SkillsMP crawl (labels + meta)
│   ├── crawl_lists.py                 # L1: list pages → cards + label sets
│   ├── crawl_details.py               # L2: detail pages → SKILL.md + JSON-LD
│   ├── merge_metadata.py              # join L1 + L2 + SOC hierarchy
│   ├── _shared.py                     # IO + checkpoint helpers
│   ├── crawler_jobs.json              # selectors, sources, scroll behavior
│   ├── runtime_config.json            # paths, browser, retries
│   ├── input/                         # SOC + category taxonomies
│   └── output/                        # crawled artifacts
│
└── skills_repos/                      # Stages 2 + 3 — GitHub-side pipeline
    ├── fetch_metadata.py              # Stage 2: GitHub REST API → metadata
    ├── download_repos.py              # Stage 3: license-aware archive download
    ├── _shared.py                     # GitHub URL parsing, IO helpers
    ├── runtime_config.json            # paths, filters, request settings
    ├── tokens.json.example            # GitHub PAT(s) for higher rate limits
    ├── README.md                      # full reference for stages 2+3
    └── output/                        # github_metadata.jsonl, raw_repos/, …
```

## Why three stages

| Stage | Boundary it respects                                         |
| ----- | ------------------------------------------------------------ |
| 1     | The SkillsMP web UI is the only place that has labels.       |
| 2     | License + canonical default branch are only on GitHub.       |
| 3     | Bulk archive download is slow + I/O heavy — keep it isolated.|

Splitting them makes each stage cheap to re-run and easy to swap. For
instance, you can re-run Stage 2 for a license refresh without re-crawling
SkillsMP, or skip Stage 3 entirely and feed the metadata into a scanner.

## End-to-end

```bash
# Stage 1 — full SkillsMP sweep (or incremental — see metadata_collection/README.md)
cd metadata_collection
sbatch run_periodic.sh
python merge_metadata.py    # produces repo_map.json + skillsmp_metadata.jsonl

# Stage 2 — fetch GitHub metadata + license
cd ../skills_repos
python fetch_metadata.py \
    --repo_map ../metadata_collection/output/repo_map.json

# Stage 3 — download archives that pass license + status filters
python download_repos.py \
    --metadata output/github_metadata.jsonl \
    --repo_map ../metadata_collection/output/repo_map.json
```

## Stage details

- **Stage 1** — see [`metadata_collection/README.md`](metadata_collection/README.md).
- **Stages 2 + 3** — see [`skills_repos/README.md`](skills_repos/README.md).

## Running

### Option A — Docker (recommended for portability)

> No prebuilt image is published — only the build files (`Dockerfile`,
> `docker-compose.yml`, `docker-entrypoint.sh`, `.dockerignore`) are in the
> repo. Anyone who clones it can build locally with one command. This keeps
> the GitHub repo small and lets users pin their own base image / Python
> version if needed.

A single container image covers all three stages (Python 3.12 + Playwright +
Chromium pre-installed):

```bash
git clone git@github.com:Rainaaaa/agent-skills-collection.git
cd agent-skills-collection
docker build -t agentskills-collection .

# Stage 1 — SkillsMP labels
docker compose run --rm labels-l1 --workers 4
docker compose run --rm labels-l2 --workers 4
docker compose run --rm merge

# Stage 2 — GitHub metadata (mount tokens.json read-only)
docker compose run --rm fetch-meta --with_license_text

# Stage 3 — license-aware archive download
docker compose run --rm download \
    --require_license \
    --license_whitelist mit,apache-2.0,bsd-3-clause
```

The `output/` directory on the host is bind-mounted into the container at
`/app/output`, so all checkpoints + artifacts persist between runs.
Override the host side:

```bash
AGENTSKILLS_OUTPUT_HOST=/scratch/agentskills/output \
    docker compose run --rm fetch-meta ...
```

### Option B — Local Python env

```bash
conda activate AgentSkillsOSS                 # or any venv
pip install -r requirements.txt
python -m playwright install chromium         # only needed for Stage 1
```

## Pipeline guarantees

- **Resumable** — every stage has a checkpoint and skips work it has already
  done; safe to re-run after SLURM time caps or rate-limit exits.
- **Append-only outputs** — `*.jsonl` files only grow; rolled-up indexes are
  written via atomic temp-file rename.
- **Loose coupling** — stages communicate only through documented file
  paths in their `runtime_config.json`, so swapping any stage's
  implementation never requires editing another stage.
