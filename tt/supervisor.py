"""
Supervisor: one Engine per selected instrument, sharing one Dhan client, one
websocket feed and one price service. An account-level refusal from any
engine halts every engine (it is about the account, not the instrument).
"""
from __future__ import annotations

import copy

from .broker import LiveBroker, PaperBroker
from .config import Settings, read_token_file, now_ist
from .dhan_api import DhanClient, DhanError, PERMANENT_HINT
from .engine import Engine
from .feed import LtpFeed, LtpService
from .instruments import ScripMaster


class Supervisor:
    def __init__(self, settings: Settings, log):
        self.s = copy.deepcopy(settings)
        self.log = log
        self.engines: dict[str, Engine] = {}
        self.feed = None
        self.prices = None
        self.client = None
        self.running = False
        self.halt_reason = ""

    def start(self):
        cid, tok = read_token_file(self.s.token_file)
        if cid and tok:
            self.log("Token: read from the shared token file.")
        else:
            cid, tok = self.s.client_id.strip(), self.s.access_token.strip()
            if not (cid and tok):
                raise RuntimeError("No Dhan client id / access token — fill them in Settings "
                                   "or check the shared token file path.")
            self.log("Token: using the client id / token from Settings.")
        self.client = DhanClient(cid, tok)

        if self.s.mode == "live":
            try:
                f = self.client.funds()
                bal = f.get("availabelBalance", f.get("availableBalance"))
                self.log(f"LIVE: account reachable, available balance {bal}.")
            except DhanError as e:
                raise RuntimeError(f"Dhan refused the account check — {e}. "
                                   f"{PERMANENT_HINT.get(e.code, '')}")

        scrip = ScripMaster(self.log)
        scrip.load()
        today = now_ist().date()

        self.feed = LtpFeed(cid, tok, self.log)
        self.prices = LtpService(self.feed, self.client, self.log)
        broker = (LiveBroker(self.client, self.prices, self.log) if self.s.mode == "live"
                  else PaperBroker(self.prices, self.log))

        names = self.s.enabled_instruments()
        for name in names:
            res = scrip.resolve(name, self.s.expiry_choice, today)
            self.engines[name] = Engine(name, self.s, res, self.client, self.prices, broker,
                                        self.log, self.halt_all)
        # one REST candle call per instrument per poll, through a 4/s gate
        need = max(self.s.poll_seconds, len(names) * 0.3)
        if need > self.s.poll_seconds:
            self.log(f"Poll interval raised to {need:.1f}s for {len(names)} instruments.")
            self.s.poll_seconds = need

        self.feed.start()
        self.prices.start()
        for e in self.engines.values():
            e.start()
        self.running = True
        self.log(f"Started in {self.s.mode.upper()} mode: {', '.join(names)}.")

    def stop(self):
        for e in self.engines.values():
            e.stop()
        if self.prices:
            self.prices.stop()
        if self.feed:
            self.feed.stop()
        self.running = False
        self.log("Stopped.")

    def halt_all(self, reason: str):
        if self.halt_reason:
            return
        code = reason.split(":")[0]
        hint = PERMANENT_HINT.get(code, "")
        self.halt_reason = f"{reason} {hint}".strip()
        self.log(f"ACCOUNT REFUSAL — {self.halt_reason} Trading halted for this session.")
        for e in self.engines.values():
            e.halt(self.halt_reason)

    def open_positions(self) -> list[str]:
        return [e.trade["contract"]["symbol"] for e in self.engines.values() if e.trade]

    def snapshot(self) -> list[dict]:
        return [e.snapshot() for e in self.engines.values()]

    def feed_connected(self) -> bool:
        return bool(self.feed and self.feed.connected)
