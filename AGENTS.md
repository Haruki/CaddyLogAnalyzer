# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project State (April 2026)

- Small Python utility repository for reading Caddy access logs.
- No package layout, no tests, and minimal README.
- Main scripts are standalone entry points.

## Repository Map

- `caddylog.py`: Batch parser for JSON-lines access log. Builds in-memory map of `remote_ip -> [(uri, status), ...]` and prints records.
- `caddylog_seek.py`: Intended tail/follow mode parser (`tail -f` style) for the same log format.
- `watchdog.py`: File change watcher prototype using the `watchdog` library.
- `README.md`: Minimal one-line description.

## Runtime Assumptions

- Current scripts hardcode log path `/mnt/d/access.log`.
- Input format is JSON per line, with keys:
  - `request.remote_ip`
  - `request.uri`
  - `status`
- Private LAN clients (`192.168*`) and `None` remote IPs are ignored.
- Parse errors and missing keys are silently ignored.

## Dependencies

- Standard library: `json`, `time`.
- Third-party: `watchdog` (needed by `watchdog.py`).

## Known Issues To Consider Before Editing

1. `caddylog_seek.py` calls `add_data(...)` but does not define or import it.
2. `caddylog_seek.py` has duplicate `import json`.
3. `watchdog.py` uses `time.sleep(...)` but does not import `time`.
4. `watchdog.py` schedules observer with `path="/mnt/d/access.log"`; watchdog scheduling is typically directory-based, so behavior may vary by platform.
5. No automated tests currently exist.

## Agent Working Agreement

1. Keep changes minimal and focused; avoid broad refactors unless requested.
2. Preserve existing behavior by default (especially filtering and error-tolerance semantics).
3. Do not silently change hardcoded paths unless requested; prefer adding opt-in configurability.
4. If introducing CLI options, use backward-compatible defaults that match current behavior.
5. If adding dependencies, document them clearly in README and usage notes.

## Verification Checklist

Run these checks after edits:

```bash
python3 -m py_compile caddylog.py caddylog_seek.py watchdog.py
```

If behavior changes in parsers, also do a smoke run against a sample log file and verify:

- malformed JSON lines are skipped without crashing
- missing keys are skipped without crashing
- LAN IP filtering still works
- output includes expected `remote_ip`, `uri`, and `status`

## Suggested Next Improvements (When Requested)

1. Introduce shared parsing utilities to remove duplication between batch and tail modes.
2. Add CLI argument for log path instead of hardcoded `/mnt/d/access.log`.
3. Add tests for parsing and filtering behavior.
4. Improve README with setup, dependency install, and example usage.
