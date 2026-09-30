"""
The Three Tells — desktop app (CustomTkinter).

Dashboard: one card per running instrument — state, live price, the armed
setup, which of the three patterns it came from, the open position and P&L —
plus the activity log. Settings: every input from Note 05 Section 07.
"""
from __future__ import annotations

import copy
import queue
import threading
import tkinter.messagebox as mb
from datetime import datetime

import customtkinter as ctk

from .config import (Settings, INSTRUMENTS, APP_NAME, APP_VERSION, IST,
                     load_settings, save_settings)
from .storage import write_log_line
from .supervisor import Supervisor

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BG = "#0b1220"
CARD = "#131c2e"
CARD2 = "#1a2540"
TEXT = "#e2e8f0"
MUTED = "#8b9ab4"
BLUE = "#3b82f6"
GREEN = "#22c55e"
RED = "#ef4444"
AMBER = "#f59e0b"
VIOLET = "#a855f7"
CYAN = "#06b6d4"

STATE_COLOUR = {"STARTING": MUTED, "SCANNING": BLUE, "ARMED": AMBER, "IN TRADE": GREEN,
                "ORDER IN FLIGHT": VIOLET, "DONE": "#64748b", "HALTED": RED}
PATTERN_INFO = [("A", "Inside Bar", CYAN), ("B", "Hammer", VIOLET), ("C", "Failed Break", AMBER)]

# Every settings control: (section, label, path, kind, choices)
FIELDS = [
    ("Account", "Dhan client id", "client_id", "str", None),
    ("Account", "Access token", "access_token", "secret", None),
    ("Account", "Shared token file", "token_file", "str", None),
    ("General", "Candle timeframe (min)", "timeframe", "choice_int", ["1", "3", "5", "15"]),
    ("General", "Pattern A — Inside Bar", "pattern_a", "bool", None),
    ("General", "Pattern B — Hammer / Inverted Hammer", "pattern_b", "bool", None),
    ("General", "Pattern C — Failed Break", "pattern_c", "bool", None),
    ("Strike", "Strike type", "strike_type", "choice", ["ATM", "ITM", "OTM"]),
    ("Strike", "Strike depth (1-5)", "strike_depth", "choice_int", ["1", "2", "3", "4", "5"]),
    ("Strike", "Expiry", "expiry_choice", "choice", ["current", "next"]),
    ("Target & pattern rules", "Reward ratio", "reward_ratio", "float", None),
    ("Target & pattern rules", "Inside Bar breakout reference", "ib_reference", "choice", ["inside", "mother"]),
    ("Target & pattern rules", "Wick-to-body minimum", "wick_ratio", "float", None),
    ("Target & pattern rules", "Opposite-wick maximum (% of range)", "opp_wick_pct", "float", None),
    ("Target & pattern rules", "Location lookback (candles)", "lookback", "int", None),
    ("Target & pattern rules", "Setup validity (candles)", "validity", "int", None),
    ("Session — NSE / BSE", "First candle", "nse_first", "str", None),
    ("Session — NSE / BSE", "Last entry", "nse_last_entry", "str", None),
    ("Session — NSE / BSE", "Square-off", "nse_squareoff", "str", None),
    ("Session — MCX", "First candle", "mcx_first", "str", None),
    ("Session — MCX", "Last entry", "mcx_last_entry", "str", None),
    ("Session — MCX", "Square-off", "mcx_squareoff", "str", None),
    ("Advanced", "Candle poll (seconds)", "poll_seconds", "float", None),
    ("Advanced", "MCX order quantity is in lots", "mcx_qty_in_lots", "bool", None),
]
INST_FIELDS = [("Trade", "enabled", "bool"), ("Lots", "lots", "int"),
               ("Entry buffer", "entry_buffer", "float"), ("Stop buffer", "stop_buffer", "float"),
               ("Max risk (0=off)", "max_risk", "float"), ("Max trades", "max_trades", "int")]


