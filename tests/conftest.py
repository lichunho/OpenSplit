"""
Test isolation: `app.db` builds its engine at import time from
`config.DATABASE_URL`, which is itself read from the environment at import
time. So DATABASE_URL is pointed at a throwaway SQLite file *before*
app.main (and therefore app.config/app.db) is imported anywhere in the test
session — the route tests never touch the developer's real app.db at the
repo root.
"""
import os
import tempfile
from pathlib import Path

_tmp_dir = tempfile.mkdtemp(prefix="splitwise-clone-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_tmp_dir) / 'test.db'}"
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SESSION_HTTPS_ONLY", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    # Entering as a context manager runs the lifespan (create_all) against
    # the throwaway SQLite file above, never the real app.db.
    with TestClient(app) as test_client:
        yield test_client
