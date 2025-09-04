#!/usr/bin/env python3
"""
Simplified overtime evidence script.

Included sources:
  - Git commits (filtered by MY_EMAILS)
  - Bitbucket merged PRs (optional)
  - Google Calendar ICS events (optional, ICS only)
  - Slack messages via search.messages (only authored by SLACK_USER_IDS)

Excluded from this simplified version:
  - Slack channel enumeration & per-channel caching / membership filtering
  - Google Calendar CSV legacy parsing
  - Advanced Slack range coverage logic

Outputs:
  - extra_commits.csv  (detailed evidence rows)
  - extra_summary.csv  (daily overtime summary)

Environment variables (load from .env if present):
  SINCE, UNTIL (YYYY-MM-DD bounds)
  MY_EMAILS=mail1,mail2
  REPOS_ROOT=path/to/repos (defaults to HOME)
  USE_BITBUCKET=true|false
  BITBUCKET_USER, BITBUCKET_APP_PASSWORD, BITBUCKET_WORKSPACE, BITBUCKET_REPO_SLUGS=repo1,repo2
  GOOGLE_CALENDAR_ICS=/path/to/export.ics
  USE_SLACK=true|false
  SLACK_BOT_TOKEN=...  (needs search scope e.g. search:read)
  SLACK_USER_IDS=U12345,U67890 (users whose authored messages count)
  SLACK_CACHE_ENABLED=true|false (cache search results per user)
  SLACK_FORCE_REFRESH=true|false (ignore existing cache)
  LOCAL_TZ=America/Sao_Paulo (default)
  HOLIDAYS_COUNTRY=BR  HOLIDAYS_PROV=DF

Run: python simplifield_overtime_script.py
"""
import os
from dotenv import load_dotenv
load_dotenv()

import csv
import subprocess
import re
import json
import base64
import urllib.request
import urllib.parse
import random
import time as time_module
from datetime import datetime, timedelta, time, timezone
from pathlib import Path
from collections import defaultdict

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py <3.9 fallback (not fully supported here)
    ZoneInfo = None
try:
    import holidays as pyholidays
except ImportError:
    pyholidays = None

# -----------------------------
# CONFIG
# -----------------------------
SINCE = os.getenv("SINCE", "2024-05-01")
UNTIL = os.getenv("UNTIL", "2025-08-30")
REPOS_ROOT = os.getenv("REPOS_ROOT", str(Path.home()))
OUT_COMMITS_CSV = "extra_commits.csv"
OUT_SUMMARY_CSV = "extra_summary.csv"

USE_BITBUCKET = os.getenv("USE_BITBUCKET", "false").lower() in ("1","true","yes")
BITBUCKET_USER = os.getenv("BITBUCKET_USER")
BITBUCKET_APP_PASSWORD = os.getenv("BITBUCKET_APP_PASSWORD")
BITBUCKET_WORKSPACE = os.getenv("BITBUCKET_WORKSPACE", "")
BITBUCKET_REPO_SLUGS = [r.strip() for r in os.getenv("BITBUCKET_REPO_SLUGS", "").split(",") if r.strip()]

GOOGLE_CALENDAR_ICS = os.getenv("GOOGLE_CALENDAR_ICS")

USE_SLACK = os.getenv("USE_SLACK", "false").lower() in ("1","true","yes")
SLACK_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_USER_IDS = {u.strip() for u in os.getenv("SLACK_USER_IDS", "").split(",") if u.strip()}
SLACK_CACHE_ENABLED = os.getenv("SLACK_CACHE_ENABLED", "true").lower() in ("1","true","yes")
SLACK_FORCE_REFRESH = os.getenv("SLACK_FORCE_REFRESH", "false").lower() in ("1","true","yes")
SLACK_CACHE_DIR = Path(os.getenv("SLACK_CACHE_DIR", ".slack_cache"))

LOCAL_TZ = os.getenv("LOCAL_TZ", "America/New_York")
MY_EMAILS = {e.strip().lower() for e in os.getenv("MY_EMAILS", "").split(",") if e.strip()}
EXCLUDE_COMMIT_MSG_RE = re.compile(r"\b(merge pull request|dependabot|bump version|chore:?)\b", re.I)

