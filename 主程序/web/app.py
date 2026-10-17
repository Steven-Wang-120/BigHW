"""Minimal service-backed health endpoint; no automatic database initialization."""

from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from 主程序.services.auth_service import AuthService
from 主程序.services.workflow_service import reject
from 主程序.web.api import build_router, canonical_origin, query
from fastapi.responses import JSONResponse, FileResponse
from 主程序.services.import_service import counts
from 主程序.services.reconcile_service import run_summary, batch_runs
from 主程序.services import pipeline_service
from 主程序.repositories.xlsx_reader import InputError

from 主程序.repositories.database import DatabaseError, database_transaction
from 主程序.services.health_service import HealthService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "数据" / "runtime" / "bootstrap.sqlite3"


def create_app(database_path: Path = DEFAULT_DATABASE, *, app_origin: str = "http://127.0.0.1:8000", snapshot_catalog=None) -> FastAPI:
    canonical_origin(app_origin)
    service = HealthService(database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.health_service = service
        app.state.started = True
        try:
            yield
        finally:
            app.state.started = False

    app = FastAPI(title="VC-R-03 local workflow", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.started = False
    auth = AuthService(database_path)
    app.state.auth_service = auth
    app.state.app_origin = app_origin
    app.include_router(build_router(database_path, auth, app_origin, snapshot_catalog))

    def error_response(request, code, status, details=None):
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": code.replace("_", " ").capitalize(),
                            "details": details or {}}, "request_id": request.state.request_id})

    @app.exception_handler(InputError)
    async def input_error(request: Request, exc: InputError):
        # Services already persist domain rejections; pre-domain HTTP failures
        # are independently audited here, never logging body/cookie/header secrets.
        if (request.url.path.startswith("/api/v1/") or getattr(request.state, "workflow_evidence", False)) and not getattr(exc, "audit_recorded", False) and exc.code != "AUDIT_WRITE_FAILED" and not (exc.code == "NOT_FOUND" and exc.status == 404 and not hasattr(request.state, "actor")):
            try:
                reject(database_path, getattr(request.state, "actor", None), request.state.request_id,
                       "http", "api", request.method, exc)
            except InputError as failed:
                exc = failed
        return error_response(request, exc.code, exc.status, exc.details)

    @app.exception_handler(DatabaseError)
    async def database_error(request: Request, exc: DatabaseError):
        return error_response(request, exc.code, 503 if exc.code == "DATABASE_BUSY" else 500)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc):
        return error_response(request, "VALIDATION_ERROR", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc):
        return error_response(request, {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR"), exc.status_code)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = uuid4().hex
        try:
            response = await call_next(request)
        except Exception:
            response = error_response(request, "INTERNAL_ERROR", 500)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def home():
        return FileResponse(Path(__file__).parent / "static/pages/login.html")

    @app.get("/pages/{page}")
    def pages(page: str, request: Request):
        allowed = {"login.html", "import.html", "results.html", "differences.html", "detail.html", "business-counts.html", "admin.html"}
        if page not in allowed:
            return error_response(request, "NOT_FOUND", 404)
        return FileResponse(Path(__file__).parent / "static/pages" / page)

    @app.get("/login")
    @app.get("/import")
    @app.get("/results")
    @app.get("/differences")
    @app.get("/detail")
    @app.get("/counts")
    @app.get("/admin")
    def named_page(request: Request):
        page = request.url.path.lstrip("/")
        filename = "business-counts.html" if page == "counts" else page + ".html"
        return pages(filename, request)

    @app.get("/assets/{kind}/{filename}")
    def assets(kind: str, filename: str, request: Request):
        allowed = {
            ("js", "api.js"), ("js", "login.js"), ("js", "import.js"), ("js", "results.js"),
            ("js", "differences.js"), ("js", "detail.js"), ("js", "business-counts.js"), ("js", "admin.js"),
            ("css", "app.css"),
        }
        if (kind, filename) not in allowed:
            return error_response(request, "NOT_FOUND", 404)
        return FileResponse(Path(__file__).parent / "static" / kind / filename)

    def workflow_version():
        with database_transaction(database_path, readonly=True) as connection:
            return connection.execute("PRAGMA user_version").fetchone()[0]

    def legacy_identity(request):
        request.state.workflow_evidence = True
        identity = auth.identity(request.cookies.get("session"))
        request.state.actor = identity.actor
        return identity

    def legacy_scope(request, action):
        identity = legacy_identity(request)
        if action == "counts" and identity.actor.role not in {"FINANCE", "SUPERVISOR"}:
            raise InputError("FORBIDDEN", 403)
        if action == "run" and identity.actor.role == "ADMIN":
            raise InputError("FORBIDDEN", 403)
        return identity

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
        # Legacy schemas have no v4 session API. Keep their read-only controls;
        # v4 uses the business page and its authenticated API, not legacy JS.
        filename = "business-counts.html" if workflow_version() == 4 else "counts.html"
        return FileResponse(Path(__file__).parent / "static/pages" / filename)

    @app.get("/static/{kind}/{filename}")
    def count_assets(kind: str, filename: str, request: Request):
        allowed = {("js", "counts.js"), ("css", "counts.css"), ("js", "run.js")}
        if (kind, filename) not in allowed:
            return error_response(request, "NOT_FOUND", 404)
        return FileResponse(Path(__file__).parent / "static" / kind / filename)

    @app.get("/evidence/counts/{batch_id}")
    def count_json(batch_id: str, request: Request):
        try:
            if workflow_version() == 4:
                identity = legacy_scope(request, "counts")
                result = pipeline_service.import_status(database_path, identity.actor, batch_id, counts_only=True)
            else:
                result = {**counts(database_path, batch_id), "runs": batch_runs(database_path, batch_id)}
            if workflow_version() == 4:
                result["runs"] = batch_runs(database_path, batch_id)
        except (InputError, DatabaseError) as exc:
            if getattr(request.state, "workflow_evidence", False):
                raise
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
            if workflow_version() == 4:
                identity = legacy_scope(request, "run")
                result = pipeline_service.run_status(database_path, identity.actor, run_id)
                if identity.actor.role == "HANDLER" and result.get("scope") == "ASSIGNED_ONLY":
                    result = {key: result[key] for key in ("run_id", "status", "attempt", "normalization_version", "rule", "rule_digest", "scope")}
            else:
                result = run_summary(database_path, run_id)
        except (InputError, DatabaseError) as exc:
            if getattr(request.state, "workflow_evidence", False):
                raise
            return JSONResponse(status_code=exc.status if isinstance(exc, InputError) else 503,
                                content={"error": {"code": exc.code, "message": "Read-only run summary unavailable", "details": {}},
                                         "request_id": request.state.request_id})
        return {**result, "request_id": request.state.request_id}

    return app


app = create_app()
