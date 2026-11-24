"""Delta Exchange India - public market data (tickers, candles, order book, options chain)."""
import re
import time
from datetime import datetime, timezone

from ..config import settings
from .base import INTERVAL_SECONDS, Provider, ProviderError, candle, fnum, std_ticker

ALLOWED = [("GET", r"/v2/(products|tickers|tickers/[A-Za-z0-9_.\-]+|history/candles|l2orderbook/[A-Za-z0-9_.\-]+)")]
TFS = {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"}
_OPT_RE = re.compile(r"^([CP])-([A-Z0-9]+)-(\d+(?:\.\d+)?)-(\d{6})$")


def parse_option_symbol(sym):
    """C-BTC-90000-271225 -> ('CE', 'BTC', 90000.0, date(2025,12,27)) or None."""
    m = _OPT_RE.match(sym or "")
    if not m:
        return None
    kind, und, strike, d = m.groups()
    try:
        exp = datetime.strptime(d, "%d%m%y").date()
    except ValueError:
        return None
    return ("CE" if kind == "C" else "PE"), und, float(strike), exp


class DeltaProvider(Provider):
    name, label, market = "delta", "Delta Exchange India", "crypto"
    capabilities = ("candles", "tickers", "depth", "options", "websocket")
    rps = 8.0

    def __init__(self, session=None, sleep=time.sleep, settings_=None):
        cfg = settings_ or settings
        super().__init__(cfg.delta_base, ALLOWED, session=session, sleep=sleep, settings=cfg)

    def _result(self, path, params=None):
        d = self.http.request("GET", path, params=params)
        if not isinstance(d, dict) or not d.get("success", True):
            raise ProviderError(f"delta: API error on {path}: {str(d)[:160]}", code="api_error")
        return d.get("result"), d.get("meta") or {}

    def _products(self, contract_types, extra=None):
        out, after = [], None
        for _ in range(20):
            p = {"contract_types": contract_types, "states": "live", "page_size": 200}
            p.update(extra or {})
            if after:
                p["after"] = after
            res, meta = self._result("/v2/products", p)
            out.extend(res or [])
            after = meta.get("after")
            if not after or not res:
                break
        return out

    def symbols(self, query=None, limit=200):
        prods = self.cached("prods", 600, lambda: self._products("perpetual_futures,futures"))
        q = (query or "").upper()
        out = []
        for p in prods:
            if q and q not in p["symbol"].upper():
                continue
            out.append({"symbol": p["symbol"], "base": (p.get("underlying_asset") or {}).get("symbol"),
                        "quote": (p.get("quoting_asset") or {}).get("symbol"), "exchange": "delta",
                        "type": p.get("contract_type")})
            if len(out) >= limit:
                break
        return out

    def _tick(self, t):
        ltp = fnum(t.get("close")) or fnum(t.get("mark_price"))
        if ltp is None:
            return None
        ts = t.get("timestamp")
        ts = int(ts / 1_000_000) if ts and ts > 1e14 else int(ts or time.time())
        return std_ticker(self.name, t["symbol"], ltp, fnum(t.get("open")), fnum(t.get("high")), fnum(t.get("low")),
                          fnum(t.get("volume")), ts, quote_volume=fnum(t.get("turnover_usd")),
                          extra={"mark_price": fnum(t.get("mark_price")), "oi": fnum(t.get("oi"))})

    def tickers(self, symbols=None):
        if symbols and len(symbols) == 1:
            res, _ = self._result(f"/v2/tickers/{symbols[0]}")
            rows = [res] if isinstance(res, dict) else (res or [])
        else:
            rows, _ = self.cached("tick", 2, lambda: self._result(
                "/v2/tickers", {"contract_types": "perpetual_futures,futures"}))
            if symbols:
                wanted = set(symbols)
                rows = [r for r in rows if r.get("symbol") in wanted]
        return [t for t in (self._tick(r) for r in rows or []) if t]

    def candles(self, symbol, tf, limit=500, start_ms=None, end_ms=None):
        if tf not in TFS:
            raise ProviderError(f"delta: unsupported timeframe {tf}", code="bad_request")
        step = INTERVAL_SECONDS[tf]
        end = int((end_ms or time.time() * 1000) / 1000)
        start = int(start_ms / 1000) if start_ms else end - limit * step
        rows, cur_end = [], end
        for _ in range(30):
            cur_start = max(start, cur_end - 1900 * step)
            res, _ = self._result("/v2/history/candles", {"symbol": symbol, "resolution": tf,
                                                         "start": cur_start, "end": cur_end})
            rows = (res or []) + rows
            if cur_start <= start or not res:
                break
            cur_end = cur_start - 1
        return self.clean([candle(int(r["time"]) * 1000, r["open"], r["high"], r["low"], r["close"],
                                  r.get("volume", 0)) for r in rows])[-limit:]

    def depth(self, symbol, levels=20):
        res, _ = self._result(f"/v2/l2orderbook/{symbol}", {"depth": levels})
        return {"symbol": symbol, "ts": int(time.time()),
                "bids": [[float(x["price"]), float(x["size"])] for x in (res.get("buy") or [])[:levels]],
                "asks": [[float(x["price"]), float(x["size"])] for x in (res.get("sell") or [])[:levels]]}

    # ----- options -----
    def expiries(self, underlying):
        prods = self.cached(f"optprods:{underlying}", 600, lambda: self._products(
            "call_options,put_options", {"underlying_asset_symbols": underlying}))
        today = datetime.now(timezone.utc).date()
        dates = {p[3] for p in (parse_option_symbol(x["symbol"]) for x in prods) if p}
        return [d.isoformat() for d in sorted(dates) if d >= today]

    def option_chain(self, underlying, expiry=None):
        exps = self.expiries(underlying)
        if not exps:
            raise ProviderError(f"delta: no live option expiries for {underlying}", code="no_data")
        expiry = expiry or exps[0]
        d = datetime.strptime(expiry, "%Y-%m-%d")
        res, _ = self._result("/v2/tickers", {"contract_types": "call_options,put_options",
                                              "underlying_asset_symbols": underlying,
                                              "expiry_date": d.strftime("%d-%m-%Y")})
        strikes, spot = {}, None
        for t in res or []:
            p = parse_option_symbol(t.get("symbol"))
            if not p:
                continue
            kind, _, strike, _ = p
            spot = spot or fnum(t.get("spot_price"))
            q, g = t.get("quotes") or {}, t.get("greeks") or {}
            iv = fnum(q.get("mark_iv"))
            leg = {"symbol": t["symbol"], "ltp": fnum(t.get("close")) or fnum(t.get("mark_price")),
                   "bid": fnum(q.get("best_bid")), "ask": fnum(q.get("best_ask")),
                   "oi": fnum(t.get("oi_contracts") if t.get("oi_contracts") is not None else t.get("oi")),
                   "oi_change": None,                      # not provided by this source -> left empty, never invented
                   "volume": fnum(t.get("volume")),
                   "iv": (iv * 100.0 if iv is not None and iv < 5 else iv),   # percent
                   **{k: fnum(g.get(k)) for k in ("delta", "gamma", "theta", "vega")}}
            strikes.setdefault(strike, {"strike": strike})[kind] = leg
        if spot is None:
            try:
                spot = self.ticker(f"{underlying}USD")["ltp"]
            except ProviderError:
                spot = None
        return {"provider": self.name, "underlying": underlying, "expiry": expiry, "spot": spot,
                "rows": sorted(strikes.values(), key=lambda r: r["strike"])}

    # WebSocket descriptor
    def ws_url(self, symbols):
        return "wss://socket.india.delta.exchange", {
            "type": "subscribe", "payload": {"channels": [{"name": "v2/ticker", "symbols": list(symbols)}]}}

    def parse_ws(self, msg):
        return self._tick(msg) if msg.get("type") == "v2/ticker" else None
