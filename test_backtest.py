import copy

import numpy as np
import pytest

from app.core.backtest import SimPosition, compare_strategies, run_backtest, size_position
from app.core.strategy import default_spec, validate_spec
from tests.helpers import candles_from_closes, random_walk, trending, zigzag


def simple_spec(**over):
    spec = {
        "name": "t", "timeframe": "15m", "side": "long",
        "indicators": [{"id": "fast", "type": "ema", "params": {"period": 5}}, {"id": "slow", "type": "ema", "params": {"period": 15}}],
        "entry_long": [{"left": "fast", "op": "crosses_above", "right": "slow"}],
        "stop_loss": {"type": "percent", "value": 2}, "targets": [{"rr": 2, "qty_pct": 100}],
        "risk": {"risk_per_trade_pct": 1, "max_position_pct": 100},
    }
    spec.update(over)
    return spec


# ------------------------------------------------------------------ SimPosition
def test_long_stop_loss_fill_and_pnl():
    p = SimPosition(1, 100.0, 10, 95.0, [], 0, entry_fee=0.0)
    fills = p.process_bar(1, 100, 101, 94, 96, fee_pct=0.0, slip_pct=0.0)
    assert fills[0]["reason"] == "stop_loss" and fills[0]["price"] == 95.0
    assert p.net_realized == pytest.approx(-50.0)
    assert not p.is_open


def test_gap_through_stop_fills_at_open():
    p = SimPosition(1, 100.0, 10, 95.0, [], 0)
    fills = p.process_bar(1, 90, 92, 88, 91, 0, 0)
    assert fills[0]["price"] == 90  # worse than the stop: gap risk is honoured


def test_stop_beats_target_in_same_bar():
    p = SimPosition(1, 100.0, 10, 95.0, [{"price": 110.0, "qty": 10}], 0)
    fills = p.process_bar(1, 100, 112, 94, 100, 0, 0)
    assert [f["reason"] for f in fills] == ["stop_loss"]


def test_partial_targets_and_remainder():
    p = SimPosition(1, 100.0, 10, 95.0, [{"price": 105.0, "qty": 5}, {"price": 110.0, "qty": 5}], 0)
    f1 = p.process_bar(1, 100, 106, 99, 104, 0, 0)
    assert len(f1) == 1 and p.remaining == 5
    f2 = p.process_bar(2, 104, 111, 103, 110, 0, 0)
    assert f2[0]["reason"] == "target_2" and not p.is_open
    assert p.net_realized == pytest.approx(5 * 5 + 5 * 10)


def test_short_position_mirrors():
    p = SimPosition(-1, 100.0, 10, 105.0, [{"price": 90.0, "qty": 10}], 0)
    fills = p.process_bar(1, 100, 101, 89, 91, 0, 0)
    assert fills[0]["reason"] == "target_1" and p.net_realized == pytest.approx(100.0)


def test_slippage_and_fees_reduce_pnl():
    p = SimPosition(1, 100.0, 10, 95.0, [], 0, entry_fee=1.0)
    p.process_bar(1, 100, 101, 94, 96, fee_pct=0.1, slip_pct=0.5)
    assert p.exits[0]["price"] == pytest.approx(95.0 * 0.995)
    assert p.net_realized < -50.0


def test_position_sizing_risk_and_cap():
    assert size_position(100000, 100, 98, 1, 100) == pytest.approx(500)   # risk 1000 / 2 dist
    assert size_position(100000, 100, 99.99, 1, 50) == pytest.approx(500)  # capped at 50% notional
    assert size_position(100000, 100, 100, 1, 100) == 0


# --------------------------------------------------------------------- engine
def test_validate_default_spec():
    errors, _ = validate_spec(default_spec())
    assert errors == []


def test_no_lookahead_entry_uses_next_open():
    c = zigzag(10)
    res = run_backtest(simple_spec(), c)
    times = {x["t"]: x for x in c}
    for tr in res["trades"]:
        bar = times[tr["entry_t"]]
        # entry price = open * (1 + slippage); never the close of the signal bar
        assert tr["entry"] == pytest.approx(bar["o"] * 1.0002)


