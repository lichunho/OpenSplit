"""
Thin entry point: create the app, run create_all on startup, register routes.
No business logic lives here — that belongs in routes/ and money.py.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import SQLModel
from starlette.middleware.sessions import SessionMiddleware

from app.config import SECRET_KEY, SESSION_HTTPS_ONLY, STATIC_DIR
from app.db import engine
from app import models  # noqa: F401  (import registers tables on SQLModel.metadata)
from app.routes.groups import router as groups_router
from app.routes.expenses import router as expenses_router
from app.routes.settlements import router as settlements_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # v1 uses create_all, which silently no-ops on schema changes — see the
    # versioning note in CLAUDE.md for the archive-before-destructive-change
    # process this implies once real group data exists.
    SQLModel.metadata.create_all(engine)
    yield


app = FastAPI(lifespan=lifespan)

# https_only=False by default (see config.py) so identify works over local
# http://127.0.0.1 dev; production sets SESSION_HTTPS_ONLY=true.
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=SESSION_HTTPS_ONLY,
    max_age=60 * 60 * 24 * 30,  # 30 days
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(groups_router)
app.include_router(expenses_router)
app.include_router(settlements_router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/robots.txt", include_in_schema=False)
def robots():
    # The link is the credential — disallow all, so nothing gets crawled
    # into a search index.
    return FileResponse(STATIC_DIR / "robots.txt")
