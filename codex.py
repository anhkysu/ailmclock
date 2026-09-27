#!/usr/bin/env python3
"""
codex.py - read OpenAI Codex usage (5h + weekly) the way CodexBar does.

Source of truth:
  - token: ~/.codex/auth.json  (or $CODEX_HOME/auth.json), field tokens.access_token
  - endpoint: GET https://chatgpt.com/backend-api/wham/usage
              header  Authorization: Bearer <access_token>
  - response: rate_limit primary window (5h) + secondary window (weekly),
              each with a percent-used and a reset time.

No third-party packages (stdlib urllib). The JSON schema is undocumented, so we
probe a few field aliases and fall back to dumping the raw payload.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib import request, error

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
UA = "Mozilla/5.0 (ailimclock)"


def auth_path() -> str:
    home = os.environ.get("CODEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".codex")
    return os.path.join(home, "auth.json")


def auth_path_for(home: str) -> str:
    """Return the auth file for an explicitly selected Codex home."""
    return os.path.join(os.path.expanduser(home), "auth.json")


def read_token() -> str:
    """Pull the OAuth access token Codex CLI stored locally."""
    path = auth_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No Codex auth at {path}. Run `codex login` first.")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Known shape: {"tokens": {"access_token": "...", "account_id": "..."}}
    tok = (data.get("tokens") or {}).get("access_token")
    # tolerate a couple of older/alternate shapes
    tok = tok or data.get("access_token") or data.get("OPENAI_ACCESS_TOKEN")
    if not tok:
        raise ValueError(f"No access_token found in {path}.")
    return tok


def read_token_for(home: str) -> str:
    """Pull the OAuth token from a specific Codex profile directory."""
    path = auth_path_for(home)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No Codex auth at {path}. Run `codex login` for this profile first.")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    tok = (data.get("tokens") or {}).get("access_token")
    tok = tok or data.get("access_token") or data.get("OPENAI_ACCESS_TOKEN")
    if not tok:
        raise ValueError(f"No access_token found in {path}.")
    return tok


def get_usage(token: str | None = None) -> dict:
    if token is None:
        token = read_token()
    req = request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        if e.code in (401, 403):
            raise PermissionError(
                "Codex auth failed (401/403). Token expired - run `codex login`.")
        raise RuntimeError(
            f"HTTP {e.code} from wham/usage: "
            f"{e.read().decode('utf-8', 'replace')[:200]}")
    except error.URLError as e:
        raise RuntimeError(f"Network error reaching wham/usage: {e.reason}")


def _find_window(usage: dict, which: str):
    """Return the primary (5h) or secondary (weekly) window dict, schema-tolerant.

    'which' is 'primary' or 'secondary'.
    """
    # Common containers seen in the wild.
    rl = (usage.get("rate_limit") or usage.get("rate_limits")
          or usage.get("usage") or usage)
    candidates = [
        f"{which}_window", which, f"{which}_rate_limit",
    ]
    for k in candidates:
        if isinstance(rl, dict) and k in rl and isinstance(rl[k], dict):
            return rl[k]

    # Some responses expose both `rate_limit` and `rate_limits`, or nest the
    # current limit under another usage object. Search those variants instead
    # of letting the first container hide a valid weekly bucket.
    def walk(value):
        if isinstance(value, dict):
            for k in candidates:
                if isinstance(value.get(k), dict):
                    return value[k]
            for child in value.values():
                found = walk(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found is not None:
                    return found
        return None

    return walk(usage)


def extract(window: dict):
    """Return (percent_used, reset_epoch_or_iso) from a Codex window dict."""
    if not isinstance(window, dict):
        return None, None
    pct = None
    for k in ("used_percent", "used_pct", "utilization", "percent_used",
              "percent"):
        if window.get(k) is not None:
            pct = float(window[k])
            break
    # Codex's wham/usage reports `used_percent` already on a 0-100 scale
    # (e.g. 1 means 1%, not 100%). Do NOT rescale small values -- a fresh,
    # just-reset window reporting `1` must stay 1%, not jump to 100%.
    # Reset: prefer the absolute epoch (authoritative server moment); fall back
    # to a relative "seconds from now" value. Real wham/usage fields are
    # `reset_at` (epoch) and `reset_after_seconds` (relative).
    reset = None
    for k in ("reset_at", "resets_at", "reset_time"):
        if window.get(k) is not None:
            reset = float(window[k])  # absolute epoch seconds
            break
    else:
        for k in ("reset_after_seconds", "resets_in_seconds",
                  "reset_in_seconds", "seconds_until_reset"):
            if window.get(k) is not None:
                reset = time.time() + float(window[k])  # -> absolute epoch
                break
    return pct, reset


# Normalized buckets the widget/CLI can render with the shared helpers in core.
def normalized(usage: dict) -> dict:
    out = {}
    for label, which in (("Codex 5h", "primary"), ("Codex Wk", "secondary")):
        bucket = normalized_window(usage, which)
        if bucket:
            out[label] = bucket
    return out


def normalized_window(usage: dict, which: str) -> dict:
    """Normalize one Codex usage window, including its inferred start time."""
    if which not in ("primary", "secondary"):
        raise ValueError("which must be 'primary' or 'secondary'")
    win = _find_window(usage, which)
    pct, reset = extract(win)
    if pct is None:
        return {}
    duration = None
    if isinstance(win, dict):
        for key in ("limit_window_seconds", "window_seconds",
                    "duration_seconds"):
            if win.get(key) is not None:
                try:
                    duration = float(win[key])
                except (TypeError, ValueError):
                    pass
                break
    starts_at = reset - duration if isinstance(reset, (int, float)) \
        and duration is not None else None
    return {
        "utilization": pct,
        "resets_at": reset,
        "starts_at": starts_at,
    }


def normalized_primary(usage: dict) -> dict:
    """Return the primary 5-hour usage window."""
    return normalized_window(usage, "primary")


def normalized_weekly(usage: dict) -> dict:
    """Return the secondary 7-day usage window."""
    return normalized_window(usage, "secondary")


def _event_epoch(value):
    """Parse a Codex JSONL event timestamp into epoch seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError, OSError):
        return None