def get_path(s: Settings, path: str):
    obj = s
    for part in path.split("."):
        obj = obj[part] if isinstance(obj, dict) else getattr(obj, part)
    return obj


def set_path(s: Settings, path: str, value):
    parts = path.split(".")
    obj = s
    for part in parts[:-1]:
        obj = obj[part] if isinstance(obj, dict) else getattr(obj, part)
    if not hasattr(obj, parts[-1]):
        raise AttributeError(path)
    setattr(obj, parts[-1], value)


def all_paths() -> list[str]:
    paths = [f[2] for f in FIELDS]
    for name in INSTRUMENTS:
        paths += [f"instruments.{name}.{k}" for _, k, _ in INST_FIELDS]
    return paths


class InstrumentCard(ctk.CTkFrame):
    def __init__(self, master, name):
        super().__init__(master, fg_color=CARD, corner_radius=14, border_width=1, border_color="#22304f")
        self.grid_columnconfigure(0, weight=1)
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 2))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text=name, font=("Segoe UI", 18, "bold"), text_color=TEXT).grid(row=0, column=0, sticky="w")
        self.state = ctk.CTkLabel(top, text="STARTING", font=("Segoe UI", 11, "bold"), corner_radius=10,
                                  fg_color=MUTED, text_color="#0b1220", width=110, height=24)
        self.state.grid(row=0, column=1, sticky="e")

        self.label = ctk.CTkLabel(self, text="", font=("Segoe UI", 11), text_color=MUTED)
        self.label.grid(row=1, column=0, sticky="w", padx=14)
        self.ltp = ctk.CTkLabel(self, text="—", font=("Segoe UI", 28, "bold"), text_color=TEXT)
        self.ltp.grid(row=2, column=0, sticky="w", padx=14)

        pats = ctk.CTkFrame(self, fg_color="transparent")
        pats.grid(row=3, column=0, sticky="ew", padx=12, pady=(6, 2))
        self.badges = {}
        for i, (code, nm, col) in enumerate(PATTERN_INFO):
            pats.grid_columnconfigure(i, weight=1)
            b = ctk.CTkLabel(pats, text=f"{code} · {nm}", font=("Segoe UI", 11, "bold"), corner_radius=8,
                             fg_color=CARD2, text_color=MUTED, height=26)
            b.grid(row=0, column=i, sticky="ew", padx=2)
            self.badges[code] = (b, col)
        self.seen = ctk.CTkLabel(self, text="", font=("Segoe UI", 10), text_color=MUTED, justify="left",
                                 wraplength=380)
        self.seen.grid(row=4, column=0, sticky="w", padx=14)

        self.setup = ctk.CTkLabel(self, text="", font=("Segoe UI", 12), text_color=AMBER, justify="left",
                                  wraplength=380)
        self.setup.grid(row=5, column=0, sticky="w", padx=14, pady=(6, 0))

        pos = ctk.CTkFrame(self, fg_color=CARD2, corner_radius=10)
        pos.grid(row=6, column=0, sticky="ew", padx=12, pady=8)
        pos.grid_columnconfigure(0, weight=1)
        self.pos = ctk.CTkLabel(pos, text="No open position", font=("Segoe UI", 12), text_color=MUTED,
                                justify="left", wraplength=260)
        self.pos.grid(row=0, column=0, sticky="w", padx=10, pady=8)
        self.pnl = ctk.CTkLabel(pos, text="", font=("Segoe UI", 18, "bold"), text_color=TEXT)
        self.pnl.grid(row=0, column=1, sticky="e", padx=10)

        self.foot = ctk.CTkLabel(self, text="", font=("Segoe UI", 11), text_color=MUTED, justify="left",
                                 wraplength=380)
        self.foot.grid(row=7, column=0, sticky="w", padx=14, pady=(0, 12))

    def update_from(self, d: dict):
        if not d:
            return
        st = d["state"]
        self.state.configure(text=st, fg_color=STATE_COLOUR.get(st, MUTED))
        self.configure(border_color=STATE_COLOUR.get(st, "#22304f") if st in ("ARMED", "IN TRADE", "HALTED")
                       else "#22304f")
        self.label.configure(text=f"{d['label']}  ·  options {d['expiry']}")
        self.ltp.configure(text=f"{d['ltp']:,.2f}" if d["ltp"] is not None else "—")
        active_code = d["setup_pattern"] or (d["position"]["code"] if d["position"] else None)
        for code, (b, col) in self.badges.items():
            on = code == active_code
            b.configure(fg_color=col if on else CARD2, text_color="#0b1220" if on else MUTED)
        seen = [f"{k}: {v}" for k, v in sorted(d["patterns"].items())]
        self.seen.configure(text=("Last seen — " + "   ".join(seen)) if seen else "No pattern yet today")
        if d["setup"]:
            self.setup.configure(text=f"⚑ {d['setup']}   (candle {d['setup_age']} of validity)")
        else:
            self.setup.configure(text="")
        p = d["position"]
        if p:
            ltp = f"{p['ltp']:.2f}" if p["ltp"] is not None else "—"
            self.pos.configure(text=f"{p['side']}  {p['symbol']}  x{p['qty']}\nentry {p['entry']:.2f}  ·  ltp {ltp}\n"
                                    f"stop {p['stop']:.2f}  ·  target {p['target']:.2f}", text_color=TEXT)
            if p["pnl"] is not None:
                self.pnl.configure(text=f"₹{p['pnl']:,.0f}", text_color=GREEN if p["pnl"] >= 0 else RED)
        else:
            self.pos.configure(text="No open position", text_color=MUTED)
            self.pnl.configure(text="")
        foot = f"Trades today {d['trades']}/{d['max_trades']}"
        if d["last_candle"]:
            foot += f"   ·   last candle {d['last_candle']}"
        if d["note"]:
            foot += f"\n{d['note']}"
        self.foot.configure(text=foot)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"Balfund · {APP_NAME} v{APP_VERSION}")
        self.geometry("1360x880")
        self.minsize(1100, 720)
        self.configure(fg_color=BG)
        self.settings = load_settings()
        self.sup: Supervisor | None = None
        self.logq: queue.Queue = queue.Queue()
        self.cards: dict[str, InstrumentCard] = {}
        self.widgets: dict[str, tuple] = {}
        self.mode = "paper"
        self._build()
        self.after(300, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log(f"{APP_NAME} v{APP_VERSION} ready — paper mode. Press START.")

    # ------------------------------------------------------------------ log
    def log(self, msg: str):
        line = f"{datetime.now(IST):%H:%M:%S}  {msg}"
        try:
            write_log_line(line)
        except OSError:
            pass
        self.logq.put(line)

    # ------------------------------------------------------------------ layout
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(self, fg_color="#0f1a30", corner_radius=0, height=72)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(1, weight=1)
        t = ctk.CTkFrame(head, fg_color="transparent")
        t.grid(row=0, column=0, padx=18, pady=10, sticky="w")
        ctk.CTkLabel(t, text="THE THREE TELLS", font=("Segoe UI", 22, "bold"), text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(t, text="Balfund · Strategy Note 05 · Inside Bar · Hammer · Failed Break",
                     font=("Segoe UI", 11), text_color=MUTED).pack(anchor="w")

        self.status = ctk.CTkLabel(head, text="● idle", font=("Segoe UI", 12, "bold"), text_color=MUTED)
        self.status.grid(row=0, column=1, sticky="e", padx=10)
        self.mode_btn = ctk.CTkSegmentedButton(head, values=["PAPER", "LIVE"], command=self._mode_changed,
                                               selected_color=BLUE, font=("Segoe UI", 12, "bold"))
        self.mode_btn.set("PAPER")
        self.mode_btn.grid(row=0, column=2, padx=10)
        self.start_btn = ctk.CTkButton(head, text="▶ START", width=110, height=38, fg_color=GREEN,
                                       hover_color="#16a34a", text_color="#0b1220",
                                       font=("Segoe UI", 13, "bold"), command=self._start)
        self.start_btn.grid(row=0, column=3, padx=6)
        self.stop_btn = ctk.CTkButton(head, text="■ STOP", width=110, height=38, fg_color=RED,
                                      hover_color="#dc2626", font=("Segoe UI", 13, "bold"),
                                      command=self._stop, state="disabled")
        self.stop_btn.grid(row=0, column=4, padx=(6, 18))

        self.tabs = ctk.CTkTabview(self, fg_color=BG, segmented_button_selected_color=BLUE)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 12))
        dash = self.tabs.add("Dashboard")
        sett = self.tabs.add("Settings")
        self._build_dash(dash)
        self._build_settings(sett)

    def _build_dash(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=3)
        parent.grid_rowconfigure(2, weight=2)
        self.card_area = ctk.CTkScrollableFrame(parent, fg_color=BG)
        self.card_area.grid(row=0, column=0, sticky="nsew")
        for i in range(3):
            self.card_area.grid_columnconfigure(i, weight=1, uniform="c")
        self.empty = ctk.CTkLabel(self.card_area, text="Press START to begin. Instruments are chosen in Settings.",
                                  font=("Segoe UI", 14), text_color=MUTED)
        self.empty.grid(row=0, column=0, columnspan=3, pady=60)

        bar = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
        bar.grid(row=1, column=0, sticky="ew", pady=6)
        self.total = ctk.CTkLabel(bar, text="Open P&L ₹0   ·   Trades today 0", font=("Segoe UI", 13, "bold"),
                                  text_color=TEXT)
        self.total.pack(side="left", padx=14, pady=8)
        self.feed_lbl = ctk.CTkLabel(bar, text="", font=("Segoe UI", 12), text_color=MUTED)
        self.feed_lbl.pack(side="right", padx=14)

        ctk.CTkLabel(parent, text="Activity log", font=("Segoe UI", 12, "bold"), text_color=MUTED).grid(
            row=2, column=0, sticky="nw", padx=4)
        self.logbox = ctk.CTkTextbox(parent, fg_color="#070d18", text_color="#cbd5e1",
                                     font=("Consolas", 12), corner_radius=10)
        self.logbox.grid(row=2, column=0, sticky="nsew", pady=(22, 0))
        self.logbox.configure(state="disabled")
        for tag, col in (("arm", AMBER), ("enter", GREEN), ("exit", CYAN), ("bad", RED)):
            self.logbox.tag_config(tag, foreground=col)

    def _build_settings(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        sc = ctk.CTkScrollableFrame(parent, fg_color=BG)
        sc.grid(row=0, column=0, sticky="nsew")
        sc.grid_columnconfigure(0, weight=1)
        sc.grid_columnconfigure(1, weight=1)

        sections: dict[str, ctk.CTkFrame] = {}
        order = []
        for sec, *_ in FIELDS:
            if sec not in order:
                order.append(sec)
        for i, sec in enumerate(order):
            f = ctk.CTkFrame(sc, fg_color=CARD, corner_radius=12)
            f.grid(row=i // 2, column=i % 2, sticky="nsew", padx=6, pady=6)
            f.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(f, text=sec, font=("Segoe UI", 14, "bold"), text_color=BLUE).grid(
                row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 4))
            sections[sec] = f
        rows: dict[str, int] = {}
        for sec, label, path, kind, choices in FIELDS:
            f = sections[sec]
            r = rows.get(sec, 1)
            rows[sec] = r + 1
            ctk.CTkLabel(f, text=label, text_color=TEXT, font=("Segoe UI", 12)).grid(
                row=r, column=0, sticky="w", padx=12, pady=3)
            w = self._make_widget(f, kind, choices, get_path(self.settings, path))
            w.grid(row=r, column=1, sticky="ew", padx=12, pady=3)
            self.widgets[path] = (w, kind)

        inst = ctk.CTkFrame(sc, fg_color=CARD, corner_radius=12)
        inst.grid(row=(len(order) + 1) // 2 + 1, column=0, columnspan=2, sticky="ew", padx=6, pady=6)
        ctk.CTkLabel(inst, text="Instruments", font=("Segoe UI", 14, "bold"), text_color=BLUE).grid(
            row=0, column=0, columnspan=7, sticky="w", padx=12, pady=(10, 4))
        ctk.CTkLabel(inst, text="Instrument", text_color=MUTED).grid(row=1, column=0, padx=12, sticky="w")
        for j, (lab, _, _) in enumerate(INST_FIELDS):
            ctk.CTkLabel(inst, text=lab, text_color=MUTED).grid(row=1, column=j + 1, padx=8)
        for i, name in enumerate(INSTRUMENTS):
            ctk.CTkLabel(inst, text=name, text_color=TEXT, font=("Segoe UI", 12, "bold")).grid(
                row=i + 2, column=0, padx=12, pady=3, sticky="w")
            for j, (_, key, kind) in enumerate(INST_FIELDS):
                path = f"instruments.{name}.{key}"
                w = self._make_widget(inst, kind, None, get_path(self.settings, path), width=110)
                w.grid(row=i + 2, column=j + 1, padx=8, pady=3)
                self.widgets[path] = (w, kind)

        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.grid(row=1, column=0, sticky="ew", pady=6)
        self.save_btn = ctk.CTkButton(btns, text="💾  Save settings", fg_color=BLUE, height=36,
                                      font=("Segoe UI", 13, "bold"), command=self._save)
        self.save_btn.pack(side="right", padx=10)
        ctk.CTkLabel(btns, text="Settings are locked while running. Live mode is never saved — the app "
                                "always opens in paper.", text_color=MUTED).pack(side="left", padx=10)

    def _make_widget(self, parent, kind, choices, value, width=200):
        if kind == "bool":
            v = ctk.BooleanVar(value=bool(value))
            w = ctk.CTkCheckBox(parent, text="", variable=v, width=30)
            w._var = v
            return w
        if kind in ("choice", "choice_int"):
            w = ctk.CTkOptionMenu(parent, values=choices, width=width, fg_color=CARD2, button_color=BLUE)
            w.set(str(value))
            return w
        w = ctk.CTkEntry(parent, width=width, show="•" if kind == "secret" else None)
        w.insert(0, str(value))
        return w

    # ------------------------------------------------------------------ settings io
    def _read_widgets(self) -> Settings:
        s = copy.deepcopy(self.settings)
        for path, (w, kind) in self.widgets.items():
            if kind == "bool":
                val = bool(w._var.get())
            else:
                raw = w.get().strip()
                if kind in ("int", "choice_int"):
                    val = int(float(raw))
                elif kind == "float":
                    val = float(raw)
                else:
                    val = raw
            set_path(s, path, val)
        return s

    def _save(self) -> bool:
        try:
            s = self._read_widgets()
        except ValueError as e:
            mb.showerror(APP_NAME, f"A number field has an invalid value ({e}).")
            return False
        errs = s.validate()
        if errs:
            mb.showerror(APP_NAME, "Please fix:\n\n" + "\n".join(errs))
            return False
        self.settings = s
        save_settings(s)
        self.log("Settings saved.")
        return True

    def _lock_settings(self, locked: bool):
        state = "disabled" if locked else "normal"
        for w, _ in self.widgets.values():
            try:
                w.configure(state=state)
            except Exception:
                pass
        self.save_btn.configure(state=state)
        self.mode_btn.configure(state=state)

    # ------------------------------------------------------------------ run
    def _mode_changed(self, value):
        if value == "LIVE":
            names = self.settings.enabled_instruments()
            limits = ", ".join(f"{n} {self.settings.instruments[n].max_trades}" for n in names)
            d = ctk.CTkInputDialog(text=f"LIVE mode sends real orders to Dhan.\n\n"
                                        f"Instruments: {', '.join(names)}\nMax trades: {limits}\n\n"
                                        f"Type LIVE to confirm:", title="Confirm live trading")
            if (d.get_input() or "").strip().upper() == "LIVE":
                self.mode = "live"
                self.log("LIVE mode selected for this session.")
                return
            self.mode_btn.set("PAPER")
        self.mode = "paper"

    def _start(self):
        if not self._save():
            return
        s = self._read_widgets()
        s.mode = self.mode
        self.start_btn.configure(state="disabled")
        self._lock_settings(True)
        self.status.configure(text="● starting…", text_color=AMBER)

        def run():
            sup = Supervisor(s, self.log)
            try:
                sup.start()
            except Exception as e:
                self.log(f"START FAILED — {e}")
                try:
                    sup.stop()
                except Exception:
                    pass
                self.after(0, self._reset_idle)
                return
            self.sup = sup
            self.after(0, self._started)

        threading.Thread(target=run, daemon=True).start()

    def _started(self):
        self.stop_btn.configure(state="normal")
        colour = RED if self.mode == "live" else GREEN
        self.status.configure(text=f"● running · {self.mode.upper()}", text_color=colour)
        self.empty.grid_forget()
        for c in self.cards.values():
            c.destroy()
        self.cards = {}
        for i, name in enumerate(self.sup.engines):
            card = InstrumentCard(self.card_area, name)
            card.grid(row=i // 3, column=i % 3, sticky="nsew", padx=6, pady=6)
            self.cards[name] = card
        self.tabs.set("Dashboard")

    def _reset_idle(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._lock_settings(False)
        self.status.configure(text="● idle", text_color=MUTED)

    def _stop(self):
        if not self.sup:
            return
        open_pos = self.sup.open_positions()
        if open_pos and not mb.askyesno(APP_NAME, "These positions are still open and will NOT be closed:\n\n"
                                        + "\n".join(open_pos) + "\n\nStop anyway?"):
            return
        self.sup.stop()
        self.sup = None
        self.mode = "paper"
        self.mode_btn.set("PAPER")
        self._reset_idle()

    def _on_close(self):
        if self.sup and self.sup.running:
            msg = "The strategy is running."
            if self.sup.open_positions():
                msg += "\nOpen positions will NOT be closed:\n" + "\n".join(self.sup.open_positions())
            if not mb.askyesno(APP_NAME, msg + "\n\nQuit anyway?"):
                return
            self.sup.stop()
        self.destroy()

    # ------------------------------------------------------------------ refresh
    def _tick(self):
        lines = []
        try:
            while True:
                lines.append(self.logq.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.logbox.configure(state="normal")
            for ln in lines:
                tag = None
                if "ARMED" in ln:
                    tag = "arm"
                elif "ENTERED" in ln or "TRIGGER" in ln:
                    tag = "enter"
                elif "CLOSED" in ln or "EXIT" in ln:
                    tag = "exit"
                elif any(w in ln for w in ("FAILED", "HALTED", "REFUSAL", "WARNING", "failed", "error")):
                    tag = "bad"
                self.logbox.insert("end", ln + "\n", tag)
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        if self.sup:
            snaps = self.sup.snapshot()
            total, trades = 0.0, 0
            for d in snaps:
                if d and d["name"] in self.cards:
                    self.cards[d["name"]].update_from(d)
                if d:
                    trades += d["trades"]
                    if d["position"] and d["position"]["pnl"] is not None:
                        total += d["position"]["pnl"]
            self.total.configure(text=f"Open P&L ₹{total:,.0f}   ·   Trades today {trades}",
                                 text_color=GREEN if total > 0 else RED if total < 0 else TEXT)
            live = self.sup.feed_connected()
            self.feed_lbl.configure(text="websocket ● connected" if live else "websocket ● down — REST prices",
                                    text_color=GREEN if live else AMBER)
        self.after(400, self._tick)


def main():
    App().mainloop()
