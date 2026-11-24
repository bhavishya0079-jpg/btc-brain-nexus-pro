"""Provider adapters against fake sessions returning documented-shape payloads."""
import base64
import hashlib
import json
import socket
import threading
import time

import pytest

from app import ws
from app.config import Settings
from app.providers import angelone, binance, delta, groww
from app.providers.base import NotConfigured
from app.providers.http import ForbiddenEndpoint, NotSupported, ProviderError, SafeHttp
from tests.fakes import FakeResp, FakeSession, binance_klines, walk

nosleep = lambda s: None  # noqa: E731


def cfg(**kw):
    s = Settings()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ---------------- Binance ----------------
BT = {"symbol": "BTCUSDT", "lastPrice": "65000.5", "openPrice": "64000", "highPrice": "66000",
      "lowPrice": "63000", "volume": "123.4", "quoteVolume": "8000000", "priceChange": "1000.5",
      "priceChangePercent": "1.56", "closeTime": 1_700_000_000_000, "bidPrice": "65000", "askPrice": "65001"}

def test_binance_ticker_candles_depth_status():
    cs = walk(5, start_ms=1_700_000_000_000, step_ms=3_600_000)
    kl = binance_klines(cs) + [[1_700_100_000_000, "100", "90", "99", "98", "1", 0, 0, 0, 0, 0, 0]]  # invalid: high<low
    sess = FakeSession([
        ("/ticker/24hr", lambda m, u, p, j, h: BT if p and "symbol" in p else [BT]),
        ("/klines", kl), ("/depth", {"bids": [["65000", "1.5"]], "asks": [["65001", "2"]]})])
    p = binance.BinanceProvider(sess, nosleep, cfg())
    assert p.status()["state"] == "idle"
    t = p.tickers(["BTCUSDT"])[0]
    assert t["ltp"] == 65000.5 and t["change_pct"] == 1.56 and t["bid"] == 65000.0
    c = p.candles("BTCUSDT", "1h", limit=10)
    assert len(c) == 5 and c[0]["t"] == cs[0]["t"]                      # invalid candle dropped
    assert p.depth("BTCUSDT", 5)["asks"] == [[65001.0, 2.0]]
    assert p.status()["state"] == "connected"
    with pytest.raises(ProviderError):
        p.candles("BTCUSDT", "2h")


def test_binance_ws_parse():
    p = binance.BinanceProvider(FakeSession([]), nosleep, cfg())
    msg = {"stream": "x", "data": {"e": "24hrTicker", "E": 1_700_000_000_000, "s": "BTCUSDT", "c": "65000", "o": "64000",
                                   "h": "66000", "l": "63000", "v": "10", "q": "650000", "p": "1000", "P": "1.5625"}}
    assert p.parse_ws(msg)["ltp"] == 65000.0 and p.parse_ws({"data": {"e": "x"}}) is None


# ---------------- Delta ----------------
def test_delta_option_symbol():
    k, u, s, d = delta.parse_option_symbol("C-BTC-90000-271225")
    assert (k, u, s, d.isoformat()) == ("CE", "BTC", 90000.0, "2025-12-27")
    assert delta.parse_option_symbol("BTCUSD") is None


def test_delta_full_flow():
    now = int(time.time())
    fut = time.strftime("%d%m%y", time.gmtime(now + 5 * 86400))
    fut_iso = time.strftime("%Y-%m-%d", time.gmtime(now + 5 * 86400))
    opt = lambda kind, k, oi, iv: {"symbol": f"{kind}-BTC-{k}-{fut}", "close": "500", "mark_price": "500",  # noqa: E731
                                   "oi_contracts": str(oi), "volume": "10", "spot_price": "65000",
                                   "quotes": {"best_bid": "490", "best_ask": "510", "mark_iv": str(iv)},
                                   "greeks": {"delta": "0.5", "gamma": "0.0001", "theta": "-5", "vega": "20"}}
    sess = FakeSession([
        ("/v2/history/candles", {"success": True, "result": [
            {"time": 1_700_003_600, "open": 2, "high": 3, "low": 1, "close": 2.5, "volume": 5},
            {"time": 1_700_000_000, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 4}]}),
        ("/v2/l2orderbook/", {"success": True, "result": {"buy": [{"price": "99", "size": 3}], "sell": [{"price": "101", "size": 4}]}}),
        ("/v2/products", {"success": True, "result": [{"symbol": f"C-BTC-65000-{fut}"}], "meta": {}}),
        ("/v2/tickers/", {"success": True, "result": {"symbol": "BTCUSD", "close": "65000", "open": "64000", "high": "66000",
                                                      "low": "63000", "volume": 5, "timestamp": 1_700_000_000_000_000}}),
        ("/v2/tickers", {"success": True, "result": [opt("C", 65000, 100, 0.55), opt("P", 65000, 150, 0.57), opt("C", 70000, 80, 0.6)]}),
    ])
    p = delta.DeltaProvider(sess, nosleep, cfg())
    assert p.tickers(["BTCUSD"])[0]["ts"] == 1_700_000_000
    c = p.candles("BTCUSD", "1h", limit=10)
    assert [x["t"] for x in c] == [1_700_000_000_000, 1_700_003_600_000]    # ascending, ms
    assert p.depth("BTCUSD")["bids"][0] == [99.0, 3.0]
    assert fut_iso in p.expiries("BTC")
    ch = p.option_chain("BTC", fut_iso)
    r65 = next(r for r in ch["rows"] if r["strike"] == 65000)
    assert ch["spot"] == 65000 and r65["CE"]["oi"] == 100 and r65["PE"]["oi"] == 150
    assert r65["CE"]["iv"] == pytest.approx(55.0) and r65["CE"]["oi_change"] is None and r65["CE"]["delta"] == 0.5


