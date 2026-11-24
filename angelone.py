"""Angel One SmartAPI - read-only market data (login, quotes, candles, option greeks).

Only login + market-data endpoints are allow-listed. There are no order / fund endpoints.
Credentials (API key, client code, PIN, TOTP secret) come from the server environment only.
"""
import base64
import hashlib
import hmac
import struct
import time
from datetime import datetime

from ..config import settings
from .base import IST, INTERVAL_SECONDS, NotConfigured, Provider, ProviderError, candle, fnum, resample, std_ticker

BASE = "https://apiconnect.angelbroking.com"
MASTER_HOST = "https://margincalculator.angelbroking.com"
ALLOWED = [
    ("POST", r"/rest/auth/angelbroking/user/v1/loginByPassword"),
    ("POST", r"/rest/secure/angelbroking/market/v1/quote/?"),
    ("POST", r"/rest/secure/angelbroking/historical/v1/getCandleData"),
    ("POST", r"/rest/secure/angelbroking/marketData/v1/optionGreek"),
]
TF_MAP = {"1m": "ONE_MINUTE", "3m": "THREE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE",
          "30m": "THIRTY_MINUTE", "1h": "ONE_HOUR", "1d": "ONE_DAY"}
MAX_DAYS = {"1m": 25, "3m": 85, "5m": 85, "15m": 170, "30m": 170, "1h": 350, "1d": 1700}  # conservative per-request spans
INDEX_TOKENS = {
    "NSE:NIFTY": ("NSE", "99926000", "Nifty 50"), "NSE:BANKNIFTY": ("NSE", "99926009", "Nifty Bank"),
    "NSE:FINNIFTY": ("NSE", "99926037", "Nifty Fin Service"), "NSE:MIDCPNIFTY": ("NSE", "99926074", "NIFTY MID SELECT"),
    "BSE:SENSEX": ("BSE", "99919000", "SENSEX"),
}


