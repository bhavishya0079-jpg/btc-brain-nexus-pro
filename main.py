"""BTC BRAIN NEXUS API (Starlette). Analysis & research only - no order execution, no fund movement."""
import json
import logging
import logging.handlers
import math
import os
import threading
import time
from pathlib import Path

import numpy as np
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import db as dbmod
from . import marketdata, security
from .config import BASE_DIR, DISCLAIMER, VERSION, settings
from .core import alerts as core_alerts
from .core import analysis as core_analysis
from .core import indicators as ind
from .core import options as core_options
from .core import paper as core_paper
from .core import scanner as core_scanner
from .core.strategy import default_spec
from .providers.base import INTERVAL_SECONDS, NotConfigured
from .providers.http import ForbiddenEndpoint, NotSupported, ProviderError
from .providers.manager import ProviderManager
from .security import ApiError, ValidationError, v_float, v_int, v_name, v_provider, v_symbol, v_tf
from .services import Services
from .ws import StreamHub

log = logging.getLogger("nexus")
STARTED = time.time()
MAX_BODY = 1_000_000


# ----------------------------------------------------------------------------- app state
class State:
    db = None
    manager = None
    svc = None
    scheduler = None
    hub = None


S = State()


def init_state(db=None, manager=None):
    S.db = db or dbmod.Database()
    S.db.migrate()
    security.ensure_admin(S.db)
    S.manager = manager or ProviderManager(S.db)
    S.svc = Services(S.db, S.manager)
    S.hub = StreamHub(S.manager)
    return S


def setup_logging():
    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(Path(settings.log_dir) / "nexus.log", maxBytes=2_000_000, backupCount=3)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("nexus")
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(h)
        root.addHandler(logging.StreamHandler())


# ----------------------------------------------------------------------------- json
def clean(o):
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return [clean(v) for v in o.tolist()]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def jr(data, status=200):
    return JSONResponse(clean(data), status_code=status)


