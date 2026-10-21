"""Test the complete localhost UI with a fake reader, never LinkedIn."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard
import reader
from playwright.sync_api import expect, sync_playwright


def main():
    with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
        received = []
        def fake_run(args):
            received.append(args)
            source = dashboard.source_name(args.url)
            result = {"status": "running", "source": source, "requested_limit": args.limit, "search_url": args.url,
                      "jobs": [], "errors": [], "progress": {"pending_links": [], "attempted_links": []}}
            for number in range(2):
                result["jobs"].append({"job_id": str(number), "title": f"Fixture Analyst {number}",
                                       "company": "Local Fixture", "description": "Complete fixture description with SQL and Python.",
                                       "source": source,
                                       "link": f"https://internshala.com/internship/detail/test{1000+number}" if source == 'internshala' else f"https://www.linkedin.com/jobs/view/{number}/"})
                result["progress"]["attempted_links"].append(str(number))
                reader.save_result(args.output, result)
                if args.stop_event.wait(0.5):
                    break
            args.stop_event.wait(15)
            result.update(status="interrupted", stop_reason="Stopped by user.")
            reader.save_result(args.output, result)
            return result, 130
        manager = dashboard.Manager(folder, execute=fake_run, bootstrap=lambda: None)
        server = dashboard.create_server(0, manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with sync_playwright() as p:
            chrome = Path(os.environ.get("ProgramFiles", "/nonexistent")) / "Google/Chrome/Application/chrome.exe"
            browser = p.chromium.launch(executable_path=str(chrome) if chrome.exists() else None, headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                expect(page.locator('#search-url')).not_to_have_value('')
                page.locator('#search-url').fill('https://www.linkedin.com/jobs/search/?keywords=python&location=India')
                page.locator('#limit').fill('10')
                page.locator('#delay').fill('1')
                page.locator('#workers').select_option('3')
                page.get_by_role('button', name='Start collection').click()
                expect(page.locator('#saved')).to_have_text('2')
                assert 'keywords=python' in received[0].url
                assert received[0].workers == 3
                assert page.locator('#start').is_disabled()
                page.get_by_role('button', name='View description').first.click()
                page.locator('#job-dialog').wait_for(state='visible')
                assert 'SQL and Python' in page.locator('#detail-description').inner_text()
                page.get_by_role('button', name='Close job description').click()
                with page.expect_download() as downloaded:
                    page.get_by_role('link', name='Download JSON').click()
                value = json.loads(Path(downloaded.value.path()).read_text(encoding='utf-8'))
                assert len(value['jobs']) == 2
                page.get_by_role('button', name='Stop', exact=True).click()
                expect(page.locator('#status')).to_have_text('interrupted')
                assert page.locator('#saved').inner_text() == '2'
                assert not page.locator('#start').is_disabled()
                expect(page.locator('#complete_candidates')).to_have_text('2')
                page.locator('#job-query').fill('Analyst 0')
                expect(page.locator('#jobs .job-row')).to_have_count(1)
                page.locator('#job-quality').select_option('suspected_truncated')
                expect(page.locator('#jobs .job-row')).to_have_count(0)
                page.locator('#job-quality').select_option('all')
                page.locator('#job-query').fill('')
                expect(page.locator('#jobs .job-row')).to_have_count(2)
                page.get_by_role('button', name='Inspect').first.click()
                page.locator('#follow').wait_for(state='visible')
                page.locator('#retry-incomplete').check()
                page.locator('#source').select_option('internshala')
                expect(page.locator('#search-url')).to_have_value(reader.internshala.DEFAULT_SEARCH)
                page.get_by_role('button', name='Start collection').click()
                expect(page.locator('#run-source')).to_have_text('Internshala')
                expect(page.locator('#saved')).to_have_text('2')
                assert received[-1].url == reader.internshala.DEFAULT_SEARCH
                assert received[-1].retry_incomplete is True
                assert received[-1].output.name.startswith('repair_')
                page.get_by_role('button', name='View description').first.click()
                expect(page.locator('#detail-link')).to_have_attribute('href', 'https://internshala.com/internship/detail/test1001')
                page.get_by_role('button', name='Close job description').click()
                page.get_by_role('button', name='Stop', exact=True).click()
                expect(page.locator('#status')).to_have_text('interrupted')
                page.locator('#source').select_option('internshala_jobs')
                expect(page.locator('#search-url')).to_have_value(reader.internshala.DEFAULT_JOBS_SEARCH)
                page.set_viewport_size({"width": 390, "height": 844})
                overflow = page.evaluate("""() => [...document.querySelectorAll('body *')]
                    .filter(el=>el.getBoundingClientRect().right > window.innerWidth + 1)
                    .slice(0,10).map(el=>({tag:el.tagName,cls:el.className,width:el.getBoundingClientRect().width,right:el.getBoundingClientRect().right}))""")
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), overflow
                assert not errors, errors
                print('Dashboard browser passed: both website choices, edited URLs, Start/Stop, counts, description dialogs, JSON download, history and mobile layout. No job-site requests.')
            finally:
                manager.stop()
                if manager.thread:
                    manager.thread.join(timeout=3)
                browser.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == '__main__':
    main()
