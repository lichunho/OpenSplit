"""
Pins the one config combination that can only be a mistake: a Postgres
DATABASE_URL (so, a deploy) with SESSION_HTTPS_ONLY unset. Nothing in the repo
forces the flag on — the deploy sets it by hand in env.json — and a session
cookie missing the Secure flag is invisible in an otherwise working app.

config.py reads the environment at import time, so these reload it under a
patched environment rather than importing it fresh.
"""
import importlib

import pytest

import app.config

NEON = "postgresql://u:p@ep-example-123456-pooler.region.aws.neon.tech/dbname"


@pytest.fixture(autouse=True)
def restore_config():
    # conftest's SQLite DATABASE_URL is still in the environment, so reloading
    # after each case puts the module back the way the rest of the suite has it.
    yield
    importlib.reload(app.config)


def test_postgres_without_https_only_refuses_to_start(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", NEON)
    monkeypatch.setenv("SESSION_HTTPS_ONLY", "false")
    with pytest.raises(RuntimeError, match="SESSION_HTTPS_ONLY"):
        importlib.reload(app.config)


def test_postgres_with_https_only_starts(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", NEON)
    monkeypatch.setenv("SESSION_HTTPS_ONLY", "true")
    importlib.reload(app.config)
    assert app.config.SESSION_HTTPS_ONLY is True
