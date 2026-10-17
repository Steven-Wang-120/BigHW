"""Explicit security-before-fields API boundary. Host never establishes trust."""

import json
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from 主程序.repositories.xlsx_reader import InputError
from starlette.concurrency import run_in_threadpool

from 主程序.services import workflow_service, auth_service, pipeline_service, query_service, export_service
from 主程序.services.snapshot_catalog import SnapshotCatalog
from 主程序.web import schemas
from 主程序.web.uploads import multipart


def canonical_origin(value):
    if not isinstance(value, str) or value == "null" or value != value.strip() or any(ord(ch) < 32 for ch in value):
        raise ValueError("invalid origin")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment or value.endswith(":") or not 1 <= (parsed.port or (443 if parsed.scheme == "https" else 80)) <= 65535:
        raise ValueError("invalid origin")
    if parsed.port == 0:
        raise ValueError("invalid origin")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower(), port


def origin(request, expected):
    try:
        valid = canonical_origin(request.headers.get("origin")) == expected
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise InputError("ORIGIN_FAILED", 403)


def validate(model, value):
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        # Never forward Pydantic input/context (may contain passwords or paths).
        raise InputError("VALIDATION_ERROR", 422, fields=sorted({str(e["loc"][0]) if e["loc"] else "body" for e in exc.errors()})) from exc


def query(request, model=schemas.Body):
    if len(request.query_params.multi_items()) != len(set(request.query_params.keys())):
        raise InputError("VALIDATION_ERROR", 422, fields=["query"])
    values = dict(request.query_params)
    if issubclass(model, schemas.Pagination) or model in {schemas.DifferenceQuery, schemas.AuditQuery}:
        for key in ("page", "page_size"):
            if key in values:
                if len(values[key]) > 7 or not values[key].isascii() or not values[key].isdigit():
                    raise InputError("VALIDATION_ERROR", 422, fields=[key])
                values[key] = int(values[key])
    return validate(model, values)


def json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


