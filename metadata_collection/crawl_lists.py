"""
Level 1 crawler for SkillsMP list pages (async, multi-worker).

Walks leaf occupation pages (/occupations/{soc_lvl_3}) and leaf category pages
(/categories/{subcategory}) with infinite-scroll hydration, extracts skill
cards, and emits:

  - level1_cards.jsonl       : append-only observation log
                               (one line per skill × source × sort_mode)
  - level1_dedup.json        : skill-level rollup with merged label sets
  - level1_request_log.jsonl : per-page audit log
  - checkpoints/level1_checkpoint.json : set of completed job keys

Concurrency: N worker coroutines share one Chromium browser, each driving
its own page. Asyncio is single-threaded — all shared-state updates
(`dedup`, `checkpoint["done_job_keys"]`) happen between awaits and are
race-free without explicit locks.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import random
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
# Label enumeration
# -----------------------------------------------------------------------------

def read_occupation_slugs(input_dir: Path, label_file_rel: str, label_column: str) -> List[str]:
    path = (input_dir / Path(label_file_rel).name).resolve()
    if not path.exists():
        path = Path(label_file_rel).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Occupation label file not found: {label_file_rel}")
    slugs: List[str] = []
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            slug = (row.get(label_column) or "").strip()
            if slug:
                slugs.append(slug)
    return slugs


def read_category_leaf_slugs(input_dir: Path, label_file_rel: str) -> List[Tuple[str, str]]:
    path = (input_dir / Path(label_file_rel).name).resolve()
    if not path.exists():
        path = Path(label_file_rel).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Category label file not found: {label_file_rel}")
    data = load_json(path)
    leaves: List[Tuple[str, str]] = []
    for top, children in data.items():
        if isinstance(children, list):
            for leaf in children:
                if isinstance(leaf, str) and leaf:
                    leaves.append((top, leaf))
        else:
            leaves.append((top, top))
    return leaves


def build_jobs(
    jobs_cfg: Dict[str, Any],
    input_dir: Path,
    source_filter: str,
    sort_modes_override: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    base_url = jobs_cfg["base_url"].rstrip("/")
    sort_modes: List[str] = (
        list(sort_modes_override)
        if sort_modes_override
        else list(jobs_cfg.get("sort_modes", ["stars"]))
    )
    sources_cfg: Dict[str, Any] = jobs_cfg.get("sources", {})
    jobs: List[Dict[str, Any]] = []

    occ_cfg = sources_cfg.get("occupation", {})
    if occ_cfg.get("enabled", False) and source_filter in ("all", "occupation"):
        slugs = read_occupation_slugs(
            input_dir=input_dir,
            label_file_rel=occ_cfg["label_file"],
            label_column=occ_cfg.get("label_column", "soc_lvl_3"),
        )
        url_tpl = occ_cfg["url_template"]
        for slug in slugs:
            for sort_mode in sort_modes:
                jobs.append({
                    "source_type": "occupation",
                    "source_label": slug,
                    "parent_label": None,
                    "sort_mode": sort_mode,
                    "url": url_tpl.format(base_url=base_url, slug=slug),
                })

    cat_cfg = sources_cfg.get("category", {})
    if cat_cfg.get("enabled", False) and source_filter in ("all", "category"):
        leaves = read_category_leaf_slugs(
            input_dir=input_dir,
            label_file_rel=cat_cfg["label_file"],
        )
        url_tpl = cat_cfg["url_template"]
        for top, leaf in leaves:
            for sort_mode in sort_modes:
                jobs.append({
                    "source_type": "category",
                    "source_label": leaf,
                    "parent_label": top,
                    "sort_mode": sort_mode,
                    "url": url_tpl.format(base_url=base_url, slug=leaf),
                })

    return jobs


def job_key(job: Dict[str, Any]) -> str:
    return f"{job['source_type']}::{job['source_label']}::{job['sort_mode']}"


# -----------------------------------------------------------------------------
# Checkpoint (done_job_keys set — safe with out-of-order completion)
# -----------------------------------------------------------------------------

def load_or_init_checkpoint(path: Path, jobs: List[Dict[str, Any]], reset: bool) -> Dict[str, Any]:
    if reset or not path.exists():
        cp = _default_checkpoint_shared(len(jobs))
        save_json(path, cp)
        return cp
    cp = load_json(path)
    # Back-compat: older "current_job_index" schema -> reset
    if "done_job_keys" not in cp:
        print(f"[INFO] Migrating old checkpoint schema; resetting done_job_keys.")
        cp = _default_checkpoint_shared(len(jobs))
        save_json(path, cp)
        return cp
    if cp.get("jobs_total") != len(jobs):
        raise ValueError(
            f"Checkpoint jobs_total={cp.get('jobs_total')} does not match current jobs_total={len(jobs)}. "
            "Use --reset_checkpoint if the label set or sort modes changed."
        )
    return cp


def mark_job_done(checkpoint: Dict[str, Any], job: Dict[str, Any]) -> None:
    _mark_job_done_shared(checkpoint, job_key(job))


# -----------------------------------------------------------------------------
# Dedup rollup
# -----------------------------------------------------------------------------

def add_label_to_dedup(
    dedup: Dict[str, Any],
    skillmp_link: str,
    card: Dict[str, Any],
    source_type: str,
    source_label: str,
    parent_label: Optional[str],
) -> bool:
    keys = dedup.setdefault("keys", {})
    entry = keys.get(skillmp_link)
    is_new = entry is None

    if entry is None:
        entry = {
            "first_seen_at": now_ts(),
            "skillmp_link": skillmp_link,
            "skill_name": card.get("skill_name"),
            "repository": card.get("repository"),
            "description": card.get("description"),
            "stars": card.get("stars"),
            "updated_at": card.get("updated_at"),
            "occupations": [],
            "categories": [],
            "category_parents": [],
        }
        keys[skillmp_link] = entry

    for field in ("skill_name", "repository", "description", "stars", "updated_at"):
        if not entry.get(field) and card.get(field):
            entry[field] = card[field]

    if source_type == "occupation":
        if source_label not in entry["occupations"]:
            entry["occupations"].append(source_label)
    elif source_type == "category":
        if source_label not in entry["categories"]:
            entry["categories"].append(source_label)
        if parent_label and parent_label not in entry["category_parents"]:
            entry["category_parents"].append(parent_label)

    dedup["count"] = len(keys)
    dedup["updated_at"] = now_ts()
    return is_new


# -----------------------------------------------------------------------------
# Playwright — in-page scripts and helpers
# -----------------------------------------------------------------------------

PAGE_SCRIPT = r"""
(selectors) => {
    const anchors = document.querySelectorAll(selectors.card_anchor);
    const out = [];
    const fields = selectors.fields_relative_to_anchor;

    const readField = (root, spec) => {
        if (!spec || !spec.selector) return null;
        const el = root.querySelector(spec.selector);
        if (!el) return null;
        let v = spec.attr ? el.getAttribute(spec.attr) : (el.textContent || '');
        if (v == null) return null;
        v = v.trim();
        if (spec.normalize === 'trim_quotes') {
            v = v.replace(/^["'\s]+|["'\s]+$/g, '');
        }
        return v || null;
    };

    for (const a of anchors) {
        if (selectors.skeleton_exclude && a.querySelector(selectors.skeleton_exclude)) continue;
        const href = a.getAttribute('href');
        if (!href) continue;

        const skill_name  = readField(a, fields.skill_name);
        const repository  = readField(a, fields.repository);
        const description = readField(a, fields.description);
        const stars       = readField(a, fields.stars);
        const updated_at  = readField(a, fields.updated_at);
        if (!skill_name) continue;

        out.push({
            skill_name, repository, description, stars, updated_at,
            skillmp_link: href,
        });
    }
    return out;
}
"""


EOF_CHECK_SCRIPT = r"""
(cfg) => {
    if (!cfg || !cfg.needle) return false;
    const needle = String(cfg.needle).toLowerCase();
    const text = (document.body && document.body.innerText || '').toLowerCase();
    return text.includes(needle);
}
"""


async def eof_marker_visible(page, eof_cfg: Dict[str, Any]) -> bool:
    needle = (eof_cfg or {}).get("match_text")
    if not needle:
        return False
    try:
        return bool(await page.evaluate(EOF_CHECK_SCRIPT, {"needle": needle}))
    except Exception:
        return False


async def click_sort_button(page, sort_buttons: Dict[str, str], sort_mode: str, timeout_ms: int) -> None:
    sel = sort_buttons.get(sort_mode)
    if not sel:
        return
    try:
        await page.wait_for_selector(sel, timeout=timeout_ms)
        await page.click(sel, timeout=timeout_ms)
    except Exception:
        pass


async def crawl_one_list_page(
    page,
    url: str,
    sort_mode: str,
    jobs_cfg: Dict[str, Any],
    runtime_cfg: Dict[str, Any],
    base_url: str = "",
    known_abs_links: Optional[set] = None,
    early_stop_known_streak: int = 0,
) -> List[Dict[str, Any]]:
    """Visit one list page, scroll until exhausted (or until the tail of the
    rendered list is `early_stop_known_streak` consecutive already-known links).

    `known_abs_links` is a set of absolute skillmp_link URLs already in the
    global L1 dedup at the time this job started. Used by incremental runs
    (sort=recent) to short-circuit once we've scrolled into pre-existing
    content. Pass an empty set / 0 streak to disable.
    """
    browser_cfg = runtime_cfg.get("browser", {})
    page_behavior = jobs_cfg.get("page_behavior", {})
    selectors = jobs_cfg.get("selectors", {})

    nav_timeout = int(browser_cfg.get("navigation_timeout_ms", 45000))
    default_timeout = int(browser_cfg.get("default_timeout_ms", 15000))

    scroll_pause_ms = int(page_behavior.get("scroll_pause_ms", 1200))
    max_no_progress = int(page_behavior.get("max_no_progress_scrolls", 6))
    max_scrolls = int(page_behavior.get("max_scrolls_per_page", 2000))
    eof_cfg = page_behavior.get("eof_marker", {})

    page.set_default_timeout(default_timeout)
    await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)

    try:
        await page.wait_for_selector(selectors["card_anchor"], timeout=default_timeout)
    except Exception:
        return []

    await click_sort_button(
        page=page,
        sort_buttons=selectors.get("sort_buttons", {}),
        sort_mode=sort_mode,
        timeout_ms=default_timeout,
    )
    await page.wait_for_timeout(scroll_pause_ms)

    seen_links: set = set()
    no_progress_count = 0
    early_stop_active = bool(early_stop_known_streak and known_abs_links)

    for _ in range(max_scrolls):
        cards = await page.evaluate(PAGE_SCRIPT, selectors) or []
        before = len(seen_links)
        for c in cards:
            link = c.get("skillmp_link")
            if link:
                seen_links.add(link)
        after = len(seen_links)

        if await eof_marker_visible(page, eof_cfg):
            break

        # Early-stop for incremental runs: if the LAST N rendered cards
        # (i.e. the bottom of the page, which with sort=recent is the
        # oldest) are all already in the global dedup, anything below
        # them is also already known — stop scrolling.
        if early_stop_active and len(cards) >= early_stop_known_streak:
            tail = cards[-early_stop_known_streak:]
            tail_known = 0
            for c in tail:
                link_rel = c.get("skillmp_link")
                if not link_rel:
                    break
                link_abs = (
                    link_rel
                    if link_rel.startswith("http")
                    else base_url.rstrip("/") + link_rel
                )
                if link_abs in known_abs_links:
                    tail_known += 1
                else:
                    break
            if tail_known >= early_stop_known_streak:
                break

        if after == before:
            no_progress_count += 1
            if no_progress_count >= max_no_progress:
                break
        else:
            no_progress_count = 0

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        await page.wait_for_timeout(scroll_pause_ms)

    cards = await page.evaluate(PAGE_SCRIPT, selectors) or []
    uniq: Dict[str, Dict[str, Any]] = {}
    for c in cards:
        link = c.get("skillmp_link")
        if link and link not in uniq:
            uniq[link] = c
    return list(uniq.values())


# -----------------------------------------------------------------------------
# URL normalization
# -----------------------------------------------------------------------------

SKILLMP_LINK_RE = re.compile(r"/skills/([^/?#]+)")


def absolutize_skillmp_link(href: Optional[str], base_url: str) -> Optional[str]:
    if not href:
        return None
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return base_url.rstrip("/") + href
    return base_url.rstrip("/") + "/" + href


def skill_id_from_link(link: Optional[str]) -> Optional[str]:
    if not link:
        return None
    m = SKILLMP_LINK_RE.search(link)
    return m.group(1) if m else None


# -----------------------------------------------------------------------------
# Worker loop
# -----------------------------------------------------------------------------

async def worker_loop(
    worker_id: int,
    browser,
    browser_cfg: Dict[str, Any],
    queue: "asyncio.Queue[Optional[Dict[str, Any]]]",
    shared_state: Dict[str, Any],
    jobs_cfg: Dict[str, Any],
    runtime_cfg: Dict[str, Any],
    paths: Dict[str, Path],
    sleep_between: float,
    base_url: str,
    max_retries: int,
    retry_backoff: float,
    early_stop_known_streak: int = 0,
    known_abs_links: Optional[set] = None,
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
            print(
                f"[W{worker_id}] RUN {key}  "
                f"done={shared_state['checkpoint']['done_count']}/{shared_state['checkpoint']['jobs_total']}  "
                f"dedup={shared_state['dedup']['count']}",
                flush=True,
            )

            cards: List[Dict[str, Any]] = []
            last_err: Optional[str] = None
            for attempt in range(1, max_retries + 1):
                try:
                    cards = await crawl_one_list_page(
                        page=page,
                        url=job["url"],
                        sort_mode=job["sort_mode"],
                        jobs_cfg=jobs_cfg,
                        runtime_cfg=runtime_cfg,
                        base_url=base_url,
                        known_abs_links=known_abs_links,
                        early_stop_known_streak=early_stop_known_streak,
                    )
                    last_err = None
                    break
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    print(f"[W{worker_id}] WARN attempt {attempt}/{max_retries} on {key}: {last_err}", flush=True)
                    append_jsonl(
                        paths["request_log"],
                        {"ts": now_ts(), "worker": worker_id, "job": job,
                         "attempt": attempt, "status": "error", "error": last_err},
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(retry_backoff * attempt)
                        # Recreate page to shake off any stuck state
                        try:
                            await page.close()
                        except Exception:
                            pass
                        page = await context.new_page()

            if last_err is not None and not cards:
                # Give up on this job for this run; don't mark done so it's retried next time
                append_jsonl(
                    paths["request_log"],
                    {"ts": now_ts(), "worker": worker_id, "job": job,
                     "status": "giveup", "error": last_err},
                )
                queue.task_done()
                continue

            new_count = 0
            for card in cards:
                link_abs = absolutize_skillmp_link(card.get("skillmp_link"), base_url)
                if not link_abs:
                    continue
                card_norm = dict(card)
                card_norm["skillmp_link"] = link_abs
                card_norm["skill_id"] = skill_id_from_link(link_abs)

                observation = {
                    "ts": now_ts(),
                    "worker": worker_id,
                    "source_type": job["source_type"],
                    "source_label": job["source_label"],
                    "parent_label": job.get("parent_label"),
                    "sort_mode": job["sort_mode"],
                    "page_url": job["url"],
                    **card_norm,
                }
                append_jsonl(paths["cards"], observation)

                if add_label_to_dedup(
                    dedup=shared_state["dedup"],
                    skillmp_link=link_abs,
                    card=card_norm,
                    source_type=job["source_type"],
                    source_label=job["source_label"],
                    parent_label=job.get("parent_label"),
                ):
                    new_count += 1

            # Persist dedup + checkpoint after each job. Sync I/O; cheap vs HTTP.
            save_json(paths["dedup"], shared_state["dedup"])
            mark_job_done(shared_state["checkpoint"], job)
            save_json(paths["checkpoint"], shared_state["checkpoint"])

            append_jsonl(
                paths["request_log"],
                {"ts": now_ts(), "worker": worker_id, "job": job,
                 "status": "ok", "cards_found": len(cards), "new_unique": new_count,
                 "dedup_total": shared_state["dedup"]["count"]},
            )
            print(
                f"[W{worker_id}] OK  {key}  cards={len(cards)} new={new_count} "
                f"dedup={shared_state['dedup']['count']}",
                flush=True,
            )

            # Jitter + polite pause
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
# Orchestration
# -----------------------------------------------------------------------------

async def run_crawl(args: argparse.Namespace) -> int:
    jobs_cfg = load_json(Path(args.jobs_config))
    runtime_cfg_raw = load_json(Path(args.runtime_config))
    runtime_cfg = normalize_paths(runtime_cfg_raw, args.input_dir, args.output_dir)

    input_dir = Path(runtime_cfg["input_dir"])
    output_dir = Path(runtime_cfg["output_dir"])
    ensure_dir(output_dir)
    ensure_dir(Path(runtime_cfg["checkpoint_dir"]))
    ensure_dir(Path(runtime_cfg["log_dir"]))

    paths = {
        "cards":       Path(runtime_cfg["level1"]["cards_file"]),
        "dedup":       Path(runtime_cfg["level1"]["dedup_file"]),
        "request_log": Path(runtime_cfg["level1"]["request_log_file"]),
        "checkpoint":  Path(runtime_cfg["level1"]["checkpoint_file"]),
    }

    sort_modes_override = None
    if getattr(args, "sort_modes", None):
        sort_modes_override = [s.strip() for s in args.sort_modes.split(",") if s.strip()]
        print(f"[INFO] sort_modes overridden via CLI: {sort_modes_override}", flush=True)

    jobs = build_jobs(jobs_cfg, input_dir, args.source, sort_modes_override=sort_modes_override)
    if not jobs:
        print("[ERROR] No jobs built. Check crawler_jobs.json and input label files.", file=sys.stderr)
        return 1

    try:
        checkpoint = load_or_init_checkpoint(paths["checkpoint"], jobs, args.reset_checkpoint)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    if paths["dedup"].exists():
        dedup = load_json(paths["dedup"])
        dedup.setdefault("keys", {})
        dedup.setdefault("count", len(dedup["keys"]))
    else:
        dedup = {"keys": {}, "count": 0, "updated_at": now_ts()}

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

    # Filter out already-completed jobs
    done_keys = set(checkpoint.get("done_job_keys", []))
    pending = [j for j in jobs if job_key(j) not in done_keys]
    print(
        f"[INFO] jobs total={len(jobs)} done={len(done_keys)} pending={len(pending)} "
        f"workers={args.workers}",
        flush=True,
    )
    if args.max_jobs is not None:
        pending = pending[: args.max_jobs]
        print(f"[INFO] --max_jobs={args.max_jobs} truncates pending to {len(pending)}", flush=True)

    if not pending:
        checkpoint["done"] = True
        save_json(paths["checkpoint"], checkpoint)
        print("[DONE] Nothing left to do.", flush=True)
        return 0

    browser_cfg = runtime_cfg.get("browser", {})
    headless = False if args.headful else bool(browser_cfg.get("headless", True))
    base_url = jobs_cfg["base_url"].rstrip("/")
    sleep_between = float(runtime_cfg.get("request_settings", {}).get("sleep_between_jobs_s", 2.0))
    max_retries = int(runtime_cfg.get("request_settings", {}).get("max_retries_per_job", 3))
    retry_backoff = float(runtime_cfg.get("request_settings", {}).get("retry_backoff_s", 4.0))

    queue: asyncio.Queue = asyncio.Queue()
    # Shuffle pending jobs so concurrent workers start on unrelated label pages
    # (avoids bursts of requests to the same URL).
    shuffled = pending[:]
    random.shuffle(shuffled)
    for job in shuffled:
        queue.put_nowait(job)
    for _ in range(args.workers):
        queue.put_nowait(None)  # sentinel per worker

    shared_state = {"dedup": dedup, "checkpoint": checkpoint}

    # Snapshot of already-known absolute skillmp_links at run start —
    # used by --early_stop_known_streak so incremental (sort=recent) runs
    # can short-circuit a label page once they've scrolled into pre-existing
    # content. Snapshot is intentionally NOT live-updated mid-run, so workers
    # don't race; new skills found this run still get fully processed.
    known_abs_links_snapshot: Optional[set] = None
    if getattr(args, "early_stop_known_streak", 0) > 0:
        known_abs_links_snapshot = set(dedup.get("keys", {}).keys())
        print(
            f"[INFO] early_stop_known_streak={args.early_stop_known_streak}; "
            f"known links snapshot size={len(known_abs_links_snapshot)}",
            flush=True,
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            workers = [
                asyncio.create_task(
                    worker_loop(
                        worker_id=i + 1,
                        browser=browser,
                        browser_cfg=browser_cfg,
                        queue=queue,
                        shared_state=shared_state,
                        jobs_cfg=jobs_cfg,
                        runtime_cfg=runtime_cfg,
                        paths=paths,
                        sleep_between=sleep_between,
                        base_url=base_url,
                        max_retries=max_retries,
                        retry_backoff=retry_backoff,
                        early_stop_known_streak=getattr(args, "early_stop_known_streak", 0),
                        known_abs_links=known_abs_links_snapshot,
                    )
                )
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
        print("[DONE] All jobs completed.", flush=True)

    print(f"[SUMMARY] done={checkpoint['done_count']}/{checkpoint['jobs_total']}", flush=True)
    print(f"[SUMMARY] unique_skills={dedup['count']}", flush=True)
    print(f"[SUMMARY] cards_file={paths['cards']}", flush=True)
    print(f"[SUMMARY] dedup_file={paths['dedup']}", flush=True)
    return 0


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
    for key, default in [
        ("cards_file", output_dir / "level1_cards.jsonl"),
        ("dedup_file", output_dir / "level1_dedup.json"),
        ("request_log_file", output_dir / "level1_request_log.jsonl"),
        ("checkpoint_file", output_dir / "checkpoints" / "level1_checkpoint.json"),
    ]:
        l1[key] = str(Path(l1.get(key, default)).resolve())
    cfg["level1"] = l1

    cfg["log_dir"] = str(Path(cfg.get("log_dir", output_dir / "logs")).resolve())
    cfg["checkpoint_dir"] = str(Path(cfg.get("checkpoint_dir", output_dir / "checkpoints")).resolve())
    return cfg


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SkillsMP Level-1 list-page crawler (async Playwright).")
    p.add_argument("--jobs_config", type=str, default="crawler_jobs.json")
    p.add_argument("--runtime_config", type=str, default="runtime_config.json")
    p.add_argument("--input_dir", type=str, default=None)
    p.add_argument("--output_dir", type=str, default=None)
    p.add_argument(
        "--source",
        type=str,
        default="all",
        choices=["all", "occupation", "category"],
    )
    p.add_argument("--workers", type=int, default=4, help="Number of concurrent page workers.")
    p.add_argument("--reset_checkpoint", action="store_true")
    p.add_argument("--max_jobs", type=int, default=None)
    p.add_argument(
        "--sort_modes",
        type=str,
        default=None,
        help="Comma-separated sort modes (overrides crawler_jobs.json). "
             "E.g. --sort_modes recent  for incremental updates.",
    )
    p.add_argument(
        "--early_stop_known_streak",
        type=int,
        default=0,
        help="If >0 and the last N rendered cards on a label page are all "
             "already in the global L1 dedup, stop scrolling that page. "
             "Use with --sort_modes recent for fast incremental refreshes "
             "(typical: 50). 0 = disabled (full scroll).",
    )
    p.add_argument("--headful", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run_crawl(args))
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted. Checkpoint is preserved; rerun to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
