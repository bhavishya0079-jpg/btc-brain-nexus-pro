"""Candle helpers. Canonical candle format (shared with app.core):
    {"t": epoch milliseconds (bar open), "o": float, "h": float, "l": float, "c": float, "v": float}
"""
from .providers.base import DataError, INTERVAL_SECONDS, resample, validate_candles  # noqa: F401


def drop_unclosed(candles, tf, now_ms):
    """Remove the still-forming last candle so signals only use closed bars."""
    if candles and candles[-1]["t"] + INTERVAL_SECONDS[tf] * 1000 > now_ms:
        return candles[:-1]
    return candles


def store_candles(db, provider, symbol, tf, candles):
    db.executemany(
        "INSERT OR REPLACE INTO candles(provider,symbol,tf,t,o,h,l,c,v) VALUES (?,?,?,?,?,?,?,?,?)",
        [(provider, symbol, tf, c["t"], c["o"], c["h"], c["l"], c["c"], c["v"]) for c in candles])


def load_candles(db, provider, symbol, tf, start_ms=None, end_ms=None, limit=None):
    sql = "SELECT t,o,h,l,c,v FROM candles WHERE provider=? AND symbol=? AND tf=?"
    p = [provider, symbol, tf]
    if start_ms is not None:
        sql += " AND t>=?"
        p.append(start_ms)
    if end_ms is not None:
        sql += " AND t<=?"
        p.append(end_ms)
    rows = db.query(sql + " ORDER BY t", tuple(p))
    return rows[-limit:] if limit else rows
