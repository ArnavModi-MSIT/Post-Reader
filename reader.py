"""Collect LinkedIn or Internshala descriptions into local JSON without input prompts."""
import argparse
import hashlib
import json
import math
import re
import sqlite3
import os
import tempfile
import time
import internshala
import linkedin
from seen_postings import SeenPostings
from run_lock import CollectionLock
from parallel_reader import ParallelDetailReader
from posting_identity import posting_key, canonical_job_url
from description_quality import annotate, assess_description, MORE_LABEL
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SEARCH = "https://www.linkedin.com/jobs/search/?keywords=data%20analyst&location=Delhi%2C%20India"
LINKEDIN_HOSTS = {"linkedin.com", "www.linkedin.com"}
from linkedin import DESCRIPTION, DESCRIPTION_READY, RESULT_LINK, RESULT_ITEM, LINKS_JS



class AccessBlocked(RuntimeError):
    """Authentication, verification, or a denied/rate-limited request."""


class BrowserStopped(RuntimeError):
    """The browser connection is gone, so pending jobs must remain resumable."""


def check_cancel(event):
    if event is not None and event.is_set():
        raise KeyboardInterrupt


def interruptible_wait(page, milliseconds, stop_event=None):
    event = stop_event or vars(page).get("_reader_stop_event")
    remaining = max(0, milliseconds)
    while remaining:
        check_cancel(event)
        step = min(250, remaining)
        page.wait_for_timeout(step)
        remaining -= step
    check_cancel(event)


class TransientReadError(RuntimeError):
    """A temporary network/server condition eligible for bounded retry."""


def error_category(exc):
    if isinstance(exc, AccessBlocked):
        return "access_blocked"
    if isinstance(exc, PlaywrightTimeoutError):
        return "timeout"
    if isinstance(exc, TransientReadError) or (isinstance(exc, PlaywrightError) and
            any(value in str(exc) for value in ("net::ERR_CONNECTION", "net::ERR_TIMED_OUT", "Execution context was destroyed"))):
        return "transient_navigation"
    return "layout_or_data" if isinstance(exc, ValueError) else "browser_error"


def read_with_retry(detail, url, args, result, page):
    retries = getattr(args, "retries", 1)
    for attempt in range(retries + 1):
        check_cancel(getattr(args, "stop_event", None))
        try:
            record = detail.read(url)
            if not isinstance(record, dict) or posting_key(record.get("link")) != posting_key(url):
                raise ValueError("Detail reader returned a different or invalid posting identity.")
            if "description" in record:
                record["extraction_attempts"] = attempt + 1
            return record
        except (AccessBlocked, BrowserStopped):
            raise
        except (PlaywrightError, RuntimeError) as exc:
            if error_category(exc) not in {"timeout", "transient_navigation"} or attempt == retries:
                raise
            result["retried"] = result.get("retried", 0) + 1
            interruptible_wait(page, min(8000, 1000 * 2 ** attempt), getattr(args, "stop_event", None))


def target_closed(exc):
    return any(text in str(exc).lower() for text in (
        "target page, context or browser has been closed", "browser has been closed", "target closed"))


def search_url(value):
    parts = urlsplit(value)
    if parts.hostname in internshala.HOSTS:
        return internshala.search_url(value)
    if (parts.scheme != "https" or parts.hostname not in LINKEDIN_HOSTS
            or parts.username or parts.password or parts.port not in (None, 443)
            or parts.path.rstrip("/") not in {"/jobs/search", "/jobs/search-results"}):
        raise argparse.ArgumentTypeError("Use a LinkedIn job-search URL or Internshala jobs/internships search URL.")
    query = dict(parse_qsl(parts.query))
    # A selected listing must not pin an old job when starting a new daily run.
    for key in ("currentJobId", "originToLandingJobPostings"):
        query.pop(key, None)
    query.update(sortBy="DD", start="0")
    return urlunsplit(("https", "www.linkedin.com", parts.path, urlencode(query), ""))