def test_delta_api_error():
    p = delta.DeltaProvider(FakeSession([("/v2/tickers", {"success": False})]), nosleep, cfg())
    with pytest.raises(ProviderError):
        p.tickers()


# ---------------- Angel One ----------------
def test_totp_rfc6238_vectors():
    sec = base64.b32encode(b"12345678901234567890").decode()
    assert angelone.totp(sec, t=59, digits=8) == "94287082"
    assert angelone.totp(sec, t=1111111109, digits=8) == "07081804"
    assert angelone.totp(sec, t=20000000000, digits=8) == "65353130"


def test_angel_not_configured():
    p = angelone.AngelOneProvider(FakeSession([]), nosleep, cfg(angel_api_key=""))
    assert p.status()["state"] == "not_configured"
    with pytest.raises(NotConfigured):
        p.tickers(["NSE:NIFTY"])


def test_angel_login_quote_candles():
    c = cfg(angel_api_key="K", angel_client_code="C1", angel_pin="1234", angel_totp_secret="JBSWY3DPEHPK3PXP")
    seen = {}

    def candle_route(m, u, params, js, h):
        seen["auth"], seen["body"] = h["Authorization"], js
        return {"status": True, "data": [["2024-01-03T09:15:00+0530", 100, 110, 95, 105, 1000],
                                          ["2024-01-03T09:20:00+0530", 105, 106, 101, 102, 500]]}

    sess = FakeSession([
        ("loginByPassword", lambda m, u, p, js, h: seen.update(login=js) or {"status": True, "data": {"jwtToken": "JWT123"}}),
        ("getCandleData", candle_route),
        ("/market/v1/quote", {"status": True, "data": {"fetched": [
            {"exchange": "NSE", "symbolToken": "99926000", "ltp": 22000.5, "open": 21900, "high": 22100, "low": 21800,
             "close": 21950, "tradeVolume": 0, "percentChange": 0.25}]}})])
    p = angelone.AngelOneProvider(sess, nosleep, c)
    t = p.tickers(["NSE:NIFTY"])[0]
    assert t["ltp"] == 22000.5 and t["prev_close"] == 21950 and len(seen["login"]["totp"]) == 6
    cs = p.candles("NSE:NIFTY", "5m", limit=10)
    assert len(cs) == 2 and seen["auth"] == "Bearer JWT123" and seen["body"]["interval"] == "FIVE_MINUTE"
    assert cs[0]["t"] == 1_704_253_500_000      # 2024-01-03 09:15 IST


# ---------------- Groww ----------------
def test_groww_quote_candles_chain():
    seen = {}
    sess = FakeSession([
        ("/v1/live-data/quote", lambda m, u, p, js, h: seen.update(h=h, p=p) or {"status": "SUCCESS", "payload": {
            "last_price": 1250.5, "ohlc": {"open": 1240, "high": 1260, "low": 1235, "close": 1245}, "volume": 100000,
            "day_change": 5.5, "day_change_perc": 0.44}}),
        ("/v1/historical/candles", {"status": "SUCCESS", "payload": {"candles": [[1_704_253_500, 10, 12, 9, 11, 100], [1_704_253_800, 11, 13, 10, 12, 80]]}}),
        ("/v1/option-chain", {"status": "SUCCESS", "payload": {"underlying_ltp": 22000, "strikes": {"22000": {
            "CE": {"trading_symbol": "N22000CE", "ltp": 200, "open_interest": 1000, "volume": 50,
                   "greeks": {"delta": 0.5, "gamma": 0.001, "theta": -10, "vega": 12, "iv": 14.5}},
            "PE": {"trading_symbol": "N22000PE", "ltp": 180, "open_interest": 1500, "volume": 40}}}}})])
    p = groww.GrowwProvider(sess, nosleep, cfg(groww_access_token="TOK"))
    t = p.tickers(["NSE:RELIANCE"])[0]
    assert t["ltp"] == 1250.5 and seen["h"]["Authorization"] == "Bearer TOK" and seen["p"]["trading_symbol"] == "RELIANCE"
    assert len(p.candles("NSE:RELIANCE", "5m", limit=5)) == 2
    row = p.option_chain("NIFTY", "2099-01-01")["rows"][0]
    assert row["CE"]["delta"] == 0.5 and row["PE"]["delta"] is None and row["PE"]["oi"] == 1500


