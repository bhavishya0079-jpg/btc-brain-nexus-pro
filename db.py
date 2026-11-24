"""SQLite storage with versioned SQL migrations. `Database` exposes .one/.query/.execute/.executemany."""
import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import BASE_DIR, settings

MIGRATIONS_DIR = BASE_DIR / "migrations"


class Database:
    def __init__(self, path=None):
        self.path = path or settings.db_path
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")

    def migrate(self):
        with self._lock:
            c = self._conn
            c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at REAL NOT NULL)")
            done = {r["name"] for r in c.execute("SELECT name FROM schema_migrations")}
            applied = []
            for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if f.name in done:
                    continue
                c.executescript(f.read_text(encoding="utf-8"))
                c.execute("INSERT INTO schema_migrations(name, applied_at) VALUES (?,?)", (f.name, time.time()))
                c.commit()
                applied.append(f.name)
            return applied

    def query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid

    def executemany(self, sql, seq):
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def audit(self, actor, action, entity=None, entity_id=None, detail=None):
        self.execute(
            "INSERT INTO audit_log(actor, action, entity, entity_id, detail, created_at) VALUES (?,?,?,?,?,?)",
            (actor, action, entity, None if entity_id is None else str(entity_id),
             json.dumps(detail, default=str) if detail is not None else None, time.time()))

    def get_setting(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_setting(self, key, value):
        self.execute("INSERT INTO settings(key,value,updated_at) VALUES (?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (key, json.dumps(value), time.time()))

    def integrity_ok(self):
        return self.one("PRAGMA integrity_check")["integrity_check"] == "ok"

    def backup_to(self, path):
        """Consistent online copy using SQLite's backup API."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(path)
        try:
            with self._lock:
                self._conn.backup(dst)
        finally:
            dst.close()

    def restore_from(self, path):
        """Replace the live database contents with a backup (integrity-checked first)."""
        src = sqlite3.connect(path)
        try:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup file failed integrity check")
            needed = {"users", "strategies", "paper_accounts", "schema_migrations"}
            have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not needed <= have:
                raise ValueError("file is not a Nexus backup")
            with self._lock:
                src.backup(self._conn)
        finally:
            src.close()

    def close(self):
        with self._lock:
            self._conn.close()
