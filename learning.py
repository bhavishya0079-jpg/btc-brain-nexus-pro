"""AI Learning System: learns from past market data and past trades, proposes strategy changes,
and VERIFIES them out-of-sample before anything may be applied.

What "learning" means here (deliberately transparent, no hidden model):
  1. Backtest the base strategy; split history chronologically into TRAIN (first part) and TEST (last part).
  2. Study the TRAIN trades: which market conditions / sides / exits lose money.
  3. Generate candidate rule changes (extra filters, side restriction, stop/target parameter changes).
  4. Re-run every candidate on TRAIN and, separately, on the untouched TEST segment.
  5. A candidate is only labelled VALIDATED_OUT_OF_SAMPLE if it beats the baseline on TEST too.
Nothing is applied automatically: a human approves, every approval is audited and can be rolled back.
The number of candidates tried is reported because testing many ideas inflates the chance of a lucky one.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import indicators as ind
from .backtest import run_backtest
from .strategy import normalize_spec, required_warmup, validate_spec

MIN_TRAIN_TRADES = 20
MIN_TEST_TRADES = 8
MIN_BUCKET_TRADES = 8


# --------------------------------------------------------------------- market regimes
def analyze_market_regimes(candles: List[Dict[str, float]]) -> Dict[str, Any]:
    if len(candles) < 120:
        raise ValueError("Need at least 120 candles for regime analysis")
    h = np.array([c["h"] for c in candles]); l = np.array([c["l"] for c in candles]); c_ = np.array([c["c"] for c in candles])
    t = np.array([c["t"] for c in candles])
    adx = ind.adx(h, l, c_, 14)["adx"]
    atr_pct = ind.atr(h, l, c_, 14) / c_ * 100.0
    ema50 = ind.ema(c_, 50)
    fwd = np.full(len(c_), np.nan)
    fwd[:-1] = (c_[1:] / c_[:-1] - 1) * 100.0
    valid = ~np.isnan(adx) & ~np.isnan(atr_pct) & ~np.isnan(ema50)
    vol_med = float(np.nanmedian(atr_pct[valid]))
    regimes: Dict[str, List[float]] = {"trending_up": [], "trending_down": [], "ranging": []}
    for i in np.where(valid)[0]:
        if adx[i] >= 25:
            regimes["trending_up" if c_[i] > ema50[i] else "trending_down"].append(fwd[i])
        else:
            regimes["ranging"].append(fwd[i])
    total = sum(len(v) for v in regimes.values()) or 1
    by_regime = {k: {"share_pct": len(v) / total * 100.0, "avg_next_bar_return_pct": float(np.nanmean(v)) if v else None}
                 for k, v in regimes.items()}
    hours: Dict[int, List[float]] = {}
    for i in np.where(valid)[0]:
        hours.setdefault(int((t[i] // 3_600_000) % 24), []).append(atr_pct[i])
    hour_vol = {h_: float(np.mean(v)) for h_, v in sorted(hours.items())}
    return {"candles": len(candles), "regimes": by_regime, "median_atr_pct": vol_med,
            "volatility_by_utc_hour": hour_vol,
            "high_volatility_share_pct": float(np.mean(atr_pct[valid] > 1.5 * vol_med) * 100.0)}


# ---------------------------------------------------------------------- trade features
def _entry_features(spec: Dict[str, Any], candles: List[Dict[str, float]], trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    h = np.array([c["h"] for c in candles]); l = np.array([c["l"] for c in candles])
    c_ = np.array([c["c"] for c in candles]); v = np.array([c["v"] for c in candles])
    t_index = {c["t"]: i for i, c in enumerate(candles)}
    adx = ind.adx(h, l, c_, 14)["adx"]
    atr_pct = ind.atr(h, l, c_, 14) / c_ * 100.0
    ema50 = ind.ema(c_, 50)
    vol_avg = ind.sma(v, 20)
    rows = []
    for tr in trades:
        i = t_index.get(tr["entry_t"])
        if i is None or i < 1:
            continue
        j = i - 1  # features known BEFORE the entry bar opened
        rows.append({
            "trade": tr, "adx": None if np.isnan(adx[j]) else float(adx[j]),
            "atr_pct": None if np.isnan(atr_pct[j]) else float(atr_pct[j]),
            "vol_ratio": None if np.isnan(vol_avg[j]) or vol_avg[j] == 0 else float(v[j] / vol_avg[j]),
            "with_trend": None if np.isnan(ema50[j]) else bool((c_[j] > ema50[j]) == (tr["side"] == "LONG")),
            "hour_block": int((tr["entry_t"] // 3_600_000) % 24) // 6, "side": tr["side"],
        })
    return rows


def _bucket_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnls = [r["trade"]["pnl"] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    return {"n": len(rows), "win_rate_pct": len(wins) / len(rows) * 100.0 if rows else None,
            "expectancy": float(np.mean(pnls)) if pnls else None,
            "profit_factor": sum(wins) / sum(losses) if losses else None, "net": float(sum(pnls))}


def analyze_trades(spec: Dict[str, Any], candles: List[Dict[str, float]], trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    feats = _entry_features(spec, candles, trades)
    findings: List[Dict[str, Any]] = []
    overall = _bucket_stats(feats)

    def check(name: str, label_bad: str, bad: List[Dict[str, Any]], good: List[Dict[str, Any]], fix: Optional[str]) -> None:
        if len(bad) < MIN_BUCKET_TRADES:
            return
        b, g = _bucket_stats(bad), _bucket_stats(good)
        if b["expectancy"] is not None and b["expectancy"] < 0 and (g["expectancy"] is None or b["expectancy"] < g["expectancy"]):
            findings.append({"id": name, "weakness": label_bad, "bad": b, "rest": g, "suggested_fix": fix})

    f_adx = [f for f in feats if f["adx"] is not None]
    check("low_adx", "Trades taken in weak/ranging markets (ADX < 20) lose money",
          [f for f in f_adx if f["adx"] < 20], [f for f in f_adx if f["adx"] >= 20], "add_adx_filter")
    f_vol = [f for f in feats if f["vol_ratio"] is not None]
    check("low_volume", "Entries on below-average volume lose money",
          [f for f in f_vol if f["vol_ratio"] < 1.0], [f for f in f_vol if f["vol_ratio"] >= 1.0], "add_volume_filter")
    f_tr = [f for f in feats if f["with_trend"] is not None]
    check("counter_trend", "Entries against the EMA50 trend lose money",
          [f for f in f_tr if not f["with_trend"]], [f for f in f_tr if f["with_trend"]], "add_trend_filter")
    for side, fix in (("LONG", "disable_long"), ("SHORT", "disable_short")):
        check(f"side_{side.lower()}", f"{side} trades lose money", [f for f in feats if f["side"] == side],
              [f for f in feats if f["side"] != side], fix)
    reasons: Dict[str, int] = {}
    for f in feats:
        reasons[f["trade"]["exit_reason"] or "?"] = reasons.get(f["trade"]["exit_reason"] or "?", 0) + 1
    n = max(1, len(feats))
    stop_rate = reasons.get("stop_loss", 0) / n
    if stop_rate >= 0.6 and len(feats) >= MIN_BUCKET_TRADES:
        findings.append({"id": "stop_rate", "weakness": f"{stop_rate * 100:.0f}% of trades end at the stop loss", "suggested_fix": "tune_stop"})
    return {"overall": overall, "exit_reasons": reasons, "findings": findings, "trades_analyzed": len(feats)}


# ------------------------------------------------------------------------- candidates
def _ensure_ind(spec: Dict[str, Any], iid: str, typ: str, params: Dict[str, Any], source: Optional[str] = None) -> None:
    if not any(i["id"] == iid for i in spec["indicators"]):
        item: Dict[str, Any] = {"id": iid, "type": typ, "params": params}
        if source:
            item["source"] = source
        spec["indicators"].append(item)


def _add_condition(spec: Dict[str, Any], cond_long: Dict[str, Any], cond_short: Optional[Dict[str, Any]] = None) -> None:
    spec["entry_long"].append(cond_long)
    spec["entry_short"].append(cond_short if cond_short else cond_long)


def make_candidates(spec_in: Dict[str, Any], analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    base = normalize_spec(spec_in)
    out: List[Dict[str, Any]] = []

    def new(note: str, rationale: str, evidence: Any) -> Dict[str, Any]:
        s = copy.deepcopy(base)
        s["name"] = (base["name"] + " (AI v)")[:120]
        return {"spec": s, "note": note, "rationale": rationale, "evidence": evidence}

    for f in analysis["findings"]:
        fix = f.get("suggested_fix")
        if fix == "add_adx_filter":
            c = new("Add ADX(14) > 20 filter", f["weakness"], f)
            _ensure_ind(c["spec"], "ai_adx", "adx", {"period": 14})
            _add_condition(c["spec"], {"left": "ai_adx", "op": ">", "right": 20})
            out.append(c)
        elif fix == "add_volume_filter":
            c = new("Require volume above its 20-bar average", f["weakness"], f)
            _ensure_ind(c["spec"], "ai_volavg", "sma", {"period": 20}, source="volume")
            _add_condition(c["spec"], {"left": "volume", "op": ">", "right": "ai_volavg"})
            out.append(c)
        elif fix == "add_trend_filter":
            c = new("Trade only with the EMA50 trend", f["weakness"], f)
            _ensure_ind(c["spec"], "ai_ema50", "ema", {"period": 50})
            c["spec"]["entry_long"].append({"left": "close", "op": ">", "right": "ai_ema50"})
            c["spec"]["entry_short"].append({"left": "close", "op": "<", "right": "ai_ema50"})
            out.append(c)
        elif fix in ("disable_long", "disable_short") and base["side"] == "both":
            c = new("Stop trading the " + ("long" if fix == "disable_long" else "short") + " side", f["weakness"], f)
            c["spec"]["side"] = "short" if fix == "disable_long" else "long"
            out.append(c)
        elif fix == "tune_stop" and base["stop_loss"]["type"] == "atr":
            c = new("Widen ATR stop by 0.5", f["weakness"], f)
            c["spec"]["stop_loss"]["mult"] = round(min(15, base["stop_loss"]["mult"] + 0.5), 2)
            out.append(c)
    # parameter sweeps (small, bounded grids)
    if base["stop_loss"]["type"] == "atr":
        for m in (1.0, 1.5, 2.0, 2.5, 3.0):
            if abs(m - base["stop_loss"]["mult"]) > 1e-9:
                c = new(f"ATR stop multiple {m}", "Parameter sweep: stop distance", {"sweep": "atr_mult", "value": m})
                c["spec"]["stop_loss"]["mult"] = m
                out.append(c)
    if base["targets"]:
        for rr in (1.5, 2.0, 3.0):
            c = new(f"Single target at {rr}R", "Parameter sweep: target distance", {"sweep": "target_rr", "value": rr})
            c["spec"]["targets"] = [{"rr": rr, "qty_pct": 100.0}]
            out.append(c)
    valid = []
    for c in out:
        errors, _ = validate_spec(c["spec"])
        if not errors and c["spec"] != base:
            valid.append(c)
    return valid[:14]


# ---------------------------------------------------------------------------- scoring
def _summary(res: Dict[str, Any]) -> Dict[str, Any]:
    s = res["stats"]
    return {k: s[k] for k in ("trades", "win_rate_pct", "profit_factor", "net_profit", "return_pct", "max_drawdown_pct", "expectancy", "avg_r_multiple")}


def _split(candles: List[Dict[str, float]], spec: Dict[str, Any], train_frac: float) -> Tuple[List, List]:
    split = int(len(candles) * train_frac)
    warm = required_warmup(spec) + 5
    return candles[:split], candles[max(0, split - warm):]


def _score(st: Dict[str, Any]) -> float:
    pf = st["profit_factor"] if st["profit_factor"] is not None else (3.0 if st["trades"] else 0.0)
    return min(pf, 5.0) + (st["expectancy"] or 0.0) * 0 + (0.0 if st["trades"] else -1.0)


def verdict_for(base_test: Dict[str, Any], cand_test: Dict[str, Any], base_train: Dict[str, Any], cand_train: Dict[str, Any]) -> Dict[str, Any]:
    reasons = []
    if cand_test["trades"] < MIN_TEST_TRADES:
        return {"verdict": "INSUFFICIENT_DATA", "reasons": [f"Only {cand_test['trades']} test trades (need {MIN_TEST_TRADES})"]}
    better_train = _score(cand_train) > _score(base_train)
    if not better_train:
        reasons.append("Does not beat the baseline on the training segment")
    better_pf = _score(cand_test) > _score(base_test) + 0.05
    if not better_pf:
        reasons.append("Profit factor is not clearly better than baseline on unseen test data")
    better_net = cand_test["net_profit"] >= base_test["net_profit"]
    if not better_net:
        reasons.append("Net profit on test data is lower than baseline")
    dd_ok = cand_test["max_drawdown_pct"] <= base_test["max_drawdown_pct"] * 1.25 + 1.0
    if not dd_ok:
        reasons.append("Drawdown on test data is materially worse")
    ok = better_train and better_pf and better_net and dd_ok and cand_test["net_profit"] > 0
    if cand_test["net_profit"] <= 0:
        reasons.append("Candidate is not profitable on test data")
    return {"verdict": "VALIDATED_OUT_OF_SAMPLE" if ok else "NOT_VALIDATED",
            "reasons": reasons or ["Beat the baseline on both training and unseen test data"]}


def run_learning(spec_in: Dict[str, Any], candles: List[Dict[str, float]], *, train_frac: float = 0.7,
                 initial_capital: float = 100000.0, fee_pct: float = 0.05, slippage_pct: float = 0.02) -> Dict[str, Any]:
    errors, _ = validate_spec(spec_in)
    if errors:
        raise ValueError("Invalid strategy: " + "; ".join(errors))
    if not 0.5 <= train_frac <= 0.9:
        raise ValueError("train_frac must be between 0.5 and 0.9")
    base = normalize_spec(spec_in)
    kw = dict(initial_capital=initial_capital, fee_pct=fee_pct, slippage_pct=slippage_pct)
    train_c, test_c = _split(candles, base, train_frac)
    base_train = run_backtest(base, train_c, **kw)
    base_test = run_backtest(base, test_c, **kw)
    out: Dict[str, Any] = {
        "baseline": {"train": _summary(base_train), "test": _summary(base_test)},
        "split": {"train_candles": len(train_c), "test_candles": len(test_c), "train_frac": train_frac},
        "market_regimes": analyze_market_regimes(train_c) if len(train_c) >= 120 else None,
        "candidates_tested": 0, "proposals": [], "status": "ok",
        "notes": ["Candidates are validated on a held-out test segment the search never saw.",
                  "Many candidates are tried, so a validated result can still be luck; confirm in paper trading before relying on it."],
    }
    if base_train["stats"]["trades"] < MIN_TRAIN_TRADES:
        out["status"] = "insufficient_data"
        out["notes"].insert(0, f"Only {base_train['stats']['trades']} training trades (need {MIN_TRAIN_TRADES}). Use more history or a faster timeframe.")
        out["analysis"] = analyze_trades(base, train_c, base_train["trades"])
        return out
    analysis = analyze_trades(base, train_c, base_train["trades"])
    out["analysis"] = {**analysis, "weaknesses": [f["weakness"] for f in analysis["findings"]]}
    cands = make_candidates(base, analysis)
    out["candidates_tested"] = len(cands)
    for c in cands:
        try:
            tr, te = run_backtest(c["spec"], train_c, **kw), run_backtest(c["spec"], test_c, **kw)
        except ValueError as exc:
            out["proposals"].append({"note": c["note"], "error": str(exc), "verdict": "ERROR"})
            continue
        v = verdict_for(_summary(base_test), _summary(te), _summary(base_train), _summary(tr))
        out["proposals"].append({
            "note": c["note"], "rationale": c["rationale"], "evidence": c["evidence"], "spec": c["spec"],
            "train": _summary(tr), "test": _summary(te), **v})
    rank = {"VALIDATED_OUT_OF_SAMPLE": 0, "NOT_VALIDATED": 1, "INSUFFICIENT_DATA": 2, "ERROR": 3}
    out["proposals"].sort(key=lambda p: (rank.get(p["verdict"], 9), -(p.get("test", {}).get("net_profit") or 0)))
    return out
