"""Opt-in B09 acceptance harness; never collected by the default pytest run.

python 测试/nfr_http.py --mode smoke --python <venv-python>
python 测试/nfr_http.py --mode formal --python <venv-python> --hardware-note ...
    --power-plan ... --load-note ...

Formal = 3 independent empty v4 DBs, F/S queries 30 each, 10+10 TCP
races on separate F data, paired SQL-only index experiments. Smoke is F only
and is NOT official-data NFR acceptance. No production source is modified.
"""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import argparse
import functools
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import sqlite3
import statistics
import subprocess
import sys
from threading import Barrier, Lock, Thread
import time
from uuid import uuid4

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from 测试.run_checks import bind_project, source_digest

SCHEMA_VERSION = 1
PIPELINE_LIMIT = 5.0
QUERY_LIMIT = 2.0
INDEX_NAMES = ("difference_run_reason", "difference_work_scope")


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def inside(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("output must be inside this checkout")
    return path


def distribution(values):
    return {"values_seconds": values, "first_seconds": values[0] if values else None,
            "min_seconds": min(values) if values else None, "max_seconds": max(values) if values else None,
            "mean_seconds": statistics.mean(values) if values else None,
            "median_seconds": statistics.median(values) if values else None}


def every_under(values, limit, expected):
    return len(values) == expected and all(0 <= v <= limit for v in values)


def environment(args):
    data = {"recorded_utc": now(), "recorded_local": datetime.now().astimezone().isoformat(),
            "python": sys.version, "executable": sys.executable, "requested_python": args.python,
            "platform": platform.platform(), "processor": platform.processor(), "cpu_count": os.cpu_count(),
            "sqlite": sqlite3.sqlite_version, "lock_sha256": sha(ROOT / "requirements.lock"),
            "dependencies": {name: importlib.metadata.version(name) for name in
                             ("fastapi", "uvicorn", "openpyxl", "httpx", "bcrypt", "pytest")},
            "hardware_note": args.hardware_note, "power_plan": args.power_plan, "load_note": args.load_note,
            "worker_count": 1, "os_cache_cleared": False}
    # Read-only local hardware inventory. Preserve failures rather than guessing.
    if os.name == "nt":
        commands = {
            "hardware": "Get-CimInstance Win32_Processor | Select-Object Name; Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory; Get-PhysicalDisk | Select-Object FriendlyName,MediaType,BusType",
            "power": "powercfg /getactivescheme",
        }
        for name, command in commands.items():
            try:
                p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                   capture_output=True, timeout=20)
                data[name + "_probe"] = {"command": command, "exit_code": p.returncode,
                                         "stdout": p.stdout.decode(errors="replace"), "stderr": p.stderr.decode(errors="replace")}
            except (OSError, subprocess.TimeoutExpired) as exc:
                data[name + "_probe"] = {"error_type": type(exc).__name__}
    return data


def synthetic_input(directory, cancel_count=24):
    """Source-native synthetic OOXML, then the real full scanner (bounded F)."""
    from 测试.synthetic_xlsx import synthetic_workbook
    from 主程序.services.snapshot_service import prepare_snapshot
    from 主程序.repositories.xlsx_reader import SHEETS
    directory.mkdir()
    rows = {2: ["F-SALE", "F-SKU", "synthetic sale", "10", "40500", "1.0050", "12345", "UK"]}
    for i in range(cancel_count):
        rows[i + 3] = [f"CF-{i}", f"F-UNMATCHED-{i}", "synthetic cancel", "-1", "40501", "1.0050", "12345", "UK"]
    source = synthetic_workbook(directory / "source.xlsx", rows, tail=cancel_count + 4)
    started = time.perf_counter()
    result = prepare_snapshot(source, directory / "snapshot", expected_sha=sha(source),
                              selection=(SHEETS[1], 2, cancel_count + 2))
    return {"kind": "F", "root": str(directory), "source": str(source), "expected_sha": sha(source),
            "registration": {"snapshot_id": result["snapshot_id"], "directory": "snapshot",
                             "manifest_digest": result["manifest_digest"]},
            "preprocessing": {"performed": True, "bounded": True, "seconds": time.perf_counter() - started,
                              "note": "F OOXML generation and complete bounded scan; outside all pipeline timers"}}


