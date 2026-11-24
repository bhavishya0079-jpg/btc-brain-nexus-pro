# BTC BRAIN NEXUS

AI-assisted **market analysis, strategy learning and trading research** platform for crypto (Binance, Delta Exchange India) and Indian stocks (Angel One, Groww).

> **Research and analysis only.** No real order is ever placed. No deposit/withdrawal/fund access. No profit is guaranteed. Signals and backtests describe past data only.

## Features
- Crypto + Indian stock modules behind modular provider adapters (`backend/app/providers/`)
- Options chain: OI, volume, IV, greeks (provider-supplied, or derived via Black-Scholes and labelled `derived_bs`), PCR, max pain
- Charts: 8 timeframes (1m 3m 5m 15m 30m 1h 4h 1d), zoom/pan/pinch, crosshair, user trend lines, entry/SL/target levels, Fibonacci, support/resistance
- Indicators: EMA, SMA, RSI, MACD, Bollinger, ATR, VWAP, Supertrend, ADX, Stochastic
- AI analysis: multi-timeframe LONG / SHORT / WAIT with entry, stop-loss, T1–T3 and reasons
- Scanners (volume, breakout, trend, RSI, MACD, EMA cross, MTF, custom rules, options OI)
- Strategy Builder: JSON rules, version history, restore, sandbox test (never live)
- Backtesting with fees/slippage, strategy comparison
- Paper trading (virtual money only)
- Alerts (price, indicator, signal) with in-app/browser/webhook/Telegram notifications
- AI Learning System: proposes rule changes → tests on unseen data → human approval → audit log → rollback
- Auth, rate limiting, security headers, SQLite WAL + migrations, backup/restore

## Safety design
- All provider HTTP goes through `providers/http.py` (`SafeHttp`): per-provider allow-list of **read-only** endpoints, a hard block on order/withdraw/fund/margin/portfolio/position paths, pinned host, no redirects.
- API keys live only in server env / `.env`. The frontend never sees them. `.env` is git-ignored.
- No synthetic prices: if a provider is down or unconfigured the UI shows the error, never fake data. (Synthetic candles exist **only** in `backend/tests/`.)
- The AI never auto-applies changes. Only `VALIDATED_OUT_OF_SAMPLE` proposals can be approved, and only by a person.
- GitHub Pages hosts the **frontend only**; the backend is deployed separately.

## Quick start
See [docs/INSTALL.md](docs/INSTALL.md). Short version:
```bash
cd backend && pip install -r requirements.txt
export NEXUS_ADMIN_PASSWORD='choose-a-strong-password'
python -m app.main        # http://127.0.0.1:8000
python -m pytest -q       # run tests (pip install -r requirements-dev.txt)
```
Deployment: [docs/DEPLOY.md](docs/DEPLOY.md) · Security: [docs/SECURITY.md](docs/SECURITY.md) · API/Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Testing: [docs/TESTING.md](docs/TESTING.md)

## Known limitations (read this)
- The automated test suite uses fake HTTP sessions. **Live provider responses were not verified in the build environment (no network).** Test each provider with your own keys (System → Providers, `/api/diagnostics`) before relying on it. Groww option-chain/expiry endpoints and some Angel One details are best-effort from public docs.
- "AI" learning is statistics/rule based (train/test split), not an LLM.
- Indian-stock data requires your own Angel One / Groww credentials.
