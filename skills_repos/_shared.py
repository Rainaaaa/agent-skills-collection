"""Shared utilities for the skills_repos pipeline (Stages 2 + 3).

Concerns kept here:
  - JSON / JSONL round-trips with atomic writes.
  - GitHub URL parsing and canonical repo-key construction.
  - Checkpoint primitives (a simple { done: [...] } set on disk).

All helpers are pure functions; the only external dependency is the
standard library so this module can be imported from a flat directory
without any sys.path manipulation.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def now_ts() -> int:
    return int(time.time())


def utc_date_str(ts: Optional[int] = None) -> str:
    if ts is None:
        ts = now_ts()
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------

def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# JSON / JSONL
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    """Atomic write via temp-file rename."""
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def append_jsonl(path: Path, item: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    ensure_dir(path.parent)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


# ---------------------------------------------------------------------------
# Checkpoint — { done: [...], total: N, updated_at }
# ---------------------------------------------------------------------------

def load_done_set(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    cp = load_json(path)
    return set(cp.get("done") or [])


def save_done_set(path: Path, done: Iterable[str], total: Optional[int] = None) -> None:
    done_list = sorted(set(done))
    save_json(path, {
        "updated_at": now_ts(),
        "total": total if total is not None else len(done_list),
        "done_count": len(done_list),
        "done": done_list,
    })


# ---------------------------------------------------------------------------
# GitHub URL parsing
# ---------------------------------------------------------------------------

_GITHUB_TREE_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/?#]+?)(?:\.git)?(?:/tree/([^/?#]+)(?:/(.+?))?)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)


def parse_github_url(url: Optional[str]) -> Optional[Dict[str, str]]:
    """Parse a GitHub URL into {owner, repo, branch, path}.

    Accepts:
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - https://github.com/owner/repo/tree/branch
      - https://github.com/owner/repo/tree/branch/some/path
      - http(s) variants and trailing slashes
    Returns None if the URL is not a recognizable GitHub repo URL.
    """
    if not url or not isinstance(url, str):
        return None
    m = _GITHUB_TREE_RE.match(url.strip())
    if not m:
        return None
    owner, repo, branch, path = m.groups()
    return {
        "owner": owner or "",
        "repo": repo or "",
        "branch": branch or "",
        "path": path or "",
    }


def repo_key_for(owner: str, repo: str, branch: str = "") -> str:
    """Canonical 'owner/repo@branch' key (branch may be empty)."""
    return f"{owner}/{repo}@{branch}" if branch else f"{owner}/{repo}@"


def split_repo_key(repo_key: str) -> Tuple[str, str, str]:
    """Inverse of repo_key_for; returns (owner, repo, branch)."""
    if "@" in repo_key:
        left, branch = repo_key.rsplit("@", 1)
    else:
        left, branch = repo_key, ""
    if "/" in left:
        owner, repo = left.split("/", 1)
    else:
        owner, repo = left, ""
    return owner, repo, branch


def safe_repo_dir_name(repo_key: str) -> str:
    """Filesystem-safe directory name for a repo_key."""
    owner, repo, branch = split_repo_key(repo_key)
    return f"{owner}__{repo}__{branch or 'HEAD'}"
