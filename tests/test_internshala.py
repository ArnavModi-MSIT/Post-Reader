import argparse
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import dashboard
import internshala
import reader

SEARCH = internshala.DEFAULT_SEARCH
JOB = "https://internshala.com/internship/detail/work-from-home-data-analytics-internship-at-fixture1775475701"


class InternshalaTests(unittest.TestCase):
    def test_jobs_search_preserves_salary_filter(self):
        search = "https://internshala.com/jobs/work-from-home/salary-6"
        self.assertEqual(reader.search_url(search), search + "/")
        self.assertEqual(reader.search_url(search + "/page-3/?keywords=python"), search + "/?keywords=python")
        self.assertEqual(reader.search_url("https://internshala.com/fresher-jobs/work-from-home/"),
                         "https://internshala.com/fresher-jobs/work-from-home/")

    def test_job_detail_identity_does_not_collide_with_internship(self):
        from posting_identity import posting_key
        from seen_postings import SeenPostings
        job = "https://internshala.com/job/detail/remote-analyst123"
        internship = "https://internshala.com/internship/detail/analyst123"
        self.assertEqual(reader.canonical_job_url(job + "?tracking=test"), job)
        self.assertEqual(posting_key(job), posting_key(job.replace("/job/", "/fresher-job/")))
        self.assertNotEqual(posting_key(job), posting_key(internship))
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            with SeenPostings(Path(folder) / "history.sqlite3") as history:
                history.remember({"link": job, "title": "Analyst", "description": "Full responsibilities."}, "jobs.json")
                self.assertTrue(history.contains(job))
                self.assertFalse(history.contains(internship))

    def test_search_is_supported_and_page_cursor_resets(self):
        self.assertEqual(reader.search_url(SEARCH), SEARCH)
        self.assertEqual(reader.search_url(SEARCH + "page-3/?keywords=python"), SEARCH + "?keywords=python")
        self.assertEqual(reader.search_url("https://www.internshala.com/internships/work-from-home-internships/"), SEARCH)

    def test_internship_link_canonicalization_and_host_validation(self):
        self.assertEqual(reader.canonical_job_url(JOB + "/?utm_source=test"), JOB)
        self.assertIsNone(reader.canonical_job_url(JOB.replace("internshala.com", "internshala.com.evil.example")))
        for url in ("https://internshala.com/student/dashboard/", "https://user:password@internshala.com/internships/", JOB):
            with self.subTest(url=url), self.assertRaises(argparse.ArgumentTypeError):
                reader.search_url(url)

    def test_site_files_are_separate_and_linkedin_name_is_preserved(self):
        self.assertTrue(reader.daily_output_path(SEARCH).name.startswith("internshala_"))
        self.assertTrue(reader.daily_output_path(reader.DEFAULT_SEARCH).name.startswith("jobs_"))
        self.assertNotEqual(reader.daily_output_path(SEARCH), reader.daily_output_path(reader.DEFAULT_SEARCH))

    def test_detail_and_list_calls_route_to_internshala(self):
        page = Mock(url=SEARCH)
        with patch.object(internshala, "read_job", return_value={"source": "internshala"}) as read:
            self.assertEqual(reader.read_job(page, JOB)["source"], "internshala")
            read.assert_called_once_with(page, JOB)
        with patch.object(internshala, "collect_links", return_value=[JOB]):
            self.assertEqual(reader.collect_links(page, 10), [JOB])
        with patch.object(internshala, "next_results", return_value=False):
            self.assertFalse(reader.next_results(page))

    def test_block_page_is_reported_as_internshala(self):
        page = Mock(url=SEARCH)
        page.is_closed.return_value = False
        page.title.return_value = "Just a moment..."
        with self.assertRaisesRegex(reader.AccessBlocked, "Internshala"):
            reader.check_access(page)

    def test_internshala_checkpoint_resumes_with_its_source(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            args = SimpleNamespace(output=Path(folder)/"internshala.json", fresh=False, limit=10)
            result = {"jobs": [], "errors": []}
            reader.prepare_output(args, result, SEARCH)
            result["jobs"] = [{"link": JOB, "source": "internshala"}]
            reader.save_result(args.output, result)
            resumed = {}
            reader.prepare_output(args, resumed, SEARCH)
            self.assertEqual(resumed["source"], "internshala")
            self.assertEqual(resumed["jobs"][0]["link"], JOB)

    def test_dashboard_accepts_and_remembers_internshala(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            gate = threading.Event()
            def execute(args):
                gate.wait(2)
                return {"status": "interrupted", "stop_reason": "test complete"}, 130
            manager = dashboard.Manager(folder, execute=execute, bootstrap=lambda: None)
            try:
                manager.start({"url": SEARCH, "limit": 10, "delay": 1})
                self.assertEqual(manager.args.url, SEARCH)
                self.assertTrue(manager.args.output.name.startswith("internshala_"))
                self.assertEqual(manager.settings()["url"], SEARCH)
                self.assertEqual(manager.snapshot()["source"], "internshala")
            finally:
                gate.set()
                manager.thread.join(timeout=3)

    def test_detail_fields_and_eligibility_are_kept(self):
        page = Mock(url=JOB)
        item = Mock()
        skill_region = Mock()
        skill_region.count.return_value = 1
        skill_region.locator.return_value.all_inner_texts.return_value = ['SQL', 'Python']
        def locate(selector):
            if selector == '.other_detail_item':
                value = Mock()
                value.all.return_value = [item]
                return value
            if selector == '.skills_container, .skill_container':
                return skill_region
            value = Mock()
            value.all_inner_texts.return_value = ['Work from home']
            return value
        page.locator.side_effect = locate
        page.get_by_text.return_value.count.return_value = 0
        def extract(scope, selectors):
            if selectors == '.item_heading': return 'DURATION'
            if selectors == '.item_body': return '3 Months'
            if selectors == '.stipend': return 'INR 10,000 /month'
            if 'profile' in selectors: return 'Data Analytics'
            if 'company_name' in selectors: return 'Fixture Company'
            return 'Build Python pipelines. Who can apply: available for three months. Perks: certificate.'
        with patch.object(reader, 'navigate'), patch.object(reader, 'wait_for_content'), patch.object(reader, 'first_text', side_effect=extract):
            record = internshala.read_job(page, JOB)
        self.assertEqual(record['source'], 'internshala')
        self.assertEqual(record['title'], 'Data Analytics')
        self.assertEqual(record['company'], 'Fixture Company')
        self.assertEqual(record['duration'], '3 Months')
        self.assertEqual(record['stipend'], 'INR 10,000 /month')
        self.assertEqual(record['skills'], ['SQL', 'Python'])
        self.assertIn('Who can apply', record['description'])

    def test_pagination_preserves_next_page_target(self):
        page = Mock(url=SEARCH)
        next_button = Mock()
        next_button.get_attribute.side_effect = lambda key: {'class':'next', 'aria-disabled':None, 'href':SEARCH+'page-2/'}.get(key)
        next_button.locator.return_value.count.return_value = 0
        next_button.is_visible.return_value = True
        next_button.is_enabled.return_value = True
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.all.return_value = [next_button]
        with patch.object(internshala, 'job_links', side_effect=[[JOB], [JOB+'2']]), \
                patch.object(reader, 'check_access'), patch.object(reader, 'navigate') as navigate:
            self.assertTrue(internshala.next_results(page))
        navigate.assert_called_once_with(page, SEARCH+'page-2/')

    def test_scroll_waits_through_delayed_batch(self):
        page = Mock(url=SEARCH)
        with patch.object(internshala, "job_links", side_effect=[[JOB]] * 6 + [[JOB, JOB+"2"]]), \
                patch.object(reader, "check_access"), patch.object(reader, "interruptible_wait") as wait:
            result = internshala.scroll_more(page, timeout=5)
        self.assertTrue(result)
        self.assertEqual(result.method, "scroll")
        self.assertGreaterEqual(wait.call_count, 5)

    def test_no_pager_uses_scroll_loader(self):
        from pagination import PaginationResult
        page = Mock(url=SEARCH)
        page.locator.return_value.all.return_value = []
        page.get_by_role.return_value.all.return_value = []
        with patch.object(internshala, "job_links", return_value=[JOB]), \
                patch.object(internshala, "scroll_more", return_value=PaginationResult("advanced", SEARCH, 2)) as scroll:
            self.assertTrue(internshala.next_results(page))
        scroll.assert_called_once_with(page)

    def test_scroll_idle_and_cancellation(self):
        page = Mock(url=SEARCH)
        with patch.object(internshala, "job_links", return_value=[JOB]), \
                patch.object(reader, "check_access"), patch.object(reader, "interruptible_wait"):
            result = internshala.scroll_more(page, timeout=1)
        self.assertEqual(result.status, "scroll_idle")
        self.assertNotIn("Next control", result.reason)
        event = threading.Event()
        event.set()
        with patch.object(internshala, "job_links", return_value=[JOB]), self.assertRaises(KeyboardInterrupt):
            internshala.scroll_more(page, stop_event=event)

    def test_stopped_collection_makes_no_browser_requests(self):
        event = threading.Event()
        event.set()
        page = Mock(url=SEARCH)
        with self.assertRaises(KeyboardInterrupt):
            internshala.collect_links(page, 10, event)
        page.evaluate.assert_not_called()

    def pagination_page(self, current=SEARCH, href=None, disabled=False):
        page = Mock(url=current)
        page.locator.return_value.all.return_value = []
        page.get_by_role.return_value.all.return_value = []
        control = Mock()
        control.is_visible.return_value = True
        control.is_enabled.return_value = True
        control.locator.return_value.count.return_value = int(disabled)
        control.get_attribute.side_effect = lambda key: {'href': href, 'class': 'next'}.get(key)
        return page, control

    def test_plain_next_link_is_used_when_id_is_missing(self):
        page, control = self.pagination_page(href=SEARCH+'page-2/')
        links = Mock()
        links.all.return_value = [control]
        empty = Mock()
        empty.all.return_value = []
        page.get_by_role.side_effect = lambda role, **kwargs: links if role == 'link' else empty
        with patch.object(internshala, 'job_links', side_effect=[[JOB], [JOB+'2']]), \
                patch.object(reader, 'check_access'), patch.object(reader, 'navigate') as navigate:
            self.assertTrue(internshala.next_results(page))
        navigate.assert_called_once_with(page, SEARCH+'page-2/')

    def test_numeric_link_preserves_filters_and_page_cursor(self):
        current = SEARCH+'stipend-10000/page-2/?keywords=python'
        target = SEARCH+'stipend-10000/page-3/?keywords=python'
        page, control = self.pagination_page(current, target)
        numeric = Mock()
        numeric.all.return_value = [control]
        empty = Mock()
        empty.all.return_value = []
        page.locator.side_effect = lambda selector: numeric if 'href*=' in selector else empty
        with patch.object(internshala, 'job_links', side_effect=[[JOB], [JOB+'2']]), \
                patch.object(reader, 'check_access'), patch.object(reader, 'navigate') as navigate:
            self.assertTrue(internshala.next_results(page))
        navigate.assert_called_once_with(page, target)

    def test_disabled_parent_does_not_follow_next_link(self):
        page, control = self.pagination_page(href=SEARCH+'page-2/', disabled=True)
        page.get_by_role.return_value.all.return_value = [control]
        with patch.object(internshala, 'job_links', return_value=[JOB]), \
                patch.object(reader, 'navigate') as navigate:
            self.assertFalse(internshala.next_results(page))
        navigate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
