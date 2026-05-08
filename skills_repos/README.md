# skills_repos — GitHub-side pipeline (Stages 2 + 3)

Two stand-alone CLIs that turn a list of GitHub repo references into a
license-aware, locally-cached corpus of agent-skill source archives.

```
Stage 2  fetch_metadata.py    →  github_metadata.jsonl + github_metadata_index.json
Stage 3  download_repos.py    →  raw_repos/ + packages/ + repo_status.csv + skill_status.csv
```

Both stages read `runtime_config.json`, write append-only outputs, and are
fully resumable via per-stage checkpoint files. They are loosely coupled by
file paths only; either can be re-run, replaced, or fed alternate inputs.

## Stage 2 — `fetch_metadata.py`

Calls `GET /repos/{owner}/{repo}` for every unique GitHub repo discovered
upstream and (optionally) `GET /repos/{owner}/{repo}/license` to pull the
SPDX-detected license body.

Captured fields (per repo):

- `default_branch`, `archived`, `disabled`, `fork`, `private`, `visibility`
- `stargazers_count`, `forks_count`, `subscribers_count`, `open_issues_count`
- `language`, `topics`, `size_kb`
- `created_at`, `updated_at`, `pushed_at`
- `license_key`, `license_name`, `license_spdx_id`, `license_url`
- raw `payload` (full GitHub JSON) for any future fields you want later

Inputs (any combination — unique `(owner, repo)` pairs are unioned):

| Flag                  | Source                                                     |
| --------------------- | ---------------------------------------------------------- |
| `--repo_map`          | `repo_map.json` from `metadata_collection/`           |
| `--repos_file`        | plain text, one GitHub URL per line                        |
| `--metadata_jsonl`    | jsonl with a `githubUrl` (or `repo_url`) on each row       |

### Token rotation

`fetch_metadata.py` rotates among the GitHub PATs declared in `tokens.json`
(see `tokens.json.example` for the schema) round-robin, and tracks each
token's `X-RateLimit-Remaining` / `X-RateLimit-Reset` headers. When all
tokens are at zero remaining, the run exits cleanly with a "soonest reset
in Ns" message — re-run any time after that to continue.

Run unauthenticated (no `tokens.json`): GitHub allows 60 req/hour.
Use this only for tiny smoke tests.

### Examples

```bash
# Smoke test: 100 repos, no license body
python fetch_metadata.py \
  --repo_map ../metadata_collection/output/repo_map.json \
  --max_repos 100

# Production: include SPDX license body for every repo
python fetch_metadata.py \
  --repo_map ../metadata_collection/output/repo_map.json \
  --with_license_text

# Backfill from a custom URL list (e.g. user-curated additions)
python fetch_metadata.py --repos_file extra_repos.txt
```

Or via SLURM:

```bash
sbatch run_fetch_metadata.sh
sbatch --export=WITH_LICENSE_TEXT=1 run_fetch_metadata.sh
sbatch --export=MAX_REPOS=200 run_fetch_metadata.sh    # smoke
```

## Stage 3 — `download_repos.py`

Reads Stage 2's `github_metadata_index.json`, applies filters, downloads
the GitHub archive (`.tar.gz`) for each surviving repo using the
**authoritative** `default_branch` from the metadata, then builds per-skill
package views matching the existing `skills_download/` shape (manifest +
`files` symlink + `SKILL.md` symlink).

### Filters

All optional, intersection semantics:

| Flag                       | Behavior                                       |
| -------------------------- | ---------------------------------------------- |
| `--require_license`        | drop repos with no SPDX license detected       |
| `--license_whitelist a,b`  | keep only repos with these SPDX ids            |
| `--license_blacklist x,y`  | drop repos with these SPDX ids                 |
| `--skip_archived`          | drop archived repos                            |
| `--skip_disabled`          | drop disabled repos                            |
| `--skip_forks`             | drop forks                                     |
| `--min_stars N`            | drop repos under N stars                       |

Filtered repos are recorded in `repo_download_log.jsonl` with a
`filtered:<reason>` status, and propagate into `skill_status.csv` so
downstream tools see the filter reason rather than a missing skill.

### Examples

```bash
# Permissive licenses only
python download_repos.py \
  --repo_map     ../metadata_collection/output/repo_map.json \
  --skills_dedup ../metadata_collection/output/dedup_index.json \
  --require_license \
  --license_whitelist mit,apache-2.0,bsd-3-clause,bsd-2-clause,isc,mpl-2.0 \
  --skip_archived

# Smoke: 20 repos, no filters
python download_repos.py \
  --repo_map ../metadata_collection/output/repo_map.json \
  --max_repos 20
```

SLURM:

```bash
sbatch run_download_repos.sh

sbatch --export=REQUIRE_LICENSE=1,LICENSE_WHITELIST=mit,apache-2.0,bsd-3-clause \
       run_download_repos.sh

sbatch --export=MAX_REPOS=50 run_download_repos.sh    # smoke
```

## Layout

```
skills_repos/
├── README.md
├── runtime_config.json
├── tokens.json.example          # rename to tokens.json and fill in PATs
├── tokens.json                  # (gitignored — secrets)
│
├── _shared.py                   # IO helpers + GitHub URL parsing
├── fetch_metadata.py            # Stage 2 entry point
├── download_repos.py            # Stage 3 entry point
│
├── run_fetch_metadata.sh        # SLURM wrapper for Stage 2
├── run_download_repos.sh        # SLURM wrapper for Stage 3
│
├── log/                         # SLURM stdout/stderr
└── output/
    ├── github_metadata.jsonl                # Stage 2: append-only fetch records
    ├── github_metadata_index.json           # Stage 2: repo_key → latest summary
    ├── fetch_metadata_request_log.jsonl     # Stage 2: per-request audit
    ├── repo_download_log.jsonl              # Stage 3: per-repo result
    ├── package_index.jsonl                  # Stage 3: per-skill view
    ├── repo_status.csv                      # Stage 3: tabular repo status
    ├── skill_status.csv                     # Stage 3: tabular skill status
    └── checkpoints/
        ├── fetch_metadata_checkpoint.json
        └── download_repos_checkpoint.json
```

`raw_repos/` and `packages/` default to `output/` but in production are
redirected to `/N/project/AdversarialModeling/agent_skills/{raw_repos,packages}`
via `runtime_config.json` because they grow large.

## Resume + re-run semantics

- **Stage 2** — completed `repo_key`s live in
  `checkpoints/fetch_metadata_checkpoint.json`. Re-running skips them
  unless `--force`. Failures with non-terminal HTTP statuses (transient
  500s, rate-limit 403s) are *not* checkpointed, so they retry on the
  next run.
- **Stage 3** — successful + cached repos live in
  `checkpoints/download_repos_checkpoint.json` and in per-repo
  `.ready.json` markers under `raw_repos/`. Re-running skips both.
  `--force` re-downloads; `--reset_checkpoint` only clears the
  checkpoint (markers remain).

## How to add a new repo source

The pipeline only requires a list of `(owner, repo)` pairs. To bring in a
new corpus (e.g. internal index, hand-curated list, alternate marketplace),
either:

1. Produce a `repo_map.json` in the same shape as
   `metadata_collection/output/repo_map.json` and pass it via
   `--repo_map`, or
2. Drop a plain text file of GitHub URLs and pass `--repos_file path.txt`.

Both stages handle the rest unchanged.