def test_groww_not_configured():
    p = groww.GrowwProvider(FakeSession([]), nosleep, cfg(groww_access_token=""))
    assert p.status()["state"] == "not_configured"
    with pytest.raises(NotConfigured):
        p.tickers(["NSE:TCS"])


# ---------------- safety: read-only policy ----------------
def test_orders_and_funds_endpoints_are_blocked():
    h = SafeHttp("https://api.binance.com", [("GET", r"/api/v3/(ticker/24hr|klines)")], session=FakeSession([]), sleep=nosleep)
    for path in ("/api/v3/order", "/api/v3/account", "/sapi/v1/capital/withdraw/apply", "/api/v3/klines/../order"):
        with pytest.raises(ForbiddenEndpoint):
            h.request("GET", path)
    with pytest.raises(ForbiddenEndpoint):
        h.request("POST", "/api/v3/klines")                  # wrong method for an allowed path


def test_every_adapter_allowlist_excludes_trading_words():
    from app.providers.http import FORBIDDEN
    for mod in (binance, delta, angelone, groww):
        for method, pat in mod.ALLOWED:
            assert method in ("GET", "POST")
            assert not FORBIDDEN.search(pat.replace(r"\.", ".")), (mod.__name__, pat)


def test_no_order_methods_on_providers():
    for cls in (binance.BinanceProvider, delta.DeltaProvider, angelone.AngelOneProvider, groww.GrowwProvider):
        names = " ".join(dir(cls)).lower()
        for bad in ("place", "withdraw", "deposit", "transfer", "cancel", "modify"):
            assert bad not in names, (cls.__name__, bad)


# ---------------- http behaviour ----------------
def test_retry_429_then_success():
    n = {"i": 0}

    def r(*a):
        n["i"] += 1
        return FakeResp(429, None, {"Retry-After": "1"}) if n["i"] < 3 else FakeResp(200, {"ok": 1})

    h = SafeHttp("https://x.test", [("GET", r"/y")], session=FakeSession([("x.test", r)]), sleep=nosleep)
    assert h.request("GET", "/y") == {"ok": 1} and n["i"] == 3


def test_auth_error_and_redirect_not_followed():
    h = SafeHttp("https://x.test", [("GET", r"/y")], session=FakeSession([("x.test", FakeResp(403, None, text="no"))]), sleep=nosleep)
    with pytest.raises(ProviderError) as e:
        h.request("GET", "/y")
    assert e.value.code == "auth_error"
    h2 = SafeHttp("https://x.test", [("GET", r"/y")], session=FakeSession([("x.test", FakeResp(302, None, {"Location": "http://evil"}))]), sleep=nosleep)
    with pytest.raises(ProviderError) as e2:
        h2.request("GET", "/y")
    assert e2.value.code == "redirect_refused"


def test_unsupported_never_fabricates():
    p = angelone.AngelOneProvider(FakeSession([]), nosleep, cfg())
    with pytest.raises(NotSupported):
        p.depth("NSE:NIFTY")


# ---------------- websocket ----------------
def test_frame_codec_roundtrip():
    class Pipe:
        def __init__(self, d):
            self.d = d

        def recv(self, n):
            o, self.d = self.d[:n], self.d[n:]
            return o

    for payload in (b"hi", b"x" * 200, b"y" * 70000):
        for mask in (True, False):
            fin, op, data = ws.read_frame(Pipe(ws.encode_frame(0x1, payload, mask=mask)))
            assert fin and op == 1 and data == payload


def test_ws_client_loopback_and_reconnect():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    subs = []

    def serve():
        for n in range(2):
            c, _ = srv.accept()
            req = c.recv(4096).decode()
            key = [l.split(": ")[1] for l in req.split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
            acc = base64.b64encode(hashlib.sha1((key + ws.GUID).encode()).digest()).decode()
            c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode())
            subs.append(json.loads(ws.read_frame(c)[2]))
            c.sendall(ws.encode_frame(0x1, json.dumps({"n": n}), mask=False))
            c.sendall(ws.encode_frame(0x9, b"p", mask=False))
            time.sleep(0.2)
            c.close()

    threading.Thread(target=serve, daemon=True).start()
    msgs = []
    cl = ws.WSClient(f"ws://127.0.0.1:{port}/x", msgs.append, {"subscribe": "t"}, max_backoff=1)
    cl.start()
    end = time.time() + 8
    while time.time() < end and len(msgs) < 2:
        time.sleep(0.05)
    cl.stop()
    assert [m["n"] for m in msgs] == [0, 1] and subs == [{"subscribe": "t"}] * 2 and cl.reconnects >= 1