def totp(secret, t=None, digits=6, step=30):
    """RFC 6238 TOTP (HMAC-SHA1)."""
    s = secret.replace(" ", "").upper()
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    h = hmac.new(key, struct.pack(">Q", int((time.time() if t is None else t) // step)), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


class AngelOneProvider(Provider):
    name, label, market = "angelone", "Angel One", "stock"
    requires_auth = True
    capabilities = ("candles", "tickers", "options")
    rps = 2.0

    def __init__(self, session=None, sleep=time.sleep, settings_=None):
        cfg = settings_ or settings
        super().__init__(BASE, ALLOWED, session=session, sleep=sleep, settings=cfg)
        self.master_http = self.extra_http(MASTER_HOST, [("GET", r"/OpenAPI_File/files/OpenAPIScripMaster\.json")])
        self.jwt, self.jwt_at = None, 0.0

    def configured(self):
        s = self.settings
        return all([s.angel_api_key, s.angel_client_code, s.angel_pin, s.angel_totp_secret])

    def _headers(self, auth=True):
        h = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER",
             "X-SourceID": "WEB", "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
             "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": self.settings.angel_api_key}
        if auth:
            h["Authorization"] = f"Bearer {self.jwt}"
        return h

    def login(self):
        if not self.configured():
            raise NotConfigured("angelone: set ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, ANGEL_TOTP_SECRET")
        d = self.http.request("POST", "/rest/auth/angelbroking/user/v1/loginByPassword", headers=self._headers(False),
                              json={"clientcode": self.settings.angel_client_code, "password": self.settings.angel_pin,
                                    "totp": totp(self.settings.angel_totp_secret)})
        if not d or not d.get("status") or not d.get("data"):
            raise ProviderError(f"angelone login failed: {(d or {}).get('message')}", code="auth_error")
        self.jwt, self.jwt_at = d["data"]["jwtToken"], time.time()

    def _ensure(self):
        if not self.configured():
            raise NotConfigured("angelone: credentials not configured")
        if not self.jwt or time.time() - self.jwt_at > 6 * 3600:
            self.login()

    def _post(self, path, body):
        self._ensure()
        d = self.http.request("POST", path, json=body, headers=self._headers())
        if d and d.get("errorcode") in ("AG8001", "AG8002", "AB8050", "AB8051"):   # token invalid/expired
            self.login()
            d = self.http.request("POST", path, json=body, headers=self._headers())
        if not d or not d.get("status"):
            raise ProviderError(f"angelone: {(d or {}).get('message', 'no response')} ({(d or {}).get('errorcode')})",
                                code="api_error")
        return d.get("data")

    # ----- instruments -----
    def _master(self):
        return self.cached("master", 12 * 3600, lambda: self.master_http.request(
            "GET", "/OpenAPI_File/files/OpenAPIScripMaster.json")) or []

    def _resolve(self, symbol):
        if symbol in INDEX_TOKENS:
            ex, tok, name = INDEX_TOKENS[symbol]
            return ex, tok, name
        ex, _, name = symbol.partition(":")
        if not name:
            raise ProviderError("angelone: use symbols like NSE:RELIANCE or NSE:NIFTY", code="bad_request")
        for it in self._master():
            if it.get("exch_seg") == ex and it.get("symbol") in (f"{name}-EQ", name):
                return ex, it["token"], it["symbol"]
        raise ProviderError(f"angelone: symbol {symbol} not found in scrip master", code="bad_request")

    def symbols(self, query=None, limit=200):
        q = (query or "").upper()
        out = [{"symbol": s, "name": v[2], "exchange": v[0], "type": "index"} for s, v in INDEX_TOKENS.items()
               if not q or q in s]
        if q:
            for it in self._master():
                if it.get("exch_seg") in ("NSE", "BSE") and it.get("symbol", "").endswith("-EQ") and q in it["symbol"]:
                    out.append({"symbol": f"{it['exch_seg']}:{it['symbol'][:-3]}", "name": it.get("name"),
                                "exchange": it["exch_seg"], "type": "equity"})
                    if len(out) >= limit:
                        break
        return out[:limit]

    # ----- quotes -----
    def tickers(self, symbols=None):
        symbols = symbols or list(INDEX_TOKENS)
        by_ex, back = {}, {}
        for s in symbols:
            ex, tok, _ = self._resolve(s)
            by_ex.setdefault(ex, []).append(tok)
            back[(ex, tok)] = s
        data = self._post("/rest/secure/angelbroking/market/v1/quote/", {"mode": "FULL", "exchangeTokens": by_ex})
        out = []
        for f in (data or {}).get("fetched", []):
            sym, ltp = back.get((f.get("exchange"), str(f.get("symbolToken")))), fnum(f.get("ltp"))
            if not sym or ltp is None:
                continue
            close = fnum(f.get("close"))
            out.append(std_ticker(self.name, sym, ltp, fnum(f.get("open")), fnum(f.get("high")), fnum(f.get("low")),
                                  fnum(f.get("tradeVolume")), int(time.time()),
                                  change=(ltp - close) if close else None, change_pct=fnum(f.get("percentChange")),
                                  extra={"prev_close": close, "oi": fnum(f.get("opnInterest"))}))
        return out

    # ----- candles -----
    def candles(self, symbol, tf, limit=500, start_ms=None, end_ms=None):
        src = "1h" if tf == "4h" else tf
        if src not in TF_MAP:
            raise ProviderError(f"angelone: unsupported timeframe {tf}", code="bad_request")
        ex, tok, _ = self._resolve(symbol)
        need = limit * (4 if tf == "4h" else 1)
        end = int((end_ms or time.time() * 1000) / 1000)
        start = int(start_ms / 1000) if start_ms else int(end - need * INTERVAL_SECONDS[src] * 2.2)  # covers closures
        rows, cur_end, span = [], end, MAX_DAYS[src] * 86400
        for _ in range(40):
            cur_start = max(start, cur_end - span)
            data = self._post("/rest/secure/angelbroking/historical/v1/getCandleData", {
                "exchange": ex, "symboltoken": tok, "interval": TF_MAP[src],
                "fromdate": datetime.fromtimestamp(cur_start, IST).strftime("%Y-%m-%d %H:%M"),
                "todate": datetime.fromtimestamp(cur_end, IST).strftime("%Y-%m-%d %H:%M")})
            rows = (data or []) + rows
            if cur_start <= start or len(rows) >= need:
                break
            cur_end = cur_start - 60
        cs = []
        for r in rows:
            try:
                ts = int(datetime.strptime(r[0], "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1000)
            except (ValueError, TypeError):
                continue
            cs.append(candle(ts, r[1], r[2], r[3], r[4], r[5]))
        cs = self.clean(cs)
        return (resample(cs, "1h", "4h") if tf == "4h" else cs)[-limit:]

    # ----- options -----
    def _contracts(self, underlying):
        items = [i for i in self._master() if i.get("exch_seg") == "NFO"
                 and i.get("instrumenttype") in ("OPTIDX", "OPTSTK") and i.get("name") == underlying]
        today = datetime.now(IST).date()
        exps = sorted({datetime.strptime(i["expiry"], "%d%b%Y").date() for i in items if i.get("expiry")})
        return items, [e for e in exps if e >= today]

    def expiries(self, underlying):
        self._ensure()
        return [e.isoformat() for e in self._contracts(underlying.upper())[1]]

    def option_chain(self, underlying, expiry=None):
        self._ensure()
        underlying = underlying.upper()
        items, exps = self._contracts(underlying)
        if not exps:
            raise ProviderError(f"angelone: no option contracts for {underlying}", code="no_data")
        expiry = expiry or exps[0].isoformat()
        exp_s = datetime.strptime(expiry, "%Y-%m-%d").strftime("%d%b%Y").upper()
        tokens = {i["token"]: i for i in items if i["expiry"].upper() == exp_s}
        quotes, toks = {}, list(tokens)
        for k in range(0, len(toks), 50):
            data = self._post("/rest/secure/angelbroking/market/v1/quote/",
                              {"mode": "FULL", "exchangeTokens": {"NFO": toks[k:k + 50]}})
            for f in (data or {}).get("fetched", []):
                quotes[str(f.get("symbolToken"))] = f
        greeks = {}
        try:
            for x in self._post("/rest/secure/angelbroking/marketData/v1/optionGreek",
                                {"name": underlying, "expirydate": exp_s}) or []:
                greeks[(float(x["strikePrice"]), x["optionType"])] = x
        except ProviderError:
            pass            # greeks optional; the analysis layer derives them from price (labelled) when missing
        strikes = {}
        for tok, it in tokens.items():
            strike = float(it["strike"]) / 100.0
            kind = "CE" if it["symbol"].endswith("CE") else "PE"
            q, gx = quotes.get(tok, {}), greeks.get((strike, kind), {})
            leg = {"symbol": it["symbol"], "ltp": fnum(q.get("ltp")), "bid": None, "ask": None,
                   "oi": fnum(q.get("opnInterest")), "oi_change": None, "volume": fnum(q.get("tradeVolume")),
                   "iv": fnum(gx.get("impliedVolatility")),
                   **{k: fnum(gx.get(k)) for k in ("delta", "gamma", "theta", "vega")}}
            strikes.setdefault(strike, {"strike": strike})[kind] = leg
        try:
            spot = self.ticker(f"NSE:{underlying}")["ltp"]
        except ProviderError:
            spot = None
        return {"provider": self.name, "underlying": underlying, "expiry": expiry, "spot": spot,
                "rows": sorted(strikes.values(), key=lambda r: r["strike"])}
