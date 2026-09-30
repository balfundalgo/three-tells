"""
Offline checks against the worked examples in Strategy Note 05 and the
engine's trade lifecycle. Run:  python -m pytest -q   (or python tests/test_core.py)
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tt.config import IST, Settings
from tt.candles import Candle, rollup
from tt import patterns as P

D = datetime(2026, 10, 1, tzinfo=IST)


def C(hm, o, h, l, c, tf=5, day=D):
    hh, mm = map(int, hm.split(":"))
    s = day.replace(hour=hh, minute=mm)
    return Candle(s, s + timedelta(minutes=tf), o, h, l, c)


R = P.Rules(entry_buffer=3, stop_buffer=3)


# ------------------------------------------------------------------ Section 03
def test_inside_bar_note_example():
    m = C("09:20", 24826, 24860, 24800, 24852)
    i = C("09:25", 24840, 24845, 24815, 24822)
    s = P.scan([m, i], D.date(), R)
    assert s.pattern == "A"
    assert (s.ce_trigger, s.ce_stop, s.pe_trigger, s.pe_stop) == (24848, 24797, 24812, 24863)


def test_inside_bar_equal_range_is_not_inside():
    m = C("09:20", 100, 110, 90, 105)
    assert not P.is_inside(m, C("09:25", 95, 110, 90, 100))


def test_inside_bar_mother_reference():
    r = P.Rules(entry_buffer=3, stop_buffer=3, ib_reference="mother")
    s = P.scan([C("09:20", 24826, 24860, 24800, 24852), C("09:25", 24840, 24845, 24815, 24822)],
               D.date(), r)
    assert (s.ce_trigger, s.pe_trigger) == (24863, 24797)


def test_consecutive_inside_keeps_mother():
    m = C("09:20", 24826, 24860, 24800, 24852)
    i1 = C("09:25", 24840, 24845, 24815, 24822)
    first = P.scan([m, i1], D.date(), R)
    i2 = C("09:30", 24835, 24850, 24805, 24830)   # inside the mother, not inside i1
    s = P.scan([m, i1, i2], D.date(), R, current=first)
    assert s.mother == m and s.candle == i2
    assert s.ce_stop == 24797 and s.ce_trigger == 24853


# ------------------------------------------------------------------ Section 04
def test_hammer_note_example():
    hist = [C("09:15", 24790, 24795, 24760, 24765), C("09:20", 24766, 24770, 24735, 24740),
            C("09:25", 24741, 24745, 24708, 24713), C("09:30", 24712, 24724, 24680, 24720)]
    s = P.scan(hist, D.date(), P.Rules(entry_buffer=3, stop_buffer=3, pattern_c=False))
    assert s.name == "Hammer" and (s.ce_trigger, s.ce_stop) == (24727, 24677) and s.pe_trigger is None


def test_inverted_hammer_note_example():
    hist = [C("09:15", 24880, 24905, 24876, 24902), C("09:20", 24903, 24930, 24899, 24926),
            C("09:25", 24927, 24952, 24922, 24948), C("09:30", 24955, 24990, 24945, 24948)]
    s = P.scan(hist, D.date(), P.Rules(entry_buffer=3, stop_buffer=3, pattern_c=False))
    assert s.name == "Inverted Hammer" and (s.pe_trigger, s.pe_stop) == (24942, 24993)


def test_hammer_not_at_bottom_rejected():
    hist = [C("09:15", 100, 105, 60, 70), C("09:20", 90, 92, 70, 91)]  # earlier low 60
    assert P.hammer_setup(hist[-1], hist, R) is None


def test_hammer_long_upper_wick_rejected():
    cur = C("09:20", 100, 120, 60, 102)   # lower wick 40, upper 18 > 25% of 60 (15)
    assert P.hammer_setup(cur, [cur], R) is None


def test_hammer_ratio_rule():
    cur = C("09:20", 100, 101, 80, 99)    # body 1, lower wick 19 -> ok
    assert P.hammer_setup(cur, [cur], R).name == "Hammer"
    cur = C("09:20", 100, 101, 88, 95)    # body 5, lower wick 7 -> < 3x
    assert P.hammer_setup(cur, [cur], R) is None


# ------------------------------------------------------------------ Section 05
def test_failed_break_bearish_note_example():
    g = C("09:20", 24800, 24845, 24795, 24840)
    r = C("09:25", 24842, 24858, 24814, 24820)
    s = P.scan([g, r], D.date(), R)
    assert s.pattern == "C" and (s.pe_trigger, s.pe_stop) == (24811, 24861)


def test_failed_break_bullish_note_example():
    r = C("09:20", 24700, 24705, 24655, 24660)
    g = C("09:25", 24658, 24690, 24642, 24685)
    s = P.scan([r, g], D.date(), R)
    assert s.pattern == "C" and (s.ce_trigger, s.ce_stop) == (24693, 24639)


def test_note_hammer_example_is_also_a_failed_break():
    # The note's hammer candle also dips under the red candle's low and closes back
    # above it, so priority C wins — with identical levels.
    hist = [C("09:15", 24790, 24795, 24760, 24765), C("09:20", 24766, 24770, 24735, 24740),
            C("09:25", 24741, 24745, 24708, 24713), C("09:30", 24712, 24724, 24680, 24720)]
    s = P.scan(hist, D.date(), R)
    assert s.pattern == "C" and (s.ce_trigger, s.ce_stop) == (24727, 24677)


def test_priority_c_over_a():
    # a red candle that is also inside? can't be both; build C and B on one candle
    g = C("09:20", 100, 110, 95, 108)
    r = C("09:25", 109, 140, 105, 106)  # red, above green high, closes below it, long upper wick
    s = P.scan([C("09:15", 90, 100, 85, 99), g, r], D.date(), R)
    assert s.pattern == "C"


def test_pattern_candles_must_be_today():
    y = D - timedelta(days=1)
    m = C("15:25", 24826, 24860, 24800, 24852, day=y)
    i = C("09:15", 24840, 24845, 24815, 24822)
    assert P.scan([m, i], D.date(), P.Rules(pattern_b=False)) is None


def test_toggles():
    m = C("09:20", 24826, 24860, 24800, 24852)
    i = C("09:25", 24840, 24845, 24815, 24822)
    assert P.scan([m, i], D.date(), P.Rules(pattern_a=False)) is None


# ------------------------------------------------------------------ candles
def test_rollup_anchor_and_forming():
    bars = []
    t = D.replace(hour=9, minute=15)
    for k in range(12):
        bars.append((t + timedelta(minutes=k), 100 + k, 101 + k, 99 + k, 100.5 + k))
    now = D.replace(hour=9, minute=26, second=10)   # 09:26 bar is forming
    out = rollup(bars, 5, "09:15", now)
    assert [c.start.strftime("%H:%M") for c in out] == ["09:15", "09:20"]
    assert out[0].o == 100 and out[0].c == 104.5 and out[0].h == 105 and out[0].l == 99


def test_rollup_waits_for_last_minute_then_grace():
    t = D.replace(hour=9, minute=15)
    bars = [(t + timedelta(minutes=k), 1, 2, 0, 1) for k in range(4)]   # 09:19 missing
    assert rollup(bars, 5, "09:15", D.replace(hour=9, minute=20, second=5)) == []
    assert len(rollup(bars, 5, "09:15", D.replace(hour=9, minute=20, second=30))) == 1


def test_rollup_mcx_anchor():
    t = D.replace(hour=9, minute=0)
    bars = [(t + timedelta(minutes=k), 1, 2, 0, 1) for k in range(15)]
    out = rollup(bars, 15, "09:00", D.replace(hour=9, minute=16))
    assert len(out) == 1 and out[0].start.strftime("%H:%M") == "09:00"


# ------------------------------------------------------------------ engine
class FakePrices:
    def __init__(self):
        self.p = {}
    def watch(self, keys):
        pass
    def price(self, key):
        return (self.p.get(key), 0.0) if key in self.p else (None, 1e9)


class FakeBroker:
    live = False
    def __init__(self, prices):
        self.prices = prices
        self.orders = []
    def buy(self, c, q, tag):
        from tt.broker import Fill
        self.orders.append(("BUY", c.symbol, q))
        return Fill(True, 100.0, "P")
    def sell(self, c, q, tag):
        from tt.broker import Fill
        self.orders.append(("SELL", c.symbol, q))
        return Fill(True, 130.0, "P")
    def net_qty(self, c):
        return None


def make_engine(tmp, **kw):
    import tt.config as cfg
    cfg.app_dir = lambda: tmp
    import tt.storage as st
    st.app_dir = lambda: tmp
    from tt.engine import Engine
    from tt.instruments import Resolved, OptionContract
    s = Settings()
    for k, v in kw.items():
        setattr(s, k, v)
    chain = {}
    for k in range(24500, 25300, 50):
        for t in ("CE", "PE"):
            chain[(float(k), t)] = OptionContract(f"{k}{t}", f"NIFTY {k} {t}", float(k), t,
                                                  date(2026, 10, 6), 75, "NSE_FNO")
    res = Resolved("NIFTY", "IDX_I", "13", "INDEX", "NIFTY spot", "NSE_FNO",
                   date(2026, 10, 6), 50.0, chain)
    prices = FakePrices()
    clock = {"t": D.replace(hour=9, minute=30)}
    logs = []
    e = Engine("NIFTY", s, res, None, prices, FakeBroker(prices), logs.append,
               lambda r: None, clock=lambda: clock["t"])
    e.state = "SCANNING"
    return e, prices, clock, logs


def test_engine_inside_bar_ce_trade_to_target():
    with tempfile.TemporaryDirectory() as tmp:
        e, prices, clock, logs = make_engine(tmp)
        e.on_candle(C("09:20", 24826, 24860, 24800, 24852))
        e.on_candle(C("09:25", 24840, 24845, 24815, 24822))
        assert e.state == "ARMED"
        e.on_price(24830, clock["t"])
        assert e.state == "ARMED"
        e.on_price(24848.5, clock["t"])                # touch CE trigger
        assert e.state == "IN TRADE" and e.trade["side"] == "CE"
        assert e.trade["contract"]["strike"] == 24850.0      # ATM
        risk = 24848.5 - 24797
        assert abs(e.trade["target"] - (24848.5 + 1.5 * risk)) < 0.01
        e.on_price(24900, clock["t"])
        assert e.trade is not None
        e.on_price(e.trade["target"] + 0.1, clock["t"])
        assert e.trade is None and e.state == "SCANNING" and e.trades_today == 1
        assert any("TARGET" in l for l in logs)
        assert os.path.exists(os.path.join(tmp, f"ttt_trades_{datetime.now(IST):%Y%m%d}.csv"))


def test_engine_patterns_ignored_during_trade_and_stop():
    with tempfile.TemporaryDirectory() as tmp:
        e, prices, clock, logs = make_engine(tmp)
        e.on_candle(C("09:20", 24800, 24845, 24795, 24840))
        e.on_candle(C("09:25", 24842, 24858, 24814, 24820))    # bearish failed break
        e.on_price(24811, clock["t"])
        assert e.trade["side"] == "PE"
        e.on_candle(C("09:30", 24815, 24830, 24812, 24825))    # would be inside — ignored
        assert e.setup is None
        e.on_price(24861, clock["t"])                           # stop
        assert e.trade is None and e.state == "SCANNING"


def test_engine_itm_otm_strikes():
    with tempfile.TemporaryDirectory() as tmp:
        e, *_ = make_engine(tmp, strike_type="ITM", strike_depth=2)
        assert e.r.pick(24848, "CE", "ITM", 2).strike == 24750
        assert e.r.pick(24848, "PE", "ITM", 2).strike == 24950
        assert e.r.pick(24848, "CE", "OTM", 1).strike == 24900
        assert e.r.pick(24848, "PE", "OTM", 1).strike == 24800


def test_engine_setup_expiry_and_stop_before_trigger():
    with tempfile.TemporaryDirectory() as tmp:
        e, prices, clock, logs = make_engine(tmp)
        hist = [C("09:15", 24790, 24795, 24760, 24765), C("09:20", 24766, 24770, 24735, 24740),
                C("09:25", 24741, 24745, 24708, 24713), C("09:30", 24712, 24724, 24680, 24720)]
        for c in hist:
            e.on_candle(c)
        assert e.state == "ARMED" and e.setup.ce_stop == 24677
        e.on_price(24676, clock["t"])                   # below stop first → cancelled
        assert e.setup is None and e.state == "SCANNING"
        for c in hist:
            e.on_candle(c)
        assert e.state == "ARMED"
        e.on_candle(C("09:35", 24721, 24740, 24712, 24738))   # no pattern
        e.on_candle(C("09:40", 24738, 24752, 24730, 24750))
        assert e.state == "ARMED"
        e.on_candle(C("09:45", 24750, 24765, 24744, 24762))
        assert e.state == "SCANNING" and any("expired" in l for l in logs)


def test_engine_last_entry_and_trade_limit():
    with tempfile.TemporaryDirectory() as tmp:
        e, prices, clock, logs = make_engine(tmp)
        e.inst.max_trades = 1
        e.on_candle(C("09:20", 24800, 24845, 24795, 24840))
        e.on_candle(C("09:25", 24842, 24858, 24814, 24820))
        e.on_price(24811, clock["t"])
        e.on_price(24700, clock["t"])     # target
        assert e.state == "DONE"
        e2, _, clock2, _ = make_engine(tmp)
        clock2["t"] = D.replace(hour=15, minute=0, second=1)
        e2.on_price(24800, clock2["t"])
        assert e2.state == "DONE"


def test_engine_square_off():
    with tempfile.TemporaryDirectory() as tmp:
        e, prices, clock, logs = make_engine(tmp)
        e.on_candle(C("09:20", 24800, 24845, 24795, 24840))
        e.on_candle(C("09:25", 24842, 24858, 24814, 24820))
        e.on_price(24811, clock["t"])
        clock["t"] = D.replace(hour=15, minute=15)
        e.on_price(24815, clock["t"])
        assert e.trade is None and any("SQUARE-OFF" in l for l in logs)


def test_engine_max_risk_drops_side():
    with tempfile.TemporaryDirectory() as tmp:
        e, prices, clock, logs = make_engine(tmp)
        e.inst.max_risk = 40
        e.on_candle(C("09:20", 24800, 24845, 24795, 24840))
        e.on_candle(C("09:25", 24842, 24858, 24814, 24820))    # risk 50 > 40
        assert e.setup is None and e.state == "SCANNING"


def test_engine_restart_restores_trade_count():
    with tempfile.TemporaryDirectory() as tmp:
        e, prices, clock, logs = make_engine(tmp)
        e.on_candle(C("09:20", 24800, 24845, 24795, 24840))
        e.on_candle(C("09:25", 24842, 24858, 24814, 24820))
        e.on_price(24811, clock["t"])
        e2, *_ = make_engine(tmp)
        e2._restore()
        assert e2.trades_today == 1 and e2.trade is not None


def test_settings_validate_and_roundtrip():
    s = Settings()
    assert s.validate() == []
    s.instruments["CRUDEOIL"].enabled = True
    d = s.to_dict()
    s2 = Settings(**d)
    assert s2.instruments["CRUDEOIL"].enabled and s2.mode == "paper"
    assert s2.session_times("CRUDEOILM") == ("09:00", "23:00", "23:15")


def _scrip_rows():
    rows = []
    def add(exch, inst, sym, sid, exp, strike="", typ="", lot="1"):
        rows.append({"SEM_EXM_EXCH_ID": exch, "SEM_INSTRUMENT_NAME": inst, "SEM_TRADING_SYMBOL": sym,
                     "SEM_SMST_SECURITY_ID": sid, "SEM_EXPIRY_DATE": exp, "SEM_STRIKE_PRICE": strike,
                     "SEM_OPTION_TYPE": typ, "SEM_LOT_UNITS": lot, "SEM_CUSTOM_SYMBOL": sym})
    n = 1
    for exp in ("2026-10-01 14:30:00", "2026-10-06 14:30:00", "2026-10-13 14:30:00"):
        for k in range(24500, 25000, 50):
            for t in ("CE", "PE"):
                add("NSE", "OPTIDX", f"NIFTY-{exp[:10]}-{k}-{t}", str(n), exp, str(k), t, "75"); n += 1
    for root in ("CRUDEOIL", "CRUDEOILM"):
        for exp in ("2026-10-01 23:30:00", "2026-10-15 23:30:00", "2026-11-16 23:30:00"):
            for k in range(5400, 5900, 50):
                for t in ("CE", "PE"):
                    add("MCX", "OPTFUT", f"{root}-{exp[:10]}-{k}-{t}", str(n), exp, str(k), t, "100"); n += 1
        add("MCX", "FUTCOM", f"{root}-19Oct2026-FUT", f"F{root}10", "2026-10-19 23:30:00")
        add("MCX", "FUTCOM", f"{root}-19Nov2026-FUT", f"F{root}11", "2026-11-19 23:30:00")
    return rows


def test_resolve_expiry_rules_and_crude_futures():
    from tt.instruments import ScripMaster
    sm = ScripMaster(log=lambda m: None)
    sm.rows = _scrip_rows()
    today = date(2026, 10, 1)
    n = sm.resolve("NIFTY", "current", today)
    assert n.expiry == date(2026, 10, 1) and n.step == 50 and n.und_segment == "IDX_I"   # index: expiry day still used
    assert sm.resolve("NIFTY", "next", today).expiry == date(2026, 10, 6)
    c = sm.resolve("CRUDEOIL", "current", today)
    assert c.expiry == date(2026, 10, 15)                    # crude switches on its expiry day
    assert c.und_id == "FCRUDEOIL10" and c.und_instrument == "FUTCOM" and c.step == 50
    m = sm.resolve("CRUDEOILM", "next", today)
    assert m.expiry == date(2026, 11, 16) and m.und_id == "FCRUDEOILM11"
    assert c.pick(5642, "CE", "ATM", 1).strike == 5650
    assert c.chain[(5650.0, "CE")].lot_size == 100


def test_mcx_quantity_modes():
    from tt.broker import order_quantity
    from tt.instruments import OptionContract
    oc = OptionContract("1", "X", 5650, "CE", date(2026, 10, 15), 100, "MCX_COMM")
    assert order_quantity(oc, 2, True, True) == 2
    assert order_quantity(oc, 2, True, False) == 200
    assert order_quantity(oc, 2, False, True) == 200


def test_every_settings_control_writes_a_real_field():
    import ast
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tt", "gui.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fields = inst = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == "FIELDS":
            fields = ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and node.targets[0].id == "INST_FIELDS":
            inst = ast.literal_eval(node.value)
    s = Settings()
    from tt.config import INSTRUMENTS
    paths = [f[2] for f in fields] + [f"instruments.{n}.{k}" for n in INSTRUMENTS for _, k, _ in inst]
    for p in paths:
        obj = s
        parts = p.split(".")
        for part in parts[:-1]:
            obj = obj[part] if isinstance(obj, dict) else getattr(obj, part)
        assert hasattr(obj, parts[-1]), p
    # and every Section 07 setting has a control
    from dataclasses import fields as dfields
    covered = {f[2] for f in fields}
    for f in dfields(Settings):
        if f.name not in ("mode", "instruments"):
            assert f.name in covered, f"no control for {f.name}"


if __name__ == "__main__":
    fails = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            try:
                f()
                print("ok  ", k)
            except Exception:
                fails += 1
                import traceback; traceback.print_exc()
                print("FAIL", k)
    print("failures:", fails)
    sys.exit(1 if fails else 0)
