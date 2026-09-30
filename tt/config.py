"""
Settings for The Three Tells (Strategy Note 05 v1.1).

Every setting in Section 07 of the note lives here. Settings are saved to
ttt_settings.json next to the EXE. Live mode is never restored from disk:
the app always opens in paper mode.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

INSTRUMENTS = ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL", "CRUDEOILM"]
MCX_INSTRUMENTS = {"CRUDEOIL", "CRUDEOILM"}
DEFAULT_BUFFER = {"NIFTY": 3.0, "BANKNIFTY": 10.0, "SENSEX": 10.0,
                  "CRUDEOIL": 3.0, "CRUDEOILM": 3.0}

APP_NAME = "The Three Tells"
APP_VERSION = "1.0"
PREFIX = "ttt_"


def app_dir() -> str:
    """Folder the EXE (or main.py) lives in — settings, logs and CSVs go here."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def now_ist() -> datetime:
    return datetime.now(IST)


@dataclass
class InstrumentSettings:
    enabled: bool = False
    lots: int = 1
    entry_buffer: float = 3.0
    stop_buffer: float = 3.0
    max_risk: float = 0.0          # points; 0 = off
    max_trades: int = 3


@dataclass
class Settings:
    # account
    client_id: str = ""
    access_token: str = ""
    token_file: str = r"C:\balfund_shared\dhan_token.json"

    # mode — never restored as live
    mode: str = "paper"

    # general
    timeframe: int = 5              # minutes: 1, 3, 5, 15
    pattern_a: bool = True          # Inside Bar
    pattern_b: bool = True          # Hammer / Inverted Hammer
    pattern_c: bool = True          # Failed Break

    # strike
    strike_type: str = "ATM"        # ATM / ITM / OTM
    strike_depth: int = 1           # 1-5, ignored for ATM
    expiry_choice: str = "current"  # current / next

    # target
    reward_ratio: float = 1.5

    # pattern rules
    ib_reference: str = "inside"    # inside / mother
    wick_ratio: float = 3.0
    opp_wick_pct: float = 25.0
    lookback: int = 10
    validity: int = 3

    # sessions
    nse_first: str = "09:15"
    nse_last_entry: str = "15:00"
    nse_squareoff: str = "15:15"
    mcx_first: str = "09:00"
    mcx_last_entry: str = "23:00"
    mcx_squareoff: str = "23:15"

    # plumbing
    poll_seconds: float = 4.0
    mcx_qty_in_lots: bool = True    # Dhan MCX quantity convention — verify once live

    instruments: dict = field(default_factory=dict)

    def __post_init__(self):
        fixed = {}
        for name in INSTRUMENTS:
            raw = self.instruments.get(name) if isinstance(self.instruments, dict) else None
            if isinstance(raw, InstrumentSettings):
                fixed[name] = raw
            elif isinstance(raw, dict):
                known = {f.name for f in fields(InstrumentSettings)}
                fixed[name] = InstrumentSettings(**{k: v for k, v in raw.items() if k in known})
            else:
                b = DEFAULT_BUFFER[name]
                fixed[name] = InstrumentSettings(enabled=(name == "NIFTY"),
                                                 entry_buffer=b, stop_buffer=b)
        self.instruments = fixed

    # ------------------------------------------------------------------
    def enabled_instruments(self) -> list[str]:
        return [n for n in INSTRUMENTS if self.instruments[n].enabled]

    def session_times(self, name: str) -> tuple[str, str, str]:
        if name in MCX_INSTRUMENTS:
            return self.mcx_first, self.mcx_last_entry, self.mcx_squareoff
        return self.nse_first, self.nse_last_entry, self.nse_squareoff

    def validate(self) -> list[str]:
        errs = []
        if not self.enabled_instruments():
            errs.append("Select at least one instrument.")
        if self.timeframe not in (1, 3, 5, 15):
            errs.append("Timeframe must be 1, 3, 5 or 15 minutes.")
        if self.strike_type not in ("ATM", "ITM", "OTM"):
            errs.append("Strike type must be ATM, ITM or OTM.")
        if not 1 <= int(self.strike_depth) <= 5:
            errs.append("Strike depth must be 1 to 5.")
        if self.expiry_choice not in ("current", "next"):
            errs.append("Expiry must be current or next.")
        if self.reward_ratio <= 0:
            errs.append("Reward ratio must be above 0.")
        if self.ib_reference not in ("inside", "mother"):
            errs.append("Inside bar reference must be inside or mother.")
        if self.wick_ratio <= 0:
            errs.append("Wick-to-body minimum must be above 0.")
        if not 0 < self.opp_wick_pct <= 100:
            errs.append("Opposite-wick maximum must be between 0 and 100%.")
        if self.lookback < 1:
            errs.append("Location lookback must be at least 1.")
        if self.validity < 1:
            errs.append("Setup validity must be at least 1 candle.")
        for key in ("nse_first", "nse_last_entry", "nse_squareoff",
                    "mcx_first", "mcx_last_entry", "mcx_squareoff"):
            try:
                parse_hm(getattr(self, key))
            except ValueError:
                errs.append(f"{key} must be HH:MM.")
        for name in self.enabled_instruments():
            s = self.instruments[name]
            if s.lots < 1:
                errs.append(f"{name}: lots must be at least 1.")
            if s.entry_buffer < 0 or s.stop_buffer < 0:
                errs.append(f"{name}: buffers cannot be negative.")
            if s.max_trades < 1:
                errs.append(f"{name}: max trades must be at least 1.")
        return errs

    def to_dict(self) -> dict:
        d = asdict(self)
        d["mode"] = "paper"
        return d


def parse_hm(text: str) -> tuple[int, int]:
    h, m = str(text).strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(text)
    return h, m


def at_time(day, hm: str) -> datetime:
    h, m = parse_hm(hm)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def settings_path() -> str:
    return os.path.join(app_dir(), PREFIX + "settings.json")


def load_settings() -> Settings:
    path = settings_path()
    if not os.path.exists(path):
        return Settings()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        known = {f.name for f in fields(Settings)}
        s = Settings(**{k: v for k, v in raw.items() if k in known})
        s.mode = "paper"
        return s
    except Exception:
        return Settings()


def save_settings(s: Settings) -> None:
    with open(settings_path(), "w", encoding="utf-8") as f:
        json.dump(s.to_dict(), f, indent=2)


def read_token_file(path: str) -> tuple[str, str]:
    """Read client id / token from the shared Dhan token file, if present.
    Accepts the common key spellings used by the Balfund token generator."""
    if not path or not os.path.exists(path):
        return "", ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return "", ""
    tok = d.get("access_token") or d.get("accessToken") or d.get("token") or ""
    cid = d.get("client_id") or d.get("clientId") or d.get("dhanClientId") or ""
    return str(cid), str(tok)
