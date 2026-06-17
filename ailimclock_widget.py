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
REFRESH_MS = 60_000  # 60s

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
            # width fits the longest reset string, e.g. "22h 53m -> Thu 08:03"
            rst = tk.Label(row, text="", width=21, anchor="e", fg="#9a9a9a",
                           bg="#1e1e1e", font=("Segoe UI", 8))
            rst.pack(side="left", padx=(6, 0))
            self.bars[label] = (canvas, fill, pct, rst)

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
        for label, _color in ROWS:
            canvas, fill, pct_lbl, _rst = self.bars[label]
            bucket = merged.get(label)
            if not bucket:
                canvas.coords(fill, 0, 0, 0, 12)
                pct_lbl.config(text="--")
                self.resets.pop(label, None)
                continue
            pct, reset = core._extract(bucket)
            if pct is None:
                pct_lbl.config(text="--")
                continue
            shown += 1
            w = int(140 * max(0.0, min(100.0, pct)) / 100)
            canvas.coords(fill, 0, 0, w, 12)
            pct_lbl.config(text=f"{pct:.0f}%")
            self.resets[label] = parse_reset(reset)
        self._paint_resets()  # repaint countdowns immediately with the fresh data

        if errs:
            self.status.config(text="; ".join(errs)[:50], fg="#e06c6c")
        elif shown == 0:
            self.status.config(text="no data - see console", fg="#e06c6c")
        else:
            self.status.config(text="updated " + datetime.now().strftime("%H:%M"),
                               fg="#888")

    def _paint_resets(self):
        """Repaint the per-row reset countdowns from stored reset times."""
        for label, _color in ROWS:
            _c, _f, _p, rst = self.bars[label]
            dt = self.resets.get(label)
            if dt is None:
                rst.config(text="")
                continue
            cd, moment = fmt_reset(dt)
            rst.config(text=f"{cd} -> {moment}" if cd else "")

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
