"""End-to-end HTTP tests: real uvicorn server + requests, providers backed by fake sessions."""
import socket
import threading
import time

import pytest
import requests
import uvicorn

from app import main, security
from app.config import settings
from app.core.strategy import default_spec
from app.db import Database
from app.providers import build_providers
from app.providers.manager import ProviderManager
from tests.fakes import FakeSession, binance_klines, walk

nosleep = lambda s: None  # noqa: E731
CANDLES = walk(1500, step_ms=900_000)


def _session():
    fut = time.strftime("%d%m%y", time.gmtime(time.time() + 6 * 86400))
    fut_iso = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 6 * 86400))
    leg = lambda k, kind, oi: {"symbol": f"{kind}-BTC-{k}-{fut}", "close": "300", "oi_contracts": str(oi), "volume": "5",  # noqa: E731
                               "spot_price": "65000", "quotes": {"mark_iv": "0.55"}, "greeks": {}}
    tick = lambda m, u, p, j, h: ({"symbol": "BTCUSDT", "lastPrice": str(CANDLES[-1]["c"]), "openPrice": "100", "highPrice": "200",  # noqa: E731
                                   "lowPrice": "50", "volume": "9", "quoteVolume": "900", "priceChangePercent": "1.5", "closeTime": 1}
                                  if p and "symbol" in p else
                                  [{"symbol": s, "lastPrice": str(CANDLES[-1]["c"]), "openPrice": "100", "highPrice": "200", "lowPrice": "50",
                                    "volume": "9", "quoteVolume": str(q), "priceChangePercent": str(c), "closeTime": 1}
                                   for s, q, c in (("BTCUSDT", 900, 1.5), ("ETHUSDT", 800, -2.0), ("SOLUSDT", 700, 4.0))])
    return FakeSession([
        ("/api/v3/klines", binance_klines(CANDLES)), ("/api/v3/ticker/24hr", tick),
        ("/api/v3/depth", {"bids": [["1", "2"]], "asks": [["2", "3"]]}),
        ("/v2/products", {"success": True, "result": [{"symbol": f"C-BTC-65000-{fut}"}], "meta": {}}),
        ("/v2/tickers", {"success": True, "result": [leg(65000, "C", 100), leg(65000, "P", 160), leg(70000, "C", 50), leg(70000, "P", 20)]}),
    ]), fut_iso


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("api")
    settings.backup_dir = str(tmp / "bk")
    settings.log_dir = str(tmp / "logs")
    db = Database(str(tmp / "api.db"))
    sess, fut_iso = _session()
    mgr = ProviderManager(db, build_providers(sess, nosleep))
    settings.admin_password = "api-test-password"
    app = main.create_app(db, mgr, start_scheduler=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}/api"
    for _ in range(100):
        try:
            requests.get(base + "/health", timeout=0.5)
            break
        except requests.RequestException:
            time.sleep(0.1)
    yield base, fut_iso
    srv.should_exit = True
    th.join(timeout=5)


