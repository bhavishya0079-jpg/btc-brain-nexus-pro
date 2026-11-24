"""Technical indicators. Every function returns numpy arrays aligned with the input
(NaN where the indicator is not yet defined). No look-ahead: value[i] only uses data <= i.

Conventions (documented so results can be cross-checked with charting platforms):
  * EMA is seeded with the SMA of the first `n` values, alpha = 2/(n+1).
  * RSI / ATR / ADX use Wilder's smoothing (alpha = 1/n, seeded with the first n-period average).
  * Bollinger Bands use the population standard deviation.
  * VWAP is cumulative typical-price VWAP, optionally reset every UTC day.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np

NAN = float("nan")


def _arr(x: Sequence[float]) -> np.ndarray:
    return np.asarray(x, dtype=float)


def sma(values: Sequence[float], n: int) -> np.ndarray:
    v = _arr(values)
    out = np.full(len(v), NAN)
    if n < 1:
        raise ValueError("period must be >= 1")
    if len(v) < n:
        return out
    csum = np.cumsum(np.insert(v, 0, 0.0))
    out[n - 1:] = (csum[n:] - csum[:-n]) / n
    return out


def ema(values: Sequence[float], n: int) -> np.ndarray:
    v = _arr(values)
    out = np.full(len(v), NAN)
    if n < 1:
        raise ValueError("period must be >= 1")
    # first valid window (supports leading NaNs, e.g. when chaining indicators)
    valid = np.where(~np.isnan(v))[0]
    if len(valid) < n:
        return out
    first = valid[0]
    if first + n > len(v):
        return out
    seed_idx = first + n - 1
    out[seed_idx] = np.mean(v[first:first + n])
    alpha = 2.0 / (n + 1)
    for i in range(seed_idx + 1, len(v)):
        out[i] = alpha * v[i] + (1 - alpha) * out[i - 1]
    return out


def _wilder(values: np.ndarray, n: int, start: int) -> np.ndarray:
    """Wilder smoothing; `start` is the index of the first value to include in the seed window."""
    out = np.full(len(values), NAN)
    if len(values) < start + n:
        return out
    seed = start + n - 1
    out[seed] = np.mean(values[start:start + n])
    for i in range(seed + 1, len(values)):
        out[i] = (out[i - 1] * (n - 1) + values[i]) / n
    return out


def rsi(close: Sequence[float], n: int = 14) -> np.ndarray:
    c = _arr(close)
    out = np.full(len(c), NAN)
    if len(c) <= n:
        return out
    delta = np.diff(c)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = _wilder(gain, n, 0)
    avg_loss = _wilder(loss, n, 0)
    for i in range(n - 1, len(delta)):
        ag, al = avg_gain[i], avg_loss[i]
        if al == 0:
            out[i + 1] = 100.0 if ag > 0 else 50.0
        else:
            out[i + 1] = 100.0 - 100.0 / (1.0 + ag / al)
    return out


def macd(close: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, np.ndarray]:
    if fast >= slow:
        raise ValueError("fast period must be smaller than slow period")
    c = _arr(close)
    line = ema(c, fast) - ema(c, slow)
    sig = ema(line, signal)
    return {"macd": line, "signal": sig, "hist": line - sig}


def bollinger(close: Sequence[float], n: int = 20, k: float = 2.0) -> Dict[str, np.ndarray]:
    c = _arr(close)
    mid = sma(c, n)
    std = np.full(len(c), NAN)
    for i in range(n - 1, len(c)):
        std[i] = np.std(c[i - n + 1:i + 1])
    upper, lower = mid + k * std, mid - k * std
    width = (upper - lower) / mid
    return {"mid": mid, "upper": upper, "lower": lower, "width": width}


def true_range(high: Sequence[float], low: Sequence[float], close: Sequence[float]) -> np.ndarray:
    h, l, c = _arr(high), _arr(low), _arr(close)
    tr = np.empty(len(c))
    if len(c) == 0:
        return tr
    tr[0] = h[0] - l[0]
    prev = c[:-1]
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - prev), np.abs(l[1:] - prev)])
    return tr


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 14) -> np.ndarray:
    return _wilder(true_range(high, low, close), n, 0)


def vwap(high: Sequence[float], low: Sequence[float], close: Sequence[float], volume: Sequence[float],
         t_ms: Optional[Sequence[int]] = None, reset_daily: bool = True) -> np.ndarray:
    h, l, c, v = _arr(high), _arr(low), _arr(close), _arr(volume)
    tp = (h + l + c) / 3.0
    out = np.full(len(c), NAN)
    cum_pv = cum_v = 0.0
    prev_day = None
    for i in range(len(c)):
        if reset_daily and t_ms is not None:
            day = int(t_ms[i] // 86_400_000)
            if day != prev_day:
                cum_pv = cum_v = 0.0
                prev_day = day
        cum_pv += tp[i] * v[i]
        cum_v += v[i]
        out[i] = cum_pv / cum_v if cum_v > 0 else NAN
    return out


def supertrend(high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 10,
               mult: float = 3.0) -> Dict[str, np.ndarray]:
    h, l, c = _arr(high), _arr(low), _arr(close)
    a = atr(h, l, c, n)
    mid = (h + l) / 2.0
    ub, lb = mid + mult * a, mid - mult * a
    final_ub, final_lb = np.full(len(c), NAN), np.full(len(c), NAN)
    line, direction = np.full(len(c), NAN), np.full(len(c), NAN)
    for i in range(len(c)):
        if np.isnan(a[i]):
            continue
        prev_valid = i > 0 and not np.isnan(final_ub[i - 1])
        if not prev_valid:
            final_ub[i], final_lb[i] = ub[i], lb[i]
            direction[i] = 1.0 if c[i] >= mid[i] else -1.0
        else:
            final_ub[i] = ub[i] if (ub[i] < final_ub[i - 1] or c[i - 1] > final_ub[i - 1]) else final_ub[i - 1]
            final_lb[i] = lb[i] if (lb[i] > final_lb[i - 1] or c[i - 1] < final_lb[i - 1]) else final_lb[i - 1]
            if direction[i - 1] == 1.0:
                direction[i] = -1.0 if c[i] < final_lb[i] else 1.0
            else:
                direction[i] = 1.0 if c[i] > final_ub[i] else -1.0
        line[i] = final_lb[i] if direction[i] == 1.0 else final_ub[i]
    return {"line": line, "direction": direction}


def adx(high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 14) -> Dict[str, np.ndarray]:
    h, l, c = _arr(high), _arr(low), _arr(close)
    size = len(c)
    out = {k: np.full(size, NAN) for k in ("adx", "plus_di", "minus_di")}
    if size < 2 * n:
        return out
    up, down = h[1:] - h[:-1], l[:-1] - l[1:]
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(h, l, c)[1:]

    def wilder_sum(x: np.ndarray) -> np.ndarray:
        res = np.full(len(x), NAN)
        res[n - 1] = np.sum(x[:n])
        for i in range(n, len(x)):
            res[i] = res[i - 1] - res[i - 1] / n + x[i]
        return res

    s_tr, s_p, s_m = wilder_sum(tr), wilder_sum(plus_dm), wilder_sum(minus_dm)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi, mdi = 100 * s_p / s_tr, 100 * s_m / s_tr
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    dx = np.where(np.isnan(dx), NAN, dx)
    adx_vals = np.full(len(dx), NAN)
    first = n - 1
    if len(dx) >= first + n:
        adx_vals[first + n - 1] = np.nanmean(dx[first:first + n])
        for i in range(first + n, len(dx)):
            adx_vals[i] = (adx_vals[i - 1] * (n - 1) + dx[i]) / n
    out["plus_di"][1:], out["minus_di"][1:], out["adx"][1:] = pdi, mdi, adx_vals
    return out


def stochastic(high: Sequence[float], low: Sequence[float], close: Sequence[float], k: int = 14, d: int = 3,
               smooth: int = 3) -> Dict[str, np.ndarray]:
    h, l, c = _arr(high), _arr(low), _arr(close)
    raw = np.full(len(c), NAN)
    for i in range(k - 1, len(c)):
        hh, ll = np.max(h[i - k + 1:i + 1]), np.min(l[i - k + 1:i + 1])
        raw[i] = 50.0 if hh == ll else 100.0 * (c[i] - ll) / (hh - ll)
    k_line = sma_nan(raw, smooth) if smooth > 1 else raw
    return {"k": k_line, "d": sma_nan(k_line, d)}


def sma_nan(values: np.ndarray, n: int) -> np.ndarray:
    """SMA that tolerates leading NaNs."""
    out = np.full(len(values), NAN)
    for i in range(len(values)):
        w = values[max(0, i - n + 1):i + 1]
        if len(w) == n and not np.any(np.isnan(w)):
            out[i] = np.mean(w)
    return out


FIB_RETRACE = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
FIB_EXT = (1.272, 1.618, 2.0)


def fibonacci(swing_high: float, swing_low: float, trend: str = "up") -> Dict[str, float]:
    """Retracement levels measured from the end of the move back toward its start.

    trend='up'   : levels between swing_high (0%) and swing_low (100%).
    trend='down' : levels between swing_low (0%) and swing_high (100%).
    """
    if swing_high <= swing_low:
        raise ValueError("swing_high must be greater than swing_low")
    rng = swing_high - swing_low
    levels: Dict[str, float] = {}
    for r in FIB_RETRACE:
        levels[f"{r:g}"] = swing_high - rng * r if trend == "up" else swing_low + rng * r
    for r in FIB_EXT:
        levels[f"{r:g}"] = swing_high + rng * (r - 1) if trend == "up" else swing_low - rng * (r - 1)
    return levels


def swing_points(high: Sequence[float], low: Sequence[float], left: int = 3, right: int = 3) -> Dict[str, List[int]]:
    h, l = _arr(high), _arr(low)
    highs, lows = [], []
    for i in range(left, len(h) - right):
        # strictly higher than the bars on the left, not lower than the bars on the right
        # (so a flat top/bottom is counted once, at its first bar)
        if h[i] > np.max(h[i - left:i]) and h[i] >= np.max(h[i + 1:i + right + 1]):
            highs.append(i)
        if l[i] < np.min(l[i - left:i]) and l[i] <= np.min(l[i + 1:i + right + 1]):
            lows.append(i)
    return {"highs": highs, "lows": lows}


def support_resistance(high: Sequence[float], low: Sequence[float], close: Sequence[float], left: int = 3,
                       right: int = 3, tolerance_pct: float = 0.4, max_levels: int = 6) -> Dict[str, List[Dict[str, float]]]:
    """Cluster swing highs/lows into horizontal levels; stronger = more touches."""
    h, l, c = _arr(high), _arr(low), _arr(close)
    if len(c) == 0:
        return {"support": [], "resistance": []}
    pts = swing_points(h, l, left, right)
    prices = [h[i] for i in pts["highs"]] + [l[i] for i in pts["lows"]]
    clusters: List[List[float]] = []
    for p in sorted(prices):
        if clusters and abs(p - np.mean(clusters[-1])) / np.mean(clusters[-1]) * 100 <= tolerance_pct:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    last = float(c[-1])
    levels = [{"price": float(np.mean(cl)), "touches": len(cl)} for cl in clusters]
    support = sorted([x for x in levels if x["price"] < last], key=lambda x: (-x["touches"], last - x["price"]))[:max_levels]
    resistance = sorted([x for x in levels if x["price"] >= last], key=lambda x: (-x["touches"], x["price"] - last))[:max_levels]
    return {"support": sorted(support, key=lambda x: -x["price"]), "resistance": sorted(resistance, key=lambda x: x["price"])}


def trend_lines(high: Sequence[float], low: Sequence[float], left: int = 3, right: int = 3) -> Dict[str, Optional[Dict[str, float]]]:
    """Simple trend lines through the last two swing lows (uptrend) and last two swing highs (downtrend)."""
    h, l = _arr(high), _arr(low)
    pts = swing_points(h, l, left, right)
    out: Dict[str, Optional[Dict[str, float]]] = {"up": None, "down": None}
    if len(pts["lows"]) >= 2:
        i1, i2 = pts["lows"][-2:]
        out["up"] = {"i1": i1, "p1": float(l[i1]), "i2": i2, "p2": float(l[i2]), "slope": float((l[i2] - l[i1]) / (i2 - i1))}
    if len(pts["highs"]) >= 2:
        i1, i2 = pts["highs"][-2:]
        out["down"] = {"i1": i1, "p1": float(h[i1]), "i2": i2, "p2": float(h[i2]), "slope": float((h[i2] - h[i1]) / (i2 - i1))}
    return out


def last_valid(arr: np.ndarray) -> Optional[float]:
    for x in arr[::-1]:
        if not np.isnan(x):
            return float(x)
    return None
