# Balfund · The Three Tells

Intraday option-buying app for **Strategy Note 05 v1.1 — The Three Tells**
(Inside Bar · Hammer / Inverted Hammer · Failed Break) on Dhan.

Instruments: NIFTY, BANKNIFTY, SENSEX, CRUDEOIL, CRUDEOILM — any combination,
each running as its own engine with its own lots, buffers and trade limit.

## How it works

| Piece | Source |
|---|---|
| Pattern candles | Dhan REST 1-minute bars, polled every 4 s, rolled up to 1/3/5/15 min from the session start (09:15 NSE/BSE, 09:00 MCX). The forming minute is never used. |
| Triggers, stops, targets, square-off | Live LTP from the Dhan websocket; REST LTP takes over if a price goes stale. |
| Underlying | Index spot for NIFTY / BANKNIFTY / SENSEX; for crude, the futures contract the chosen option month is written on. |
| Orders | One MARKET order per entry/exit, confirmed by order status, fill price from the trade book. Never re-sent if unconfirmed — the broker position decides. |

Patterns are read only from candles that close **after** START. Earlier
candles (including previous sessions) only feed the hammer's 10-candle
location lookback. Trades taken today and any open trade survive a restart.

## Files written next to the EXE

- `ttt_settings.json` — settings (always reopens in PAPER)
- `ttt_activity_YYYYMMDD.log` — everything in the log panel
- `ttt_trades_YYYYMMDD.csv` — one row per closed trade
- `ttt_state_<NAME>.json` — today's trade count and open trade
- `ttt_scrip_YYYYMMDD.csv` — cached Dhan scrip master

## Token

Read from `C:\balfund_shared\dhan_token.json` (the Balfund token generator)
if present; otherwise the client id / access token typed in Settings.

## Before the first LIVE session

1. Whitelist the machine's public IP in the DhanHQ API section — without it
   every order is refused with DH-905 (data and websocket still work).
2. **MCX quantity:** the app sends crude quantity in **lots** by default
   (`MCX order quantity is in lots`). Confirm with one CRUDEOILM lot; if Dhan
   rejects it, untick that box so quantity is sent as lots × lot size.

## Develop

```bash
pip install -r requirements.txt
python -m pytest -q tests
python main.py
```

The EXE is built by GitHub Actions on every push to `main`
(artifact `Balfund_TheThreeTells`) and attached to a release on `v*` tags.
