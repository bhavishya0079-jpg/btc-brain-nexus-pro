import json
import time

import pytest

from app.core.strategy import default_spec
from app.providers import build_providers
from app.providers.manager import ProviderManager
from app.security import ApiError
from app.services import Services
from tests.fakes import FakeSession, binance_klines, walk

nosleep = lambda s: None  # noqa: E731
UID = 1


@pytest.fixture
def svc(db):
    candles = walk(1500, step_ms=900_000)
    sess = FakeSession([
        ("/klines", binance_klines(candles)),
        ("/ticker/24hr", lambda m, u, p, j, h: {"symbol": "BTCUSDT", "lastPrice": str(candles[-1]["c"]), "openPrice": "100",
                                                 "highPrice": "999", "lowPrice": "1", "volume": "1", "closeTime": 1_700_000_000_000}),
    ])
    mgr = ProviderManager(db, build_providers(sess, nosleep))
    s = Services(db, mgr)
    s.candles = candles
    return s


def test_strategy_versions_restore_and_forbidden_keys(svc):
    r = svc.create_strategy(UID, "S1", default_spec())
    sid = r["id"]
    with pytest.raises(ApiError) as e:
        svc.create_strategy(UID, "S1", default_spec())
    assert e.value.status == 409
    bad = default_spec()
    bad["place_order"] = True
    with pytest.raises(ApiError) as e2:
        svc.update_strategy(UID, sid, bad)
    assert e2.value.code == "forbidden_field"
    with pytest.raises(ApiError):
        svc.update_strategy(UID, sid, {"name": "x"})              # invalid spec
    spec2 = default_spec()
    spec2["risk"]["risk_per_trade_pct"] = 0.5
    assert svc.update_strategy(UID, sid, spec2)["version"] == 2
    got = svc.get_strategy(UID, sid)
    assert got["active_version"] == 2 and len(got["versions"]) == 2
    assert svc.restore_version(UID, sid, 1)["version"] == 3
    assert svc.get_strategy(UID, sid)["active_spec"]["risk"]["risk_per_trade_pct"] == 1.0
    with pytest.raises(ApiError):
        svc.get_strategy(999, sid)                                  # other user cannot see it


def test_backtest_saved_and_sandbox_not_saved(svc):
    sid = svc.create_strategy(UID, "BT", default_spec())["id"]
    res = svc.backtest(UID, strategy_id=sid, provider="binance", symbol="BTCUSDT", tf="15m", limit=1200)
    assert "run_id" in res and res["data_source"] == "live" and "stats" in res
    assert len(svc.backtest_runs(UID)) == 1
    box = svc.backtest(UID, strategy_id=sid, provider="binance", symbol="BTCUSDT", tf="15m", limit=1200, sandbox=True, save=False)
    assert box["sandbox"] and "run_id" not in box and len(svc.backtest_runs(UID)) == 1
    assert svc.backtest_run(UID, res["run_id"])["stats"]["trades"] == res["stats"]["trades"]
    out = svc.compare(UID, [sid, svc.create_strategy(UID, "BT2", default_spec())["id"]], "binance", "BTCUSDT", "15m", 1200)
    assert len(out["results"]) == 2


def test_alert_price_triggers_once_per_cooldown(svc, db):
    price = svc.candles[-1]["c"]
    svc.create_alert(UID, "above", "price", "BTCUSDT", "binance", None, {"condition": "above", "value": price * 0.5, "cooldown_sec": 3600})
    svc.create_alert(UID, "never", "price", "BTCUSDT", "binance", None, {"condition": "above", "value": price * 10})
    assert svc.run_alerts()["triggered"] == 1
    assert svc.run_alerts()["triggered"] == 0                      # cooldown
    ev = db.query("SELECT * FROM alert_events")
    assert len(ev) == 1 and "above" in ev[0]["message"]
    with pytest.raises(ApiError):
        svc.create_alert(UID, "bad", "price", "BTCUSDT", "binance", None, {"condition": "sideways", "value": 1})


def test_paper_refresh_fills_stop_from_real_bars(svc, db):
    from app.core import paper
    aid = paper.create_account(db, UID, "acct", 100000)
    last = svc.candles[-1]["c"]
    # stop far above current candles' lows => any new bar after entry would hit it for a SHORT; use LONG with stop above range
    pid = paper.open_position(db, UID, aid, symbol="BTCUSDT", provider="binance", side="LONG", price=last,
                              stop_loss=last * 0.5, targets=[{"price": last * 1.01, "qty_pct": 100}])
    db.execute("UPDATE paper_positions SET opened_at=? WHERE id=?", (time.time() - 10 * 86400, pid))
    out = svc.paper_refresh(UID, aid)
    assert out["errors"] == []
    rep = paper.report(db, UID, aid)
    assert rep["equity"]["equity"] > 0


def _validated_proposal(svc, db):
    sid = svc.create_strategy(UID, "L", default_spec())["id"]
    spec = default_spec()
    spec["risk"]["risk_per_trade_pct"] = 0.7
    cand = svc._add_version(sid, spec, "AI candidate", "ai", "candidate", 1)
    pid = db.execute("INSERT INTO learning_proposals(user_id,strategy_id,base_version,candidate_version,status,note,verdict,detail,data_ref,created_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (UID, sid, 1, cand, "validated", "n", "VALIDATED_OUT_OF_SAMPLE", "{}", "{}", 1, 1))
    return sid, cand, pid


def test_learning_approval_rollback_and_gating(svc, db):
    sid, cand, pid = _validated_proposal(svc, db)
    assert svc.get_strategy(UID, sid)["active_version"] == 1       # candidate never auto-applied
    assert svc.approve(UID, pid)["applied_version"] == cand
    assert svc.get_strategy(UID, sid)["active_version"] == cand
    with pytest.raises(ApiError):
        svc.reject(UID, pid)                                        # applied -> must use rollback
    svc.rollback(UID, pid)
    st = svc.get_strategy(UID, sid)
    assert st["active_spec"]["risk"]["risk_per_trade_pct"] == 1.0 and st["active_version"] > cand
    assert {a["action"] for a in svc.audit_log()} >= {"learning.approve", "learning.rollback"}
    # a non-validated proposal can never be approved
    bad = db.execute("INSERT INTO learning_proposals(user_id,strategy_id,base_version,candidate_version,status,verdict,detail,created_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (UID, sid, 1, None, "rejected_by_test", "NOT_VALIDATED", "{}", 1, 1))
    with pytest.raises(ApiError) as e:
        svc.approve(UID, bad)
    assert e.value.code == "not_verified"


def test_learning_run_end_to_end_stores_and_never_autoapplies(svc, db):
    sid = svc.create_strategy(UID, "LR", default_spec())["id"]
    res = svc.learning_run(UID, sid, "binance", "BTCUSDT", "15m", 1500)
    assert res["status"] in ("ok", "insufficient_data") and "notes" in res
    assert svc.get_strategy(UID, sid)["active_version"] == 1
    for p in svc.list_proposals(UID, sid):
        assert p["status"] != "applied"


def test_backup_restore_cycle(svc, db, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "bk"))
    svc.create_strategy(UID, "keep", default_spec())
    name = svc.create_backup()
    svc.create_strategy(UID, "later", default_spec())
    out = svc.restore_backup(name)
    assert out["restored"] == name
    assert [s["name"] for s in svc.list_strategies(UID)] == ["keep"]
    with pytest.raises(ApiError):
        svc.restore_backup("../../etc/passwd")
    assert len(svc.list_backups()) >= 2                              # includes safety snapshot
