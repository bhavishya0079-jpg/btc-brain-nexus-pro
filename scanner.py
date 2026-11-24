"""Market scanners. Each scanner inspects real candles supplied by a provider and returns a match
dict (or None). Symbols that fail to load are reported as errors - never silently skipped, never faked."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from . import indicators as ind
from .analysis import analyze_timeframe, multi_timeframe_signal
from .strategy import Series, compute_series, eval_conditions, validate_spec

MAX_SYMBOLS = 40


def _arrs(candles):
    return (np.array([c["h"] for c in candles]), np.array([c["l"] for c in candles]),
            np.array([c["c"] for c in candles]), np.array([c["v"] for c in candles]))


def scan_high_volume(candles, p):
    h, l, c, v = _arrs(candles)
    n = int(p.get("period", 20))
    mult = float(p.get("multiplier", 2.0))
    if len(v) < n + 2:
        return None
    avg = float(np.mean(v[-n - 1:-1]))
    if avg > 0 and v[-1] >= mult * avg:
        return {"volume_ratio": float(v[-1] / avg), "price": float(c[-1]), "detail": f"Volume {v[-1] / avg:.1f}x the {n}-bar average"}
    return None


def scan_breakout(candles, p):
    h, l, c, v = _arrs(candles)
    n = int(p.get("lookback", 20))
    if len(c) < n + 2:
        return None
    prev_hi, prev_lo = float(np.max(h[-n - 1:-1])), float(np.min(l[-n - 1:-1]))
    if c[-1] > prev_hi:
        return {"direction": "UP", "level": prev_hi, "price": float(c[-1]), "detail": f"Close above {n}-bar high {prev_hi:.4g}"}
    if c[-1] < prev_lo:
        return {"direction": "DOWN", "level": prev_lo, "price": float(c[-1]), "detail": f"Close below {n}-bar low {prev_lo:.4g}"}
    return None


def scan_trend(candles, p):
    a = analyze_timeframe(candles)
    want = p.get("direction", "any")
    if a["trend"] == "sideways":
        return None
    if want == "bullish" and a["trend"] != "bullish":
        return None
    if want == "bearish" and a["trend"] != "bearish":
        return None
    if a["adx"] is not None and a["adx"] < float(p.get("min_adx", 20)):
        return None
    return {"trend": a["trend"], "score": a["score"], "adx": a["adx"], "price": a["price"], "detail": f"{a['trend']} trend, ADX {a['adx'] and round(a['adx'])}"}


def scan_rsi(candles, p):
    h, l, c, v = _arrs(candles)
    r = ind.rsi(c, int(p.get("period", 14)))[-1]
    if np.isnan(r):
        return None
    lo, hi = float(p.get("oversold", 30)), float(p.get("overbought", 70))
    if r <= lo:
        return {"rsi": float(r), "state": "oversold", "price": float(c[-1]), "detail": f"RSI {r:.1f} <= {lo:g}"}
    if r >= hi:
        return {"rsi": float(r), "state": "overbought", "price": float(c[-1]), "detail": f"RSI {r:.1f} >= {hi:g}"}
    return None


def scan_macd(candles, p):
    h, l, c, v = _arrs(candles)
    m = ind.macd(c)
    a, b = m["macd"], m["signal"]
    if len(c) < 40 or np.isnan(a[-2]) or np.isnan(b[-2]):
        return None
    if a[-2] <= b[-2] and a[-1] > b[-1]:
        return {"cross": "bullish", "price": float(c[-1]), "detail": "MACD crossed above signal"}
    if a[-2] >= b[-2] and a[-1] < b[-1]:
        return {"cross": "bearish", "price": float(c[-1]), "detail": "MACD crossed below signal"}
    return None


def scan_ema_cross(candles, p):
    h, l, c, v = _arrs(candles)
    fast, slow = int(p.get("fast", 9)), int(p.get("slow", 21))
    if fast >= slow or len(c) < slow + 3:
        return None
    f, s = ind.ema(c, fast), ind.ema(c, slow)
    if np.isnan(f[-2]) or np.isnan(s[-2]):
        return None
    if f[-2] <= s[-2] and f[-1] > s[-1]:
        return {"cross": "bullish", "price": float(c[-1]), "detail": f"EMA{fast} crossed above EMA{slow}"}
    if f[-2] >= s[-2] and f[-1] < s[-1]:
        return {"cross": "bearish", "price": float(c[-1]), "detail": f"EMA{fast} crossed below EMA{slow}"}
    return None


def scan_custom(candles, p):
    spec = {"name": "scan", "timeframe": "15m", "side": "long", "indicators": p.get("indicators", []),
            "entry_long": p.get("conditions", []), "stop_loss": {"type": "percent", "value": 1}}
    errors, _ = validate_spec(spec)
    if errors:
        raise ValueError("Invalid custom filter: " + "; ".join(errors))
    s = Series(candles)
    series = compute_series(spec, s)
    if eval_conditions(spec["entry_long"], series, s.n - 1):
        return {"price": float(s.close[-1]), "detail": "All custom conditions are true on the last bar"}
    return None


SINGLE_TF_SCANNERS: Dict[str, Callable] = {
    "high_volume": scan_high_volume, "breakout": scan_breakout, "trend": scan_trend, "rsi": scan_rsi,
    "macd": scan_macd, "ema_cross": scan_ema_cross, "custom": scan_custom,
}
SCAN_TYPES = sorted(list(SINGLE_TF_SCANNERS) + ["mtf"])


def run_scan(symbols: List[str], scan_type: str, interval: str, params: Dict[str, Any],
             fetch: Callable[[str, str, int], List[Dict[str, float]]], *, limit: int = 300,
             mtf_intervals: Optional[List[str]] = None, workers: int = 4) -> Dict[str, Any]:
    if scan_type not in SCAN_TYPES:
        raise ValueError(f"Unknown scan type. Use one of {SCAN_TYPES}")
    if not symbols:
        raise ValueError("No symbols to scan")
    symbols = list(dict.fromkeys(symbols))[:MAX_SYMBOLS]
    if scan_type == "custom":
        errors, _ = validate_spec({"name": "x", "timeframe": "15m", "side": "long", "indicators": params.get("indicators", []),
                                   "entry_long": params.get("conditions", []), "stop_loss": {"type": "percent", "value": 1}})
        if errors:
            raise ValueError("Invalid custom filter: " + "; ".join(errors))

    def work(sym: str) -> Dict[str, Any]:
        try:
            if scan_type == "mtf":
                tfs = mtf_intervals or [interval]
                data = {tf: fetch(sym, tf, limit) for tf in tfs}
                res = multi_timeframe_signal(data)
                if res["signal"] == "WAIT":
                    return {"symbol": sym, "match": None}
                return {"symbol": sym, "match": {"signal": res["signal"], "confidence": res["confidence"],
                                                 "score": res["combined_score"], "detail": "; ".join(res["reasons"][:2])}}
            candles = fetch(sym, interval, limit)
            return {"symbol": sym, "match": SINGLE_TF_SCANNERS[scan_type](candles, params)}
        except Exception as exc:  # report per-symbol, keep scanning
            return {"symbol": sym, "error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(work, symbols))
    matches = [{"symbol": r["symbol"], **r["match"]} for r in results if r.get("match")]
    errors = [{"symbol": r["symbol"], "error": r["error"]} for r in results if r.get("error")]
    return {"scan_type": scan_type, "interval": interval, "scanned": len(symbols), "matches": matches, "errors": errors}


def scan_options_chain(analyzed: Dict[str, Any], kind: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Option scanner over an analyzed chain: high_oi | high_volume | high_iv | oi_change | pcr_extreme."""
    legs = []
    for r in analyzed["rows"]:
        for side in ("CE", "PE"):
            leg = r.get(side)
            if leg:
                legs.append({"strike": r["strike"], "side": side, **leg})
    top_n = int(params.get("top", 5))
    keys = {"high_oi": "oi", "high_volume": "volume", "high_iv": "iv", "oi_change": "oi_change"}
    if kind in keys:
        k = keys[kind]
        ranked = sorted([x for x in legs if x.get(k) is not None], key=lambda x: -abs(x[k]))
        return ranked[:top_n]
    if kind == "pcr_extreme":
        pcr = analyzed["summary"].get("pcr_oi")
        hi, lo = float(params.get("high", 1.3)), float(params.get("low", 0.7))
        if pcr is None:
            return []
        if pcr >= hi or pcr <= lo:
            return [{"pcr_oi": pcr, "reading": analyzed["summary"]["pcr_reading"]}]
        return []
    raise ValueError("Unknown options scan. Use high_oi, high_volume, high_iv, oi_change or pcr_extreme")
