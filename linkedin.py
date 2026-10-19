"""LinkedIn DOM adapter. Browser lifecycle and persistence live in reader.py."""
import re
from datetime import datetime, timezone
from description_quality import MORE_LABEL, annotate

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


def read_description(page):
    import reader
    semantic = None
    heading = page.get_by_role("heading", name="About the job", exact=True)
    if heading.count():
        candidate = heading.first
        for _ in range(4):
            candidate = candidate.locator("xpath=..")
            if candidate.evaluate("el => [\"BODY\", \"HTML\"].includes(el.tagName)"):
                break
            if len(candidate.inner_text().strip()) > len("About the job") + 30:
                semantic = candidate
                break
    regions = page.locator(".jobs-description, section.description, #job-details")
    region = next((item for item in regions.all() if item.is_visible()), semantic)
    if region is None:
        region = page.locator(":is(" + DESCRIPTION + "):visible")

    def text_content():
        values = []
        for selector in DESCRIPTION.split(","):
            for element in region.locator(selector.strip()).all():
                if element.is_visible() and element.inner_text().strip():
                    values.append(element.inner_text().strip())
        if not values:
            # Some layouts use only semantic wrappers, or the region is the description itself.
            for item in region.all():
                if item.is_visible():
                    values.append(re.sub(r"^About the job\s*", "", item.inner_text().strip(), flags=re.I))
        return max(values, key=len, default="")

    expanded = False
    for _ in range(3):
        buttons = [button for button in region.get_by_role("button", name=MORE_LABEL).all()
                   if button.is_visible() and button.is_enabled()]
        if not buttons:
            break
        before = text_content()
        buttons[0].click(timeout=3000)
        for _ in range(12):
            reader.interruptible_wait(page, 250)
            if text_content() != before:
                expanded = True
                break
        else:
            break
    pending = any(button.is_visible() for button in region.get_by_role("button", name=MORE_LABEL).all())
    return text_content(), expanded, pending


def read_job(page, url):
    import reader
    reader.navigate(page, url)
    reader.wait_for_content(page, DESCRIPTION_READY)
    description, expanded, pending = read_description(page)
    title = reader.first_text(page, ".job-details-jobs-unified-top-card__job-title, .top-card-layout__title, h1")
    job_id = url.rstrip("/").split("/")[-1]
    company = reader.first_text(page, ".job-details-jobs-unified-top-card__company-name, .topcard__org-name-link")
    metadata = reader.first_text(page, ".job-details-jobs-unified-top-card__primary-description-container, .topcard__flavor-row")
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
    return annotate({
        "source": "linkedin",
        "job_id": url.rstrip("/").split("/")[-1],
        "link": url,
        "title": title,
        "company": company,
        "location_and_posting_info": metadata,
        "criteria": reader.first_text(page, ".description__job-criteria-list, .job-details-jobs-unified-top-card__job-insight"),
        "description": description,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "description_expanded": expanded, "extraction_method": "scoped_dom",
    }, expansion_pending=pending)
