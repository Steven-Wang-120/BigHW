"""Minimal service-backed health endpoint; no automatic database initialization."""

from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from 主程序.repositories.database import DatabaseError
from 主程序.services.health_service import HealthService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "数据" / "runtime" / "bootstrap.sqlite3"


def create_app(database_path: Path = DEFAULT_DATABASE) -> FastAPI:
    service = HealthService(database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.health_service = service
        app.state.started = True
        try:
            yield
        finally:
            app.state.started = False

    app = FastAPI(title="VC-R-03 bootstrap", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.started = False

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.get("/health")
    def health(request: Request):
        try:
            result = service.check()
        except DatabaseError as exc:
            return JSONResponse(status_code=503, content={
                "error": {"code": exc.code, "message": "Database health check failed; explicit init-db is required for a new database.", "details": {}},
                "request_id": request.state.request_id,
            })
        return {"status": "ok", **result, "request_id": request.state.request_id}

    return app


app = create_app()
