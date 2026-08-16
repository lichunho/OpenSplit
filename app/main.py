"""
Thin entry point: create the app, run create_all on startup, register routes.
No business logic lives here — that belongs in routes/ and money.py.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import SQLModel

from app.db import engine
from app import models  # noqa: F401  (import registers tables on SQLModel.metadata)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # v1 uses create_all, which silently no-ops on schema changes — see the
    # versioning note in CLAUDE.md for the archive-before-destructive-change
    # process this implies once real group data exists.
    SQLModel.metadata.create_all(engine)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