def formal_input(snapshot_id=None):
    """Select ONLY a production-registered anchor; never register user R manifests."""
    from 主程序.services.snapshot_catalog import SnapshotCatalog
    catalog = SnapshotCatalog()
    candidates = []
    for entry in catalog.registrations:
        path = catalog.root / entry.directory / "snapshot_10k.json"
        if snapshot_id and entry.snapshot_id != snapshot_id:
            continue
        if path.is_file():
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if sha(path) != entry.manifest_digest or manifest["snapshot_id"] != entry.snapshot_id:
                raise ValueError("registered manifest mismatch")
            candidates.append((manifest.get("manifest_version", 1), entry.directory, entry, manifest))
    if not candidates:
        raise ValueError("no available registered official snapshot; prepare/review/register on coordinator first")
    _, _, entry, manifest = max(candidates, key=lambda v: (v[0], v[1]))
    return {"kind": "R", "root": str(catalog.root), "source": str(catalog.source), "expected_sha": catalog.expected_sha,
            "registration": {"snapshot_id": entry.snapshot_id, "directory": entry.directory, "manifest_digest": entry.manifest_digest},
            "catalog_choice": "registered quality v2 preferred; original registered snapshot otherwise",
            "manifest_version": manifest.get("manifest_version", 1),
            "preprocessing": {"performed": False, "seconds": None,
                              "note": "existing trusted artifacts; extraction/full scan excluded, not rerun or falsely timed by this harness"}}


def input_files(config):
    directory = Path(config["root"]) / config["registration"]["directory"]
    paths = [Path(config["source"]), directory / "snapshot_10k.json", directory / "full_scan.json",
             directory / "retail_2010_rows_2_10001.csv"]
    return [{"path": str(p), "sha256": sha(p), "bytes": p.stat().st_size} for p in paths]


def make_catalog(config):
    from 主程序.services.snapshot_catalog import SnapshotCatalog, Registration
    return SnapshotCatalog(root=Path(config["root"]), source=Path(config["source"]),
                           expected_sha=config["expected_sha"], registrations=(Registration(**config["registration"]),))


class Timings:
    """Transparent wrappers call the real function once with unchanged arguments.

    Inclusive/nested totals are NOT additive; classify records an aggregate of
    individual row calls. Overhead is included in the independent HTTP timer.
    """
    def __init__(self):
        self.records, self.originals, self.lock = {}, [], Lock()

    def add(self, name, elapsed):
        with self.lock:
            record = self.records.setdefault(name, {"calls": 0, "inclusive_seconds": 0.0})
            record["calls"] += 1
            record["inclusive_seconds"] += elapsed

    def wrap(self, owner, name, label, asynchronous=False):
        original = getattr(owner, name)
        if asynchronous:
            @functools.wraps(original)
            async def wrapped(*args, **kwargs):
                start = time.perf_counter()
                try:
                    return await original(*args, **kwargs)
                finally:
                    self.add(label, time.perf_counter() - start)
        else:
            @functools.wraps(original)
            def wrapped(*args, **kwargs):
                start = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    self.add(label, time.perf_counter() - start)
        self.originals.append((owner, name, original))
        setattr(owner, name, wrapped)

    def install(self):
        from 主程序.web import api
        from 主程序.services import snapshot_catalog, import_service, reconcile_service
        from 主程序.repositories import import_repository, reconcile_repository
        self.wrap(api, "multipart", "http_read_parse_upload", True)
        self.wrap(snapshot_catalog.SnapshotCatalog, "verified", "verify_manifest_full_source_csv")
        self.wrap(import_service, "classify_row", "classify_normalize_rows")
        self.wrap(import_service, "mark_suspected_duplicates", "duplicate_flags")
        self.wrap(import_service, "import_verified", "import_inclusive")
        self.wrap(import_repository, "insert_rows", "insert_rows")
        self.wrap(import_repository, "finish_batch", "publish_import_audit")
        self.wrap(reconcile_service, "reconcile_rows", "lots_match_differences")
        self.wrap(reconcile_repository, "publish", "publish_run_differences_audit")

    def restore(self):
        for owner, name, original in reversed(self.originals):
            setattr(owner, name, original)


def initialize(directory):
    from 主程序.repositories.database import initialize_database, upgrade_database, database_transaction
    from 主程序.services.auth_service import create_user
    from 主程序.domain.permissions import Actor
    db = directory / "fresh.sqlite3"
    if db.exists():
        raise ValueError("refusing existing database")
    start = time.perf_counter()
    if not initialize_database(db):
        raise ValueError("database was not new")
    upgrade_database(db, target_version=4)
    init_seconds = time.perf_counter() - start
    start = time.perf_counter()
    password = secrets.token_urlsafe(24)
    admin = create_user(db, None, "nfr-admin", password, "ADMIN", uuid4().hex, bootstrap=True)
    actor = Actor(admin["id"], "ADMIN")
    users = {}
    for username, role in (("finance", "FINANCE"), ("supervisor", "SUPERVISOR"), ("h1", "HANDLER"), ("h2", "HANDLER")):
        users[username] = create_user(db, actor, username, password, role, uuid4().hex)
    with database_transaction(db, readonly=True) as c:
        empty = {name: c.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in
                 ("import_batches", "import_rows", "runs", "sale_lots", "allocations", "differences")}
    if any(empty.values()):
        raise ValueError("business database not empty")
    return db, users, password, {"database_init_seconds": init_seconds, "account_hash_init_seconds": time.perf_counter() - start,
                                 "business_counts_before": empty}