def search_signature(url):
    normalized = urlsplit(search_url(url))
    path = "/jobs/search" if normalized.hostname in LINKEDIN_HOSTS else normalized.path.rstrip("/")
    query = tuple(sorted((key, value) for key, value in parse_qsl(normalized.query)
                         if key not in {"start", "origin", "trackingId", "trk"}))
    return normalized.hostname, path, query


def validate_checkpoint(value, search):
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise ValueError("Existing output has an unsupported checkpoint schema; file preserved.")
    if search_signature(value.get("search_url", "")) != search_signature(search):
        raise ValueError("Checkpoint belongs to a different search; file preserved.")
    for field in ("jobs", "errors"):
        if not isinstance(value.get(field), list) or any(not isinstance(row, dict) for row in value[field]):
            raise ValueError(f"Invalid checkpoint {field}; file preserved.")
    source = "internshala" if urlsplit(search).hostname in internshala.HOSTS else "linkedin"
    for job in value["jobs"]:
        if any(field in job and not isinstance(job[field], str) for field in ("title", "description", "company", "job_id")):
            raise ValueError("Invalid posting fields in checkpoint; file preserved.")
        key = posting_key(job.get("link"))
        if not key or key[0] != source:
            raise ValueError("Invalid posting in checkpoint; file preserved.")
    progress = value.get("progress")
    if not isinstance(progress, dict):
        raise ValueError("Invalid checkpoint progress; file preserved.")
    if search_signature(progress.get("results_page_url", "")) != search_signature(search):
        raise ValueError("Checkpoint results page has different search filters; file preserved.")
    for field in ("pending_links", "attempted_links"):
        if not isinstance(progress.get(field), list):
            raise ValueError(f"Invalid checkpoint {field}; file preserved.")
        for link in progress[field]:
            key = posting_key(link)
            if not key or key[0] != source or canonical_job_url(link) != link:
                raise ValueError("Invalid job URL in checkpoint; file preserved.")
    if "page_complete" in progress and not isinstance(progress["page_complete"], bool):
        raise ValueError("Invalid page-complete flag; file preserved.")
    if "previously_collected" in progress and (not isinstance(progress["previously_collected"], list)
            or any(not isinstance(key, str) for key in progress["previously_collected"])):
        raise ValueError("Invalid duplicate checkpoint; file preserved.")


def check_access(page):
    if page.is_closed():
        raise RuntimeError("The reader's browser tab was closed.")
    path = urlsplit(page.url).path.lower()
    site = "Internshala" if urlsplit(page.url).hostname in internshala.HOSTS else "LinkedIn"
    if any(path == prefix or path.startswith(prefix + "/") for prefix in ("/checkpoint", "/login", "/authwall", "/challenge", "/signup")):
        raise AccessBlocked(f"{site} requires login/verification. Complete it manually before running again.")
    if site == "Internshala" and re.match(r"^\s*(?:just a moment|access denied|security check|verify you are human|attention required|captcha)(?:\s*[.!|:-]|\s*$)", page.title(), re.I):
        raise AccessBlocked("Internshala requires browser verification or denied access. Complete verification manually before running again.")
    if page.locator('input[name="session_key"]:visible, input#username:visible').count():
        raise AccessBlocked("LinkedIn is displaying a sign-in form. Log in manually before running again.")


def navigate(page, url):
    response = None
    previous_url = page.url
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightError as exc:
        if "interrupted by another navigation" not in str(exc) and not isinstance(exc, PlaywrightTimeoutError):
            raise
        check_access(page)
        if page.url == previous_url and previous_url != url:
            raise RuntimeError("Navigation did not reach the requested page; refusing to read stale content.") from exc
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PlaywrightTimeoutError:
            raise TransientReadError("Navigation did not finish; no description was collected.") from exc
    check_access(page)
    if response is not None:
        if response.status in {401, 403, 429}:
            raise AccessBlocked(f"{urlsplit(url).hostname} returned HTTP {response.status}; stopping this run.")
        if response.status >= 500:
            raise TransientReadError(f"Temporary HTTP {response.status}: {url}")
        if response.status >= 400:
            raise ValueError(f"Page returned HTTP {response.status}: {url}")
    if not canonical_job_url(url):
        if search_signature(url) != search_signature(page.url):
            raise ValueError("The requested search redirected to different filters or website; progress was preserved.")
    expected_job = canonical_job_url(url)
    if expected_job and posting_key(page.url) != posting_key(url):
        raise ValueError("The requested job redirected elsewhere; no description was collected.")


