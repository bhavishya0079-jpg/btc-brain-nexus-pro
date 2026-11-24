"""Bar-by-bar backtest engine + the shared simulated-position logic (also used by paper trading).

Execution assumptions (all explicit, none favourable by accident):
  * Signals are evaluated on a bar's CLOSE; the order fills at the NEXT bar's OPEN (no look-ahead).
  * Slippage is applied against the trader on market fills (entries, stop-loss, rule exits).
    Target (limit) fills get no slippage but also no price improvement except gap-through.
  * If stop-loss and a target are both touched inside one bar, the stop-loss is assumed to hit first.
  * Fees are charged per side as % of notional. Position size = risk% of equity / stop distance,
    capped by max_position_pct of equity (no leverage). One position at a time.
Backtest results describe the past; they are not a forecast.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from . import indicators as ind
from .strategy import Series, compute_series, eval_conditions, normalize_spec, required_warmup, validate_spec

MAX_CANDLES = 20000


class SimPosition:
    def __init__(self, side: int, entry: float, qty: float, sl: Optional[float], targets: List[Dict[str, float]],
                 opened_t: int, entry_fee: float = 0.0, bar_index: int = 0):
        self.side = side
        self.entry = entry
        self.qty0 = qty
        self.remaining = qty
        self.sl = sl
        self.targets = [dict(t, hit=False) for t in targets]  # each: price, qty
        self.opened_t = opened_t
        self.bar_index = bar_index
        self.fees = entry_fee
        self.realized_gross = 0.0
        self.exits: List[Dict[str, Any]] = []
        self.initial_risk = abs(entry - sl) * qty if sl is not None else None

    # ----------------------------------------------------------------- fills
    def _fill(self, price: float, qty: float, reason: str, t: int, fee_pct: float) -> Dict[str, Any]:
        qty = min(qty, self.remaining)
        pnl = (price - self.entry) * self.side * qty
        fee = qty * price * fee_pct / 100.0
        self.remaining -= qty
        self.realized_gross += pnl
        self.fees += fee
        fill = {"t": t, "price": price, "qty": qty, "reason": reason, "pnl": pnl, "fee": fee}
        self.exits.append(fill)
        return fill

    def process_bar(self, t: int, o: float, h: float, l: float, c: float, fee_pct: float, slip_pct: float) -> List[Dict[str, Any]]:
        fills: List[Dict[str, Any]] = []
        s = self.side
        if self.remaining <= 1e-12:
            return fills
        if self.sl is not None:
            hit = l <= self.sl if s == 1 else h >= self.sl
            if hit:
                raw = min(o, self.sl) if s == 1 else max(o, self.sl)  # gap-through fills at the open
                price = raw * (1 - s * slip_pct / 100.0)
                fills.append(self._fill(price, self.remaining, "stop_loss", t, fee_pct))
                return fills
        for tg in self.targets:
            if tg["hit"] or self.remaining <= 1e-12:
                continue
            reached = h >= tg["price"] if s == 1 else l <= tg["price"]
            if reached:
                price = max(o, tg["price"]) if s == 1 else min(o, tg["price"])
                tg["hit"] = True
                fills.append(self._fill(price, tg["qty"], f"target_{self.targets.index(tg) + 1}", t, fee_pct))
        return fills

    def close(self, t: int, price: float, reason: str, fee_pct: float, slip_pct: float, apply_slip: bool = True) -> Dict[str, Any]:
        if apply_slip:
            price = price * (1 - self.side * slip_pct / 100.0)
        return self._fill(price, self.remaining, reason, t, fee_pct)

    @property
    def is_open(self) -> bool:
        return self.remaining > 1e-12

    def unrealized(self, price: float) -> float:
        return (price - self.entry) * self.side * self.remaining

    @property
    def net_realized(self) -> float:
        return self.realized_gross - self.fees


def build_targets(side: int, entry: float, sl: float, spec_targets: List[Dict[str, float]], qty: float) -> List[Dict[str, float]]:
    risk = abs(entry - sl)
    out = []
    for tg in spec_targets:
        out.append({"price": entry + side * risk * tg["rr"], "qty": qty * tg.get("qty_pct", 100.0) / 100.0, "rr": tg["rr"]})
    return out


def size_position(equity: float, entry: float, sl: float, risk_pct: float, max_pos_pct: float) -> float:
    dist = abs(entry - sl)
    if dist <= 0 or entry <= 0 or equity <= 0:
        return 0.0
    qty = equity * risk_pct / 100.0 / dist
    cap = equity * max_pos_pct / 100.0 / entry
    return max(0.0, min(qty, cap))


def _trade_record(pos: SimPosition, closed_t: int, closed_idx: int, symbol_t: np.ndarray) -> Dict[str, Any]:
    qty_total = sum(x["qty"] for x in pos.exits) or 1e-12
    avg_exit = sum(x["price"] * x["qty"] for x in pos.exits) / qty_total
    net = pos.net_realized
    notional = pos.entry * pos.qty0
    return {
        "side": "LONG" if pos.side == 1 else "SHORT", "entry_t": int(pos.opened_t), "exit_t": int(closed_t),
        "entry": pos.entry, "exit": avg_exit, "qty": pos.qty0, "pnl": net, "pnl_pct": net / notional * 100.0 if notional else 0.0,
        "fees": pos.fees, "bars": closed_idx - pos.bar_index,
        "r_multiple": (net / pos.initial_risk) if pos.initial_risk else None,
        "exit_reason": pos.exits[-1]["reason"] if pos.exits else None,
        "stop_loss": pos.sl, "exits": pos.exits,
    }


def compute_stats(trades: List[Dict[str, Any]], equity: np.ndarray, initial: float, bars_in_market: int,
                  candles_close: np.ndarray) -> Dict[str, Any]:
    n = len(trades)
    pnls = np.array([t["pnl"] for t in trades]) if n else np.array([])
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    peak = np.maximum.accumulate(equity) if len(equity) else np.array([initial])
    dd = (peak - equity) / peak * 100.0 if len(equity) else np.array([0.0])
    dd_abs = peak - equity if len(equity) else np.array([0.0])
    streak = longest = 0
    for p in pnls:
        streak = streak + 1 if p < 0 else 0
        longest = max(longest, streak)
    final = float(equity[-1]) if len(equity) else initial
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    buy_hold = None
    if len(candles_close) > 1:
        buy_hold = float((candles_close[-1] / candles_close[0] - 1) * 100.0)
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    return {
        "trades": n, "wins": int(len(wins)), "losses": int(len(losses)),
        "win_rate_pct": float(len(wins) / n * 100.0) if n else None,
        "net_profit": final - initial, "return_pct": (final / initial - 1) * 100.0,
        "final_equity": final, "initial_capital": initial,
        "gross_profit": gross_win, "gross_loss": gross_loss,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "profit_factor_note": None if gross_loss > 0 else ("no losing trades" if n else "no trades"),
        "max_drawdown_pct": float(dd.max()) if len(dd) else 0.0, "max_drawdown_abs": float(dd_abs.max()) if len(dd_abs) else 0.0,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "risk_reward_realized": (avg_win / abs(avg_loss)) if avg_loss < 0 and avg_win > 0 else None,
        "expectancy": float(pnls.mean()) if n else 0.0,
        "avg_r_multiple": float(np.mean(rs)) if rs else None,
        "longest_losing_streak": int(longest),
        "fees_paid": float(sum(t["fees"] for t in trades)),
        "exposure_pct": bars_in_market / max(1, len(equity)) * 100.0,
        "buy_and_hold_return_pct": buy_hold,
        "disclaimer": "Backtest results describe past data only and do not guarantee future performance.",
    }


def run_backtest(spec_in: Dict[str, Any], candles: List[Dict[str, float]], *, initial_capital: float = 100000.0,
                 fee_pct: float = 0.05, slippage_pct: float = 0.02) -> Dict[str, Any]:
    errors, _ = validate_spec(spec_in)
    if errors:
        raise ValueError("Invalid strategy: " + "; ".join(errors))
    if len(candles) > MAX_CANDLES:
        candles = candles[-MAX_CANDLES:]
    spec = normalize_spec(spec_in)
    warmup = required_warmup(spec)
    if len(candles) < warmup + 10:
        raise ValueError(f"Not enough candles ({len(candles)}); need at least {warmup + 10} for this strategy")
    s = Series(candles)
    series = compute_series(spec, s)
    sl_cfg = spec["stop_loss"]
    atr_sl = ind.atr(s.high, s.low, s.close, int(sl_cfg.get("period", 14))) if sl_cfg["type"] == "atr" else None
    risk = spec["risk"]
    side_mode = spec["side"]
    cash = float(initial_capital)
    pos: Optional[SimPosition] = None
    pending_entry: Optional[int] = None  # +1 / -1 decided at previous close
    pending_exit = False
    trades: List[Dict[str, Any]] = []
    equity = np.full(s.n, np.nan)
    bars_in_market = 0
    entry_sl_atr: Optional[float] = None

    for i in range(s.n):
        o, h, l, c, t = s.open[i], s.high[i], s.low[i], s.close[i], int(s.t[i])
        # 1) execute pending exit at this open
        if pos and pending_exit:
            pos.close(t, o, "rule_exit", fee_pct, slippage_pct)
            cash += pos.net_realized
            trades.append(_trade_record(pos, t, i, s.t))
            pos, pending_exit = None, False
        # 2) execute pending entry at this open
        if pos is None and pending_entry is not None:
            side = pending_entry
            entry = o * (1 + side * slippage_pct / 100.0)
            if sl_cfg["type"] == "atr":
                dist = entry_sl_atr * sl_cfg["mult"] if entry_sl_atr else None
            else:
                dist = entry * sl_cfg["value"] / 100.0
            if dist and dist > 0:
                sl_price = entry - side * dist
                qty = size_position(cash, entry, sl_price, risk["risk_per_trade_pct"], risk["max_position_pct"])
                if qty > 0 and sl_price > 0:
                    fee = qty * entry * fee_pct / 100.0
                    targets = build_targets(side, entry, sl_price, spec["targets"], qty)
                    pos = SimPosition(side, entry, qty, sl_price, targets, t, fee, bar_index=i)
            pending_entry = None
        # 3) intrabar stop / target handling
        if pos:
            pos.process_bar(t, o, h, l, c, fee_pct, slippage_pct)
            bars_in_market += 1
            if not pos.is_open:
                cash += pos.net_realized
                trades.append(_trade_record(pos, t, i, s.t))
                pos = None
        # 4) rule evaluation on this bar's close (acts on the next open)
        if pos:
            conds = spec["exit_long"] if pos.side == 1 else spec["exit_short"]
            if eval_conditions(conds, series, i):
                pending_exit = True
            mb = risk.get("max_bars_in_trade")
            if mb and i - pos.bar_index >= mb:
                pending_exit = True
        elif i >= warmup and i < s.n - 1:
            want_long = side_mode in ("long", "both") and eval_conditions(spec["entry_long"], series, i)
            want_short = side_mode in ("short", "both") and eval_conditions(spec["entry_short"], series, i)
            if want_long != want_short:  # conflicting signals => stand aside
                pending_entry = 1 if want_long else -1
                entry_sl_atr = float(atr_sl[i]) if atr_sl is not None and not np.isnan(atr_sl[i]) else None
        # equity = cash from closed trades + net result of the open position so far
        open_result = (pos.realized_gross - pos.fees + pos.unrealized(c)) if pos else 0.0
        equity[i] = cash + open_result
    if pos and pos.is_open:
        pos.close(int(s.t[-1]), float(s.close[-1]), "end_of_data", fee_pct, slippage_pct, apply_slip=False)
        cash += pos.net_realized
        trades.append(_trade_record(pos, int(s.t[-1]), s.n - 1, s.t))
        equity[-1] = cash
    stats = compute_stats(trades, equity, initial_capital, bars_in_market, s.close)
    curve = [{"t": int(s.t[k]), "equity": float(equity[k])} for k in range(0, s.n, max(1, s.n // 1500))]
    if curve[-1]["t"] != int(s.t[-1]):
        curve.append({"t": int(s.t[-1]), "equity": float(equity[-1])})
    return {
        "stats": stats, "trades": trades, "equity": curve,
        "params": {"initial_capital": initial_capital, "fee_pct": fee_pct, "slippage_pct": slippage_pct,
                   "candles": s.n, "from_t": int(s.t[0]), "to_t": int(s.t[-1]), "timeframe": spec["timeframe"]},
    }


def compare_strategies(specs: List[Dict[str, Any]], candles: List[Dict[str, float]], **kwargs: Any) -> List[Dict[str, Any]]:
    rows = []
    for spec in specs:
        try:
            res = run_backtest(spec, candles, **kwargs)
            st = res["stats"]
            rows.append({"name": spec.get("name"), "ok": True, "trades": st["trades"], "net_profit": st["net_profit"],
                         "return_pct": st["return_pct"], "win_rate_pct": st["win_rate_pct"],
                         "profit_factor": st["profit_factor"], "max_drawdown_pct": st["max_drawdown_pct"],
                         "avg_r_multiple": st["avg_r_multiple"]})
        except ValueError as exc:
            rows.append({"name": spec.get("name"), "ok": False, "error": str(exc)})
    return rows
