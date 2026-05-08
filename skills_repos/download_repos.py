#!/usr/bin/env python3
"""Stage 3 — download GitHub repo archives that pass configured filters.

This stage is intentionally decoupled from `fetch_metadata.py`: it consumes
the metadata index, applies filters (license, archived state, min stars, …),
and downloads `tar.gz` archives via the unauthenticated public archive
endpoint. The metadata fetch is what carries the rate-limit cost; the
download leg only hits the public CDN and parallelizes cleanly.

Inputs:
  --metadata          path to github_metadata_index.json (Stage 2 output, default)
  --metadata_jsonl    path to github_metadata.jsonl (fallback if no index)
  --repo_map          path to repo_map.json (Stage 1) — needed to build
                      per-skill package views
  --skills_dedup      path to dedup_index.json (Stage 1) — used for per-package
                      manifest enrichment

Filters (all optional; intersection):
  --require_license               drop repos with no detected license
  --license_whitelist a,b,c       keep only repos whose SPDX id is in this set
  --license_blacklist x,y         drop repos whose SPDX id is in this set
  --skip_archived                 drop archived repos
  --skip_disabled                 drop disabled repos
  --skip_forks                    drop forks
  --min_stars N                   keep repos with stargazers_count >= N

Outputs (under runtime_config.json `output_dir`):
  raw_repos/<owner>__<repo>__<branch>/repo.tar.gz
  raw_repos/<owner>__<repo>__<branch>/extracted/<top>/...
  raw_repos/<owner>__<repo>__<branch>/.ready.json     (presence = cached)
  packages/<skill_id>/manifest.json                   (per-skill view)
  packages/<skill_id>/files                           (symlink → extracted skill path)
  packages/<skill_id>/SKILL.md                        (symlink, when present)
  output/repo_download_log.jsonl
  output/package_index.jsonl
  output/repo_status.csv
  output/skill_status.csv
  output/checkpoints/download_repos_checkpoint.json
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from _shared import (
    append_jsonl,
    ensure_dir,
    iter_jsonl,
    load_done_set,
    load_json,
    now_ts,
    repo_key_for,
    safe_repo_dir_name,
    save_done_set,
    save_json,
    split_repo_key,
)


# ---------------------------------------------------------------------------
# CSV helper (no need to live in _shared — only this script writes CSV)
# ---------------------------------------------------------------------------

def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class RepoFilter:
    """Boolean predicate over Stage-2 metadata summaries."""

    def __init__(
        self,
        require_license: bool = False,
        license_whitelist: Optional[Set[str]] = None,
        license_blacklist: Optional[Set[str]] = None,
        skip_archived: bool = False,
        skip_disabled: bool = False,
        skip_forks: bool = False,
        min_stars: int = 0,
    ):
        self.require_license = require_license
        self.license_whitelist = {s.lower() for s in (license_whitelist or set())}
        self.license_blacklist = {s.lower() for s in (license_blacklist or set())}
        self.skip_archived = skip_archived
        self.skip_disabled = skip_disabled
        self.skip_forks = skip_forks
        self.min_stars = int(min_stars or 0)

    def reason_to_skip(self, summary: Dict[str, Any]) -> Optional[str]:
        spdx = (summary.get("license_spdx_id") or "").lower() or None

        if self.require_license and not spdx:
            return "no_license"
        if self.license_whitelist and (spdx not in self.license_whitelist):
            return f"license_not_whitelisted({spdx})"
        if self.license_blacklist and spdx and spdx in self.license_blacklist:
            return f"license_blacklisted({spdx})"

        if self.skip_archived and summary.get("archived"):
            return "archived"
        if self.skip_disabled and summary.get("disabled"):
            return "disabled"
        if self.skip_forks and summary.get("fork"):
            return "fork"

        stars = summary.get("stargazers_count") or 0
        try:
            stars = int(stars)
        except (TypeError, ValueError):
            stars = 0
        if stars < self.min_stars:
            return f"min_stars(have={stars},want={self.min_stars})"

        return None


# ---------------------------------------------------------------------------
# Metadata loading — prefer the rolled-up index, fall back to the JSONL
# ---------------------------------------------------------------------------

def load_metadata_index(index_path: Path, jsonl_path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """Returns repo_key → summary dict (latest record wins)."""
    if index_path.exists():
        idx = load_json(index_path)
        if isinstance(idx, dict):
            return idx

    if not jsonl_path or not jsonl_path.exists():
        return {}

    latest: Dict[str, Dict[str, Any]] = {}
    for row in iter_jsonl(jsonl_path):
        rk = row.get("repo_key")
        if not rk:
            owner = row.get("owner")
            repo = row.get("repo")
            if not owner or not repo:
                continue
            rk = repo_key_for(owner, repo)
        prev = latest.get(rk)
        if prev is None or (row.get("ts") or 0) >= (prev.get("ts") or 0):
            summary = row.get("summary") or {}
            latest[rk] = {**summary, "ts": row.get("ts")}
    return latest


# ---------------------------------------------------------------------------
# Archive download
# ---------------------------------------------------------------------------

def github_archive_url(owner: str, repo: str, branch: str) -> str:
    return f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.tar.gz"


def download_file(url: str, out_path: Path, retries: int = 3, timeout: int = 60) -> None:
    ensure_dir(out_path.parent)
    headers = {"User-Agent": "agent-skills-collection/1.0"}
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp, out_path.open("wb") as f:
                shutil.copyfileobj(resp, f)
            return
        except (HTTPError, URLError, TimeoutError) as e:
            last_err = e
            time.sleep(min(2 ** attempt, 10))
    raise RuntimeError(f"Failed to download {url}: {last_err}")


def extract_tar_gz(tar_path: Path, dest_dir: Path) -> Path:
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    ensure_dir(dest_dir)
    with tarfile.open(tar_path, "r:gz") as tf:
        tf.extractall(dest_dir)
    subdirs = [p for p in dest_dir.iterdir() if p.is_dir()]
    if len(subdirs) == 1:
        return subdirs[0]
    return dest_dir


def remove_if_exists(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def relativize_symlink(target: Path, link_path: Path) -> str:
    return os.path.relpath(target, start=link_path.parent)


def find_skill_md(skill_root: Path) -> Optional[Path]:
    for name in ("SKILL.md", "skill.md"):
        p = skill_root / name
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# Per-repo work unit
# ---------------------------------------------------------------------------

def download_one_repo(
    repo_key: str,
    owner: str,
    repo: str,
    branch: str,
    raw_root: Path,
    force: bool,
    timeout: int,
) -> Dict[str, Any]:
    repo_dir_name = safe_repo_dir_name(repo_key)
    repo_root = raw_root / repo_dir_name
    archive_path = repo_root / "repo.tar.gz"
    extract_parent = repo_root / "extracted"
    ready_marker = repo_root / ".ready.json"

    if ready_marker.exists() and not force:
        return {
            "repo_key": repo_key,
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "status": "cached",
            "url": github_archive_url(owner, repo, branch),
            "archive_path": str(archive_path),
            "extract_parent": str(extract_parent),
            "extracted_top": "",
            "error": "",
            "elapsed_sec": 0.0,
        }

    ensure_dir(repo_root)
    url = github_archive_url(owner, repo, branch)
    started = time.time()
    try:
        if force or not archive_path.exists():
            download_file(url, archive_path, timeout=timeout)
        extracted_top = extract_tar_gz(archive_path, extract_parent)
        result = {
            "repo_key": repo_key,
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "status": "ok",
            "url": url,
            "archive_path": str(archive_path),
            "extract_parent": str(extract_parent),
            "extracted_top": str(extracted_top),
            "error": "",
            "elapsed_sec": round(time.time() - started, 3),
        }
        save_json(ready_marker, result)
        return result
    except Exception as e:
        return {
            "repo_key": repo_key,
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "status": "error",
            "url": url,
            "archive_path": str(archive_path),
            "extract_parent": str(extract_parent),
            "extracted_top": "",
            "error": repr(e),
            "elapsed_sec": round(time.time() - started, 3),
        }


# ---------------------------------------------------------------------------
# Per-skill package views
# ---------------------------------------------------------------------------

def build_skill_id_to_meta(dedup_index: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    keys = dedup_index.get("keys", {}) if isinstance(dedup_index, dict) else {}
    out: Dict[str, Dict[str, Any]] = {}
    for _, item in keys.items():
        sid = item.get("id") or item.get("skill_id")
        if sid:
            out[sid] = item
    return out


def repo_entry_pairs(repo_info: Dict[str, Any]) -> List[Tuple[str, str]]:
    paths = repo_info.get("paths") or []
    source_skills = repo_info.get("source_skills") or []
    n = min(len(paths), len(source_skills))
    return list(zip(paths[:n], source_skills[:n]))


def create_package_views(
    repo_map: Dict[str, Any],
    dedup_meta: Dict[str, Dict[str, Any]],
    license_meta_by_key: Dict[str, Dict[str, Any]],
    raw_root: Path,
    packages_root: Path,
    repo_results_by_key: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    package_rows: List[Dict[str, Any]] = []

    for repo_key, repo_info in repo_map.items():
        repo_result = repo_results_by_key.get(repo_key, {})
        repo_status = repo_result.get("status", "unknown")

        repo_dir_name = safe_repo_dir_name(repo_key)
        extract_parent = raw_root / repo_dir_name / "extracted"
        license_summary = license_meta_by_key.get(repo_key, {}) or {}

        if repo_status not in ("ok", "cached"):
            for rel_path, skill_id in repo_entry_pairs(repo_info):
                package_rows.append({
                    "skill_id": skill_id,
                    "repo_key": repo_key,
                    "owner": repo_info.get("owner"),
                    "repo": repo_info.get("repo"),
                    "branch": repo_info.get("branch"),
                    "relative_path": rel_path,
                    "status": f"repo_{repo_status}",
                    "package_dir": "",
                    "has_skill_md": False,
                    "license_spdx_id": license_summary.get("license_spdx_id"),
                    "error": repo_result.get("error", "") or "",
                })
            continue

        if not extract_parent.exists():
            for rel_path, skill_id in repo_entry_pairs(repo_info):
                package_rows.append({
                    "skill_id": skill_id,
                    "repo_key": repo_key,
                    "owner": repo_info.get("owner"),
                    "repo": repo_info.get("repo"),
                    "branch": repo_info.get("branch"),
                    "relative_path": rel_path,
                    "status": "missing_extract",
                    "package_dir": "",
                    "has_skill_md": False,
                    "license_spdx_id": license_summary.get("license_spdx_id"),
                    "error": "extract_parent_missing",
                })
            continue

        top_dirs = [p for p in extract_parent.iterdir() if p.is_dir()]
        extracted_top = top_dirs[0] if len(top_dirs) == 1 else extract_parent

        for rel_path, skill_id in repo_entry_pairs(repo_info):
            skill_root = extracted_top / rel_path
            pkg_dir = packages_root / skill_id

            if not skill_root.exists():
                package_rows.append({
                    "skill_id": skill_id,
                    "repo_key": repo_key,
                    "owner": repo_info.get("owner"),
                    "repo": repo_info.get("repo"),
                    "branch": repo_info.get("branch"),
                    "relative_path": rel_path,
                    "status": "missing_skill_path",
                    "package_dir": "",
                    "has_skill_md": False,
                    "license_spdx_id": license_summary.get("license_spdx_id"),
                    "error": "skill_root_missing",
                })
                continue

            remove_if_exists(pkg_dir)
            ensure_dir(pkg_dir)

            manifest = {
                "skill_id": skill_id,
                "repo_key": repo_key,
                "owner": repo_info.get("owner"),
                "repo": repo_info.get("repo"),
                "branch": repo_info.get("branch"),
                "relative_path": rel_path,
                "source_skill_meta": dedup_meta.get(skill_id, {}),
                "license_spdx_id": license_summary.get("license_spdx_id"),
                "license_key": license_summary.get("license_key"),
                "license_name": license_summary.get("license_name"),
                "license_url": license_summary.get("license_url"),
                "package_dir": str(pkg_dir),
                "files_symlink": "files",
            }
            save_json(pkg_dir / "manifest.json", manifest)

            files_link = pkg_dir / "files"
            os.symlink(relativize_symlink(skill_root, files_link), files_link)

            skill_md = find_skill_md(skill_root)
            has_skill_md = skill_md is not None
            if skill_md is not None:
                skill_md_link = pkg_dir / "SKILL.md"
                os.symlink(relativize_symlink(skill_md, skill_md_link), skill_md_link)

            package_rows.append({
                "skill_id": skill_id,
                "repo_key": repo_key,
                "owner": repo_info.get("owner"),
                "repo": repo_info.get("repo"),
                "branch": repo_info.get("branch"),
                "relative_path": rel_path,
                "status": "ok" if has_skill_md else "no_skill_md",
                "package_dir": str(pkg_dir),
                "has_skill_md": has_skill_md,
                "license_spdx_id": license_summary.get("license_spdx_id"),
                "error": "",
            })

    return package_rows


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def normalize_paths(runtime_cfg: Dict[str, Any], output_override: Optional[str]) -> Dict[str, Any]:
    cfg = dict(runtime_cfg)
    if output_override:
        cfg["output_dir"] = output_override
    output_dir = Path(cfg.get("output_dir", "./output")).resolve()
    cfg["output_dir"] = str(output_dir)

    dl = dict(cfg.get("download_repos", {}))
    dl["raw_repos_root"]    = str(Path(dl.get("raw_repos_root",    output_dir / "raw_repos")).resolve())
    dl["packages_root"]     = str(Path(dl.get("packages_root",     output_dir / "packages")).resolve())
    dl["repo_log_file"]     = str(Path(dl.get("repo_log_file",     output_dir / "repo_download_log.jsonl")).resolve())
    dl["package_index_file"]= str(Path(dl.get("package_index_file",output_dir / "package_index.jsonl")).resolve())
    dl["repo_status_csv"]   = str(Path(dl.get("repo_status_csv",   output_dir / "repo_status.csv")).resolve())
    dl["skill_status_csv"]  = str(Path(dl.get("skill_status_csv",  output_dir / "skill_status.csv")).resolve())
    dl["checkpoint_file"]   = str(Path(dl.get("checkpoint_file",   output_dir / "checkpoints" / "download_repos_checkpoint.json")).resolve())
    cfg["download_repos"] = dl

    fm = dict(cfg.get("fetch_metadata", {}))
    fm["index_file"]    = str(Path(fm.get("index_file",    output_dir / "github_metadata_index.json")).resolve())
    fm["metadata_file"] = str(Path(fm.get("metadata_file", output_dir / "github_metadata.jsonl")).resolve())
    cfg["fetch_metadata"] = fm

    cfg["checkpoint_dir"] = str(Path(cfg.get("checkpoint_dir", output_dir / "checkpoints")).resolve())
    cfg["log_dir"]        = str(Path(cfg.get("log_dir",        output_dir / "logs")).resolve())
    return cfg


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 3 — license-aware GitHub archive downloader.")
    p.add_argument("--runtime_config", type=str, default="runtime_config.json")
    p.add_argument("--output_dir",     type=str, default=None)

    p.add_argument("--repo_map",       type=str, required=True,
                   help="repo_map.json from metadata_collection/merge_metadata.py.")
    p.add_argument("--skills_dedup",   type=str, default=None,
                   help="dedup_index.json from metadata_collection (for per-package manifest).")
    p.add_argument("--metadata",       type=str, default=None,
                   help="github_metadata_index.json from fetch_metadata.py "
                        "(default: runtime_config.fetch_metadata.index_file).")
    p.add_argument("--metadata_jsonl", type=str, default=None,
                   help="github_metadata.jsonl fallback if no index is present.")

    # Filters
    p.add_argument("--require_license",   action="store_true")
    p.add_argument("--license_whitelist", type=str, default=None,
                   help="Comma-separated SPDX ids (case-insensitive).")
    p.add_argument("--license_blacklist", type=str, default=None,
                   help="Comma-separated SPDX ids (case-insensitive).")
    p.add_argument("--skip_archived",     action="store_true")
    p.add_argument("--skip_disabled",     action="store_true")
    p.add_argument("--skip_forks",        action="store_true")
    p.add_argument("--min_stars",         type=int, default=0)

    p.add_argument("--workers",   type=int, default=None)
    p.add_argument("--force",     action="store_true",
                   help="Re-download repos even if .ready.json marker exists.")
    p.add_argument("--max_repos", type=int, default=None)
    p.add_argument("--reset_checkpoint", action="store_true")
    return p.parse_args()


def _to_set(s: Optional[str]) -> Optional[Set[str]]:
    if not s:
        return None
    return {x.strip().lower() for x in s.split(",") if x.strip()}


def main() -> int:
    args = parse_args()

    runtime_cfg = load_json(Path(args.runtime_config))
    runtime_cfg = normalize_paths(runtime_cfg, args.output_dir)

    output_dir = Path(runtime_cfg["output_dir"])
    ensure_dir(output_dir)
    ensure_dir(Path(runtime_cfg["checkpoint_dir"]))
    ensure_dir(Path(runtime_cfg["log_dir"]))

    dl_paths = {
        "raw":           Path(runtime_cfg["download_repos"]["raw_repos_root"]),
        "packages":      Path(runtime_cfg["download_repos"]["packages_root"]),
        "repo_log":      Path(runtime_cfg["download_repos"]["repo_log_file"]),
        "package_index": Path(runtime_cfg["download_repos"]["package_index_file"]),
        "repo_status":   Path(runtime_cfg["download_repos"]["repo_status_csv"]),
        "skill_status":  Path(runtime_cfg["download_repos"]["skill_status_csv"]),
        "checkpoint":    Path(runtime_cfg["download_repos"]["checkpoint_file"]),
    }
    ensure_dir(dl_paths["raw"])
    ensure_dir(dl_paths["packages"])

    req_cfg = runtime_cfg.get("request_settings", {}) or {}
    timeout = int(req_cfg.get("timeout_seconds", 60))
    workers = int(args.workers if args.workers is not None
                  else (runtime_cfg.get("download_repos", {}).get("workers") or 8))

    # Inputs
    repo_map = load_json(Path(args.repo_map))
    if not isinstance(repo_map, dict):
        print(f"[ERROR] repo_map.json must be a dict; got {type(repo_map).__name__}", file=sys.stderr)
        return 1

    dedup_meta: Dict[str, Dict[str, Any]] = {}
    if args.skills_dedup:
        dedup_meta = build_skill_id_to_meta(load_json(Path(args.skills_dedup)))

    md_index_path = Path(args.metadata) if args.metadata else Path(runtime_cfg["fetch_metadata"]["index_file"])
    md_jsonl_path = Path(args.metadata_jsonl) if args.metadata_jsonl else Path(runtime_cfg["fetch_metadata"]["metadata_file"])
    md_index = load_metadata_index(md_index_path, md_jsonl_path)
    print(f"[INFO] metadata index: {len(md_index)} entries")

    # Filter
    filt = RepoFilter(
        require_license=args.require_license,
        license_whitelist=_to_set(args.license_whitelist),
        license_blacklist=_to_set(args.license_blacklist),
        skip_archived=args.skip_archived,
        skip_disabled=args.skip_disabled,
        skip_forks=args.skip_forks,
        min_stars=args.min_stars,
    )

    # Selection — for each repo_key in repo_map, look up metadata, apply filter.
    pending: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    skipped_rows: List[Dict[str, Any]] = []
    for repo_key, repo_info in repo_map.items():
        # metadata is keyed without trailing branch in some flows — try both
        summary = (
            md_index.get(repo_key)
            or md_index.get(repo_key.rstrip("@"))
            or md_index.get(f"{repo_info.get('owner')}/{repo_info.get('repo')}@")
            or {}
        )
        skip_reason = filt.reason_to_skip(summary)
        if skip_reason:
            skipped_rows.append({
                "repo_key": repo_key,
                "status": f"filtered:{skip_reason}",
                "license_spdx_id": summary.get("license_spdx_id"),
                "default_branch":  summary.get("default_branch"),
            })
            continue
        pending.append((repo_key, repo_info, summary))

    print(f"[INFO] repos in repo_map={len(repo_map)} pending={len(pending)} filtered={len(skipped_rows)}")

    # Checkpoint
    if args.reset_checkpoint and dl_paths["checkpoint"].exists():
        dl_paths["checkpoint"].unlink()
    done = load_done_set(dl_paths["checkpoint"])
    if not args.force and done:
        before = len(pending)
        pending = [p for p in pending if p[0] not in done]
        print(f"[INFO] Skipping {before - len(pending)} repos already in checkpoint; pending={len(pending)}.")

    if args.max_repos:
        pending = pending[: args.max_repos]
        print(f"[INFO] --max_repos={args.max_repos}; truncated to {len(pending)}.")

    # Drive parallel downloads. The unauthenticated archive endpoint is fine
    # with concurrent fetches (different hosts than api.github.com), but we
    # cap at `workers` so we don't blast a single user/repo origin.
    repo_results: List[Dict[str, Any]] = []
    if pending:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = []
            for repo_key, repo_info, summary in pending:
                owner = repo_info.get("owner") or summary.get("owner") or split_repo_key(repo_key)[0]
                repo = repo_info.get("repo") or summary.get("name") or split_repo_key(repo_key)[1]
                # Prefer Stage-2's authoritative default_branch over Stage-1's guess.
                branch = (
                    summary.get("default_branch")
                    or repo_info.get("branch")
                    or "main"
                )
                futures.append(ex.submit(
                    download_one_repo,
                    repo_key=repo_key,
                    owner=owner,
                    repo=repo,
                    branch=branch,
                    raw_root=dl_paths["raw"],
                    force=args.force,
                    timeout=timeout,
                ))

            for i, fut in enumerate(as_completed(futures), 1):
                res = fut.result()
                repo_results.append(res)
                append_jsonl(dl_paths["repo_log"], res)
                if res["status"] in ("ok", "cached"):
                    done.add(res["repo_key"])
                if i % 25 == 0 or i == len(futures):
                    save_done_set(dl_paths["checkpoint"], done, total=len(done))
                    print(f"[OK] {i}/{len(futures)} done={len(done)} last={res['repo_key']}({res['status']})", flush=True)

    save_done_set(dl_paths["checkpoint"], done, total=len(done))

    # Append filtered rows so the audit log is complete
    for sr in skipped_rows:
        append_jsonl(dl_paths["repo_log"], {**sr, "ts": now_ts()})

    # Build per-skill package views over the FULL repo_map (so skipped repos
    # show up in skill_status with their filter reason rather than silently
    # disappearing).
    repo_results_by_key = {r["repo_key"]: r for r in repo_results}
    for sr in skipped_rows:
        repo_results_by_key[sr["repo_key"]] = {
            "repo_key": sr["repo_key"],
            "status": sr["status"],
            "error": sr["status"],
        }

    # Map repo_key -> license summary for manifests + skill_status
    license_meta_by_key: Dict[str, Dict[str, Any]] = {
        rk: (md_index.get(rk) or md_index.get(rk.rstrip("@")) or {})
        for rk in repo_map.keys()
    }

    package_rows = create_package_views(
        repo_map=repo_map,
        dedup_meta=dedup_meta,
        license_meta_by_key=license_meta_by_key,
        raw_root=dl_paths["raw"],
        packages_root=dl_paths["packages"],
        repo_results_by_key=repo_results_by_key,
    )
    package_rows.sort(key=lambda x: x.get("skill_id") or "")

    # Persist bookkeeping
    repo_results.sort(key=lambda x: x["repo_key"])
    write_csv(
        dl_paths["repo_status"],
        repo_results,
        fieldnames=[
            "repo_key", "status", "owner", "repo", "branch", "url",
            "archive_path", "extract_parent", "extracted_top", "error", "elapsed_sec",
        ],
    )
    write_csv(
        dl_paths["skill_status"],
        package_rows,
        fieldnames=[
            "skill_id", "repo_key", "owner", "repo", "branch",
            "relative_path", "status", "package_dir", "has_skill_md",
            "license_spdx_id", "error",
        ],
    )
    # Also write the package_index in jsonl form (matches existing skills_download)
    ensure_dir(dl_paths["package_index"].parent)
    with dl_paths["package_index"].open("w", encoding="utf-8") as f:
        import json as _json
        for row in package_rows:
            f.write(_json.dumps(row, ensure_ascii=False) + "\n")

    print(f"\n[SUMMARY] processed={len(repo_results)} filtered={len(skipped_rows)} packages={len(package_rows)}")
    print(f"[SUMMARY] raw_repos:   {dl_paths['raw']}")
    print(f"[SUMMARY] packages:    {dl_paths['packages']}")
    print(f"[SUMMARY] repo_status: {dl_paths['repo_status']}")
    print(f"[SUMMARY] skill_status:{dl_paths['skill_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