def wait_for_content(page, selector):
    for attempt in range(20):
        check_cancel(vars(page).get("_reader_stop_event"))
        check_access(page)
        try:
            page.locator(f":is({selector}):visible").first.wait_for(state="visible", timeout=1000)
            check_access(page)
            return
        except PlaywrightTimeoutError:
            if attempt == 19:
                check_access(page)
                raise


def first_text(page, selectors):
    # Preserve selector priority rather than letting DOM order select a broad wrapper.
    for selector in selectors.split(","):
        for element in page.locator(selector.strip()).all():
            if element.is_visible():
                text = element.inner_text().strip()
                if text:
                    return text
    return ""


def job_links(page):
    if urlsplit(page.url).hostname in internshala.HOSTS:
        return internshala.job_links(page)
    return list(dict.fromkeys(url for href in page.evaluate(LINKS_JS)
                              if (url := canonical_job_url(href))))


def collect_links(page, limit, stop_event=None):
    if urlsplit(page.url).hostname in internshala.HOSTS:
        return internshala.collect_links(page, limit, stop_event)
    found = {}
    unchanged = 0
    for _ in range(30):
        check_cancel(stop_event)
        check_access(page)
        before = len(found)
        for url in job_links(page):
            found.setdefault(url, None)
        unchanged = unchanged + 1 if len(found) == before else 0
        if len(found) >= limit or unchanged >= 3:
            break
        page.evaluate("""() => {
            const roots = [...document.querySelectorAll(
                '.jobs-search-results-list, .scaffold-layout__list, .jobs-search-results__list')];
            const root = roots.find(el => el.getClientRects().length) || document;
            const link = root.querySelector('[role="button"][componentkey^="job-card-component-ref-"], a[href*="/jobs/view/"], a[href*="currentJobId="]');
            let panel = link && link.parentElement;
            while (panel && panel !== document.body) {
                const style = getComputedStyle(panel);
                if (panel.scrollHeight > panel.clientHeight + 2 && /auto|scroll/.test(style.overflowY)) {
                    panel.scrollBy(0, Math.max(200, panel.clientHeight * 0.8));
                    return;
                }
                panel = panel.parentElement;
            }
            window.scrollBy(0, window.innerHeight * 0.8);
        }""")
        interruptible_wait(page, 1000, stop_event)
    return list(found)[:limit]


def read_job(page, url):
    if urlsplit(url).hostname in internshala.HOSTS:
        return internshala.read_job(page, url)
    return linkedin.read_job(page, url)


class DetailReader:
    def __init__(self, context, owned_pages, stop_event=None):
        self.context = context
        self.owned_pages = owned_pages
        self.page = None
        self.stop_event = stop_event

    def read(self, url):
        for attempt in range(2):
            try:
                if self.page is None or self.page.is_closed():
                    self.page = self.context.new_page()
                    self.page.set_default_timeout(10000)
                    self.page._reader_stop_event = self.stop_event
                    self.owned_pages.append(self.page)
                return read_job(self.page, url)
            except PlaywrightError as exc:
                if not target_closed(exc):
                    raise
                browser = self.context.browser
                if browser is None or not browser.is_connected() or attempt:
                    raise BrowserStopped("Chrome/tab connection closed. Progress saved; run the launcher again to resume.") from exc
                self.page = None
                print("Detail tab closed; recreating it and retrying this job once.", flush=True)


