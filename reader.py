"""Collect LinkedIn or Internshala descriptions into local JSON without input prompts."""
import argparse
import hashlib
import json
import math
import re
import sqlite3
import internshala
from seen_postings import SeenPostings, posting_key
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SEARCH = "https://www.linkedin.com/jobs/search/?keywords=data%20analyst&location=Delhi%2C%20India"
LINKEDIN_HOSTS = {"linkedin.com", "www.linkedin.com"}
DESCRIPTION = ".jobs-description-content__text, .jobs-description__content, .show-more-less-html__markup"
DESCRIPTION_READY = DESCRIPTION + ', h2:text-is("About the job")'
RESULT_LINK = 'a[href*="/jobs/view/"], a[href*="currentJobId="]'
RESULT_ITEM = RESULT_LINK + ', [data-job-id], [data-occludable-job-id], [componentkey^="job-card-component-ref-"]'
# Prefer the results column so recommended jobs in the detail panel do not enter the queue.
LINKS_JS = """() => {
    const visible = el => el.getClientRects().length > 0;
    const roots = [...document.querySelectorAll(
        '.jobs-search-results-list, .scaffold-layout__list, .jobs-search-results__list')];
    const modernCard = document.querySelector('[role="button"][componentkey^="job-card-component-ref-"]');
    let modernRoot = modernCard && modernCard.parentElement;
    while (modernRoot && !(modernRoot.scrollHeight > modernRoot.clientHeight + 2 &&
            /auto|scroll/.test(getComputedStyle(modernRoot).overflowY))) modernRoot = modernRoot.parentElement;
    const root = roots.find(visible) || modernRoot || document;
    const links = [...root.querySelectorAll('a[href*="/jobs/view/"], a[href*="currentJobId="]')]
        .map(el => el.href);
    for (const card of root.querySelectorAll('[data-job-id], [data-occludable-job-id]')) {
        const id = card.getAttribute('data-job-id') || card.getAttribute('data-occludable-job-id');
        if (/^\d+$/.test(id || '')) links.push('https://www.linkedin.com/jobs/view/' + id + '/');
    }
    for (const card of root.querySelectorAll('[componentkey^="job-card-component-ref-"]')) {
        const id = card.getAttribute('componentkey').replace('job-card-component-ref-', '');
        if (/^\d+$/.test(id)) links.push('https://www.linkedin.com/jobs/view/' + id + '/');
    }
    return [...new Set(links)];
}"""


class AccessBlocked(RuntimeError):
    """Authentication, verification, or a denied/rate-limited request."""


class BrowserStopped(RuntimeError):
    """The browser connection is gone, so pending jobs must remain resumable."""


def check_cancel(event):
    if event is not None and event.is_set():
        raise KeyboardInterrupt


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
        raise argparse.ArgumentTypeError("Use a LinkedIn job-search URL or https://internshala.com/internships/ URL.")
    query = dict(parse_qsl(parts.query))
    # A selected listing must not pin an old job when starting a new daily run.
    for key in ("currentJobId", "originToLandingJobPostings"):
        query.pop(key, None)
    query.update(sortBy="DD", start="0")
    return urlunsplit(("https", "www.linkedin.com", parts.path, urlencode(query), ""))


def canonical_job_url(value):
    parts = urlsplit(value)
    if parts.hostname in internshala.HOSTS:
        return internshala.canonical_job_url(value)
    if parts.hostname not in LINKEDIN_HOSTS:
        return None
    match = re.fullmatch(r"/jobs/view/(?:[^/]*-)?(\d+)/?", parts.path)
    job_id = match.group(1) if match else None
    if not job_id and parts.path.rstrip("/") in {"/jobs/search", "/jobs/search-results"}:
        job_id = dict(parse_qsl(parts.query)).get("currentJobId")
    if job_id and job_id.isascii() and job_id.isdigit():
        return f"https://www.linkedin.com/jobs/view/{job_id}/"
    return None


def check_access(page):
    if page.is_closed():
        raise RuntimeError("The reader's browser tab was closed.")
    path = urlsplit(page.url).path.lower()
    site = "Internshala" if urlsplit(page.url).hostname in internshala.HOSTS else "LinkedIn"
    if any(part in path for part in ("/checkpoint", "/login", "/authwall", "/challenge", "/signup")):
        raise AccessBlocked(f"{site} requires login/verification. Complete it manually before running again.")
    if site == "Internshala" and re.search(r"just a moment|access denied|security check|verify you are human|attention required|captcha", page.title(), re.I):
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
            raise RuntimeError("Navigation did not finish; no description was collected.") from exc
    check_access(page)
    if response is not None:
        if response.status in {401, 403, 429}:
            raise AccessBlocked(f"{urlsplit(url).hostname} returned HTTP {response.status}; stopping this run.")
        if response.status >= 400:
            raise ValueError(f"Page returned HTTP {response.status}: {url}")
    expected_job = canonical_job_url(url)
    if expected_job and canonical_job_url(page.url) != expected_job:
        raise ValueError("The requested job redirected elsewhere; no description was collected.")


