"""Real Chromium tests using intercepted fixture HTML only."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
import reader
import linkedin

JOB = "https://www.linkedin.com/jobs/view/123/"
SEARCH = "https://www.linkedin.com/jobs/search/?keywords=fixture"
HEADER = '<h1 class="top-card-layout__title">Analyst</h1><a class="topcard__org-name-link">Fixture</a>'
EXPAND = HEADER + '''<section class="jobs-description"><h2>About the job</h2>
<div class="jobs-description-content__text">A short preview ... more</div>
<button onclick="const button=this;setTimeout(()=>{document.querySelector('.jobs-description-content__text').innerText='Full responsibilities: SQL, Python, data quality and reports.';button.hidden=true},200)">... more</button></section>'''
TRUNCATED = HEADER + '<section class="jobs-description"><div class="jobs-description-content__text">An incomplete preview ... more</div></section>'
PAGER = '''<div class="jobs-search-results-list"><a href="/jobs/view/123/">First</a></div>
<button aria-label="View next page" onclick="void(0)">Next</button>
<button aria-label="Next" onclick="document.querySelector('.jobs-search-results-list').innerHTML='<a href=/jobs/view/456/>Second</a>';document.querySelectorAll('button').forEach(b=>b.disabled=true)">Next</button>'''


def main():
    chrome = Path(os.environ.get("ProgramFiles", "/nonexistent")) / "Google/Chrome/Application/chrome.exe"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(chrome) if chrome.exists() else None, headless=True)
        try:
            context = browser.new_context(service_workers="block")
            fixture = {"body": EXPAND}
            context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=fixture["body"]))
            page = context.new_page()
            record = reader.read_job(page, JOB)
            assert record["description_status"] == "complete_candidate", record
            assert record["description_expanded"] is True
            assert "Full responsibilities" in record["description"]
            assert "short preview" not in record["description"]
            fixture["body"] = TRUNCATED
            record = reader.read_job(page, JOB)
            assert record["description_status"] == "suspected_truncated", record
            fixture["body"] = HEADER + '<div>Other page content is not a job description.</div><h2>About the job</h2>'
            try:
                reader.read_job(page, JOB)
            except ValueError:
                pass
            else:
                raise AssertionError("Missing description captured unrelated body text")
            fixture["body"] = PAGER
            page.goto(SEARCH)
            result = reader.next_results(page)
            assert result.status == "advanced", result
            assert len(result.next_controls) == 2, result
            assert reader.next_results(page).status == "verified_last_page"
            fixture["body"] = '<div class="jobs-search-results-list"><a href="/jobs/view/123/">First</a></div>'
            page.goto(SEARCH)
            assert reader.next_results(page).status == "missing_control"
            print("Reliability browser fixtures passed: delayed ellipsis expansion, suspected truncation, dead first Next, disabled last page and missing pager.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
