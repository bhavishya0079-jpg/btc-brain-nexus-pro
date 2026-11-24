"""Options analytics: Black-Scholes greeks/IV (optional derivation), PCR, max pain, moneyness.

Policy: values supplied by the provider are used as they are. Greeks / IV are only DERIVED
(Black-Scholes) when the caller explicitly asks (derive=True) and the required inputs are
present; derived values are flagged greeks_source='derived_bs'. Missing inputs -> None.
"""
from __future__ import annotations

import math
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional

from ..providers.base import IST

SQRT2 = math.sqrt(2.0)
SECONDS_PER_YEAR = 365.0 * 24 * 3600


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price(spot: float, strike: float, t: float, r: float, sigma: float, kind: str) -> float:
    if t <= 0 or sigma <= 0:
        return max(0.0, spot - strike) if kind == "CE" else max(0.0, strike - spot)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if kind == "CE":
        return spot * norm_cdf(d1) - strike * math.exp(-r * t) * norm_cdf(d2)
    return strike * math.exp(-r * t) * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_greeks(spot: float, strike: float, t: float, r: float, sigma: float, kind: str) -> Dict[str, float]:
    """Delta, gamma, theta (per calendar day), vega (per 1 vol point = 1%)."""
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        raise ValueError("t, sigma, spot and strike must be positive")
    sq = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / (sigma * sq)
    d2 = d1 - sigma * sq
    pdf = norm_pdf(d1)
    if kind == "CE":
        delta = norm_cdf(d1)
        theta = -(spot * pdf * sigma) / (2 * sq) - r * strike * math.exp(-r * t) * norm_cdf(d2)
    else:
        delta = norm_cdf(d1) - 1.0
        theta = -(spot * pdf * sigma) / (2 * sq) + r * strike * math.exp(-r * t) * norm_cdf(-d2)
    return {
        "delta": delta,
        "gamma": pdf / (spot * sigma * sq),
        "theta": theta / 365.0,
        "vega": spot * pdf * sq / 100.0,
    }


def implied_vol(price: float, spot: float, strike: float, t: float, r: float, kind: str) -> Optional[float]:
    """Bisection IV (decimal, e.g. 0.25). Returns None if the price is outside no-arbitrage bounds."""
    if min(price, spot, strike) <= 0 or t <= 0:
        return None
    intrinsic = max(0.0, spot - strike * math.exp(-r * t)) if kind == "CE" else max(0.0, strike * math.exp(-r * t) - spot)
    upper = spot if kind == "CE" else strike * math.exp(-r * t)
    if price < intrinsic - 1e-9 or price > upper:
        return None
    lo, hi = 1e-4, 5.0
    if bs_price(spot, strike, t, r, hi, kind) < price:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t, r, mid, kind) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


def expiry_datetime(expiry: str, provider_id: str = "") -> datetime:
    day = datetime.strptime(expiry, "%Y-%m-%d").date()
    close = dtime(17, 30) if provider_id == "delta" else dtime(15, 30)
    return datetime.combine(day, close, tzinfo=IST)


def years_to_expiry(expiry: str, provider_id: str = "", now: Optional[datetime] = None) -> float:
    now = now or datetime.now(IST)
    return max(0.0, (expiry_datetime(expiry, provider_id) - now).total_seconds() / SECONDS_PER_YEAR)


def moneyness(strike: float, spot: float, atm_strike: float, kind: str) -> str:
    if strike == atm_strike:
        return "ATM"
    if kind == "CE":
        return "ITM" if strike < spot else "OTM"
    return "ITM" if strike > spot else "OTM"


def _sum(rows: List[Dict[str, Any]], side: str, field: str) -> Optional[float]:
    vals = [r[side][field] for r in rows if r.get(side) and r[side].get(field) is not None]
    return float(sum(vals)) if vals else None


