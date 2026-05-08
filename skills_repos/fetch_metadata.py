#!/usr/bin/env python3
"""Stage 2 — fetch GitHub repo metadata + license for unique repos.

For every (owner, repo) sourced from a `repo_map.json` (Stage 1 output) or a
plain list of GitHub URLs, this script calls:

  GET /repos/{owner}/{repo}                  → license, default_branch, …
  GET /repos/{owner}/{repo}/license          → SPDX id + license body (optional)

Outputs (all under runtime_config.json `output_dir`, default `./output`):

  github_metadata.jsonl              append-only, one record per fetch
  github_metadata_index.json         repo_key → latest summary (license, branch, stars, …)
  fetch_metadata_request_log.jsonl   per-request audit log
  checkpoints/fetch_metadata_checkpoint.json
                                     set of repo_keys that have been fetched

The fetch is resumable: previously-fetched repo_keys are skipped unless
`--force` is passed. GitHub PATs from `tokens.json` are rotated round-robin
to extend the per-token rate limit; on 403/429 the token is taken out of
rotation until its `X-RateLimit-Reset` window passes, with safe exit if no
token is usable.

Inputs are loosely coupled — pick one:

  --repo_map   /path/to/repo_map.json     (from skills_labels_collection/output)
  --repos_file /path/to/repos.txt         (one GitHub URL per line)
  --metadata_jsonl /path/to/skillsmp_metadata.jsonl
                                          (uses each row's `githubUrl`)

If multiple are given, their unique (owner, repo) pairs are unioned.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from _shared import (
    append_jsonl,
    ensure_dir,
    iter_jsonl,
    load_done_set,
    load_json,
    now_ts,
    parse_github_url,
    repo_key_for,
    save_done_set,
    save_json,
    split_repo_key,
)


GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Token pool — extends per-token 5000/hr rate limit by round-robin rotation
# ---------------------------------------------------------------------------

class TokenPool:
    """Rotates among GitHub PATs and tracks per-token rate-limit reset times.

    A token is `usable` when:
      - its remaining count is unknown (never used yet), OR
      - its remaining count > 0, OR
      - its reset window has already passed.
    """

    def __init__(self, tokens: List[Dict[str, str]]):
        self._tokens = tokens
        # state per token: {remaining, reset_ts, last_status}
        self._state: List[Dict[str, Any]] = [
            {"remaining": None, "reset_ts": 0, "last_status": None} for _ in tokens
        ]
        self._idx = 0

    @property
    def empty(self) -> bool:
        return not self._tokens

    def _is_usable(self, i: int) -> bool:
        st = self._state[i]
        if st["remaining"] is None:
            return True
        if st["remaining"] > 0:
            return True
        return st["reset_ts"] <= now_ts()

    def pick(self) -> Optional[Tuple[int, Dict[str, str]]]:
        """Return (index, token) for the next usable token, or None."""
        n = len(self._tokens)
        for off in range(n):
            i = (self._idx + off) % n
            if self._is_usable(i):
                self._idx = (i + 1) % n
                return i, self._tokens[i]
        return None

    def update_from_headers(self, idx: int, headers: Dict[str, str], status: int) -> None:
        st = self._state[idx]
        st["last_status"] = status
        try:
            remaining = headers.get("X-RateLimit-Remaining")
            reset = headers.get("X-RateLimit-Reset")
            if remaining is not None:
                st["remaining"] = int(remaining)
            if reset is not None:
                st["reset_ts"] = int(reset)
        except (TypeError, ValueError):
            pass

    def soonest_reset(self) -> Optional[int]:
        ts = [s["reset_ts"] for s in self._state if s["reset_ts"]]
        return min(ts) if ts else None


def load_tokens(path: Optional[Path]) -> List[Dict[str, str]]:
    """Load GitHub PATs from tokens.json. Returns [] if no file or no tokens.

    Format:
      {
        "active_account": "account_1",
        "accounts": {
          "account_1": {"github_pat": "ghp_…", "note": "personal"},
          "account_2": {"github_pat": "ghp_…", "note": "team"}
        }
      }
    """
    if not path or not path.exists():
        return []
    data = load_json(path)
    accounts = data.get("accounts") or {}
    out: List[Dict[str, str]] = []
    for name, acct in accounts.items():
        pat = acct.get("github_pat") or acct.get("api_key")
        if pat:
            out.append({"name": name, "pat": pat})
    return out


# ---------------------------------------------------------------------------
# HTTP — single-call helpers with rotating token pool
# ---------------------------------------------------------------------------

def _do_request(
    url: str,
    pool: TokenPool,
    timeout: int,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, str], Optional[bytes], Optional[str]]:
    """Issue a GET request using a rotating token. Returns
    (status, response_headers, body_bytes, account_name_used).

    Body is None for 304/403/404 etc. so callers can branch cleanly.
    """
    chosen = pool.pick()
    headers: Dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "agent-skills-collection/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if extra_headers:
        headers.update(extra_headers)
    account_name = None
    if chosen is not None:
        idx, tok = chosen
        headers["Authorization"] = f"Bearer {tok['pat']}"
        account_name = tok.get("name")
    else:
        idx = -1

    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = resp.status
            resp_headers = {k: v for k, v in resp.headers.items()}
            body = resp.read()
            if idx >= 0:
                pool.update_from_headers(idx, resp_headers, status)
            return status, resp_headers, body, account_name
    except HTTPError as e:
        resp_headers = {k: v for k, v in (e.headers.items() if e.headers else [])}
        if idx >= 0:
            pool.update_from_headers(idx, resp_headers, e.code)
        return e.code, resp_headers, None, account_name


# ---------------------------------------------------------------------------
# Repo enumeration from any of the supported inputs
# ---------------------------------------------------------------------------

def repos_from_repo_map(path: Path) -> Iterable[Tuple[str, str]]:
    if not path.exists():
        return
    data = load_json(path)
    for repo_key, info in data.items():
        owner = info.get("owner") or ""
        repo = info.get("repo") or ""
        if not owner or not repo:
            owner, repo, _ = split_repo_key(repo_key)
        if owner and repo:
            yield owner, repo


def repos_from_url_file(path: Path) -> Iterable[Tuple[str, str]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parsed = parse_github_url(line)
            if parsed and parsed["owner"] and parsed["repo"]:
                yield parsed["owner"], parsed["repo"]


def repos_from_metadata_jsonl(path: Path) -> Iterable[Tuple[str, str]]:
    for row in iter_jsonl(path):
        url = row.get("githubUrl") or row.get("repo_url")
        if not url:
            continue
        parsed = parse_github_url(url)
        if parsed and parsed["owner"] and parsed["repo"]:
            yield parsed["owner"], parsed["repo"]


def collect_unique_repos(args: argparse.Namespace) -> List[Tuple[str, str]]:
    seen: Set[Tuple[str, str]] = set()
    out: List[Tuple[str, str]] = []
    sources: List[Iterable[Tuple[str, str]]] = []

    if args.repo_map:
        sources.append(repos_from_repo_map(Path(args.repo_map)))
    if args.repos_file:
        sources.append(repos_from_url_file(Path(args.repos_file)))
    if args.metadata_jsonl:
        sources.append(repos_from_metadata_jsonl(Path(args.metadata_jsonl)))

    for src in sources:
        for owner, repo in src:
            key = (owner.lower(), repo.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append((owner, repo))
    return out


# ---------------------------------------------------------------------------
# Per-repo fetch
# ---------------------------------------------------------------------------

def _summarize_repo_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Pick the fields most useful for downstream filtering + downloading."""
    license_obj = payload.get("license") or {}
    owner_obj = payload.get("owner") or {}
    return {
        "id":                payload.get("id"),
        "node_id":           payload.get("node_id"),
        "name":              payload.get("name"),
        "full_name":         payload.get("full_name"),
        "owner":             owner_obj.get("login"),
        "owner_type":        owner_obj.get("type"),
        "html_url":          payload.get("html_url"),
        "description":       payload.get("description"),
        "fork":              payload.get("fork"),
        "archived":          payload.get("archived"),
        "disabled":          payload.get("disabled"),
        "private":           payload.get("private"),
        "visibility":        payload.get("visibility"),
        "default_branch":    payload.get("default_branch"),
        "size_kb":           payload.get("size"),
        "language":          payload.get("language"),
        "topics":            payload.get("topics"),
        "stargazers_count":  payload.get("stargazers_count"),
        "watchers_count":    payload.get("watchers_count"),
        "forks_count":       payload.get("forks_count"),
        "open_issues_count": payload.get("open_issues_count"),
        "subscribers_count": payload.get("subscribers_count"),
        "created_at":        payload.get("created_at"),
        "updated_at":        payload.get("updated_at"),
        "pushed_at":         payload.get("pushed_at"),
        "license_key":       license_obj.get("key"),
        "license_name":      license_obj.get("name"),
        "license_spdx_id":   license_obj.get("spdx_id"),
        "license_url":       license_obj.get("url"),
        "homepage":          payload.get("homepage"),
        "has_wiki":          payload.get("has_wiki"),
        "has_issues":        payload.get("has_issues"),
        "has_pages":         payload.get("has_pages"),
    }


