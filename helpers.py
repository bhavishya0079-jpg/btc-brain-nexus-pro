"""Synthetic candle builders used ONLY by tests (never imported by app code)."""
from typing import List, Dict

import numpy as np


def candles_from_closes(closes, step_ms=900_000, start=1_700_000_000_000, spread=0.002, volume=100.0):
    out: List[Dict[str, float]] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        h = max(o, c) * (1 + spread)
        l = min(o, c) * (1 - spread)
        out.append({"t": start + i * step_ms, "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(volume)})
        prev = c
    return out


def random_walk(n=600, seed=11, start=100.0, vol=0.01, drift=0.0, step_ms=900_000):
    rng = np.random.default_rng(seed)
    closes = start * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    c = candles_from_closes(list(closes), step_ms=step_ms)
    for x in c:
        x["v"] = float(rng.uniform(50, 150))
    return c


def trending(n=300, step=0.5, start=100.0, step_ms=900_000):
    return candles_from_closes([start + i * step for i in range(n)], step_ms=step_ms)


def zigzag(n_cycles=20, amplitude=10.0, base=100.0, bars=20, step_ms=900_000):
    closes = []
    for _ in range(n_cycles):
        closes += list(np.linspace(base - amplitude, base + amplitude, bars)) + list(np.linspace(base + amplitude, base - amplitude, bars))
    return candles_from_closes(closes, step_ms=step_ms)