@contextmanager
def live_server(db, catalog, record):
    import uvicorn
    from 主程序.web.app import create_app
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    server = None
    thread = None
    start = time.perf_counter()
    record.update(pid=os.getpid(), host="127.0.0.1", port=port, workers=1, origin=origin)
    try:
        app = create_app(db, app_origin=origin, snapshot_catalog=catalog)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, workers=1,
                                loop="asyncio", http="h11", ws="none", access_log=False, log_level="warning"))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("own server startup failed")
            time.sleep(.01)
        record["startup_seconds"] = time.perf_counter() - start
        yield origin
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=15)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=5)
        sock.close()
        record["thread_stopped"] = thread is None or not thread.is_alive()
        with socket.socket() as probe:
            probe.settimeout(.25)
            record["port_closed"] = probe.connect_ex(("127.0.0.1", port)) != 0
        record["stopped_utc"] = now()
        if not record["thread_stopped"] or not record["port_closed"]:
            raise RuntimeError("own server cleanup failed")


def client(origin):
    import httpx
    return httpx.Client(base_url=origin, timeout=90, trust_env=False,
                        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))


def login(c, username, password, origin):
    start = time.perf_counter()
    response = c.post("/api/v1/auth/login", json={"username": username, "password": password}, headers={"Origin": origin})
    payload = response.json()
    if response.status_code != 200:
        raise RuntimeError("login failed: " + str(response.status_code) + " " + payload.get("error", {}).get("code", ""))
    return time.perf_counter() - start


def headers(c, origin):
    return {"Origin": origin, "X-CSRF-Token": c.cookies["csrf_token"]}


def upload(c, origin, config):
    from 主程序.domain.matching import RULE
    directory = Path(config["root"]) / config["registration"]["directory"]
    fields = {k: config["registration"][k] for k in ("snapshot_id", "manifest_digest")}
    fields["matching_rule_version"] = RULE["version"]
    # Opening AND reading the CSV are inside the caller's timer.
    with (directory / "retail_2010_rows_2_10001.csv").open("rb") as csv:
        response = c.post("/api/v1/pipeline", data=fields, files={"file": ("trusted.csv", csv, "application/vnd.ms-excel")},
                          headers=headers(c, origin))
    payload = response.json()  # full response read and JSON decode included
    return response, payload


def persisted(db, run_id, batch_id):
    from 主程序.repositories.database import database_transaction
    with database_transaction(db, readonly=True) as c:
        summary = json.loads(c.execute("SELECT summary_json FROM runs WHERE id=? AND status='COMPLETED'", (run_id,)).fetchone()[0])
        counts = json.loads(c.execute("SELECT counts_json FROM import_batches WHERE id=? AND status='SUCCESS'", (batch_id,)).fetchone()[0])
        result = {"summary": summary, "counts": counts}
        result["rows_by_class"] = dict(c.execute("SELECT class,COUNT(*) FROM import_rows WHERE batch_id=? GROUP BY class", (batch_id,)))
        result["persisted_counts"] = {table: c.execute("SELECT COUNT(*) FROM " + table + " WHERE " +
                                      ("batch_id" if table == "import_rows" else "run_id") + "=?",
                                      (batch_id if table == "import_rows" else run_id,)).fetchone()[0]
                                      for table in ("import_rows", "sale_lots", "sale_lot_rows", "allocations", "differences")}
        result["success_audits"] = dict(c.execute("SELECT action,COUNT(*) FROM audit_events WHERE target_id IN (?,?) AND outcome='SUCCESS' GROUP BY action", (batch_id, run_id)))
        result["foreign_key_violations"] = len(c.execute("PRAGMA foreign_key_check").fetchall())
        # Independent persisted integer triangle, not copied from expected data.
        cancels = c.execute("SELECT quantity,amount_units FROM import_rows WHERE batch_id=? AND class='CANCEL'", (batch_id,)).fetchall()
        allocated = c.execute("SELECT quantity,amount_units FROM allocations WHERE run_id=?", (run_id,)).fetchall()
        unmatched = c.execute("SELECT quantity,amount_units FROM differences WHERE run_id=?", (run_id,)).fetchall()
        result["integer_triangles"] = {name: {"cancel": str(sum(abs(r[i]) for r in cancels)),
                                           "allocated": str(sum(r[i] for r in allocated)), "unmatched": str(sum(r[i] for r in unmatched)),
                                           "delta": str(sum(abs(r[i]) for r in cancels) - sum(r[i] for r in allocated) - sum(r[i] for r in unmatched))}
                                       for i, name in enumerate(("quantity", "amount_units"))}
        return result


