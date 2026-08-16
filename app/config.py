"""
Central config module. Every `os.environ` read in the codebase happens here —
db.py, auth.py, and main.py import values from this module instead of reading
the environment themselves.
"""
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Load .env before the reads below. This does not override variables already in
# the environment, so Render's real config always beats a stray local file.
load_dotenv(BASE_DIR.parent / ".env")

# Local dev default: a SQLite file next to the project root. In prod this is
# overridden with a Neon Postgres URL (pooled host, see db.py trap #1).
_DEFAULT_SQLITE_PATH = BASE_DIR.parent / "app.db"
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{_DEFAULT_SQLITE_PATH}")

# Dev convenience: generate a throwaway key at startup when none is set, so
# `uvicorn app.main:app --reload` works with no .env. Every restart gets a new
# key, which invalidates existing sessions — fine in dev, never used in prod
# because Render sets a real SECRET_KEY.
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_urlsafe(32))
