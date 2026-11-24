"""Binance spot - public market data only (no API key needed, none ever used)."""
import json
import time

from ..config import settings
from .base import INTERVAL_SECONDS, Provider, ProviderError, candle, fnum, std_ticker

ALLOWED = [("GET", r"/api/v3/(ticker/24hr|klines|depth|exchangeInfo|ping)")]
TFS = {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"}


class BinanceProvider(Provider):
    name, label, market = "binance", "Binance", "crypto"
    capabilities = ("candles", "tickers", "depth", "websocket")
    rps = 8.0

    def __init__(self, session=None, sleep=time.sleep, settings_=None):
        cfg = settings_ or settings
        super().__init__(cfg.binance_base, ALLOWED, session=session, sleep=sleep, settings=cfg)

    def symbols(self, query=None, limit=200):
        info = self.cached("exinfo", 3600, lambda: self.http.request("GET", "/api/v3/exchangeInfo"))
        q = (query or "").upper()
        out = []
        for s in info.get("symbols", []):
            if s.get("status") != "TRADING" or (q and q not in s["symbol"]):
                continue
            if not q and s.get("quoteAsset") not in ("USDT", "BTC", "ETH", "USDC"):
                continue
            out.append({"symbol": s["symbol"], "base": s.get("baseAsset"), "quote": s.get("quoteAsset"),
                        "exchange": "binance", "type": "spot"})
            if len(out) >= limit:
                break
        return out

    def tickers(self, symbols=None):
        if symbols and len(symbols) == 1:
            data = [self.http.request("GET", "/api/v3/ticker/24hr", params={"symbol": symbols[0]})]
        elif symbols:
            data = self.http.request("GET", "/api/v3/ticker/24hr",
                                     params={"symbols": json.dumps(symbols, separators=(",", ":"))})
        else:
            data = self.cached("t24", 2, lambda: self.http.request("GET", "/api/v3/ticker/24hr"))
        if not isinstance(data, list):
            raise ProviderError("binance: unexpected ticker response", code="invalid_response")
        out = []
        for d in data:
            ltp = fnum(d.get("lastPrice"))
            if ltp is None:
                continue
            out.append(std_ticker(
                self.name, d["symbol"], ltp, fnum(d.get("openPrice")), fnum(d.get("highPrice")),
                fnum(d.get("lowPrice")), fnum(d.get("volume")), int((d.get("closeTime") or time.time() * 1000) / 1000),
                quote_volume=fnum(d.get("quoteVolume")), change=fnum(d.get("priceChange")),
                change_pct=fnum(d.get("priceChangePercent")),
                extra={"bid": fnum(d.get("bidPrice")), "ask": fnum(d.get("askPrice"))}))
        return out

    def candles(self, symbol, tf, limit=500, start_ms=None, end_ms=None):
        if tf not in TFS:
            raise ProviderError(f"binance: unsupported timeframe {tf}", code="bad_request")
        step_ms = INTERVAL_SECONDS[tf] * 1000
        rows, remaining = [], limit
        for _ in range(40):
            if remaining <= 0:
                break
            n = min(1000, remaining)
            params = {"symbol": symbol, "interval": tf, "limit": n}
            if start_ms is not None:
                params["startTime"] = start_ms
            if end_ms is not None:
                params["endTime"] = end_ms
            batch = self.http.request("GET", "/api/v3/klines", params=params)
            if not isinstance(batch, list):
                raise ProviderError("binance: unexpected klines response", code="invalid_response")
            if not batch:
                break
            if start_ms is not None:            # paginate forward from start
                rows.extend(batch)
                start_ms = batch[-1][0] + step_ms
            else:                               # paginate backwards from end/now
                rows = batch + rows
                end_ms = batch[0][0] - 1
            if len(batch) < n:
                break
            remaining = limit - len(rows)
        return self.clean([candle(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows])[-limit:]

    def depth(self, symbol, levels=20):
        d = self.http.request("GET", "/api/v3/depth", params={"symbol": symbol, "limit": min(max(levels, 5), 5000)})
        return {"symbol": symbol, "ts": int(time.time()),
                "bids": [[float(p), float(q)] for p, q in d.get("bids", [])[:levels]],
                "asks": [[float(p), float(q)] for p, q in d.get("asks", [])[:levels]]}

    # WebSocket descriptor used by the stream hub
    def ws_url(self, symbols):
        streams = "/".join(f"{s.lower()}@ticker" for s in symbols)
        return f"wss://stream.binance.com:9443/stream?streams={streams}", None

    def parse_ws(self, msg):
        d = msg.get("data", msg)
        if d.get("e") != "24hrTicker":
            return None
        return std_ticker(self.name, d["s"], fnum(d["c"]), fnum(d["o"]), fnum(d["h"]), fnum(d["l"]),
                          fnum(d["v"]), int(d["E"] / 1000), quote_volume=fnum(d["q"]),
                          change=fnum(d["p"]), change_pct=fnum(d["P"]))
