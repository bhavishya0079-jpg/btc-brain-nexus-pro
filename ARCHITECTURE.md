# Architecture
```
frontend/ (vanilla JS SPA, canvas chart)  ──HTTP/SSE──>  backend/app/main.py (Starlette)
                                                          ├─ services.py   (strategies, backtest, paper, alerts, learning, backups)
                                                          ├─ core/         (indicators, strategy, backtest, analysis, scanner, paper, alerts, learning, options)
                                                          ├─ providers/    (binance, delta, angelone, groww → SafeHttp read-only)
                                                          ├─ ws.py         (stdlib WebSocket client + StreamHub)
                                                          └─ db.py         (SQLite WAL + migrations/)
```
Candle format: `{t: epoch_ms, o,h,l,c,v}`. Options chain: rows `{strike, CE{…}, PE{…}}` + `provider, underlying, expiry, spot`.

Main API groups (all under `/api`, bearer token except `health` and `auth/login`): `providers`, `diagnostics`, `market/*`, `options/*`, `analysis/*`, `chart`, `scan`, `strategies*`, `backtest*`, `paper/*`, `alerts/*`, `learning/*`, `audit`, `backup*`, `stream/tickers`.

AI learning flow: run → proposal (validated / not validated / insufficient data) → candidate version (never auto-applied) → human approve (validated only) → rollback creates a new version from the base. Every step is audited.
