#!/usr/bin/env python3
"""
ailimclock widget - a tiny always-on-top desktop widget for Claude usage.

Shows the 5-hour (session) and weekly limits as live progress bars in a small
frameless window that floats on top of everything. Drag it anywhere; it
remembers where you put it.

Run:
    pythonw ailimclock_widget.py      (no console window)
    python  ailimclock_widget.py      (with console, for seeing errors)

Session key:
    Reads AILIMCLOCK_SESSION_KEY, else a local 'session.key' file next to this
    script. If neither exists, the widget pops a one-time box to paste it and
    saves it to 'session.key' (gitignored).

Stdlib only (tkinter). No pip installs needed.
"""

import json
import os
import threading
import tkinter as tk
from datetime import datetime, timezone
from tkinter import simpledialog

# Reuse the API logic from the CLI tools.
import ailimclock as core
import codex

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "session.key")
POS_FILE = os.path.join(HERE, "widget.pos")
HIST_FILE = os.path.join(HERE, "history.json")
REFRESH_MS = 60_000  # 60s

# Pacing history, per window kind (keyed by the last word of a row label).
# A 5-hour limit moves fast, so look back only 10 min; a weekly limit barely
# budges in 10 min, so measure its pace over a few hours instead.
HIST_LOOKBACK = {"5h": 10 * 60, "Wk": 3 * 3600}
HIST_MIN_SPAN = {"5h": 3 * 60,  "Wk": 20 * 60}

# How to show the burn rate, per window kind: (interval seconds, unit suffix).
# 5-hour -> "%/h", weekly -> "%/24h".
RATE_DISPLAY = {"5h": (3600, "/h"), "Wk": (86400, "/24h")}

# Rows to display, in order: (label, bar color)
# Bars keep two shades per provider (lighter = 5h, darker = weekly)...
ROWS = [
    ("Claude 5h", "#f0883e"),
    ("Claude Wk", "#c96a2b"),
    ("Codex 5h",  "#7b6cf0"),
    ("Codex Wk",  "#4f46c4"),
]

# ...but the row LABELS use one flat color per provider.
LABEL_COLORS = {
    "Claude": "#f0883e",  # orange
    "Codex":  "#7b6cf0",  # violet-blue
}


def label_color(label: str) -> str:
    return LABEL_COLORS.get(label.split()[0], "#dddddd")

# Window lengths, keyed by the last word of a row label ("5h" / "Wk").
# Used by the pacing advisor to know how far through a window we are.
WINDOW_SECONDS = {
    "5h": 5 * 3600,
    "Wk": 7 * 86400,
}

# Claude usage payload -> row label, with field aliases (schema is undocumented).
CLAUDE_MAP = {
    "Claude 5h": ("five_hour", "fiveHour", "session"),
    "Claude Wk": ("seven_day", "sevenDay", "weekly"),
}


# ---------- session key handling ----------

def load_key(root) -> str:
    key = os.environ.get("AILIMCLOCK_SESSION_KEY")
    if key:
        return key.strip()
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "r", encoding="utf-8") as f:
            k = f.read().strip()
            if k:
                return k
    # Prompt once.
    k = simpledialog.askstring(
        "ailimclock",
        "Paste your claude.ai sessionKey cookie (sk-ant-sid01-...):",
        parent=root, show="*")
    if not k:
        root.destroy()
        raise SystemExit("No sessionKey provided.")
    k = k.strip()
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(k)
    return k


# ---------- usage history (for the trailing burn rate) ----------

def load_history() -> dict:
    """{row label -> [[epoch, pct], ...]} of past usage samples."""
    if os.path.exists(HIST_FILE):
        try:
            with open(HIST_FILE, encoding="utf-8") as f:
                h = json.load(f)
                if isinstance(h, dict):
                    return h
        except (OSError, ValueError):
            pass
    return {}


def save_history(hist: dict):
    try:
        with open(HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f)
    except OSError:
        pass


def trailing_rate(samples, window_start, now, lookback, min_span):
    """Current burn rate in %/hour from recent samples, or None if unknown.

    Only samples from the current window (ts >= window_start) are used, so a
    reset -- which drops usage back near 0 -- never reads as a huge negative
    rate. Needs at least two samples spanning `min_span` seconds.
    """
    if not samples:
        return None
    recent = [s for s in samples
              if s[0] >= window_start and now - s[0] <= lookback]
    if len(recent) < 2:
        return None
    recent.sort()
    (t0, p0), (t1, p1) = recent[0], recent[-1]
    span_h = (t1 - t0) / 3600
    if span_h < min_span / 3600:
        return None
    return (p1 - p0) / span_h


