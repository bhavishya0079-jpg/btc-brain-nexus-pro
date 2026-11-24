import time

import pytest

from app import security
from app.config import settings
from app.db import Database


def test_password_hash_roundtrip():
    h = security.hash_password("correct horse")
    assert security.verify_password("correct horse", h) and not security.verify_password("wrong", h)
    assert not security.verify_password("x", "garbage")


def test_token_sign_expiry_and_tamper():
    t = security.make_token("admin", secret="s", hours=1)
    assert security.verify_token(t, secret="s") == "admin"
    assert security.verify_token(t, secret="other") is None
    body, sig = t.split(".")
    assert security.verify_token(body + "." + sig[:-2] + "AA", secret="s") is None
    assert security.verify_token(security.make_token("a", secret="s", hours=-1), secret="s") is None
    assert security.verify_token("nonsense", secret="s") is None


def test_rate_limiter():
    rl = security.RateLimiter()
    assert all(rl.allow("k", 3, 60) for _ in range(3)) and not rl.allow("k", 3, 60)
    assert rl.allow("other", 3, 60)


def test_validators():
    assert security.v_symbol("BTCUSDT") == "BTCUSDT" and security.v_symbol("NSE:M&M")
    for bad in ("", "a b", "x" * 50, "a;drop", None, "../x"):
        with pytest.raises(security.ValidationError):
            security.v_symbol(bad)
    assert security.v_tf("15m") == "15m"
    with pytest.raises(security.ValidationError):
        security.v_tf("2h")
    with pytest.raises(security.ValidationError):
        security.v_provider("robinhood")
    assert security.v_int("5", "n", 1, 10) == 5
    for bad in ("x", 0, 11):
        with pytest.raises(security.ValidationError):
            security.v_int(bad, "n", 1, 10)
    with pytest.raises(security.ValidationError):
        security.v_float("nan", "f", 0, 1)


def test_live_trading_keys_are_rejected():
    security.assert_no_live_keys({"entry": [], "risk": {"x": 1}})
    for k in ("place_order", "withdraw", "execute_live", "transfer"):
        with pytest.raises(security.ValidationError):
            security.assert_no_live_keys({"a": [{"b": {k: True}}]})


def test_ensure_admin(db, monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "short")
    with pytest.raises(RuntimeError):
        security.ensure_admin(db)
    monkeypatch.setattr(settings, "admin_password", "long-enough-pass")
    assert security.ensure_admin(db) is True and security.ensure_admin(db) is False
    row = db.one("SELECT * FROM users WHERE username='admin'")
    assert security.verify_password("long-enough-pass", row["pw_hash"]) and row["role"] == "admin"


def test_migrations_idempotent_and_settings(tmp_path):
    d = Database(str(tmp_path / "m.db"))
    first = d.migrate()
    assert "001_init.sql" in first and d.migrate() == []
    d.set_setting("theme", {"dark": True})
    d.set_setting("theme", {"dark": False})
    assert d.get_setting("theme") == {"dark": False} and d.get_setting("missing", 7) == 7
    assert d.integrity_ok()


def test_backup_and_restore(tmp_path):
    d = Database(str(tmp_path / "a.db"))
    d.migrate()
    d.execute("INSERT INTO users(username,pw_hash,role,created_at) VALUES ('u','h','user',?)", (time.time(),))
    d.backup_to(str(tmp_path / "bk.db"))
    d.execute("INSERT INTO users(username,pw_hash,role,created_at) VALUES ('v','h','user',?)", (time.time(),))
    assert d.one("SELECT COUNT(*) n FROM users")["n"] == 2
    d.restore_from(str(tmp_path / "bk.db"))
    assert d.one("SELECT COUNT(*) n FROM users")["n"] == 1
    (tmp_path / "junk.db").write_bytes(b"not a database at all")
    with pytest.raises(Exception):
        d.restore_from(str(tmp_path / "junk.db"))
    assert d.one("SELECT COUNT(*) n FROM users")["n"] == 1        # failed restore leaves data untouched