def err(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


# ----------------------------------------------------------------------------- routing helper
class Ctx:
    def __init__(self, request, uid, body):
        self.request, self.uid, self.body = request, uid, body
        self.q = request.query_params
        self.p = request.path_params

    def qs(self, name, default=None):
        return self.q.get(name, default)


ROUTES = []


def api(path, methods=("GET",), auth=True, limit_key=None):
    def deco(fn):
        async def endpoint(request):
            ip = request.client.host if request.client else "?"
            if limit_key:
                if not security.limiter.allow(f"{limit_key}:{ip}", settings.login_limit_per_min):
                    return err(429, "rate_limited", "Too many attempts, slow down")
            elif not security.limiter.allow(f"api:{ip}", settings.rate_limit_per_min):
                return err(429, "rate_limited", "Too many requests")
            try:
                uid = authenticate(request) if auth else None
                body = {}
                if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                    raw = await request.body()
                    if len(raw) > MAX_BODY:
                        raise ApiError(413, "too_large", "Request body too large")
                    if raw:
                        try:
                            body = json.loads(raw)
                        except ValueError:
                            raise ApiError(400, "bad_json", "Body must be valid JSON")
                        if not isinstance(body, dict):
                            raise ApiError(400, "bad_json", "Body must be a JSON object")
                return jr(await run_in_threadpool(fn, Ctx(request, uid, body)))
            except ApiError as e:
                return err(e.status, e.code, e.message)
            except ValidationError as e:
                return err(400, "invalid_input", str(e))
            except ForbiddenEndpoint as e:
                log.error("blocked endpoint: %s", e)
                return err(403, "read_only_policy", str(e))
            except NotConfigured as e:
                return err(503, "not_configured", e.message)
            except NotSupported as e:
                return err(501, "not_supported", e.message)
            except ProviderError as e:
                return err(e.status if e.status and 400 <= e.status < 600 and e.status != 401 else 502, e.code, e.message)
            except ValueError as e:
                return err(400, "invalid_input", str(e))
            except Exception:
                log.exception("unhandled error on %s", request.url.path)
                return err(500, "internal_error", "Internal error (see server log)")
        ROUTES.append(Route("/api" + path, endpoint, methods=list(methods)))
        return fn
    return deco


def authenticate(request):
    h = request.headers.get("authorization", "")
    token = h[7:] if h.lower().startswith("bearer ") else request.query_params.get("token", "")
    user = security.verify_token(token) if token else None
    if not user:
        raise ApiError(401, "unauthorized", "Login required")
    row = S.db.one("SELECT id FROM users WHERE username=?", (user,))
    if not row:
        raise ApiError(401, "unauthorized", "Unknown user")
    return row["id"]


def prov(c, key="provider"):
    return v_provider(c.qs(key) or c.body.get(key))


def arg(c, name, default=None):
    return c.body.get(name, c.qs(name, default))


# ----------------------------------------------------------------------------- system
@api("/health", auth=False)
def health(c):
    return {"status": "ok", "version": VERSION, "uptime_sec": int(time.time() - STARTED), "disclaimer": DISCLAIMER}


@api("/auth/login", ("POST",), auth=False, limit_key="login")
def login(c):
    u, p = str(c.body.get("username", "")), str(c.body.get("password", ""))
    row = S.db.one("SELECT * FROM users WHERE username=?", (u,))
    ok = security.verify_password(p, row["pw_hash"]) if row else security.verify_password(p, security.hash_password("x"))
    if not row or not ok:
        raise ApiError(401, "bad_credentials", "Invalid username or password")
    return {"token": security.make_token(u), "username": u, "expires_hours": settings.session_hours}


@api("/auth/me")
def me(c):
    return {"user_id": c.uid, "disclaimer": DISCLAIMER}


@api("/providers")
def providers(c):
    return {"providers": S.manager.status_all()}


@api("/diagnostics")
def diagnostics(c):
    return {
        "version": VERSION, "uptime_sec": int(time.time() - STARTED), "database_ok": S.db.integrity_ok(),
        "providers": S.manager.status_all(),
        "streams": S.hub.status() if S.hub else [],
        "scheduler": {"enabled": settings.scheduler_enabled, "running": bool(S.scheduler and S.scheduler.is_alive())},
        "recent_provider_errors": S.db.query("SELECT provider,message,created_at FROM provider_log ORDER BY id DESC LIMIT 30"),
        "backups": S.svc.list_backups()[:5],
        "read_only": True, "disclaimer": DISCLAIMER,
    }


# ----------------------------------------------------------------------------- market data
@api("/market/symbols")
def symbols(c):
    p = prov(c)
    return {"symbols": S.manager.get(p).symbols(c.qs("q"), v_int(c.qs("limit"), "limit", 1, 1000, 200))}


@api("/market/tickers")
def tickers(c):
    p = prov(c)
    syms = [v_symbol(s) for s in c.qs("symbols", "").split(",") if s][:60]
    return {"tickers": S.manager.get(p).tickers(syms or None)[:500]}


@api("/market/candles")
def candles(c):
    p, sym, tf = prov(c), v_symbol(c.qs("symbol")), v_tf(c.qs("tf", "15m"))
    n = v_int(c.qs("limit"), "limit", 10, 5000, 500)
    return {"provider": p, "symbol": sym, "tf": tf, "candles": S.manager.candles(p, sym, tf, limit=n)}


@api("/market/depth")
def depth(c):
    p = prov(c)
    return S.manager.get(p).depth(v_symbol(c.qs("symbol")), v_int(c.qs("levels"), "levels", 1, 100, 20))


@api("/market/compare")
def compare_exchanges(c):
    """Exchange-wise comparison: items=binance:BTCUSDT,delta:BTCUSD"""
    out = []
    for item in c.qs("items", "").split(",")[:6]:
        if ":" not in item:
            continue
        p, s = item.split(":", 1)
        try:
            t = S.manager.get(v_provider(p)).ticker(v_symbol(s))
            out.append({**t, "error": None})
        except (ProviderError, ValidationError) as e:
            out.append({"provider": p, "symbol": s, "error": str(e)})
    return {"items": out}


@api("/market/movers")
def movers(c):
    """Top gainers/losers/volume from live 24h tickers of a provider."""
    p = prov(c)
    ts = [t for t in S.manager.get(p).tickers() if t.get("change_pct") is not None]
    key = "quote_volume" if any(t.get("quote_volume") for t in ts) else "volume"
    return {"gainers": sorted(ts, key=lambda t: -t["change_pct"])[:10], "losers": sorted(ts, key=lambda t: t["change_pct"])[:10],
            "volume": sorted(ts, key=lambda t: -(t.get(key) or 0))[:10]}


@api("/market/sectors")
def sectors(c):
    return {"sectors": S.manager.sector_analysis(prov(c)),
            "note": "Sector membership is a static reference map; changes come from live quotes."}


# ----------------------------------------------------------------------------- options
@api("/options/expiries")
def expiries(c):
    return {"expiries": S.manager.get(prov(c)).expiries(v_symbol(c.qs("underlying")))}


def _chain(c):
    p = prov(c)
    chain = S.manager.get(p).option_chain(v_symbol(c.qs("underlying")), c.qs("expiry"))
    return core_options.analyze_chain(chain, derive=c.qs("derive", "1") == "1")


@api("/options/chain")
def chain(c):
    return _chain(c)


@api("/options/strike")
def strike(c):
    return core_options.strike_analysis(_chain(c), v_float(c.qs("strike"), "strike", 0, 1e9))


@api("/options/scan", ("POST",))
def options_scan(c):
    p, und = v_provider(c.body.get("provider")), v_symbol(c.body.get("underlying"))
    ch = core_options.analyze_chain(S.manager.get(p).option_chain(und, c.body.get("expiry")), derive=True)
    return {"matches": core_scanner.scan_options_chain(ch, str(c.body.get("kind", "high_oi")), c.body.get("params", {})),
            "summary": ch["summary"]}


# ----------------------------------------------------------------------------- analysis / charts
def _closed(p, sym, tf, limit=300):
    return S.manager.closed_candles(p, sym, tf, limit)


@api("/analysis/signal")
def signal(c):
    p, sym = prov(c), v_symbol(c.qs("symbol"))
    tfs = [v_tf(t) for t in c.qs("tfs", "15m,1h,4h").split(",") if t][:5]
    data = {tf: _closed(p, sym, tf) for tf in tfs}
    return {"provider": p, "symbol": sym, **core_analysis.multi_timeframe_signal(data, primary=c.qs("primary"))}


@api("/analysis/overview")
def overview(c):
    p, sym, tf = prov(c), v_symbol(c.qs("symbol")), v_tf(c.qs("tf", "1h"))
    return {"provider": p, "symbol": sym, "tf": tf, **core_analysis.market_overview(_closed(p, sym, tf))}


def _overlay(name, parts, cs):
    h = [x["h"] for x in cs]
    l = [x["l"] for x in cs]
    cl = [x["c"] for x in cs]
    v = [x["v"] for x in cs]
    n = lambda i, d: int(parts[i]) if len(parts) > i and parts[i] else d  # noqa: E731
    if name in ("ema", "sma"):
        per = n(0, 20)
        return f"{name}{per}", "price", {"v": getattr(ind, name)(cl, per)}
    if name == "bb":
        return "bb", "price", ind.bollinger(cl, n(0, 20), float(parts[1]) if len(parts) > 1 else 2.0)
    if name == "vwap":
        return "vwap", "price", {"v": ind.vwap(h, l, cl, v, [x["t"] for x in cs])}
    if name == "supertrend":
        return "supertrend", "price", ind.supertrend(h, l, cl, n(0, 10), float(parts[1]) if len(parts) > 1 else 3.0)
    if name == "rsi":
        return "rsi", "sub", {"v": ind.rsi(cl, n(0, 14))}
    if name == "macd":
        return "macd", "sub", ind.macd(cl)
    if name == "adx":
        return "adx", "sub", ind.adx(h, l, cl, n(0, 14))
    if name == "stoch":
        return "stoch", "sub", ind.stochastic(h, l, cl)
    if name == "atr":
        return "atr", "sub", {"v": ind.atr(h, l, cl, n(0, 14))}
    raise ValidationError(f"unknown indicator '{name}'")


@api("/chart")
def chart(c):
    p, sym, tf = prov(c), v_symbol(c.qs("symbol")), v_tf(c.qs("tf", "15m"))
    cs = S.manager.candles(p, sym, tf, limit=v_int(c.qs("limit"), "limit", 30, 2000, 400))
    overlays = {}
    for spec in [s for s in c.qs("ind", "").split(",") if s][:10]:
        parts = spec.split(":")
        name, pane, series = _overlay(parts[0], parts[1:], cs)
        overlays[name] = {"pane": pane, "series": series}
    out = {"provider": p, "symbol": sym, "tf": tf, "candles": cs, "overlays": overlays}
    if len(cs) >= 20:
        h, l, cl = [x["h"] for x in cs], [x["l"] for x in cs], [x["c"] for x in cs]
        out["support_resistance"] = ind.support_resistance(h, l, cl)
        out["trend_lines"] = ind.trend_lines(h, l)
        if c.qs("fib", "0") == "1":
            hi, lo = max(h[-100:]), min(l[-100:])
            out["fibonacci"] = ind.fibonacci(hi, lo, "up" if cl[-1] >= (hi + lo) / 2 else "down")
    return out


# ----------------------------------------------------------------------------- scanner
@api("/scan", ("POST",))
def scan(c):
    p = v_provider(c.body.get("provider"))
    interval = v_tf(c.body.get("interval", "15m"))
    symbols_ = [v_symbol(s) for s in c.body.get("symbols") or []]
    if not symbols_:                              # default universe: most active symbols from live tickers
        ts = S.manager.get(p).tickers()
        key = "quote_volume" if any(t.get("quote_volume") for t in ts) else "volume"
        symbols_ = [t["symbol"] for t in sorted(ts, key=lambda t: -(t.get(key) or 0))[:v_int(c.body.get("top"), "top", 1, 40, 20)]]
    mtf = [v_tf(t) for t in c.body.get("mtf_intervals", [])] or None
    return core_scanner.run_scan(symbols_, str(c.body.get("scan_type")), interval, c.body.get("params", {}),
                                 lambda s, tf, n: _closed(p, s, tf, n), mtf_intervals=mtf,
                                 workers=2 if p in ("angelone", "groww") else 4)


# ----------------------------------------------------------------------------- strategies
@api("/strategies/default")
def strat_default(c):
    return default_spec()


@api("/strategies/validate", ("POST",))
def strat_validate(c):
    return S.svc.validate(c.body.get("spec"))


@api("/strategies", ("GET", "POST"))
def strategies(c):
    if c.request.method == "GET":
        return {"strategies": S.svc.list_strategies(c.uid)}
    return S.svc.create_strategy(c.uid, v_name(c.body.get("name")), c.body.get("spec"), str(c.body.get("note", ""))[:200])


@api("/strategies/{sid:int}", ("GET", "PUT", "DELETE"))
def strategy_item(c):
    sid = c.p["sid"]
    if c.request.method == "GET":
        return S.svc.get_strategy(c.uid, sid)
    if c.request.method == "PUT":
        return S.svc.update_strategy(c.uid, sid, c.body.get("spec"), str(c.body.get("note", ""))[:200])
    S.svc.delete_strategy(c.uid, sid)
    return {"deleted": sid}


@api("/strategies/{sid:int}/restore", ("POST",))
def strategy_restore(c):
    return S.svc.restore_version(c.uid, c.p["sid"], v_int(c.body.get("version"), "version", 1, 10**6))


@api("/strategies/{sid:int}/sandbox", ("POST",))
def strategy_sandbox(c):
    b = c.body
    return S.svc.backtest(c.uid, strategy_id=c.p["sid"], version=b.get("version"), provider=v_provider(b.get("provider")),
                          symbol=v_symbol(b.get("symbol")), tf=b.get("tf"), limit=v_int(b.get("limit"), "limit", 100, 5000, 1000),
                          sandbox=True, save=False)


# ----------------------------------------------------------------------------- backtest
def _bt_args(b):
    return dict(provider=v_provider(b.get("provider")), symbol=v_symbol(b.get("symbol")),
                tf=v_tf(b["tf"]) if b.get("tf") else None, limit=v_int(b.get("limit"), "limit", 100, 5000, 1500),
                capital=v_float(b.get("capital"), "capital", 100, 1e10, 100000.0),
                fee_pct=v_float(b.get("fee_pct"), "fee_pct", 0, 5, 0.05),
                slippage_pct=v_float(b.get("slippage_pct"), "slippage_pct", 0, 5, 0.02))


@api("/backtest", ("POST",))
def backtest(c):
    b = c.body
    return S.svc.backtest(c.uid, strategy_id=b.get("strategy_id"), version=b.get("version"), spec=b.get("spec"), **_bt_args(b))


@api("/backtest/runs")
def runs(c):
    return {"runs": S.svc.backtest_runs(c.uid)}


@api("/backtest/runs/{rid:int}")
def run_item(c):
    return S.svc.backtest_run(c.uid, c.p["rid"])


@api("/backtest/compare", ("POST",))
def bt_compare(c):
    a = _bt_args(c.body)
    ids = [int(i) for i in c.body.get("strategy_ids", [])]
    if len(ids) < 2:
        raise ApiError(400, "need_two", "Select at least two strategies")
    return S.svc.compare(c.uid, ids, a["provider"], a["symbol"], a["tf"] or "15m", a["limit"],
                         initial_capital=a["capital"], fee_pct=a["fee_pct"], slippage_pct=a["slippage_pct"])


# ----------------------------------------------------------------------------- paper trading
@api("/paper/accounts", ("GET", "POST"))
def paper_accounts(c):
    if c.request.method == "GET":
        rows = S.db.query("SELECT * FROM paper_accounts WHERE user_id=? ORDER BY id DESC", (c.uid,))
        return {"accounts": [{**r, "equity": core_paper.account_equity(S.db, r)} for r in rows]}
    b = c.body
    aid = core_paper.create_account(S.db, c.uid, v_name(b.get("name")), v_float(b.get("capital"), "capital", 100, 1e10),
                                    v_float(b.get("fee_pct"), "fee_pct", 0, 5, 0.05), b.get("strategy_id"), b.get("strategy_version"))
    return {"id": aid}


@api("/paper/accounts/{aid:int}")
def paper_report(c):
    return core_paper.report(S.db, c.uid, c.p["aid"])


@api("/paper/accounts/{aid:int}/positions", ("POST",))
def paper_open(c):
    b = c.body
    core_paper.get_account(S.db, c.uid, c.p["aid"])
    p, sym = v_provider(b.get("provider")), v_symbol(b.get("symbol"))
    price = v_float(b.get("price"), "price", 0, 1e12, 0.0) or S.manager.get(p).ticker(sym)["ltp"]   # live price when omitted
    pid = core_paper.open_position(S.db, c.uid, c.p["aid"], symbol=sym, provider=p, side=str(b.get("side", "")).upper(),
                                   price=price, stop_loss=v_float(b.get("stop_loss"), "stop_loss", 0, 1e12),
                                   targets=b.get("targets"), qty=b.get("qty"), risk_pct=v_float(b.get("risk_pct"), "risk_pct", 0.01, 100, 1.0),
                                   note=str(b.get("note", ""))[:200])
    return {"position_id": pid, "entry_price": price}


@api("/paper/accounts/{aid:int}/refresh", ("POST",))
def paper_refresh(c):
    return S.svc.paper_refresh(c.uid, c.p["aid"])


@api("/paper/positions/{pid:int}/close", ("POST",))
def paper_close(c):
    row = S.db.one("SELECT provider,symbol FROM paper_positions WHERE id=?", (c.p["pid"],))
    if not row:
        raise ApiError(404, "not_found", "Position not found")
    price = v_float(c.body.get("price"), "price", 0, 1e12, 0.0) or S.manager.get(row["provider"]).ticker(row["symbol"])["ltp"]
    return core_paper.close_position(S.db, c.uid, c.p["pid"], price, "manual")


@api("/paper/accounts/{aid:int}", ("DELETE",))
def paper_delete(c):
    core_paper.get_account(S.db, c.uid, c.p["aid"])
    S.db.execute("DELETE FROM paper_accounts WHERE id=?", (c.p["aid"],))
    return {"deleted": c.p["aid"]}


# ----------------------------------------------------------------------------- alerts
@api("/alerts", ("GET", "POST"))
def alerts(c):
    if c.request.method == "GET":
        return {"alerts": S.svc.list_alerts(c.uid), "kinds": core_alerts.ALERT_TYPES,
                "server_notifications": bool(settings.webhook_url or settings.telegram_bot_token)}
    b = c.body
    sym = v_symbol(b["symbol"]) if b.get("symbol") else None
    prv = v_provider(b["provider"]) if b.get("provider") else None
    return {"id": S.svc.create_alert(c.uid, v_name(b.get("name")), str(b.get("kind")), sym, prv,
                                     v_tf(b["tf"]) if b.get("tf") else None, b.get("params", {}))}


@api("/alerts/{aid:int}", ("PUT", "DELETE"))
def alert_item(c):
    row = S.db.one("SELECT id FROM alerts WHERE id=? AND user_id=?", (c.p["aid"], c.uid))
    if not row:
        raise ApiError(404, "not_found", "Alert not found")
    if c.request.method == "DELETE":
        S.db.execute("DELETE FROM alerts WHERE id=?", (c.p["aid"],))
    else:
        S.db.execute("UPDATE alerts SET enabled=? WHERE id=?", (1 if c.body.get("enabled", True) else 0, c.p["aid"]))
    return {"ok": True}


@api("/alerts/events")
def alert_events(c):
    since = v_int(c.qs("since_id"), "since_id", 0, 10**12, 0)
    rows = S.db.query("SELECT * FROM alert_events WHERE user_id=? AND id>? ORDER BY id DESC LIMIT 100", (c.uid, since))
    return {"events": rows, "unseen": S.db.one("SELECT COUNT(*) n FROM alert_events WHERE user_id=? AND seen=0", (c.uid,))["n"]}


@api("/alerts/events/seen", ("POST",))
def alert_seen(c):
    S.db.execute("UPDATE alert_events SET seen=1 WHERE user_id=?", (c.uid,))
    return {"ok": True}


@api("/alerts/run", ("POST",))
def alerts_run(c):
    return S.svc.run_alerts()


# ----------------------------------------------------------------------------- AI learning
@api("/learning/run", ("POST",))
def learning_run(c):
    b = c.body
    return S.svc.learning_run(c.uid, v_int(b.get("strategy_id"), "strategy_id", 1, 10**9), v_provider(b.get("provider")),
                              v_symbol(b.get("symbol")), v_tf(b["tf"]) if b.get("tf") else None,
                              v_int(b.get("limit"), "limit", 500, 5000, 3000), v_float(b.get("train_frac"), "train_frac", 0.5, 0.9, 0.7),
                              v_float(b.get("capital"), "capital", 100, 1e10, 100000.0), v_float(b.get("fee_pct"), "fee_pct", 0, 5, 0.05),
                              v_float(b.get("slippage_pct"), "slippage_pct", 0, 5, 0.02))


@api("/learning/proposals")
def proposals(c):
    sid = c.qs("strategy_id")
    return {"proposals": S.svc.list_proposals(c.uid, int(sid) if sid else None)}


@api("/learning/proposals/{pid:int}/{action}", ("POST",))
def proposal_action(c):
    pid, a = c.p["pid"], c.p["action"]
    if a == "approve":
        return S.svc.approve(c.uid, pid)
    if a == "reject":
        S.svc.reject(c.uid, pid)
        return {"ok": True}
    if a == "rollback":
        return S.svc.rollback(c.uid, pid)
    if a == "attach-paper":
        S.svc.attach_paper(c.uid, pid, v_int(c.body.get("account_id"), "account_id", 1, 10**9))
        return {"ok": True}
    raise ApiError(404, "unknown_action", "Unknown action")


@api("/audit")
def audit(c):
    return {"audit": S.svc.audit_log()}


# ----------------------------------------------------------------------------- backup
@api("/backup", ("GET", "POST"))
def backup(c):
    if c.request.method == "GET":
        return {"backups": S.svc.list_backups()}
    return {"created": S.svc.create_backup()}


@api("/backup/restore", ("POST",))
def backup_restore(c):
    return S.svc.restore_backup(str(c.body.get("name", "")))


# ----------------------------------------------------------------------------- live stream (Server-Sent Events)
async def stream_tickers(request):
    ip = request.client.host if request.client else "?"
    if not security.limiter.allow(f"sse:{ip}", 10):
        return err(429, "rate_limited", "Too many streams")
    try:
        authenticate(request)
        p = v_provider(request.query_params.get("provider"))
        syms = [v_symbol(s) for s in request.query_params.get("symbols", "").split(",") if s][:30]
        if not syms:
            raise ValidationError("symbols required")
    except ApiError as e:
        return err(e.status, e.code, e.message)
    except ValidationError as e:
        return err(400, "invalid_input", str(e))
    provider = S.manager.get(p)
    if request.query_params.get("ws", "1") == "1":
        try:
            S.hub.ensure(p, syms)
        except Exception:
            log.exception("websocket start failed")

    def gen():
        end = time.time() + 600
        while time.time() < end:
            try:
                ticks, missing = [], []
                for sym in syms:                       # prefer fresh WebSocket ticks, fall back to REST
                    t = S.hub.get(p, sym)
                    (ticks if t else missing).append(t or sym)
                if missing:
                    ticks += provider.tickers(missing)
                yield "data: " + json.dumps(clean({"tickers": ticks, "ts": time.time()})) + "\n\n"
            except Exception as e:     # keep the stream alive, report the failure to the client
                yield "event: problem\ndata: " + json.dumps({"message": str(e)[:200]}) + "\n\n"
            time.sleep(2)
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


ROUTES.append(Route("/api/stream/tickers", stream_tickers))


# ----------------------------------------------------------------------------- scheduler
class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="nexus-scheduler")
        self.halt = threading.Event()
        self.last_backup = time.time()

    def run(self):
        while not self.halt.wait(settings.alert_interval_sec):
            try:
                S.svc.run_alerts()
                if time.time() - self.last_backup > settings.auto_backup_hours * 3600:
                    S.svc.create_backup()
                    self.last_backup = time.time()
            except Exception:
                log.exception("scheduler iteration failed")