def fmt_rate(rate, label) -> str:
    """Render a %/hour rate in the window's natural unit (%/10m or %/24h)."""
    if rate is None:
        return ""
    secs, unit = RATE_DISPLAY.get(label.split()[-1], (3600, "/h"))
    val = rate * secs / 3600
    if abs(val) < 0.5:
        return "flat"
    return f"{val:+.0f}%{unit}"


# ---------- position persistence ----------

def load_pos() -> str:
    if os.path.exists(POS_FILE):
        try:
            with open(POS_FILE) as f:
                return f.read().strip()
        except OSError:
            pass
    return "+80+80"


def save_pos(geom: str):
    try:
        with open(POS_FILE, "w") as f:
            f.write(geom)
    except OSError:
        pass


# ---------- reset-time formatting ----------

def parse_reset(val):
    """Normalize a reset value (epoch seconds or ISO-8601 string) to a tz-aware
    datetime in the local zone, or None if it can't be parsed."""
    if val is None:
        return None
    try:
        if isinstance(val, (int, float)):
            dt = datetime.fromtimestamp(val, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, OSError):
        return None
    return dt.astimezone()  # local time


def fmt_reset(dt):
    """-> ('1h 42m', '14:30') countdown + absolute local clock moment.

    The weekday is shown whenever the reset falls on a different calendar day
    than today -- independent of whether the countdown is under 24h. (A reset
    23h away can still be *tomorrow*.)
    """
    if dt is None:
        return "", ""
    now = datetime.now(timezone.utc).astimezone()
    secs = int((dt - now).total_seconds())
    if secs <= 0:
        return "now", dt.strftime("%H:%M")
    d, rem = divmod(secs, 86400)
    h, m = rem // 3600, (rem % 3600) // 60
    cd = f"{d}d {h}h" if d else f"{h}h {m:02d}m"
    same_day = dt.date() == now.date()
    moment = dt.strftime("%H:%M") if same_day else dt.strftime("%a %H:%M")
    return cd, moment


# ---------- pacing advisor ----------

def _ratio_to_action(ratio):
    """Map a (current pace / sustainable pace) ratio to an action + color."""
    if ratio >= 1.5:
        return "slow down", "#e06c6c"   # on track to exhaust before reset
    if ratio >= 1.15:
        return "ease up", "#e0b24c"
    if ratio >= 0.6:
        return "steady", "#5fb56f"
    return "use more", "#6fa8d4"        # plenty of headroom -- safe to do more


def pace_hint(label, pct, dt, rate=None):
    """Judge usage pace for one window -> (note, color).

    With a trailing `rate` (%/hour, from history) it compares how fast you're
    *currently* burning against the rate you could sustain and still land at
    100% exactly at reset. Without history yet, it falls back to a static
    used%-vs-elapsed% estimate.
    """
    if pct is None or dt is None:
        return "", "#9a9a9a"
    win = WINDOW_SECONDS.get(label.split()[-1])
    if not win:
        return "", "#9a9a9a"
    now = datetime.now(timezone.utc).astimezone()
    remaining = (dt - now).total_seconds()

    if pct >= 95:
        return "stop", "#e06c6c"        # limit essentially spent

    if rate is not None:
        # Dynamic: compare the *current* burn rate to what's sustainable.
        remaining_h = remaining / 3600
        if remaining_h <= 0:
            return "go ahead", "#9a9a9a"   # resetting now
        sustainable = (100 - pct) / remaining_h    # %/h you can keep up
        # Low/zero rate naturally yields a small ratio -> "use more".
        ratio = rate / sustainable if sustainable > 0 else 99
        return _ratio_to_action(ratio)

    # Static fallback (no history yet): used% vs fraction of window elapsed.
    frac = min(1.0, max(0.0, (win - remaining) / win))
    if frac < 0.08:
        return "go ahead", "#9a9a9a"    # fresh window -- use it freely
    expected = frac * 100
    ratio = pct / expected if expected > 0 else 0
    return _ratio_to_action(ratio)


# ---------- the widget ----------

