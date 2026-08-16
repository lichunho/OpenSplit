"""
Trap #1 regression tests (see CLAUDE.md). These assert the engine *configuration*
that only misbehaves against a suspended Neon instance — the failure mode is a
500 in production that a green local SQLite run never reproduces, so the config
itself is what gets pinned here. Building an engine opens no connection, so the
Postgres cases run offline against a fake URL.
"""
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from app.db import create_db_engine, normalise_database_url

NEON = "@ep-example-123456-pooler.region.aws.neon.tech/dbname?sslmode=require"


def test_legacy_postgres_scheme_is_normalised_to_psycopg3():
    assert normalise_database_url("postgres://u:p" + NEON) == "postgresql+psycopg://u:p" + NEON


def test_bare_postgresql_scheme_is_normalised_to_psycopg3():
    # Without this a bare postgresql:// URL would silently select psycopg2.
    assert normalise_database_url("postgresql://u:p" + NEON) == "postgresql+psycopg://u:p" + NEON


def test_already_normalised_url_is_left_alone():
    url = "postgresql+psycopg://u:p" + NEON
    assert normalise_database_url(url) == url


def test_normalisation_preserves_the_query_string():
    # Losing ?sslmode=require would break the connection to Neon entirely.
    assert normalise_database_url("postgres://u:p" + NEON).endswith("?sslmode=require")


def test_postgres_engine_uses_nullpool_and_psycopg3():
    engine = create_db_engine("postgres://u:p" + NEON)
    # NullPool is the whole fix for the autosuspend SSL-EOF trap: no pooled
    # connection survives to be reused against a sleeping database.
    assert isinstance(engine.pool, NullPool)
    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "psycopg"


def test_sqlite_engine_enables_wal(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'wal.db'}")
    assert not isinstance(engine.pool, NullPool)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    finally:
        engine.dispose()