def wait_for_content(page, selector):
    check_access(page)
    try:
        # Hidden template copies must not take precedence over a visible description.
        page.locator(f":is({selector}):visible").first.wait_for(state="visible", timeout=20000)
    except PlaywrightTimeoutError:
        check_access(page)
        raise
    check_access(page)


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
        page.wait_for_timeout(1000)
    return list(found)[:limit]


def read_job(page, url):
    if urlsplit(url).hostname in internshala.HOSTS:
        return internshala.read_job(page, url)
    navigate(page, url)
    wait_for_content(page, DESCRIPTION_READY)
    semantic_region = None
    heading = page.get_by_role("heading", name="About the job", exact=True)
    if heading.count():
        candidate = heading.first
        for _ in range(4):
            candidate = candidate.locator("xpath=..")
            if len(candidate.inner_text().strip()) > len("About the job") + 30:
                semantic_region = candidate
                break
    # Only expand the description region; unrelated page buttons must not be clicked.
    region = semantic_region if semantic_region is not None else page.locator(".jobs-description, section.description, #job-details")
    for button in region.get_by_role("button", name=re.compile(r"^(show more|see more)\b", re.I)).all():
        if button.is_visible():
            button.click(timeout=3000)
            break
    description = first_text(page, DESCRIPTION)
    if not description and semantic_region is not None:
        description = semantic_region.evaluate(r"""el => {
            // Read rendered text to preserve paragraphs and lists.
            return el.innerText.replace(/^About the job\s*/i, '').replace(/\n(?:Show more|See more)\s*$/i, '').trim();
        }""")
    title = first_text(page, ".job-details-jobs-unified-top-card__job-title, .top-card-layout__title, h1")
    job_id = url.rstrip("/").split("/")[-1]
    company = first_text(page, ".job-details-jobs-unified-top-card__company-name, .topcard__org-name-link")
    metadata = first_text(page, ".job-details-jobs-unified-top-card__primary-description-container, .topcard__flavor-row")
    if not title or not company or not metadata:
        modern_header = page.evaluate("""() => {
            for (const company of document.querySelectorAll('a[href*="/company/"]')) {
                if (!company.getClientRects().length || !company.innerText.trim()) continue;
                let root = company.parentElement;
                for (let i=0; root && i<5; i++, root=root.parentElement) {
                    const paragraphs = [...root.querySelectorAll('p')].filter(p =>
                        !p.querySelector('a[href*="/company/"]') && p.innerText.trim() &&
                        p.innerText.trim() !== company.innerText.trim());
                    if (paragraphs.length >= 2 && !root.querySelector('h2')) {
                        return {company: company.innerText.trim(), title: paragraphs[0].innerText.trim(),
                            metadata: paragraphs.slice(1).map(p=>p.innerText.trim()).join('\\n')};
                    }
                }
            }
            return {};
        }""")
        title = title or modern_header.get("title", "")
        company = company or modern_header.get("company", "")
        metadata = metadata or modern_header.get("metadata", "")
    if not description or not title:
        raise ValueError("No visible job title/description found; page layout may have changed.")
    return {
        "source": "linkedin",
        "job_id": url.rstrip("/").split("/")[-1],
        "link": url,
        "title": title,
        "company": company,
        "location_and_posting_info": metadata,
        "criteria": first_text(page, ".description__job-criteria-list, .job-details-jobs-unified-top-card__job-insight"),
        "description": description,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


class DetailReader:
    def __init__(self, context, owned_pages):
        self.context = context
        self.owned_pages = owned_pages
        self.page = None

    def read(self, url):
        for attempt in range(2):
            try:
                if self.page is None or self.page.is_closed():
                    self.page = self.context.new_page()
                    self.page.set_default_timeout(10000)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    result["updated_at"] = datetime.now(timezone.utc).isoformat()
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def next_results(page):
    if urlsplit(page.url).hostname in internshala.HOSTS:
        return internshala.next_results(page)
    before = page.evaluate(LINKS_JS)
    buttons = page.locator('button[data-testid="pagination-controls-next-button"], button[aria-label="View next page"], button[aria-label="Next"], a[aria-label="Next"]')
    if not buttons.count():
        buttons = page.get_by_role("button", name=re.compile(r"^Next(?:\s+page)?$", re.I))
    for button in buttons.all():
        if button.is_visible() and button.is_enabled() and button.get_attribute("aria-disabled") != "true":
            button.click(timeout=10000)
            try:
                page.wait_for_function(
                    f"previous => JSON.stringify(({LINKS_JS})()) !== JSON.stringify(previous) && ({LINKS_JS})().length > 0",
                    arg=before, timeout=20000)
            except PlaywrightTimeoutError:
                check_access(page)
                raise RuntimeError("Next-page control did not load different results; stopping instead of rereading stale jobs.")
            check_access(page)
            return True
    return False


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


def collect_jobs(page, detail, args, result, output):
    stop_event = getattr(args, "stop_event", None)
    check_cancel(stop_event)
    progress = result.setdefault("progress", {"results_page_url": result["search_url"], "pending_links": [], "attempted_links": []})
    seen = {job["link"] for job in result["jobs"]} | set(progress["attempted_links"])
    navigate(page, progress["results_page_url"])
    consecutive_errors = 0
    history = getattr(args, "seen_store", None)
    skipped = set(progress.setdefault("previously_collected", []))
    exhausted_pages = set()

    def new_links(candidates):
        fresh = []
        for url in candidates:
            if url in seen:
                continue
            if history is not None and history.contains(url):
                key = posting_key(url)
                skipped.add(":".join(key))
                continue
            fresh.append(url)
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
                if next_results(page):
                    progress.update(results_page_url=page.url, page_complete=False)
                    save_result(output, result)
                    continue
                result["stop_reason"] = "No enabled next-page control; all currently accessible results collected."
                break
            result["stop_reason"] = "No new job links; results exhausted or pagination did not advance."
            break
        for url in fresh:
            check_cancel(stop_event)
            if len(result["jobs"]) >= args.limit:
                break
            print(f"Reading job {len(seen) + 1}; saved {len(result['jobs'])}: {url}", flush=True)
            try:
                result["jobs"].append(detail.read(url))
                consecutive_errors = 0
            except AccessBlocked as exc:
                result["errors"].append({"link": url, "error": str(exc)})
                raise
            except BrowserStopped:
                raise
            except (PlaywrightError, ValueError, RuntimeError) as exc:
                if isinstance(exc, PlaywrightError) and target_closed(exc):
                    raise BrowserStopped("Chrome connection closed. Pending jobs saved for the next run.") from exc
                result["errors"].append({"link": url, "error": str(exc)})
                consecutive_errors += 1
            seen.add(url)
            progress["attempted_links"].append(url)
            progress["pending_links"].remove(url)
            save_result(output, result)
            if history is not None and result["jobs"] and result["jobs"][-1].get("link") == url:
                history.remember(result["jobs"][-1], output)
            if consecutive_errors >= 5:
                raise RuntimeError("Five consecutive description failures; stopping this run.")
            if len(result["jobs"]) < args.limit:
                page.wait_for_timeout(args.delay * 1000)
        if len(result["jobs"]) >= args.limit:
            result["stop_reason"] = "Requested limit reached."
            break
        progress["page_complete"] = True
        save_result(output, result)
        if not next_results(page):
            result["stop_reason"] = "No enabled next-page control; all currently accessible results collected."
            break
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
        if (previous.get("schema_version") != 2 or previous.get("search_url") != search
                or not isinstance(previous.get("jobs"), list) or not isinstance(previous.get("errors"), list)
                or not isinstance(previous.get("progress"), dict)):
            raise ValueError("Existing output is not a compatible checkpoint. Use --fresh with a new --output path; the existing file was preserved.")
        progress = previous["progress"]
        # Validate persisted URLs before using them in a browser.
        search_url(progress["results_page_url"])
        if any(canonical_job_url(link) != link for link in progress["pending_links"] + progress["attempted_links"]):
            raise ValueError("Invalid job URLs in the checkpoint; existing output was preserved.")
        result.clear()
        result.update(previous)
        print(f"Resuming {len(result['jobs'])} saved jobs, {len(progress['pending_links'])} pending links: {args.output}", flush=True)
    result.update(schema_version=2, source="internshala" if urlsplit(search).hostname in internshala.HOSTS else "linkedin",
                  search_url=search, requested_limit=args.limit, status="running", stop_reason=None)
    result.setdefault("progress", {"results_page_url": search, "pending_links": [], "attempted_links": []})
    args.output_ready = True
    save_result(args.output, result)


def run_browser(args, result, output):
    with sync_playwright() as p:
        context = None
        owned_pages = []
        history = None
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
            prepare_output(args, result, result["search_url"])
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
            detail = DetailReader(context, owned_pages)
            collect_jobs(owned_pages[0], detail, args, result, args.output)
        finally:
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
    args.output_ready = True
    stamp = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d_%H-%M-%S_%f")
    output = args.output or BASE_DIR / "data" / f"jobs_{stamp}.json"
    result = {"search_url": args.url, "collected_at": datetime.now(timezone.utc).isoformat(),
              "requested_limit": args.limit, "status": "running", "stop_reason": None,
              "jobs": [], "errors": []}
    try:
        run_browser(args, result, output)
    except AccessBlocked as exc:
        result.update(status="blocked", stop_reason=str(exc))
    except (PlaywrightError, ValueError, RuntimeError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
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
    parser.add_argument("--delay", type=float, default=3.0)
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000 or not math.isfinite(args.delay) or args.delay < 1:
        parser.error("--limit must be 1..1000 and --delay must be a finite number of at least 1")
    return execute(args)[1]


if __name__ == "__main__":
    raise SystemExit(main())
