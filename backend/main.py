"""FastAPI entry point: `uvicorn backend.main:app --reload` (run from the repo root)."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.config import check_settings, get_settings
from backend.deps import get_repo
from backend.errors import AppError
from backend.routers import geocode, trips

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("trip_planner")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()  # raises with a clear message if config is invalid
    check_settings(settings)
    repo = get_repo()
    repo.init()
    purged = repo.purge_old_drafts()
    if purged:
        log.info("purged %d old unsaved drafts", purged)
    yield


app = FastAPI(title="Trip Planner API", version="1.0", lifespan=lifespan)
app.include_router(geocode.router)
app.include_router(trips.router)


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError):
    return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "request"
    msg = str(first.get("msg", "Invalid input")).removeprefix("Value error, ")
    return JSONResponse(status_code=422, content={"error": {
        "code": "invalid_input", "message": f"{field}: {msg}" if field != "request" else msg,
        "retryable": False, "provider": None}})


@app.get("/api/health")
def health():
    s = get_settings()
    return {
        "status": "ok",
        "agent": "gemini" if s.agent_enabled else "built-in",
        "model": s.gemini_model if s.agent_enabled else None,
        "routing": s.routing_base_url,
    }
