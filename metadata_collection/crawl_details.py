"""
Level 2 crawler for SkillsMP skill detail pages (async, multi-worker).

Consumes level1_dedup.json (produced by crawl_lists.py) and visits each
skillmp_link to extract detail-page fields not available in the API:

  description, forks, repository (from JSON-LD / anchor), run_in_manus_link,
  author (JSON-LD), dateModified (JSON-LD), skill_md_text, skill_md_html,
  canonical_url.

Outputs:
  - level2_details.jsonl : one record per skill (append-only)
  - level2_details_index.json : skillmp_link -> last-seen detail record
  - level2_request_log.jsonl : per-page audit
  - checkpoints/level2_checkpoint.json : set of completed skillmp_links

Run multiple times to resume — already-completed links are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from _shared import (
    append_jsonl,
    default_checkpoint as _default_checkpoint_shared,
    ensure_dir,
    load_json,
    mark_job_done as _mark_job_done_shared,
    now_ts,
    save_json,
)


# -----------------------------------------------------------------------------
# Job list = all skillmp_links from level1_dedup.json
# -----------------------------------------------------------------------------

def build_jobs_from_l1_dedup(dedup_file: Path, base_url: str) -> List[Dict[str, Any]]:
    if not dedup_file.exists():
        raise FileNotFoundError(
            f"Level-1 dedup file not found at {dedup_file}. Run crawl_lists.py first."
        )
    data = load_json(dedup_file)
    keys = data.get("keys", {})
    jobs: List[Dict[str, Any]] = []
    for skillmp_link, rec in keys.items():
        url = skillmp_link if skillmp_link.startswith("http") else base_url.rstrip("/") + skillmp_link
        jobs.append({
            "skillmp_link": skillmp_link,
            "url": url,
            "l1_hint": {
                "skill_name": rec.get("skill_name"),
                "repository": rec.get("repository"),
                "stars": rec.get("stars"),
                "updated_at": rec.get("updated_at"),
            },
        })
    return jobs


def job_key(job: Dict[str, Any]) -> str:
    return job["skillmp_link"]


# -----------------------------------------------------------------------------
# Checkpoint
# -----------------------------------------------------------------------------

def load_or_init_checkpoint(path: Path, jobs: List[Dict[str, Any]], reset: bool) -> Dict[str, Any]:
    if reset or not path.exists():
        cp = _default_checkpoint_shared(len(jobs))
        save_json(path, cp)
        return cp
    cp = load_json(path)
    if "done_job_keys" not in cp:
        cp = _default_checkpoint_shared(len(jobs))
        save_json(path, cp)
        return cp
    # Unlike L1, jobs_total can grow as L1 keeps running. Keep checkpoint
    # compatible; just update jobs_total.
    cp["jobs_total"] = len(jobs)
    return cp


def mark_job_done(checkpoint: Dict[str, Any], job: Dict[str, Any]) -> None:
    _mark_job_done_shared(checkpoint, job_key(job))


# -----------------------------------------------------------------------------
# In-page extraction
# -----------------------------------------------------------------------------

DETAIL_SCRIPT = r"""
(cfg) => {
    const sels = cfg.selectors;

    const readText = (sel) => {
        if (!sel) return null;
        const el = document.querySelector(sel);
        if (!el) return null;
        const t = (el.textContent || '').trim();
        return t || null;
    };
    const readAttr = (sel, attr) => {
        if (!sel || !attr) return null;
        const el = document.querySelector(sel);
        if (!el) return null;
        return el.getAttribute(attr);
    };
    const readMany = (sel, attr) => {
        if (!sel) return [];
        const out = [];
        for (const el of document.querySelectorAll(sel)) {
            const v = attr ? el.getAttribute(attr) : (el.textContent || '').trim();
            if (v) out.push(v);
        }
        return out;
    };

    const rd = (spec) => {
        if (!spec) return null;
        return spec.attr ? readAttr(spec.selector, spec.attr) : readText(spec.selector);
    };

    // Robust stat extraction: find a <span> whose trimmed text is exactly the
    // label (e.g. "stars:"), and return the text of its next element sibling.
    // The detail page has many colored spans (terminal nav, code imports),
    // so CSS-only `span.text-yellow-500 + span` is not reliable.
    const getAfterLabel = (labelText) => {
        const target = labelText.toLowerCase();
        const spans = document.querySelectorAll('span');
        for (const el of spans) {
            const t = (el.textContent || '').trim().toLowerCase();
            if (t === target) {
                const sib = el.nextElementSibling;
                if (sib) return (sib.textContent || '').trim() || null;
            }
        }
        return null;
    };

    // Inner HTML (for skill_md_html)
    const htmlOf = (sel) => {
        if (!sel) return null;
        const el = document.querySelector(sel);
        return el ? el.innerHTML : null;
    };

    // JSON-LD parsing: walk every ld+json script, merge its payloads
    const jsonlds = [];
    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
        try {
            const raw = s.textContent || '';
            if (!raw.trim()) continue;
            const obj = JSON.parse(raw);
            if (Array.isArray(obj)) obj.forEach(o => jsonlds.push(o));
            else jsonlds.push(obj);
        } catch (e) { /* ignore parse errors */ }
    }
    // Pull specific fields, searching across all LD payloads
    const pickLd = (key) => {
        for (const o of jsonlds) {
            if (o && o[key] != null) return o[key];
        }
        return null;
    };
    const authorVal = (() => {
        for (const o of jsonlds) {
            if (o && o.author) {
                if (typeof o.author === 'string') return o.author;
                if (typeof o.author === 'object') return o.author.name || null;
            }
        }
        return null;
    })();

    // All GitHub links found on the page, for repo fallback
    const githubLinks = readMany('a[href*="github.com/"]', 'href');

    return {
        skill_name:           readText(sels.skill_name ? sels.skill_name.selector : null),
        description_primary:  rd(sels.description_primary),
        stars:                getAfterLabel('stars:') || rd(sels.stars),
        forks:                getAfterLabel('forks:') || rd(sels.forks),
        updated_at_text:      getAfterLabel('updated:') || rd(sels.updated_at),
        canonical_url:        rd(sels.canonical_url),
        run_in_manus_link:    rd(sels.run_in_manus_link),
        repository_link:      rd(sels.repository_link),
        github_links:         githubLinks,
        skill_md_text:        readText(sels.skill_md_container ? sels.skill_md_container.selector : null),
        skill_md_html:        htmlOf(sels.skill_md_container ? sels.skill_md_container.selector : null),
        jsonld_raw:           jsonlds,
        jsonld_code_repository: pickLd('codeRepository'),
        jsonld_date_modified:  pickLd('dateModified'),
        jsonld_author_name:    authorVal,
        page_url:             window.location.href,
    };
}
"""


async def extract_detail(page, url: str, l2_cfg: Dict[str, Any], browser_cfg: Dict[str, Any]) -> Dict[str, Any]:
    nav_timeout = int(browser_cfg.get("navigation_timeout_ms", 45000))
    default_timeout = int(browser_cfg.get("default_timeout_ms", 15000))
    behavior = l2_cfg.get("page_behavior", {})
    wait_sel = behavior.get("wait_for_selector", "h1")
    post_wait_ms = int(behavior.get("post_load_wait_ms", 1500))
    max_hydration_waits = int(behavior.get("max_hydration_waits", 3))

    page.set_default_timeout(default_timeout)
    await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)

    # Progressive hydration: wait for the first signal (h1), then settle.
    try:
        await page.wait_for_selector(wait_sel, timeout=default_timeout)
    except Exception:
        pass

    # A couple of short waits so the SKILL.md container / JSON-LD hydrate.
    prev_len = -1
    for _ in range(max_hydration_waits):
        await page.wait_for_timeout(post_wait_ms)
        try:
            cur_len = await page.evaluate(
                "() => (document.body && document.body.innerText || '').length"
            )
        except Exception:
            cur_len = prev_len
        if cur_len == prev_len:
            break
        prev_len = cur_len

    result = await page.evaluate(DETAIL_SCRIPT, {"selectors": l2_cfg.get("selectors", {})})
    return result or {}


# -----------------------------------------------------------------------------
# Worker
# -----------------------------------------------------------------------------

async def worker_loop(
    worker_id: int,
    browser,
    browser_cfg: Dict[str, Any],
    l2_cfg: Dict[str, Any],
    queue: "asyncio.Queue[Optional[Dict[str, Any]]]",
    shared_state: Dict[str, Any],
    paths: Dict[str, Path],
    sleep_between: float,
    max_retries: int,
    retry_backoff: float,
) -> None:
    context = await browser.new_context(
        viewport=browser_cfg.get("viewport", {"width": 1440, "height": 900}),
        user_agent=browser_cfg.get("user_agent"),
    )
    page = await context.new_page()
    try:
        while True:
            job = await queue.get()
            if job is None:
                queue.task_done()
                break

            key = job_key(job)
            total = shared_state["checkpoint"]["jobs_total"]
            done = shared_state["checkpoint"]["done_count"]
            print(f"[W{worker_id}] RUN {done}/{total} {key}", flush=True)

            detail: Dict[str, Any] = {}
            last_err: Optional[str] = None
            for attempt in range(1, max_retries + 1):
                try:
                    detail = await extract_detail(page, job["url"], l2_cfg, browser_cfg)
                    last_err = None
                    break
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    print(f"[W{worker_id}] WARN {attempt}/{max_retries} {key}: {last_err}", flush=True)
                    append_jsonl(paths["request_log"], {
                        "ts": now_ts(), "worker": worker_id, "skillmp_link": key,
                        "attempt": attempt, "status": "error", "error": last_err,
                    })
                    if attempt < max_retries:
                        await asyncio.sleep(retry_backoff * attempt)
                        try:
                            await page.close()
                        except Exception:
                            pass
                        page = await context.new_page()

            if last_err and not detail:
                append_jsonl(paths["request_log"], {
                    "ts": now_ts(), "worker": worker_id, "skillmp_link": key,
                    "status": "giveup", "error": last_err,
                })
                queue.task_done()
                continue

            record = {
                "ts": now_ts(),
                "worker": worker_id,
                "skillmp_link": key,
                "url": job["url"],
                "l1_hint": job.get("l1_hint"),
                **detail,
            }
            append_jsonl(paths["details"], record)
            shared_state["index"][key] = {k: record.get(k) for k in (
                "ts", "skill_name", "description_primary",
                "jsonld_code_repository", "jsonld_author_name", "jsonld_date_modified",
                "forks", "stars", "canonical_url", "run_in_manus_link",
            )}
            save_json(paths["index"], shared_state["index"])

            mark_job_done(shared_state["checkpoint"], job)
            save_json(paths["checkpoint"], shared_state["checkpoint"])

            append_jsonl(paths["request_log"], {
                "ts": now_ts(), "worker": worker_id, "skillmp_link": key,
                "status": "ok",
                "has_skill_md": bool(record.get("skill_md_text")),
                "has_jsonld": bool(record.get("jsonld_raw")),
            })
            has_md = "Y" if record.get("skill_md_text") else "-"
            has_ld = "Y" if record.get("jsonld_raw") else "-"
            print(
                f"[W{worker_id}] OK  {key}  md={has_md} ld={has_ld}",
                flush=True,
            )

            await asyncio.sleep(sleep_between * random.uniform(0.7, 1.3))
            queue.task_done()
    finally:
        try:
            await page.close()
        except Exception:
            pass
        try:
            await context.close()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Path + arg normalization (mirrors crawl_lists.py layout)
# -----------------------------------------------------------------------------

def normalize_paths(runtime_cfg: Dict[str, Any], input_override: Optional[str], output_override: Optional[str]) -> Dict[str, Any]:
    cfg = dict(runtime_cfg)
    if input_override:
        cfg["input_dir"] = input_override
    if output_override:
        cfg["output_dir"] = output_override

    input_dir = Path(cfg.get("input_dir", "./input")).resolve()
    output_dir = Path(cfg.get("output_dir", "./output")).resolve()
    cfg["input_dir"] = str(input_dir)
    cfg["output_dir"] = str(output_dir)

    l1 = dict(cfg.get("level1", {}))
    l1_dedup = Path(l1.get("dedup_file", output_dir / "level1_dedup.json")).resolve()
    cfg["level1_dedup_file"] = str(l1_dedup)

    l2 = dict(cfg.get("level2", {}))
    for key, default in [
        ("details_file", output_dir / "level2_details.jsonl"),
        ("dedup_file", output_dir / "level2_details_index.json"),
        ("request_log_file", output_dir / "level2_request_log.jsonl"),
        ("checkpoint_file", output_dir / "checkpoints" / "level2_checkpoint.json"),
    ]:
        l2[key] = str(Path(l2.get(key, default)).resolve())
    cfg["level2"] = l2

    cfg["checkpoint_dir"] = str(Path(cfg.get("checkpoint_dir", output_dir / "checkpoints")).resolve())
    cfg["log_dir"] = str(Path(cfg.get("log_dir", output_dir / "logs")).resolve())
    return cfg


async def run_crawl(args: argparse.Namespace) -> int:
    jobs_cfg = load_json(Path(args.jobs_config))
    runtime_cfg = normalize_paths(load_json(Path(args.runtime_config)), args.input_dir, args.output_dir)

    output_dir = Path(runtime_cfg["output_dir"])
    ensure_dir(output_dir)
    ensure_dir(Path(runtime_cfg["checkpoint_dir"]))
    ensure_dir(Path(runtime_cfg["log_dir"]))

    paths = {
        "details":     Path(runtime_cfg["level2"]["details_file"]),
        "index":       Path(runtime_cfg["level2"]["dedup_file"]),
        "request_log": Path(runtime_cfg["level2"]["request_log_file"]),
        "checkpoint":  Path(runtime_cfg["level2"]["checkpoint_file"]),
    }
    l1_dedup_file = Path(runtime_cfg["level1_dedup_file"])
    base_url = jobs_cfg["base_url"].rstrip("/")

    try:
        jobs = build_jobs_from_l1_dedup(l1_dedup_file, base_url)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    if not jobs:
        print("[ERROR] level1_dedup.json contains no skillmp_links.", file=sys.stderr)
        return 1

    try:
        checkpoint = load_or_init_checkpoint(paths["checkpoint"], jobs, args.reset_checkpoint)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    if paths["index"].exists():
        index = load_json(paths["index"])
    else:
        index = {}

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "[ERROR] playwright is not installed. Run:\n"
            "    pip install playwright\n"
            "    python -m playwright install chromium",
            file=sys.stderr,
        )
        return 2

    done_keys = set(checkpoint.get("done_job_keys", []))
    pending = [j for j in jobs if job_key(j) not in done_keys]
    print(f"[INFO] jobs total={len(jobs)} done={len(done_keys)} pending={len(pending)} workers={args.workers}", flush=True)
    if args.max_jobs is not None:
        pending = pending[: args.max_jobs]
        print(f"[INFO] --max_jobs={args.max_jobs} truncates pending to {len(pending)}", flush=True)

    if not pending:
        checkpoint["done"] = True
        save_json(paths["checkpoint"], checkpoint)
        print("[DONE] nothing to do.")
        return 0

    browser_cfg = runtime_cfg.get("browser", {})
    headless = False if args.headful else bool(browser_cfg.get("headless", True))
    sleep_between = float(runtime_cfg.get("request_settings", {}).get("sleep_between_jobs_s", 1.0))
    max_retries = int(runtime_cfg.get("request_settings", {}).get("max_retries_per_job", 3))
    retry_backoff = float(runtime_cfg.get("request_settings", {}).get("retry_backoff_s", 3.0))

    queue: asyncio.Queue = asyncio.Queue()
    shuffled = pending[:]
    random.shuffle(shuffled)
    for j in shuffled:
        queue.put_nowait(j)
    for _ in range(args.workers):
        queue.put_nowait(None)

    shared_state = {"checkpoint": checkpoint, "index": index}
    l2_cfg = jobs_cfg.get("level2", {})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            workers = [
                asyncio.create_task(worker_loop(
                    worker_id=i + 1,
                    browser=browser,
                    browser_cfg=browser_cfg,
                    l2_cfg=l2_cfg,
                    queue=queue,
                    shared_state=shared_state,
                    paths=paths,
                    sleep_between=sleep_between,
                    max_retries=max_retries,
                    retry_backoff=retry_backoff,
                ))
                for i in range(args.workers)
            ]
            await queue.join()
            for w in workers:
                try:
                    await w
                except Exception as e:
                    print(f"[ERROR] worker raised: {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            await browser.close()

    if checkpoint["done_count"] >= checkpoint["jobs_total"]:
        checkpoint["done"] = True
        save_json(paths["checkpoint"], checkpoint)
        print("[DONE] all skills processed.", flush=True)

    print(f"[SUMMARY] done={checkpoint['done_count']}/{checkpoint['jobs_total']}", flush=True)
    print(f"[SUMMARY] details_file={paths['details']}", flush=True)
    print(f"[SUMMARY] index_file={paths['index']}", flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SkillsMP Level-2 detail-page crawler (async Playwright).")
    p.add_argument("--jobs_config", type=str, default="crawler_jobs.json")
    p.add_argument("--runtime_config", type=str, default="runtime_config.json")
    p.add_argument("--input_dir", type=str, default=None)
    p.add_argument("--output_dir", type=str, default=None)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--reset_checkpoint", action="store_true")
    p.add_argument("--max_jobs", type=int, default=None)
    p.add_argument("--headful", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run_crawl(args))
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted. Checkpoint preserved; rerun to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
