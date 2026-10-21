# Changes

## 2026-10-21 — Three readers and durable Windows saves

- Support one, two or three independent detail readers through the dashboard and CLI.
- Retry transient Windows checkpoint replacement locks with unique, flushed temporary files; preserve recovery snapshots after persistent locks.
- Fix a parallel-future polling race and reject invalid worker counts before starting threads.
- Skip malformed local result files and recover dashboard defaults from invalid settings.
- Keep failed checkpoint retries within their original source.
- Add checkpoint-lock, three-reader CDP, timeout-race and malformed-file regression coverage.

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
