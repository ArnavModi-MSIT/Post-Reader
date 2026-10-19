"""Check Internshala selectors against local fixture pages, without network access."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import internshala
import reader
from playwright.sync_api import sync_playwright

SEARCH = internshala.DEFAULT_SEARCH
JOB1 = "https://internshala.com/internship/detail/work-from-home-data-analytics-internship-at-fixture1775475701"
JOB2 = "https://internshala.com/internship/detail/work-from-home-python-internship-at-fixture1775475702"
LIST1 = f'''<html><body><div class="individual_internship" data-href="{JOB1}">
<a href="{JOB1}?utm_source=test">Data Analytics</a></div>
<button id="navigation-forward" style="display:none">Next</button>
<a class="next_page" href="{SEARCH}page-2/">Next</a></body></html>'''
LIST2 = f'''<html><body><a href="{JOB2}">Python</a><button id="navigation-forward" disabled>Next</button></body></html>'''
DETAIL = '''<html><head><title>Data Analytics internship</title></head><body>
<h1 class="heading_4_5 profile">Data Analytics</h1><div class="company_name"><a>Fixture Company</a></div>
<a class="location_link">Work from home</a>
<div class="other_detail_item"><div class="item_heading">DURATION</div><div class="item_body">3 Months</div></div>
<div class="other_detail_item"><div class="item_heading">STIPEND</div><div class="item_body">INR 10,000 /month</div></div>
<div class="other_detail_item"><div class="item_heading">APPLY BY</div><div class="item_body">25 Oct</div></div>
<div class="internship_other_details"><h2>About the internship</h2><p>Build SQL dashboards and Python data pipelines.</p>
<div class="skills_container"><span class="round_tabs">SQL</span><span class="round_tabs">Python</span></div>
<h3>Who can apply</h3><p>Available for a three-month internship.</p>
<h3>Perks</h3><span class="round_tabs">Certificate</span></div></body></html>'''


def main():
    with sync_playwright() as p:
        chrome=Path(os.environ["ProgramFiles"])/"Google/Chrome/Application/chrome.exe"
        browser=p.chromium.launch(executable_path=str(chrome),headless=True)
        try:
            context=browser.new_context(service_workers="block")
            def route(request):
                url=request.request.url
                body = LIST1 if url == SEARCH else LIST2 if url == SEARCH + 'page-2/' else DETAIL if url in (JOB1, JOB2) else '<title>Just a moment...</title>'
                request.fulfill(status=200,content_type='text/html',body=body)
            context.route('**/*',route)
            page=context.new_page()
            reader.navigate(page,SEARCH)
            assert reader.collect_links(page,10)==[JOB1]
            assert reader.next_results(page)
            assert reader.job_links(page)==[JOB2]
            assert not reader.next_results(page)
            job=reader.read_job(page,JOB1)
            assert job['title']=='Data Analytics' and job['company']=='Fixture Company', job
            assert job['source']=='internshala' and job['job_id']=='1775475701'
            assert job['duration']=='3 Months' and job['stipend']=='INR 10,000 /month'
            assert job['skills']==['SQL','Python'], job['skills']
            assert 'Who can apply' in job['description'] and 'three-month internship' in job['description']
            try:
                reader.navigate(page,'https://internshala.com/internships/blocked/')
            except reader.AccessBlocked:
                pass
            else:
                raise AssertionError('Verification page was not stopped')
            print('Internshala browser fixtures passed: links, pagination, details, stipend, duration, skills, criteria and verification stop.')
        finally:
            browser.close()


if __name__=='__main__':
    main()