def validate_observed(data, summary, kind):
    """Expected R invariants are assertions, NEVER used as measured results."""
    s, counts, tables = data["summary"], data["counts"], data["persisted_counts"]
    checks = {
        "completed": summary.get("status") == "COMPLETED" and summary.get("attempt") == 1,
        "source_conservation": counts["delta_original"] == counts["delta_selected"] == 0,
        "class_rows": tables["import_rows"] == counts["selected_count"] == sum(data["rows_by_class"].values()),
        "class_aggregates": data["rows_by_class"].get("SALE", 0) == counts["sale_count"] and data["rows_by_class"].get("CANCEL", 0) == counts["cancel_count"] and data["rows_by_class"].get("EXCLUDED", 0) == counts["excluded_count"],
        "match_conservation": s["matched"] + s["suspected"] + s["no_original"] == s["product_valid_cancel"],
        "published_counts": tables["differences"] == s["difference_count"] and tables["allocations"] == s["matched"],
        "integer_triangles": all(t["delta"] == "0" for t in data["integer_triangles"].values()),
        "audits": data["success_audits"] == {"IMPORT": 1, "RECONCILE": 1},
        "foreign_keys": data["foreign_key_violations"] == 0,
        "summary_matches_db": all(summary.get(k) == v for k, v in s.items()),
    }
    if kind == "R":
        expected_counts = {"original_total": 1067371, "selected_count": 10000, "sale_count": 9854, "cancel_count": 100, "excluded_count": 46}
        expected_match = {"product_valid_cancel": 98, "matched": 10, "suspected": 0, "no_original": 88, "difference_count": 90}
        checks["official_fixed_range"] = all(counts.get(k) == v for k, v in expected_counts.items()) and all(s.get(k) == v for k, v in expected_match.items())
    return checks


def query_measurements(db, origin, password, run_id, repeats):
    """Never persist real list/detail bodies: only status, timing and SHA/length."""
    from 主程序.repositories.database import database_transaction
    with database_transaction(db, readonly=True) as connection:
        actual_d = connection.execute("SELECT COUNT(*) FROM differences WHERE run_id=?", (run_id,)).fetchone()[0]
        target = connection.execute("SELECT id FROM differences WHERE run_id=? ORDER BY id LIMIT 1", (run_id,)).fetchone()
    record = {"actual_D": actual_d, "repeats_per_role_per_endpoint": repeats, "roles": {},
              "first_note": "first measured query per role/endpoint; OS cache NOT cleared; earlier server/DB validation may warm cache",
              "browser_render_included": False}
    if not target:
        record.update(passed=False, reason="D=0; no authorized real detail, no synthetic data inserted into R")
        return record
    for role in ("finance", "supervisor"):
        with client(origin) as c:
            login(c, role, password, origin)
            role_record = {}
            for endpoint, path in (("list", f"/api/v1/runs/{run_id}/differences?page=1&page_size=50"),
                                   ("detail", "/api/v1/differences/" + target[0])):
                values, responses = [], []
                for _ in range(repeats):
                    start = time.perf_counter()
                    response = c.get(path)
                    payload = response.json()
                    elapsed = time.perf_counter() - start
                    valid = response.status_code == 200 and (payload.get("total") == actual_d and len(payload.get("items", [])) == min(actual_d, 50) if endpoint == "list" else payload.get("id") == target[0])
                    values.append(elapsed)
                    responses.append({"status": response.status_code, "valid": valid,
                                      "bytes": len(response.content), "body_sha256": hashlib.sha256(response.content).hexdigest()})
                role_record[endpoint] = {**distribution(values), "responses": responses,
                                         "passed": every_under(values, QUERY_LIMIT, repeats) and all(r["valid"] for r in responses)}
            record["roles"][role] = role_record
    record["passed"] = all(v["passed"] for role in record["roles"].values() for v in role.values())
    return record


def pipeline_worker(config, result):
    directory = Path(config["directory"])
    db, _, password, setup = initialize(directory)
    result.update(database=str(db), setup=setup, server={})
    timings = Timings()
    timings.install()
    try:
        with live_server(db, make_catalog(config["input"]), result["server"]) as origin, client(origin) as c:
            setup["finance_login_seconds"] = login(c, "finance", password, origin)
            result["timed_start_utc"] = now()
            start = time.perf_counter()
            response, payload = upload(c, origin, config["input"])
            if response.status_code == 201 and payload.get("status") == "COMPLETED":
                summary_response = c.get("/api/v1/runs/" + payload["run_id"])
                summary = summary_response.json()
            else:
                summary_response, summary = None, {}
            result["http_seconds"] = time.perf_counter() - start
            result["timed_end_utc"] = now()
            result["pipeline_status"] = response.status_code
            result["summary_status"] = summary_response.status_code if summary_response is not None else None
            result["server_stages"] = dict(timings.records)
            result["stage_note"] = "Inclusive/nested measurements, NOT additive; all wrapper overhead is inside HTTP total"
            if response.status_code != 201 or summary_response is None or summary_response.status_code != 200:
                result.update(passed=False, error_code=payload.get("error", {}).get("code", "PIPELINE_OR_SUMMARY_FAILED"))
                return
            observed = persisted(db, payload["run_id"], payload["batch_id"])
            result.update(observed=observed, checks=validate_observed(observed, summary, config["input"]["kind"]),
                          run_id=payload["run_id"], batch_id=payload["batch_id"],
                          quality=summary.get("quality"), source_counts=summary.get("source_counts"),
                          normalization_version=summary.get("normalization_version"), rule_digest=summary.get("rule_digest"), rule=summary.get("rule"))
            result["passed"] = result["http_seconds"] <= PIPELINE_LIMIT and all(result["checks"].values())
            if config.get("query_repeats"):
                result["queries"] = query_measurements(db, origin, password, payload["run_id"], config["query_repeats"])
    finally:
        timings.restore()
        result["database_bytes"] = db.stat().st_size


