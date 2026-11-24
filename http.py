"""Hardened HTTP client for market-data providers.

Safety properties enforced here (and covered by tests):
  * Only explicitly allow-listed (method, path) pairs can ever be called.
  * Any path that looks like order placement, funds, withdrawal, deposit, transfer,
    positions or portfolio is refused even if somebody adds it to an allow-list.
  * Requests can only go to the configured host (no redirects to other hosts).
  * Token-bucket rate limiting, retry with backoff, Retry-After support.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple
from urllib.parse import urlparse

import requests

FORBIDDEN = re.compile(
    r"(order(?!book)|place|modify|cancel|withdraw|deposit|transfer|payout|fund|margin|portfolio|holding|position|rms|gtt|trade/v)",
    re.IGNORECASE,
)


class ForbiddenEndpoint(Exception):
    """Raised when code tries to call an endpoint outside the read-only market-data allow-list."""


class ProviderError(Exception):
    def __init__(self, message: str, *, code: str = "provider_error", status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.retryable = retryable


class NotSupported(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, code="not_supported", status=501)


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: Optional[int] = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.rate = max(rate_per_sec, 0.001)
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_sec)))
        self.tokens = self.capacity
        self.clock = clock
        self.sleep = sleep
        self.updated = clock()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            self.sleep(wait)


Allow = Tuple[str, str]  # (METHOD, regex for path)


class SafeHttp:
    def __init__(
        self,
        base_url: str,
        allowed: List[Allow],
        *,
        session: Optional[Any] = None,
        rps: float = 5.0,
        timeout: float = 10.0,
        retries: int = 2,
        default_headers: Optional[Dict[str, str]] = None,
        sleep: Callable[[float], None] = time.sleep,
        bucket: Optional[TokenBucket] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.host = urlparse(self.base_url).netloc
        self.allowed: List[Tuple[str, Pattern[str]]] = [(m.upper(), re.compile(p)) for m, p in allowed]
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = retries
        self.default_headers = dict(default_headers or {})
        self.sleep = sleep
        self.bucket = bucket or TokenBucket(rps, sleep=sleep)
        # statistics for the diagnostics panel
        self.calls = 0
        self.errors = 0
        self.last_ok: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_error_at: Optional[float] = None
        self.last_latency_ms: Optional[float] = None
        self._lock = threading.Lock()

    # --------------------------------------------------------------- allow-list
    def _check(self, method: str, path: str) -> None:
        if not path.startswith("/"):
            raise ForbiddenEndpoint(f"Path must be relative to the provider base URL: {path!r}")
        if FORBIDDEN.search(path):
            raise ForbiddenEndpoint(f"Endpoint blocked by read-only policy: {path}")
        for m, pattern in self.allowed:
            if m == method and pattern.fullmatch(path):
                return
        raise ForbiddenEndpoint(f"Endpoint is not on the market-data allow-list: {method} {path}")

    # ------------------------------------------------------------------ request
    def request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                json: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
        method = method.upper()
        self._check(method, path)
        url = self.base_url + path
        merged = dict(self.default_headers)
        merged.update(headers or {})
        attempt = 0
        while True:
            attempt += 1
            self.bucket.acquire()
            started = time.monotonic()
            try:
                resp = self.session.request(method, url, params=params, json=json, headers=merged,
                                            timeout=self.timeout, allow_redirects=False)
            except requests.RequestException as exc:
                self._record_error(f"{type(exc).__name__}: {exc}")
                if attempt <= self.retries:
                    self.sleep(min(2 ** attempt * 0.25, 4))
                    continue
                raise ProviderError(f"Connection failed: {type(exc).__name__}", code="connection_error", retryable=True)
            latency = (time.monotonic() - started) * 1000
            status = resp.status_code
            if 300 <= status < 400:
                self._record_error(f"redirect to {resp.headers.get('Location', '?')} refused")
                raise ProviderError("Provider redirected the request (refused)", code="redirect_refused", status=status)
            if status == 429 or status >= 500:
                self._record_error(f"HTTP {status}")
                if attempt <= self.retries:
                    retry_after = 0.0
                    try:
                        retry_after = float(resp.headers.get("Retry-After", 0))
                    except (TypeError, ValueError):
                        pass
                    self.sleep(min(max(retry_after, 2 ** attempt * 0.25), 10))
                    continue
                raise ProviderError(
                    "Rate limit hit" if status == 429 else f"Provider server error ({status})",
                    code="rate_limited" if status == 429 else "server_error", status=status, retryable=True,
                )
            if status >= 400:
                text = ""
                try:
                    text = resp.text[:300]
                except Exception:
                    pass
                self._record_error(f"HTTP {status}: {text[:120]}")
                code = "auth_error" if status in (401, 403) else "bad_request"
                raise ProviderError(f"Provider rejected request ({status}): {text}", code=code, status=status)
            try:
                data = resp.json()
            except ValueError:
                self._record_error("invalid JSON from provider")
                raise ProviderError("Provider returned invalid JSON", code="invalid_response", status=status)
            with self._lock:
                self.calls += 1
                self.last_ok = time.time()
                self.last_latency_ms = round(latency, 1)
            return data

    def _record_error(self, message: str) -> None:
        with self._lock:
            self.calls += 1
            self.errors += 1
            self.last_error = message
            self.last_error_at = time.time()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "calls": self.calls,
                "errors": self.errors,
                "last_ok": self.last_ok,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "last_latency_ms": self.last_latency_ms,
            }
