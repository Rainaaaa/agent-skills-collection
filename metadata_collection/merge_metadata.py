"""
Merge Level-1 (label membership + card fields) and Level-2 (detail-page
fields) outputs into training-ready metadata, and emit drop-in files for
skills_download/config.json.

Inputs:
  - output/level1_dedup.json            (from crawl_lists.py)
  - output/level2_details_index.json    (from crawl_details.py)  [optional]
  - output/level2_details.jsonl         (latest-wins per skillmp_link if index missing)
  - input/SOC_code.xlsx                 (hierarchy: detailed → broad → minor)
  - input/category.json                 (leaf subcategory → top category)

Outputs:
  - output/merged_metadata.jsonl        (one row per skill — the training view)
  - output/dedup_index.json             (skills_download-compatible)
  - output/repo_map.json                (skills_download-compatible)
  - output/skillsmp_metadata.jsonl      (legacy API-shape, drop-in for skills_download)

Level-2 is optional: if it's missing or partial, L1-only rows still flow through.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from _shared import (
    iter_jsonl as load_jsonl,
    load_json,
    now_ts,
    save_json,
    write_jsonl,
)

try:
    import pandas as pd
except ImportError:
    pd = None  # SOC derivation is skipped gracefully if pandas isn't installed


# -----------------------------------------------------------------------------
# Taxonomy lookups: lvl-3 slug -> (lvl-2 slug, lvl-1 slug, title)
# -----------------------------------------------------------------------------

SOC_LEAF_TRAILING_DIGITS = re.compile(r"-(\d{6})$")


def soc_code_from_leaf_slug(slug: str) -> Optional[str]:
    m = SOC_LEAF_TRAILING_DIGITS.search(slug)
    if not m:
        return None
    digits = m.group(1)  # e.g. "151252"
    return f"{digits[:2]}-{digits[2:]}"  # -> "15-1252"


def clean_slug(soc_code: str, title: str) -> str:
    code_flat = soc_code.lower().replace("-", "")
    t = (title or "").lower().replace(" ", "-").replace(",", "")
    return f"{t}-{code_flat}"


def build_soc_hierarchy(soc_xlsx: Path) -> Dict[str, Dict[str, Any]]:
    """Return: soc_lvl_3_slug -> {title, soc_code, soc_lvl_2, soc_lvl_1}."""
    if not soc_xlsx.exists():
        print(f"[WARN] SOC file not found: {soc_xlsx}. Parent-SOC derivation disabled.", file=sys.stderr)
        return {}
    if pd is None:
        print("[WARN] pandas not installed; SOC derivation disabled.", file=sys.stderr)
        return {}

    df = pd.read_excel(soc_xlsx)
    out: Dict[str, Dict[str, Any]] = {}
    # The xlsx columns: SOC Code, Title, SOC Broad Occupation, SOC Minor Occupation
    for _, row in df.iterrows():
        soc_code = str(row.get("SOC Code") or "").strip()
        title = str(row.get("Title") or "").strip()
        broad = str(row.get("SOC Broad Occupation") or "").strip()
        minor = str(row.get("SOC Minor Occupation") or "").strip()
        if not soc_code or not title:
            continue
        lvl3_slug = clean_slug(soc_code, title)
        out[lvl3_slug] = {
            "title": title,
            "soc_code": soc_code,
            "soc_broad": broad,
            "soc_minor": minor,
        }

    # Second pass: resolve broad/minor codes to their canonical titles and build slugs.
    # Easiest: dedup broad codes by picking any row with that broad code as representative;
    # title lookup uses the column `SOC Broad Occupation` title on the rare rows where
    # broad==detailed (aggregate rows). For the common case we build broad/minor slugs
    # as "<code-flat-only>" since category.json-style titles aren't given inline.
    for lvl3_slug, rec in out.items():
        rec["soc_lvl_3"] = lvl3_slug
        rec["soc_lvl_2"] = rec["soc_broad"].lower().replace("-", "") if rec["soc_broad"] else None
        rec["soc_lvl_1"] = rec["soc_minor"].lower().replace("-", "") if rec["soc_minor"] else None
    return out


def build_soc_lvl2_slug_map(soc_lvl_2_csv: Path) -> Dict[str, str]:
    """Broad-code flat (e.g. '151250') -> full slug (e.g. 'software-developers-151250')."""
    m: Dict[str, str] = {}
    if not soc_lvl_2_csv.exists():
        return m
    import csv as _csv
    with soc_lvl_2_csv.open("r", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        for row in reader:
            slug = (row.get("soc_lvl_2") or "").strip()
            if slug:
                trail = SOC_LEAF_TRAILING_DIGITS.search(slug)
                if trail:
                    m[trail.group(1)] = slug
    return m


def build_soc_lvl1_slug_map(soc_lvl_1_csv: Path) -> Dict[str, str]:
    m: Dict[str, str] = {}
    if not soc_lvl_1_csv.exists():
        return m
    import csv as _csv
    with soc_lvl_1_csv.open("r", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        for row in reader:
            slug = (row.get("soc_lvl_1") or "").strip()
            if slug:
                trail = SOC_LEAF_TRAILING_DIGITS.search(slug)
                if trail:
                    m[trail.group(1)] = slug
    return m


def build_category_parent_map(category_json: Path) -> Dict[str, str]:
    """leaf subcategory slug -> top-level category slug."""
    m: Dict[str, str] = {}
    if not category_json.exists():
        return m
    data = load_json(category_json)
    for top, children in data.items():
        if isinstance(children, list):
            for leaf in children:
                if isinstance(leaf, str):
                    m[leaf] = top
    return m


# -----------------------------------------------------------------------------
# Repo parsing (mirrors skills_collection/crawl.py so skills_download stays happy)
# -----------------------------------------------------------------------------

GITHUB_TREE_RE = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+)(?:/tree/([^/]+)(?:/(.+))?)?",
    re.IGNORECASE,
)


def parse_github_url(url: Optional[str]) -> Optional[Dict[str, str]]:
    if not url or "github.com" not in url:
        return None
    m = GITHUB_TREE_RE.search(url)
    if not m:
        return None
    owner, repo, branch, path = m.groups()
    repo = (repo or "").rstrip(".git")
    return {
        "owner": owner or "",
        "repo": repo,
        "branch": branch or "",
        "path": path or "",
    }


def pick_repo_url(merged_rec: Dict[str, Any]) -> Optional[str]:
    """Pick the best GitHub URL for a skill (L2 JSON-LD > L2 anchor > L1 repo hint)."""
    ld = merged_rec.get("jsonld_code_repository")
    if isinstance(ld, str) and "github.com" in ld:
        return ld
    for k in ("repository_link_l2", "repository_link"):
        v = merged_rec.get(k)
        if isinstance(v, str) and "github.com" in v:
            return v
    for gh in (merged_rec.get("github_links") or []):
        if isinstance(gh, str) and "github.com" in gh:
            return gh
    # L1 stored "owner/repo" string — synthesize a URL
    l1_repo = merged_rec.get("repository")
    if isinstance(l1_repo, str) and "/" in l1_repo:
        return f"https://github.com/{l1_repo}"
    return None


# -----------------------------------------------------------------------------
# L2 index loading — prefer the rolled-up index, fall back to the JSONL (latest wins)
# -----------------------------------------------------------------------------

def load_level2_records(index_file: Path, jsonl_file: Path) -> Dict[str, Dict[str, Any]]:
    if index_file.exists():
        idx = load_json(index_file)
        if isinstance(idx, dict):
            return idx

    latest: Dict[str, Dict[str, Any]] = {}
    for row in load_jsonl(jsonl_file):
        link = row.get("skillmp_link")
        if not link:
            continue
        prev = latest.get(link)
        if prev is None or (row.get("ts") or 0) >= (prev.get("ts") or 0):
            latest[link] = row
    return latest


# -----------------------------------------------------------------------------
# Merge core
# -----------------------------------------------------------------------------

def merge_one(
    skillmp_link: str,
    l1_rec: Dict[str, Any],
    l2_rec: Optional[Dict[str, Any]],
    soc_hier: Dict[str, Dict[str, Any]],
    lvl2_slug_map: Dict[str, str],
    lvl1_slug_map: Dict[str, str],
    category_parent_map: Dict[str, str],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "skillmp_link": skillmp_link,
        "skill_id": skillmp_link.rstrip("/").rsplit("/", 1)[-1],
        "first_seen_at": l1_rec.get("first_seen_at"),
        # L1 fields
        "skill_name_l1": l1_rec.get("skill_name"),
        "repository":    l1_rec.get("repository"),
        "description_l1": l1_rec.get("description"),
        "stars_l1":      l1_rec.get("stars"),
        "updated_at_l1": l1_rec.get("updated_at"),
        # Labels (leaves)
        "occupations":       list(l1_rec.get("occupations") or []),
        "categories":        list(l1_rec.get("categories") or []),
        "category_parents":  list(l1_rec.get("category_parents") or []),
    }

    # Derive SOC lvl-2 / lvl-1 slugs from each occupation leaf.
    soc_lvl_2 = set()
    soc_lvl_1 = set()
    for leaf in out["occupations"]:
        rec = soc_hier.get(leaf, {})
        broad_flat = rec.get("soc_lvl_2")
        minor_flat = rec.get("soc_lvl_1")
        if broad_flat and broad_flat in lvl2_slug_map:
            soc_lvl_2.add(lvl2_slug_map[broad_flat])
        if minor_flat and minor_flat in lvl1_slug_map:
            soc_lvl_1.add(lvl1_slug_map[minor_flat])
    out["occupation_soc_lvl_2"] = sorted(soc_lvl_2)
    out["occupation_soc_lvl_1"] = sorted(soc_lvl_1)

    # Re-derive category top parents from subcategory leaves (in case L1 missed any).
    derived_parents = {category_parent_map[c] for c in out["categories"] if c in category_parent_map}
    if derived_parents:
        merged_parents = set(out["category_parents"]) | derived_parents
        out["category_parents"] = sorted(merged_parents)

    # L2 fields
    if l2_rec:
        out.update({
            "skill_name_l2":         l2_rec.get("skill_name"),
            "description_l2":        l2_rec.get("description_primary"),
            "stars_l2":              l2_rec.get("stars"),
            "forks_l2":              l2_rec.get("forks"),
            "updated_at_l2":         l2_rec.get("updated_at_text"),
            "canonical_url":         l2_rec.get("canonical_url"),
            "run_in_manus_link":     l2_rec.get("run_in_manus_link"),
            "repository_link_l2":    l2_rec.get("repository_link"),
            "github_links":          l2_rec.get("github_links"),
            "skill_md_text":         l2_rec.get("skill_md_text"),
            "skill_md_html":         l2_rec.get("skill_md_html"),
            "jsonld_code_repository": l2_rec.get("jsonld_code_repository"),
            "jsonld_author_name":    l2_rec.get("jsonld_author_name"),
            "jsonld_date_modified":  l2_rec.get("jsonld_date_modified"),
        })

    # Training-friendly canonical fields (best source per field)
    out["skill_name"] = out.get("skill_name_l2") or out.get("skill_name_l1")
    out["description"] = out.get("description_l2") or out.get("description_l1")
    out["stars_text"] = out.get("stars_l2") or out.get("stars_l1")
    out["updated_at"] = out.get("updated_at_l2") or out.get("updated_at_l1")

    # Resolve the best repo URL and parse GitHub identity
    repo_url = pick_repo_url(out)
    out["repo_url"] = repo_url
    parsed = parse_github_url(repo_url) if repo_url else None
    if parsed:
        out["github_identity"] = parsed
        out["github_repo_key"] = f"{parsed['owner']}/{parsed['repo']}@{parsed['branch']}"

    return out


# -----------------------------------------------------------------------------
# skills_download-compatible outputs
# -----------------------------------------------------------------------------

def build_repo_map(merged_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    rmap: Dict[str, Any] = {}
    for rec in merged_rows:
        gi = rec.get("github_identity")
        if not gi:
            continue
        key = f"{gi['owner']}/{gi['repo']}@{gi['branch']}"
        entry = rmap.setdefault(key, {
            "owner": gi["owner"],
            "repo": gi["repo"],
            "branch": gi["branch"],
            "paths": [],
            "source_skills": [],
        })
        if gi["path"] and gi["path"] not in entry["paths"]:
            entry["paths"].append(gi["path"])
        sid = rec.get("skill_id") or rec.get("skillmp_link")
        if sid and sid not in entry["source_skills"]:
            entry["source_skills"].append(sid)
    return rmap


def build_dedup_index(merged_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    keys: Dict[str, Any] = {}
    for rec in merged_rows:
        sid = rec.get("skill_id") or rec.get("skillmp_link")
        if not sid:
            continue
        keys[f"id::{sid}"] = {
            "first_seen_at": rec.get("first_seen_at"),
            "id": sid,
            "name": rec.get("skill_name"),
            "githubUrl": rec.get("repo_url"),
            "skillUrl": rec.get("skillmp_link"),
        }
    return {"count": len(keys), "keys": keys, "updated_at": now_ts()}


def build_legacy_metadata_rows(merged_rows: List[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    """Shape each row like skills_collection/output/skillsmp_metadata.jsonl so
    skills_download/config.json can consume it with only a path swap."""
    for rec in merged_rows:
        yield {
            "id": rec.get("skill_id"),
            "name": rec.get("skill_name"),
            "author": (rec.get("github_identity") or {}).get("owner"),
            "description": rec.get("description"),
            "githubUrl": rec.get("repo_url"),
            "skillUrl": rec.get("skillmp_link"),
            "stars": rec.get("stars_text"),
            "updatedAt": rec.get("jsonld_date_modified") or rec.get("updated_at"),
            "parsed_github_identity": rec.get("github_identity"),
            "occupations": rec.get("occupations"),
            "occupation_soc_lvl_2": rec.get("occupation_soc_lvl_2"),
            "occupation_soc_lvl_1": rec.get("occupation_soc_lvl_1"),
            "categories": rec.get("categories"),
            "category_parents": rec.get("category_parents"),
            "crawler_source": "labels_collection",
        }


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Merge L1 + L2 into training metadata + skills_download-compatible files.")
    p.add_argument("--runtime_config", type=str, default="runtime_config.json")
    p.add_argument("--input_dir", type=str, default=None)
    p.add_argument("--output_dir", type=str, default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    runtime_cfg = load_json(Path(args.runtime_config))
    if args.input_dir:  runtime_cfg["input_dir"] = args.input_dir
    if args.output_dir: runtime_cfg["output_dir"] = args.output_dir

    input_dir = Path(runtime_cfg.get("input_dir", "./input")).resolve()
    output_dir = Path(runtime_cfg.get("output_dir", "./output")).resolve()

    l1_dedup_file = Path(runtime_cfg.get("level1", {}).get("dedup_file", output_dir / "level1_dedup.json")).resolve()
    l2_index_file = Path(runtime_cfg.get("level2", {}).get("dedup_file", output_dir / "level2_details_index.json")).resolve()
    l2_jsonl_file = Path(runtime_cfg.get("level2", {}).get("details_file", output_dir / "level2_details.jsonl")).resolve()

    merged_file       = Path(runtime_cfg.get("merged", {}).get("metadata_file", output_dir / "merged_metadata.jsonl")).resolve()
    repo_map_file     = Path(runtime_cfg.get("merged", {}).get("repo_map_file", output_dir / "repo_map.json")).resolve()
    dedup_index_file  = Path(runtime_cfg.get("merged", {}).get("dedup_index_file", output_dir / "dedup_index.json")).resolve()
    legacy_meta_file  = output_dir / "skillsmp_metadata.jsonl"

    if not l1_dedup_file.exists():
        print(f"[ERROR] L1 dedup not found at {l1_dedup_file}", file=sys.stderr)
        return 1

    print(f"[INFO] loading L1 dedup: {l1_dedup_file}")
    l1 = load_json(l1_dedup_file).get("keys", {})
    print(f"[INFO] L1 skills: {len(l1)}")

    print(f"[INFO] loading L2 records (index={l2_index_file.name}, jsonl={l2_jsonl_file.name})")
    l2 = load_level2_records(l2_index_file, l2_jsonl_file)
    print(f"[INFO] L2 skills: {len(l2)}")

    # Taxonomy
    soc_hier = build_soc_hierarchy(input_dir / "SOC_code.xlsx")
    lvl2_slug_map = build_soc_lvl2_slug_map(input_dir / "soc_lvl_2.csv")
    lvl1_slug_map = build_soc_lvl1_slug_map(input_dir / "soc_lvl_1.csv")
    category_parent_map = build_category_parent_map(input_dir / "category.json")
    print(
        f"[INFO] taxonomy: soc_lvl_3={len(soc_hier)} "
        f"soc_lvl_2_slugs={len(lvl2_slug_map)} "
        f"soc_lvl_1_slugs={len(lvl1_slug_map)} "
        f"category_leaves={len(category_parent_map)}"
    )

    merged_rows: List[Dict[str, Any]] = []
    for skillmp_link, l1_rec in l1.items():
        merged_rows.append(merge_one(
            skillmp_link=skillmp_link,
            l1_rec=l1_rec,
            l2_rec=l2.get(skillmp_link),
            soc_hier=soc_hier,
            lvl2_slug_map=lvl2_slug_map,
            lvl1_slug_map=lvl1_slug_map,
            category_parent_map=category_parent_map,
        ))

    n = write_jsonl(merged_file, merged_rows)
    print(f"[OK] wrote {n} rows -> {merged_file}")

    repo_map = build_repo_map(merged_rows)
    save_json(repo_map_file, repo_map)
    print(f"[OK] wrote repo_map ({len(repo_map)} repos) -> {repo_map_file}")

    dedup = build_dedup_index(merged_rows)
    save_json(dedup_index_file, dedup)
    print(f"[OK] wrote dedup_index ({dedup['count']} skills) -> {dedup_index_file}")

    legacy_rows = list(build_legacy_metadata_rows(merged_rows))
    n_legacy = write_jsonl(legacy_meta_file, legacy_rows)
    print(f"[OK] wrote legacy skillsmp_metadata.jsonl ({n_legacy} rows) -> {legacy_meta_file}")

    # Summary stats
    with_l2 = sum(1 for r in merged_rows if r.get("skill_md_text") or r.get("jsonld_code_repository"))
    with_repo = sum(1 for r in merged_rows if r.get("repo_url"))
    print(f"[SUMMARY] merged={len(merged_rows)} with_l2={with_l2} with_repo={with_repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
