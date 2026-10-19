# Post Reader

A local Python + Playwright dashboard for collecting LinkedIn job listings and Internshala internships into JSON for later review.

## Features

- Windows one-click launcher and a localhost dashboard at http://127.0.0.1:8765.
- Choose a website, paste a filtered search URL, and set a target and delay.
- Start/Stop controls, saved/attempted/error/queued/skipped counts, descriptions and JSON downloads.
- Resume interrupted runs from saved checkpoints.
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

Failures and empty descriptions do not enter global success history. Attempted failures remain skipped within the same resumed checkpoint; an independent run can retry them. Nonempty but incomplete descriptions can enter history; there is currently no dedicated refresh-incomplete control.

The browser profile, results, database, logs, settings and local code-review package are excluded from Git. Keep the profile and local debug port private.

## CLI

With debug-enabled Chrome running (the dashboard starts it automatically):

```powershell
.\.venv\Scripts\python.exe reader.py --url "https://www.linkedin.com/jobs/search/?keywords=data%20analyst&location=India" --limit 100 --delay 3
.\.venv\Scripts\python.exe reader.py --url "https://internshala.com/internships/work-from-home-internships/" --limit 100
```

Quote URLs in PowerShell because ampersands are shell operators. `--cdp` defaults to http://127.0.0.1:9222. `--output` selects a compatible checkpoint file. `--fresh` creates a separate timestamped file; combined with explicit `--output`, it overwrites that selected output. Global history still applies.

Optional `--launch` uses Playwright Chromium instead of CDP and requires `python -m playwright install chromium` plus login in that separate profile. Ordinary Chrome cannot be attached without remote debugging enabled.

## Validation and limitations

The offline unit suites cover URL normalization, checkpoints, browser lifecycle mocks, extraction mocks, pagination mocks and persistent history. The most recent offline execution passed 36 tests:

```powershell
python -m unittest discover -s tests -p "test_reader.py" -v
python -m unittest discover -s tests -p "test_internshala.py" -v
python -m unittest discover -s tests -p "test_seen_postings.py" -v
```

The full suite additionally exercises localhost HTTP APIs:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tests/browser_smoke.py
python tests/dashboard_browser_smoke.py
python tests/internshala_browser_smoke.py
```

Browser scripts use local intercepted fixtures, not live job collection. They require Chrome. The latest Internshala pagination change has unit coverage but has not been confirmed on a live signed-in page. Passing fixtures or mocks does not prove current website compatibility.

A 1,000-listing target is not guaranteed or verified. Available results, ranking and access limits are website-controlled. LinkedIn descriptions have sometimes remained truncated despite expansion attempts; review output quality before relying on it. Newest-first sorting is requested for LinkedIn but promoted/personalized results can differ. Selectors may need maintenance.

Login, verification and denied/rate-limited responses stop collection; there is no access-control bypass. Five consecutive description failures also stop a run. Partial results are kept. Use only where permitted by the relevant website.

See [RESEARCH.md](RESEARCH.md) for earlier repository comparisons and design context. Those projects were inspected rather than installed/run; the report is not a current live verification of alternatives.