def fresh_differences(db, run_id, count):
    """Clone F-only source/difference templates; never modifies an R database."""
    from 主程序.repositories.database import database_transaction
    with database_transaction(db) as c:
        source_cursor = c.execute("SELECT * FROM import_rows WHERE id=(SELECT cancel_id FROM differences WHERE run_id=? LIMIT 1)", (run_id,))
        source_columns = [v[0] for v in source_cursor.description]
        template_source = list(source_cursor.fetchone())
        cursor = c.execute("SELECT * FROM differences WHERE run_id=? LIMIT 1", (run_id,))
        columns = [v[0] for v in cursor.description]
        template = list(cursor.fetchone())
        ids = []
        for i in range(count):
            row = template_source.copy()
            row[source_columns.index("id")] = "F-race-source-" + str(i)
            row[source_columns.index("source_row")] = 1000 + i
            c.execute("INSERT INTO import_rows VALUES (" + ",".join("?" for _ in row) + ")", row)
            difference = template.copy()
            did = "F-race-difference-" + str(i)
            difference[columns.index("id")] = did
            difference[columns.index("cancel_id")] = row[source_columns.index("id")]
            c.execute("INSERT INTO differences VALUES (" + ",".join("?" for _ in difference) + ")", difference)
            ids.append(did)
        return ids


def race_worker(config, result):
    """Two actual TCP connections + independent login sessions; no TestClient."""
    from 主程序.repositories.database import database_transaction
    if config["input"]["kind"] != "F":
        raise ValueError("races require pure synthetic data")
    directory = Path(config["directory"])
    db, users, password, setup = initialize(directory)
    result.update(database=str(db), setup=setup, server={}, rounds=[], data_kind="F")
    with live_server(db, make_catalog(config["input"]), result["server"]) as origin:
        with client(origin) as finance:
            login(finance, "finance", password, origin)
            response, payload = upload(finance, origin, config["input"])
            if response.status_code != 201:
                raise RuntimeError("F race setup pipeline failed")
        count = config["race_rounds"]
        ids = fresh_differences(db, payload["run_id"], count * 2)
        # Four logins, separate supervisors and two sessions for the SAME handler
        # at close: two different handlers would violate object authorization.
        with client(origin) as a, client(origin) as b, client(origin) as h1, client(origin) as h1_second:
            for c, username in ((a, "supervisor"), (b, "supervisor"), (h1, "h1"), (h1_second, "h1")):
                login(c, username, password, origin)
            result["sessions_independent"] = a.cookies["session"] != b.cookies["session"] and h1.cookies["session"] != h1_second.cookies["session"]
            for event in ("assign", "close"):
                for i in range(count):
                    did = ids[i if event == "assign" else count + i]
                    if event == "close":
                        assert a.post(f"/api/v1/differences/{did}/assign", json={"expected_version": 0, "assignee_id": users["h1"]["id"]}, headers=headers(a, origin)).status_code == 200
                        assert h1.post(f"/api/v1/differences/{did}/start", json={"expected_version": 1}, headers=headers(h1, origin)).status_code == 200
                    barrier = Barrier(2)
                    def send(n):
                        c = (a, b)[n] if event == "assign" else (h1, h1_second)[n]
                        fields = {"expected_version": 0, "assignee_id": users[("h1", "h2")[n]]["id"]} if event == "assign" else {"expected_version": 2, "resolution_note": f"F-note-{i}-{n}"}
                        barrier.wait(timeout=10)
                        start = time.perf_counter()
                        timestamp = now()
                        r = c.post(f"/api/v1/differences/{did}/{event}", json=fields, headers=headers(c, origin))
                        body = r.json()
                        return {"client": n, "sent_utc": timestamp, "sent_perf_counter": start,
                                "seconds": time.perf_counter() - start, "status": r.status_code,
                                "request_id": r.headers.get("X-Request-ID"), "response": body}
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        responses = list(pool.map(send, (0, 1)))
                    with database_transaction(db, readonly=True) as c:
                        row = c.execute("SELECT version,work_status,assignee_id,resolution_note FROM differences WHERE id=?", (did,)).fetchone()
                        assignments = c.execute("SELECT assignee_id FROM assignments WHERE difference_id=?", (did,)).fetchall()
                        audits = c.execute("SELECT request_id FROM audit_events WHERE target_id=? AND action=? AND outcome='SUCCESS'", (did, event.upper())).fetchall()
                    winners = [r for r in responses if r["status"] == 200]
                    losers = [r for r in responses if r["status"] == 409]
                    winner = winners[0] if len(winners) == 1 else None
                    expected_code = "ALREADY_ASSIGNED" if event == "assign" else "VERSION_CONFLICT"
                    ok = (len(winners) == len(losers) == 1 and losers[0]["response"].get("error", {}).get("code") == expected_code
                          and row[0] == (1 if event == "assign" else 3) and len(assignments) == 1 and len(audits) == 1
                          and audits[0][0] == winner["request_id"] and row[2] == winner["response"].get("assignee_id")
                          and assignments[0][0] == row[2] and row[1] == ("PENDING" if event == "assign" else "CLOSED")
                          and (event == "assign" or row[3] == f"F-note-{i}-{winner['client']}"))
                    result["rounds"].append({"event": event, "round": i + 1, "difference_id": did,
                        "responses": responses, "send_skew_seconds": abs(responses[0]["sent_perf_counter"] - responses[1]["sent_perf_counter"]),
                        "db_version": row[0], "db_status": row[1], "db_assignee": row[2], "db_note": row[3],
                        "assignment_count": len(assignments), "success_audit_count": len(audits), "winner_client": winner["client"] if winner else None, "passed": ok})
            result["passed"] = result["sessions_independent"] and len(result["rounds"]) == count * 2 and all(r["passed"] for r in result["rounds"])


