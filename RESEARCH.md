# Local job-reader research - 18 October 2026

The implementation files below were inspected, not just the project READMEs. Their code was not installed or run. These comparisons describe their source, not verified live success or a guaranteed collection volume.

| Project | What the inspected code does | Relevant limitation |
| --- | --- | --- |
| [JobSpy](https://github.com/speedyapply/JobSpy) | Uses requests and BeautifulSoup against LinkedIn's guest search endpoint; advances an offset, deduplicates job IDs, and optionally reads each job page for its description. | An HTTP approach avoids browser-tab lifecycle problems but still depends on what LinkedIn serves. Its code stops on request errors, unexpected empty results, or exhausted results. It does not reproduce a signed-in personalized search. |
| [joeyism/linkedin_scraper](https://github.com/joeyism/linkedin_scraper) | Uses asynchronous Playwright. The job search scrolls, extracts job links, and deduplicates them; a separate scraper reads job information, including semantic “About the job” content. | Its inspected search method scrolls at most three times and has no multi-page loop. A large `limit` alone does not establish 1,000 results. |
| [spinlud/py-linkedin-jobs-scraper](https://github.com/spinlud/py-linkedin-jobs-scraper) | Uses Selenium, supports persistent Chrome profiles, and implements result-page offsets, job-ID extraction, pacing, and waits for lists to settle. | Its many selectors and session-handling branches show why this needs ongoing maintenance. We did not install it or adopt its authentication handling. |

Source files inspected:

- [JobSpy LinkedIn implementation](https://github.com/speedyapply/JobSpy/blob/main/jobspy/linkedin/__init__.py) and [pagination constants](https://github.com/speedyapply/JobSpy/blob/main/jobspy/linkedin/constant.py).
- [Playwright job search](https://github.com/joeyism/linkedin_scraper/blob/master/linkedin_scraper/scrapers/job_search.py), [job detail extraction](https://github.com/joeyism/linkedin_scraper/blob/master/linkedin_scraper/scrapers/job.py), and [browser lifecycle](https://github.com/joeyism/linkedin_scraper/blob/master/linkedin_scraper/core/browser.py).
- [Selenium browser orchestration](https://github.com/spinlud/py-linkedin-jobs-scraper/blob/master/linkedin_jobs_scraper/linkedin_scraper.py) and [authenticated results strategy](https://github.com/spinlud/py-linkedin-jobs-scraper/blob/master/linkedin_jobs_scraper/strategies/authenticated_strategy.py).

## Decision for this project

Keep the existing Python + Playwright + Chrome reader. It runs on this computer, stores JSON locally, and requires no paid API, hosted scraper, external AI service, or proxy subscription. It still needs internet access to LinkedIn. We used the research to guide lifecycle handling and data validation; no third-party repository implementation was copied into the reader.

The user's actual page was also inspected. LinkedIn redirected the old search URL to `/jobs/search-results/`, with job IDs exposed in DOM `componentkey="job-card-component-ref-..."` attributes. Its descriptions used an “About the job” heading rather than the old description classes. The reader now supports this observed markup alongside the older layout.

The failed run's JSON contained zero successful jobs, two closed-target navigation errors, and a later pagination error. The precise reason the tab closed was not captured. The fixes address closed detail tabs, new-layout extraction, and saving/resuming the pending queue. Verification pages and denied/rate-limited responses still stop collection.

No project or local setup can promise that LinkedIn will expose 1,000 readable postings every day. Treat that as a target, and judge each run by its saved count and stop reason.
