"""
Order execution.

PaperBroker fills at the option's live price. LiveBroker sends one MARKET
order per entry or exit and confirms it — lessons carried over from the
earlier Dhan builds:

  * one market order only, never a retry ladder (a late-confirming order plus a
    retry is how double fills happen);
  * the fill price comes from the trade book, not the last traded price;
  * if the order status never confirms, the broker position decides;
  * DH-901 / DH-902 / DH-905 are account problems — they halt the session.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .dhan_api import DhanClient, DhanError
from .feed import LtpService
from .instruments import OptionContract


@dataclass
class Fill:
    ok: bool
    price: float = 0.0
    order_id: str = ""
    note: str = ""
    flat: bool = False      # broker holds nothing — position already closed elsewhere
    unknown: bool = False   # order outcome could not be established


def order_quantity(contract: OptionContract, lots: int, mcx: bool, mcx_in_lots: bool) -> int:
    if mcx and mcx_in_lots:
        return lots
    return lots * contract.lot_size


class PaperBroker:
    live = False

    def __init__(self, prices: LtpService, log=print):
        self.prices = prices
        self.log = log

    def _price(self, contract: OptionContract) -> float | None:
        key = (contract.segment, contract.security_id)
        self.prices.watch([key])
        end = time.time() + 8
        while time.time() < end:
            p, age = self.prices.price(key)
            if p is not None and age < 10:
                return p
            time.sleep(0.25)
        return None

    def buy(self, contract, qty, tag) -> Fill:
        p = self._price(contract)
        if p is None:
            return Fill(False, note="no option price available for the paper fill")
        return Fill(True, p, "PAPER")

    def sell(self, contract, qty, tag) -> Fill:
        p = self._price(contract)
        if p is None:
            return Fill(False, note="no option price available for the paper exit")
        return Fill(True, p, "PAPER")

    def net_qty(self, contract) -> int | None:
        return None


class LiveBroker:
    live = True

    def __init__(self, client: DhanClient, prices: LtpService, log=print):
        self.client = client
        self.prices = prices
        self.log = log

    # -------------------------------------------------------------- helpers
    def net_qty(self, contract: OptionContract) -> int | None:
        try:
            for p in self.client.positions():
                if str(p.get("securityId")) == contract.security_id:
                    return int(float(p.get("netQty") or 0))
            return 0
        except DhanError as e:
            if e.permanent:
                raise
            self.log(f"Positions check failed ({e}).")
            return None

    def _fill_price(self, order_id: str) -> float | None:
        try:
            trades = self.client.trades(order_id)
        except DhanError:
            return None
        q = sum(float(t.get("tradedQuantity") or 0) for t in trades)
        if q <= 0:
            return None
        v = sum(float(t.get("tradedPrice") or 0) * float(t.get("tradedQuantity") or 0) for t in trades)
        return round(v / q, 2)

    def _execute(self, contract: OptionContract, qty: int, side: str, tag: str,
                 expected_net_after: int) -> Fill:
        oid = self.client.place_market(contract.segment, contract.security_id, side, qty, tag)
        self.log(f"Order sent: {side} {qty} {contract.symbol} MARKET (id {oid}).")
        status, reason = "", ""
        end = time.time() + 12
        while time.time() < end:
            time.sleep(0.7)
            try:
                o = self.client.order(oid)
            except DhanError as e:
                if e.permanent:
                    raise
                continue
            status = str(o.get("orderStatus") or "").upper()
            reason = str(o.get("omsErrorDescription") or "")
            if status in ("TRADED",):
                break
            if status in ("REJECTED", "CANCELLED", "EXPIRED"):
                return Fill(False, order_id=oid, note=f"order {status}: {reason}".strip())
        if status != "TRADED":
            # never re-send — ask the broker what we actually hold
            net = self.net_qty(contract)
            if net is None or net != expected_net_after:
                return Fill(False, order_id=oid, unknown=True,
                            note=f"order not confirmed (status '{status or 'unknown'}', "
                                 f"broker net qty {net}); NOT re-sent — check the Dhan app")
            self.log(f"Order {oid} not confirmed by status, but the broker position "
                     f"shows it filled — adopting it.")
        price = None
        for _ in range(6):
            price = self._fill_price(oid)
            if price is not None:
                break
            time.sleep(0.5)
        if price is None:
            p, _ = self.prices.price((contract.segment, contract.security_id))
            price = p or 0.0
            self.log(f"WARNING: trade book silent for order {oid}; recorded {price:.2f} "
                     f"from the last price — reconcile against the contract note.")
        return Fill(True, price, oid)

    # -------------------------------------------------------------- api
    def buy(self, contract, qty, tag) -> Fill:
        before = self.net_qty(contract) or 0
        return self._execute(contract, qty, "BUY", tag, before + qty)

    def sell(self, contract, qty, tag) -> Fill:
        before = self.net_qty(contract)
        if before is not None and before <= 0:
            return Fill(False, note="broker shows no open quantity to sell", flat=True)
        return self._execute(contract, qty, "SELL", tag, (before or qty) - qty)
