"""Central configuration. Secrets are read from environment / .env (server side only)."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VERSION = "1.0.0"


def _load_dotenv():
    for p in (BASE_DIR / ".env", BASE_DIR.parent / ".env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(name, default=None):
    return os.environ.get(name, default)


class Settings:
    def __init__(self):
        self.db_path = env("NEXUS_DB_PATH", str(BASE_DIR / "data" / "nexus.db"))
        self.backup_dir = env("NEXUS_BACKUP_DIR", str(BASE_DIR / "data" / "backups"))
        self.log_dir = env("NEXUS_LOG_DIR", str(BASE_DIR / "data" / "logs"))
        self.admin_password = env("NEXUS_ADMIN_PASSWORD", "")
        # An ephemeral secret is generated if none is configured (sessions reset on restart).
        self.secret_key = env("NEXUS_SECRET_KEY") or secrets.token_hex(32)
        self.session_hours = int(env("NEXUS_SESSION_HOURS", "12"))
        self.cors_origins = [o.strip() for o in env("NEXUS_CORS_ORIGINS", "*").split(",") if o.strip()]
        self.rate_limit_per_min = int(env("NEXUS_RATE_LIMIT_PER_MIN", "240"))
        self.login_limit_per_min = int(env("NEXUS_LOGIN_LIMIT_PER_MIN", "10"))
        self.serve_frontend = env("NEXUS_SERVE_FRONTEND", "1") == "1"
        self.scheduler_enabled = env("NEXUS_SCHEDULER", "1") == "1"
        self.alert_interval_sec = int(env("NEXUS_ALERT_INTERVAL_SEC", "30"))
        self.auto_backup_hours = int(env("NEXUS_AUTO_BACKUP_HOURS", "24"))
        # Providers (read-only market data credentials)
        self.binance_base = env("BINANCE_BASE_URL", "https://api.binance.com")
        self.delta_base = env("DELTA_BASE_URL", "https://api.india.delta.exchange")
        self.angel_api_key = env("ANGEL_API_KEY", "")
        self.angel_client_code = env("ANGEL_CLIENT_CODE", "")
        self.angel_pin = env("ANGEL_PIN", "")
        self.angel_totp_secret = env("ANGEL_TOTP_SECRET", "")
        self.groww_access_token = env("GROWW_ACCESS_TOKEN", "")
        # Notifications
        self.webhook_url = env("NEXUS_WEBHOOK_URL", "")
        self.telegram_bot_token = env("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = env("TELEGRAM_CHAT_ID", "")


settings = Settings()

DISCLAIMER = (
    "Research and analysis only. No real orders are placed. "
    "Signals and backtests do not guarantee future profit."
)
