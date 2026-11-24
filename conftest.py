import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp = tempfile.mkdtemp(prefix="nexus_test_")
os.environ.update({
    "NEXUS_DB_PATH": os.path.join(_tmp, "session.db"),
    "NEXUS_BACKUP_DIR": os.path.join(_tmp, "backups"),
    "NEXUS_LOG_DIR": os.path.join(_tmp, "logs"),
    "NEXUS_ADMIN_PASSWORD": "test-password-123",
    "NEXUS_SECRET_KEY": "test-secret",
    "NEXUS_SCHEDULER": "0",
    "NEXUS_SERVE_FRONTEND": "0",
    "NEXUS_RATE_LIMIT_PER_MIN": "100000",
    "NEXUS_LOGIN_LIMIT_PER_MIN": "100000",
    "ANGEL_API_KEY": "", "ANGEL_CLIENT_CODE": "", "ANGEL_PIN": "", "ANGEL_TOTP_SECRET": "", "GROWW_ACCESS_TOKEN": "",
})


@pytest.fixture
def db(tmp_path):
    from app.db import Database
    d = Database(str(tmp_path / "t.db"))
    d.migrate()
    yield d
    d.close()
