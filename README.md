# Post Reader

A local Python + Playwright dashboard for collecting LinkedIn job listings and Internshala jobs/internships into JSON for later review.

## Features

- Windows one-click launcher and a localhost dashboard at http://127.0.0.1:8765.
- Choose a website, paste a filtered search URL, and set a target and delay.
- Start/Stop controls, saved/attempted/error/queued/skipped counts, descriptions and JSON downloads.
- Resume interrupted runs from validated checkpoints, with startup failures preserving existing files.
- Description-quality warnings, an optional repair pass, saved-job search, quality filtering and pagination diagnostics.
- Bounded retries for transient failures; one collector process per data directory.
- Persistent SQLite history skips successfully saved posting IDs across days, searches and fresh runs.
- Reuses a dedicated, manually logged-in Chrome profile through local CDP. No paid APIs or cloud backend.

## Setup

Requires Windows, Python 3.10+ and Google Chrome.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Double-click **Run_Reader.cmd**. Click **Start collection** to launch or reuse the dedicated Chrome session. On first use, log into the selected website in that Chrome window. If collection stopped for login or verification, finish it manually and start again.

This Chrome profile is separate from your normal Chrome profile. Keep Chrome and the dashboard terminal open while collecting. There are no terminal Enter prompts. **Stop** is cooperative and may wait for the current browser operation. Press Ctrl+C in the terminal to stop the dashboard. Restart the dashboard after updating Python files.

Example searches:

- [LinkedIn data analyst jobs](https://www.linkedin.com/jobs/search/?keywords=data%20analyst&location=India)
- [Internshala remote internships](https://internshala.com/internships/work-from-home-internships/)

The dashboard binds only to 127.0.0.1. To use another port:

```powershell
.\.venv\Scripts\python.exe dashboard.py --port 8766 --open
```

## Output and duplicate history

Daily results are stored in `data/jobs_YYYY-MM-DD_searchhash.json` or `data/internshala_YYYY-MM-DD_searchhash.json`, using the India date. JSON includes title, company, available description, link, available metadata, errors, status, stop reason and a resume checkpoint. Internshala also includes available stipend, duration, deadline and skills. Missing fields are not inferred.

Matching daily searches resume their existing file. Uncheck **Resume today's progress** to create a new run. Resuming counts existing jobs toward that file's target.

`data/seen_postings.sqlite3` tracks website + posting ID across runs. Existing result JSON in `data/` is imported automatically. Tracking parameters do not create new identities. Keep this database to preserve history after deleting old JSON. A new posting ID or another website counts as a separate listing. Fresh runs still skip globally saved postings.

Description records carry `description_status` (`complete_candidate`, `suspected_truncated`, or `missing`), `description_length`, and extraction metadata where available. A complete candidate passed local checks; it is not a guarantee that the website exposed its entire description. Ellipsis/More suffixes and remaining expansion controls trigger warnings.

Normal collection skips previously saved identities, including warned records, so daily searches do not collect them repeatedly. Check **Repair incomplete descriptions** to re-read warned records for the selected website across all searches. This is a separate repair pass, up to the selected target; it writes a new `repair_*.json` file and upgrades the existing SQLite identity when extraction improves. Original JSON files remain unchanged. Do not select an existing file as the repair output.

On first use of the new history schema, an existing database is backed up as `data/seen_postings_before_quality_v2.sqlite3`. Existing result JSON is re-audited once to populate quality states. Legacy identities without accessible source JSON remain protected against duplicates but cannot be assessed automatically. A later incomplete result does not downgrade an existing complete candidate.

Failed reads are retried when their checkpoint is resumed. Transient network/timeouts receive one additional attempt by default with bounded backoff; layout failures are reported directly. Login/verification and denied responses stop collection. JSON records precede history updates, and startup import reconciles saved outputs. A local OS file lock prevents two new collector processes from writing concurrently.

The browser profile, results, database, logs, settings and local code-review package are excluded from Git. Keep the profile and local debug port private.

## CLI

With debug-enabled Chrome running (the dashboard starts it automatically):

```powershell
.\.venv\Scripts\python.exe reader.py --url "https://www.linkedin.com/jobs/search/?keywords=data%20analyst&location=India" --limit 100 --delay 3
.\.venv\Scripts\python.exe reader.py --url "https://internshala.com/internships/work-from-home-internships/" --limit 100
```

Quote URLs in PowerShell because ampersands are shell operators. `--cdp` defaults to http://127.0.0.1:9222. `--retries 0..3` configures bounded transient retries. `--retry-incomplete` runs the repair pass for the selected website. `--output` selects a compatible checkpoint file. `--fresh` creates a separate timestamped file; combined with explicit `--output`, it overwrites that selected output. Global history still applies.

Optional `--launch` uses Playwright Chromium instead of CDP and requires `python -m playwright install chromium` plus login in that separate profile. Ordinary Chrome cannot be attached without remote debugging enabled.

## Validation and limitations

The offline unit suites cover URL normalization, checkpoints, browser lifecycle mocks, extraction mocks, pagination mocks and persistent history. The reliability update adds regression tests for startup data loss, quality migration, repair, strict identities, cancellation, retry limits, filtering and process locking. Run the focused suites with:

```powershell
python -m unittest discover -s tests -p "test_reader.py" -v
python -m unittest discover -s tests -p "test_internshala.py" -v
python -m unittest discover -s tests -p "test_seen_postings.py" -v
python -m unittest discover -s tests -p "test_reliability.py" -v
```

The full suite additionally exercises localhost HTTP APIs:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tests/browser_smoke.py
python tests/dashboard_browser_smoke.py
python tests/internshala_browser_smoke.py
python tests/reliability_browser_smoke.py
```

Browser scripts use local intercepted fixtures, not live job collection. They use installed Chrome when available, otherwise Playwright Chromium (`python -m playwright install chromium`). GitHub Actions runs unit/API tests and browser fixtures without signed-in sessions or job-site requests; Windows launchers still require Windows. The latest Internshala pagination change has unit coverage but has not been confirmed on a live signed-in page. Passing fixtures or mocks does not prove current website compatibility.

A 1,000-listing target is not guaranteed or verified. Available results, ranking and access limits are website-controlled. LinkedIn descriptions have sometimes remained truncated despite expansion attempts; review output quality before relying on it. Newest-first sorting is requested for LinkedIn but promoted/personalized results can differ. Selectors may need maintenance.

Login, verification and denied/rate-limited responses stop collection; there is no access-control bypass. Five consecutive description failures also stop a run. Partial results are kept. Use only where permitted by the relevant website.

See [RESEARCH.md](RESEARCH.md) for earlier repository comparisons and design context. Those projects were inspected rather than installed/run; the report is not a current live verification of alternatives.

## Source layout

- `reader.py`: CLI, browser lifecycle, checkpoint validation, retries and collection orchestration.
- `linkedin.py` / `internshala.py`: scoped description extraction and website DOM selectors.
- `posting_identity.py`: shared strict posting URL/identity parser.
- `description_quality.py`: extraction-quality warnings.
- `seen_postings.py`: SQLite identities, quality migration, import and repair candidates.
- `pagination.py`: bounded navigation, candidate fallbacks and structured outcomes.
- `run_lock.py`: OS-backed single-collector lock.
- `dashboard.py` and `web/`: local API and interface.

## Troubleshooting

- **Chrome connection unavailable:** use `Run_Reader.cmd`, keep its dedicated Chrome open, and verify the local debugging port is 9222. Ordinary Chrome is a different session.
- **Login or verification:** finish it manually in the dedicated profile, then start again. Avoid closing unrelated browser processes.
- **Pagination stopped:** expand **Pagination diagnostics**. `missing_control` does not mean all jobs were collected. Share the compact diagnostic if the layout needs an update.
- **Many descriptions need review:** use the repair checkbox with a modest target first and inspect output quality. A repair can still return an incomplete result if the source does not expand.
- **Another reader process is collecting:** stop that run or allow it to finish; the OS releases the lock after exit/crash. Deleting its lock file is unnecessary.
- **Code updated but UI is old:** stop the dashboard terminal with Ctrl+C, launch again, and refresh its browser page.

Internshala also supports infinite scrolling: each loaded batch is saved, then the reader scrolls the listing container/window and waits up to 30 seconds for new unique links. A missing Next button does not end the run. If nothing loads within that window, it reports `scroll_idle` and keeps progress for a later resume. Explicit Next controls remain supported for older layouts. The dashboard's result-batch counter includes scroll-loaded batches.

Internshala jobs are supported alongside internships. Choose **Internshala jobs** or paste `https://internshala.com/jobs/work-from-home/salary-6`. `/jobs/`, `/fresher-jobs/` and `/internships/` searches retain their filters and share scrolling, checkpoints and history. Job records include `listing_type`, available `salary` and `experience` fields. Job and internship identity namespaces are separate to avoid collisions while preserving existing internship history.

## Two detail readers

Choose **2 readers — parallel** in the dashboard, or add `--workers 2` to the CDP CLI command. Each worker owns its Playwright connection and detail tab; both attach to the same dedicated Chrome login session. One coordinator writes JSON and SQLite. The saved target, global duplicate protection and checkpoint queue still apply. Single-reader mode remains the default.

Delay applies separately to each reader. Two description requests can overlap, so actual throughput may improve, but it is not guaranteed to double. Stop or verification prevents further scheduling; in-flight browser operations may finish before worker tabs close. Unsaved work remains pending for resume. Two-reader mode also supports repair passes. It requires CDP and cannot be combined with `--launch`.

Run `python tests/parallel_browser_smoke.py` for the isolated CDP concurrency fixture; it starts temporary Chrome and never contacts a job website.
