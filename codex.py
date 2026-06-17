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
from urllib import request, error

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
UA = "Mozilla/5.0 (ailimclock)"


def auth_path() -> str:
    home = os.environ.get("CODEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".codex")
    return os.path.join(home, "auth.json")


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
    return None


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
    if pct is not None and pct <= 1.0:
        pct *= 100
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
        win = _find_window(usage, which)
        if win is None:
            continue
        pct, reset = extract(win)
        if pct is None:
            continue
        out[label] = {"utilization": pct, "resets_at": reset}
    return out


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
