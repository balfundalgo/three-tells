"""
Contract resolution from Dhan's public scrip master.

Indices trade options on the index spot; MCX crude options are written on the
futures, so for CRUDEOIL / CRUDEOILM the "underlying" is the futures contract
the chosen option month is written on (Note 05, Section 02).
"""
from __future__ import annotations

import csv
import glob
import io
import os
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

import requests

from .config import app_dir, PREFIX, MCX_INSTRUMENTS, now_ist

SCRIP_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

SPEC = {
    "NIFTY":     dict(exch="NSE", opt_seg="NSE_FNO", opt_inst="OPTIDX", idx_id="13"),
    "BANKNIFTY": dict(exch="NSE", opt_seg="NSE_FNO", opt_inst="OPTIDX", idx_id="25"),
    "SENSEX":    dict(exch="BSE", opt_seg="BSE_FNO", opt_inst="OPTIDX", idx_id="51"),
    "CRUDEOIL":  dict(exch="MCX", opt_seg="MCX_COMM", opt_inst="OPTFUT", fut_inst="FUTCOM"),
    "CRUDEOILM": dict(exch="MCX", opt_seg="MCX_COMM", opt_inst="OPTFUT", fut_inst="FUTCOM"),
}


@dataclass
class OptionContract:
    security_id: str
    symbol: str
    strike: float
    opt_type: str          # CE / PE
    expiry: date
    lot_size: int
    segment: str


@dataclass
class Resolved:
    name: str
    # underlying (what the patterns read)
    und_segment: str
    und_id: str
    und_instrument: str    # charts API instrument: INDEX / FUTCOM
    und_label: str
    # options
    opt_segment: str
    expiry: date
    step: float
    chain: dict            # (strike, "CE"/"PE") -> OptionContract

    def pick(self, spot: float, side: str, strike_type: str, depth: int) -> OptionContract | None:
        """side: CE/PE. ITM for CE is below ATM, for PE above (Note 05, Section 02)."""
        atm = round(spot / self.step) * self.step
        shift = 0
        if strike_type == "ITM":
            shift = -depth if side == "CE" else depth
        elif strike_type == "OTM":
            shift = depth if side == "CE" else -depth
        strike = round(atm + shift * self.step, 2)
        c = self.chain.get((strike, side))
        if c is None:  # float drift guard
            for (k, t), v in self.chain.items():
                if t == side and abs(k - strike) < 1e-6:
                    return v
        return c


def _root(row: dict) -> str:
    sym = (row.get("SEM_TRADING_SYMBOL") or "").strip()
    if "-" in sym:
        return sym.split("-")[0].upper()
    cus = (row.get("SEM_CUSTOM_SYMBOL") or "").strip()
    return cus.split(" ")[0].upper() if cus else sym.upper()


def _expiry(row: dict) -> date | None:
    raw = (row.get("SEM_EXPIRY_DATE") or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw[:19] if " " in raw else raw, fmt).date()
        except ValueError:
            continue
    return None


class ScripMaster:
    def __init__(self, log=print):
        self.log = log
        self.rows: list[dict] = []

    def load(self) -> None:
        today = now_ist().strftime("%Y%m%d")
        path = os.path.join(app_dir(), f"{PREFIX}scrip_{today}.csv")
        text = None
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            self.log("Scrip master: using today's cached copy.")
        else:
            self.log("Scrip master: downloading from Dhan…")
            r = requests.get(SCRIP_URL, timeout=60)
            r.raise_for_status()
            text = r.text
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            for old in glob.glob(os.path.join(app_dir(), f"{PREFIX}scrip_*.csv")):
                if old != path:
                    try:
                        os.remove(old)
                    except OSError:
                        pass
        roots = set(SPEC)
        keep = []
        for row in csv.DictReader(io.StringIO(text)):
            if (row.get("SEM_EXM_EXCH_ID") or "").strip() not in ("NSE", "BSE", "MCX"):
                continue
            if (row.get("SEM_INSTRUMENT_NAME") or "").strip() not in ("OPTIDX", "OPTFUT", "FUTCOM"):
                continue
            if _root(row) in roots:
                keep.append(row)
        self.rows = keep
        self.log(f"Scrip master: {len(keep)} relevant contracts.")

    # ------------------------------------------------------------------
    def resolve(self, name: str, expiry_choice: str, today: date) -> Resolved:
        spec = SPEC[name]
        mcx = name in MCX_INSTRUMENTS
        opts = [r for r in self.rows
                if _root(r) == name
                and r["SEM_EXM_EXCH_ID"].strip() == spec["exch"]
                and r["SEM_INSTRUMENT_NAME"].strip() == spec["opt_inst"]]
        expiries = sorted({e for e in (_expiry(r) for r in opts) if e})
        # indices: today's expiry is still tradable; crude switches on its expiry day
        expiries = [e for e in expiries if (e > today if mcx else e >= today)]
        if not expiries:
            raise RuntimeError(f"{name}: no option expiries found in the scrip master.")
        idx = 1 if expiry_choice == "next" and len(expiries) > 1 else 0
        exp = expiries[idx]

        chain = {}
        for r in opts:
            if _expiry(r) != exp:
                continue
            t = (r.get("SEM_OPTION_TYPE") or "").strip().upper()
            if t not in ("CE", "PE"):
                continue
            try:
                strike = round(float(r["SEM_STRIKE_PRICE"]), 2)
                lot = int(float(r.get("SEM_LOT_UNITS") or 1))
            except (TypeError, ValueError):
                continue
            chain[(strike, t)] = OptionContract(
                security_id=str(r["SEM_SMST_SECURITY_ID"]).strip(),
                symbol=(r.get("SEM_CUSTOM_SYMBOL") or r.get("SEM_TRADING_SYMBOL") or "").strip(),
                strike=strike, opt_type=t, expiry=exp, lot_size=max(lot, 1),
                segment=spec["opt_seg"])
        strikes = sorted({k for k, _ in chain})
        if len(strikes) < 3:
            raise RuntimeError(f"{name}: option chain for {exp} is too thin.")
        diffs = Counter(round(b - a, 2) for a, b in zip(strikes, strikes[1:]) if b > a)
        step = diffs.most_common(1)[0][0]

        if mcx:
            futs = [r for r in self.rows
                    if _root(r) == name and r["SEM_EXM_EXCH_ID"].strip() == "MCX"
                    and r["SEM_INSTRUMENT_NAME"].strip() == "FUTCOM"]
            futs = [(e, r) for r in futs if (e := _expiry(r)) and e >= exp]
            if not futs:
                raise RuntimeError(f"{name}: no futures contract found for the {exp} options.")
            fexp, frow = min(futs, key=lambda x: x[0])
            return Resolved(name=name, und_segment="MCX_COMM",
                            und_id=str(frow["SEM_SMST_SECURITY_ID"]).strip(),
                            und_instrument="FUTCOM",
                            und_label=f"{name} FUT {fexp:%d-%b-%Y}",
                            opt_segment=spec["opt_seg"], expiry=exp, step=step, chain=chain)
        return Resolved(name=name, und_segment="IDX_I", und_id=spec["idx_id"],
                        und_instrument="INDEX", und_label=f"{name} spot",
                        opt_segment=spec["opt_seg"], expiry=exp, step=step, chain=chain)
