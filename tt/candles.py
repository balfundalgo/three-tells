"""
Closed candles of the chosen timeframe, built from Dhan's REST 1-minute bars.

Buckets are anchored to each day's session start (09:15 NSE/BSE, 09:00 MCX),
so a 5-minute NIFTY candle is 09:15-09:20, 09:20-09:25, ...

A bucket counts as closed when its end time has passed and either its last
minute has arrived or a grace period has run out (thin contracts may skip a
minute). The minute still forming is never used.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import at_time

LATE_GRACE = timedelta(seconds=25)


@dataclass(frozen=True)
class Candle:
    start: datetime
    end: datetime
    o: float
    h: float
    l: float
    c: float

    @property
    def day(self):
        return self.start.date()

    @property
    def green(self) -> bool:
        return self.c > self.o

    @property
    def red(self) -> bool:
        return self.c < self.o

    def text(self) -> str:
        return (f"{self.start:%H:%M} O {self.o:.2f} H {self.h:.2f} "
                f"L {self.l:.2f} C {self.c:.2f}")


def rollup(bars: list[tuple], tf: int, session_first: str, now: datetime) -> list[Candle]:
    """bars: [(minute_start, o, h, l, c)] sorted or not. Returns closed candles, oldest first."""
    buckets: dict[datetime, list[tuple]] = {}
    for b in bars:
        t = b[0]
        if t + timedelta(minutes=1) > now:          # forming minute — never used
            continue
        anchor = at_time(t, session_first)
        if t < anchor:
            continue
        k = int((t - anchor).total_seconds() // 60) // tf
        bstart = anchor + timedelta(minutes=k * tf)
        buckets.setdefault(bstart, []).append(b)

    out = []
    for bstart in sorted(buckets):
        bend = bstart + timedelta(minutes=tf)
        rows = sorted(buckets[bstart], key=lambda x: x[0])
        if now < bend:
            continue
        last_minute_in = rows[-1][0] == bend - timedelta(minutes=1)
        if not last_minute_in and now < bend + LATE_GRACE:
            continue
        out.append(Candle(start=bstart, end=bend, o=rows[0][1],
                          h=max(r[2] for r in rows), l=min(r[3] for r in rows), c=rows[-1][4]))
    return out
