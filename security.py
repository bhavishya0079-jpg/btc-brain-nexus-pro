"""Authentication, sessions, rate limiting, validation and security headers."""
import base64
import hashlib
import hmac
import json
import re
import threading
import time
from collections import defaultdict, deque

from .config import settings
from .providers.base import INTERVAL_SECONDS

# ---------- passwords ----------

def hash_password(password, salt=None, iterations=200_000):
    salt = salt or hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:32]
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2${iterations}${salt}${dk.hex()}"


def verify_password(password, stored):
    try:
        _, it, salt, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except Exception:
        return False


class ApiError(Exception):
    """Error carrying an HTTP status and a stable machine-readable code."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def ensure_admin(db):
    """Create the single admin user from NEXUS_ADMIN_PASSWORD if no user exists."""
    if db.query("SELECT id FROM users LIMIT 1"):
        return False
    if not settings.admin_password:
        return False
    if len(settings.admin_password) < 8:
        raise RuntimeError("NEXUS_ADMIN_PASSWORD must be at least 8 characters")
    db.execute(
        "INSERT INTO users(username, pw_hash, role, created_at) VALUES (?,?,'admin',?)",
        ("admin", hash_password(settings.admin_password), time.time()),
    )
    return True


# ---------- signed session tokens ----------

def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(username, secret=None, hours=None):
    secret = secret or settings.secret_key
    exp = int(time.time() + 3600 * (hours or settings.session_hours))
    body = _b64(json.dumps({"u": username, "exp": exp}).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token, secret=None):
    secret = secret or settings.secret_key
    try:
        body, sig = token.split(".")
        good = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(_unb64(body))
        if data["exp"] < time.time():
            return None
        return data["u"]
    except Exception:
        return None


# ---------- rate limiting ----------

class RateLimiter:
    def __init__(self):
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key, limit, window=60):
        t = time.time()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= t - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(t)
            if len(self.hits) > 5000:  # bound memory
                for k in [k for k, v in self.hits.items() if not v or v[-1] < t - window]:
                    self.hits.pop(k, None)
            return True


limiter = RateLimiter()

# ---------- validation ----------

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9:_\-\.&]{1,40}$")
_PROVIDERS = {"binance", "delta", "angelone", "groww"}


class ValidationError(ValueError):
    pass


def v_symbol(s):
    if not isinstance(s, str) or not _SYMBOL_RE.match(s):
        raise ValidationError("invalid symbol")
    return s


def v_tf(tf):
    if tf not in INTERVAL_SECONDS:
        raise ValidationError(f"invalid timeframe; allowed: {', '.join(INTERVAL_SECONDS)}")
    return tf


def v_provider(p):
    if p not in _PROVIDERS:
        raise ValidationError(f"invalid provider; allowed: {', '.join(sorted(_PROVIDERS))}")
    return p


def v_int(val, name, lo, hi, default=None):
    if val is None or val == "":
        if default is None:
            raise ValidationError(f"{name} is required")
        return default
    try:
        n = int(val)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be an integer")
    if not lo <= n <= hi:
        raise ValidationError(f"{name} must be between {lo} and {hi}")
    return n


def v_float(val, name, lo, hi, default=None):
    if val is None or val == "":
        if default is None:
            raise ValidationError(f"{name} is required")
        return default
    try:
        n = float(val)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be a number")
    if n != n or not lo <= n <= hi:
        raise ValidationError(f"{name} must be between {lo} and {hi}")
    return n


def v_name(s, name="name", maxlen=80):
    if not isinstance(s, str) or not s.strip() or len(s) > maxlen:
        raise ValidationError(f"{name} must be 1-{maxlen} characters")
    return s.strip()


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# Hard guard: the platform never places real orders or touches broker funds.
FORBIDDEN_KEYS = {
    "place_order", "execute_live", "live_trading", "withdraw", "deposit", "transfer",
    "broker_order", "real_order", "fund_transfer",
}


def assert_no_live_keys(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                raise ValidationError(f"'{k}' is not allowed: this platform never executes real orders or moves funds")
            assert_no_live_keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            assert_no_live_keys(v, f"{path}[{i}]")