def save_result(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    result["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    # Dashboard reads, indexing or security software can briefly deny Windows
    # replacement. Retry the already-written snapshot; never truncate the target.
    delays = (0.05, 0.1, 0.2, 0.4, 0.8, 0.8)
    for attempt in range(len(delays) + 1):
        try:
            temp.replace(path)
            return
        except PermissionError as exc:
            if attempt == len(delays):
                raise PermissionError(
                    f"Checkpoint stayed locked after {attempt + 1} attempts. "
                    f"Previous checkpoint preserved; unsaved snapshot retained at {temp}. Original error: {exc}"
                ) from exc
            time.sleep(delays[attempt])


def next_results(page):
    if urlsplit(page.url).hostname in internshala.HOSTS:
        return internshala.next_results(page)
    from pagination import advance
    return advance(page, [
        page.locator('button[data-testid="pagination-controls-next-button"], button[aria-label="View next page"], button[aria-label="Next"], a[aria-label="Next"], a[rel="next"]'),
        page.get_by_role("button", name=re.compile(r"^Next(?:\s+page)?$", re.I)),
        page.get_by_role("link", name=re.compile(r"^Next(?:\s+page)?$", re.I)),
    ], job_links)


def choose_search(context, explicit_url):
    if explicit_url:
        return explicit_url
    for tab in context.pages:
        try:
            return search_url(tab.url)
        except (argparse.ArgumentTypeError, ValueError):
            continue
    remembered = BASE_DIR / "data" / "last_search_url.txt"
    if remembered.exists():
        try:
            return search_url(remembered.read_text(encoding="utf-8").strip())
        except (argparse.ArgumentTypeError, ValueError):
            pass
    return search_url(DEFAULT_SEARCH)


def persist_record(result, record, output, history):
    if "description" in record:
        annotate(record)
    key = posting_key(record.get("link"))
    for index, old in enumerate(result["jobs"]):
        if posting_key(old.get("link")) == key:
            result["jobs"][index] = record
            break
    else:
        result["jobs"].append(record)
    result["complete_candidates"] = sum(assess_record(job) == "complete_candidate" for job in result["jobs"])
    result["incomplete"] = len(result["jobs"]) - result["complete_candidates"]
    save_result(output, result)
    if history is not None:
        history.remember(record, output)


def assess_record(job):
    return annotate(dict(job))["description_status"]


def collect_repairs(page, detail, args, result, output):
    history = args.seen_store
    source = "internshala" if urlsplit(result["search_url"]).hostname in internshala.HOSTS else "linkedin"
    candidates = history.retry_candidates(source, args.limit)
    if hasattr(detail, "prime"):
        detail.prime([item["link"] for item in candidates])
    result["run_mode"] = "retry_incomplete"
    consecutive_errors = 0
    for record in candidates:
        check_cancel(getattr(args, "stop_event", None))
        url = record["link"]
        result["progress"]["pending_links"] = [item["link"] for item in candidates
                                               if item["link"] not in result["progress"]["attempted_links"]]
        save_result(output, result)
        try:
            replacement = read_with_retry(detail, url, args, result, page)
            replacement["replaces_collected_at"] = record.get("collected_at")
            persist_record(result, replacement, output, history)
            consecutive_errors = 0
        except (AccessBlocked, BrowserStopped):
            raise
        except (PlaywrightError, ValueError, RuntimeError) as exc:
            result["errors"].append({"link": url, "error": str(exc), "category": error_category(exc), "exception_type": type(exc).__name__})
            consecutive_errors += 1
        result["progress"]["attempted_links"].append(url)
        result["progress"]["pending_links"].remove(url)
        save_result(output, result)
        if consecutive_errors >= 5:
            raise RuntimeError("Five consecutive repair failures; stopping this run.")
        if not isinstance(detail, ParallelDetailReader):
            interruptible_wait(page, args.delay * 1000, getattr(args, "stop_event", None))
    result["progress"]["pending_links"] = []
    result.update(status="partial" if result["errors"] or result.get("incomplete", 0) else "completed",
                  stop_reason=f"Repair pass finished: {len(candidates)} previously incomplete postings attempted. Original JSON files were preserved.")


def collect_jobs(page, detail, args, result, output):
    stop_event = getattr(args, "stop_event", None)
    check_cancel(stop_event)
    progress = result.setdefault("progress", {"results_page_url": result["search_url"], "pending_links": [], "attempted_links": []})
    seen = {job["link"] for job in result["jobs"]} | set(progress["attempted_links"])
    seen_keys = {posting_key(url) for url in seen}
    navigate(page, progress["results_page_url"])
    consecutive_errors = 0
    history = getattr(args, "seen_store", None)
    skipped = set(progress.setdefault("previously_collected", []))
    exhausted_pages = set()

    def new_links(candidates):
        fresh = []
        queued_keys = set()
        for url in candidates:
            identity = posting_key(url)
            if identity in seen_keys or identity in queued_keys:
                continue
            if history is not None and history.was_seen(url):
                key = posting_key(url)
                skipped.add(":".join(key))
                continue
            fresh.append(url)
            queued_keys.add(identity)
        progress["previously_collected"] = sorted(skipped)
        result["duplicates_skipped"] = len(skipped)
        return fresh

    result_item = internshala.RESULT_ITEM if urlsplit(result["search_url"]).hostname in internshala.HOSTS else RESULT_ITEM
    while len(result["jobs"]) < args.limit:
        check_cancel(stop_event)
        wait_for_content(page, result_item)
        candidates = list(progress["pending_links"])
        fresh = new_links(candidates)
        if not fresh:
            candidates = collect_links(page, max(25, args.limit), stop_event)
            fresh = new_links(candidates)
        progress["results_page_url"] = page.url
        progress["pending_links"] = fresh.copy()
        save_result(output, result)
        if not fresh:
            if candidates or progress.get("page_complete"):
                fingerprint = tuple(sorted(candidates))
                if fingerprint in exhausted_pages:
                    result["stop_reason"] = "Results repeated a page with no new postings; progress was kept."
                    break
                exhausted_pages.add(fingerprint)
                outcome = next_results(page)
                if hasattr(outcome, "to_dict"):
                    result["pagination"] = outcome.to_dict()
                if outcome:
                    result["pages_advanced"] = result.get("pages_advanced", 0) + 1
                    progress.update(results_page_url=page.url, page_complete=False)
                    save_result(output, result)
                    continue
                result["stop_reason"] = getattr(outcome, "reason", "No usable Next control found; check pagination diagnostics.")
                break
            result["stop_reason"] = "No new job links; results exhausted or pagination did not advance."
            break
        if hasattr(detail, "prime"):
            detail.prime(fresh[:max(0, args.limit - len(result["jobs"]))])
        for url in fresh:
            check_cancel(stop_event)
            if len(result["jobs"]) >= args.limit:
                break
            print(f"Reading job {len(seen) + 1}; saved {len(result['jobs'])}: {url}", flush=True)
            record = None
            try:
                record = read_with_retry(detail, url, args, result, page)
                consecutive_errors = 0
            except AccessBlocked as exc:
                result["errors"].append({"link": url, "error": str(exc), "category": error_category(exc), "exception_type": type(exc).__name__})
                raise
            except BrowserStopped:
                raise
            except (PlaywrightError, ValueError, RuntimeError) as exc:
                if isinstance(exc, PlaywrightError) and target_closed(exc):
                    raise BrowserStopped("Chrome connection closed. Pending jobs saved for the next run.") from exc
                result["errors"].append({"link": url, "error": str(exc), "category": error_category(exc), "exception_type": type(exc).__name__})
                consecutive_errors += 1
            seen.add(url)
            seen_keys.add(posting_key(url))
            progress["attempted_links"].append(url)
            progress["pending_links"].remove(url)
            if record is not None:
                persist_record(result, record, output, history)
            else:
                save_result(output, result)
            if consecutive_errors >= 5:
                raise RuntimeError("Five consecutive description failures; stopping this run.")
            if len(result["jobs"]) < args.limit and not isinstance(detail, ParallelDetailReader):
                interruptible_wait(page, args.delay * 1000, stop_event)
        if len(result["jobs"]) >= args.limit:
            result["stop_reason"] = "Requested limit reached."
            break
        progress["page_complete"] = True
        save_result(output, result)
        outcome = next_results(page)
        if hasattr(outcome, "to_dict"):
            result["pagination"] = outcome.to_dict()
        if not outcome:
            result["stop_reason"] = getattr(outcome, "reason", "No usable Next control found; check pagination diagnostics.")
            break
        result["pages_advanced"] = result.get("pages_advanced", 0) + 1
        progress.update(results_page_url=page.url, page_complete=False)
        save_result(output, result)
    result["status"] = "completed" if len(result["jobs"]) >= args.limit else "partial"


def daily_output_path(search, fresh=False):
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")
    key = hashlib.sha256(search.encode("utf-8")).hexdigest()[:10]
    suffix = "_" + datetime.now().strftime("%H%M%S_%f") if fresh else ""
    prefix = "internshala" if urlsplit(search).hostname in internshala.HOSTS else "jobs"
    return BASE_DIR / "data" / f"{prefix}_{today}_{key}{suffix}.json"


def prepare_output(args, result, search):
    if args.output is None:
        args.output = daily_output_path(search, args.fresh)
    args.output_ready = False
    if args.output.exists() and not args.fresh:
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        validate_checkpoint(previous, search)
        result.clear()
        result.update(previous)
        successful = {posting_key(job["link"]) for job in result["jobs"]}
        checkpoint_source = "internshala" if urlsplit(search).hostname in internshala.HOSTS else "linkedin"
        failed = list(dict.fromkeys(error.get("link") for error in result["errors"]
                                    if posting_key(error.get("link")) and posting_key(error["link"])[0] == checkpoint_source
                                    and posting_key(error["link"]) not in successful))
        progress = result["progress"]
        progress["attempted_links"] = [url for url in progress["attempted_links"] if url not in failed]
        progress["pending_links"] = list(dict.fromkeys(failed + progress["pending_links"]))
        print(f"Resuming {len(result['jobs'])} saved jobs, {len(progress['pending_links'])} pending links: {args.output}", flush=True)
    result.update(schema_version=2, source="internshala" if urlsplit(search).hostname in internshala.HOSTS else "linkedin",
                  search_url=search, requested_limit=args.limit, status="running", stop_reason=None)
    result.setdefault("progress", {"results_page_url": search, "pending_links": [], "attempted_links": []})
    save_result(args.output, result)
    args.output_ready = True


def run_browser(args, result, output):
    with sync_playwright() as p:
        context = None
        owned_pages = []
        history = None
        detail = None
        try:
            if args.launch:
                context = p.chromium.launch_persistent_context(str(args.profile.resolve()), headless=False)
            else:
                try:
                    browser = p.chromium.connect_over_cdp(args.cdp, timeout=10000)
                except PlaywrightError as exc:
                    raise RuntimeError("Cannot attach to Chrome. Double-click Run_Reader.cmd to start/reuse Chrome.") from exc
                if not browser.contexts:
                    raise RuntimeError("Chrome has no available browser context.")
                context = browser.contexts[0]
            result["search_url"] = choose_search(context, args.url)
            history = SeenPostings(BASE_DIR / "data" / "seen_postings.sqlite3")
            imported = history.import_directory(BASE_DIR / "data")
            if args.output is not None and args.output.exists():
                previous = json.loads(args.output.read_text(encoding="utf-8-sig"))
                if isinstance(previous, dict):
                    for record in previous.get("jobs", []):
                        history.remember(record, args.output)
            if getattr(args, "retry_incomplete", False):
                args.output = BASE_DIR / "data" / ("repair_" + daily_output_path(result["search_url"], True).name) if args.output is None else args.output
                if args.output.exists():
                    raise ValueError("Repair output already exists. Choose a new output path; existing file preserved.")
                args.fresh = True
            prepare_output(args, result, result["search_url"])
            result["history_quality"] = history.quality_counts()
            for record in result["jobs"]:
                history.remember(record, args.output)
            args.seen_store = history
            print(f"Global history: {history.count()} saved postings; imported {imported['postings_added']} from JSON.", flush=True)
            remembered = BASE_DIR / "data" / "last_search_url.txt"
            remembered.parent.mkdir(parents=True, exist_ok=True)
            remembered.write_text(result["search_url"], encoding="utf-8")
            save_result(args.output, result)
            if len(result["jobs"]) >= args.limit:
                result.update(status="completed", stop_reason="Today's requested limit is already saved. Use --fresh for an independent run.")
                return
            owned_pages.append(context.new_page())
            for page in owned_pages:
                page.set_default_timeout(10000)
                page._reader_stop_event = getattr(args, "stop_event", None)
            workers = getattr(args, "workers", 1)
            if workers > 1:
                if args.launch:
                    raise ValueError("Parallel readers require CDP Chrome. Use the dashboard or omit --launch.")
                detail = ParallelDetailReader(args.cdp, getattr(args, "stop_event", None), args.delay, workers=workers)
            else:
                detail = DetailReader(context, owned_pages, getattr(args, "stop_event", None))
            result["workers"] = workers
            if getattr(args, "retry_incomplete", False):
                collect_repairs(owned_pages[0], detail, args, result, args.output)
            else:
                collect_jobs(owned_pages[0], detail, args, result, args.output)
        finally:
            if isinstance(detail, ParallelDetailReader):
                detail.close()
            if history is not None:
                history.close()
            # Cleanup must not mask the original error or close the user's browser/tabs.
            try:
                if context is not None and args.launch:
                    context.close()
                else:
                    for page in owned_pages:
                        if not page.is_closed():
                            page.close()
            except PlaywrightError as exc:
                print(f"Tab cleanup: {exc}", flush=True)


def execute(args):
    args.output_ready = False
    stamp = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d_%H-%M-%S_%f")
    output = args.output or BASE_DIR / "data" / f"jobs_{stamp}.json"
    result = {"search_url": args.url, "collected_at": datetime.now(timezone.utc).isoformat(),
              "requested_limit": args.limit, "status": "running", "stop_reason": None,
              "jobs": [], "errors": []}
    try:
        with CollectionLock(BASE_DIR / "data" / ".collection.lock"):
            run_browser(args, result, output)
    except AccessBlocked as exc:
        result.update(status="blocked", stop_reason=str(exc))
        if hasattr(exc, "pagination"):
            result["pagination"] = exc.pagination
    except (PlaywrightError, ValueError, RuntimeError, OSError, KeyError, TypeError, sqlite3.Error, argparse.ArgumentTypeError) as exc:
        result.update(status="failed", stop_reason=str(exc))
    except KeyboardInterrupt:
        result.update(status="interrupted", stop_reason="Stopped by user.")
    finally:
        try:
            output = args.output or output
            if args.output_ready:
                save_result(output, result)
        except OSError as exc:
            result.update(status="failed", stop_reason=f"Could not save JSON: {exc}")
    print(f"{result['status']}: saved {len(result['jobs'])} jobs, {len(result['errors'])} errors. Output: {output.resolve()}", flush=True)
    print(result["stop_reason"], flush=True)
    code = 130 if result["status"] == "interrupted" else int(result["status"] in {"failed", "blocked"})
    return result, code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=search_url, help="Use this search instead of an open or remembered search")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", type=Path, default=BASE_DIR / ".browser-profile")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--fresh", action="store_true", help="Start an independent run instead of resuming today's JSON")
    parser.add_argument("--retry-incomplete", action="store_true", help="Repair previously incomplete postings for the selected website")
    parser.add_argument("--retries", type=int, default=1, help="Transient retries per listing (0..3)")
    parser.add_argument("--workers", type=int, choices=(1, 2, 3), default=1, help="Independent detail readers (default: 1)")
    parser.add_argument("--delay", type=float, default=3.0)
    args = parser.parse_args()
    if not 0 <= args.retries <= 3 or not 1 <= args.limit <= 1000 or not math.isfinite(args.delay) or args.delay < 1:
        parser.error("--limit must be 1..1000 and --delay must be a finite number of at least 1")
    if args.launch and args.workers > 1:
        parser.error("Parallel readers use debug-enabled Chrome; omit --launch")
    return execute(args)[1]


if __name__ == "__main__":
    raise SystemExit(main())
