import numpy as np
import pytest

from app.core import indicators as ind
from app.providers.base import DataError, resample, validate_candles
from app import marketdata
from tests.fakes import walk

CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
          45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]


def cols(c):
    return [x["h"] for x in c], [x["l"] for x in c], [x["c"] for x in c], [x["v"] for x in c]


def test_sma():
    r = ind.sma([1, 2, 3, 4, 5], 3)
    assert np.isnan(r[:2]).all() and list(r[2:]) == [2, 3, 4]


def test_ema_seed_and_recursion():
    x = [22.27, 22.19, 22.08, 22.17, 22.18, 22.13, 22.23, 22.43, 22.24, 22.29, 22.15, 22.39]
    r = ind.ema(x, 10)
    seed = float(np.mean(x[:10]))
    assert r[9] == pytest.approx(seed)
    assert r[10] == pytest.approx((22.15 - seed) * 2 / 11 + seed)


def test_rsi_wilder_reference_values():
    r = ind.rsi(CLOSES, 14)
    assert np.isnan(r[:14]).all()
    assert r[14] == pytest.approx(70.46, abs=0.2)       # StockCharts worked example
    assert r[15] == pytest.approx(66.25, abs=0.3)
    assert r[19] == pytest.approx(57.92, abs=0.5)


def test_rsi_bounds():
    c = [x["c"] for x in walk(300)]
    v = ind.rsi(c, 14)
    v = v[~np.isnan(v)]
    assert v.min() >= 0 and v.max() <= 100
    assert ind.rsi(list(np.arange(1, 40.0)), 14)[-1] == 100.0
    assert ind.rsi(list(np.arange(40.0, 1, -1)), 14)[-1] == pytest.approx(0.0, abs=1e-9)


def test_macd_identities():
    c = [x["c"] for x in walk(300)]
    m = ind.macd(c)
    ok = ~np.isnan(m["macd"])
    assert np.allclose(m["macd"][ok], (ind.ema(c, 12) - ind.ema(c, 26))[ok])
    ok2 = ~np.isnan(m["hist"])
    assert np.allclose(m["hist"][ok2], (m["macd"] - m["signal"])[ok2])


def test_bollinger():
    c = np.array([x["c"] for x in walk(200)])
    b = ind.bollinger(c, 20, 2)
    w = c[81:101]
    assert b["mid"][100] == pytest.approx(w.mean())
    assert b["upper"][100] == pytest.approx(w.mean() + 2 * w.std())
    assert b["lower"][100] == pytest.approx(w.mean() - 2 * w.std())


def test_true_range_and_atr():
    tr = ind.true_range([10, 12, 11, 13], [8, 9, 9, 10], [9, 11, 10, 12])
    assert list(tr) == [2, 3, 2, 3]
    h, l, c, _ = cols(walk(200))
    a = ind.atr(h, l, c, 14)
    assert np.nanmin(a) > 0


def test_vwap_resets_daily():
    t = [0, 3_600_000, 86_400_000, 86_400_000 + 3_600_000]
    r = ind.vwap([10, 20, 30, 50], [10, 20, 30, 50], [10, 20, 30, 50], [1, 1, 1, 3], t)
    assert r[0] == 10 and r[1] == 15 and r[2] == 30 and r[3] == pytest.approx((30 + 150) / 4)


def test_supertrend_direction():
    up = np.linspace(100, 200, 80)
    s = ind.supertrend(up + 1, up - 1, up, 10, 3)
    assert s["direction"][-1] == 1 and s["line"][-1] < up[-1]
    dn = np.linspace(200, 100, 80)
    s = ind.supertrend(dn + 1, dn - 1, dn, 10, 3)
    assert s["direction"][-1] == -1 and s["line"][-1] > dn[-1]


def test_adx_trend_vs_range():
    up = np.linspace(100, 200, 120)
    a = ind.adx(up + 1, up - 1, up, 14)
    keys = set(a)
    assert {"adx"} <= keys
    assert a["adx"][-1] > 40
    flat = 100 + np.sin(np.linspace(0, 40, 200))
    f = ind.adx(flat + .5, flat - .5, flat, 14)
    assert f["adx"][-1] < a["adx"][-1]
    v = a["adx"][~np.isnan(a["adx"])]
    assert v.min() >= 0 and v.max() <= 100


def test_stochastic():
    x = np.linspace(1, 100, 60)
    s = ind.stochastic(x + .5, x - .5, x)
    assert s["k"][-1] > 90
    kk = s["k"][~np.isnan(s["k"])]
    assert kk.min() >= 0 and kk.max() <= 100


def test_fibonacci():
    up = ind.fibonacci(200, 100, "up")
    dn = ind.fibonacci(200, 100, "down")
    vals_up, vals_dn = sorted(up.values()), sorted(dn.values())
    assert min(vals_up) <= 100 + 1e-9 and max(vals_up) >= 200 - 1e-9
    assert any(abs(v - 150) < 1e-9 for v in vals_up) and any(abs(v - 138.2) < 1e-6 for v in vals_up)
    assert any(abs(v - 150) < 1e-9 for v in vals_dn)


def test_support_resistance_and_trend_lines():
    x = np.concatenate([np.linspace(100, 120, 20), np.linspace(120, 105, 20), np.linspace(105, 121, 20),
                        np.linspace(121, 106, 20), np.linspace(106, 118, 20)])
    h, l = x + .3, x - .3
    sr = ind.support_resistance(h, l, x, 3, 3, 1.0)
    assert any(119 < z["price"] < 122 for z in sr["resistance"])
    assert any(104 < z["price"] < 107 for z in sr["support"])
    tl = ind.trend_lines(h, l, 3, 3)
    assert isinstance(tl, dict)


def test_validate_and_resample():
    c = walk(50, step_ms=3_600_000, start_ms=1_700_006_400_000)
    bad = [{"t": 1, "o": 1, "h": .5, "l": 1, "c": 1, "v": 1}, {"t": 2, "o": float("nan"), "h": 1, "l": 1, "c": 1, "v": 1},
           {"t": 3, "o": 1, "h": 2, "l": .5, "c": 1, "v": -5}, {"t": 4}]
    good, dropped = validate_candles(bad + c + [c[0]])
    assert dropped == 4 and len(good) == 50
    h4 = resample(c, "1h", "4h")
    bucket = h4[1]["t"]
    members = [x for x in c if x["t"] - x["t"] % 14_400_000 == bucket]
    assert h4[1]["v"] == pytest.approx(sum(x["v"] for x in members)) and h4[1]["h"] == max(x["h"] for x in members)
    assert h4[1]["o"] == members[0]["o"] and h4[1]["c"] == members[-1]["c"]
    with pytest.raises(DataError):
        resample(c, "1h", "30m")


def test_drop_unclosed():
    c = [{"t": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}, {"t": 60_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]
    assert len(marketdata.drop_unclosed(c, "1m", 90_000)) == 1
    assert len(marketdata.drop_unclosed(c, "1m", 120_000)) == 2
