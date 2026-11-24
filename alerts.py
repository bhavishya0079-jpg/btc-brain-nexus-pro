"""Alert evaluation (pure logic). The scheduler in services.py supplies live data and delivers events."""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .strategy import Series, compute_series, eval_conditions, normalize_spec, validate_spec

ALERT_TYPES = ["price", "indicator", "strategy", "breakout", "stop_loss", "target", "volume", "custom"]
DEFAULT_COOLDOWN = 300


def validate_alert(kind: str, params: Dict[str, Any], symbol: Optional[str], interval: Optional[str]) -> None:
    if kind not in ALERT_TYPES:
        raise ValueError(f"type must be one of {ALERT_TYPES}")
    if kind in ("price", "indicator", "breakout", "volume", "custom", "strategy") and not symbol:
        raise ValueError("symbol is required")
    if kind == "price":
        if params.get("condition") not in ("above", "below") or not isinstance(params.get("value"), (int, float)):
            raise ValueError("price alert needs condition ('above'|'below') and numeric value")
    elif kind in ("indicator", "custom"):
        spec = {"name": "a", "timeframe": "15m", "side": "long", "indicators": params.get("indicators", []),
                "entry_long": params.get("conditions", []), "stop_loss": {"type": "percent", "value": 1}}
        errors, _ = validate_spec(spec)
        if errors:
            raise ValueError("; ".join(errors))
    elif kind == "breakout":
        if not isinstance(params.get("lookback", 20), int) or not 2 <= params.get("lookback", 20) <= 500:
            raise ValueError("lookback must be an integer 2-500")
        if params.get("direction", "any") not in ("up", "down", "any"):
            raise ValueError("direction must be up, down or any")
    elif kind == "volume":
        if not isinstance(params.get("multiplier", 2.0), (int, float)) or params.get("multiplier", 2.0) <= 1:
            raise ValueError("multiplier must be a number greater than 1")
    elif kind == "strategy":
        if not isinstance(params.get("strategy_id"), int):
            raise ValueError("strategy alert needs an integer strategy_id")
    elif kind in ("stop_loss", "target"):
        if not isinstance(params.get("position_id"), int):
            raise ValueError("needs an integer position_id")
    cd = params.get("cooldown_sec", DEFAULT_COOLDOWN)
    if not isinstance(cd, (int, float)) or not 0 <= cd <= 86400:
        raise ValueError("cooldown_sec must be 0-86400")


def evaluate(kind: str, params: Dict[str, Any], *, candles: Optional[List[Dict[str, float]]] = None,
             price: Optional[float] = None, strategy_spec: Optional[Dict[str, Any]] = None,
             position: Optional[Dict[str, Any]] = None, since_ms: int = 0) -> Optional[Dict[str, Any]]:
    """Return {'message':..., 'data':...} when the alert condition is met, else None."""
    if kind == "price":
        if price is None:
            return None
        v = params["value"]
        if (params["condition"] == "above" and price >= v) or (params["condition"] == "below" and price <= v):
            return {"message": f"Price {price:g} is {params['condition']} {v:g}", "data": {"price": price}}
        return None
    if kind in ("indicator", "custom"):
        if not candles:
            return None
        spec = {"indicators": params.get("indicators", []), "entry_long": params.get("conditions", [])}
        s = Series(candles)
        series = compute_series(spec, s)
        if eval_conditions(spec["entry_long"], series, s.n - 1):
            return {"message": "Indicator conditions met", "data": {"price": float(s.close[-1])}}
        return None
    if kind == "breakout":
        if not candles:
            return None
        n = int(params.get("lookback", 20))
        if len(candles) < n + 2:
            return None
        prev = candles[-n - 1:-1]
        hi, lo = max(c["h"] for c in prev), min(c["l"] for c in prev)
        last = candles[-1]["c"]
        d = params.get("direction", "any")
        if last > hi and d in ("up", "any"):
            return {"message": f"Breakout above {n}-bar high {hi:g} (price {last:g})", "data": {"level": hi, "price": last}}
        if last < lo and d in ("down", "any"):
            return {"message": f"Breakdown below {n}-bar low {lo:g} (price {last:g})", "data": {"level": lo, "price": last}}
        return None
    if kind == "volume":
        if not candles:
            return None
        n = int(params.get("period", 20))
        if len(candles) < n + 2:
            return None
        avg = float(np.mean([c["v"] for c in candles[-n - 1:-1]]))
        mult = float(params.get("multiplier", 2.0))
        if avg > 0 and candles[-1]["v"] >= mult * avg:
            return {"message": f"Volume {candles[-1]['v'] / avg:.1f}x average", "data": {"ratio": candles[-1]["v"] / avg}}
        return None
    if kind == "strategy":
        if not candles or not strategy_spec:
            return None
        spec = normalize_spec(strategy_spec)
        s = Series(candles)
        series = compute_series(spec, s)
        i = s.n - 2  # last CLOSED bar
        if i < 1:
            return None
        if spec["side"] in ("long", "both") and eval_conditions(spec["entry_long"], series, i):
            return {"message": f"Strategy '{spec['name']}': LONG entry conditions met", "data": {"side": "LONG", "bar_t": int(s.t[i])}}
        if spec["side"] in ("short", "both") and eval_conditions(spec["entry_short"], series, i):
            return {"message": f"Strategy '{spec['name']}': SHORT entry conditions met", "data": {"side": "SHORT", "bar_t": int(s.t[i])}}
        return None
    if kind in ("stop_loss", "target"):
        if not position:
            return None
        prefix = "stop_loss" if kind == "stop_loss" else "target"
        for ex in position.get("exits", []):
            if ex["reason"].startswith(prefix) and ex["t"] > since_ms:
                return {"message": f"Paper position #{position['id']} {position['symbol']}: {ex['reason']} filled at {ex['price']:g}",
                        "data": {"position_id": position["id"], "fill": ex}}
        return None
    return None
