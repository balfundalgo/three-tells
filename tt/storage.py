"""
Files written next to the EXE:
  ttt_activity_YYYYMMDD.log      everything the log panel shows
  ttt_trades_YYYYMMDD.csv        one row per closed trade
  ttt_state_<NAME>.json          trades taken today + any open trade (survives restart)
"""
from __future__ import annotations

import csv
import json
import os
import threading

from .config import app_dir, PREFIX, now_ist

_lock = threading.Lock()

TRADE_FIELDS = ["date", "mode", "instrument", "pattern", "side", "contract", "qty",
                "entry_time", "entry_underlying", "stop", "target", "entry_premium",
                "exit_time", "exit_underlying", "exit_premium", "exit_reason",
                "underlying_points", "pnl_rupees"]


def write_log_line(line: str) -> None:
    path = os.path.join(app_dir(), f"{PREFIX}activity_{now_ist():%Y%m%d}.log")
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def append_trade(row: dict) -> None:
    path = os.path.join(app_dir(), f"{PREFIX}trades_{now_ist():%Y%m%d}.csv")
    with _lock:
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)


def state_path(name: str) -> str:
    return os.path.join(app_dir(), f"{PREFIX}state_{name}.json")


def load_state(name: str) -> dict:
    """Today's state only — a file from an earlier day is ignored."""
    try:
        with open(state_path(name), "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == now_ist().strftime("%Y-%m-%d"):
            return d
    except (OSError, ValueError):
        pass
    return {}


def save_state(name: str, d: dict) -> None:
    d = dict(d)
    d["date"] = now_ist().strftime("%Y-%m-%d")
    tmp = state_path(name) + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, default=str)
        os.replace(tmp, state_path(name))
