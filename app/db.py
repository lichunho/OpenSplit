"""
Engine + session dependency. This is where the Neon/SQLite production traps
documented in CLAUDE.md get handled — see the inline comments at each branch.
`db.py` reads no environment variables itself; it takes the raw URL from
config and does its own normalisation.
"""
from sqlalchemy import event
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine

from app.config import DATABASE_URL

# Normalise in exactly one place. Some providers hand out the legacy
# `postgres://` scheme, and a bare `postgresql://` would default to psycopg2;
# we want psycopg3 specifically (see requirements.txt / stack notes).
_url = DATABASE_URL
if _url.startswith("postgres://"):
    _url = "postgresql+psycopg://" + _url[len("postgres://"):]
elif _url.startswith("postgresql://"):
    _url = "postgresql+psycopg://" + _url[len("postgresql://"):]

if _url.startswith("postgresql+psycopg://"):
    # Neon's pooled endpoint (hostname contains "-pooler") already sits behind
    # PgBouncer. Adding SQLAlchemy's own pool on top would hold connections
    # across Neon's 5-minute autosuspend, and the next request would die with
    # "SSL SYSCALL error: EOF detected" — a 500 that never reproduces locally.
    # NullPool opens a fresh connection per request and leaves pooling to
    # PgBouncer entirely, which deletes that bug class.
    engine = create_engine(_url, poolclass=NullPool)
else:
    # SQLite: FastAPI can dispatch a sync route on a worker thread other than
    # the one that opened the connection, so check_same_thread must be off.
    # WAL mode lets a reader proceed without blocking a concurrent writer.
    engine = create_engine(_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_sqlite_wal(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def get_session():
    with Session(engine) as session:
        yield session
