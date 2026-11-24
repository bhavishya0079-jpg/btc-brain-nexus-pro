"""Provider framework. Providers are READ-ONLY market-data adapters.

All HTTP goes through SafeHttp (providers/http.py): explicit allow-list of market-data endpoints,
order/fund/position paths blocked, pinned host, no redirects, rate limiting and retries.
Canonical candle: {"t": epoch ms, "o", "h", "l", "c", "v"}.
"""
import math
import time
from datetime import timedelta, timezone

from .http import NotSupported, ProviderError, SafeHttp

IST = timezone(timedelta(hours=5, minutes=30))
INTERVAL_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


def validate_candles(candles):
    """Drop malformed candles (NaN, non-positive prices, inconsistent OHLC); sort and de-duplicate by t."""
    seen, dropped = {}, 0
    for c in candles:
        try:
            t, o, h, l, cl, v = int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), float(c["v"])
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
        if any(not math.isfinite(x) for x in (o, h, l, cl, v)) or min(o, h, l, cl) <= 0 or v < 0:
            dropped += 1
            continue
        if h < max(o, cl) - 1e-9 or l > min(o, cl) + 1e-9 or h < l:
            dropped += 1
            continue
        seen[t] = {"t": t, "o": o, "h": h, "l": l, "c": cl, "v": v}
    return [seen[k] for k in sorted(seen)], dropped


class DataError(ValueError):
    pass


def resample(candles, tf_from, tf_to):
    """Aggregate lower-timeframe candles into a higher timeframe (buckets aligned to the epoch)."""
    f, t = INTERVAL_SECONDS[tf_from] * 1000, INTERVAL_SECONDS[tf_to] * 1000
    if t < f or t % f:
        raise DataError(f"cannot resample {tf_from} -> {tf_to}")
    if t == f:
        return list(candles)
    out, cur = [], None
    for c in candles:
        b = c["t"] - (c["t"] % t)
        if cur is None or cur["t"] != b:
            if cur:
                out.append(cur)
            cur = {"t": b, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"]}
        else:
            cur["h"] = max(cur["h"], c["h"])
            cur["l"] = min(cur["l"], c["l"])
            cur["c"] = c["c"]
            cur["v"] += c["v"]
    if cur:
        out.append(cur)
    return out


class NotConfigured(ProviderError):
    def __init__(self, message):
        super().__init__(message, code="not_configured", status=503)


class Provider:
    name = "base"
    label = "Base"
    market = "crypto"           # crypto | stock
    requires_auth = False
    capabilities = ()
    rps = 5.0

    def __init__(self, base_url, allowed, session=None, sleep=time.sleep, headers=None, settings=None):
        self.settings = settings
        self.base_url = base_url
        self.http = SafeHttp(base_url, allowed, session=session, rps=self.rps, sleep=sleep, default_headers=headers)
        self._cache = {}
        self._extra_http = []

    def extra_http(self, base_url, allowed, timeout=60.0):
        c = SafeHttp(base_url, allowed, session=self.http.session, rps=1.0, timeout=timeout, sleep=self.http.sleep)
        self._extra_http.append(c)
        return c

    # ---- status ----
    def configured(self):
        return True

    def status(self):
        clients = [self.http] + self._extra_http
        calls = sum(c.calls for c in clients)
        errors = sum(c.errors for c in clients)
        last_ok = max([c.last_ok for c in clients if c.last_ok] or [None], key=lambda x: x or 0)
        errs = [(c.last_error_at, c.last_error) for c in clients if c.last_error_at]
        last_err_at, last_err = max(errs, default=(None, None))
        if not self.configured():
            state = "not_configured"
        elif not calls:
            state = "idle"
        elif last_err_at and (not last_ok or last_err_at > last_ok):
            state = "error"
        else:
            state = "connected"
        return {"name": self.name, "label": self.label, "market": self.market, "state": state,
                "configured": self.configured(), "requires_auth": self.requires_auth,
                "capabilities": list(self.capabilities), "calls": calls, "errors": errors, "last_ok": last_ok,
                "last_error": last_err, "last_error_at": last_err_at, "latency_ms": self.http.last_latency_ms}

    def cached(self, key, ttl, fn):
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (now, val)
        return val

    # ---- standard interface (adapters override what the source really offers) ----
    def symbols(self, query=None, limit=200):
        raise NotSupported(f"{self.name}: symbol list not supported")

    def tickers(self, symbols=None):
        raise NotSupported(f"{self.name}: tickers not supported")

    def ticker(self, symbol):
        res = self.tickers([symbol])
        if not res:
            raise ProviderError(f"{self.name}: no ticker for {symbol}", code="no_data")
        return res[0]

    def candles(self, symbol, tf, limit=500, start_ms=None, end_ms=None):
        raise NotSupported(f"{self.name}: candles not supported")

    def depth(self, symbol, levels=20):
        raise NotSupported(f"{self.name}: market depth is not provided by this source")

    def expiries(self, underlying):
        raise NotSupported(f"{self.name}: options not supported")

    def option_chain(self, underlying, expiry=None):
        raise NotSupported(f"{self.name}: options not supported")

    @staticmethod
    def clean(candles):
        good, _ = validate_candles(candles)
        return good


def std_ticker(provider, symbol, ltp, open_=None, high=None, low=None, volume=None, ts=None,
               quote_volume=None, change=None, change_pct=None, extra=None):
    """Normalised ticker. Missing fields stay None - never invented."""
    if change is None and open_ and ltp is not None:
        change = ltp - open_
    if change_pct is None and open_ and ltp is not None:
        change_pct = (ltp - open_) / open_ * 100.0
    t = {"provider": provider, "symbol": symbol, "ltp": ltp, "open": open_, "high": high, "low": low,
         "volume": volume, "quote_volume": quote_volume, "change": change, "change_pct": change_pct,
         "ts": ts or int(time.time())}
    if extra:
        t.update(extra)
    return t


def fnum(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def candle(t_ms, o, h, l, c, v):
    return {"t": int(t_ms), "o": o, "h": h, "l": l, "c": c, "v": v}
