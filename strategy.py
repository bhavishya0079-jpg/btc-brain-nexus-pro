"""Declarative strategies: JSON spec -> validation -> indicator series -> condition evaluation.

A strategy is DATA, never code. There is no eval/exec anywhere; rules are comparisons between
named series, so imported strategies cannot execute anything on the server.
Strategies can only be backtested / paper traded - there is no live execution path at all.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ..providers.base import INTERVAL_SECONDS
from . import indicators as ind

OHLCV = ("open", "high", "low", "close", "volume")
OPS = (">", "<", ">=", "<=", "crosses_above", "crosses_below")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_INDICATORS, MAX_CONDITIONS = 20, 12

# type -> (allowed params with (default, min, max), output fields, primary field)
INDICATOR_TYPES: Dict[str, Dict[str, Any]] = {
    "sma": {"params": {"period": (20, 1, 500)}, "fields": ["value"], "uses_source": True},
    "ema": {"params": {"period": (20, 1, 500)}, "fields": ["value"], "uses_source": True},
    "rsi": {"params": {"period": (14, 2, 200)}, "fields": ["value"], "uses_source": True},
    "macd": {"params": {"fast": (12, 2, 200), "slow": (26, 3, 400), "signal": (9, 2, 100)},
             "fields": ["macd", "signal", "hist"], "primary": "macd", "uses_source": True},
    "bollinger": {"params": {"period": (20, 2, 300), "k": (2, 0.5, 5)}, "fields": ["mid", "upper", "lower", "width"],
                  "primary": "mid", "uses_source": True},
    "atr": {"params": {"period": (14, 1, 200)}, "fields": ["value"]},
    "vwap": {"params": {}, "fields": ["value"]},
    "supertrend": {"params": {"period": (10, 1, 100), "mult": (3, 0.5, 10)}, "fields": ["line", "direction"], "primary": "line"},
    "adx": {"params": {"period": (14, 2, 100)}, "fields": ["adx", "plus_di", "minus_di"], "primary": "adx"},
    "stochastic": {"params": {"k": (14, 2, 100), "d": (3, 1, 50), "smooth": (3, 1, 50)}, "fields": ["k", "d"], "primary": "k"},
    "highest": {"params": {"period": (20, 1, 500), "shift": (1, 0, 50)}, "fields": ["value"], "uses_source": True},
    "lowest": {"params": {"period": (20, 1, 500), "shift": (1, 0, 50)}, "fields": ["value"], "uses_source": True},
}
SOURCES = ("open", "high", "low", "close", "volume")


class Series:
    """Candles as numpy columns."""

    def __init__(self, candles: List[Dict[str, float]]):
        self.t = np.array([c["t"] for c in candles], dtype=np.int64)
        self.open = np.array([c["o"] for c in candles], dtype=float)
        self.high = np.array([c["h"] for c in candles], dtype=float)
        self.low = np.array([c["l"] for c in candles], dtype=float)
        self.close = np.array([c["c"] for c in candles], dtype=float)
        self.volume = np.array([c["v"] for c in candles], dtype=float)
        self.n = len(candles)


def _shifted_extreme(values: np.ndarray, period: int, shift: int, fn) -> np.ndarray:
    out = np.full(len(values), np.nan)
    for i in range(period + shift - 1, len(values)):
        window = values[i - shift - period + 1:i - shift + 1]
        out[i] = fn(window)
    return out


def compute_indicator(spec: Dict[str, Any], s: Series) -> Dict[str, np.ndarray]:
    t = spec["type"]
    p = {k: spec.get("params", {}).get(k, v[0]) for k, v in INDICATOR_TYPES[t]["params"].items()}
    src = getattr(s, spec.get("source", "close")) if INDICATOR_TYPES[t].get("uses_source") else None
    if t == "sma":
        return {"value": ind.sma(src, int(p["period"]))}
    if t == "ema":
        return {"value": ind.ema(src, int(p["period"]))}
    if t == "rsi":
        return {"value": ind.rsi(src, int(p["period"]))}
    if t == "macd":
        return ind.macd(src, int(p["fast"]), int(p["slow"]), int(p["signal"]))
    if t == "bollinger":
        return ind.bollinger(src, int(p["period"]), float(p["k"]))
    if t == "atr":
        return {"value": ind.atr(s.high, s.low, s.close, int(p["period"]))}
    if t == "vwap":
        return {"value": ind.vwap(s.high, s.low, s.close, s.volume, s.t)}
    if t == "supertrend":
        return ind.supertrend(s.high, s.low, s.close, int(p["period"]), float(p["mult"]))
    if t == "adx":
        return ind.adx(s.high, s.low, s.close, int(p["period"]))
    if t == "stochastic":
        return ind.stochastic(s.high, s.low, s.close, int(p["k"]), int(p["d"]), int(p["smooth"]))
    if t == "highest":
        return {"value": _shifted_extreme(src, int(p["period"]), int(p["shift"]), np.max)}
    if t == "lowest":
        return {"value": _shifted_extreme(src, int(p["period"]), int(p["shift"]), np.min)}
    raise ValueError(f"unknown indicator {t}")


def compute_series(spec: Dict[str, Any], s: Series) -> Dict[str, np.ndarray]:
    """Return name -> array for every OHLCV column and indicator output ('id' and 'id.field')."""
    out: Dict[str, np.ndarray] = {k: getattr(s, k) for k in OHLCV}
    for item in spec.get("indicators", []):
        fields = compute_indicator(item, s)
        meta = INDICATOR_TYPES[item["type"]]
        primary = meta.get("primary", meta["fields"][0])
        for fname, arr in fields.items():
            out[f"{item['id']}.{fname}"] = arr
        out[item["id"]] = fields[primary]
    return out


# ----------------------------------------------------------------------- conditions
def _operand(series: Dict[str, np.ndarray], x: Any, i: int) -> float:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(x)
    arr = series[x]
    return float(arr[i]) if 0 <= i < len(arr) else float("nan")


def eval_condition(cond: Dict[str, Any], series: Dict[str, np.ndarray], i: int) -> bool:
    op = cond["op"]
    l_now, r_now = _operand(series, cond["left"], i), _operand(series, cond["right"], i)
    if np.isnan(l_now) or np.isnan(r_now):
        return False
    if op in ("crosses_above", "crosses_below"):
        if i < 1:
            return False
        l_prev, r_prev = _operand(series, cond["left"], i - 1), _operand(series, cond["right"], i - 1)
        if np.isnan(l_prev) or np.isnan(r_prev):
            return False
        if op == "crosses_above":
            return l_prev <= r_prev and l_now > r_now
        return l_prev >= r_prev and l_now < r_now
    if op == ">":
        return l_now > r_now
    if op == "<":
        return l_now < r_now
    if op == ">=":
        return l_now >= r_now
    return l_now <= r_now


def eval_conditions(conds: List[Dict[str, Any]], series: Dict[str, np.ndarray], i: int) -> bool:
    """All conditions must hold (AND). An empty list is False (a rule set must say something)."""
    return bool(conds) and all(eval_condition(c, series, i) for c in conds)


# ---------------------------------------------------------------------- validation
def default_spec() -> Dict[str, Any]:
    return {
        "name": "EMA crossover + RSI filter", "timeframe": "15m", "side": "both",
        "indicators": [
            {"id": "ema_fast", "type": "ema", "params": {"period": 9}},
            {"id": "ema_slow", "type": "ema", "params": {"period": 21}},
            {"id": "rsi", "type": "rsi", "params": {"period": 14}},
            {"id": "atr", "type": "atr", "params": {"period": 14}},
        ],
        "entry_long": [{"left": "ema_fast", "op": "crosses_above", "right": "ema_slow"}, {"left": "rsi", "op": ">", "right": 50}],
        "entry_short": [{"left": "ema_fast", "op": "crosses_below", "right": "ema_slow"}, {"left": "rsi", "op": "<", "right": 50}],
        "exit_long": [], "exit_short": [],
        "stop_loss": {"type": "atr", "period": 14, "mult": 2.0},
        "targets": [{"rr": 1.5, "qty_pct": 50}, {"rr": 3.0, "qty_pct": 50}],
        "risk": {"risk_per_trade_pct": 1.0, "max_position_pct": 100, "max_bars_in_trade": None},
    }


def _known_names(spec: Dict[str, Any]) -> set:
    names = set(OHLCV)
    for item in spec.get("indicators", []):
        t = item.get("type")
        if t in INDICATOR_TYPES and isinstance(item.get("id"), str):
            names.add(item["id"])
            for f in INDICATOR_TYPES[t]["fields"]:
                names.add(f"{item['id']}.{f}")
    return names


def validate_spec(spec: Any) -> Tuple[List[str], List[str]]:
    """Returns (errors, warnings). A spec with errors must never be backtested or saved as active."""
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(spec, dict):
        return ["Strategy must be a JSON object"], []
    name = spec.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        errors.append("name: required, 1-120 characters")
    if spec.get("timeframe") not in INTERVAL_SECONDS:
        errors.append(f"timeframe: must be one of {list(INTERVAL_SECONDS)}")
    side = spec.get("side")
    if side not in ("long", "short", "both"):
        errors.append("side: must be long, short or both")
    inds = spec.get("indicators", [])
    if not isinstance(inds, list) or len(inds) > MAX_INDICATORS:
        errors.append(f"indicators: must be a list of at most {MAX_INDICATORS}")
        inds = []
    seen = set()
    for n, item in enumerate(inds):
        label = f"indicators[{n}]"
        if not isinstance(item, dict):
            errors.append(f"{label}: must be an object")
            continue
        iid, typ = item.get("id"), item.get("type")
        if not isinstance(iid, str) or not ID_RE.match(iid):
            errors.append(f"{label}.id: lowercase letters/digits/_ , start with a letter")
        elif iid in seen or iid in OHLCV:
            errors.append(f"{label}.id: '{iid}' is duplicated or reserved")
        else:
            seen.add(iid)
        if typ not in INDICATOR_TYPES:
            errors.append(f"{label}.type: unknown '{typ}'. Allowed: {sorted(INDICATOR_TYPES)}")
            continue
        meta = INDICATOR_TYPES[typ]
        params = item.get("params", {})
        if not isinstance(params, dict):
            errors.append(f"{label}.params: must be an object")
            continue
        for key, value in params.items():
            if key not in meta["params"]:
                errors.append(f"{label}.params.{key}: not valid for {typ}")
                continue
            _, lo, hi = meta["params"][key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                errors.append(f"{label}.params.{key}: number between {lo} and {hi} required")
        if typ == "macd" and params.get("fast", 12) >= params.get("slow", 26):
            errors.append(f"{label}: macd fast must be < slow")
        if "source" in item and (item["source"] not in SOURCES or not meta.get("uses_source")):
            errors.append(f"{label}.source: invalid for {typ}")
    names = _known_names({"indicators": [i for i in inds if isinstance(i, dict)]})

    def check_rules(key: str) -> None:
        rules = spec.get(key, [])
        if not isinstance(rules, list) or len(rules) > MAX_CONDITIONS:
            errors.append(f"{key}: must be a list of at most {MAX_CONDITIONS} conditions")
            return
        for n, c in enumerate(rules):
            label = f"{key}[{n}]"
            if not isinstance(c, dict) or c.get("op") not in OPS:
                errors.append(f"{label}: needs op in {list(OPS)}")
                continue
            for side_key in ("left", "right"):
                v = c.get(side_key)
                if isinstance(v, bool) or not (isinstance(v, (int, float)) or (isinstance(v, str) and v in names)):
                    errors.append(f"{label}.{side_key}: must be a number or a known series ({side_key}={v!r})")

    for key in ("entry_long", "entry_short", "exit_long", "exit_short"):
        check_rules(key)
    if side in ("long", "both") and not spec.get("entry_long"):
        errors.append("entry_long: at least one entry condition is required for long trading")
    if side in ("short", "both") and not spec.get("entry_short"):
        errors.append("entry_short: at least one entry condition is required for short trading")

    sl = spec.get("stop_loss")
    if not isinstance(sl, dict) or sl.get("type") not in ("atr", "percent"):
        errors.append("stop_loss: required - {type:'atr',period,mult} or {type:'percent',value}")
    elif sl["type"] == "atr":
        if not isinstance(sl.get("mult"), (int, float)) or not 0.2 <= sl["mult"] <= 15:
            errors.append("stop_loss.mult: 0.2 - 15")
        if not isinstance(sl.get("period", 14), int) or not 1 <= sl.get("period", 14) <= 200:
            errors.append("stop_loss.period: integer 1 - 200")
    else:
        if not isinstance(sl.get("value"), (int, float)) or not 0.05 <= sl["value"] <= 50:
            errors.append("stop_loss.value: percent between 0.05 and 50")

    targets = spec.get("targets", [])
    if not isinstance(targets, list) or len(targets) > 3:
        errors.append("targets: at most 3 targets")
    else:
        total = 0.0
        last_rr = 0.0
        for n, tg in enumerate(targets):
            if not isinstance(tg, dict) or not isinstance(tg.get("rr"), (int, float)) or not 0.1 <= tg["rr"] <= 50:
                errors.append(f"targets[{n}].rr: 0.1 - 50")
                continue
            if tg["rr"] <= last_rr:
                errors.append(f"targets[{n}].rr: targets must be in increasing order")
            last_rr = tg["rr"]
            q = tg.get("qty_pct", 100.0 / len(targets))
            if not isinstance(q, (int, float)) or not 0 < q <= 100:
                errors.append(f"targets[{n}].qty_pct: 1 - 100")
            else:
                total += q
        if total > 100.0001:
            errors.append("targets: qty_pct values add up to more than 100")
    if not targets and not spec.get("exit_long") and not spec.get("exit_short"):
        warnings.append("No targets and no exit rules: trades only exit via stop loss or end of data")

    risk = spec.get("risk", {})
    if not isinstance(risk, dict):
        errors.append("risk: must be an object")
    else:
        rp = risk.get("risk_per_trade_pct", 1.0)
        if not isinstance(rp, (int, float)) or not 0 < rp <= 10:
            errors.append("risk.risk_per_trade_pct: greater than 0 and at most 10")
        elif rp > 3:
            warnings.append("risk_per_trade_pct above 3% is aggressive")
        mp = risk.get("max_position_pct", 100)
        if not isinstance(mp, (int, float)) or not 0 < mp <= 100:
            errors.append("risk.max_position_pct: greater than 0 and at most 100 (no leverage)")
        mb = risk.get("max_bars_in_trade")
        if mb is not None and (not isinstance(mb, int) or isinstance(mb, bool) or not 1 <= mb <= 100000):
            errors.append("risk.max_bars_in_trade: integer or null")
    return errors, warnings


def normalize_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Fill defaults so downstream code can rely on every key."""
    s = copy.deepcopy(spec)
    s.setdefault("indicators", [])
    for key in ("entry_long", "entry_short", "exit_long", "exit_short", "targets"):
        s.setdefault(key, [])
    s.setdefault("risk", {})
    s["risk"].setdefault("risk_per_trade_pct", 1.0)
    s["risk"].setdefault("max_position_pct", 100)
    s["risk"].setdefault("max_bars_in_trade", None)
    n = len(s["targets"])
    for tg in s["targets"]:
        tg.setdefault("qty_pct", 100.0 / n if n else 100.0)
    for item in s["indicators"]:
        item.setdefault("params", {})
    return s


def required_warmup(spec: Dict[str, Any]) -> int:
    """Rough number of bars before every indicator is defined."""
    warm = 2
    for item in spec.get("indicators", []):
        p = item.get("params", {})
        vals = [v for v in p.values() if isinstance(v, (int, float))]
        warm = max(warm, int(max(vals)) * 3 if vals else 2)
        if item.get("type") == "macd":
            warm = max(warm, int(p.get("slow", 26) + p.get("signal", 9)) * 2)
    sl = spec.get("stop_loss") or {}
    if sl.get("type") == "atr":
        warm = max(warm, int(sl.get("period", 14)) * 2)
    return warm
