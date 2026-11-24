import math
from datetime import datetime

import pytest

from app.core import options as op
from app.core.analysis import analyze_timeframe, market_overview, multi_timeframe_signal
from app.core.scanner import run_scan, scan_options_chain
from app.providers.base import IST
from tests.helpers import candles_from_closes, random_walk, trending


# -------------------------------------------------------------- Black-Scholes
def test_bs_known_value():
    # Hull textbook: S=42 K=40 r=10% sigma=20% T=0.5 -> call 4.76, put 0.81
    assert op.bs_price(42, 40, 0.5, 0.10, 0.20, "CE") == pytest.approx(4.76, abs=0.01)
    assert op.bs_price(42, 40, 0.5, 0.10, 0.20, "PE") == pytest.approx(0.81, abs=0.01)


def test_put_call_parity():
    s, k, t, r, v = 100, 95, 0.4, 0.05, 0.3
    c, p = op.bs_price(s, k, t, r, v, "CE"), op.bs_price(s, k, t, r, v, "PE")
    assert c - p == pytest.approx(s - k * math.exp(-r * t), abs=1e-9)


def test_greeks_signs_and_finite_difference():
    s, k, t, r, v = 100, 100, 0.25, 0.05, 0.25
    g = op.bs_greeks(s, k, t, r, v, "CE")
    assert 0 < g["delta"] < 1 and g["gamma"] > 0 and g["vega"] > 0 and g["theta"] < 0
    h = 0.01
    fd_delta = (op.bs_price(s + h, k, t, r, v, "CE") - op.bs_price(s - h, k, t, r, v, "CE")) / (2 * h)
    assert g["delta"] == pytest.approx(fd_delta, abs=1e-4)
    fd_vega = (op.bs_price(s, k, t, r, v + 0.0001, "CE") - op.bs_price(s, k, t, r, v - 0.0001, "CE")) / 0.0002 / 100
    assert g["vega"] == pytest.approx(fd_vega, abs=1e-3)
    pg = op.bs_greeks(s, k, t, r, v, "PE")
    assert -1 < pg["delta"] < 0
    assert pg["delta"] - g["delta"] == pytest.approx(-1.0, abs=1e-9)


def test_implied_vol_roundtrip_and_bounds():
    price = op.bs_price(100, 105, 0.3, 0.06, 0.37, "CE")
    assert op.implied_vol(price, 100, 105, 0.3, 0.06, "CE") == pytest.approx(0.37, abs=1e-4)
    assert op.implied_vol(0.0001, 100, 50, 0.3, 0.06, "CE") is None   # below intrinsic
    assert op.implied_vol(500, 100, 105, 0.3, 0.06, "CE") is None     # above spot


