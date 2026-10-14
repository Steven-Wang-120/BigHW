"""Minimal service-backed health endpoint; no automatic database initialization."""

from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from 主程序.services.import_service import counts
from 主程序.services.reconcile_service import run_summary, batch_runs
from 主程序.repositories.xlsx_reader import InputError

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

    @app.get("/evidence/counts")
    def count_page():
        return FileResponse(Path(__file__).parent / "static/pages/counts.html")

    @app.get("/static/{kind}/{filename}")
    def count_assets(kind: str, filename: str):
        allowed = {("js", "counts.js"), ("css", "counts.css"), ("js", "run.js")}
        if (kind, filename) not in allowed:
            return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND"}})
        return FileResponse(Path(__file__).parent / "static" / kind / filename)

    @app.get("/evidence/counts/{batch_id}")
    def count_json(batch_id: str, request: Request):
        try:
            result = {**counts(database_path, batch_id), "runs": batch_runs(database_path, batch_id)}
        except (InputError, DatabaseError) as exc:
            return JSONResponse(status_code=exc.status if isinstance(exc, InputError) else 503,
                                content={"error": {"code": exc.code, "message": "Read-only counts unavailable", "details": {}},
                                         "request_id": request.state.request_id})
        return {**result, "request_id": request.state.request_id}

    @app.get("/evidence/run")
    def run_page():
        return FileResponse(Path(__file__).parent / "static/pages/run.html")

    @app.get("/evidence/run/{run_id}")
    def run_json(run_id: str, request: Request):
        try:
            result = run_summary(database_path, run_id)
        except (InputError, DatabaseError) as exc:
            return JSONResponse(status_code=exc.status if isinstance(exc, InputError) else 503,
                                content={"error": {"code": exc.code, "message": "Read-only run summary unavailable", "details": {}},
                                         "request_id": request.state.request_id})
        return {**result, "request_id": request.state.request_id}

    return app


app = create_app()
