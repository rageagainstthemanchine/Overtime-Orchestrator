# Overtime Analysis Script

Rich evidence-based estimation of overtime using multiple activity sources:

* Git commits (author email filtered)
* Bitbucket merged PRs (optional)
* Google Calendar events (CSV legacy or ICS modern export) with smart exclusions
* Slack messages (optional) with caching, concurrency & robust retry
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
| Calendar events | From CSV or ICS; excluded if title in exclusion list; skips all‑day | Title stored as "Meeting: …" |
| Slack messages | If `USE_SLACK=true`; user id ∈ `SLACK_USER_IDS`; no subtype | Channel allow/deny via env; caching accelerates reruns |

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
Features:
* Channel discovery across public/private channels, IMs and group DMs (subject to token scopes).
* Concurrency: Parallel channel history fetch using a thread pool (`SLACK_CONCURRENCY`).
* Incremental Caching: Per‑channel JSON cache in `.slack_cache/CHANNELID.json` storing:
  - `raw_messages`
  - `covered_since` / `covered_until` (UNIX epoch float bounds)
  - `last_fetched`
* Smart Range Filling: Only missing older/newer slices within the analysis window are fetched; previously covered spans reused.
* Robust Retry & Rate Limit Handling:
  - Exponential backoff with jitter for generic failures (1,2,4,8,16,30s cap + random 0–0.5s)
  - Honors Slack `ratelimited` errors using `Retry-After` header plus jitter
  - Resets attempt counter after each successful page

Recommended Slack Bot Scopes (minimal example – adapt as needed):
`channels:history`, `channels:read`, `groups:history`, `groups:read`, `im:history`, `im:read`, `mpim:history`, `mpim:read`.

Security: Token only read from environment. Caches are local JSON. Avoid committing `.slack_cache/`.

---
## Calendar (CSV & ICS)
* CSV: Legacy Google export (`Start Date`, `Start Time`, `End Date`, `End Time`, `Subject`). Supports 12h & 24h formats tried sequentially.
* ICS: Parsed using `icalendar`. Skips all‑day events (date objects) and excluded titles.
* Exclusion list (case-insensitive, configurable in code) defaults to: `Out of office`, `PTO`, `OOO`.

---
## Output Files
### `extra_commits.csv`
Columns: date, time, weekday, source, repo_or_channel, detail, outside_work (quick heuristic; final computation happens separately).

### `extra_summary.csv`
Columns: date, weekday, hours_extra_estimated, evidence_count (currently placeholder), examples (up to 5 sample notes including lunch penalty note if applied).
> NOTE: `evidence_count` may be enhanced later to reflect number of raw evidence items contributing to that day’s calculation.

---
## Environment Variables
Provide these in `.env` (loaded automatically by `python-dotenv`). Lists show defaults where applicable.

### Core
* `MY_EMAILS` – Comma-separated commit author emails to include. If empty, git evidence is skipped entirely.
* `LOCAL_TZ` – IANA timezone (default `America/Sao_Paulo`).
* `HOLIDAYS_COUNTRY` – e.g. `BR`.
* `HOLIDAYS_PROV` – Subdivision / state (e.g. `DF`).

### Date & Scope (set in code but can be edited directly in script)
* `SINCE`, `UNTIL` – Analysis bounds (ISO date strings) – currently defined in code constants.
* `REPOS_ROOT` – Root path scanned recursively for `.git` folders (adjust in script if needed).

### Bitbucket (optional)
* `BITBUCKET_USER`
* `BITBUCKET_APP_PASSWORD`
* Additional constants: `BITBUCKET_WORKSPACE`, `BITBUCKET_REPO_SLUGS` (edit in code).

### Google Calendar
* `GOOGLE_CALENDAR_ICS` – Path to exported `.ics` file. (If using CSV instead, set `GOOGLE_CALENDAR_CSV` constant in code.)

### Slack
* `USE_SLACK` – `true|false` (default false)
* `SLACK_BOT_TOKEN` – Bot/User token (xoxb...)
* `SLACK_USER_IDS` – Only messages from these users counted (comma-separated user IDs)
* `SLACK_CHANNEL_IDS` – Optional allowlist (IDs). If empty -> all accessible channels considered.
* `SLACK_EXCLUDE_CHANNEL_IDS` – Optional denylist (IDs)
* `SLACK_MAX_CHANNELS` – Safety cap (default 300)
* `SLACK_PAGE_LIMIT` – Messages per history page (default 100)
* `SLACK_CONCURRENCY` – Thread pool size for channel fetches (default 6)
* `SLACK_CACHE_ENABLED` – Enable local JSON caching (default true)
* `SLACK_CACHE_DIR` – Directory for cache files (default `.slack_cache`)

### Other (code constants)
* `WORK_START`, `WORK_END` – Simpler hour-based outside_work field (legacy heuristic) separate from shift windows.
* `SHIFT_WINDOWS` – Detailed per-weekday working intervals for precise calculation.
* `WEEKENDS_COUNT_AS_EXTRA` – If true, weekends have no work windows (all counts).
* `PTO_DAYS_STR` – Hard-coded PTO ISO dates treated like holidays.
* `EXCLUDED_CALENDAR_TITLES` – List of meeting subjects to ignore (lowercased at runtime).

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
* Cache audit improvements (see `slack_cache_audit.py`).

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
| Slow Slack fetch | Large channel set | Narrow allowlist / raise concurrency (cautiously) |
| Rate limit delays | High volume fetch | Allow backoff to proceed; rerun uses cache |
| Calendar empty | Wrong path or missing `icalendar` lib | Verify `GOOGLE_CALENDAR_ICS` & dependency |
| Suspect duplicate Slack msgs | Cache merge anomaly | Run: `python slack_cache_audit.py --since 2024-05-01 --until 2025-08-30` |

---
## Data Privacy
All processing is local. Only your network calls are to Bitbucket & Slack APIs you configure. Caches stored locally; review before sharing.

---
## License
No explicit license provided; treat as internal script unless a license file is added.

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
