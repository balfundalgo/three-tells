"""
The three tells (Note 05, Sections 03-05). Pure functions, no I/O.

A setup carries a trigger and a stop for each side it arms. All levels are
underlying prices (index spot, or the crude futures).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from .candles import Candle


@dataclass
class Rules:
    pattern_a: bool = True
    pattern_b: bool = True
    pattern_c: bool = True
    ib_reference: str = "inside"   # inside / mother
    wick_ratio: float = 3.0
    opp_wick_pct: float = 25.0
    lookback: int = 10
    entry_buffer: float = 3.0
    stop_buffer: float = 3.0


@dataclass
class Setup:
    pattern: str                    # A / B / C
    name: str                       # human label
    candle: Candle                  # the candle that completed the pattern
    ce_trigger: Optional[float] = None
    ce_stop: Optional[float] = None
    pe_trigger: Optional[float] = None
    pe_stop: Optional[float] = None
    mother: Optional[Candle] = None  # pattern A only
    age: int = 0                     # candles closed since the pattern candle

    def sides(self) -> list[str]:
        s = []
        if self.ce_trigger is not None:
            s.append("CE")
        if self.pe_trigger is not None:
            s.append("PE")
        return s

    def risk(self, side: str) -> float:
        if side == "CE":
            return self.ce_trigger - self.ce_stop
        return self.pe_stop - self.pe_trigger

    def drop(self, side: str) -> "Setup":
        if side == "CE":
            return replace(self, ce_trigger=None, ce_stop=None)
        return replace(self, pe_trigger=None, pe_stop=None)

    def describe(self) -> str:
        parts = [self.name]
        if self.ce_trigger is not None:
            parts.append(f"CE ≥ {self.ce_trigger:.2f} (stop {self.ce_stop:.2f})")
        if self.pe_trigger is not None:
            parts.append(f"PE ≤ {self.pe_trigger:.2f} (stop {self.pe_stop:.2f})")
        return " · ".join(parts)


# ---------------------------------------------------------------- anatomy
def body(c: Candle) -> float:
    return abs(c.c - c.o)


def upper_wick(c: Candle) -> float:
    return c.h - max(c.o, c.c)


def lower_wick(c: Candle) -> float:
    return min(c.o, c.c) - c.l


def rng(c: Candle) -> float:
    return c.h - c.l


# ---------------------------------------------------------------- A
def is_inside(mother: Candle, cur: Candle) -> bool:
    if cur.h == mother.h and cur.l == mother.l:
        return False
    return cur.h <= mother.h and cur.l >= mother.l


def inside_setup(mother: Candle, inside: Candle, r: Rules) -> Setup:
    ref = inside if r.ib_reference == "inside" else mother
    return Setup(pattern="A", name="Inside Bar", candle=inside, mother=mother,
                 ce_trigger=round(ref.h + r.entry_buffer, 2),
                 ce_stop=round(mother.l - r.stop_buffer, 2),
                 pe_trigger=round(ref.l - r.entry_buffer, 2),
                 pe_stop=round(mother.h + r.stop_buffer, 2))


# ---------------------------------------------------------------- B
def hammer_setup(cur: Candle, history: list[Candle], r: Rules) -> Optional[Setup]:
    """history: closed candles ending with cur (may include earlier sessions)."""
    R = rng(cur)
    if R <= 0:
        return None
    b, uw, lw = body(cur), upper_wick(cur), lower_wick(cur)
    cap = r.opp_wick_pct / 100.0 * R
    window = history[-r.lookback:] if r.lookback > 0 else [cur]
    if lw > 0 and lw >= r.wick_ratio * b and uw <= cap and cur.l <= min(x.l for x in window):
        return Setup(pattern="B", name="Hammer", candle=cur,
                     ce_trigger=round(cur.h + r.entry_buffer, 2),
                     ce_stop=round(cur.l - r.stop_buffer, 2))
    if uw > 0 and uw >= r.wick_ratio * b and lw <= cap and cur.h >= max(x.h for x in window):
        return Setup(pattern="B", name="Inverted Hammer", candle=cur,
                     pe_trigger=round(cur.l - r.entry_buffer, 2),
                     pe_stop=round(cur.h + r.stop_buffer, 2))
    return None


# ---------------------------------------------------------------- C
def failed_break_setup(prev: Candle, cur: Candle, r: Rules) -> Optional[Setup]:
    if prev.green and cur.red and cur.h > prev.h and cur.c < prev.h:
        return Setup(pattern="C", name="Failed Break (bearish)", candle=cur,
                     pe_trigger=round(cur.l - r.entry_buffer, 2),
                     pe_stop=round(cur.h + r.stop_buffer, 2))
    if prev.red and cur.green and cur.l < prev.l and cur.c > prev.l:
        return Setup(pattern="C", name="Failed Break (bullish)", candle=cur,
                     ce_trigger=round(cur.h + r.entry_buffer, 2),
                     ce_stop=round(cur.l - r.stop_buffer, 2))
    return None


# ---------------------------------------------------------------- scan
def scan(history: list[Candle], session_day, r: Rules,
         current: Optional[Setup] = None) -> Optional[Setup]:
    """Evaluate the newest closed candle. Priority C, then B, then A.

    Pattern candles must belong to today's session; earlier sessions only feed
    the hammer's location lookback. If an Inside Bar setup is armed and the new
    candle is again inside the same mother, the mother is kept and the new
    candle becomes the reference (Section 03, consecutive inside bars).
    """
    if not history:
        return None
    cur = history[-1]
    if cur.day != session_day:
        return None
    prev = history[-2] if len(history) >= 2 and history[-2].day == session_day else None

    if r.pattern_c and prev is not None:
        s = failed_break_setup(prev, cur, r)
        if s:
            return s
    if r.pattern_b:
        s = hammer_setup(cur, history, r)
        if s:
            return s
    if r.pattern_a:
        if current is not None and current.pattern == "A" and current.mother is not None \
                and is_inside(current.mother, cur):
            s = inside_setup(current.mother, cur, r)
            s.name = "Inside Bar (again inside mother)"
            return s
        if prev is not None and is_inside(prev, cur):
            return inside_setup(prev, cur, r)
    return None