def sql_copy(source, destination):
    if destination.exists():
        raise ValueError("refusing existing index comparison DB")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)


def sql_benchmark(config, result):
    """SQL ONLY: strict API schema rejects dropped-index copies, as documented.

    Same snapshot data + SQL + transaction count. Only two named non-unique
    performance indexes differ. Never drop constraint/autoindexes or triggers.
    Writes UPDATE existing rows inside identical rolled-back transactions to
    isolate index maintenance cost, not pretend to measure HTTP pipeline writes.
    """
    source = Path(config["source_database"])
    directory = Path(config["directory"])
    before = sha(source)
    result.update(scope="SQL-only; NOT schema-equivalent API or end-to-end NFR", source_database_sha256=before,
                  repeats=config["index_repeats"], dropped_indexes=list(INDEX_NAMES), cases={})
    paths = {"indexed": directory / "indexed.sqlite3", "without_extra_indexes": directory / "without-extra.sqlite3"}
    for path in paths.values():
        sql_copy(source, path)
    with sqlite3.connect(paths["without_extra_indexes"]) as c:
        for name in INDEX_NAMES:
            index = next((row for row in c.execute("PRAGMA index_list(differences)") if row[1] == name), None)
            if index is None or index[2] or index[3] != "c":
                raise ValueError("requested index missing or is a constraint: " + name)
            c.execute('DROP INDEX "' + name + '"')
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as c:
        run_id = c.execute("SELECT id FROM runs WHERE status='COMPLETED' ORDER BY id LIMIT 1").fetchone()[0]
    queries = {
        "first50_pending": ("SELECT * FROM differences WHERE run_id=? AND work_status=? ORDER BY id LIMIT 50 OFFSET 0", (run_id, "PENDING")),
        "count_reason": ("SELECT COUNT(*) FROM differences WHERE run_id=? AND reason=?", (run_id, "NO_CANDIDATE_IN_RANGE")),
    }
    schema_objects = {}
    for case, path in paths.items():
        with sqlite3.connect(path) as c:
            c.execute("PRAGMA foreign_keys=ON")
            schema_objects[case] = dict(c.execute("SELECT name,sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'"))
            result["cases"][case] = {"queries": {}, "db_bytes": path.stat().st_size}
            for name, (sql, params) in queries.items():
                plan = c.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
                result["cases"][case]["queries"][name] = {"sql": sql, "parameter_values": ["<same-completed-run>", params[1]],
                                                               "plan": plan, "values_seconds": [], "row_counts": []}
            result["cases"][case]["write"] = {"sql": "UPDATE differences SET work_status=work_status WHERE run_id=?",
                                                       "transaction": "BEGIN IMMEDIATE / UPDATE / ROLLBACK", "values_seconds": []}
    comparable_objects = {k: v for k, v in schema_objects["indexed"].items() if k not in INDEX_NAMES}
    reduced_objects = {k: v for k, v in schema_objects["without_extra_indexes"].items() if k not in INDEX_NAMES}
    result["only_requested_indexes_differ"] = comparable_objects == reduced_objects
    # Alternate paired order to make cache/order effects visible rather than always favoring one case.
    for iteration in range(config["index_repeats"]):
        order = list(paths) if iteration % 2 == 0 else list(reversed(paths))
        for case in order:
            with sqlite3.connect(paths[case]) as c:
                c.execute("PRAGMA foreign_keys=ON")
                for name, (sql, params) in queries.items():
                    start = time.perf_counter()
                    rows = c.execute(sql, params).fetchall()
                    elapsed = time.perf_counter() - start
                    entry = result["cases"][case]["queries"][name]
                    entry["values_seconds"].append(elapsed)
                    entry["row_counts"].append(len(rows))
                start = time.perf_counter()
                c.execute("BEGIN IMMEDIATE")
                c.execute("UPDATE differences SET work_status=work_status WHERE run_id=?", (run_id,))
                c.rollback()
                result["cases"][case]["write"]["values_seconds"].append(time.perf_counter() - start)
    for record in result["cases"].values():
        for entry in [*record["queries"].values(), record["write"]]:
            entry.update(distribution(entry["values_seconds"]))
    result["indexed_over_without_mean_ratios"] = {
        name: result["cases"]["indexed"]["queries"][name]["mean_seconds"] / result["cases"]["without_extra_indexes"]["queries"][name]["mean_seconds"] for name in queries}
    result["write_indexed_over_without_mean_ratio"] = result["cases"]["indexed"]["write"]["mean_seconds"] / result["cases"]["without_extra_indexes"]["write"]["mean_seconds"]
    result["ratio_note"] = ">1 means indexed slower; no speedup asserted, small D may have negative benefit"
    result["source_database_unchanged"] = before == sha(source)
    result["passed"] = result["only_requested_indexes_differ"] and result["source_database_unchanged"]