# ------------------------------------------------------------------- chain
def mk_chain(spot=100.0, provider="angelone", with_oi=True):
    rows = []
    for k in (90, 95, 100, 105, 110):
        rows.append({"strike": float(k),
                     "CE": {"ltp": max(0.5, spot - k + 3), "oi": (1000 * (k - 80)) if with_oi else None, "volume": 10 * k,
                            "iv": None, "delta": None, "gamma": None, "theta": None, "vega": None},
                     "PE": {"ltp": max(0.5, k - spot + 3), "oi": (1000 * (120 - k)) if with_oi else None, "volume": 5 * k,
                            "iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}})
    return {"provider": provider, "underlying": "TEST", "expiry": "2030-01-31", "spot": spot, "rows": rows}


def test_moneyness_classification():
    a = op.analyze_chain(mk_chain())
    by = {r["strike"]: r for r in a["rows"]}
    assert a["summary"]["atm_strike"] == 100.0
    assert by[100.0]["CE"]["moneyness"] == "ATM"
    assert by[95.0]["CE"]["moneyness"] == "ITM" and by[105.0]["CE"]["moneyness"] == "OTM"
    assert by[95.0]["PE"]["moneyness"] == "OTM" and by[105.0]["PE"]["moneyness"] == "ITM"


def test_pcr_and_max_pain_hand_calculated():
    rows = [{"strike": 100.0, "CE": {"oi": 100}, "PE": {"oi": 300}},
            {"strike": 110.0, "CE": {"oi": 200}, "PE": {"oi": 100}}]
    chain = {"provider": "x", "underlying": "T", "expiry": "2030-01-31", "spot": 105, "rows": rows}
    a = op.analyze_chain(chain)
    assert a["summary"]["pcr_oi"] == pytest.approx(400 / 300)
    # pain at 100: CE:0 + PE:0 (K=100) + strike110: CE 0, PE 100*10=1000 -> 1000
    # pain at 110: strike100: CE 100*10=1000, PE 0; strike110: 0 -> 1000 -> tie; first (lowest) strike wins
    mp = op.max_pain(rows)
    assert mp["strike"] == 100.0 and mp["curve"][0]["pain"] == 1000.0


def test_missing_data_is_not_fabricated():
    a = op.analyze_chain(mk_chain(with_oi=False))
    assert a["summary"]["pcr_oi"] is None and a["summary"]["max_pain"] is None
    # greeks stay None unless derive=True
    assert a["rows"][2]["CE"]["delta"] is None


def test_derive_flag_computes_only_when_requested_and_labels_it():
    now = datetime(2029, 12, 1, 10, 0, tzinfo=IST)
    a = op.analyze_chain(mk_chain(), derive=True, now=now)
    leg = a["rows"][2]["CE"]
    assert leg["greeks_source"] == "derived_bs" and leg["delta"] is not None and leg["iv_source"] == "derived_bs"


def test_provider_values_never_overwritten():
    ch = mk_chain()
    ch["rows"][2]["CE"].update({"delta": 0.51, "gamma": 0.1, "theta": -1, "vega": 2, "iv": 20.0})
    a = op.analyze_chain(ch, derive=True, now=datetime(2029, 12, 1, tzinfo=IST))
    assert a["rows"][2]["CE"]["delta"] == 0.51


def test_strike_analysis():
    a = op.analyze_chain(mk_chain())
    s = op.strike_analysis(a, 100.0)
    assert s["ce_breakeven"] == pytest.approx(100 + a["rows"][2]["CE"]["ltp"])
    assert s["straddle_premium"] > 0
    with pytest.raises(ValueError):
        op.strike_analysis(a, 123.0)


def test_option_scanner():
    a = op.analyze_chain(mk_chain())
    top = scan_options_chain(a, "high_oi", {"top": 2})
    assert len(top) == 2 and top[0]["oi"] >= top[1]["oi"]
    with pytest.raises(ValueError):
        scan_options_chain(a, "nope", {})


# --------------------------------------------------------------- analysis
def test_uptrend_is_long_downtrend_short():
    up = analyze_timeframe(candles_from_closes([100 * 1.004 ** i for i in range(260)]))
    dn = analyze_timeframe(candles_from_closes([100 * 0.996 ** i for i in range(260)]))
    assert up["direction"] == "LONG" and up["score"] > 35
    assert dn["direction"] == "SHORT" and dn["score"] < -35
    assert up["reasons"]


def test_mtf_signal_levels_and_disclaimer():
    tf = {k: candles_from_closes([100 * 1.004 ** i for i in range(260)]) for k in ("15m", "1h", "4h")}
    sig = multi_timeframe_signal(tf)
    assert sig["signal"] == "LONG" and 5 <= sig["confidence"] <= 90
    lv = sig["levels"]
    assert lv["stop_loss"] < lv["entry"] < lv["target_1"] < lv["target_2"] < lv["target_3"]
    assert lv["target_2"] - lv["entry"] == pytest.approx(2 * (lv["entry"] - lv["stop_loss"]))
    assert "not investment advice" in sig["disclaimer"] and "guarantee" in sig["disclaimer"]


def test_mtf_conflict_gives_wait():
    low = candles_from_closes([100 * 1.004 ** i for i in range(260)])
    high = candles_from_closes([100 * 0.996 ** i for i in range(260)])
    sig = multi_timeframe_signal({"15m": low, "1d": high})
    assert sig["signal"] == "WAIT" and sig["levels"] is None and sig["confidence"] is None


def test_insufficient_data_raises():
    with pytest.raises(ValueError):
        analyze_timeframe(random_walk(20))
    with pytest.raises(ValueError):
        multi_timeframe_signal({"15m": random_walk(20)})


def test_market_overview_has_context():
    ov = market_overview(random_walk(300, seed=3))
    assert {"analysis", "support_resistance", "fibonacci"} <= set(ov)


# ----------------------------------------------------------------- scanner
def fetcher(sym, tf, limit):
    if sym == "BAD":
        raise RuntimeError("provider down")
    return {"UP": candles_from_closes([100 * 1.004 ** i for i in range(260)]),
            "DOWN": candles_from_closes([100 * 0.996 ** i for i in range(260)])}[sym]


def test_trend_scan_reports_errors_per_symbol():
    res = run_scan(["UP", "DOWN", "BAD"], "trend", "15m", {"direction": "bullish"}, fetcher)
    assert [m["symbol"] for m in res["matches"]] == ["UP"]
    assert res["errors"][0]["symbol"] == "BAD" and "provider down" in res["errors"][0]["error"]


def test_breakout_volume_rsi_custom_scans():
    base = [100.0] * 60
    c = candles_from_closes(base + [110.0])
    c[-1]["v"] = 1000.0
    f = lambda s, t, l: c
    assert run_scan(["X"], "breakout", "15m", {"lookback": 20}, f)["matches"][0]["direction"] == "UP"
    assert run_scan(["X"], "high_volume", "15m", {"multiplier": 3}, f)["matches"]
    custom = {"indicators": [{"id": "r", "type": "rsi", "params": {"period": 14}}], "conditions": [{"left": "r", "op": ">", "right": 60}, {"left": "close", "op": ">", "right": 105}]}
    assert run_scan(["X"], "custom", "15m", custom, f)["matches"]
    with pytest.raises(ValueError):
        run_scan(["X"], "custom", "15m", {"indicators": [], "conditions": [{"left": "nope", "op": ">", "right": 1}]}, f)


def test_scan_input_validation():
    with pytest.raises(ValueError):
        run_scan([], "trend", "15m", {}, fetcher)
    with pytest.raises(ValueError):
        run_scan(["UP"], "magic", "15m", {}, fetcher)
