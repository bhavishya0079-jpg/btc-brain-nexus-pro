# Install

Requirements: Python 3.11+ (tested on 3.13), pip.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate     # optional
pip install -r requirements.txt
cp ../.env.example .env                               # edit values
python -m app.main
```
Open http://127.0.0.1:8000 and sign in as `admin` with `NEXUS_ADMIN_PASSWORD` (the admin account is created on first start; min 8 chars).

## Provider keys (optional, read-only)
| Provider | Needs |
|---|---|
| Binance | nothing (public market data) |
| Delta Exchange India | nothing (public market data) |
| Angel One | `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET` |
| Groww | `GROWW_ACCESS_TOKEN` |

Use read-only/market-data keys only. Never put keys in the frontend or in GitHub.

## Verify
```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
Then in the UI open **System & Diagnostics** to see each provider's state/latency.

## Data & backups
SQLite at `backend/data/nexus.db`. Backups in `backend/data/backups` (auto every 24h, keeps 14; manual via System page or `scripts/backup.sh`). Restore takes a safety snapshot first.
