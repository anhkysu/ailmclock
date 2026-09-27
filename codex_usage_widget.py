#!/usr/bin/env python3
"""Codex Usage — a small always-on-top usage widget for six profiles."""

import os
import threading
import tkinter as tk
from datetime import datetime, timezone

import codex

REFRESH_MS = 60_000
BG = "#11131a"
PANEL = "#1b1f2a"
MUTED = "#8c95a8"
TEXT = "#eef2ff"
ACCENTS = (
    ("#9a8cff", "#6f5ee8"),
    ("#57ddba", "#25ad89"),
    ("#ffb45c", "#df8429"),
    ("#ff8aad", "#d94f7a"),
    ("#69c8ff", "#318fc9"),
    ("#d8a2ff", "#a15ed0"),
)
WINDOWS = (("5H", "primary"), ("WEEK", "secondary"))
PROFILES = tuple(
    (
        f"Codex {number}",
        os.path.join(
            os.path.expanduser("~"),
            ".codex" if number == 1 else f".codex{number}",
        ),
    )
    for number in range(1, 7)
)


def parse_reset(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except (ValueError, OSError, TypeError):
        return None


def reset_text(dt):
    if dt is None:
        return "reset time unavailable"
    seconds = int((dt - datetime.now(timezone.utc).astimezone()).total_seconds())
    if seconds <= 0:
        return "resetting now"
    days, rem = divmod(seconds, 86400)
    hours, minutes = divmod(rem, 3600)
    minutes //= 60
    countdown = f"{days}d {hours}h" if days else f"{hours}h {minutes:02d}m"
    when = dt.strftime("%H:%M") if dt.date() == datetime.now().date() else dt.strftime("%a %H:%M")
    return f"resets in {countdown}  ·  {when}"


def format_token_count(value):
    if value is None:
        return "--"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def format_token_usage(usage):
    if usage is None:
        return "tokens unavailable"
    return (f"{format_token_count(usage.get('input_tokens'))} in · "
            f"{format_token_count(usage.get('output_tokens'))} out")


class Widget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Codex Usage")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.96)
        self.root.configure(bg=BG)
        self.widgets = {}
        self.metric_rows = {}
        self.token_labels = {}
        self.copy_buttons = {}
        self.weekly_visible = True
        self._drag = (0, 0)
        self._build_ui()
        self.root.update_idletasks()
        self.root.geometry(
            f"250x{max(220, self.root.winfo_reqheight())}+80+80")
        self._bind_drag(self.root)
        self.root.bind_all("<Escape>", lambda _e: self.quit())
        self.root.bind_all("<Control-q>", lambda _e: self.quit())
        self.root.bind_all("<Button-3>", lambda _e: self.quit())
        self.root.after(200, self.refresh)
        self.root.after(1000, self.tick)

    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=9, pady=(7, 3))
        tk.Label(header, text="CODEX USAGE", fg=TEXT, bg=BG,
                 font=("Segoe UI", 7, "bold")).pack(side="left")
        self.window_label = tk.Label(
            header, text="5-HOUR + WEEKLY", fg=MUTED, bg=BG,
            font=("Segoe UI", 6, "bold"))
        self.window_label.pack(side="left", padx=5, pady=1)
        self.close = tk.Label(header, text="×", fg=MUTED, bg=BG,
                              font=("Segoe UI", 10), cursor="hand2")
        self.close.pack(side="right")
        self.weekly_toggle = tk.Button(
            header, text="WEEK −", command=self.toggle_weekly,
            padx=3, pady=0, relief="flat", bd=0,
            bg=BG, fg=MUTED, activebackground=BG,
            activeforeground=TEXT, cursor="hand2",
            font=("Segoe UI", 6, "bold"), takefocus=0)
        self.weekly_toggle.pack(side="right", padx=(0, 4))
        for (name, _home), accents in zip(PROFILES, ACCENTS):
            card = tk.Frame(self.root, bg=PANEL)
            card.pack(fill="x", padx=7, pady=2)
            top = tk.Frame(card, bg=PANEL)
            top.pack(fill="x", padx=7, pady=(4, 2))
            tk.Label(top, text=name, fg=TEXT, bg=PANEL,
                     font=("Segoe UI", 7, "bold")).pack(side="left")
            copy_button = tk.Button(
                top, text="⧉", command=lambda path=_home: self.copy_home(path),
                width=2, padx=0, pady=0, relief="flat", bd=0,
                bg=PANEL, fg=MUTED, activebackground=PANEL,
                activeforeground=TEXT, cursor="hand2",
                font=("Segoe UI Symbol", 8), takefocus=0)
            copy_button.pack(side="right", padx=(3, 0))
            self.copy_buttons[name] = copy_button
            tokens = tk.Label(top, text="-- tok", fg=MUTED, bg=PANEL,
                              font=("Segoe UI", 6, "bold"))
            tokens.pack(side="right", padx=(2, 0))
            self.token_labels[name] = tokens

            metrics = tk.Frame(card, bg=PANEL)
            metrics.pack(fill="x", padx=7, pady=(0, 4))
            self.widgets[name] = {}
            self.metric_rows[name] = {}
            for (label, which), accent in zip(WINDOWS, accents):
                metric = tk.Frame(metrics, bg=PANEL)
                metric.pack(fill="x", pady=1)
                tk.Label(metric, text=label, width=5, anchor="w", fg=MUTED,
                         bg=PANEL, font=("Segoe UI", 6, "bold")).pack(
                             side="left")
                pct = tk.Label(metric, text="--", width=4, anchor="e",
                               fg=accent, bg=PANEL,
                               font=("Segoe UI", 7, "bold"))
                pct.pack(side="right")
                reset = tk.Label(metric, text="waiting", width=17, fg=MUTED,
                                 bg=PANEL, anchor="e", font=("Segoe UI", 6))
                reset.pack(side="right")
                # Canvas defaults to a few hundred pixels wide. Explicitly use
                # a tiny requested width so it expands only into the remaining
                # row space instead of clipping the following controls.
                bar = tk.Canvas(metric, width=1, height=5, bg="#2d3342",
                                highlightthickness=0)
                bar.pack(side="left", fill="x", expand=True, padx=(2, 4))
                fill = bar.create_rectangle(0, 0, 0, 5, fill=accent, width=0)
                self.widgets[name][which] = (bar, fill, pct, reset)
                self.metric_rows[name][which] = metric
            self._bind_drag(card)
        self.status = tk.Label(self.root, text="connecting…", fg=MUTED, bg=BG,
                               font=("Segoe UI", 6))
        self.status.pack(fill="x", padx=9, pady=(2, 5))
        self.close.bind("<Button-1>", lambda _e: self.quit())

    def _bind_drag(self, widget):
        if (widget is getattr(self, "close", None)
                or widget is getattr(self, "weekly_toggle", None)
                or widget in self.copy_buttons.values()):
            return
        widget.bind("<Button-1>", self.start_drag)
        widget.bind("<B1-Motion>", self.drag)
        for child in widget.winfo_children():
            self._bind_drag(child)

    def start_drag(self, event):
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag(self, event):
        self.root.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def toggle_weekly(self):
        self.weekly_visible = not self.weekly_visible
        for name, _home in PROFILES:
            weekly_row = self.metric_rows[name]["secondary"]
            token_label = self.token_labels[name]
            if self.weekly_visible:
                weekly_row.pack(fill="x", pady=1)
                token_label.pack(side="right", padx=(2, 0))
            else:
                weekly_row.pack_forget()
                token_label.pack_forget()

        self.weekly_toggle.config(
            text="WEEK −" if self.weekly_visible else "WEEK +")
        self.window_label.config(
            text="5-HOUR + WEEKLY" if self.weekly_visible else "5-HOUR ONLY")

        # Keep the current screen position while fitting the window to the
        # newly visible set of rows.
        self.root.update_idletasks()
        self.root.geometry(
            f"250x{max(140, self.root.winfo_reqheight())}"
            f"+{self.root.winfo_x()}+{self.root.winfo_y()}")

    def copy_home(self, home):
        command = f'$env:CODEX_HOME="{os.path.normpath(home)}"'
        self.root.clipboard_clear()
        self.root.clipboard_append(command)
        self.root.update()  # keep clipboard contents after the widget exits
        self.status.config(text="copied CODEX_HOME command", fg="#36c7a0")
        self.root.after(1400, lambda: self.status.config(
            text="ready", fg=MUTED))

    def refresh(self):
        threading.Thread(target=self.fetch, daemon=True).start()
        self.root.after(REFRESH_MS, self.refresh)

    def fetch(self):
        results, errors = {}, []
        for name, home in PROFILES:
            try:
                usage = codex.get_usage(codex.read_token_for(home))
                windows = {
                    which: codex.normalized_window(usage, which)
                    for _label, which in WINDOWS
                }
                weekly = windows["secondary"]
                if weekly:
                    weekly["local_token_usage"] = codex.local_token_usage_since(
                        home, weekly.get("starts_at"))
                results[name] = windows
            except FileNotFoundError:
                results[name] = None
            except Exception as exc:  # noqa: BLE001
                results[name] = None
                errors.append(f"{name}: {exc}")
        self.root.after(0, self.render, results, errors)

    def render(self, results, errors):
        shown = 0
        for name, _home in PROFILES:
            windows = results.get(name)
            if windows is not None and any(windows.values()):
                shown += 1
            for label, which in WINDOWS:
                bar, fill, pct, reset = self.widgets[name][which]
                bucket = windows.get(which) if windows is not None else None
                value = bucket.get("utilization") if bucket else None
                if value is None:
                    bar.coords(fill, 0, 0, 0, 5)
                    pct.config(text="--")
                    unavailable = "not logged in" if windows is None \
                        else f"{label.lower()} unavailable"
                    reset.config(text=unavailable)
                    continue
                value = max(0.0, min(100.0, float(value)))
                bar.coords(fill, 0, 0, bar.winfo_width() * value / 100, 5)
                pct.config(text=f"{value:.0f}%")
                reset.config(text=reset_text(
                    parse_reset(bucket.get("resets_at"))))
            weekly = windows.get("secondary") if windows else None
            self.token_labels[name].config(text=format_token_usage(
                weekly.get("local_token_usage") if weekly else None))
        if errors:
            self.status.config(text=" · ".join(errors)[:70], fg="#e87979")
        else:
            self.status.config(
                text=(f"updated {datetime.now().strftime('%H:%M')}  ·  "
                      f"{shown}/{len(PROFILES)} connected"),
                fg=MUTED)

    def tick(self):
        self.root.after(1000, self.tick)

    def quit(self):
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Widget().run()
