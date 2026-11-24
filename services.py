"""Application services: strategy store, backtests, paper refresh, alerts, AI-learning workflow, backups."""
import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from . import marketdata
from .config import settings
from .core import alerts as core_alerts
from .core import backtest as core_bt
from .core import learning as core_learning
from .core import paper as core_paper
from .core.strategy import default_spec, normalize_spec, validate_spec
from .providers.base import INTERVAL_SECONDS
from .security import ApiError, ValidationError, assert_no_live_keys

log = logging.getLogger("nexus.services")


def jl(s, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


class Services:
    def __init__(self, db, manager):
        self.db, self.manager = db, manager
        self._alert_lock = threading.Lock()

    # ------------------------------------------------------------ strategies
    def _check_spec(self, spec):
        try:
            assert_no_live_keys(spec)
        except ValidationError as e:
            raise ApiError(400, "forbidden_field", str(e))
        errors, warnings = validate_spec(spec)
        if errors:
            raise ApiError(400, "invalid_strategy", "; ".join(errors))
        return normalize_spec(spec), warnings

    def validate(self, spec):
        try:
            assert_no_live_keys(spec)
        except ValidationError as e:
            return {"valid": False, "errors": [str(e)], "warnings": []}
        errors, warnings = validate_spec(spec)
        return {"valid": not errors, "errors": errors, "warnings": warnings}

    def create_strategy(self, uid, name, spec, note=""):
        spec, warnings = self._check_spec(spec)
        name = (name or spec["name"]).strip()[:80]
        if not name:
            raise ApiError(400, "bad_name", "name required")
        if self.db.one("SELECT id FROM strategies WHERE user_id=? AND name=?", (uid, name)):
            raise ApiError(409, "duplicate", "A strategy with this name already exists")
        now = time.time()
        sid = self.db.execute("INSERT INTO strategies(user_id,name,active_version,created_at,updated_at) VALUES (?,?,?,?,?)",
                              (uid, name, 1, now, now))
        self.db.execute("INSERT INTO strategy_versions(strategy_id,version,spec,status,note,created_by,created_at) "
                        "VALUES (?,?,?,?,?,?,?)", (sid, 1, json.dumps(spec), "active", note, "user", now))
        self.db.audit("user", "strategy.create", "strategy", sid, {"name": name})
        return {"id": sid, "version": 1, "warnings": warnings}

    def _strategy(self, uid, sid):
        row = self.db.one("SELECT * FROM strategies WHERE id=? AND user_id=?", (sid, uid))
        if not row:
            raise ApiError(404, "not_found", "Strategy not found")
        return row

    def list_strategies(self, uid):
        rows = self.db.query("SELECT * FROM strategies WHERE user_id=? ORDER BY updated_at DESC", (uid,))
        for r in rows:
            r["versions"] = self.db.one("SELECT COUNT(*) n FROM strategy_versions WHERE strategy_id=?", (r["id"],))["n"]
        return rows

    def get_strategy(self, uid, sid):
        s = self._strategy(uid, sid)
        vers = self.db.query("SELECT * FROM strategy_versions WHERE strategy_id=? ORDER BY version DESC", (sid,))
        for v in vers:
            v["spec"] = jl(v["spec"])
        s["versions"] = vers
        s["active_spec"] = next((v["spec"] for v in vers if v["version"] == s["active_version"]), None)
        return s

    def spec_of(self, uid, sid, version=None):
        s = self._strategy(uid, sid)
        v = version or s["active_version"]
        row = self.db.one("SELECT spec FROM strategy_versions WHERE strategy_id=? AND version=?", (sid, v))
        if not row:
            raise ApiError(404, "not_found", "Version not found")
        return jl(row["spec"]), v

    def _add_version(self, sid, spec, note, created_by, status, parent):
        nxt = self.db.one("SELECT COALESCE(MAX(version),0)+1 n FROM strategy_versions WHERE strategy_id=?", (sid,))["n"]
        self.db.execute("INSERT INTO strategy_versions(strategy_id,version,spec,status,parent_version,note,created_by,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?)", (sid, nxt, json.dumps(spec), status, parent, note, created_by, time.time()))
        return nxt

    def update_strategy(self, uid, sid, spec, note=""):
        s = self._strategy(uid, sid)
        spec, warnings = self._check_spec(spec)
        v = self._add_version(sid, spec, note or "edited", "user", "active", s["active_version"])
        self._activate(sid, v)
        self.db.audit("user", "strategy.update", "strategy", sid, {"version": v})
        return {"id": sid, "version": v, "warnings": warnings}

    def _activate(self, sid, version):
        self.db.execute("UPDATE strategy_versions SET status='archived' WHERE strategy_id=? AND status='active'", (sid,))
        self.db.execute("UPDATE strategy_versions SET status='active' WHERE strategy_id=? AND version=?", (sid, version))
        self.db.execute("UPDATE strategies SET active_version=?, updated_at=? WHERE id=?", (version, time.time(), sid))

    def restore_version(self, uid, sid, version):
        spec, _ = self.spec_of(uid, sid, version)
        v = self._add_version(sid, spec, f"restored from v{version}", "restore", "active", version)
        self._activate(sid, v)
        self.db.audit("user", "strategy.restore", "strategy", sid, {"from": version, "new": v})
        return {"id": sid, "version": v}

    def delete_strategy(self, uid, sid):
        self._strategy(uid, sid)
        self.db.execute("DELETE FROM strategies WHERE id=?", (sid,))
        self.db.audit("user", "strategy.delete", "strategy", sid)

    # ------------------------------------------------------------ backtests
    def _candles(self, provider, symbol, tf, limit):
        cs, source = self.manager.history(provider, symbol, tf, limit)
        return cs, source

    def backtest(self, uid, *, strategy_id=None, version=None, spec=None, provider, symbol, tf=None, limit=1500,
                 capital=100000.0, fee_pct=0.05, slippage_pct=0.02, save=True, sandbox=False):
        if strategy_id is not None:
            spec, version = self.spec_of(uid, strategy_id, version)
        if spec is None:
            raise ApiError(400, "missing_strategy", "strategy_id or spec required")
        spec, _ = self._check_spec(spec)
        tf = tf or spec["timeframe"]
        if tf != spec["timeframe"]:
            spec = {**spec, "timeframe": tf}
        candles, source = self._candles(provider, symbol, tf, limit)
        try:
            res = core_bt.run_backtest(spec, candles, initial_capital=capital, fee_pct=fee_pct, slippage_pct=slippage_pct)
        except ValueError as e:
            raise ApiError(400, "backtest_failed", str(e))
        res["data_source"] = source
        res["sandbox"] = sandbox
        if save and not sandbox:
            res["run_id"] = self.db.execute(
                "INSERT INTO backtest_runs(user_id,strategy_id,version,provider,symbol,tf,params,stats,result,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uid, strategy_id, version, provider, symbol, tf, json.dumps(res["params"]),
                 json.dumps(res["stats"]), json.dumps({"trades": res["trades"][-300:], "equity": res["equity"]}), time.time()))
        return res

    def backtest_runs(self, uid, limit=50):
        rows = self.db.query("SELECT id,strategy_id,version,provider,symbol,tf,params,stats,created_at FROM backtest_runs "
                             "WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit))
        for r in rows:
            r["params"], r["stats"] = jl(r["params"]), jl(r["stats"])
        return rows

    def backtest_run(self, uid, rid):
        r = self.db.one("SELECT * FROM backtest_runs WHERE id=? AND user_id=?", (rid, uid))
        if not r:
            raise ApiError(404, "not_found", "Run not found")
        r["params"], r["stats"], r["result"] = jl(r["params"]), jl(r["stats"]), jl(r["result"])
        return r

    def compare(self, uid, strategy_ids, provider, symbol, tf, limit=1500, **kw):
        specs = []
        for sid in strategy_ids[:6]:
            spec, v = self.spec_of(uid, sid)
            specs.append({**spec, "name": f"{self._strategy(uid, sid)['name']} v{v}", "timeframe": tf})
        candles, source = self._candles(provider, symbol, tf, limit)
        try:
            out = core_bt.compare_strategies(specs, candles, **kw)
        except ValueError as e:
            raise ApiError(400, "compare_failed", str(e))
        return {"data_source": source, "results": out}

    # ------------------------------------------------------------ paper trading
    def paper_refresh(self, uid, account_id):
        """Feed fresh 1m bars (real provider data) to open positions so SL/targets fill; never fabricates prices."""
        acct = core_paper.get_account(self.db, uid, account_id)
        rows = self.db.query("SELECT * FROM paper_positions WHERE account_id=? AND status='open'", (account_id,))
        fills, errors = [], []
        for r in rows:
            try:
                bars = self.manager.candles(r["provider"], r["symbol"], "1m", limit=300, store=False)
                since = max(int(r.get("last_checked_t") or 0), int(r["opened_at"] * 1000) - 60_000)
                new = [b for b in bars if b["t"] > since]
                last = bars[-1]["c"] if bars else None
                try:
                    last = self.manager.get(r["provider"]).ticker(r["symbol"])["ltp"]
                except Exception:
                    pass
                fills += core_paper.evaluate_position(self.db, r["id"], new, acct["fee_pct"], last)
            except Exception as e:
                errors.append({"position_id": r["id"], "error": str(e)[:200]})
        return {"fills": fills, "errors": errors}

    # ------------------------------------------------------------ alerts
    def create_alert(self, uid, name, kind, symbol, provider, tf, params):
        try:
            core_alerts.validate_alert(kind, params, symbol, tf)
        except ValueError as e:
            raise ApiError(400, "invalid_alert", str(e))
        return self.db.execute("INSERT INTO alerts(user_id,name,kind,provider,symbol,tf,params,enabled,created_at) "
                               "VALUES (?,?,?,?,?,?,?,1,?)",
                               (uid, name, kind, provider, symbol, tf, json.dumps(params), time.time()))

    def list_alerts(self, uid):
        rows = self.db.query("SELECT * FROM alerts WHERE user_id=? ORDER BY id DESC", (uid,))
        for r in rows:
            r["params"] = jl(r["params"], {})
        return rows

    def run_alerts(self):
        """Evaluate every enabled alert once. Called by the scheduler and by the 'run now' endpoint."""
        if not self._alert_lock.acquire(blocking=False):
            return {"triggered": 0, "skipped": "already running"}
        triggered, errors = 0, []
        try:
            for a in self.db.query("SELECT * FROM alerts WHERE enabled=1"):
                params = jl(a["params"], {})
                cooldown = params.get("cooldown_sec", core_alerts.DEFAULT_COOLDOWN)
                if a["last_triggered_at"] and time.time() - a["last_triggered_at"] < cooldown:
                    continue
                try:
                    hit = self._eval_alert(a, params)
                except Exception as e:
                    errors.append({"alert_id": a["id"], "error": str(e)[:200]})
                    continue
                if hit:
                    triggered += 1
                    self._fire(a, hit)
        finally:
            self._alert_lock.release()
        return {"triggered": triggered, "errors": errors}

    def _eval_alert(self, a, params):
        kind, prov, sym, tf = a["kind"], a["provider"], a["symbol"], a["tf"]
        if kind == "price":
            return core_alerts.evaluate(kind, params, price=self.manager.get(prov).ticker(sym)["ltp"])
        if kind in ("stop_loss", "target"):
            row = self.db.one("SELECT * FROM paper_positions WHERE id=?", (params["position_id"],))
            if not row:
                return None
            self.paper_refresh_position(row)
            row = self.db.one("SELECT * FROM paper_positions WHERE id=?", (params["position_id"],))
            return core_alerts.evaluate(kind, params, position=core_paper.position_view(row),
                                        since_ms=int((a["last_triggered_at"] or a["created_at"]) * 1000))
        spec = None
        if kind == "strategy":
            row = self.db.one("SELECT s.active_version v, s.id FROM strategies s WHERE s.id=?", (params["strategy_id"],))
            if not row:
                return None
            spec = jl(self.db.one("SELECT spec FROM strategy_versions WHERE strategy_id=? AND version=?",
                                  (row["id"], row["v"]))["spec"])
            tf = tf or spec["timeframe"]
        candles = self.manager.candles(prov, sym, tf, limit=300, store=False)   # includes forming bar by design
        return core_alerts.evaluate(kind, params, candles=candles, price=candles[-1]["c"] if candles else None,
                                    strategy_spec=spec)

    def paper_refresh_position(self, row):
        acct = self.db.one("SELECT * FROM paper_accounts WHERE id=?", (row["account_id"],))
        bars = self.manager.candles(row["provider"], row["symbol"], "1m", limit=300, store=False)
        since = max(int(row.get("last_checked_t") or 0), int(row["opened_at"] * 1000) - 60_000)
        core_paper.evaluate_position(self.db, row["id"], [b for b in bars if b["t"] > since], acct["fee_pct"],
                                     bars[-1]["c"] if bars else None)

    def _fire(self, a, hit):
        now = time.time()
        self.db.execute("UPDATE alerts SET last_triggered_at=? WHERE id=?", (now, a["id"]))
        msg = f"[{a['name']}] {hit['message']}"
        eid = self.db.execute("INSERT INTO alert_events(user_id,alert_id,message,data,created_at) VALUES (?,?,?,?,?)",
                              (a["user_id"], a["id"], msg, json.dumps(hit.get("data")), now))
        if jl(a["params"], {}).get("once"):
            self.db.execute("UPDATE alerts SET enabled=0 WHERE id=?", (a["id"],))
        if self._notify_server(msg):
            self.db.execute("UPDATE alert_events SET server_notified=1 WHERE id=?", (eid,))

    @staticmethod
    def _notify_server(msg):
        ok = False
        try:
            if settings.webhook_url.startswith("https://"):
                ok = requests.post(settings.webhook_url, json={"text": msg}, timeout=8).ok or ok
            if settings.telegram_bot_token and settings.telegram_chat_id:
                r = requests.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                                  json={"chat_id": settings.telegram_chat_id, "text": msg}, timeout=8)
                ok = r.ok or ok
        except requests.RequestException as e:
            log.warning("server notification failed: %s", e)
        return ok

    # ------------------------------------------------------------ AI learning workflow
    def learning_run(self, uid, sid, provider, symbol, tf=None, limit=3000, train_frac=0.7, capital=100000.0,
                     fee_pct=0.05, slippage_pct=0.02):
        spec, base_v = self.spec_of(uid, sid)
        tf = tf or spec["timeframe"]
        spec = {**spec, "timeframe": tf}
        candles, source = self._candles(provider, symbol, tf, limit)
        try:
            res = core_learning.run_learning(spec, candles, train_frac=train_frac, initial_capital=capital,
                                             fee_pct=fee_pct, slippage_pct=slippage_pct)
        except ValueError as e:
            raise ApiError(400, "learning_failed", str(e))
        ref = {"provider": provider, "symbol": symbol, "tf": tf, "candles": len(candles), "source": source}
        stored = []
        for p in res["proposals"]:
            status = {"VALIDATED_OUT_OF_SAMPLE": "validated", "NOT_VALIDATED": "rejected_by_test",
                      "INSUFFICIENT_DATA": "insufficient_data"}.get(p.get("verdict"), "error")
            cand_v = None
            if status == "validated":      # candidate version is created but NEVER activated automatically
                cand_v = self._add_version(sid, p["spec"], "AI candidate: " + p["note"], "ai", "candidate", base_v)
            pid = self.db.execute(
                "INSERT INTO learning_proposals(user_id,strategy_id,base_version,candidate_version,status,note,verdict,"
                "detail,data_ref,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (uid, sid, base_v, cand_v, status, p.get("note"), p.get("verdict"),
                 json.dumps({k: v for k, v in p.items() if k != "spec"}), json.dumps(ref), time.time(), time.time()))
            stored.append(pid)
            self.db.audit("ai", "learning.proposal", "proposal", pid, {"status": status, "note": p.get("note")})
        res["stored_proposal_ids"] = stored
        res["data_source"] = source
        for p in res["proposals"]:
            p.pop("spec", None)
        return res

    def _proposal(self, uid, pid):
        r = self.db.one("SELECT * FROM learning_proposals WHERE id=? AND user_id=?", (pid, uid))
        if not r:
            raise ApiError(404, "not_found", "Proposal not found")
        return r

    def list_proposals(self, uid, sid=None):
        rows = self.db.query("SELECT * FROM learning_proposals WHERE user_id=?" + (" AND strategy_id=?" if sid else "")
                             + " ORDER BY id DESC LIMIT 200", (uid, sid) if sid else (uid,))
        for r in rows:
            r["detail"], r["data_ref"] = jl(r["detail"], {}), jl(r["data_ref"], {})
        return rows

    def approve(self, uid, pid):
        p = self._proposal(uid, pid)
        if p["verdict"] != "VALIDATED_OUT_OF_SAMPLE" or p["status"] != "validated" or not p["candidate_version"]:
            raise ApiError(409, "not_verified", "Only proposals validated on unseen data can be approved")
        self._activate(p["strategy_id"], p["candidate_version"])
        self.db.execute("UPDATE learning_proposals SET status='applied', updated_at=? WHERE id=?", (time.time(), pid))
        self.db.audit("user", "learning.approve", "proposal", pid, {"strategy": p["strategy_id"], "version": p["candidate_version"]})
        return {"applied_version": p["candidate_version"]}

    def reject(self, uid, pid):
        p = self._proposal(uid, pid)
        if p["status"] == "applied":
            raise ApiError(409, "already_applied", "Use rollback for applied proposals")
        self.db.execute("UPDATE learning_proposals SET status='rejected_by_user', updated_at=? WHERE id=?", (time.time(), pid))
        if p["candidate_version"]:
            self.db.execute("UPDATE strategy_versions SET status='archived' WHERE strategy_id=? AND version=? AND status='candidate'",
                            (p["strategy_id"], p["candidate_version"]))
        self.db.audit("user", "learning.reject", "proposal", pid)

    def rollback(self, uid, pid):
        p = self._proposal(uid, pid)
        if p["status"] != "applied":
            raise ApiError(409, "not_applied", "Only applied proposals can be rolled back")
        out = self.restore_version(uid, p["strategy_id"], p["base_version"])
        self.db.execute("UPDATE learning_proposals SET status='rolled_back', updated_at=? WHERE id=?", (time.time(), pid))
        self.db.audit("user", "learning.rollback", "proposal", pid, out)
        return out

    def attach_paper(self, uid, pid, account_id):
        self._proposal(uid, pid)
        core_paper.get_account(self.db, uid, account_id)
        self.db.execute("UPDATE learning_proposals SET paper_account_id=?, updated_at=? WHERE id=?",
                        (account_id, time.time(), pid))

    def audit_log(self, limit=200):
        rows = self.db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["detail"] = jl(r["detail"])
        return rows

    # ------------------------------------------------------------ backup / restore
    def backup_dir(self):
        d = Path(settings.backup_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def create_backup(self, keep=14):
        stamp = datetime.now().strftime("nexus-%Y%m%d-%H%M%S")
        name, k = f"{stamp}.db", 1
        while (self.backup_dir() / name).exists():          # never overwrite an existing backup
            k += 1
            name = f"{stamp}-{k}.db"
        self.db.backup_to(str(self.backup_dir() / name))
        for old in sorted(self.backup_dir().glob("nexus-*.db"), key=lambda f: f.stat().st_mtime)[:-keep]:
            old.unlink()
        self.db.audit("system", "backup.create", "backup", name)
        return name

    def list_backups(self):
        return [{"name": f.name, "size": f.stat().st_size, "created_at": f.stat().st_mtime}
                for f in sorted(self.backup_dir().glob("nexus-*.db"), reverse=True)]

    def restore_backup(self, name):
        if not re.fullmatch(r"nexus-\d{8}-\d{6}(-\d+)?\.db", name or ""):
            raise ApiError(400, "bad_name", "invalid backup name")
        path = self.backup_dir() / name
        if not path.exists():
            raise ApiError(404, "not_found", "backup not found")
        safety = self.create_backup()           # always snapshot current state before restoring
        try:
            self.db.restore_from(str(path))
        except ValueError as e:
            raise ApiError(400, "bad_backup", str(e))
        self.db.audit("user", "backup.restore", "backup", name, {"safety_snapshot": safety})
        return {"restored": name, "safety_snapshot": safety}
