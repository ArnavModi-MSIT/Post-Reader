# Changes

## 2026-10-19 — Reliability and repair

- Preserve existing checkpoints on Chrome/SQLite/startup errors and reject mismatched search checkpoints.
- Centralize strict posting identities and isolate LinkedIn description extraction.
- Expand ellipsis/More controls, wait for rendered changes and label suspected truncation.
- Back up/migrate history, audit old JSON and repair warned descriptions without deleting identities or original exports.
- Retry transient reads with bounded backoff and retry failed checkpoint links on resume.
- Record typed pagination outcomes and compact control diagnostics; try other real controls after a failed candidate.
- Improve cooperative cancellation and prevent concurrent collector processes.
- Add dashboard quality/retry/page counts, search, quality filters and paged saved-job browsing.
- Bound dashboard result caching and history parsing; fix HTTP rejection resets on Windows.
- Add regression and real-browser fixtures plus an offline GitHub Actions workflow.

These checks do not establish live source compatibility or an uninterrupted 1,000-listing run.
