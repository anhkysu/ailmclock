#!/usr/bin/env python3
"""
ailimclock - a tiny Claude usage clock.

Shows your Claude 5-hour (session) and weekly limits with reset times,
read straight from the claude.ai web API using your sessionKey cookie.

Usage:
    1. Log into https://claude.ai in your browser.
    2. Open DevTools -> Application/Storage -> Cookies -> https://claude.ai
       Copy the value of the "sessionKey" cookie (starts with "sk-ant-sid01-...").
    3. Run:
         set AILIMCLOCK_SESSION_KEY=sk-ant-sid01-...   (Windows cmd)
         $env:AILIMCLOCK_SESSION_KEY="sk-ant-sid01-..." (PowerShell)
       or pass --session-key on the command line.
    4. python ailimclock.py            (print once)
       python ailimclock.py --watch    (refresh every 60s)

No third-party packages required (uses urllib from the stdlib).

NOTE: the sessionKey *is* your full claude.ai login. Keep it private;
never commit it or send it anywhere off your machine.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib import request, error

BASE = "https://claude.ai/api"
UA = "Mozilla/5.0 (ailimclock)"


def _get(url: str, session_key: str):
    req = request.Request(url, headers={
        "Cookie": f"sessionKey={session_key}",
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit("Auth failed (401/403). Your sessionKey is missing, wrong, or expired.")
        sys.exit(f"HTTP {e.code} from {url}: {e.read().decode('utf-8', 'replace')[:200]}")
    except error.URLError as e:
        sys.exit(f"Network error reaching {url}: {e.reason}")


def get_org_id(session_key: str) -> str:
    orgs = _get(f"{BASE}/organizations", session_key)
    if not orgs:
        sys.exit("No organizations returned for this account.")
    # Pick the first org; most personal accounts have exactly one.
    return orgs[0]["uuid"]


def get_usage(session_key: str, org_id: str) -> dict:
    return _get(f"{BASE}/organizations/{org_id}/usage", session_key)


def _bar(pct: float, width: int = 24) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _fmt_reset(ts) -> str:
    """Accepts an ISO-8601 string or epoch seconds; returns 'in 1h 42m'."""
    if ts is None:
        return "reset time unknown"
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, OSError):
        return f"resets {ts}"
    delta = dt - datetime.now(timezone.utc)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "resetting now"
    h, m = secs // 3600, (secs % 3600) // 60
    return f"resets in {h}h {m:02d}m"


def _extract(bucket) -> tuple:
    """Pull (percent_used, reset_ts) from a usage bucket, tolerating shape drift."""
    if not isinstance(bucket, dict):
        return None, None
    pct = None
    for k in ("utilization", "percent_used", "percentage", "used_pct"):
        if k in bucket and bucket[k] is not None:
            pct = float(bucket[k])
            break
    # Claude's `utilization` is already on a 0-100 scale (e.g. 2.0 means 2%).
    # Do NOT rescale small values: a weekly window at 1.0% must stay 1%, not
    # jump to 100%. (Same bug we already fixed for Codex.)
    reset = None
    for k in ("resets_at", "reset_at", "resets", "next_reset", "ends_at"):
        if k in bucket and bucket[k] is not None:
            reset = bucket[k]
            break
    return pct, reset


# The claude.ai /usage payload groups limits under these keys (mirrors the
# OAuth names). We probe a few aliases since the schema is undocumented.
BUCKETS = [
    ("5-hour session", ("five_hour", "fiveHour", "session")),
    ("Weekly (all)",   ("seven_day", "sevenDay", "weekly")),
    ("Weekly (Opus)",  ("seven_day_opus", "sevenDayOpus")),
]


def render(usage: dict) -> str:
    lines = []
    for label, keys in BUCKETS:
        bucket = next((usage[k] for k in keys if k in usage), None)
        if bucket is None:
            continue
        pct, reset = _extract(bucket)
        if pct is None:
            continue
        lines.append(f"{label:<16} {_bar(pct)} {pct:5.1f}%   {_fmt_reset(reset)}")
    if not lines:
        return ("Couldn't find known usage fields. Raw payload below "
                "(tell me the keys and I'll map them):\n"
                + json.dumps(usage, indent=2)[:1500])
    stamp = datetime.now().strftime("%H:%M:%S")
    return f"ailimclock  @ {stamp}\n" + "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Claude 5h + weekly usage clock.")
    ap.add_argument("--session-key", default=os.environ.get("AILIMCLOCK_SESSION_KEY"),
                    help="claude.ai sessionKey cookie (or set AILIMCLOCK_SESSION_KEY).")
    ap.add_argument("--watch", action="store_true", help="Refresh continuously.")
    ap.add_argument("--interval", type=int, default=60, help="Watch refresh seconds.")
    args = ap.parse_args()

    if not args.session_key:
        sys.exit("No sessionKey. Set AILIMCLOCK_SESSION_KEY or pass --session-key. "
                 "See the header of this file for how to grab it.")

    org_id = get_org_id(args.session_key)

    def once():
        usage = get_usage(args.session_key, org_id)
        out = render(usage)
        if args.watch:
            print("\033[2J\033[H", end="")  # clear screen
        print(out)

    if not args.watch:
        once()
        return
    try:
        while True:
            once()
            time.sleep(max(10, args.interval))
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
