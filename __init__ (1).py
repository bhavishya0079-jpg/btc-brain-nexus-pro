"""Read-only market-data providers."""
from .angelone import AngelOneProvider
from .base import INTERVAL_SECONDS, IST, NotConfigured, Provider
from .binance import BinanceProvider
from .delta import DeltaProvider
from .groww import GrowwProvider
from .http import ForbiddenEndpoint, NotSupported, ProviderError

__all__ = ["AngelOneProvider", "BinanceProvider", "DeltaProvider", "GrowwProvider", "Provider", "ProviderError",
           "NotSupported", "NotConfigured", "ForbiddenEndpoint", "INTERVAL_SECONDS", "IST", "build_providers"]


def build_providers(session=None, sleep=None):
    kw = {"session": session}
    if sleep:
        kw["sleep"] = sleep
    return {p.name: p for p in (BinanceProvider(**kw), DeltaProvider(**kw), AngelOneProvider(**kw), GrowwProvider(**kw))}