@pytest.fixture(scope="module")
def auth(server):
    base, _ = server
    t = requests.post(base + "/auth/login", json={"username": "admin", "password": "api-test-password"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def get(base, path, auth, **p):
    return requests.get(base + path, headers=auth, params=p, timeout=30)


def post(base, path, auth, body=None):
    return requests.post(base + path, headers=auth, json=body or {}, timeout=60)


def test_health_headers_and_auth(server):
    base, _ = server
    r = requests.get(base + "/health")
    assert r.status_code == 200 and r.headers["X-Content-Type-Options"] == "nosniff" and "disclaimer" in r.json()
    assert requests.get(base + "/providers").status_code == 401
    assert requests.get(base + "/providers", headers={"Authorization": "Bearer junk"}).status_code == 401
    bad = requests.post(base + "/auth/login", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "bad_credentials"
    assert requests.post(base + "/auth/login", data="not json").status_code in (400, 401)


def test_market_endpoints(server, auth):
    base, _ = server
    t = get(base, "/market/tickers", auth, provider="binance", symbols="BTCUSDT").json()["tickers"]
    assert t[0]["symbol"] == "BTCUSDT"
    c = get(base, "/market/candles", auth, provider="binance", symbol="BTCUSDT", tf="15m", limit=300).json()["candles"]
    assert len(c) == 300 and set(c[0]) == {"t", "o", "h", "l", "c", "v"}
    assert get(base, "/market/depth", auth, provider="binance", symbol="BTCUSDT").json()["bids"]
    assert get(base, "/market/candles", auth, provider="binance", symbol="BTC;DROP", tf="15m").status_code == 400
    assert get(base, "/market/candles", auth, provider="binance", symbol="BTCUSDT", tf="7m").status_code == 400
    assert get(base, "/market/candles", auth, provider="bogus", symbol="BTCUSDT").status_code == 400
    mv = get(base, "/market/movers", auth, provider="binance").json()
    assert mv["gainers"][0]["symbol"] == "SOLUSDT" and mv["losers"][0]["symbol"] == "ETHUSDT"
    cmp_ = get(base, "/market/compare", auth, items="binance:BTCUSDT,groww:NSE:TCS").json()["items"]
    assert cmp_[0]["error"] is None and "not_configured" in str(cmp_[1]["error"]).lower() or cmp_[1]["error"]


def test_provider_states_and_not_configured(server, auth):
    base, _ = server
    st = {p["name"]: p["state"] for p in get(base, "/providers", auth).json()["providers"]}
    assert st["angelone"] == "not_configured" and st["groww"] == "not_configured" and st["binance"] in ("connected", "idle")
    r = get(base, "/market/tickers", auth, provider="angelone", symbols="NSE:NIFTY")
    assert r.status_code == 503 and r.json()["error"]["code"] == "not_configured"
    assert get(base, "/market/depth", auth, provider="angelone", symbol="NSE:NIFTY").status_code in (501, 503)
    d = get(base, "/diagnostics", auth).json()
    assert d["database_ok"] and d["read_only"]


def test_chart_signal_scan(server, auth):
    base, _ = server
    ch = get(base, "/chart", auth, provider="binance", symbol="BTCUSDT", tf="15m", limit=300,
             ind="ema:20,sma:50,bb,vwap,supertrend,rsi,macd,adx,stoch,atr", fib="1").json()
    assert set(ch["overlays"]) >= {"ema20", "sma50", "bb", "vwap", "supertrend", "rsi", "macd", "adx", "stoch", "atr"}
    assert len(ch["overlays"]["ema20"]["series"]["v"]) == len(ch["candles"]) and "support_resistance" in ch and ch["fibonacci"]
    assert get(base, "/chart", auth, provider="binance", symbol="BTCUSDT", ind="nope").status_code == 400
    sg = get(base, "/analysis/signal", auth, provider="binance", symbol="BTCUSDT", tfs="15m,1h").json()
    assert sg["signal"] in ("LONG", "SHORT", "WAIT") and sg["disclaimer"] and sg["confidence"] <= 90
    ov = get(base, "/analysis/overview", auth, provider="binance", symbol="BTCUSDT", tf="15m").json()
    assert "support_resistance" in ov
    sc = post(base, "/scan", auth, {"provider": "binance", "scan_type": "rsi", "interval": "15m", "params": {}, "top": 3}).json()
    assert sc["scanned"] == 3 and sc["errors"] == []
    assert post(base, "/scan", auth, {"provider": "binance", "scan_type": "bogus", "interval": "15m"}).status_code == 400


def test_options_chain(server, auth):
    base, fut = server
    ex = get(base, "/options/expiries", auth, provider="delta", underlying="BTC").json()["expiries"]
    assert fut in ex
    ch = get(base, "/options/chain", auth, provider="delta", underlying="BTC", expiry=fut).json()
    s = ch["summary"]
    assert s["atm_strike"] == 65000 and s["pcr_oi"] == pytest.approx(180 / 150) and s["max_pain"] in (65000, 70000)
    leg = ch["rows"][0]["CE"]
    assert leg["moneyness"] == "ATM" and leg["delta"] is not None and leg["greeks_source"] == "derived_bs"
    assert leg["oi_change"] is None                                  # provider gave none -> stays empty
    assert get(base, "/options/strike", auth, provider="delta", underlying="BTC", expiry=fut, strike=70000).status_code == 200


def test_strategy_backtest_flow_and_forbidden(server, auth):
    base, _ = server
    r = post(base, "/strategies", auth, {"name": "API strat", "spec": default_spec()})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert post(base, "/strategies", auth, {"name": "API strat", "spec": default_spec()}).status_code == 409
    evil = default_spec()
    evil["execute_live"] = True
    r2 = post(base, "/strategies", auth, {"name": "evil", "spec": evil})
    assert r2.status_code == 400 and r2.json()["error"]["code"] == "forbidden_field"
    assert post(base, "/strategies/validate", auth, {"spec": {"name": "x"}}).json()["valid"] is False
    bt = post(base, "/backtest", auth, {"strategy_id": sid, "provider": "binance", "symbol": "BTCUSDT", "tf": "15m", "limit": 1200}).json()
    assert bt["stats"]["trades"] >= 0 and "equity" in bt and "disclaimer" in bt["stats"] and bt["run_id"]
    assert get(base, f"/backtest/runs/{bt['run_id']}", auth).json()["stats"]
    assert len(get(base, "/backtest/runs", auth).json()["runs"]) >= 1
    sb = post(base, f"/strategies/{sid}/sandbox", auth, {"provider": "binance", "symbol": "BTCUSDT", "limit": 1000}).json()
    assert sb["sandbox"] is True
    spec2 = default_spec()
    spec2["risk"]["risk_per_trade_pct"] = 0.5
    assert requests.put(f"{base}/strategies/{sid}", headers=auth, json={"spec": spec2}).json()["version"] == 2
    assert post(base, f"/strategies/{sid}/restore", auth, {"version": 1}).json()["version"] == 3
    sid2 = post(base, "/strategies", auth, {"name": "API strat 2", "spec": default_spec()}).json()["id"]
    cmpr = post(base, "/backtest/compare", auth, {"strategy_ids": [sid, sid2], "provider": "binance", "symbol": "BTCUSDT", "tf": "15m", "limit": 1000})
    assert cmpr.status_code == 200 and len(cmpr.json()["results"]) == 2
    assert requests.delete(f"{base}/strategies/{sid2}", headers=auth).status_code == 200


def test_paper_alert_backup_flow(server, auth):
    base, _ = server
    price = CANDLES[-1]["c"]
    aid = post(base, "/paper/accounts", auth, {"name": "api paper", "capital": 50000}).json()["id"]
    pos = post(base, f"/paper/accounts/{aid}/positions", auth,
               {"provider": "binance", "symbol": "BTCUSDT", "side": "LONG", "stop_loss": price * 0.9,
                "targets": [{"price": price * 1.2, "qty_pct": 100}]})
    assert pos.status_code == 200, pos.text
    bad = post(base, f"/paper/accounts/{aid}/positions", auth, {"provider": "binance", "symbol": "BTCUSDT", "side": "LONG", "stop_loss": price * 2})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "bad_stop"
    assert post(base, f"/paper/accounts/{aid}/refresh", auth).status_code == 200
    rep = get(base, f"/paper/accounts/{aid}", auth).json()
    assert len(rep["open_positions"]) == 1 and rep["equity"]["equity"] > 0
    cl = post(base, f"/paper/positions/{pos.json()['position_id']}/close", auth)
    assert cl.status_code == 200
    assert get(base, f"/paper/accounts/{aid}", auth).json()["stats"]["closed_trades"] == 1
    # alerts
    al = post(base, "/alerts", auth, {"name": "px", "kind": "price", "symbol": "BTCUSDT", "provider": "binance",
                                      "params": {"condition": "above", "value": price * 0.1, "cooldown_sec": 0}})
    assert al.status_code == 200, al.text
    assert post(base, "/alerts/run", auth).json()["triggered"] >= 1
    ev = get(base, "/alerts/events", auth).json()
    assert ev["unseen"] >= 1 and "px" in ev["events"][0]["message"]
    post(base, "/alerts/events/seen", auth)
    assert get(base, "/alerts/events", auth).json()["unseen"] == 0
    # backup
    name = post(base, "/backup", auth).json()["created"]
    assert name in [b["name"] for b in get(base, "/backup", auth).json()["backups"]]
    assert post(base, "/backup/restore", auth, {"name": "../../x"}).status_code == 400
    assert post(base, "/backup/restore", auth, {"name": name}).status_code == 200
    assert get(base, "/audit", auth).json()["audit"]


def test_learning_endpoints_and_unverified_approval_blocked(server, auth):
    base, _ = server
    sid = post(base, "/strategies", auth, {"name": "learn me", "spec": default_spec()}).json()["id"]
    r = post(base, "/learning/run", auth, {"strategy_id": sid, "provider": "binance", "symbol": "BTCUSDT", "tf": "15m", "limit": 1500})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] in ("ok", "insufficient_data") and out["notes"]
    props = get(base, "/learning/proposals", auth, strategy_id=sid).json()["proposals"]
    for p in props:
        if p["verdict"] != "VALIDATED_OUT_OF_SAMPLE":
            assert post(base, f"/learning/proposals/{p['id']}/approve", auth).status_code == 409
    assert post(base, "/learning/proposals/999999/approve", auth).status_code == 404


def test_sse_stream_and_rate_limit(server, auth):
    base, _ = server
    tok = auth["Authorization"].split()[1]
    with requests.get(base + "/stream/tickers", params={"provider": "binance", "symbols": "BTCUSDT", "token": tok, "ws": "0"},
                      stream=True, timeout=10) as r:
        assert r.status_code == 200
        line = next(l for l in r.iter_lines() if l.startswith(b"data:"))
        assert b"BTCUSDT" in line
    assert requests.get(base + "/stream/tickers", params={"provider": "binance", "symbols": "BTCUSDT"}).status_code == 401
    old = settings.rate_limit_per_min
    settings.rate_limit_per_min = 3
    try:
        codes = [requests.get(base + "/health").status_code for _ in range(12)]
        assert 429 in codes
    finally:
        settings.rate_limit_per_min = old
        security.limiter.hits.clear()