async def body(request, model=schemas.Body, *, empty=False):
    # Bound read without buffering an unlimited client body.
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 32768:
            raise InputError("VALIDATION_ERROR", 422, fields=["body"])
        chunks.append(chunk)
    raw = b"".join(chunks)
    if empty and not raw:
        return validate(model, {})
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise InputError("VALIDATION_ERROR", 422, fields=["body"])
    try:
        value = json.loads(raw, object_pairs_hook=json_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as exc:
        raise InputError("VALIDATION_ERROR", 422, fields=["body"]) from exc
    return validate(model, value)


def build_router(database, auth, app_origin, catalog=None):
    catalog = catalog if catalog is not None else SnapshotCatalog()
    async def require_available():
        # Legacy schemas have no v4 rejection-audit store; keep these routes absent.
        if not auth.workflow_available():
            raise InputError("NOT_FOUND", 404)

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_available)])
    expected = canonical_origin(app_origin)
    secure = expected[0] == "https"

    def session(request, action=None, *, mutate=False):
        identity = auth.identity(request.cookies.get("session"))
        request.state.actor = identity.actor
        if mutate:
            origin(request, expected)
            auth.csrf(identity, request.headers.get("x-csrf-token"))
        if action:
            workflow_service.authorize(identity.actor, action)
        return identity

    def result(request, payload, status=200):
        return JSONResponse(status_code=status, content={**payload, "request_id": request.state.request_id})

    def cookies(response, token=None, csrf=None):
        for name, value, httponly in (("session", token, True), ("csrf_token", csrf, False)):
            if value is None:
                response.delete_cookie(name, path="/", secure=secure, httponly=httponly, samesite="strict")
            else:
                response.set_cookie(name, value, path="/", secure=secure, httponly=httponly, samesite="strict", max_age=auth.session_seconds)

    def context(identity, request):
        return pipeline_service.WriteContext(identity.actor, identity.session_id, request.state.request_id)

    @router.get("/snapshots")
    async def snapshots(request: Request):
        session(request, "import")
        data = query(request, schemas.Pagination)
        return result(request, await run_in_threadpool(catalog.listing, **data.model_dump()))

    async def upload(request, pipeline=False):
        identity = session(request, "import", mutate=True)
        query(request)
        model = schemas.PipelineForm if pipeline else schemas.ImportForm
        data, csv_digest = await multipart(request, model, validate)
        operation = pipeline_service.pipeline if pipeline else pipeline_service.imported
        payload = await run_in_threadpool(operation, database, context(identity, request), catalog,
                                          **data.model_dump(), csv_digest=csv_digest)
        return result(request, payload, 201)

    @router.post("/imports")
    async def import_file(request: Request):
        return await upload(request)

    @router.post("/pipeline")
    async def pipeline(request: Request):
        return await upload(request, pipeline=True)

    @router.post("/runs")
    async def start_run(request: Request):
        identity = session(request, "run", mutate=True)
        query(request)
        data = await body(request, schemas.Run)
        payload, status = await run_in_threadpool(pipeline_service.start_run, database, context(identity, request), **data.model_dump())
        return result(request, payload, status)

    @router.get("/imports/{batch_id}")
    async def import_status(batch_id: str, request: Request):
        identity = session(request, "import_read")
        query(request)
        return result(request, pipeline_service.import_status(database, identity.actor, batch_id))

    @router.get("/imports/{batch_id}/counts")
    async def import_counts(batch_id: str, request: Request):
        identity = session(request, "import_read")
        query(request)
        return result(request, pipeline_service.import_status(database, identity.actor, batch_id, counts_only=True))

    @router.get("/runs/{run_id}")
    async def run_status(run_id: str, request: Request):
        identity = session(request, "read")
        query(request)
        return result(request, pipeline_service.run_status(database, identity.actor, run_id))

    @router.post("/auth/login")
    async def login(request: Request):
        origin(request, expected)
        auth.limiter.check(request.client.host if request.client else "unknown")
        query(request)
        data = await body(request, schemas.Login)
        user, token, csrf = auth.login(data.username, data.password, request.state.request_id)
        response = result(request, {"user": user})
        cookies(response, token, csrf)
        return response

    @router.get("/auth/me")
    async def me(request: Request):
        identity = session(request)
        query(request)
        return result(request, {"user": identity.user()})

    @router.post("/auth/logout")
    async def logout(request: Request):
        identity = session(request, mutate=True)
        query(request)
        await body(request, empty=True)
        auth.logout(identity, request.state.request_id)
        response = Response(status_code=204)
        cookies(response)
        return response

    @router.get("/users/handlers")
    async def handlers(request: Request):
        identity = session(request, "handlers")
        query(request)
        return result(request, auth_service.handlers(database, identity.actor))

    @router.post("/users")
    async def create_user(request: Request):
        identity = session(request, "users", mutate=True)
        query(request)
        data = await body(request, schemas.CreateUser)
        user = auth_service.create_user(database, identity.actor, data.username, data.password, data.role, request.state.request_id, session_id=identity.session_id)
        return result(request, {"user": user}, 201)

    @router.patch("/users/{user_id}")
    async def active(user_id: str, request: Request):
        identity = session(request, "users", mutate=True)
        query(request)
        data = await body(request, schemas.Active)
        user = auth_service.set_active(database, identity.actor, user_id, data.active, data.expected_version, request.state.request_id, session_id=identity.session_id)
        return result(request, {"user": user})

    @router.get("/audit")
    async def audit(request: Request):
        from 主程序.services.query_service import audits
        identity = session(request, "audit")
        data = query(request, schemas.AuditQuery)
        return result(request, audits(database, identity.actor, **data.model_dump()))

    @router.get("/runs")
    async def runs(request: Request):
        identity = session(request, "read")
        data = query(request, schemas.RunQuery)
        return result(request, query_service.runs(database, pipeline_service.WriteContext(identity.actor, identity.session_id, request.state.request_id), **data.model_dump()))

    @router.get("/users")
    async def users(request: Request):
        identity = session(request, "users")
        data = query(request, schemas.Pagination)
        return result(request, query_service.users(database, pipeline_service.WriteContext(identity.actor, identity.session_id, request.state.request_id), **data.model_dump()))

    @router.get("/runs/{run_id}/export")
    async def export_run(run_id: str, request: Request):
        identity = session(request, "read")
        data = query(request, schemas.ExportQuery)
        path, meta = await run_in_threadpool(export_service.export_run, database,
                                              pipeline_service.WriteContext(identity.actor, identity.session_id, request.state.request_id),
                                              run_id, reason=data.type, status=data.status, assignee_id=data.assignee_id)
        return export_service.ExportResponse(path, database,
                                              pipeline_service.WriteContext(identity.actor, identity.session_id, request.state.request_id),
                                              run_id, meta)

    @router.get("/runs/{run_id}/differences")
    async def differences(run_id: str, request: Request):
        identity = session(request, "read")
        data = query(request, schemas.DifferenceQuery)
        return result(request, workflow_service.differences(database, identity.actor, run_id, reason=data.type, status=data.status,
                      assignee_id=data.assignee_id, page=data.page, page_size=data.page_size))

    @router.get("/differences/{difference_id}")
    async def detail(difference_id: str, request: Request):
        identity = session(request, "read")
        query(request)
        return result(request, workflow_service.detail(database, identity.actor, difference_id))

    async def change(request, difference_id, event, model):
        identity = session(request, event, mutate=True)
        query(request)
        data = await body(request, model)
        payload = workflow_service.change(database, identity.actor, difference_id, event, data.expected_version,
                    request.state.request_id, session_id=identity.session_id, **data.model_dump(exclude={"expected_version"}))
        return result(request, payload)

    @router.post("/differences/{difference_id}/assign")
    async def assign(difference_id: str, request: Request):
        return await change(request, difference_id, "assign", schemas.Assign)

    @router.post("/differences/{difference_id}/start")
    async def start(difference_id: str, request: Request):
        return await change(request, difference_id, "start", schemas.Start)

    @router.post("/differences/{difference_id}/close")
    async def close(difference_id: str, request: Request):
        return await change(request, difference_id, "close", schemas.Close)

    return router