def worker(config_path):
    origins = bind_project()
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    directory = inside(config["directory"])
    result = {"schema_version": SCHEMA_VERSION, "kind": config["kind"], "started_utc": now(),
              "started_local": datetime.now().astimezone().isoformat(), "import_origins": origins, "passed": False}
    before, sources = source_digest()
    try:
        {"pipeline": pipeline_worker, "races": race_worker, "index": sql_benchmark}[config["kind"]](config, result)
    except Exception as exc:
        # Do not print exception/body/SQL params which could contain R rows.
        result["error_type"] = type(exc).__name__
        result["error_code"] = getattr(exc, "code", None)
        result["passed"] = False
    finally:
        after, _ = source_digest()
        result.update(finished_utc=now(), source_tree_digest_before=before, source_tree_digest_after=after,
                      source_unchanged=before == after, source_files=sources)
        if "server" in result and not (result["server"].get("thread_stopped") and result["server"].get("port_closed")):
            result["passed"] = False
        result["passed"] = result["passed"] and result["source_unchanged"]
        write_json(directory / "measurement.json", result)
    print(json.dumps({"kind": config["kind"], "passed": result["passed"], "measurement": str(directory / "measurement.json")}, ensure_ascii=False))
    return 0 if result["passed"] else 1


def execute_worker(python, config):
    directory = inside(config["directory"])
    directory.mkdir()
    config_path = directory / "config.json"
    write_json(config_path, config)
    command = [python, "-B", str(ROOT / "测试/nfr_http.py"), "--worker", str(config_path)]
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    write_json(directory / "command.json", command)
    with (directory / "stdout.txt").open("wb") as out, (directory / "stderr.txt").open("wb") as err:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=out, stderr=err)
        try:
            code = process.wait(timeout=240)
        except subprocess.TimeoutExpired:
            # This is exactly the child we spawned, never a guessed PID/port.
            process.kill()
            process.wait(timeout=20)
            code = 124
    path = directory / "measurement.json"
    result = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"passed": False, "error": "worker did not publish measurement"}
    result.update(child_pid=process.pid, child_exit_code=code, child_process_stopped=process.poll() is not None)
    if result.get("server"):
        with socket.socket() as probe:
            probe.settimeout(.25)
            result["port_closed_after_process"] = probe.connect_ex(("127.0.0.1", result["server"]["port"])) != 0
    result["passed"] = result.get("passed", False) and code == 0 and result["child_process_stopped"] and result.get("port_closed_after_process", True)
    write_json(path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=("smoke", "formal"), default="smoke")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--output-root", type=Path, default=ROOT / "数据/runtime/B09")
    parser.add_argument("--data-root", type=Path, help="F-only generated input parent; never overrides production R trust")
    parser.add_argument("--snapshot-id", help="formal: select an already production-registered snapshot")
    parser.add_argument("--hardware-note", default="not supplied (smoke)")
    parser.add_argument("--power-plan", default="not supplied (smoke)")
    parser.add_argument("--load-note", default="not supplied (smoke)")
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    origins = bind_project()
    if args.mode == "formal" and (args.data_root or any(v == "not supplied (smoke)" for v in (args.hardware_note, args.power_plan, args.load_note))):
        parser.error("formal requires hardware/power/load notes and prohibits --data-root")
    output_root = inside(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / (args.mode + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir()
    before, sources = source_digest()
    result = {"schema_version": SCHEMA_VERSION, "mode": args.mode, "official_nfr_acceptance": args.mode == "formal",
              "started_utc": now(), "started_local": datetime.now().astimezone().isoformat(), "output": str(output),
              "import_origins": origins, "environment": environment(args), "pipeline_rounds": [], "passed": False,
              "cache_note": "cold EMPTY business DBs only; OS file cache NOT cleared",
              "timed_region": "CSV open/read + HTTP upload + full validation/normalization/import/match/differences/audit + COMPLETED + GET full summary JSON read",
              "excluded": "dependency install, original download/extraction/full-scan preparation, fresh schema/accounts, app/startup and login; separate measurements where performed",
              "limits": {"pipeline_each_seconds": PIPELINE_LIMIT, "query_each_seconds": QUERY_LIMIT}}
    try:
        data_parent = inside(args.data_root) if args.data_root else output
        data_parent.mkdir(parents=True, exist_ok=True)
        f_input = synthetic_input(data_parent / ("F-input-" + uuid4().hex[:8]))
        selected = formal_input(args.snapshot_id) if args.mode == "formal" else f_input
        result.update(input=selected, input_files_before=input_files(selected), synthetic_input_files=input_files(f_input))
        rounds, query_repeats, race_rounds, index_repeats = (3, 30, 10, 30) if args.mode == "formal" else (1, 3, 2, 3)
        for i in range(rounds):
            round_result = execute_worker(args.python, {"kind": "pipeline", "directory": str(output / f"pipeline-{i+1}"), "input": selected,
                                                        "query_repeats": query_repeats if i == rounds - 1 else 0})
            result["pipeline_rounds"].append(round_result)
        result["races"] = execute_worker(args.python, {"kind": "races", "directory": str(output / "F-races"), "input": f_input, "race_rounds": race_rounds})
        successful = [r for r in result["pipeline_rounds"] if r.get("run_id")]
        if successful:
            result["index"] = execute_worker(args.python, {"kind": "index", "directory": str(output / "index-sql-only"),
                                                          "source_database": successful[-1]["database"], "index_repeats": index_repeats})
        else:
            result["index"] = {"passed": False, "reason": "no completed pipeline database"}
        result["input_files_after"] = input_files(selected)
        result["input_unchanged"] = result["input_files_before"] == result["input_files_after"]
        result["verdicts"] = {
            "NFR01": len(result["pipeline_rounds"]) == rounds and all(r["passed"] for r in result["pipeline_rounds"]),
            "NFR02": result["pipeline_rounds"][-1].get("queries", {}).get("passed", False),
            "NFR03": result["races"]["passed"], "index_comparison_completed_not_speedup_claim": result["index"]["passed"],
            "input_unchanged": result["input_unchanged"]}
        result["passed"] = all(result["verdicts"].values())
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error_code=getattr(exc, "code", None), passed=False)
    finally:
        after, _ = source_digest()
        result.update(finished_utc=now(), finished_local=datetime.now().astimezone().isoformat(),
                      source_tree_digest_before=before, source_tree_digest_after=after, source_unchanged=before == after, source_files=sources)
        result["passed"] = result["passed"] and result["source_unchanged"]
        write_json(output / "measurement.json", result)
        lines = ["# B09 " + args.mode, "", "实际时间: " + result["started_local"],
                 "", "状态: " + ("通过本模式检查" if result["passed"] else "未通过/未完成，见原始 JSON"),
                 "", "正式 R 验收: " + ("本次 formal，逐项判定如下" if args.mode == "formal" else "未执行；纯 F smoke 不构成 R NFR 通过"),
                 "", "OS cache 未清；空业务库冷数据。分项 inclusive/nested 不可相加。SQL-only 索引对照不是 HTTP NFR。", ""]
        lines += [f"- {key}: {'通过' if value else '未通过'}" for key, value in result.get("verdicts", {}).items()]
        lines += ["", "完整聚合测量、输入 SHA、命令、server PID/port/清理、逐次耗时见 measurement.json 和各子目录。"]
        (output / "handoff.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"mode": args.mode, "passed": result["passed"], "official_nfr_acceptance": result["official_nfr_acceptance"],
                      "verdicts": result.get("verdicts"), "evidence": str(output)}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
