import pytest

from app.core import paper
from app.security import ApiError

UID = 1


def bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1}


def test_long_target_hit_updates_cash_and_pnl(db):
    acc = paper.create_account(db, UID, "A", 10000, fee_pct=0.0)
    pid = paper.open_position(db, UID, acc, symbol="BTCUSDT", provider="binance", side="LONG", price=100, stop_loss=95,
                              targets=[{"price": 110, "qty_pct": 100}], qty=10)
    assert db.one("SELECT cash FROM paper_accounts WHERE id=?", (acc,))["cash"] == pytest.approx(9000)
    fills = paper.evaluate_position(db, pid, [bar(1, 100, 111, 99, 110)], 0.0)
    assert fills[0]["reason"] == "target_1"
    rep = paper.report(db, UID, acc)
    assert rep["equity"]["cash"] == pytest.approx(10100)
    assert rep["realized_pnl"] == pytest.approx(100)
    assert rep["stats"]["closed_trades"] == 1 and rep["stats"]["win_rate_pct"] == 100.0


def test_short_stop_loss_and_fees(db):
    acc = paper.create_account(db, UID, "A", 10000, fee_pct=0.1)
    pid = paper.open_position(db, UID, acc, symbol="X", provider="p", side="SHORT", price=100, stop_loss=105, qty=10)
    paper.evaluate_position(db, pid, [bar(1, 100, 106, 99, 104)], 0.1)
    rep = paper.report(db, UID, acc)
    # loss 50 + entry fee 1.0 + exit fee 1.05
    assert rep["realized_pnl"] == pytest.approx(-50 - 1.0 - 1.05)
    assert rep["equity"]["cash"] == pytest.approx(10000 - 52.05)
    assert rep["equity"]["equity"] == pytest.approx(rep["equity"]["cash"])


def test_unrealized_mark_to_market(db):
    acc = paper.create_account(db, UID, "A", 10000, fee_pct=0)
    pid = paper.open_position(db, UID, acc, symbol="X", provider="p", side="LONG", price=100, stop_loss=90, qty=10)
    paper.evaluate_position(db, pid, [bar(1, 100, 103, 99, 102)], 0, last_price=102)
    rep = paper.report(db, UID, acc)
    assert rep["unrealized_pnl"] == pytest.approx(20) and rep["equity"]["equity"] == pytest.approx(10020)
    assert rep["open_positions"][0]["status"] == "open"


def test_risk_based_sizing(db):
    acc = paper.create_account(db, UID, "A", 100000, fee_pct=0)
    pid = paper.open_position(db, UID, acc, symbol="X", provider="p", side="LONG", price=100, stop_loss=98, risk_pct=1)
    row = db.one("SELECT qty FROM paper_positions WHERE id=?", (pid,))
    assert row["qty"] == pytest.approx(500)


def test_validation_errors(db):
    acc = paper.create_account(db, UID, "A", 1000, fee_pct=0)
    with pytest.raises(ApiError):   # stop on wrong side
        paper.open_position(db, UID, acc, symbol="X", provider="p", side="LONG", price=100, stop_loss=101, qty=1)
    with pytest.raises(ApiError):   # not enough virtual cash
        paper.open_position(db, UID, acc, symbol="X", provider="p", side="LONG", price=100, stop_loss=90, qty=100)
    with pytest.raises(ApiError):   # wrong-side target
        paper.open_position(db, UID, acc, symbol="X", provider="p", side="LONG", price=100, stop_loss=90, qty=1,
                            targets=[{"price": 95}])
    with pytest.raises(ApiError):
        paper.create_account(db, UID, "bad", -5)


def test_manual_close_and_double_close(db):
    acc = paper.create_account(db, UID, "A", 10000, fee_pct=0)
    pid = paper.open_position(db, UID, acc, symbol="X", provider="p", side="LONG", price=100, stop_loss=90, qty=10)
    paper.close_position(db, UID, pid, 105)
    assert paper.report(db, UID, acc)["realized_pnl"] == pytest.approx(50)
    with pytest.raises(ApiError):
        paper.close_position(db, UID, pid, 105)


def test_users_cannot_see_each_others_accounts(db):
    db.execute("INSERT INTO users(username, pw_hash, role, created_at) VALUES ('other','x','user',0)")
    acc = paper.create_account(db, UID, "A", 1000)
    with pytest.raises(ApiError):
        paper.get_account(db, 2, acc)


def test_partial_targets_cash_conservation(db):
    acc = paper.create_account(db, UID, "A", 10000, fee_pct=0)
    pid = paper.open_position(db, UID, acc, symbol="X", provider="p", side="LONG", price=100, stop_loss=90, qty=10,
                              targets=[{"price": 105, "qty_pct": 50}, {"price": 110, "qty_pct": 50}])
    paper.evaluate_position(db, pid, [bar(1, 100, 106, 100, 105)], 0)
    rep = paper.report(db, UID, acc)
    assert rep["realized_pnl"] == pytest.approx(25)
    assert rep["equity"]["equity"] == pytest.approx(10050)   # 25 realized + 25 unrealized on the remaining 5 units marked at 105
    paper.evaluate_position(db, pid, [bar(2, 105, 111, 104, 110)], 0)
    assert paper.report(db, UID, acc)["equity"]["cash"] == pytest.approx(10075)