def local_token_usage_since(home: str, since) -> dict | None:
    """Count local input/output tokens consumed on or after ``since``.

    Token telemetry is cumulative within each session. Counting positive
    increments avoids duplicate ``token_count`` events while still handling a
    session that began before the current server usage window.
    """
    since_epoch = _event_epoch(since)
    sessions = os.path.join(os.path.expanduser(home), "sessions")
    if since_epoch is None or not os.path.isdir(sessions):
        return None

    # Session directories are partitioned by date. Include a one-day margin on
    # each side for timezone differences and sessions that straddle the reset.
    first_day = datetime.fromtimestamp(
        since_epoch, tz=timezone.utc).date() - timedelta(days=1)
    last_day = datetime.now(timezone.utc).date() + timedelta(days=1)
    paths = []
    day = first_day
    while day <= last_day:
        folder = os.path.join(sessions, f"{day:%Y}", f"{day:%m}", f"{day:%d}")
        try:
            paths.extend(os.path.join(folder, name) for name in os.listdir(folder)
                         if name.endswith(".jsonl"))
        except OSError:
            pass
        day += timedelta(days=1)

    consumed = {"input_tokens": 0, "output_tokens": 0}
    found = False
    for path in paths:
        filename = os.path.basename(path)
        if not filename.endswith(".jsonl"):
            continue
        try:
            previous = {"input_tokens": 0, "output_tokens": 0}
            with open(path, "r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    payload = event.get("payload") or {}
                    if event.get("type") != "event_msg" or \
                            payload.get("type") != "token_count":
                        continue
                    info = payload.get("info") or {}
                    usage = info.get("total_token_usage") or {}
                    if usage.get("input_tokens") is None and \
                            usage.get("output_tokens") is None:
                        continue
                    current = {}
                    try:
                        for key in previous:
                            current[key] = int(usage.get(key) or 0)
                    except (TypeError, ValueError):
                        continue
                    timestamp = _event_epoch(event.get("timestamp"))
                    if timestamp is not None and timestamp >= since_epoch:
                        for key, value in current.items():
                            delta = value - previous[key] \
                                if value >= previous[key] else value
                            consumed[key] += max(0, delta)
                        found = True
                    previous = current
        except (OSError, PermissionError):
            continue
    consumed["total_tokens"] = (
        consumed["input_tokens"] + consumed["output_tokens"])
    return consumed if found else {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def local_tokens_since(home: str, since) -> int | None:
    """Return the combined local input/output token count."""
    usage = local_token_usage_since(home, since)
    return usage["total_tokens"] if usage is not None else None


if __name__ == "__main__":
    try:
        u = get_usage()
    except Exception as e:  # noqa: BLE001
        sys.exit(str(e))
    norm = normalized(u)
    if not norm:
        print("Could not map Codex fields. Raw payload:\n",
              json.dumps(u, indent=2)[:1500])
        sys.exit(0)
    from datetime import datetime, timezone

    def fmt(ts):
        try:
            dt = (datetime.fromtimestamp(ts, tz=timezone.utc)
                  if isinstance(ts, (int, float))
                  else datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
        except Exception:  # noqa: BLE001
            return str(ts)
        secs = int((dt - datetime.now(timezone.utc)).total_seconds())
        if secs <= 0:
            return "resetting now"
        return f"resets in {secs // 3600}h {(secs % 3600) // 60:02d}m"

    for label, b in norm.items():
        print(f"{label:<10} {b['utilization']:5.1f}%   {fmt(b['resets_at'])}")
