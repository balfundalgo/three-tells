"""
Live prices.

The Dhan v2 websocket (ticker mode) supplies LTP for the underlyings and any
option we hold. If a price goes stale (socket down or quiet contract), the
LtpService refreshes it through the REST LTP endpoint, batching every stale
contract into one call per second.

Candles never come from here — they come from REST (see candles.py).
"""
from __future__ import annotations

import json
import struct
import threading
import time

import websocket  # websocket-client

from .dhan_api import DhanClient, DhanError

WS_URL = "wss://api-feed.dhan.co?version=2&token={tok}&clientId={cid}&authType=2"

SEG_CODE = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ",
            5: "MCX_COMM", 7: "BSE_CURRENCY", 8: "BSE_FNO"}

STALE_SECONDS = 2.0   # websocket price older than this → refreshed over REST (1 batched call/s)


class LtpFeed:
    def __init__(self, client_id: str, token: str, log=print):
        self.url = WS_URL.format(tok=token, cid=client_id)
        self.log = log
        self.prices: dict[tuple[str, str], tuple[float, float]] = {}
        self.subs: set[tuple[str, str]] = set()
        self.lock = threading.Lock()
        self.ws = None
        self.running = False
        self.connected = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True, name="ttt-ws")
        self.thread.start()

    def stop(self):
        self.running = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def subscribe(self, keys):
        new = []
        with self.lock:
            for k in keys:
                if k not in self.subs:
                    self.subs.add(k)
                    new.append(k)
        if new and self.connected:
            self._send_subs(new)

    def get(self, key) -> tuple[float | None, float]:
        with self.lock:
            p = self.prices.get(key)
        return (p[0], p[1]) if p else (None, 0.0)

    # ------------------------------------------------------------------
    def _send_subs(self, keys):
        keys = list(keys)
        for i in range(0, len(keys), 100):
            chunk = keys[i:i + 100]
            msg = {"RequestCode": 15, "InstrumentCount": len(chunk),
                   "InstrumentList": [{"ExchangeSegment": s, "SecurityId": str(x)} for s, x in chunk]}
            try:
                self.ws.send(json.dumps(msg))
            except Exception as e:
                self.log(f"Feed: subscribe failed ({e}).")

    def _on_open(self, ws):
        self.connected = True
        self.log("Feed: websocket connected.")
        with self.lock:
            keys = list(self.subs)
        if keys:
            self._send_subs(keys)

    def _on_message(self, ws, data):
        if not isinstance(data, (bytes, bytearray)) or len(data) < 8:
            return
        code, _length, seg, sid = struct.unpack_from("<BHBI", data, 0)
        if code in (2, 4, 8) and len(data) >= 12:      # ticker / quote / full: LTP at byte 8
            ltp = struct.unpack_from("<f", data, 8)[0]
            key = (SEG_CODE.get(seg, str(seg)), str(sid))
            with self.lock:
                self.prices[key] = (round(float(ltp), 2), time.time())
        elif code == 50 and len(data) >= 10:
            reason = struct.unpack_from("<H", data, 8)[0]
            self.log(f"Feed: server disconnect, reason code {reason}.")

    def _on_close(self, ws, *a):
        self.connected = False

    def _on_error(self, ws, err):
        self.connected = False
        self.log(f"Feed: websocket error ({err}).")

    def _loop(self):
        backoff = 2
        while self.running:
            self.ws = websocket.WebSocketApp(self.url, on_open=self._on_open,
                                             on_message=self._on_message,
                                             on_close=self._on_close, on_error=self._on_error)
            self.ws.run_forever(ping_interval=20, ping_timeout=10)
            self.connected = False
            if not self.running:
                break
            self.log(f"Feed: reconnecting in {backoff}s…")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


class LtpService:
    """Freshest available price per contract: websocket first, REST when stale."""

    def __init__(self, feed: LtpFeed | None, client: DhanClient | None, log=print):
        self.feed = feed
        self.client = client
        self.log = log
        self.rest: dict[tuple[str, str], tuple[float, float]] = {}
        self.wanted: set[tuple[str, str]] = set()
        self.lock = threading.Lock()
        self.running = False
        self._last_err = 0.0

    def watch(self, keys):
        keys = list(keys)
        with self.lock:
            self.wanted.update(keys)
        if self.feed:
            self.feed.subscribe(keys)

    def price(self, key) -> tuple[float | None, float]:
        """(price, age_seconds). age is large if nothing is known."""
        best, ts = None, 0.0
        if self.feed:
            best, ts = self.feed.get(key)
        with self.lock:
            r = self.rest.get(key)
        if r and r[1] > ts:
            best, ts = r
        age = time.time() - ts if ts else 1e9
        return best, age

    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True, name="ttt-ltp").start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            time.sleep(1.0)
            if not self.client:
                continue
            with self.lock:
                wanted = list(self.wanted)
            stale = [k for k in wanted if self.price(k)[1] > STALE_SECONDS]
            if not stale:
                continue
            req: dict[str, list[str]] = {}
            for seg, sid in stale:
                req.setdefault(seg, []).append(sid)
            try:
                got = self.client.ltp(req)
            except DhanError as e:
                if time.time() - self._last_err > 60:
                    self.log(f"Prices: REST fallback failed ({e}).")
                    self._last_err = time.time()
                continue
            now = time.time()
            with self.lock:
                for k, v in got.items():
                    self.rest[k] = (v, now)
