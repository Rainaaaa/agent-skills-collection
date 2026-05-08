"""Shared IO + checkpoint helpers for the SkillsMP label crawlers.

`crawl_lists.py`, `crawl_details.py`, and `merge_metadata.py` all need the
same json/jsonl round-trips, atomic-write, ensure-dir, time helpers, and
checkpoint primitives. They used to each carry their own copy. This module
is the single source of truth for them.

Kept intentionally small and dependency-free so it can be imported from a
flat directory layout without sys.path manipulation: every script in this
directory just does `from _shared import ...`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional


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
    """Atomic write: serialize to <path>.tmp then rename onto <path>."""
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
# Checkpoint shape used by both L1 and L2:
# {created_at, updated_at, jobs_total, done_count, done_job_keys: [...], done}
# ---------------------------------------------------------------------------

def default_checkpoint(jobs_total: int) -> Dict[str, Any]:
    now = now_ts()
    return {
        "created_at": now,
        "updated_at": now,
        "jobs_total": jobs_total,
        "done_count": 0,
        "done_job_keys": [],
        "done": False,
    }


def mark_job_done(checkpoint: Dict[str, Any], key: str) -> None:
    done_set = checkpoint.setdefault("done_job_keys", [])
    if key not in done_set:
        done_set.append(key)
    checkpoint["done_count"] = len(done_set)
    checkpoint["updated_at"] = now_ts()