def max_pain(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Strike at which total option-writer payout is lowest, using open interest."""
    usable = [r for r in rows if (r.get("CE") or {}).get("oi") is not None or (r.get("PE") or {}).get("oi") is not None]
    if len(usable) < 2:
        return None
    strikes = [r["strike"] for r in usable]
    best, best_pain, table = None, None, []
    for k in strikes:
        pain = 0.0
        for r in usable:
            ce_oi = (r.get("CE") or {}).get("oi") or 0.0
            pe_oi = (r.get("PE") or {}).get("oi") or 0.0
            pain += ce_oi * max(0.0, k - r["strike"]) + pe_oi * max(0.0, r["strike"] - k)
        table.append({"strike": k, "pain": pain})
        if best_pain is None or pain < best_pain:
            best, best_pain = k, pain
    if best_pain == 0 and all(t["pain"] == 0 for t in table):
        return None
    return {"strike": best, "pain": best_pain, "curve": table}


def analyze_chain(chain: Dict[str, Any], *, derive: bool = False, rate: float = 0.07,
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    rows = [dict(r) for r in chain["rows"]]
    spot = chain.get("spot")
    provider_id = chain.get("provider", "")
    t = years_to_expiry(chain["expiry"], provider_id, now)
    atm = None
    if spot and rows:
        atm = min((r["strike"] for r in rows), key=lambda k: abs(k - spot))
    for r in rows:
        for side in ("CE", "PE"):
            leg = r.get(side)
            if not leg:
                continue
            leg = dict(leg)
            r[side] = leg
            if spot and atm is not None:
                leg["moneyness"] = moneyness(r["strike"], spot, atm, side)
                leg["distance_pct"] = (r["strike"] - spot) / spot * 100.0
            if derive and spot and t > 0 and leg.get("ltp"):
                missing_greeks = any(leg.get(g) is None for g in ("delta", "gamma", "theta", "vega"))
                iv_dec = None
                if leg.get("iv") is None:
                    iv_dec = implied_vol(leg["ltp"], spot, r["strike"], t, rate, side)
                    if iv_dec is not None:
                        leg["iv"] = iv_dec * 100.0
                        leg["iv_source"] = "derived_bs"
                else:
                    iv_dec = leg["iv"] / 100.0 if leg["iv"] > 3 else leg["iv"]
                if missing_greeks and iv_dec:
                    try:
                        g = bs_greeks(spot, r["strike"], t, rate, iv_dec, side)
                        for key, value in g.items():
                            if leg.get(key) is None:
                                leg[key] = value
                        leg["greeks_source"] = "derived_bs"
                    except ValueError:
                        pass
    total_ce_oi, total_pe_oi = _sum(rows, "CE", "oi"), _sum(rows, "PE", "oi")
    total_ce_vol, total_pe_vol = _sum(rows, "CE", "volume"), _sum(rows, "PE", "volume")
    pcr_oi = total_pe_oi / total_ce_oi if total_ce_oi and total_pe_oi is not None else None
    pcr_vol = total_pe_vol / total_ce_vol if total_ce_vol and total_pe_vol is not None else None

    def top(side: str, n: int = 3):
        items = [(r["strike"], r[side]["oi"]) for r in rows if r.get(side) and r[side].get("oi")]
        return [{"strike": k, "oi": v} for k, v in sorted(items, key=lambda x: -x[1])[:n]]

    atm_row = next((r for r in rows if r["strike"] == atm), None)
    straddle = None
    if atm_row and (atm_row.get("CE") or {}).get("ltp") is not None and (atm_row.get("PE") or {}).get("ltp") is not None:
        straddle = atm_row["CE"]["ltp"] + atm_row["PE"]["ltp"]
    summary = {
        "spot": spot, "atm_strike": atm, "days_to_expiry": round(t * 365, 3),
        "total_ce_oi": total_ce_oi, "total_pe_oi": total_pe_oi, "pcr_oi": pcr_oi,
        "total_ce_volume": total_ce_vol, "total_pe_volume": total_pe_vol, "pcr_volume": pcr_vol,
        "max_pain": (max_pain(rows) or {}).get("strike"),
        "resistance_by_ce_oi": top("CE"), "support_by_pe_oi": top("PE"),
        "atm_straddle_premium": straddle,
        "pcr_reading": None if pcr_oi is None else ("bullish-leaning (PE writing dominant)" if pcr_oi > 1.2 else
                                                     "bearish-leaning (CE writing dominant)" if pcr_oi < 0.7 else "neutral"),
        "note": "Derived from provider data; PCR/max-pain readings are context, not predictions.",
    }
    return {**{k: v for k, v in chain.items() if k != "rows"}, "rows": rows, "summary": summary,
            "max_pain_curve": (max_pain(rows) or {}).get("curve")}


def strike_analysis(analyzed: Dict[str, Any], strike: float) -> Dict[str, Any]:
    rows = analyzed["rows"]
    row = next((r for r in rows if r["strike"] == strike), None)
    if row is None:
        raise ValueError(f"Strike {strike} not in chain")
    spot = analyzed.get("spot")
    ce, pe = row.get("CE") or {}, row.get("PE") or {}
    total_ce = analyzed["summary"].get("total_ce_oi")
    total_pe = analyzed["summary"].get("total_pe_oi")
    out: Dict[str, Any] = {"strike": strike, "spot": spot, "CE": ce, "PE": pe}
    if spot:
        out["distance_pct"] = (strike - spot) / spot * 100.0
    if ce.get("ltp") is not None:
        out["ce_breakeven"] = strike + ce["ltp"]
        if spot:
            out["ce_breakeven_move_pct"] = (strike + ce["ltp"] - spot) / spot * 100.0
    if pe.get("ltp") is not None:
        out["pe_breakeven"] = strike - pe["ltp"]
        if spot:
            out["pe_breakeven_move_pct"] = (strike - pe["ltp"] - spot) / spot * 100.0
    if ce.get("ltp") is not None and pe.get("ltp") is not None:
        out["straddle_premium"] = ce["ltp"] + pe["ltp"]
    if ce.get("oi") is not None and total_ce:
        out["ce_oi_share_pct"] = ce["oi"] / total_ce * 100.0
    if pe.get("oi") is not None and total_pe:
        out["pe_oi_share_pct"] = pe["oi"] / total_pe * 100.0
    if ce.get("oi") and pe.get("oi") is not None:
        out["strike_pcr"] = pe["oi"] / ce["oi"]
    return out
