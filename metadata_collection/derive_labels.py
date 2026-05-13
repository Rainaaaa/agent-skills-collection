#!/usr/bin/env python3
"""Derive skill-level labels by joining the downloader's skill_status.csv
with the L1 crawler's level1_dedup.json.

Background
----------
The L1 listing crawler enumerates SkillsMP via occupation/category pages.
Skills in `level1_dedup.json` have direct labels (occupations, categories,
repository, stars, ...). But the downloaded packages tracked in
`skill_status.csv` are typically a superset — many were pulled from
GitHub via a previous-vintage SkillsMP listing or another source, so
SkillsMP doesn't index them by URL today.

This script fills the gap in two passes (free, no network):

  Pass 1 — direct match
      skill_id == trailing slug of an L1 URL → copy labels verbatim.
      Source tag: "direct".

  Pass 2 — repo propagation
      If the downloaded skill's `owner/repo` matches a `repository`
      field on ANY labeled L1 record, propagate that repo's
      majority-vote occupations + categories to the downloaded skill.
      Repo-level fields (`repository`, `stars`) are copied verbatim.
      Source tag: "repo_propagated".

  Residual — truly dark
      Downloaded skills whose `owner/repo` doesn't appear anywhere in
      L1 are written to `unlabeled_skill_ids.txt` for follow-up via
      `run_probe_unlabeled.sh` (direct URL probe against SkillsMP).

Outputs (under metadata_collection/output/)
-------------------------------------------
  derived_skill_labels.csv
      skill_id, source, occupations, categories, repository, stars, skill_name
      (occupations/categories pipe-joined: "a|b|c")

  unlabeled_skill_ids.txt
      one skill_id per line — input to run_probe_unlabeled.sh.

Run
---
  python derive_labels.py   [--skill_status PATH]   [--l1_dedup PATH]   [--output_dir PATH]

Cheap (seconds). Designed for the login node; no SLURM wrapper needed.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


def _load_l1(l1_path: Path) -> Tuple[Dict[str, dict], Dict[str, List[dict]]]:
    """Return (by_skill_id, by_repo)."""
    data = json.loads(l1_path.read_text())
    keys = data.get("keys") or {}
    by_id: Dict[str, dict] = {}
    by_repo: Dict[str, List[dict]] = defaultdict(list)
    for url, rec in keys.items():
        sid = url.rsplit("/", 1)[-1]
        by_id[sid] = rec
        repo = (rec.get("repository") or "").strip().lower()
        if repo:
            by_repo[repo].append(rec)
    return by_id, by_repo


def _majority(items: Iterable[dict], key: str) -> List[str]:
    """Union with frequency tie-break across sibling L1 records."""
    bag: Counter = Counter()
    for it in items:
        for x in (it.get(key) or []):
            bag[x] += 1
    return [x for x, _ in bag.most_common()]


def derive(
    skill_status_csv: Path,
    l1_dedup: Path,
    output_csv: Path,
    unlabeled_txt: Path,
) -> Counter:
    l1_by_id, l1_by_repo = _load_l1(l1_dedup)
    stats: Counter = Counter()
    rows: List[dict] = []
    dark: List[str] = []

    with skill_status_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip().lower() != "ok":
                continue
            sid = row["skill_id"]
            owner_repo = f"{row.get('owner','')}/{row.get('repo','')}".strip("/").lower()

            rec = l1_by_id.get(sid)
            if rec is not None:
                source = "direct"
                occs = rec.get("occupations") or []
                cats = rec.get("categories") or []
                repo = rec.get("repository") or ""
                stars = rec.get("stars") or ""
                name = rec.get("skill_name") or ""
            elif owner_repo in l1_by_repo:
                siblings = l1_by_repo[owner_repo]
                source = "repo_propagated"
                occs = _majority(siblings, "occupations")
                cats = _majority(siblings, "categories")
                repo = siblings[0].get("repository") or ""
                stars = siblings[0].get("stars") or ""
                name = ""
            else:
                stats["unlabeled"] += 1
                dark.append(sid)
                continue

            stats[source] += 1
            rows.append({
                "skill_id":    sid,
                "source":      source,
                "occupations": "|".join(occs),
                "categories":  "|".join(cats),
                "repository":  repo,
                "stars":       stars,
                "skill_name":  name,
            })

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "skill_id", "source", "occupations", "categories",
            "repository", "stars", "skill_name",
        ])
        w.writeheader()
        w.writerows(rows)
    unlabeled_txt.write_text("\n".join(dark) + ("\n" if dark else ""))

    return stats


def main() -> int:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Derive skill labels via direct match + repo propagation.")
    p.add_argument("--skill_status", type=Path,
                   default=Path("/N/slate/cz1/GitHub/AgentSkills-OSS/skills_download/output/skill_status.csv"),
                   help="skill_status.csv emitted by skills_download.")
    p.add_argument("--l1_dedup", type=Path,
                   default=here / "output" / "level1_dedup.json",
                   help="level1_dedup.json emitted by crawl_lists.py.")
    p.add_argument("--output_dir", type=Path,
                   default=here / "output",
                   help="Directory for derived_skill_labels.csv + unlabeled_skill_ids.txt.")
    args = p.parse_args()

    out_csv  = args.output_dir / "derived_skill_labels.csv"
    out_dark = args.output_dir / "unlabeled_skill_ids.txt"
    stats = derive(args.skill_status, args.l1_dedup, out_csv, out_dark)

    total = sum(stats.values())
    print(f"derived_skill_labels: {stats['direct'] + stats['repo_propagated']} rows  -> {out_csv}")
    print(f"unlabeled_skill_ids:  {stats['unlabeled']} ids        -> {out_dark}")
    print()
    print(f"coverage breakdown (of {total} downloaded skills):")
    for k in ("direct", "repo_propagated", "unlabeled"):
        n = stats[k]
        pct = (100.0 * n / total) if total else 0.0
        print(f"  {k:<16} {n:>7}  ({pct:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