# Work schedule windows per weekday (0=Mon .. 6=Sun)
WEEKENDS_COUNT_AS_EXTRA = True
SHIFT_WINDOWS = {
    0: [(time(9,0), time(18,0))],
    1: [(time(9,0), time(18,0))],
    2: [(time(9,0), time(18,0))],
    3: [(time(9,0), time(18,0))],
    4: [(time(9,0), time(18,0))],
    5: [] if WEEKENDS_COUNT_AS_EXTRA else [(time(9,0), time(18,0))],
    6: [] if WEEKENDS_COUNT_AS_EXTRA else [(time(9,0), time(18,0))],
}

HOLIDAYS_COUNTRY = os.getenv("HOLIDAYS_COUNTRY", "US")
HOLIDAYS_PROV = os.getenv("HOLIDAYS_PROV", "NY")
HOLIDAYS = (pyholidays.country_holidays(HOLIDAYS_COUNTRY, subdiv=HOLIDAYS_PROV) if pyholidays else set())

EXCLUDED_CALENDAR_TITLES = {t.strip().lower() for t in os.getenv(
    "EXCLUDED_CALENDAR_TITLES",
    "Out of office,PTO,OOO"
).split(",") if t.strip()}
PTO_DAYS_STR = [d.strip() for d in os.getenv("PTO_DAYS_STR", "").split(",") if d.strip()]
PTO_DAYS = set()
for d_str in PTO_DAYS_STR:
    try:
        PTO_DAYS.add(datetime.fromisoformat(d_str).date())
    except Exception:
        pass
for d in PTO_DAYS:
    HOLIDAYS[d] = "PTO"

# -----------------------------
# UTILITIES
# -----------------------------
def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(LOCAL_TZ))
    return dt.astimezone(ZoneInfo(LOCAL_TZ))

def parse_iso_local(dt_str: str):
    try:
        return to_local(datetime.fromisoformat(dt_str.replace("Z", "+00:00")))
    except Exception:
        try:
            return to_local(datetime.fromisoformat(dt_str))
        except Exception:
            try:
                return to_local(datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S %z"))
            except Exception:
                return None

def day_work_windows(d):
    if (d in HOLIDAYS) or not SHIFT_WINDOWS.get(d.weekday()):
        return []
    return SHIFT_WINDOWS[d.weekday()]

def merge_intervals(intervals):
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x[0])
    merged = [intervals[0]]
    for s,e in intervals[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s,e))
    return merged

def outside_segments_for_day(d):
    tz = ZoneInfo(LOCAL_TZ)
    full_start = datetime.combine(d, time(0,0), tzinfo=tz)
    full_end = datetime.combine(d, time(23,59,59), tzinfo=tz)
    windows = day_work_windows(d)
    if not windows:
        return [(full_start, full_end)]
    inside = [
        (datetime.combine(d, s, tzinfo=tz), datetime.combine(d, e, tzinfo=tz))
        for s,e in windows
    ]
    inside = merge_intervals(inside)
    cursor = full_start
    outside = []
    for s,e in inside:
        if cursor < s:
            outside.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < full_end:
        outside.append((cursor, full_end))
    return outside

def intersect_interval_with_outside(start: datetime, end: datetime):
    out = []
    cur = start
    while cur.date() <= end.date():
        day = cur.date()
        day_start = datetime.combine(day, time(0,0), tzinfo=start.tzinfo)
        day_end = datetime.combine(day, time(23,59,59), tzinfo=start.tzinfo)
        seg_start = max(start, day_start)
        seg_end = min(end, day_end)
        if seg_start < seg_end:
            for os, oe in outside_segments_for_day(day):
                s = max(seg_start, os)
                e = min(seg_end, oe)
                if s < e:
                    out.append((s,e))
        cur = day_end + timedelta(seconds=1)
    return merge_intervals(out)

def sessions_from_points(points, gap_min=45, pad_before_min=10, pad_after_min=15):
    if not points:
        return []
    points = sorted(points)
    sessions = []
    start = points[0]
    last = points[0]
    for t in points[1:]:
        if (t - last) > timedelta(minutes=gap_min):
            sessions.append((start - timedelta(minutes=pad_before_min), last + timedelta(minutes=pad_after_min)))
            start = t
        last = t
    sessions.append((start - timedelta(minutes=pad_before_min), last + timedelta(minutes=pad_after_min)))
    return sessions

