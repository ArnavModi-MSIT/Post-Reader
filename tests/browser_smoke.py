"""Exercise real browser selectors against local fixtures; never contact LinkedIn."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reader
from playwright.sync_api import sync_playwright

JOB = "https://www.linkedin.com/jobs/view/123/"
SEARCH = "https://www.linkedin.com/jobs/search/?keywords=test"
MODERN_JOB = "https://www.linkedin.com/jobs/view/321/"
MODERN_HTML = """<html><body>
<div><div><div><p><a href='/company/fixture/'>Modern Company</a></p>
<div data-display-contents='true'><p>Modern Analyst</p></div>
<p>India · 1 hour ago</p></div>
<a href='/jobs/view/321/'>On-site</a><a href='/jobs/view/321/'>Full-time</a></div></div>
<div><div><h2>About the job</h2></div><p>Complete modern description with SQL skills.</p></div>
</body></html>"""
MODERN_SEARCH = """<html><body><div style='height:80px;overflow:auto'>
<div role='button' componentkey='job-card-component-ref-123' style='height:100px'>First</div>
<div role='button' componentkey='job-card-component-ref-321' style='height:100px'>Second</div>
</div><a href='/jobs/view/999/'>Unrelated recommendation</a></body></html>"""
JOB_HTML = """<html><body>
<div class="jobs-description-content__text" style="display:none">hidden template</div>
<h1 class="top-card-layout__title">Data Analyst</h1>
<a class="topcard__org-name-link">Fixture Company</a>
<section class="jobs-description">
<h2>About the job</h2>
<div class="jobs-description-content__text">SQL and Python.
<span id="extra" style="display:none">Full responsibilities and qualifications.</span></div>
<button onclick="document.getElementById('extra').style.display='inline'; this.hidden=true">Show more</button>
</section>
<button onclick="document.title='WRONG BUTTON'">Show more</button>
</body></html>"""
SEARCH_HTML = """<html><body><div class="jobs-search-results-list" style="height:200px;overflow:auto">
<a href="/jobs/view/123/">First</a><a href="/jobs/view/456/">Second</a></div>
<a href="/jobs/view/999/">Unrelated recommendation</a>
<button aria-label="View next page" onclick="
document.querySelector('.jobs-search-results-list').innerHTML='<a href=/jobs/view/789/>Third</a>';
this.disabled=true;">Next</button></body></html>"""


def main():
    chrome = Path(os.environ.get("ProgramFiles", "/nonexistent")) / "Google/Chrome/Application/chrome.exe"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(chrome) if chrome.exists() else None, headless=True)
        try:
            context = browser.new_context(service_workers="block")
            def route_request(route):
                url = route.request.url
                if url == JOB:
                    route.fulfill(status=200, content_type="text/html", body=JOB_HTML)
                elif url == MODERN_JOB:
                    route.fulfill(status=200, content_type="text/html", body=MODERN_HTML)
                elif url.endswith('keywords=modern'):
                    route.fulfill(status=200, content_type="text/html", body=MODERN_SEARCH)
                elif url.startswith(SEARCH):
                    route.fulfill(status=200, content_type="text/html", body=SEARCH_HTML)
                elif "/checkpoint/" in url:
                    route.fulfill(status=200, content_type="text/html", body="verification required")
                elif "/jobs/view/429/" in url:
                    route.fulfill(status=429, content_type="text/html", body="too many requests")
                else:
                    route.abort()
            context.route("**/*", route_request)
            page = context.new_page()
            result = reader.read_job(page, JOB)
            assert result["title"] == "Data Analyst", result
            assert result["company"] == "Fixture Company", result
            assert "Full responsibilities and qualifications." in result["description"], result
            assert "hidden template" not in result["description"], result
            assert "About the job" not in result["description"], result
            assert page.title() != "WRONG BUTTON"
            reader.navigate(page, SEARCH)
            assert reader.collect_links(page, 2) == [JOB, "https://www.linkedin.com/jobs/view/456/"]
            assert reader.next_results(page)
            assert reader.job_links(page) == ["https://www.linkedin.com/jobs/view/789/"]
            assert not reader.next_results(page)
            reader.navigate(page, 'https://www.linkedin.com/jobs/search/?keywords=modern')
            assert reader.job_links(page) == [JOB, MODERN_JOB]
            owned = []
            worker = reader.DetailReader(context, owned)
            modern = worker.read(MODERN_JOB)
            assert modern['title'] == 'Modern Analyst', modern
            assert modern['company'] == 'Modern Company', modern
            assert modern['description'] == 'Complete modern description with SQL skills.', modern
            worker.page.close()
            assert worker.read(JOB)['title'] == 'Data Analyst'
            assert len(owned) == 2
            for url in ("https://www.linkedin.com/checkpoint/challenge", "https://www.linkedin.com/jobs/view/429/"):
                try:
                    reader.navigate(page, url)
                except reader.AccessBlocked:
                    pass
                else:
                    raise AssertionError(f"Expected access stop for {url}")
            print("Browser fixtures passed: legacy/modern descriptions, component-key cards, closed-tab recovery, pagination, checkpoint and HTTP 429 stops.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
