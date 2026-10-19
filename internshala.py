"""Internshala DOM adapter for the shared local reader."""
import argparse
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

HOSTS = {"internshala.com", "www.internshala.com"}
DEFAULT_SEARCH = "https://internshala.com/internships/work-from-home-internships/"
RESULT_ITEM = 'a[href*="/internship/detail/"], [data-href*="/internship/detail/"]'
DESCRIPTION = ".internship_other_details, .internship_details"


def search_url(value):
    parts = urlsplit(value)
    if (parts.scheme != "https" or parts.hostname not in HOSTS or parts.username or parts.password
            or parts.port not in (None, 443) or not re.match(r"^/internships(?:/|$)", parts.path)):
        raise argparse.ArgumentTypeError("Use an https://internshala.com/internships/ search URL.")
    path = re.sub(r"/page-\d+/?$", "/", parts.path)
    query = [(key, item) for key, item in parse_qsl(parts.query) if key not in {"page", "pageNumber"}]
    return urlunsplit(("https", "internshala.com", path.rstrip("/") + "/", urlencode(query), ""))


def canonical_job_url(value):
    parts = urlsplit(value)
    if parts.hostname not in HOSTS or parts.scheme != "https":
        return None
    if not re.fullmatch(r"/internship/detail/[^/]+\d+/?", parts.path):
        return None
    return urlunsplit(("https", "internshala.com", parts.path.rstrip("/"), "", ""))


def job_links(page):
    raw = page.locator(RESULT_ITEM).evaluate_all("""els => els.map(el =>
        el.href || new URL(el.getAttribute('data-href'), location.href).href)""")
    return list(dict.fromkeys(url for value in raw if (url := canonical_job_url(value))))


def collect_links(page, limit, stop_event=None):
    import reader
    found = {}
    unchanged = 0
    for _ in range(20):
        reader.check_cancel(stop_event)
        reader.check_access(page)
        before = len(found)
        for url in job_links(page):
            found.setdefault(url, None)
        unchanged = unchanged + 1 if len(found) == before else 0
        if len(found) >= limit or unchanged >= 3:
            break
        page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        page.wait_for_timeout(800)
    return list(found)[:limit]


def next_results(page):
    import reader
    before = set(job_links(page))
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
    controls = (control for group in groups for control in group.all())
    for control in controls:
        classes = (control.get_attribute("class") or "").split()
        if (not control.is_visible() or not control.is_enabled() or "disabled" in classes
                or control.get_attribute("aria-disabled") == "true"):
            continue
        if control.locator('xpath=ancestor::*[@disabled or @aria-disabled="true" or contains(concat(" ", normalize-space(@class), " "), " disabled ")]').count():
            continue
        href = control.get_attribute("href")
        if href and not href.startswith(("#", "javascript:")):
            target = urljoin(page.url, href)
            target_parts = urlsplit(target)
            if re.search(r"/page-\d+/?$", target_parts.path) and target_parts.path.rstrip("/") != expected_path.rstrip("/"):
                continue
            search_url(target)  # Validate the actual target, without resetting its page cursor.
            reader.navigate(page, target)
        else:
            control.click(timeout=10000)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            reader.check_access(page)
            after = set(job_links(page))
            if after and after != before:
                return True
            page.wait_for_timeout(500)
        raise RuntimeError("Internshala next-page control did not load different internships. Progress was kept.")
    return False


def read_job(page, url):
    import reader
    reader.navigate(page, url)
    reader.wait_for_content(page, DESCRIPTION)
    title = reader.first_text(page, ".heading_4_5.profile, .internship_details h1, h1")
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
    return {"source": "internshala", "job_id": re.search(r"(\d+)$", url).group(1), "link": url,
            "title": title, "company": company, "description": description,
            "location_and_posting_info": metadata, "criteria": "\n".join(skills),
            "stipend": stipend, "duration": fields.get("duration", ""),
            "start_date": fields.get("start date", ""), "apply_by": fields.get("apply by", ""),
            "skills": skills, "details": fields, "collected_at": datetime.now(timezone.utc).isoformat()}
