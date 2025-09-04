# Overtime Analysis Script

Rich evidence-based estimation of overtime using multiple activity sources:

* Git commits (author email filtered)
* Bitbucket merged PRs (optional)
* Google Calendar events (ICS export only) with smart exclusions
* Slack messages (optional) via search API with per-user caching
* Automatic lunch-break penalty if no uninterrupted 60‑minute gap during defined work windows

> Goal: Provide auditable evidence of activity outside normal working hours.

---
## Quick Start
1. Clone repo / place script.
2. Create & activate a Python environment (recommended).
3. Install dependencies:
	```bash
	pip install -r requirements.txt
	```
4. Copy `.env.example` (if present) to `.env` and fill variables (at minimum `MY_EMAILS`).
5. Adjust constants at top of `overtime_script.py` if needed (work hours, date range, repo root, PTO list).
6. Run:
	```bash
	python overtime_script.py
	```
7. Inspect outputs:
	* `extra_commits.csv` – granular evidence rows
	* `extra_summary.csv` – per‑day overtime aggregation & sample notes

---
## Core Concepts
### Evidence Sources
| Source | Inclusion Logic | Notes |
| ------ | --------------- | ----- |
| git commits | Author email ∈ `MY_EMAILS`, subject not matching exclusion regex | Each commit timestamp clustered into sessions |
| Bitbucket PRs (merged) | If credentials + workspace + repos configured | Uses `updated_on` for merged PRs |
| Calendar events | From ICS only; excluded if title in exclusion list; skips all‑day | Title stored as "Meeting: …" |
| Slack messages | If `USE_SLACK=true`; user id ∈ `SLACK_USER_IDS`; via search API | Per-user caching accelerates reruns |

### Session Clustering
Commit/PR/Slack timestamps are clustered if gaps ≤ 45 minutes. Each session is padded (default: -10 min before, +15 min after) to approximate setup/cleanup/context switching.

### Outside‑Work Determination
* Work windows defined per weekday via `SHIFT_WINDOWS` (or empty meaning full day counts as outside if weekend/holiday and configured so).
* For each session interval we intersect with the complement (outside) of that day’s work windows.
* Calendar events contribute only their portions that fall outside work windows (precise intersection).

### Lunch Break Penalty
If a working day has any activity inside work windows but lacks a continuous free interval of ≥ 60 minutes between first and last activity inside those windows, +60 minutes are added as overtime (`[lunch] no 60m break (+1h)`).

### Holidays & PTO
Public holidays (via `holidays` library) plus explicit PTO dates are treated as days with no work windows → all evidence on those days counts entirely as overtime.

---
## Slack Integration Details
This simplified version uses Slack's search API to find messages by specific users.

Features:
* Search-based message collection using `search.messages` API
* Per-user caching in `.slack_cache/search_user_USERID.json` storing:
  - `raw_messages`
  - `covered_since` / `covered_until` (UNIX epoch float bounds)
  - `last_fetched`
  - `mode: 'search'`
* Date-sliced queries (14-day chunks) to handle large date ranges
* Robust Retry & Rate Limit Handling:
  - Exponential backoff with jitter for generic failures (1,2,4,8,16,30s cap + random 0–0.5s)
  - Honors Slack `ratelimited` errors using `Retry-After` header plus jitter
  - Resets attempt counter after each successful page

Required Slack Bot Scopes:
`channels:history`, `channels:read`,
`groups:history`, `groups:read`,
`im:history`, `im:read`,
`mpim:history`, `mpim:read`,
`search:messages`, `search:read`, `search:read.files`, `search:read.im`, `search:read.mpim`, `search:read.private`, `search:read.public`, `search:read.users`,
`users:read`.

Security: Token only read from environment. Caches are local JSON. Avoid committing `.slack_cache/`.

---
## Calendar (ICS Only)
* ICS: Parsed using `icalendar` library. Skips all‑day events (date objects) and excluded titles.
* Exclusion list (case-insensitive, configurable via `EXCLUDED_CALENDAR_TITLES` env var) defaults to: `Out of office`, `PTO`, `OOO`.
* Note: CSV support has been removed in this simplified version.

---
## Output Files
### `extra_commits.csv`
Columns: date, time, weekday, source, repo_or_channel, detail

### `extra_summary.csv`
Columns: date, weekday, hours_extra_estimated, examples (up to 5 sample notes including lunch penalty note if applied)

---
## Environment Variables
Provide these in `.env` (loaded automatically by `python-dotenv`). Lists show defaults where applicable.

### Core
* `MY_EMAILS` – Comma-separated commit author emails to include. If empty, git evidence is skipped entirely.
* `LOCAL_TZ` – IANA timezone (default `America/New_York`).
* `HOLIDAYS_COUNTRY` – Country code (default `US`).
* `HOLIDAYS_PROV` – Subdivision / state (default `NY`).

