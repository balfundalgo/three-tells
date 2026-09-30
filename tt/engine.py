"""
One engine per instrument (Note 05). States:

  STARTING  loading history
  SCANNING  waiting for a pattern to complete
  ARMED     a setup is waiting for spot to touch its trigger
  IN TRADE  one option position is open
  DONE      no more entries today (last-entry time or trade limit)
  HALTED    account refusal or unknown order outcome — no new entries

Candles (REST, closed only) drive pattern detection. Live price (websocket,
REST fallback) drives triggers, cancels, stops, targets and square-off.
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta

from .broker import order_quantity
from .candles import Candle, rollup
from .config import Settings, MCX_INSTRUMENTS, at_time, now_ist
from .dhan_api import DhanError
from .instruments import OptionContract, Resolved
from .patterns import Rules, Setup, scan
from . import storage

HISTORY_DAYS = 6
RECONCILE_SECONDS = 15
EXIT_RETRY_SECONDS = 5
MAX_PRICE_AGE = 15


class Engine:
    def __init__(self, name: str, settings: Settings, resolved: Resolved, client, prices,
                 broker, log, halt_all, clock=now_ist):
        self.name = name
        self.s = settings
        self.inst = settings.instruments[name]
        self.r = resolved
        self.client = client
        self.prices = prices
        self.broker = broker
        self._log = log
        self.halt_all = halt_all
        self.clock = clock
        self.mcx = name in MCX_INSTRUMENTS
        self.first, self.last_entry, self.squareoff = settings.session_times(name)
        self.und_key = (resolved.und_segment, resolved.und_id)

        self.state = "STARTING"
        self.setup: Setup | None = None
        self.trade: dict | None = None
        self.trades_today = 0
        self.candles: list[Candle] = []
        self.last_patterns: dict[str, str] = {}
        self.ltp: float | None = None
        self.opt_ltp: float | None = None
        self.note = ""
        self.halted = False

        self.q: queue.Queue = queue.Queue()
        self.running = False
        self._bars: dict[datetime, tuple] = {}
        self._seen_end: datetime | None = None
        self._last_poll_err = 0.0
        self._last_reconcile = 0.0
        self._lock = threading.RLock()
        self._last_snap: dict = {}

    # ------------------------------------------------------------------ basics
    def log(self, msg: str):
        self._log(f"[{self.name}] {msg}")

    def rules(self) -> Rules:
        return Rules(pattern_a=self.s.pattern_a, pattern_b=self.s.pattern_b,
                     pattern_c=self.s.pattern_c, ib_reference=self.s.ib_reference,
                     wick_ratio=self.s.wick_ratio, opp_wick_pct=self.s.opp_wick_pct,
                     lookback=self.s.lookback, entry_buffer=self.inst.entry_buffer,
                     stop_buffer=self.inst.stop_buffer)

    def _t(self, hm: str, now: datetime) -> datetime:
        return at_time(now, hm)

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self.running = True
        self._restore()
        self.prices.watch([self.und_key])
        threading.Thread(target=self._poll_loop, daemon=True, name=f"ttt-poll-{self.name}").start()
        threading.Thread(target=self._run_loop, daemon=True, name=f"ttt-eng-{self.name}").start()

    def stop(self):
        self.running = False
        if self.trade:
            self.log("STOPPED with a position still open: "
                     f"{self.trade['contract']['symbol']} — it has NOT been closed.")

    def halt(self, reason: str):
        with self._lock:
            if self.halted:
                return
            self.halted = True
            self.setup = None
            if not self.trade:
                self.state = "HALTED"
            self.note = reason
            self.log(f"HALTED — {reason}")

    # ------------------------------------------------------------------ persistence
    def _persist(self):
        storage.save_state(self.name, {"trades_today": self.trades_today, "trade": self.trade})

    def _restore(self):
        d = storage.load_state(self.name)
        self.trades_today = int(d.get("trades_today") or 0)
        t = d.get("trade")
        if t:
            if t.get("mode") != self.s.mode:
                self.log(f"Found an open {t.get('mode')} trade from earlier today "
                         f"({t['contract']['symbol']}) but this session is {self.s.mode} — "
                         f"NOT restored. Check the Dhan app.")
            else:
                self.trade = t
                self.log(f"Restored open trade {t['contract']['symbol']} "
                         f"({t['side']}, entry {t['entry_premium']:.2f}).")
        if self.trades_today:
            self.log(f"Trades already taken today: {self.trades_today}/{self.inst.max_trades}.")

    # ------------------------------------------------------------------ candles
    def _poll_loop(self):
        first = True
        while self.running:
            now = self.clock()
            try:
                if first:
                    start = (now - timedelta(days=HISTORY_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    start = self._t(self.first, now) - timedelta(minutes=max(self.s.timeframe, 1) * 3)
                bars = self.client.intraday(self.r.und_segment, self.r.und_id,
                                            self.r.und_instrument, start, now)
                for b in bars:
                    self._bars[b[0]] = b
                closed = rollup(list(self._bars.values()), self.s.timeframe, self.first, self.clock())
                if first:
                    self.q.put(("seed", closed))
                    self._seen_end = closed[-1].end if closed else None
                    first = False
                else:
                    for c in closed:
                        if self._seen_end is None or c.end > self._seen_end:
                            self.q.put(("candle", c))
                            self._seen_end = c.end
                self._trim_bars(now)
            except DhanError as e:
                if e.permanent:
                    self.halt_all(f"{e.code}: {e.message}")
                elif time.time() - self._last_poll_err > 30:
                    self.log(f"Candle poll failed ({e}); retrying.")
                    self._last_poll_err = time.time()
            except Exception as e:  # never let the poller die silently
                if time.time() - self._last_poll_err > 30:
                    self.log(f"Candle poll error: {e!r}")
                    self._last_poll_err = time.time()
            time.sleep(self.s.poll_seconds)

    def _trim_bars(self, now):
        cutoff = now - timedelta(days=HISTORY_DAYS + 1)
        for k in [k for k in self._bars if k < cutoff]:
            del self._bars[k]

    # ------------------------------------------------------------------ main loop
    def _run_loop(self):
        while self.running:
            try:
                while True:
                    kind, payload = self.q.get_nowait()
                    if kind == "seed":
                        self.on_seed(payload)
                    else:
                        self.on_candle(payload)
            except queue.Empty:
                pass
            except Exception as e:
                self.log(f"Candle handling error: {e!r}")
            try:
                now = self.clock()
                p, age = self.prices.price(self.und_key)
                if p is not None and age < MAX_PRICE_AGE:
                    self.ltp = p
                    self.on_price(p, now)
                elif self.trade and now >= self._t(self.squareoff, now) and self.ltp is not None:
                    self.on_price(self.ltp, now)       # square-off even on a stale feed
                if self.trade:
                    k = (self.trade["contract"]["segment"], self.trade["contract"]["security_id"])
                    op, oage = self.prices.price(k)
                    if op is not None:
                        self.opt_ltp = op
                    self._reconcile(now)
            except Exception as e:
                self.log(f"Price handling error: {e!r}")
            time.sleep(0.25)

    def on_seed(self, closed: list[Candle]):
        with self._lock:
            self.candles = closed[-500:]
            now = self.clock()
            if self.trade:
                self.state = "IN TRADE"
                c = self.trade["contract"]
                self.prices.watch([(c["segment"], c["security_id"])])
            elif self.halted:
                self.state = "HALTED"
            elif self.trades_today >= self.inst.max_trades:
                self.state = "DONE"
                self.note = "trade limit reached"
            elif now >= self._t(self.last_entry, now):
                self.state = "DONE"
                self.note = "after last entry time"
            else:
                self.state = "SCANNING"
            last = self.candles[-1] if self.candles else None
            self.log(f"Ready on {self.r.und_label}, {self.s.timeframe}-min candles, "
                     f"options {self.r.expiry:%d-%b-%Y}, strike step {self.r.step:g}. "
                     + (f"Last closed candle {last.start:%d-%b %H:%M}; patterns are read from "
                        f"candles that close from now on." if last else "No candles yet."))

    def on_candle(self, c: Candle):
        with self._lock:
            self.candles.append(c)
            self.candles = self.candles[-500:]
            today = self.clock().date()
            if c.day != today or self.halted or self.state == "DONE" or self.trade:
                return
            if c.end >= self._t(self.last_entry, c.end):
                if self.setup:
                    self.log("Setup cancelled — last entry time reached.")
                self.setup = None
                self.state = "DONE"
                self.note = "after last entry time"
                return
            current = self.setup if self.state == "ARMED" else None
            found = scan(self.candles, today, self.rules(), current)
            if found:
                self.last_patterns[found.pattern] = f"{found.name} @ {c.end:%H:%M}"
                found = self._risk_filter(found)
                if found and found.sides():
                    replaced = " (replaces the earlier setup)" if self.setup else ""
                    self.setup = found
                    self.state = "ARMED"
                    self.log(f"ARMED {found.describe()} — candle {c.text()}{replaced}")
                return
            if self.state == "ARMED" and self.setup:
                self.setup.age += 1
                if self.setup.age >= self.s.validity:
                    self.log(f"Setup expired untriggered after {self.s.validity} candles: "
                             f"{self.setup.name}.")
                    self.setup = None
                    self.state = "SCANNING"

    def _risk_filter(self, s: Setup) -> Setup | None:
        cap = self.inst.max_risk
        if cap <= 0:
            return s
        for side in s.sides():
            if s.risk(side) > cap:
                self.log(f"{s.name} {side} side skipped: risk {s.risk(side):.2f} > max {cap:g}.")
                s = s.drop(side)
        return s

    # ------------------------------------------------------------------ prices
    def on_price(self, p: float, now: datetime):
        with self._lock:
            if self.trade:
                self._manage_trade(p, now)
                return
            if self.halted or self.state in ("DONE", "STARTING"):
                return
            if now >= self._t(self.last_entry, now):
                if self.setup:
                    self.log("Setup cancelled — last entry time reached.")
                self.setup = None
                self.state = "DONE"
                self.note = "after last entry time"
                return
            if self.state != "ARMED" or not self.setup:
                return
            s = self.setup
            if s.ce_trigger is not None:
                if p <= s.ce_stop:
                    self.log(f"CE side cancelled — price {p:.2f} reached its stop {s.ce_stop:.2f} "
                             f"before the trigger.")
                    s = self.setup = s.drop("CE")
                elif p >= s.ce_trigger:
                    self._enter("CE", p, now)
                    return
            if s.pe_trigger is not None:
                if p >= s.pe_stop:
                    self.log(f"PE side cancelled — price {p:.2f} reached its stop {s.pe_stop:.2f} "
                             f"before the trigger.")
                    s = self.setup = s.drop("PE")
                elif p <= s.pe_trigger:
                    self._enter("PE", p, now)
                    return
            if not s.sides():
                self.setup = None
                self.state = "SCANNING"

    def _enter(self, side: str, spot: float, now: datetime):
        s = self.setup
        if self.trades_today >= self.inst.max_trades:
            self.setup, self.state, self.note = None, "DONE", "trade limit reached"
            return
        stop = s.ce_stop if side == "CE" else s.pe_stop
        risk = abs(spot - stop)
        if self.inst.max_risk > 0 and risk > self.inst.max_risk:
            self.log(f"{side} trigger touched at {spot:.2f} but risk {risk:.2f} exceeds max "
                     f"{self.inst.max_risk:g} (gap) — side skipped.")
            self.setup = s.drop(side)
            if not self.setup.sides():
                self.setup, self.state = None, "SCANNING"
            return
        target = spot + self.s.reward_ratio * risk if side == "CE" else spot - self.s.reward_ratio * risk
        contract = self.r.pick(spot, side, self.s.strike_type, int(self.s.strike_depth))
        if contract is None:
            self.log(f"No {side} contract found near {spot:.2f} in the {self.r.expiry} chain — side skipped.")
            self.setup = s.drop(side)
            if not self.setup.sides():
                self.setup, self.state = None, "SCANNING"
            return
        qty = order_quantity(contract, self.inst.lots, self.mcx, self.s.mcx_qty_in_lots)
        self.log(f"TRIGGER {side} at {spot:.2f} ({s.name}) → buying {contract.symbol}, "
                 f"stop {stop:.2f}, target {target:.2f}.")
        tag = f"TTT{self.name[:4]}{now:%H%M%S}"
        try:
            fill = self.broker.buy(contract, qty, tag)
        except DhanError as e:
            if e.permanent:
                self.halt_all(f"{e.code}: {e.message}")
            else:
                self.log(f"Entry order failed ({e}). Setup dropped.")
                self.setup, self.state = None, "SCANNING"
            return
        if not fill.ok:
            self.log(f"Entry failed: {fill.note}.")
            self.setup = None
            if fill.unknown:
                self.halt(f"entry outcome unknown for {contract.symbol} — check the Dhan app "
                          f"before restarting")
            else:
                self.state = "SCANNING"
            return
        units = self.inst.lots * contract.lot_size
        self.trade = {
            "mode": self.s.mode, "pattern": s.name, "code": s.pattern, "side": side,
            "contract": {"security_id": contract.security_id, "symbol": contract.symbol,
                         "strike": contract.strike, "opt_type": contract.opt_type,
                         "expiry": str(contract.expiry), "lot_size": contract.lot_size,
                         "segment": contract.segment},
            "qty": qty, "units": units,
            "entry_time": now.strftime("%H:%M:%S"), "entry_underlying": round(spot, 2),
            "stop": round(stop, 2), "target": round(target, 2),
            "entry_premium": fill.price, "entry_order": fill.order_id,
            "exit_pending_since": 0.0,
        }
        self.trades_today += 1
        self.setup = None
        self.state = "IN TRADE"
        self.prices.watch([(contract.segment, contract.security_id)])
        self._persist()
        self.log(f"ENTERED {side} {contract.symbol} x{qty} @ {fill.price:.2f} "
                 f"(trade {self.trades_today}/{self.inst.max_trades}).")

    def _manage_trade(self, p: float, now: datetime):
        t = self.trade
        if t.get("exit_pending_since") and time.time() - t["exit_pending_since"] < EXIT_RETRY_SECONDS:
            return
        reason = None
        if now >= self._t(self.squareoff, now):
            reason = "SQUARE-OFF"
        elif t["side"] == "CE":
            if p <= t["stop"]:
                reason = "STOP"
            elif p >= t["target"]:
                reason = "TARGET"
        else:
            if p >= t["stop"]:
                reason = "STOP"
            elif p <= t["target"]:
                reason = "TARGET"
        if reason:
            self._exit(reason, p, now)

    def _contract(self) -> OptionContract:
        c = self.trade["contract"]
        return OptionContract(security_id=c["security_id"], symbol=c["symbol"], strike=c["strike"],
                              opt_type=c["opt_type"], expiry=c["expiry"], lot_size=c["lot_size"],
                              segment=c["segment"])

    def _exit(self, reason: str, spot: float, now: datetime):
        t = self.trade
        contract = self._contract()
        if not t.get("exit_pending_since"):
            self.log(f"EXIT {reason} at {spot:.2f} — selling {contract.symbol}.")
        try:
            fill = self.broker.sell(contract, t["qty"], f"TTTX{self.name[:3]}{now:%H%M%S}")
        except DhanError as e:
            if e.permanent:
                self.halt_all(f"{e.code}: {e.message}")
            self.log(f"Exit order failed ({e}); retrying in {EXIT_RETRY_SECONDS}s.")
            t["exit_pending_since"] = time.time()
            return
        if not fill.ok:
            if fill.flat:
                self.log("Broker shows no open quantity — the position was closed outside the app.")
                self._close(reason="CLOSED OUTSIDE APP", spot=spot, premium=self.opt_ltp or 0.0, now=now)
                return
            self.log(f"Exit failed: {fill.note}; retrying in {EXIT_RETRY_SECONDS}s.")
            t["exit_pending_since"] = time.time()
            return
        self._close(reason, spot, fill.price, now)

    def _close(self, reason: str, spot: float, premium: float, now: datetime):
        t = self.trade
        pts = (spot - t["entry_underlying"]) if t["side"] == "CE" else (t["entry_underlying"] - spot)
        pnl = (premium - t["entry_premium"]) * t["units"]
        storage.append_trade({
            "date": now.strftime("%Y-%m-%d"), "mode": t["mode"], "instrument": self.name,
            "pattern": t["pattern"], "side": t["side"], "contract": t["contract"]["symbol"],
            "qty": t["qty"], "entry_time": t["entry_time"], "entry_underlying": t["entry_underlying"],
            "stop": t["stop"], "target": t["target"], "entry_premium": t["entry_premium"],
            "exit_time": now.strftime("%H:%M:%S"), "exit_underlying": round(spot, 2),
            "exit_premium": premium, "exit_reason": reason,
            "underlying_points": round(pts, 2), "pnl_rupees": round(pnl, 2)})
        self.log(f"CLOSED {t['contract']['symbol']} @ {premium:.2f} — {reason}, "
                 f"P&L ₹{pnl:,.2f} ({pts:+.2f} pts on the underlying).")
        self.trade = None
        self.opt_ltp = None
        self._persist()
        if self.halted:
            self.state = "HALTED"
        elif self.trades_today >= self.inst.max_trades:
            self.state, self.note = "DONE", "trade limit reached"
        elif now >= self._t(self.last_entry, now):
            self.state, self.note = "DONE", "after last entry time"
        else:
            self.state = "SCANNING"

    def _reconcile(self, now: datetime):
        if not self.broker.live or not self.trade:
            return
        if time.time() - self._last_reconcile < RECONCILE_SECONDS:
            return
        self._last_reconcile = time.time()
        try:
            net = self.broker.net_qty(self._contract())
        except DhanError as e:
            if e.permanent:
                self.halt_all(f"{e.code}: {e.message}")
            return
        if net == 0:
            with self._lock:
                if self.trade:
                    self.log("Reconcile: broker position is flat — closed outside the app.")
                    self._close("CLOSED OUTSIDE APP", self.ltp or 0.0, self.opt_ltp or 0.0, now)

    # ------------------------------------------------------------------ GUI
    def snapshot(self) -> dict:
        # never block the GUI behind an order in flight — show the last picture instead
        if not self._lock.acquire(timeout=0.05):
            snap = dict(self._last_snap)
            if snap:
                snap["state"] = "ORDER IN FLIGHT"
            return snap
        try:
            self._last_snap = self._snapshot()
            return self._last_snap
        finally:
            self._lock.release()

    def _snapshot(self) -> dict:
        t = self.trade
        pos = None
        if t:
            cur = self.opt_ltp
            pos = {"symbol": t["contract"]["symbol"], "side": t["side"], "qty": t["qty"],
                   "entry": t["entry_premium"], "ltp": cur, "stop": t["stop"],
                   "target": t["target"], "pattern": t["pattern"], "code": t.get("code", ""),
                   "pnl": (cur - t["entry_premium"]) * t["units"] if cur is not None else None}
        last = self.candles[-1] if self.candles else None
        return {"name": self.name, "state": self.state, "ltp": self.ltp,
                "label": self.r.und_label, "expiry": f"{self.r.expiry:%d-%b-%Y}",
                "setup": self.setup.describe() if self.setup else "",
                "setup_pattern": self.setup.pattern if self.setup else "",
                "setup_age": self.setup.age if self.setup else 0,
                "position": pos, "trades": self.trades_today, "max_trades": self.inst.max_trades,
                "patterns": dict(self.last_patterns), "note": self.note,
                "last_candle": last.text() if last and last.day == self.clock().date() else ""}
