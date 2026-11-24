# Testing
`cd backend && python -m pytest -q` — unit tests (indicators, options, backtest, paper, security, DB), provider tests with fake HTTP sessions, service tests, and end-to-end HTTP tests against a real uvicorn thread.

Manual checklist (needs your network/keys):
- [ ] System → each provider turns green (Binance, Delta); Angel/Groww with your credentials
- [ ] Charts load all 8 timeframes; closed candles only
- [ ] Options chain for BTC (Delta) and NIFTY (Angel/Groww); compare IV/greeks with the broker's own screen
- [ ] AI signal on a few symbols; levels sane vs chart
- [ ] Backtest, sandbox, paper trade open/close
- [ ] Alert fires and notification arrives
- [ ] Learning run → proposal → approve → rollback; audit log shows each step
- [ ] Backup + restore on a copy
Automated tests cannot confirm live provider payloads; this checklist does.