# -----------------------------
# GIT COMMITS
# -----------------------------
def find_git_repos(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            yield dirpath
            dirnames[:] = [d for d in dirnames if d != ".git"]

def collect_git_commits():
    if not MY_EMAILS:
        print("MY_EMAILS empty; skipping git commits")
        return []
    rows = []
    since_dt = parse_iso_local(SINCE+"T00:00:00")
    until_dt = parse_iso_local(UNTIL+"T23:59:59")
    for repo in find_git_repos(REPOS_ROOT):
        try:
            cmd = [
                "git", "-C", repo, "log",
                f"--since={SINCE}", f"--until={UNTIL}",
                "--no-merges",
                "--pretty=format:%H|%an|%ae|%cI|%s"
            ]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            continue
        for line in out.splitlines():
            try:
                sha, author_name, author_email, ciso, subject = line.split("|", 4)
            except ValueError:
                continue
            if author_email.lower() not in MY_EMAILS:
                continue
            if EXCLUDE_COMMIT_MSG_RE.search(subject):
                continue
            dt = parse_iso_local(ciso)
            if not dt or dt < since_dt or dt > until_dt:
                continue
            rows.append({
                "source": "git",
                "repo": os.path.relpath(repo, REPOS_ROOT),
                "timestamp_local": dt.isoformat(),
                "detail": f"commit {sha[:7]}: {subject}",
            })
    return rows

# -----------------------------
# BITBUCKET PRS
# -----------------------------
def http_get(url, auth=None, params=None):
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    headers = {}
    if auth:
        user, pwd = auth
        import base64 as b64
        creds = b64.b64encode(f"{user}:{pwd}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        import json as _json
        return _json.loads(resp.read().decode())

def collect_bitbucket_prs():
    if not USE_BITBUCKET or not all([BITBUCKET_USER, BITBUCKET_APP_PASSWORD, BITBUCKET_REPO_SLUGS, BITBUCKET_WORKSPACE]):
        return []
    rows = []
    q = f'state = "MERGED" AND updated_on >= "{SINCE}" AND updated_on <= "{UNTIL}T23:59:59"'
    auth = (BITBUCKET_USER, BITBUCKET_APP_PASSWORD)
    for repo in BITBUCKET_REPO_SLUGS:
        base = f"https://api.bitbucket.org/2.0/repositories/{BITBUCKET_WORKSPACE}/{repo}/pullrequests"
        url = f"{base}?{urllib.parse.urlencode({'q': q, 'pagelen': 50})}"
        while url:
            data = http_get(url, auth=auth)
            for pr in data.get("values", []):
                dt = parse_iso_local(pr.get("updated_on") or "")
                if not dt:
                    continue
                rows.append({
                    "source": "bitbucket_pr",
                    "repo": repo,
                    "timestamp_local": dt.isoformat(),
                    "detail": f"PR #{pr.get('id',0)} merged: {pr.get('title','')}",
                })
            url = data.get("next")
    return rows

# -----------------------------
# CALENDAR (ICS ONLY)
# -----------------------------
def collect_calendar_events():
    rows = []
    if not GOOGLE_CALENDAR_ICS or not os.path.exists(GOOGLE_CALENDAR_ICS):
        return rows
    try:
        from icalendar import Calendar
    except ImportError:
        print("icalendar not installed; skipping ICS events")
        return rows
    try:
        with open(GOOGLE_CALENDAR_ICS, 'r', encoding='utf-8') as f:
            cal_text = f.read()
        cal = Calendar.from_ical(cal_text)
    except Exception as e:
        print(f"Failed to parse ICS file: {e}")
        return rows

    def add_event(s: datetime, e: datetime, title: str):
        if e <= s:
            return
        since_date = datetime.fromisoformat(SINCE).date()
        until_date = datetime.fromisoformat(UNTIL).date()
        if s.date() > until_date or e.date() < since_date:
            return
        rows.append({
            "source": "calendar",
            "repo": "",
            "timestamp_local": s.isoformat(),
            "start": s,
            "end": e,
            "detail": f"Meeting: {title}",
        })

    for component in cal.walk():
        if component.name != 'VEVENT':
            continue
        try:
            dtstart_field = component.get('dtstart')
            dtend_field = component.get('dtend')
            summary = str(component.get('summary') or '(no title)')
        except Exception:
            continue
        if not dtstart_field or not dtend_field:
            continue
        dtstart = dtstart_field.dt
        dtend = dtend_field.dt
        if not isinstance(dtstart, datetime) or not isinstance(dtend, datetime):
            continue  # skip all-day
        if dtstart.tzinfo is None:
            dtstart = dtstart.replace(tzinfo=ZoneInfo(LOCAL_TZ))
        if dtend.tzinfo is None:
            dtend = dtend.replace(tzinfo=ZoneInfo(LOCAL_TZ))
        s = to_local(dtstart)
        e = to_local(dtend)
        if summary.strip().lower() in EXCLUDED_CALENDAR_TITLES:
            continue
        add_event(s, e, summary)
    return rows

# -----------------------------
# SLACK (SEARCH MODE ONLY)
# -----------------------------
def collect_slack_messages_search():
    if not USE_SLACK:
        return []
    if not SLACK_TOKEN or not SLACK_USER_IDS:
        print("Slack enabled but token/user IDs missing; skipping")
        return []
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        print("slack_sdk not installed; skipping Slack")
        return []

    client = WebClient(token=SLACK_TOKEN)
    if SLACK_CACHE_ENABLED:
        SLACK_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    oldest_ts = datetime.fromisoformat(SINCE + "T00:00:00").timestamp()
    latest_ts = datetime.fromisoformat(UNTIL + "T23:59:59").timestamp()

    rows = []

    def cache_path(uid):
        return SLACK_CACHE_DIR / f"search_user_{uid}.json"

    def load_cache(uid):
        if not SLACK_CACHE_ENABLED:
            return None
        p = cache_path(uid)
        if not p.exists():
            return None
        try:
            with p.open('r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    def save_cache(uid, data):
        if not SLACK_CACHE_ENABLED:
            return
        p = cache_path(uid)
        try:
            with p.open('w', encoding='utf-8') as f:
                json.dump(data, f, indent=1)
        except Exception as e:
            print(f"Failed writing search cache {p}: {e}")

    for uid in SLACK_USER_IDS:
        cache = None if SLACK_FORCE_REFRESH else load_cache(uid)
        need_fetch = True
        if cache:
            cs = cache.get('covered_since')
            cu = cache.get('covered_until')
            if isinstance(cs,(int,float)) and isinstance(cu,(int,float)) and cs <= oldest_ts and cu >= latest_ts:
                need_fetch = False
        messages_accum = [] if need_fetch else cache.get('raw_messages', [])
        if need_fetch:
            start_date = datetime.fromisoformat(SINCE).date()
            end_date = datetime.fromisoformat(UNTIL).date()
            slice_days = 14
            current = start_date
            while current <= end_date:
                slice_end = min(end_date, current + timedelta(days=slice_days-1))
                q = f"from:<@{uid}> after:{current.isoformat()} before:{(slice_end + timedelta(days=1)).isoformat()}"
                cursor = None
                attempts = 0
                while True:
                    try:
                        resp = client.search_messages(query=q, sort='timestamp', sort_dir='asc', count=100, cursor=cursor)
                        attempts = 0
                    except SlackApiError as e:
                        err = e.response.get('error') if hasattr(e,'response') else str(e)
                        if err == 'ratelimited':
                            retry_after = int(e.response.headers.get('Retry-After','5')) if hasattr(e.response,'headers') else 5
                            time_module.sleep(retry_after + random.uniform(0,1))
                            continue
                        attempts += 1
                        if attempts > 5:
                            print(f"search abort uid={uid} slice {current}->{slice_end}: {err}")
                            break
                        backoff = min(30, 2 ** (attempts-1))
                        time_module.sleep(backoff + random.uniform(0,0.5))
                        continue
                    matches = resp.get('messages', {}).get('matches', [])
                    for m in matches:
                        ts_str = m.get('ts') or m.get('message', {}).get('ts')
                        if not ts_str:
                            continue
                        try:
                            ts_f = float(ts_str)
                        except Exception:
                            continue
                        if not (oldest_ts <= ts_f <= latest_ts):
                            continue
                        channel_info = m.get('channel', {})
                        ch_name = channel_info.get('name') or channel_info.get('id') or 'unknown'
                        text = (m.get('text') or m.get('message', {}).get('text') or '').strip()
                        messages_accum.append({'ts': ts_str, 'text': text, 'channel': ch_name})
                    cursor = resp.get('response_metadata', {}).get('next_cursor') or None
                    if not cursor:
                        break
                current = slice_end + timedelta(days=1)
            dedup = {m['ts']: m for m in messages_accum}
            messages_accum = list(dedup.values())
            if messages_accum:
                cov_since = min(float(m['ts']) for m in messages_accum)
                cov_until = max(float(m['ts']) for m in messages_accum)
            else:
                cov_since, cov_until = oldest_ts, latest_ts
            save_cache(uid, {
                'user_id': uid,
                'raw_messages': messages_accum,
                'covered_since': cov_since,
                'covered_until': cov_until,
                'last_fetched': datetime.now().isoformat(),
                'mode': 'search'
            })
        for m in messages_accum:
            try:
                ts_float = float(m['ts'])
            except Exception:
                continue
            if not (oldest_ts <= ts_float <= latest_ts):
                continue
            dt_utc = datetime.fromtimestamp(ts_float, tz=timezone.utc)
            dt_local = to_local(dt_utc)
            text = m.get('text','')
            snippet = (text[:60] + '...') if len(text) > 63 else text
            ch_name = m.get('channel','search')
            rows.append({
                'source': 'slack',
                'repo': ch_name,
                'timestamp_local': dt_local.isoformat(),
                'detail': f"msg in #{ch_name}: {snippet}",
            })
    return rows

# -----------------------------
# OVERTIME CALC
# -----------------------------
def calendar_outside_intervals(rows_from_calendar):
    intervals = []
    for r in rows_from_calendar:
        intervals.extend(intersect_interval_with_outside(r['start'], r['end']))
    return merge_intervals(intervals)

def compute_overtime(commits, prs, calendar, slack_msgs):
    commit_times = [parse_iso_local(r['timestamp_local']) for r in commits]
    pr_times = [parse_iso_local(r['timestamp_local']) for r in prs]
    slack_times = [parse_iso_local(r['timestamp_local']) for r in slack_msgs]
    sessions = sessions_from_points([t for t in commit_times + pr_times + slack_times if t])

    outside_sessions = []
    for s,e in sessions:
        outside_sessions.extend(intersect_interval_with_outside(s,e))
    outside_sessions = merge_intervals(outside_sessions)

    cal_intervals = calendar_outside_intervals([
        {"start": r['start'], "end": r['end'], "title": r['detail']} for r in calendar
    ])

    all_intervals = merge_intervals(outside_sessions + cal_intervals)
    per_day = defaultdict(lambda: {"minutes": 0, "notes": []})
    for s,e in all_intervals:
        per_day[s.date().isoformat()]["minutes"] += int((e - s).total_seconds() // 60)

    for r in sorted(commits + prs + calendar + slack_msgs, key=lambda x: x['timestamp_local']):
        day = datetime.fromisoformat(r['timestamp_local']).date().isoformat()
        if len(per_day[day]['notes']) < 5:
            per_day[day]['notes'].append(f"[{r['source']}] {r['detail']}")

    # Lunch gap heuristic (+60 if no 60m free inside normal windows)
    LUNCH_MINUTES = 60
    event_days = set(datetime.fromisoformat(r['timestamp_local']).date() for r in commits + prs + slack_msgs)
    event_days.update(r['start'].date() for r in calendar)
    event_days.update(datetime.fromisoformat(d).date() for d in per_day.keys())

    def inside_work_intervals_for_date(d):
        windows = day_work_windows(d)
        tz = ZoneInfo(LOCAL_TZ)
        return [(datetime.combine(d, s, tzinfo=tz), datetime.combine(d, e, tzinfo=tz)) for s,e in windows]

    for d in sorted(event_days):
        if not day_work_windows(d):
            continue
        inside_windows = inside_work_intervals_for_date(d)
        if not inside_windows:
            continue
        occupied = []
        for s,e in sessions:
            if s.date() <= d <= e.date():
                day_start = datetime.combine(d, time(0,0), tzinfo=s.tzinfo)
                day_end = datetime.combine(d, time(23,59,59), tzinfo=s.tzinfo)
                seg_s = max(s, day_start)
                seg_e = min(e, day_end)
                if seg_s < seg_e:
                    for iw_s, iw_e in inside_windows:
                        cs = max(seg_s, iw_s)
                        ce = min(seg_e, iw_e)
                        if cs < ce:
                            occupied.append((cs, ce))
        for r in calendar:
            s = r['start']
            e = r['end']
            if s.date() <= d <= e.date():
                day_start = datetime.combine(d, time(0,0), tzinfo=s.tzinfo)
                day_end = datetime.combine(d, time(23,59,59), tzinfo=s.tzinfo)
                seg_s = max(s, day_start)
                seg_e = min(e, day_end)
                if seg_s < seg_e:
                    for iw_s, iw_e in inside_windows:
                        cs = max(seg_s, iw_s)
                        ce = min(seg_e, iw_e)
                        if cs < ce:
                            occupied.append((cs, ce))
        if not occupied:
            continue
        occupied = merge_intervals(occupied)
        free = []
        for iw_s, iw_e in inside_windows:
            cursor = iw_s
            for os_s, os_e in occupied:
                if os_e <= cursor or os_s >= iw_e:
                    continue
                if cursor < os_s:
                    free.append((cursor, os_s))
                cursor = max(cursor, os_e)
                if cursor >= iw_e:
                    break
            if cursor < iw_e:
                free.append((cursor, iw_e))
        has_lunch_gap = any((iv[1]-iv[0]).total_seconds() >= LUNCH_MINUTES*60 for iv in free)
        if not has_lunch_gap:
            key = d.isoformat()
            _ = per_day[key]
            per_day[key]['minutes'] += LUNCH_MINUTES
            lunch_note = "[lunch] no 60m break (+1h)"
            if lunch_note not in per_day[key]['notes']:
                if len(per_day[key]['notes']) < 5:
                    per_day[key]['notes'].insert(0, lunch_note)
                else:
                    per_day[key]['notes'][-1] = lunch_note
    return per_day

# -----------------------------
# MAIN
# -----------------------------
def main():
    print("Collecting git commits...")
    commits = collect_git_commits()
    print(f"Commits: {len(commits)}")

    prs = collect_bitbucket_prs()
    if prs:
        print(f"Bitbucket merged PRs: {len(prs)}")

    calendar_events = collect_calendar_events()
    if calendar_events:
        print(f"Calendar events: {len(calendar_events)}")

    slack_msgs = collect_slack_messages_search()
    if slack_msgs:
        print(f"Slack messages: {len(slack_msgs)}")

    all_rows = commits + prs + calendar_events + slack_msgs
    all_rows.sort(key=lambda r: r['timestamp_local'])

    with open(OUT_COMMITS_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["date", "time", "weekday", "source", "repo_or_channel", "detail"])
        for r in all_rows:
            dt = datetime.fromisoformat(r['timestamp_local'])
            w.writerow([
                dt.date().isoformat(),
                dt.time().isoformat(timespec='seconds'),
                dt.strftime('%a'),
                r['source'],
                r['repo'],
                r['detail']
            ])

    per_day = compute_overtime(commits, prs, calendar_events, slack_msgs)
    with open(OUT_SUMMARY_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["date", "weekday", "hours_extra_estimated", "examples"])
        for day in sorted(per_day.keys()):
            d = datetime.fromisoformat(day)
            mins = per_day[day]['minutes']
            w.writerow([day, d.strftime('%a'), f"{mins/60:.2f}", "; ".join(per_day[day]['notes'][:5])])

    print(f"Done -> {OUT_COMMITS_CSV}, {OUT_SUMMARY_CSV}")

if __name__ == '__main__':
    main()
