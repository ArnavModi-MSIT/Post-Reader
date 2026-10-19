"""Internshala DOM adapter for the shared local reader."""
import argparse
import re
from posting_identity import canonical_job_url, posting_key
from description_quality import annotate
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

HOSTS = {"internshala.com", "www.internshala.com"}
DEFAULT_SEARCH = "https://internshala.com/internships/work-from-home-internships/"
DEFAULT_JOBS_SEARCH = "https://internshala.com/jobs/work-from-home/salary-6/"
RESULT_ITEM = ', '.join(f'{tag}[{attribute}*="/{kind}/detail/"]'
                        for kind in ('internship', 'job', 'fresher-job')
                        for tag, attribute in (('a', 'href'), ('', 'data-href')))
DESCRIPTION = ".internship_other_details, .job_other_details, .job_description, .internship_details, .job_details"


def search_url(value):
    parts = urlsplit(value)
    if (parts.scheme != "https" or parts.hostname not in HOSTS or parts.username or parts.password
            or parts.port not in (None, 443) or not re.match(r"^/(?:internships|jobs|fresher-jobs)(?:/|$)", parts.path)):
        raise argparse.ArgumentTypeError("Use an Internshala /internships/, /jobs/ or /fresher-jobs/ search URL.")
    path = re.sub(r"/page-\d+/?$", "/", parts.path)
    query = [(key, item) for key, item in parse_qsl(parts.query) if key not in {"page", "pageNumber"}]
    return urlunsplit(("https", "internshala.com", path.rstrip("/") + "/", urlencode(query), ""))


def job_links(page):
    raw = page.locator(RESULT_ITEM).evaluate_all("""els => els.map(el =>
        el.href || new URL(el.getAttribute('data-href'), location.href).href)""")
    return list(dict.fromkeys(url for value in raw if (url := canonical_job_url(value))))


SCROLL_WAIT_SECONDS = 30
SCROLL_JS = """() => {
    const cards = [...document.querySelectorAll(__RESULT_SELECTOR__)]
        .filter(el => el.getClientRects().length);
    const last = cards[cards.length - 1];
    if (last) last.scrollIntoView({block: 'end'});
    let panel = last && last.parentElement;
    while (panel && panel !== document.body) {
        if (panel.scrollHeight > panel.clientHeight + 2 && /auto|scroll/.test(getComputedStyle(panel).overflowY)) {
            panel.scrollTo(0, panel.scrollHeight);
            return;
        }
        panel = panel.parentElement;
    }
    window.scrollTo(0, document.scrollingElement.scrollHeight);
}""".replace("__RESULT_SELECTOR__", repr(RESULT_ITEM))


def collect_links(page, limit, stop_event=None):
    import reader
    reader.check_cancel(stop_event)
    reader.check_access(page)
    # Save each loaded batch before waiting for another one. Do not truncate to
    # `limit`: older IDs must not hide new IDs deeper in an infinite-scroll list.
    return job_links(page)


def scroll_more(page, timeout=None, stop_event=None):
    import reader
    from pagination import PaginationResult
    reader.check_cancel(stop_event or vars(page).get("_reader_stop_event"))
    before = set(job_links(page))
    timeout = SCROLL_WAIT_SECONDS if timeout is None else timeout
    outcome = PaginationResult("scroll_idle", page.url, len(before), navigation_attempted=True)
    outcome.method = "scroll"
    for _ in range(max(0, int(timeout * 2))):
        reader.check_cancel(stop_event or vars(page).get("_reader_stop_event"))
        reader.check_access(page)
        page.evaluate(SCROLL_JS)
        reader.interruptible_wait(page, 500, stop_event)
        after = set(job_links(page))
        if after - before:
            outcome.status = "advanced"
            outcome.visible_job_count = len(after)
            return outcome
    outcome.error = f"No new listing links appeared during {timeout:g} seconds of scrolling and waiting."
    return outcome


def next_results(page):
    import reader
    # Desktop/mobile pagers can use ordinary anchors with no ID or aria-label.
    # Try each group even if an earlier group contains only hidden controls.
    current = urlsplit(page.url)
    match = re.search(r"/page-(\d+)/?$", current.path)
    next_number = int(match.group(1)) + 1 if match else 2
    expected_path = re.sub(r"/page-\d+/?$", "/", current.path).rstrip("/") + f"/page-{next_number}/"
    next_path_prefix = expected_path.rstrip("/")
    groups = [
        page.locator('#navigation-forward, a[rel="next"], button[aria-label="Next"], a[aria-label="Next"]'),
        page.get_by_role("link", name=re.compile(r"^\s*Next(?:\s+page)?\s*[»›→]?\s*$", re.I)),
        page.get_by_role("button", name=re.compile(r"^\s*Next(?:\s+page)?\s*[»›→]?\s*$", re.I)),
        page.locator(f'a[href*="{next_path_prefix}"]'),
    ]
    from pagination import advance
    def accept(target):
        path = urlsplit(target).path
        return not re.search(r"/page-\d+/?$", path) or path.rstrip("/") == expected_path.rstrip("/")
    outcome = advance(page, groups, job_links, accept_target=accept)
    if outcome.status in {"missing_control", "stale_results", "navigation_failed"}:
        # Current Internshala uses infinite scrolling, not necessarily a pager.
        scrolling = scroll_more(page)
        scrolling.next_controls = outcome.next_controls
        return scrolling
    return outcome


def read_job(page, url):
    import reader
    reader.navigate(page, url)
    reader.wait_for_content(page, DESCRIPTION)
    title = reader.first_text(page, ".heading_4_5.profile, .internship_details h1, .job_details h1, h1")
    company = reader.first_text(page, ".company_name a, .company_name")
    description = reader.first_text(page, DESCRIPTION)
    if not title or not company or not description:
        raise ValueError("Internshala title, company or description was missing; no empty listing was saved.")
    fields = {}
    for item in page.locator(".other_detail_item").all():
        label = reader.first_text(item, ".item_heading").strip().lower()
        value = reader.first_text(item, ".item_body")
        if label and value:
            fields[label] = value
    locations = list(dict.fromkeys(text.strip() for text in page.locator(".location_link, #location_names").all_inner_texts() if text.strip()))
    skill_region = page.locator(".skills_container, .skill_container")
    heading = page.get_by_text("Skill(s) required", exact=True)
    if not skill_region.count() and heading.count():
        skill_region = heading.first.locator("xpath=following-sibling::*[1]")
    skills = list(dict.fromkeys(text.strip() for text in skill_region.locator(".round_tabs, .skill").all_inner_texts() if text.strip()))
    stipend = fields.get("stipend", "") or reader.first_text(page, ".stipend")
    metadata = "\n".join(locations + [f"{key}: {value}" for key, value in fields.items()])
    return annotate({"source": "internshala", "job_id": posting_key(url)[1].removeprefix("job:"), "listing_type": "internship" if "/internship/detail/" in url else "job", "link": url,
            "title": title, "company": company, "description": description,
            "location_and_posting_info": metadata, "criteria": "\n".join(skills),
            "salary": fields.get("annual salary", "") or fields.get("ctc", "") or fields.get("salary", "") or reader.first_text(page, ".salary"),
            "experience": fields.get("experience", "") or fields.get("years of experience", ""),
            "stipend": stipend, "duration": fields.get("duration", ""),
            "start_date": fields.get("start date", ""), "apply_by": fields.get("apply by", ""),
            "skills": skills, "details": fields, "collected_at": datetime.now(timezone.utc).isoformat()})
