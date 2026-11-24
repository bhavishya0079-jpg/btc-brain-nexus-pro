"""Provider connection manager: registry, status, candle fetch with cache fallback, sector analysis."""
import time

from .. import marketdata
from . import build_providers
from .base import NotConfigured
from .http import ProviderError

SECTORS = {  # static reference map of large caps (not live data; verify against index factsheets)
    "RELIANCE": "Energy", "ONGC": "Energy", "NTPC": "Power", "POWERGRID": "Power", "COALINDIA": "Mining",
    "TCS": "IT", "INFY": "IT", "HCLTECH": "IT", "WIPRO": "IT", "TECHM": "IT",
    "HDFCBANK": "Banking", "ICICIBANK": "Banking", "SBIN": "Banking", "AXISBANK": "Banking", "KOTAKBANK": "Banking",
    "BAJFINANCE": "Financials", "BAJAJFINSV": "Financials", "HINDUNILVR": "FMCG", "ITC": "FMCG",
    "NESTLEIND": "FMCG", "BRITANNIA": "FMCG", "MARUTI": "Auto", "M&M": "Auto", "TATAMOTORS": "Auto",
    "BAJAJ-AUTO": "Auto", "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma",
    "LT": "Infrastructure", "ULTRACEMCO": "Cement", "TATASTEEL": "Metals", "JSWSTEEL": "Metals",
    "HINDALCO": "Metals", "BHARTIARTL": "Telecom", "ASIANPAINT": "Consumer", "TITAN": "Consumer",
}


class ProviderManager:
    def __init__(self, db, providers=None):
        self.db = db
        self.providers = providers if providers is not None else build_providers()

    def get(self, name):
        p = self.providers.get(name)
        if not p:
            raise ProviderError(f"unknown provider '{name}'", code="bad_request", status=400)
        return p

    def status_all(self):
        return [p.status() for p in self.providers.values()]

    def candles(self, provider, symbol, tf, limit=500, start_ms=None, end_ms=None, store=True):
        cs = self.get(provider).candles(symbol, tf, limit=limit, start_ms=start_ms, end_ms=end_ms)
        if store and cs:
            try:
                marketdata.store_candles(self.db, provider, symbol, tf, cs)
            except Exception:       # caching must never break the data path
                pass
        return cs

    def closed_candles(self, provider, symbol, tf, limit=500):
        """Candles without the still-forming last bar (used for signals/alerts)."""
        cs = self.candles(provider, symbol, tf, limit=limit + 1)
        return marketdata.drop_unclosed(cs, tf, int(time.time() * 1000))[-limit:]

    def history(self, provider, symbol, tf, limit, start_ms=None, end_ms=None):
        """Backtest history: live provider first; falls back to locally cached candles if the provider fails."""
        try:
            cs = self.candles(provider, symbol, tf, limit=limit, start_ms=start_ms, end_ms=end_ms)
            if cs:
                return cs, "live"
        except (ProviderError, NotConfigured) as exc:
            cached = marketdata.load_candles(self.db, provider, symbol, tf, start_ms, end_ms, limit)
            if cached:
                return cached, "cache"
            raise exc
        return marketdata.load_candles(self.db, provider, symbol, tf, start_ms, end_ms, limit), "cache"

    def sector_analysis(self, provider):
        p = self.get(provider)
        ticks = p.tickers([f"NSE:{n}" for n in SECTORS])
        sec = {}
        for t in ticks:
            name = t["symbol"].split(":")[1]
            if SECTORS.get(name) and t.get("change_pct") is not None:
                sec.setdefault(SECTORS[name], []).append((name, t["change_pct"]))
        out = [{"sector": s, "avg_change_pct": sum(x for _, x in it) / len(it), "count": len(it),
                "leaders": sorted(it, key=lambda z: -z[1])[:2], "laggards": sorted(it, key=lambda z: z[1])[:2]}
               for s, it in sec.items()]
        return sorted(out, key=lambda z: -z["avg_change_pct"])
