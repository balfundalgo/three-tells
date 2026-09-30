"""
Dhan v2 REST client — candles, quotes, orders, positions, trades.

Rate gates (Dhan limits): data APIs 5/s, quote APIs 1/s, order APIs 10/s.
We stay a little under each.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import requests

from .config import IST

BASE = "https://api.dhan.co/v2"

# account-level refusals: retrying cannot help, so the session halts
PERMANENT_CODES = {"DH-901", "DH-902", "DH-905"}
PERMANENT_HINT = {
    "DH-901": "Invalid or expired access token — regenerate the Dhan token.",
    "DH-902": "Account not subscribed to Data / Trading APIs in DhanHQ.",
    "DH-905": "This machine's public IP is not whitelisted in the DhanHQ API section "
              "(needed for orders only).",
}


class DhanError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    @property
    def permanent(self) -> bool:
        return self.code in PERMANENT_CODES


class RateGate:
    def __init__(self, per_second: float):
        self.min_gap = 1.0 / per_second
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            gap = self.last + self.min_gap - now
            if gap > 0:
                time.sleep(gap)
            self.last = time.monotonic()


class DhanClient:
    def __init__(self, client_id: str, access_token: str):
        self.client_id = client_id
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": access_token,
            "client-id": client_id,
        })
        self.data_gate = RateGate(4.0)
        self.quote_gate = RateGate(0.95)
        self.order_gate = RateGate(8.0)

    # ------------------------------------------------------------------
    def _req(self, method: str, path: str, gate: RateGate, body=None, timeout=8):
        gate.wait()
        try:
            r = self.session.request(method, BASE + path, json=body, timeout=timeout)
        except requests.RequestException as e:
            raise DhanError("NET", str(e)) from e
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {"raw": r.text[:300]}
        if r.status_code >= 400 or (isinstance(data, dict) and data.get("errorCode")):
            code = str((data or {}).get("errorCode") or f"HTTP{r.status_code}")
            msg = str((data or {}).get("errorMessage") or (data or {}).get("raw") or r.reason)
            raise DhanError(code, msg)
        return data

    # ------------------------------------------------------------------ data
    def intraday(self, segment: str, security_id: str, instrument: str,
                 start: datetime, end: datetime, interval: int = 1) -> list[tuple]:
        """1-minute bars as (start_dt_ist, open, high, low, close)."""
        body = {
            "securityId": str(security_id),
            "exchangeSegment": segment,
            "instrument": instrument,
            "interval": str(interval),
            "oi": False,
            "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": end.strftime("%Y-%m-%d %H:%M:%S"),
        }
        d = self._req("POST", "/charts/intraday", self.data_gate, body, timeout=10)
        ts = d.get("timestamp") or []
        o, h, l, c = d.get("open") or [], d.get("high") or [], d.get("low") or [], d.get("close") or []
        out = []
        for i in range(min(len(ts), len(o), len(h), len(l), len(c))):
            t = datetime.fromtimestamp(float(ts[i]), IST).replace(second=0, microsecond=0)
            out.append((t, float(o[i]), float(h[i]), float(l[i]), float(c[i])))
        return out

    def ltp(self, request: dict[str, list[str]]) -> dict[tuple[str, str], float]:
        """request: {segment: [security ids]} -> {(segment, id): last price}"""
        body = {seg: [int(x) for x in ids] for seg, ids in request.items() if ids}
        if not body:
            return {}
        d = self._req("POST", "/marketfeed/ltp", self.quote_gate, body, timeout=5)
        out = {}
        for seg, per in ((d or {}).get("data") or {}).items():
            for sid, q in (per or {}).items():
                try:
                    out[(seg, str(sid))] = float(q.get("last_price"))
                except (TypeError, ValueError, AttributeError):
                    pass
        return out

    # ------------------------------------------------------------------ orders
    def place_market(self, segment: str, security_id: str, side: str, qty: int,
                     tag: str) -> str:
        body = {
            "dhanClientId": self.client_id,
            "correlationId": tag[:25],
            "transactionType": side,          # BUY / SELL
            "exchangeSegment": segment,
            "productType": "INTRADAY",
            "orderType": "MARKET",
            "validity": "DAY",
            "securityId": str(security_id),
            "quantity": int(qty),
            "disclosedQuantity": 0,
            "price": 0,
            "triggerPrice": 0,
            "afterMarketOrder": False,
        }
        d = self._req("POST", "/orders", self.order_gate, body)
        oid = str(d.get("orderId") or "")
        if not oid:
            raise DhanError("NO-ID", f"order not acknowledged: {d}")
        return oid

    def order(self, order_id: str) -> dict:
        d = self._req("GET", f"/orders/{order_id}", self.order_gate)
        if isinstance(d, list):
            d = d[0] if d else {}
        return d or {}

    def trades(self, order_id: str) -> list[dict]:
        d = self._req("GET", f"/trades/{order_id}", self.order_gate)
        if isinstance(d, dict):
            d = [d]
        return d or []

    def positions(self) -> list[dict]:
        d = self._req("GET", "/positions", self.order_gate)
        return d if isinstance(d, list) else []

    def funds(self) -> dict:
        return self._req("GET", "/fundlimit", self.order_gate) or {}