def test_equity_conservation():
    c = random_walk(800, seed=5, vol=0.012)
    res = run_backtest(default_spec(), c, initial_capital=50000)
    st = res["stats"]
    assert st["final_equity"] == pytest.approx(50000 + sum(t["pnl"] for t in res["trades"]), rel=1e-9)
    assert st["trades"] == len(res["trades"])
    assert res["equity"][-1]["equity"] == pytest.approx(st["final_equity"])


def test_uptrend_long_strategy_profits_and_zero_costs_vs_costs():
    closes = list(np.concatenate([np.linspace(100, 80, 40), np.linspace(80, 160, 200)]))
    c = candles_from_closes(closes, spread=0.001)
    spec = simple_spec(stop_loss={"type": "percent", "value": 5}, targets=[{"rr": 6, "qty_pct": 100}])
    free = run_backtest(spec, c, fee_pct=0, slippage_pct=0)
    costly = run_backtest(spec, c, fee_pct=0.2, slippage_pct=0.2)
    assert free["stats"]["net_profit"] > 0
    assert costly["stats"]["net_profit"] < free["stats"]["net_profit"]
    assert costly["stats"]["fees_paid"] > 0


def test_stats_drawdown_and_profit_factor_consistency():
    res = run_backtest(default_spec(), random_walk(1200, seed=8, vol=0.015))
    st = res["stats"]
    assert 0 <= st["max_drawdown_pct"] <= 100
    wins = sum(t["pnl"] for t in res["trades"] if t["pnl"] > 0)
    losses = -sum(t["pnl"] for t in res["trades"] if t["pnl"] < 0)
    if losses > 0:
        assert st["profit_factor"] == pytest.approx(wins / losses)
    if st["trades"]:
        assert st["win_rate_pct"] == pytest.approx(100.0 * sum(1 for t in res["trades"] if t["pnl"] > 0) / st["trades"])


def test_short_side_and_both():
    closes = list(np.linspace(160, 80, 250))
    c = candles_from_closes(closes, spread=0.001)
    spec = simple_spec(side="short", entry_short=[{"left": "fast", "op": "crosses_below", "right": "slow"}], entry_long=[],
                       stop_loss={"type": "percent", "value": 5}, targets=[{"rr": 4, "qty_pct": 100}])
    res = run_backtest(spec, candles_from_closes(list(np.concatenate([np.linspace(100, 160, 60), np.linspace(160, 80, 200)]))))
    assert any(t["side"] == "SHORT" for t in res["trades"])


def test_atr_stop_and_rule_exit():
    spec = simple_spec(stop_loss={"type": "atr", "period": 14, "mult": 2},
                       exit_long=[{"left": "fast", "op": "crosses_below", "right": "slow"}], targets=[])
    res = run_backtest(spec, zigzag(12, amplitude=8, bars=30))
    reasons = {t["exit_reason"] for t in res["trades"]}
    assert reasons <= {"rule_exit", "stop_loss", "end_of_data"}
    assert res["trades"]


def test_invalid_strategy_rejected_and_short_data_rejected():
    bad = simple_spec()
    bad["stop_loss"] = None
    with pytest.raises(ValueError):
        run_backtest(bad, random_walk(300))
    with pytest.raises(ValueError):
        run_backtest(simple_spec(), random_walk(20))


def test_deterministic():
    c = random_walk(500, seed=2)
    a, b = run_backtest(default_spec(), c), run_backtest(default_spec(), c)
    assert a["stats"] == b["stats"]


def test_compare_strategies():
    c = random_walk(600, seed=4)
    s2 = copy.deepcopy(default_spec())
    s2["name"] = "tight"
    s2["stop_loss"] = {"type": "atr", "period": 14, "mult": 1.0}
    bad = {"name": "bad"}
    rows = compare_strategies([default_spec(), s2, bad], c)
    assert rows[0]["ok"] and rows[1]["ok"] and not rows[2]["ok"]


def test_max_bars_in_trade():
    spec = simple_spec(targets=[], stop_loss={"type": "percent", "value": 50}, risk={"risk_per_trade_pct": 1, "max_position_pct": 100, "max_bars_in_trade": 3})
    res = run_backtest(spec, zigzag(10))
    assert all(t["bars"] <= 5 for t in res["trades"][:-1])
