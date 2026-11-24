"""Rule-based market analysis and LONG / SHORT / WAIT signals.

This is transparent technical analysis (trend, momentum, volatility, volume), not a prediction
engine. Every output lists the reasons behind it and carries a disclaimer. Confidence is capped
at 90 because no technical signal is certain.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from ..providers.base import INTERVAL_SECONDS
from . import indicators as ind

DISCLAIMER = ("Analysis is based on historical price/volume patterns. It is not investment advice and "
              "does not guarantee any profit. Markets can move against any signal.")
TF_WEIGHT = {"1m": 0.5, "3m": 0.6, "5m": 0.8, "15m": 1.0, "30m": 1.2, "1h": 1.5, "4h": 2.0, "1d": 2.5}
MIN_CANDLES = 60


def _cols(candles: List[Dict[str, float]]):
    return (np.array([c["h"] for c in candles]), np.array([c["l"] for c in candles]),
            np.array([c["c"] for c in candles]), np.array([c["v"] for c in candles]))


def _f(x: float) -> Optional[float]:
    return None if x is None or np.isnan(x) else float(x)


def analyze_timeframe(candles: List[Dict[str, float]]) -> Dict[str, Any]:
    if len(candles) < MIN_CANDLES:
        raise ValueError(f"Need at least {MIN_CANDLES} candles, got {len(candles)}")
    h, l, c, v = _cols(candles)
    ema20, ema50 = ind.ema(c, 20), ind.ema(c, 50)
    ema200 = ind.ema(c, 200) if len(c) >= 200 else np.full(len(c), np.nan)
    rsi = ind.rsi(c, 14)
    mac = ind.macd(c)
    st = ind.supertrend(h, l, c, 10, 3.0)
    adx = ind.adx(h, l, c, 14)
    atr = ind.atr(h, l, c, 14)
    bb = ind.bollinger(c, 20, 2.0)
    vol_avg = ind.sma(v, 20)
    last = len(c) - 1
    price = float(c[last])
    reasons: List[str] = []
    score = 0.0

    # trend structure (weight 35)
    e20, e50 = ema20[last], ema50[last]
    if price > e20 > e50:
        score += 35; reasons.append("Price above EMA20 and EMA20 above EMA50 (uptrend structure)")
    elif price < e20 < e50:
        score -= 35; reasons.append("Price below EMA20 and EMA20 below EMA50 (downtrend structure)")
    elif price > e50:
        score += 12; reasons.append("Price above EMA50 but short-term structure is mixed")
    elif price < e50:
        score -= 12; reasons.append("Price below EMA50 but short-term structure is mixed")
    if not np.isnan(ema200[last]):
        if price > ema200[last]:
            score += 5; reasons.append("Price above EMA200 (long-term bias up)")
        else:
            score -= 5; reasons.append("Price below EMA200 (long-term bias down)")
    # supertrend (20)
    if st["direction"][last] == 1.0:
        score += 20; reasons.append("Supertrend is bullish")
    elif st["direction"][last] == -1.0:
        score -= 20; reasons.append("Supertrend is bearish")
    # MACD (20)
    hist, hist_prev = mac["hist"][last], mac["hist"][last - 1]
    if not np.isnan(hist):
        if hist > 0:
            score += 20 if hist > hist_prev else 10
            reasons.append("MACD histogram positive" + (" and rising" if hist > hist_prev else " but fading"))
        else:
            score -= 20 if hist < hist_prev else 10
            reasons.append("MACD histogram negative" + (" and falling" if hist < hist_prev else " but recovering"))
    # RSI (15) - momentum with exhaustion guard
    r = rsi[last]
    if not np.isnan(r):
        if 52 <= r <= 70:
            score += 15; reasons.append(f"RSI {r:.0f}: bullish momentum, not overbought")
        elif r > 75:
            score += 0; reasons.append(f"RSI {r:.0f}: overbought, upside momentum may be exhausted")
        elif 30 <= r <= 48:
            score -= 15; reasons.append(f"RSI {r:.0f}: bearish momentum, not oversold")
        elif r < 25:
            score += 0; reasons.append(f"RSI {r:.0f}: oversold, downside momentum may be exhausted")
        else:
            reasons.append(f"RSI {r:.0f}: neutral")
    # trend strength multiplier from ADX
    adx_v = adx["adx"][last]
    strength = "unknown"
    mult = 0.7
    if not np.isnan(adx_v):
        if adx_v >= 25:
            strength, mult = ("strong" if adx_v >= 40 else "moderate"), 1.0
        else:
            strength, mult = "weak/ranging", 0.55
        reasons.append(f"ADX {adx_v:.0f}: {strength} trend")
    score *= mult
    # volume confirmation
    vr = None
    if vol_avg[last] and not np.isnan(vol_avg[last]) and vol_avg[last] > 0:
        vr = float(v[last] / vol_avg[last])
    vol_confirms = vr is not None and vr >= 1.1
    if vr is not None:
        reasons.append(f"Volume is {vr:.2f}x its 20-bar average" + (" (confirming)" if vol_confirms else ""))
    # volatility
    atr_pct = float(atr[last] / price * 100.0) if not np.isnan(atr[last]) else None
    width = bb["width"]
    recent = width[~np.isnan(width)][-100:]
    pct_rank = float((recent < width[last]).mean() * 100.0) if len(recent) > 10 and not np.isnan(width[last]) else None
    vol_state = "unknown" if pct_rank is None else ("high" if pct_rank >= 80 else "low" if pct_rank <= 20 else "normal")
    score = max(-100.0, min(100.0, score))
    direction = "LONG" if score >= 35 else "SHORT" if score <= -35 else "WAIT"
    return {
        "score": round(score, 1), "direction": direction, "trend": "bullish" if score > 15 else "bearish" if score < -15 else "sideways",
        "trend_strength": strength, "adx": _f(adx_v), "rsi": _f(r), "atr": _f(atr[last]), "atr_pct": atr_pct,
        "macd_hist": _f(hist), "supertrend": "up" if st["direction"][last] == 1.0 else "down",
        "volume_ratio": vr, "volume_confirms": bool(vol_confirms),
        "volatility": {"state": vol_state, "bb_width_percentile": pct_rank, "atr_pct": atr_pct},
        "price": price, "ema20": _f(e20), "ema50": _f(e50), "reasons": reasons,
    }


def multi_timeframe_signal(candles_by_tf: Dict[str, List[Dict[str, float]]], *, primary: Optional[str] = None,
                           sl_atr_mult: float = 1.5) -> Dict[str, Any]:
    if not candles_by_tf:
        raise ValueError("No candle data provided")
    tfs = sorted(candles_by_tf, key=lambda k: INTERVAL_SECONDS[k])
    primary = primary if primary in candles_by_tf else tfs[0]
    per_tf: Dict[str, Any] = {}
    skipped: Dict[str, str] = {}
    for tf in tfs:
        try:
            per_tf[tf] = analyze_timeframe(candles_by_tf[tf])
        except ValueError as exc:
            skipped[tf] = str(exc)
    if not per_tf:
        raise ValueError("Not enough candles on any timeframe: " + "; ".join(skipped.values()))
    wsum = sum(TF_WEIGHT[tf] for tf in per_tf)
    avg = sum(per_tf[tf]["score"] * TF_WEIGHT[tf] for tf in per_tf) / wsum
    n_long = sum(1 for a in per_tf.values() if a["score"] >= 15)
    n_short = sum(1 for a in per_tf.values() if a["score"] <= -15)
    top_tf = max(per_tf, key=lambda k: INTERVAL_SECONDS[k])
    top_score = per_tf[top_tf]["score"]
    reasons: List[str] = []
    direction = "WAIT"
    if avg >= 30:
        if top_score < -15:
            reasons.append(f"Higher timeframe ({top_tf}) is bearish, which conflicts with the lower-timeframe bullish score")
        elif n_long >= max(1, (len(per_tf) + 1) // 2):
            direction = "LONG"
    elif avg <= -30:
        if top_score > 15:
            reasons.append(f"Higher timeframe ({top_tf}) is bullish, which conflicts with the lower-timeframe bearish score")
        elif n_short >= max(1, (len(per_tf) + 1) // 2):
            direction = "SHORT"
    if direction == "WAIT" and not reasons:
        reasons.append("Timeframes do not agree strongly enough, or the combined score is too weak")
    base = per_tf[primary] if primary in per_tf else per_tf[next(iter(per_tf))]
    agree = n_long if direction == "LONG" else n_short if direction == "SHORT" else 0
    confidence = None
    if direction != "WAIT":
        confidence = abs(avg) * 0.6 + (agree / len(per_tf)) * 25 + (8 if base["volume_confirms"] else 0)
        if base["trend_strength"] == "weak/ranging":
            confidence -= 10
        if base["volatility"]["state"] == "high":
            confidence -= 5
        confidence = int(max(5, min(90, confidence)))
        reasons += [f"{tf}: {a['direction']} (score {a['score']})" for tf, a in per_tf.items()]
        reasons += base["reasons"][:6]
    levels = None
    if direction != "WAIT" and base["atr"]:
        entry = base["price"]
        risk = base["atr"] * sl_atr_mult
        sgn = 1 if direction == "LONG" else -1
        sl = entry - sgn * risk
        levels = {
            "entry": entry, "stop_loss": sl,
            "target_1": entry + sgn * risk, "target_2": entry + sgn * 2 * risk, "target_3": entry + sgn * 3 * risk,
            "risk_reward": {"target_1": 1.0, "target_2": 2.0, "target_3": 3.0},
            "risk_per_unit": risk, "basis": f"Stop = {sl_atr_mult} x ATR(14) on {primary}; targets at 1R/2R/3R",
        }
    return {
        "signal": direction, "confidence": confidence, "combined_score": round(avg, 1), "primary_timeframe": primary,
        "levels": levels, "timeframes": per_tf, "skipped_timeframes": skipped, "reasons": reasons,
        "disclaimer": DISCLAIMER,
    }


def market_overview(candles: List[Dict[str, float]]) -> Dict[str, Any]:
    """Trend, S/R, volatility and fibonacci context for a single timeframe (used by the chart panel)."""
    a = analyze_timeframe(candles)
    h, l, c, _ = _cols(candles)
    sr = ind.support_resistance(h, l, c)
    window = candles[-100:]
    hi = max(x["h"] for x in window)
    lo = min(x["l"] for x in window)
    fib = ind.fibonacci(hi, lo, "up" if a["score"] >= 0 else "down") if hi > lo else {}
    return {"analysis": a, "support_resistance": sr, "fibonacci": fib, "swing_high": hi, "swing_low": lo,
            "trend_lines": ind.trend_lines(h, l), "disclaimer": DISCLAIMER}
