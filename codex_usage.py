#!/usr/bin/env python3
"""Print 5-hour and weekly usage for all local Codex profiles."""

import os
from datetime import datetime

import codex

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


def main():
    print(f"Codex Usage  @ {datetime.now():%H:%M:%S}")
    for name, home in PROFILES:
        try:
            usage = codex.get_usage(codex.read_token_for(home))
            primary = codex.normalized_primary(usage)
            weekly = codex.normalized_weekly(usage)
            if not primary and not weekly:
                print(f"{name:<8} usage data unavailable")
                continue
            if primary:
                print(f"{name:<8} 5-hour {primary['utilization']:5.1f}%   "
                      f"reset: {primary.get('resets_at') or 'unknown'}")
            else:
                print(f"{name:<8} 5-hour data unavailable")
            if weekly:
                tokens = codex.local_token_usage_since(
                    home, weekly.get("starts_at"))
                token_text = (f"{tokens['input_tokens']:,} in / "
                              f"{tokens['output_tokens']:,} out") \
                    if tokens is not None else "local tokens unavailable"
                print(f"{'':8} weekly {weekly['utilization']:5.1f}%   "
                      f"{token_text}   "
                      f"reset: {weekly.get('resets_at') or 'unknown'}")
            else:
                print(f"{'':8} weekly data unavailable")
        except FileNotFoundError:
            print(f"{name:<8} not logged in ({home})")
        except Exception as exc:  # noqa: BLE001
            print(f"{name:<8} error: {exc}")


if __name__ == "__main__":
    main()