class Widget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ailimclock")
        self.root.overrideredirect(True)          # frameless
        self.root.attributes("-topmost", True)    # always on top
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg="#1e1e1e")
        self.root.geometry(load_pos())

        self.session_key = load_key(self.root)
        self.org_id = None
        self.resets = {}                          # row label -> reset datetime
        self.pcts = {}                            # row label -> used percent
        self.history = load_history()             # row label -> [[epoch, pct]]

        self._build_ui()
        self._make_draggable()
        self.root.after(200, self.refresh)        # first fetch shortly after show
        self._tick()                              # live reset countdown, every 1s

    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}
        header = tk.Frame(self.root, bg="#1e1e1e")
        header.pack(fill="x")
        tk.Label(header, text="AI usage", fg="#bbbbbb", bg="#1e1e1e",
                 font=("Segoe UI", 9, "bold")).pack(side="left", **pad)
        self.close_btn = tk.Label(header, text="✕", fg="#777", bg="#1e1e1e",
                                  font=("Segoe UI", 9), cursor="hand2")
        self.close_btn.pack(side="right", padx=8)
        self.close_btn.bind("<Button-1>", lambda e: self.quit())
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.config(fg="#e06c6c"))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.config(fg="#777"))

        self.bars = {}
        for label, color in ROWS:
            row = tk.Frame(self.root, bg="#1e1e1e")
            row.pack(fill="x", **pad)
            tk.Label(row, text=label, width=9, anchor="w", fg=label_color(label),
                     bg="#1e1e1e", font=("Segoe UI", 9, "bold")).pack(side="left")
            canvas = tk.Canvas(row, width=140, height=12, bg="#333333",
                               highlightthickness=0)
            canvas.pack(side="left", padx=6)
            fill = canvas.create_rectangle(0, 0, 0, 12, fill=color, width=0)
            pct = tk.Label(row, text="--", width=4, anchor="e", fg="#dddddd",
                           bg="#1e1e1e", font=("Segoe UI", 9))
            pct.pack(side="left")
            # current burn rate, e.g. "+12%/h" (5h) / "+14%/24h" (weekly)
            rate = tk.Label(row, text="", width=9, anchor="e", fg="#9a9a9a",
                            bg="#1e1e1e", font=("Segoe UI", 8))
            rate.pack(side="left", padx=(6, 0))
            # width fits the longest reset string, e.g. "22h 53m -> Thu 08:03"
            rst = tk.Label(row, text="", width=21, anchor="e", fg="#9a9a9a",
                           bg="#1e1e1e", font=("Segoe UI", 8))
            rst.pack(side="left", padx=(6, 0))
            # pacing note (an action), e.g. "slow down" / "steady" / "use more"
            hint = tk.Label(row, text="", width=9, anchor="e", fg="#9a9a9a",
                            bg="#1e1e1e", font=("Segoe UI", 8, "bold"))
            hint.pack(side="left", padx=(6, 0))
            self.bars[label] = (canvas, fill, pct, rst, hint, rate)

        self.status = tk.Label(self.root, text="loading…", fg="#888",
                               bg="#1e1e1e", font=("Segoe UI", 8))
        self.status.pack(fill="x", padx=10, pady=(0, 6))

    def _make_draggable(self):
        self._drag = (0, 0)
        for w in (self.root,):
            w.bind("<Button-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)
        # also let labels drag the window -- but never steal the close button
        def bind_children(widget):
            for c in widget.winfo_children():
                if c is self.close_btn:
                    continue
                if isinstance(c, (tk.Label, tk.Frame)):
                    c.bind("<Button-1>", self._start_drag)
                    c.bind("<B1-Motion>", self._on_drag)
                bind_children(c)
        bind_children(self.root)
        # keyboard fallbacks so it can always be closed
        self.root.bind_all("<Escape>", lambda e: self.quit())
        self.root.bind_all("<Control-q>", lambda e: self.quit())
        # right-click anywhere -> Quit menu
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Quit ailimclock", command=self.quit)
        self.root.bind_all("<Button-3>",
                           lambda e: self.menu.tk_popup(e.x_root, e.y_root))

    def _start_drag(self, e):
        self._drag = (e.x_root - self.root.winfo_x(),
                      e.y_root - self.root.winfo_y())

    def _on_drag(self, e):
        x = e.x_root - self._drag[0]
        y = e.y_root - self._drag[1]
        self.root.geometry(f"+{x}+{y}")

    # ----- data -----

    def refresh(self):
        threading.Thread(target=self._fetch, daemon=True).start()
        self.root.after(REFRESH_MS, self.refresh)

    def _fetch(self):
        """Gather both providers into one {row_label: bucket} dict.

        Each provider is independent: one failing never blocks the other.
        """
        merged = {}
        errs = []

        # --- Claude (web API + sessionKey cookie) ---
        try:
            if self.org_id is None:
                self.org_id = core.get_org_id(self.session_key)
            cu = core.get_usage(self.session_key, self.org_id)
            for label, keys in CLAUDE_MAP.items():
                bucket = next((cu[k] for k in keys if k in cu), None)
                if bucket is not None:
                    merged[label] = bucket
        except Exception as e:  # noqa: BLE001
            errs.append(f"Claude: {e}")

        # --- Codex (wham/usage + ~/.codex/auth.json token) ---
        try:
            cxu = codex.get_usage()
            merged.update(codex.normalized(cxu))  # keys: "Codex 5h"/"Codex Wk"
        except FileNotFoundError:
            pass  # Codex not logged in on this machine -> just skip its rows
        except Exception as e:  # noqa: BLE001
            errs.append(f"Codex: {e}")

        self.root.after(0, self._render, merged, errs)

    def _render(self, merged: dict, errs: list):
        shown = 0
        now = datetime.now(timezone.utc).timestamp()
        for label, _color in ROWS:
            canvas, fill, pct_lbl, _rst, _hint, _rate = self.bars[label]
            bucket = merged.get(label)
            if not bucket:
                canvas.coords(fill, 0, 0, 0, 12)
                pct_lbl.config(text="--")
                self.resets.pop(label, None)
                self.pcts.pop(label, None)
                continue
            if label.startswith("Codex"):
                # codex.normalized() already produced a final 0-100 percent and
                # an absolute reset epoch. Do NOT re-run core._extract here: its
                # "<=1.0 means a fraction, x100" heuristic would turn a just-reset
                # 1% window into 100%.
                pct, reset = bucket.get("utilization"), bucket.get("resets_at")
            else:
                pct, reset = core._extract(bucket)
            if pct is None:
                pct_lbl.config(text="--")
                continue
            shown += 1
            w = int(140 * max(0.0, min(100.0, pct)) / 100)
            canvas.coords(fill, 0, 0, w, 12)
            pct_lbl.config(text=f"{pct:.0f}%")
            new_reset = parse_reset(reset)
            if self._reset_rolled(label, new_reset, pct):
                self.history.pop(label, None)   # new window -> start history clean
            self.resets[label] = new_reset
            self.pcts[label] = pct
            self._record(label, now, pct)
        save_history(self.history)
        self._paint_resets()  # repaint countdowns immediately with the fresh data

        if errs:
            self.status.config(text="; ".join(errs)[:50], fg="#e06c6c")
        elif shown == 0:
            self.status.config(text="no data - see console", fg="#e06c6c")
        else:
            self.status.config(text="updated " + datetime.now().strftime("%H:%M"),
                               fg="#888")

    def _reset_rolled(self, label, new_reset, pct):
        """True if this row's window just reset since the last fetch.

        Detected two ways: the reset moment jumped forward into a new window, or
        usage fell sharply (a fresh window starts back near 0%).
        """
        prev_reset = self.resets.get(label)
        if new_reset and prev_reset and \
                (new_reset - prev_reset).total_seconds() > 120:
            return True
        prev_pct = self.pcts.get(label)
        if prev_pct is not None and pct < prev_pct - 10:
            return True
        return False

    def _record(self, label, now, pct):
        """Append a usage sample and drop ones older than the current window."""
        win = WINDOW_SECONDS.get(label.split()[-1], 0)
        samples = self.history.setdefault(label, [])
        samples.append([now, pct])
        # Keep only this window's worth of history (+ a little slack).
        cutoff = now - win - 60
        samples[:] = [s for s in samples if s[0] >= cutoff][-500:]

    def _rate_for(self, label, now):
        """Trailing burn rate (%/h) for a row, scoped to its current window."""
        suf = label.split()[-1]
        win = WINDOW_SECONDS.get(suf)
        dt = self.resets.get(label)
        if not win or dt is None:
            return None
        window_start = dt.timestamp() - win
        return trailing_rate(self.history.get(label, []), window_start, now,
                             HIST_LOOKBACK.get(suf, 600),
                             HIST_MIN_SPAN.get(suf, 180))

    def _paint_resets(self):
        """Repaint the per-row reset countdowns + burn rate + pacing note."""
        now = datetime.now(timezone.utc).timestamp()
        for label, _color in ROWS:
            _c, _f, _p, rst, hint, rate_lbl = self.bars[label]
            dt = self.resets.get(label)
            if dt is None:
                rst.config(text="")
                hint.config(text="")
                rate_lbl.config(text="")
                continue
            cd, moment = fmt_reset(dt)
            rst.config(text=f"{cd} -> {moment}" if cd else "")
            rate = self._rate_for(label, now)
            rate_lbl.config(text=fmt_rate(rate, label))
            note, color = pace_hint(label, self.pcts.get(label), dt, rate)
            hint.config(text=note, fg=color)

    def _tick(self):
        """Tick the countdowns once a second (no network), then reschedule."""
        self._paint_resets()
        self.root.after(1000, self._tick)

    def _error(self, msg: str):
        self.status.config(text=msg[:40], fg="#e06c6c")

    def quit(self, *_):
        try:
            save_pos(f"+{self.root.winfo_x()}+{self.root.winfo_y()}")
        except tk.TclError:
            pass
        try:
            self.root.quit()      # stop mainloop
            self.root.destroy()   # tear down widgets
        except tk.TclError:
            pass
        # background fetch threads are daemons, but force a clean exit anyway
        os._exit(0)

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.mainloop()


if __name__ == "__main__":
    Widget().run()
