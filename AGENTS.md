# Repository Guidelines

## Project Structure & Module Organization

- `update_strava_weather.sh` is the CLI; `strava_common.sh` handles credentials and HTTP requests. `strava_oauth.py` manages login; `strava_sync.py` coordinates discovery, retries, locking, and weather calculation.
- `weather_template.py` renders descriptions and orders summit data. `templates/weather.default.txt` defines the default output. `backfill_all.sh` and `reorder_summitbag.sh` provide maintenance commands.
- `webhook/` contains the receiver, deployment scripts, and systemd templates. `tests/` contains isolated regression suites. Personal integrations belong in machine-local webhook hooks.

## Build, Test, and Development Commands

Requires Bash, Python 3.9+, and curl; there is no build step.

```bash
bash tests/regression.sh                 # Complete offline regression suite
python3 tests/sync.py                    # Synchronization tests
python3 tests/webhook.py                 # Local HTTP webhook integration tests
python3 tests/templates.py               # Rendering and description ordering
bash -n update_strava_weather.sh strava_common.sh
git diff --check
```

For live development, configure `strava_config.sh` from its example and run `./update_strava_weather.sh login`. Preview with `./update_strava_weather.sh sync --dry-run`; this reads real APIs but does not update activities or synchronization state.

## Coding Style & Naming Conventions

Use four-space Python indentation, two-space Bash indentation, `snake_case` functions, and uppercase constants/environment variables. Prefer Python standard-library modules, explicit errors, quoted shell variables, and small changes consistent with surrounding code. No formatter or linter is configured; use Bash syntax checks and diff checks.

## Testing Guidelines

Tests use `unittest`, mocked API responses, temporary directories, and local HTTP servers. Name test methods `test_<behavior>`. Add or update tests before behavior changes, confirm the expected failure, implement, then rerun targeted tests and the full suite. No numerical coverage threshold is configured.

Cover request counts, retries, dry runs, idempotency, and handwritten-note preservation. Maintain one detail read per processing attempt and submit only changed fields. Keep handwritten prefixes before mountain data and weather.

## Commit & Pull Request Guidelines

Check `git status` before editing; preserve unrelated changes and stage only task-owned files. Recent commits use prefixes such as `feat:` and `test:` with Chinese summaries; follow that convention. Commit completed changes on the current branch. Push or rewrite history only when explicitly requested.

PR descriptions should explain behavior, configuration impact, related issues when applicable, and validation commands/results. Include sanitized before/after descriptions for formatting changes.

## Security & Configuration

Never commit credentials, tokens, processing state, logs, or personal hook configurations. Keep sensitive files at `600` and temporary directories at `700`. Use example configurations; keep machine-specific integrations outside shared application code.
