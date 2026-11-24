"""Groww Trade API - read-only market data (quote, historical candles, option chain).

Uses the daily access token from GROWW_ACCESS_TOKEN (server environment only).
Only market-data endpoints are allow-listed; no order, portfolio or margin endpoints exist here.
"""
import time
from datetime import datetime

from ..config import settings
from .base import IST, INTERVAL_SECONDS, NotConfigured, Provider, ProviderError, candle, fnum, resample, std_ticker

BASE = "https://api.groww.in"
ALLOWED = [
    ("GET", r"/v1/live-data/quote"),
    ("GET", r"/v1/historical/candles"),
    ("GET", r"/v1/historical/expiries"),
    ("GET", r"/v1/option-chain/exchange/(NSE|BSE)/underlying/[A-Za-z0-9&_\-]+"),
]
TF_MAP = {"1m": "1minute", "3m": "3minute", "5m": "5minute", "15m": "15minute", "30m": "30minute",
          "1h": "1hour", "1d": "1day"}
MAX_DAYS = {"1m": 7, "3m": 14, "5m": 14, "15m": 28, "30m": 55, "1h": 140, "1d": 900}
WATCHLIST = [("NSE", "NIFTY", "index"), ("NSE", "BANKNIFTY", "index"), ("NSE", "FINNIFTY", "index"),
             ("BSE", "SENSEX", "index"), ("NSE", "RELIANCE", "equity"), ("NSE", "TCS", "equity"),
             ("NSE", "HDFCBANK", "equity"), ("NSE", "INFY", "equity"), ("NSE", "ICICIBANK", "equity"),
             ("NSE", "SBIN", "equity"), ("NSE", "ITC", "equity"), ("NSE", "LT", "equity")]


def split_symbol(symbol):
    ex, _, name = symbol.partition(":")
    if not name:
        raise ProviderError("groww: use symbols like NSE:RELIANCE or NSE:NIFTY", code="bad_request")
    return ex.upper(), name.upper()


class GrowwProvider(Provider):
    name, label, market = "groww", "Groww", "stock"
    requires_auth = True
    capabilities = ("candles", "tickers", "options")
    rps = 5.0

    def __init__(self, session=None, sleep=time.sleep, settings_=None):
        cfg = settings_ or settings
        super().__init__(BASE, ALLOWED, session=session, sleep=sleep, settings=cfg)

    def configured(self):
        return bool(self.settings.groww_access_token)

    def _get(self, path, params=None):
        if not self.configured():
            raise NotConfigured("groww: set GROWW_ACCESS_TOKEN")
        d = self.http.request("GET", path, params=params, headers={
            "Accept": "application/json", "Authorization": f"Bearer {self.settings.groww_access_token}",
            "X-API-VERSION": "1.0"})
        if not isinstance(d, dict) or d.get("status") not in ("SUCCESS", None):
            raise ProviderError(f"groww: {str((d or {}).get('error') or d)[:200]}", code="api_error")
        return d.get("payload", d)

    def symbols(self, query=None, limit=200):
        q = (query or "").upper()
        out = [{"symbol": f"{ex}:{n}", "exchange": ex, "type": t, "name": n} for ex, n, t in WATCHLIST
               if not q or q in n]
        if q and not any(o["name"] == q for o in out):      # any NSE cash symbol may be typed directly
            out.append({"symbol": f"NSE:{q}", "exchange": "NSE", "type": "equity", "name": q})
        return out[:limit]

    def tickers(self, symbols=None):
        out = []
        for s in symbols or [f"{ex}:{n}" for ex, n, _ in WATCHLIST]:
            ex, name = split_symbol(s)
            p = self._get("/v1/live-data/quote", {"exchange": ex, "segment": "CASH", "trading_symbol": name})
            ltp = fnum(p.get("last_price"))
            if ltp is None:
                continue
            ohlc = p.get("ohlc") or {}
            out.append(std_ticker(self.name, s, ltp, fnum(ohlc.get("open")), fnum(ohlc.get("high")),
                                  fnum(ohlc.get("low")), fnum(p.get("volume")), int(time.time()),
                                  change=fnum(p.get("day_change")), change_pct=fnum(p.get("day_change_perc")),
                                  extra={"prev_close": fnum(ohlc.get("close"))}))
        return out

    def candles(self, symbol, tf, limit=500, start_ms=None, end_ms=None):
        src = "1h" if tf == "4h" else tf
        if src not in TF_MAP:
            raise ProviderError(f"groww: unsupported timeframe {tf}", code="bad_request")
        ex, name = split_symbol(symbol)
        need = limit * (4 if tf == "4h" else 1)
        end = int((end_ms or time.time() * 1000) / 1000)
        start = int(start_ms / 1000) if start_ms else int(end - need * INTERVAL_SECONDS[src] * 2.2)
        rows, cur_end, span = [], end, MAX_DAYS[src] * 86400
        for _ in range(40):
            cur_start = max(start, cur_end - span)
            p = self._get("/v1/historical/candles", {
                "exchange": ex, "segment": "CASH", "groww_symbol": f"{ex}-{name}",
                "start_time": datetime.fromtimestamp(cur_start, IST).strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": datetime.fromtimestamp(cur_end, IST).strftime("%Y-%m-%d %H:%M:%S"),
                "candle_interval": TF_MAP[src]})
            rows = (p.get("candles") or []) + rows
            if cur_start <= start or len(rows) >= need:
                break
            cur_end = cur_start - 1
        cs = []
        for r in rows:
            t = r[0]
            if isinstance(t, str):
                try:
                    dt = datetime.fromisoformat(t)
                    t = (dt if dt.tzinfo else dt.replace(tzinfo=IST)).timestamp()
                except ValueError:
                    continue
            cs.append(candle(int(t) * 1000, r[1], r[2], r[3], r[4], r[5] if len(r) > 5 and r[5] is not None else 0))
        cs = self.clean(cs)
        return (resample(cs, "1h", "4h") if tf == "4h" else cs)[-limit:]

    def expiries(self, underlying):
        p = self._get("/v1/historical/expiries", {"exchange": "NSE", "underlying_symbol": underlying.upper()})
        today = datetime.now(IST).date().isoformat()
        return sorted(e for e in (p.get("expiries") or []) if e >= today)

    def option_chain(self, underlying, expiry=None):
        underlying = underlying.upper()
        if not expiry:
            exps = self.expiries(underlying)
            if not exps:
                raise ProviderError("groww: no upcoming expiries", code="no_data")
            expiry = exps[0]
        p = self._get(f"/v1/option-chain/exchange/NSE/underlying/{underlying}", {"expiry_date": expiry})
        rows = []
        for k, v in (p.get("strikes") or {}).items():
            row = {"strike": float(k)}
            for kind in ("CE", "PE"):
                x = v.get(kind)
                if not x:
                    continue
                g = x.get("greeks") or {}
                row[kind] = {"symbol": x.get("trading_symbol"), "ltp": fnum(x.get("ltp")), "bid": None, "ask": None,
                             "oi": fnum(x.get("open_interest")), "oi_change": fnum(x.get("oi_change")),
                             "volume": fnum(x.get("volume")), "iv": fnum(g.get("iv")),
                             **{kk: fnum(g.get(kk)) for kk in ("delta", "gamma", "theta", "vega")}}
            rows.append(row)
        return {"provider": self.name, "underlying": underlying, "expiry": expiry,
                "spot": fnum(p.get("underlying_ltp")), "rows": sorted(rows, key=lambda r: r["strike"])}
