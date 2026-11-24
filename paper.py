"""Paper trading: virtual capital, virtual positions. No real money, no broker, no orders.

Cash model (no leverage): opening a position reserves its notional from virtual cash; closing
(fully or partially) returns the reserved notional plus P&L minus fees.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .backtest import SimPosition, build_targets, size_position
from ..security import ApiError


def _now_ms() -> int:
    return int(time.time() * 1000)


def create_account(db, user_id: int, name: str, capital: float, fee_pct: float = 0.05,
                   strategy_id: Optional[int] = None, strategy_version: Optional[int] = None,
                   config: Optional[Dict[str, Any]] = None) -> int:
    if capital <= 0:
        raise ApiError(400, "bad_capital", "Capital must be positive")
    return db.execute(
        "INSERT INTO paper_accounts(user_id,name,initial_capital,cash,fee_pct,strategy_id,strategy_version,created_at,config) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, name, capital, capital, fee_pct, strategy_id, strategy_version, time.time(), json.dumps(config or {})))


def get_account(db, user_id: int, account_id: int):
    row = db.one("SELECT * FROM paper_accounts WHERE id=? AND user_id=?", (account_id, user_id))
    if not row:
        raise ApiError(404, "not_found", "Paper account not found")
    return row


def _sim_from_row(row) -> SimPosition:
    targets = json.loads(row["targets"])
    sim = SimPosition(row["side"], row["entry_price"], row["qty"], row["stop_loss"], [], row["opened_at"])
    sim.targets = [dict(t) for t in targets]
    sim.remaining = row["remaining"]
    sim.fees = row["fees"]
    sim.exits = json.loads(row["exits"])
    sim.realized_gross = sum(x["pnl"] for x in sim.exits)
    return sim


def open_position(db, user_id: int, account_id: int, *, symbol: str, provider: str, side: str, price: float,
                  stop_loss: float, targets: Optional[List[Dict[str, float]]] = None, qty: Optional[float] = None,
                  risk_pct: float = 1.0, note: str = "", source: str = "manual", slippage_pct: float = 0.0) -> int:
    acct = get_account(db, user_id, account_id)
    if side not in ("LONG", "SHORT"):
        raise ApiError(400, "bad_side", "side must be LONG or SHORT")
    s = 1 if side == "LONG" else -1
    entry = price * (1 + s * slippage_pct / 100.0)
    if stop_loss <= 0 or (s == 1 and stop_loss >= entry) or (s == -1 and stop_loss <= entry):
        raise ApiError(400, "bad_stop", "Stop loss must be below entry for LONG and above entry for SHORT")
    equity = account_equity(db, acct)["equity"]
    if qty is None:
        qty = size_position(equity, entry, stop_loss, risk_pct, 100.0)
    if qty is None or qty <= 0:
        raise ApiError(400, "bad_qty", "Quantity must be positive")
    notional = qty * entry
    fee = notional * acct["fee_pct"] / 100.0
    if notional + fee > acct["cash"] + 1e-9:
        raise ApiError(400, "insufficient_cash", f"Not enough virtual cash (need {notional + fee:.2f}, have {acct['cash']:.2f})")
    tgs = []
    for i, tg in enumerate(targets or []):
        p = tg["price"]
        if (s == 1 and p <= entry) or (s == -1 and p >= entry):
            raise ApiError(400, "bad_target", f"Target {i + 1} is on the wrong side of entry")
        tgs.append({"price": float(p), "qty": qty * float(tg.get("qty_pct", 100.0 / len(targets))) / 100.0, "hit": False})
    now = time.time()
    pid = db.execute(
        "INSERT INTO paper_positions(account_id,provider,symbol,side,qty,remaining,entry_price,stop_loss,targets,opened_at,status,"
        "last_price,realized_pnl,fees,exits,note,last_checked_t,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, provider, symbol, s, qty, qty, entry, stop_loss, json.dumps(tgs), now, "open", entry, 0.0, fee, "[]",
         note, _now_ms(), source))
    db.execute("UPDATE paper_accounts SET cash = cash - ? WHERE id=?", (notional + fee, account_id))
    return pid


def _persist(db, acct_id: int, row, sim: SimPosition, new_fills: List[Dict[str, Any]], last_price: float) -> None:
    returned = sum(f["qty"] * row["entry_price"] + f["pnl"] - f["fee"] for f in new_fills)
    status = "open" if sim.is_open else "closed"
    db.execute(
        "UPDATE paper_positions SET remaining=?, targets=?, status=?, last_price=?, realized_pnl=?, fees=?, exits=?, closed_at=? WHERE id=?",
        (sim.remaining, json.dumps([{k: v for k, v in t.items() if k in ("price", "qty", "hit")} for t in sim.targets]), status,
         last_price, sim.realized_gross, sim.fees, json.dumps(sim.exits), None if sim.is_open else time.time(), row["id"]))
    if returned:
        db.execute("UPDATE paper_accounts SET cash = cash + ? WHERE id=?", (returned, acct_id))


def evaluate_position(db, position_id: int, bars: List[Dict[str, float]], fee_pct: float,
                      last_price: Optional[float] = None) -> List[Dict[str, Any]]:
    """Feed bars (oldest first) to an open position; returns the fills that happened."""
    row = db.one("SELECT * FROM paper_positions WHERE id=?", (position_id,))
    if not row or row["status"] != "open":
        return []
    sim = _sim_from_row(row)
    fills: List[Dict[str, Any]] = []
    for b in bars:
        got = sim.process_bar(int(b["t"]), b["o"], b["h"], b["l"], b["c"], fee_pct, 0.0)
        fills += got
        if not sim.is_open:
            break
    price = last_price if last_price is not None else (bars[-1]["c"] if bars else row["last_price"])
    # fees on partial fills were added to sim.fees inside _fill; persist everything
    _persist(db, row["account_id"], row, sim, fills, price)
    if bars:
        db.execute("UPDATE paper_positions SET last_checked_t=? WHERE id=?", (int(bars[-1]["t"]), position_id))
    return fills


def close_position(db, user_id: int, position_id: int, price: float, reason: str = "manual") -> Dict[str, Any]:
    row = db.one("SELECT p.*, a.user_id, a.fee_pct FROM paper_positions p JOIN paper_accounts a ON a.id=p.account_id WHERE p.id=?", (position_id,))
    if not row or row["user_id"] != user_id:
        raise ApiError(404, "not_found", "Position not found")
    if row["status"] != "open":
        raise ApiError(400, "already_closed", "Position is already closed")
    sim = _sim_from_row(row)
    fill = sim.close(_now_ms(), price, reason, row["fee_pct"], 0.0, apply_slip=False)
    _persist(db, row["account_id"], row, sim, [fill], price)
    return fill


def account_equity(db, acct) -> Dict[str, float]:
    rows = db.query("SELECT * FROM paper_positions WHERE account_id=? AND status='open'", (acct["id"],))
    reserved = sum(r["remaining"] * r["entry_price"] for r in rows)
    unrealized = sum((r["last_price"] - r["entry_price"]) * r["side"] * r["remaining"] for r in rows if r["last_price"])
    return {"cash": acct["cash"], "reserved": reserved, "unrealized": unrealized, "equity": acct["cash"] + reserved + unrealized}


def position_view(r) -> Dict[str, Any]:
    exits = json.loads(r["exits"])
    gross = sum(x["pnl"] for x in exits)
    unreal = (r["last_price"] - r["entry_price"]) * r["side"] * r["remaining"] if r["status"] == "open" and r["last_price"] else 0.0
    return {
        "id": r["id"], "symbol": r["symbol"], "provider": r["provider"], "side": "LONG" if r["side"] == 1 else "SHORT",
        "qty": r["qty"], "remaining": r["remaining"], "entry": r["entry_price"], "stop_loss": r["stop_loss"],
        "targets": json.loads(r["targets"]), "status": r["status"], "last_price": r["last_price"],
        "unrealized_pnl": unreal, "realized_pnl": gross - r["fees"], "fees": r["fees"],
        "opened_at": r["opened_at"], "closed_at": r["closed_at"], "exits": exits, "note": r["note"], "source": r["source"],
        "net_pnl": gross - r["fees"] + unreal,
    }


def report(db, user_id: int, account_id: int) -> Dict[str, Any]:
    acct = get_account(db, user_id, account_id)
    rows = db.query("SELECT * FROM paper_positions WHERE account_id=? ORDER BY id", (account_id,))
    views = [position_view(r) for r in rows]
    closed = [v for v in views if v["status"] == "closed"]
    openp = [v for v in views if v["status"] == "open"]
    eq = account_equity(db, acct)
    wins = [v for v in closed if v["net_pnl"] > 0]
    losses = [v for v in closed if v["net_pnl"] < 0]
    gp, gl = sum(v["net_pnl"] for v in wins), -sum(v["net_pnl"] for v in losses)
    realized_total = sum(v["realized_pnl"] for v in views)
    return {
        "account": {"id": acct["id"], "name": acct["name"], "initial_capital": acct["initial_capital"], "fee_pct": acct["fee_pct"],
                    "strategy_id": acct["strategy_id"], "strategy_version": acct["strategy_version"],
                    "config": json.loads(acct["config"])},
        "equity": eq, "return_pct": (eq["equity"] / acct["initial_capital"] - 1) * 100.0,
        "realized_pnl": realized_total, "unrealized_pnl": eq["unrealized"],
        "open_positions": openp, "closed_positions": closed,
        "stats": {"closed_trades": len(closed), "wins": len(wins), "losses": len(losses),
                  "win_rate_pct": len(wins) / len(closed) * 100.0 if closed else None,
                  "profit_factor": gp / gl if gl > 0 else None, "gross_profit": gp, "gross_loss": gl,
                  "fees_paid": sum(v["fees"] for v in views)},
        "disclaimer": "Virtual money only. Paper results are not a guarantee of live performance.",
    }