def fetch_repo_metadata(
    owner: str,
    repo: str,
    pool: TokenPool,
    timeout: int,
    with_license_text: bool,
) -> Dict[str, Any]:
    """Fetch /repos and (optionally) /license. Returns a record with the raw
    payload(s) plus a flat 'summary' subset for downstream consumers."""
    base = f"{GITHUB_API}/repos/{owner}/{repo}"
    record: Dict[str, Any] = {
        "ts": now_ts(),
        "owner": owner,
        "repo": repo,
        "repo_lookup_url": base,
        "status": None,
        "error": None,
        "account_used": None,
        "payload": None,
        "license_payload": None,
        "summary": None,
    }

    status, headers, body, account = _do_request(base, pool, timeout)
    record["status"] = status
    record["account_used"] = account

    if status == 200 and body is not None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            record["error"] = f"parse error: {e}"
            return record
        record["payload"] = payload
        record["summary"] = _summarize_repo_payload(payload)
    elif status in (404, 410, 451):
        record["error"] = f"unavailable (HTTP {status})"
        return record
    elif status in (401, 403, 429):
        record["error"] = f"rate-limited or forbidden (HTTP {status})"
        return record
    else:
        record["error"] = f"HTTP {status}"
        return record

    if with_license_text:
        lic_url = f"{base}/license"
        l_status, _l_headers, l_body, _l_account = _do_request(lic_url, pool, timeout)
        if l_status == 200 and l_body is not None:
            try:
                lic_payload = json.loads(l_body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                lic_payload = None
            if lic_payload:
                content_b64 = lic_payload.get("content") or ""
                encoding = (lic_payload.get("encoding") or "").lower()
                lic_text = None
                if content_b64 and encoding == "base64":
                    try:
                        lic_text = base64.b64decode(content_b64).decode("utf-8", "replace")
                    except Exception:
                        lic_text = None
                lic_payload["_decoded_text"] = lic_text
                record["license_payload"] = lic_payload
        else:
            # 404 here is normal: GitHub couldn't auto-detect a license file.
            record["license_payload"] = {"_status": l_status}

    return record


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def normalize_paths(runtime_cfg: Dict[str, Any], output_override: Optional[str]) -> Dict[str, Any]:
    cfg = dict(runtime_cfg)
    if output_override:
        cfg["output_dir"] = output_override
    output_dir = Path(cfg.get("output_dir", "./output")).resolve()
    cfg["output_dir"] = str(output_dir)

    fm = dict(cfg.get("fetch_metadata", {}))
    fm["metadata_file"]    = str(Path(fm.get("metadata_file",    output_dir / "github_metadata.jsonl")).resolve())
    fm["index_file"]       = str(Path(fm.get("index_file",       output_dir / "github_metadata_index.json")).resolve())
    fm["request_log_file"] = str(Path(fm.get("request_log_file", output_dir / "fetch_metadata_request_log.jsonl")).resolve())
    fm["checkpoint_file"]  = str(Path(fm.get("checkpoint_file",  output_dir / "checkpoints" / "fetch_metadata_checkpoint.json")).resolve())
    cfg["fetch_metadata"] = fm
    cfg["checkpoint_dir"] = str(Path(cfg.get("checkpoint_dir", output_dir / "checkpoints")).resolve())
    cfg["log_dir"]        = str(Path(cfg.get("log_dir",        output_dir / "logs")).resolve())
    return cfg


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 2 — fetch GitHub repo metadata + license.")
    p.add_argument("--runtime_config", type=str, default="runtime_config.json")
    p.add_argument("--tokens_file",    type=str, default="tokens.json")
    p.add_argument("--output_dir",     type=str, default=None)

    # Inputs (any combination — unique repos are unioned)
    p.add_argument("--repo_map",       type=str, default=None,
                   help="repo_map.json from skills_labels_collection/merge_metadata.py.")
    p.add_argument("--repos_file",     type=str, default=None,
                   help="Plain text file with one GitHub URL per line.")
    p.add_argument("--metadata_jsonl", type=str, default=None,
                   help="JSONL with githubUrl / repo_url per row.")

    p.add_argument("--with_license_text", action="store_true",
                   help="Also call /repos/{o}/{r}/license to download SPDX-detected license body.")
    p.add_argument("--max_repos",         type=int, default=None,
                   help="Process at most N repos this run (smoke test).")
    p.add_argument("--force",             action="store_true",
                   help="Refetch repos already in the checkpoint.")
    p.add_argument("--reset_checkpoint",  action="store_true",
                   help="Drop the existing checkpoint and start over.")
    p.add_argument("--sleep_between_s",   type=float, default=None,
                   help="Override sleep between requests (seconds).")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    runtime_cfg = load_json(Path(args.runtime_config))
    runtime_cfg = normalize_paths(runtime_cfg, args.output_dir)

    output_dir = Path(runtime_cfg["output_dir"])
    ensure_dir(output_dir)
    ensure_dir(Path(runtime_cfg["checkpoint_dir"]))
    ensure_dir(Path(runtime_cfg["log_dir"]))

    fm_paths = {
        "metadata":   Path(runtime_cfg["fetch_metadata"]["metadata_file"]),
        "index":      Path(runtime_cfg["fetch_metadata"]["index_file"]),
        "log":        Path(runtime_cfg["fetch_metadata"]["request_log_file"]),
        "checkpoint": Path(runtime_cfg["fetch_metadata"]["checkpoint_file"]),
    }

    req_cfg = runtime_cfg.get("request_settings", {}) or {}
    timeout = int(req_cfg.get("timeout_seconds", 30))
    sleep_s = float(args.sleep_between_s if args.sleep_between_s is not None
                    else req_cfg.get("sleep_between_requests_s", 0.5))

    # Token pool
    tokens = load_tokens(Path(args.tokens_file))
    pool = TokenPool(tokens)
    if pool.empty:
        print("[WARN] No GitHub PATs loaded; will run unauthenticated (60 req/hour).")
    else:
        print(f"[INFO] Loaded {len(tokens)} GitHub token(s).")

    # Inputs
    repos = collect_unique_repos(args)
    if not repos:
        print("[ERROR] No input repos. Pass --repo_map / --repos_file / --metadata_jsonl.", file=sys.stderr)
        return 1
    print(f"[INFO] {len(repos)} unique repos selected from inputs.")

    if args.max_repos:
        repos = repos[: args.max_repos]
        print(f"[INFO] --max_repos={args.max_repos}; truncated to {len(repos)}.")

    # Checkpoint
    if args.reset_checkpoint and fm_paths["checkpoint"].exists():
        fm_paths["checkpoint"].unlink()
    done = load_done_set(fm_paths["checkpoint"])
    if not args.force and done:
        repos = [(o, r) for (o, r) in repos if repo_key_for(o, r) not in done]
        print(f"[INFO] Skipping {len(done)} repos already in checkpoint; pending={len(repos)}.")

    # Existing index → upsert
    index: Dict[str, Any] = {}
    if fm_paths["index"].exists():
        try:
            index = load_json(fm_paths["index"])
        except Exception:
            index = {}

    # Drive
    fetched_ok = 0
    fetched_err = 0
    for i, (owner, repo) in enumerate(repos, 1):
        # If every token has burned its quota, exit cleanly so we can resume.
        if not pool.empty and pool.pick() is None:
            soonest = pool.soonest_reset() or 0
            wait_for = max(0, soonest - now_ts())
            print(
                f"[STOP] All tokens are rate-limited; soonest reset in {wait_for}s. "
                "Re-run later to continue.",
                file=sys.stderr,
            )
            break

        rk = repo_key_for(owner, repo)
        record = fetch_repo_metadata(
            owner=owner,
            repo=repo,
            pool=pool,
            timeout=timeout,
            with_license_text=args.with_license_text,
        )
        record["repo_key"] = rk

        append_jsonl(fm_paths["metadata"], record)
        append_jsonl(fm_paths["log"], {
            "ts": record["ts"],
            "owner": owner,
            "repo": repo,
            "status": record["status"],
            "error": record["error"],
            "account": record["account_used"],
            "license_spdx_id": (record.get("summary") or {}).get("license_spdx_id"),
            "default_branch":  (record.get("summary") or {}).get("default_branch"),
        })

        summary = record.get("summary")
        if summary:
            index[rk] = {
                "ts": record["ts"],
                "status": record["status"],
                "owner": owner,
                "repo": repo,
                **summary,
            }

        if record["error"] is None:
            fetched_ok += 1
            done.add(rk)
        else:
            fetched_err += 1
            # Mark `done` only for terminal statuses so transient failures retry.
            if record["status"] in (404, 410, 451, 200):
                done.add(rk)

        if i % 25 == 0 or i == len(repos):
            save_json(fm_paths["index"], index)
            save_done_set(fm_paths["checkpoint"], done, total=len(done))
            print(
                f"[OK] {i}/{len(repos)} ok={fetched_ok} err={fetched_err} "
                f"index_size={len(index)}",
                flush=True,
            )

        if sleep_s > 0:
            time.sleep(sleep_s)

    # Final flush
    save_json(fm_paths["index"], index)
    save_done_set(fm_paths["checkpoint"], done, total=len(done))

    print(
        f"[SUMMARY] processed={fetched_ok + fetched_err} ok={fetched_ok} err={fetched_err} "
        f"index_size={len(index)}"
    )
    print(f"[SUMMARY] metadata: {fm_paths['metadata']}")
    print(f"[SUMMARY] index:    {fm_paths['index']}")
    print(f"[SUMMARY] log:      {fm_paths['log']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