# ----------------------------------------------------------------------------- middleware / app factory
class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def wrapped(msg):
            if msg["type"] == "http.response.start":
                headers = list(msg.get("headers", []))
                for k, v in security.SECURITY_HEADERS.items():
                    if not any(h[0].lower() == k.lower().encode() for h in headers):
                        headers.append((k.encode(), v.encode()))
                msg["headers"] = headers
            await send(msg)

        await self.app(scope, receive, wrapped)


def create_app(db=None, manager=None, start_scheduler=None):
    setup_logging()
    init_state(db, manager)
    routes = list(ROUTES)
    fe = BASE_DIR.parent / "frontend"
    if settings.serve_frontend and fe.exists():
        routes.append(Mount("/", app=StaticFiles(directory=str(fe), html=True), name="frontend"))
    app = Starlette(routes=routes, middleware=[
        Middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST", "PUT", "DELETE"],
                   allow_headers=["Authorization", "Content-Type"]),
        Middleware(SecurityHeaders)])
    if (settings.scheduler_enabled if start_scheduler is None else start_scheduler):
        S.scheduler = Scheduler()
        S.scheduler.start()
    if settings.secret_key and not os.environ.get("NEXUS_SECRET_KEY"):
        log.warning("NEXUS_SECRET_KEY not set: sessions will reset on restart")
    if not S.db.one("SELECT id FROM users LIMIT 1"):
        log.warning("No user exists. Set NEXUS_ADMIN_PASSWORD (min 8 chars) and restart to create the admin account.")
    return app


def run():
    import uvicorn
    uvicorn.run(create_app(), host=os.environ.get("NEXUS_HOST", "127.0.0.1"),
                port=int(os.environ.get("NEXUS_PORT", "8000")))


if __name__ == "__main__":
    run()