### Date & Scope (set in code but can be edited directly in script)
* `SINCE`, `UNTIL` – Analysis bounds (ISO date strings) – currently defined in code constants.
* `REPOS_ROOT` – Root path scanned recursively for `.git` folders (adjust in script if needed).

### Bitbucket (optional)
* `BITBUCKET_USER`
* `BITBUCKET_APP_PASSWORD`
* Additional constants: `BITBUCKET_WORKSPACE`, `BITBUCKET_REPO_SLUGS` (edit in code).

### Google Calendar
* `GOOGLE_CALENDAR_ICS` – Path to exported `.ics` file.

### Slack
* `USE_SLACK` – `true|false` (default false)
* `SLACK_BOT_TOKEN` – Bot/User token (xoxb...)
* `SLACK_USER_IDS` – Only messages from these users counted (comma-separated user IDs)
* `SLACK_CACHE_ENABLED` – Enable local JSON caching (default true)
* `SLACK_FORCE_REFRESH` – Ignore existing cache and fetch fresh (default false)
* `SLACK_CACHE_DIR` – Directory for cache files (default `.slack_cache`)

### Other (code constants and env vars)
* `SHIFT_WINDOWS` – Detailed per-weekday working intervals for precise calculation (9am-6pm Mon-Fri by default).
* `WEEKENDS_COUNT_AS_EXTRA` – If true, weekends have no work windows (default true).
* `PTO_DAYS_STR` – Hard-coded PTO ISO dates treated like holidays (comma-separated).
* `EXCLUDED_CALENDAR_TITLES` – Meeting subjects to ignore (comma-separated, default "Out of office,PTO,OOO").

---
## Algorithms & Logic
1. Collection phase (git, PRs, calendar, slack) → list of raw events with timestamps.
2. Session clustering of point events (git/PR/slack) with gap threshold (45m) + padding.
3. Intersect each session with outside-of-work intervals derived from shift windows per day.
4. Calendar events: individually intersect with outside-of-work windows (more precise than session approach).
5. Merge all outside intervals → per-day minutes.
6. Add lunch penalty where applicable.
7. Emit CSV outputs.

Edge Handling:
* Empty `MY_EMAILS` ⇒ skip git entirely.
* Timezone normalization using `zoneinfo` (falls back if missing).
* Robust parsing attempts for ISO / slack UNIX timestamps / ICS datetimes.
* Merge logic ensures overlapping intervals don’t double-count.

---
## Extensibility Ideas
* Force-refresh flag to ignore Slack cache for a run.
* Populate `evidence_count` with actual contributing items.
* Unit tests for interval arithmetic & lunch detection.
* Configurable lunch duration & required gap.
* Web dashboard or notebook visualization.
* Enhanced cache management and audit tools.

---
## Limitations
* Not a time tracker; infers activity from sparse artifacts.
* Calendar inside-work events don’t contribute (only outside parts) – intentional.
* Slack presence/reads not considered—only authored messages.
* Lunch penalty heuristic may overcount in edge cases (e.g., long single meeting blocking mid-day).

---
## Troubleshooting
| Symptom | Cause | Action |
| ------- | ----- | ------ |
| 0 commits collected | `MY_EMAILS` empty or mismatch | Confirm emails & case | 
| Slack messages missing | Missing scopes or user IDs | Add required scopes & check `SLACK_USER_IDS` |
| Slow Slack fetch | Large date range or many users | Use `SLACK_FORCE_REFRESH=false` to leverage cache |
| Rate limit delays | High volume fetch | Allow backoff to proceed; rerun uses cache |
| Calendar empty | Wrong path or missing `icalendar` lib | Verify `GOOGLE_CALENDAR_ICS` & dependency |
| Slack search errors | Missing search scopes | Ensure bot has `search:read` scope |

---
## Data Privacy
All processing is local. Only your network calls are to Bitbucket & Slack APIs you configure. Caches stored locally; review before sharing.

---
## License
MIT License - see LICENSE file for details.

---
## Changelog (High-Level)
* Initial: Git commits & simple overtime heuristic
* Added: Email filtering strict mode (empty list ⇒ no commits)
* Added: `.env` loading via `python-dotenv`
* Added: Google Calendar ICS parsing & exclusion list
* Added: Slack integration (messages evidence)
* Added: Concurrency for Slack channel history
* Added: Slack caching with partial coverage merging
* Added: Exponential backoff + jitter w/ reset on success
* Added: Lunch break penalty (+60m when no 1h gap)

---
## Disclaimer
Use results as supporting evidence only. Validate manually before external reporting.

---
Happy analyzing!
