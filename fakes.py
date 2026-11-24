"""Test doubles. Used ONLY by tests - the application never fabricates market data."""
import json
import math


class FakeResp:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = text if text is not None else json.dumps(body)[:300]

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """requests.Session look-alike. routes: list of (url-substring, obj | callable(method,url,params,json,headers))."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, allow_redirects=False):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        for sub, r in self.routes:
            if sub in url:
                r = r(method, url, params, json, headers) if callable(r) else r
                return r if isinstance(r, FakeResp) else FakeResp(200, r)
        return FakeResp(404, None, text="not found")


def walk(n=600, start_ms=1_700_000_000_000, step_ms=900_000, seed=11, base=100.0, drift=0.0003, vol=0.008):
    """Deterministic pseudo-random OHLCV in the canonical {t,o,h,l,c,v} format (tests only)."""
    import random
    rnd = random.Random(seed)
    out, p = [], base
    for i in range(n):
        o = p
        c = o * (1 + drift * math.sin(i / 40) + vol * rnd.gauss(0, 1))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append({"t": start_ms + i * step_ms, "o": o, "h": h, "l": l, "c": c, "v": 1000 + 400 * abs(rnd.gauss(0, 1))})
        p = c
    return out


def binance_klines(candles):
    return [[c["t"], str(c["o"]), str(c["h"]), str(c["l"]), str(c["c"]), str(c["v"]), 0, 0, 0, 0, 0, 0] for c in candles]
