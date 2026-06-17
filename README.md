# ailimclock

A tiny clock for your AI coding usage limits — shows the **5-hour session** and
**weekly** windows (with reset times) for **Claude** *and* **OpenAI Codex**, in a
CLI and an always-on-top desktop widget.

No login flow, no third-party Python packages. Stdlib only.

## How it works
Two providers, two mechanisms — both reusing credentials already on your machine:

**Claude** — calls the same endpoints the claude.ai settings page uses,
authenticated with your browser's `sessionKey` cookie:
1. `GET https://claude.ai/api/organizations` → your org UUID
2. `GET https://claude.ai/api/organizations/{orgId}/usage` → 5h + weekly usage

**Codex** — reads the OAuth token the Codex CLI already stored, then calls the
ChatGPT backend:
1. token from `~/.codex/auth.json` (field `tokens.access_token`)
2. `GET https://chatgpt.com/backend-api/wham/usage` with `Authorization: Bearer …`
   → `primary_window` (5h) + `secondary_window` (weekly)

So both sets of numbers match what each vendor's own UI shows. Each provider is
independent — if one isn't set up, its rows are simply skipped.

## Setup
1. Log into <https://claude.ai> in your browser.
2. DevTools → Application/Storage → Cookies → `https://claude.ai`.
   Copy the **`sessionKey`** value (looks like `sk-ant-sid01-...`).
3. Provide it via env var or flag:
   ```powershell
   $env:AILIMCLOCK_SESSION_KEY = "sk-ant-sid01-..."
   python ailimclock.py
   ```
   ```powershell
   python ailimclock.py --watch          # live, refresh every 60s
   python ailimclock.py --session-key sk-ant-sid01-...
   ```

## Example output
```
ailimclock  @ 08:41:12
5-hour session   [########----------------]  33.0%   resets in 1h 42m
Weekly (all)     [###---------------------]  12.0%   resets in 4d 06h 10m
```

## ⚠️ Security
Your `sessionKey` **is** your full claude.ai login. Keep it private — never commit
it, never send it anywhere off your machine. It's read locally and used only for
these two GET requests.

## Status / caveats
- The `/usage` JSON schema is **undocumented**; field names may change. The script
  probes a few aliases and, if it can't map them, prints the raw payload so the
  mapping can be fixed.
- Endpoints are an unofficial consumer API — treat as best-effort, not a contract.
