# metadata_collection

A DOM-based crawler for [SkillsMP](https://skillsmp.com) that walks **list
pages** and **detail pages** to collect skills *with* their category and
occupation labels — things the API-based sibling [`skills_collection/`](../skills_collection/)
cannot get because the SkillsMP search API has no wildcard search and
doesn't return category / occupation tags.

Three stages, all implemented:

| Stage                | Script              | Purpose                                                            |
| -------------------- | ------------------- | ------------------------------------------------------------------ |
| **L1 (list pages)**  | `crawl_lists.py`    | Enumerate skill cards per occupation / category leaf page.         |
| **L2 (detail pages)**| `crawl_details.py`  | Visit each `skillmp_link` and pull full detail-page fields.        |
| **Merge**            | `merge_metadata.py` | Join L1 + L2 + SOC hierarchy → training-ready + `skills_download`-compatible files. |

All three use async Playwright + multi-worker concurrency (default 4 pages
in parallel) and resumable checkpoints.

---

## Why this module exists

- The API in `skills_collection/` returns skill metadata but **no category
  and no occupation labels**.
- Those labels live only in the web UI, on dedicated list pages:
  - `https://skillsmp.com/occupations/{soc_lvl_3}` — 846 leaf occupations.
  - `https://skillsmp.com/categories/{subcategory}` — ~60 leaf subcategories.
- A skill can appear under **multiple** occupations and **multiple**
  subcategories. We crawl only the **leaves**; parent labels (SOC lvl 1/2,
  top-level category) are derived at merge time from
  [`input/SOC_code.xlsx`](input/SOC_code.xlsx) and
  [`input/category.json`](input/category.json), so we never store them
  redundantly on each skill.

---

## Contents

```
metadata_collection/
├── crawler_jobs.json            # Selectors + job-space config (L1 + L2)
├── runtime_config.json          # Paths, browser, retry settings, worker defaults
│
├── crawl_lists.py               # Level 1 — async Playwright list-page crawler
├── crawl_details.py             # Level 2 — async detail-page crawler
├── merge_metadata.py            # Merge step — emits training + skills_download files
├── pw_minimal.py                # Bare smoke test for Playwright + a single page
│
├── run_crawl_lists.sh           # SLURM wrapper for L1
├── run_crawl_details.sh         # SLURM wrapper for L2
├── run_merge_metadata.sh        # SLURM wrapper for the merge
│
├── input/                       # Label sources
│   ├── category.json            # top-category → [leaf subcategory, …]
│   ├── SOC_code.xlsx            # Raw SOC taxonomy (source of truth for lvl-3 → lvl-2 → lvl-1)
│   ├── soc_lvl_1.csv            # SOC minor occupation slugs
│   ├── soc_lvl_2.csv            # SOC broad occupation slugs
│   └── soc_lvl_3.csv            # SOC detailed occupation slugs (crawled leaves)
│
├── log/                         # SLURM stdout / stderr
└── output/
    ├── level1_cards.jsonl               # append-only observation log
    ├── level1_dedup.json                # skill-level rollup with merged label sets
    ├── level1_request_log.jsonl
    ├── level2_details.jsonl             # append-only detail-page records
    ├── level2_details_index.json        # skillmp_link → rolled-up detail summary
    ├── level2_request_log.jsonl
    ├── merged_metadata.jsonl            # training-ready union (L1 + L2 + derived parents)
    ├── dedup_index.json                 # drop-in for skills_download/config.json
    ├── repo_map.json                    # drop-in for skills_download/config.json
    ├── skillsmp_metadata.jsonl          # legacy API-shape, drop-in for skills_download
    ├── checkpoints/
    │   ├── level1_checkpoint.json
    │   └── level2_checkpoint.json
    └── logs/
```

---

## How it works

### Level 1 — list-page enumeration

`crawl_lists.py` builds the job list as the Cartesian product
`(source_type, source_label, sort_mode)` where:

- `source_type ∈ {occupation, category}` (toggle via `crawler_jobs.json`).
- `source_label` ∈ 846 SOC lvl-3 slugs ∪ ~62 category-leaf slugs.
- `sort_mode ∈ {stars, recent}` — both are crawled because `recent`
  surfaces skills that `stars` misses (confirmed on smoke test).

Total: **~1816 jobs**.

Each job:
1. Navigates to the list URL (headless Chromium).
2. Clicks the `stars` or `recent` sort button.
3. Scrolls to the bottom until the EOF marker appears *or* the card count
   stops growing for `max_no_progress_scrolls` (=15) iterations
   (~18 s of patient waiting before declaring a label exhausted).
4. Iterates every `a[href^="/skills/"]` anchor and extracts
   `skill_name` (`span[title]` attr), `repository`
   (`span.text-green-600`), `description` (`div[class*="leading-relaxed"]`),
   `stars` (`svg.lucide-star + span`), `updated_at`
   (`div.mt-auto span.text-muted-foreground`).
5. Appends **one observation per card** to `level1_cards.jsonl`.
6. Rolls that observation into `level1_dedup.json` — the skill-level view
   with `{occupations: [...], categories: [...], category_parents: [...]}`
   unioned across all observations.

### Level 2 — detail-page extraction

`crawl_details.py` reads `level1_dedup.json` and treats each `skillmp_link`
as a job. For each skill it:

1. Navigates to the detail page.
2. Waits for progressive hydration (`h1` visible, then 1–3 short settle
   waits until body text length stops growing).
3. Extracts in a single `page.evaluate()` call:
   - `skill_name` (`h1`), `description_primary`, `stars`, `forks`, `updated_at`.
   - `canonical_url`, `run_in_manus_link`, `repository_link`, all
     `github_links` on the page.
   - `skill_md_text` / `skill_md_html` (the full SKILL.md body).
   - **All** `<script type="application/ld+json">` payloads, parsed and
     searched for `codeRepository`, `author.name`, `dateModified`.
4. Appends to `level2_details.jsonl` and rolls a small summary into
   `level2_details_index.json`.

### Merge

`merge_metadata.py` joins L1 + L2 per `skillmp_link` and derives parent
labels:

- **Occupation lvl-2 / lvl-1**: parsed from each leaf's trailing 6-digit
  SOC code. `input/SOC_code.xlsx` gives the `SOC Broad Occupation` /
  `SOC Minor Occupation` for each detailed code; those are then matched
  back to the slug form in `soc_lvl_2.csv` / `soc_lvl_1.csv`.
- **Category parent**: leaf subcategory → top category via
  `input/category.json`.
- **Best repo URL**: prefers L2 JSON-LD `codeRepository`, falls back to
  the anchor in the detail page, then to the L1 `"owner/repo"` hint.

Outputs:
- `merged_metadata.jsonl` — one row per skill with everything joined
  (L1 labels + L2 body + derived parents + canonical field picks).
- `dedup_index.json`, `repo_map.json`, `skillsmp_metadata.jsonl` — shapes
  that match [`skills_collection/output/`](../skills_collection/output/),
  so [`skills_download/config.json`](../skills_download/config.json) can
  consume them with only a path swap.

---

## Setup

### 1. Python environment

```bash
conda activate AgentSkillsOSS
pip install playwright pandas openpyxl
python -m playwright install chromium                # ~170 MB download
```

### 2. Label files

Already present in [`input/`](input/). Re-run
`../skills_collection/clean_soc.py` if `SOC_code.xlsx` ever changes.

---

## Running

### Level 1

```bash
# Full crawl (occupations + categories, both sort modes, 4 concurrent pages)
sbatch run_crawl_lists.sh

# Override workers / source / max_jobs
sbatch --export=WORKERS=8 run_crawl_lists.sh
sbatch --export=SOURCE=category run_crawl_lists.sh
sbatch --export=MAX_JOBS=8 run_crawl_lists.sh              # smoke test
sbatch --export=MODE=reset run_crawl_lists.sh              # forget checkpoint
```

Tested throughput: **~10 jobs/min at 4 workers**, ≈ 3 hours for the full
1816-job sweep on the IU Slate `general` partition.

### Level 2

```bash
sbatch run_crawl_details.sh                                # resume
sbatch --export=WORKERS=8 run_crawl_details.sh             # more parallelism
sbatch --export=MAX_JOBS=50 run_crawl_details.sh           # smoke test
```

L2 can run against a **partial** `level1_dedup.json` — just rerun when
more L1 data arrives (the checkpoint's `done_job_keys` set keeps skipping
already-done skills).

### Merge

Fast (minutes); can be run on the login node or via sbatch:

```bash
# Direct
python merge_metadata.py

# Or via SLURM
sbatch run_merge_metadata.sh
```

Running merge is safe to re-run — it overwrites the three output files
from scratch.

### Smoke tests (local / debug allocation)

```bash
cd /N/slate/cz1/GitHub/AgentSkills-OSS/metadata_collection
python pw_minimal.py                                        # raw Playwright smoke
python crawl_lists.py  --source category --max_jobs 4       # L1 smoke
python crawl_details.py --max_jobs 10                       # L2 smoke (needs L1 output)
python merge_metadata.py                                    # merge whatever we have
```

---

## Periodic re-run (grow coverage over time)

SkillsMP grows continuously — new skills get added under existing
occupation/category leaves daily. `run_periodic.sh` is a single SLURM
script that performs one full refresh cycle:

1. **L1 with `--reset_checkpoint`** — re-crawls every one of the 1816
   leaf pages from scratch. `level1_cards.jsonl` (append-only
   observations) and `level1_dedup.json` (skill-level rollup) are
   *preserved across runs* — new observations append, new skills become
   new dedup entries, and any newly-added label memberships union into
   existing entries' `occupations` / `categories` lists.

2. **L2 without reset** — its `done_job_keys` checkpoint already lists
   every `skillmp_link` we've extracted detail for, so this run only
   processes the **delta**: skills that L1 just discovered for the first
   time.

```bash
# Standard periodic refresh — full sweep, both sort modes, very patient scroll.
# Run this on the FIRST big run, or whenever you want a full re-sweep.
sbatch run_periodic.sh

# FAST incremental update — sort=recent ONLY, stops scrolling each label page
# once the tail of rendered cards is 50 already-known skills. Drastically
# faster than the full sweep once the dedup is large; ideal for daily/weekly
# cron runs once the initial big sweep is done.
sbatch --export=INCREMENTAL=1 run_periodic.sh

# If a 10h cap interrupts L1 mid-sweep, resubmit without re-resetting:
sbatch --export=MODE=resume run_periodic.sh

# Refresh L1 only, defer L2 catch-up:
sbatch --export=SKIP_L2=1 run_periodic.sh

# Bump worker count for faster runs:
sbatch --export=WORKERS=12 run_periodic.sh
```

### Two operating modes

| Use case                     | Command                                       | sort_modes      | Per-page scroll | Speed |
| ---------------------------- | --------------------------------------------- | --------------- | --------------- | ----- |
| Initial big sweep            | `sbatch run_periodic.sh`                      | stars + recent  | up to 10000     | slow  |
| Daily/weekly incremental     | `sbatch --export=INCREMENTAL=1 run_periodic.sh` | recent only   | early-stop on 50 known | fast  |

**Why `recent` is enough for incremental:** the live site's `recent` sort
puts newest skills first. Once we scroll into 50 consecutive skills already
in our dedup, every skill below them is also pre-existing — there's nothing
new past that point. The crawler exits that label page early and moves on.

Important: the periodic pipeline **does not use search-query crawling**.
It strictly enumerates `/categories/{leaf}` and `/occupations/{leaf}` — the
two URL families that have authoritative label semantics on SkillsMP.
Coverage grows monotonically as the site adds skills to those existing
leaves.

---

## Live-site selector drift

The selectors in `crawler_jobs.json` were reverse-engineered from the live
DOM on 2026-04; the earlier Notion scraping spec was partially stale and
its `div.grid.grid-cols-1...` card wrapper does not exist anymore. If the
site reshuffles class names, you'll see empty card extraction — update
`crawler_jobs.json` (no Python changes needed) and rerun.

L2 selectors are especially likely to drift because the detail page has
many hydrated widgets. On first real run, spot-check with:

```bash
head -1 output/level2_details.jsonl | jq '{skill_name, description_primary, forks, jsonld_code_repository, has_md: (.skill_md_text|length > 0)}'
```

If `skill_md_text` is empty but `skill_md_html` is not, the container
selector needs widening. If both are empty, the container selector needs
replacement entirely.

---

## Feeding results into skills_download

[`skills_download/config.json`](../skills_download/config.json) currently
points at [`../skills_collection/output/`](../skills_collection/output/).
After the merge, swap those three paths to this module's `output/`:

```diff
- "repo_map":          ".../skills_collection/output/repo_map.json",
- "dedup_index":       ".../skills_collection/output/dedup_index.json",
- "skillsmp_metadata": ".../skills_collection/output/skillsmp_metadata.jsonl",
+ "repo_map":          ".../metadata_collection/output/repo_map.json",
+ "dedup_index":       ".../metadata_collection/output/dedup_index.json",
+ "skillsmp_metadata": ".../metadata_collection/output/skillsmp_metadata.jsonl",
```

The shapes are the same so `download_skill_packages.py` needs no changes.

---

## Output schemas (condensed)

### `level1_cards.jsonl`
```json
{"ts": 1745000000, "worker": 2, "source_type": "category",
 "source_label": "defi", "parent_label": "blockchain", "sort_mode": "stars",
 "page_url": "https://skillsmp.com/categories/defi",
 "skill_id": "sickn33-...-skill-md", "skill_name": "lightning-architecture-review.md",
 "repository": "sickn33/antigravity-awesome-skills", "description": "...",
 "stars": "34.4k", "updated_at": "2026-04-13",
 "skillmp_link": "https://skillsmp.com/skills/..."}
```

### `level1_dedup.json` (keyed on `skillmp_link`)
```json
{"count": 18432, "keys": {
  "https://skillsmp.com/skills/...": {
    "first_seen_at": 1745000000, "skill_name": "...", "repository": "...",
    "stars": "...", "updated_at": "...",
    "occupations":       ["software-developers-151252", ...],
    "categories":        ["llm-ai", "automation-tools"],
    "category_parents":  ["data-ai", "tools"]
  }
}}
```

### `level2_details.jsonl`
Every key from `DETAIL_SCRIPT` in [crawl_details.py](crawl_details.py),
plus `skillmp_link`, `url`, `l1_hint`, `ts`, `worker`.

### `merged_metadata.jsonl` (training view, one row per skill)
```json
{"skillmp_link": "...", "skill_id": "...",
 "skill_name": "...", "description": "...",
 "repository": "owner/repo", "repo_url": "https://github.com/owner/repo/tree/main/path",
 "github_identity": {"owner": "...", "repo": "...", "branch": "main", "path": "..."},
 "github_repo_key": "owner/repo@main",
 "stars_text": "34.4k", "updated_at": "2026-04-13",
 "occupations":            [leaf, ...],
 "occupation_soc_lvl_2":   [broad-slug, ...],
 "occupation_soc_lvl_1":   [minor-slug, ...],
 "categories":             [leaf, ...],
 "category_parents":       [top-category, ...],
 "skill_md_text": "...", "skill_md_html": "...",
 "jsonld_code_repository": "...", "jsonld_author_name": "...",
 "jsonld_date_modified": "..."}
```

### `dedup_index.json` and `repo_map.json`
Shapes identical to [`skills_collection/output/`](../skills_collection/output/);
see those files for reference.
