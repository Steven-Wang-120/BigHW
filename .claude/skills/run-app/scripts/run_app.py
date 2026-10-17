"""Windows loopback CLI lifecycle helper; no initialization or password handling.

start --database existing-v4 --port N [--python existing-venv-python]
status/stop --state path-returned-by-start

Popen(CREATE_NO_WINDOW) avoids a console window (no Start-Process needed).
Only recorded launcher/direct child handles with matching creation time,
executable and exact CLI arguments may be terminated. Never kill by port.
"""

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler
from uuid import uuid4

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[4]
RUNTIME = ROOT / "数据/runtime/B10-run-app"
CLI = ROOT / "主程序/cli.py"


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def readonly_preflight(python, database):
    before = sha(database)
    code = ("import json,sys;from pathlib import Path;root=Path(sys.argv[1]).resolve();sys.path.insert(0,str(root));"
            "import 主程序;assert Path(主程序.__file__).resolve().is_relative_to(root);"
            "from 主程序.repositories.database import database_health;"
            "h=database_health(Path(sys.argv[2]));assert h['schema_version']==4;print(json.dumps(h))")
    p = subprocess.run([str(python), "-B", "-c", code, str(ROOT), str(database)], cwd=ROOT,
                       env=child_env(), capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    after = sha(database)
    if p.returncode or before != after:
        raise RuntimeError("V4_READONLY_PREFLIGHT_FAILED_OR_DB_CHANGED (no migration attempted)")
    return {"database_sha256_before": before, "database_sha256_after": after, "health": json.loads(p.stdout)}


def child_env():
    return {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def inventory(port):
    # Fixed script; no command/path interpolation. Only integer port is supplied.
    code = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $p=@(Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId,CommandLine,ExecutablePath); "
            "$l=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
            f"Where-Object LocalPort -eq {int(port)} | Select-Object LocalAddress,OwningProcess); "
            "@{processes=$p;listeners=$l} | ConvertTo-Json -Depth 4 -Compress")
    p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
                       capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if p.returncode:
        raise RuntimeError("PROCESS_INVENTORY_FAILED")
    return json.loads(p.stdout.decode("utf-8-sig"))


def command_arguments(command):
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    parse = shell.CommandLineToArgvW
    parse.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    args = parse(command, ctypes.byref(count))
    if not args:
        raise RuntimeError("COMMANDLINE_UNAVAILABLE")
    try:
        return [args[i] for i in range(count.value)]
    finally:
        kernel.LocalFree(ctypes.cast(args, wintypes.HLOCAL))


@contextmanager
def process_handle(pid, terminate=False):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000 | (1 if terminate else 0), False, pid)
    if not handle:
        raise RuntimeError("PROCESS_HANDLE_UNAVAILABLE")
    try:
        yield kernel, handle
    finally:
        kernel.CloseHandle(handle)


def handle_identity(kernel, handle):
    values = [wintypes.FILETIME() for _ in range(4)]
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
    kernel.GetProcessTimes.restype = wintypes.BOOL
    if not kernel.GetProcessTimes(handle, *[ctypes.byref(v) for v in values]):
        raise RuntimeError("PROCESS_TIME_UNAVAILABLE")
    size = wintypes.DWORD(32768)
    path = ctypes.create_unicode_buffer(size.value)
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
        raise RuntimeError("PROCESS_IMAGE_UNAVAILABLE")
    return {"creation_ticks": (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime,
            "executable": str(Path(path.value).resolve())}


def describe(row, expected):
    args = command_arguments(row.get("CommandLine") or "")
    if args[1:] != expected:
        raise RuntimeError("PROCESS_COMMAND_MISMATCH")
    with process_handle(int(row["ProcessId"])) as (kernel, handle):
        identity = handle_identity(kernel, handle)
    return {"pid": int(row["ProcessId"]), "parent_pid": int(row["ParentProcessId"]),
            "command_line": row["CommandLine"], "arguments": args[1:], **identity}


def confirm(record, row):
    if (record["pid"] != row["ProcessId"] or record["parent_pid"] != row["ParentProcessId"]
            or command_arguments(row.get("CommandLine") or "")[1:] != record["arguments"]):
        raise RuntimeError("OWNERSHIP_MISMATCH_NO_STOP")


def stop_owned(state):
    snapshot = inventory(state["port"])
    rows = {int(p["ProcessId"]): p for p in snapshot["processes"]}
    owned = state["owned_processes"]
    # Validate the WHOLE recorded set before stopping any process.
    for record in owned:
        if record["pid"] in rows:
            confirm(record, rows[record["pid"]])
            with process_handle(record["pid"]) as (kernel, handle):
                if handle_identity(kernel, handle) != {k: record[k] for k in ("creation_ticks", "executable")}:
                    raise RuntimeError("PID_REUSED_NO_STOP")
    stopped = []
    for record in reversed(owned):
        if record["pid"] not in rows:
            continue
        # Re-check live command and parent, then creation time on the same handle
        # passed to TerminateProcess: PID reuse cannot redirect the termination.
        current = next((p for p in inventory(state["port"])["processes"] if p["ProcessId"] == record["pid"]), None)
        if current is None:
            continue
        confirm(record, current)
        with process_handle(record["pid"], terminate=True) as (kernel, handle):
            if handle_identity(kernel, handle) != {k: record[k] for k in ("creation_ticks", "executable")}:
                raise RuntimeError("PID_REUSED_NO_STOP")
            kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
            kernel.TerminateProcess.restype = wintypes.BOOL
            if not kernel.TerminateProcess(handle, 0):
                raise RuntimeError("OWN_PROCESS_STOP_FAILED")
        stopped.append(record["pid"])
    deadline = time.monotonic() + 15
    while True:
        final = inventory(state["port"])
        still_owned = [r["pid"] for r in owned for p in final["processes"] if p["ProcessId"] == r["pid"]]
        if not still_owned or time.monotonic() > deadline:
            break
        time.sleep(.1)
    result = {"stopped_pids": stopped, "owned_pids_absent": not still_owned,
              "port_has_no_listener": not final["listeners"], "checked_utc": datetime.now(timezone.utc).isoformat()}
    # Foreign listener is reported but NEVER stopped.
    if still_owned:
        raise RuntimeError("OWN_PROCESS_STILL_PRESENT")
    return result


def new_directory():
    parent = RUNTIME / datetime.now().astimezone().date().isoformat()
    parent.mkdir(parents=True, exist_ok=True)
    directory = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12])
    directory.mkdir()
    return directory


def start(args):
    database, python = Path(args.database).resolve(), Path(args.python).resolve()
    if not database.is_relative_to(ROOT / "数据/runtime") or not database.is_file() or not python.is_file():
        raise ValueError("existing database must be inside this checkout 数据/runtime; supply an existing Python")
    directory = new_directory()
    state = {"project_root": str(ROOT), "database": str(database), "port": args.port, "host": "127.0.0.1",
             "python": str(python), "started_utc": datetime.now(timezone.utc).isoformat(),
             "started_local": datetime.now().astimezone().isoformat(), "owned_processes": [], "status": "PREPARING"}
    path = directory / "state.json"
    process = None
    try:
        state["preflight"] = readonly_preflight(python, database)
        with socket.socket() as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", args.port))
        expected = ["-B", str(CLI), "serve", "--database", str(database), "--host", "127.0.0.1", "--port", str(args.port)]
        state["command"] = [str(python), *expected]
        with (directory / "stdout.txt").open("wb") as out, (directory / "stderr.txt").open("wb") as err:
            process = subprocess.Popen(state["command"], cwd=ROOT, env=child_env(), stdout=out, stderr=err,
                                       stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        state["launcher_pid"] = process.pid
        # Capture creation identity from our own Popen handle, even if startup exits early.
        with process_handle(process.pid) as (kernel, handle):
            state["launcher_identity"] = handle_identity(kernel, handle)
        deadline = time.monotonic() + 25
        while True:
            snapshot = inventory(args.port)
            launcher = next((p for p in snapshot["processes"] if p["ProcessId"] == process.pid), None)
            if launcher:
                own = describe(launcher, expected)
                if any(own[k] != state["launcher_identity"][k] for k in ("creation_ticks", "executable")):
                    raise RuntimeError("LAUNCHER_IDENTITY_MISMATCH")
                state["owned_processes"] = [own]
                # CREATE_NO_WINDOW can also create conhost.exe. It is not the
                # Python listener and is never added to the termination set.
                children = [p for p in snapshot["processes"] if p["ParentProcessId"] == process.pid
                            and Path(p.get("ExecutablePath") or "").name.lower() in {"python.exe", "pythonw.exe"}]
                for child in children:
                    state["owned_processes"].append(describe(child, expected))
            save(path, state)
            owners = {int(p["OwningProcess"]) for p in snapshot["listeners"]}
            known = {p["pid"] for p in state["owned_processes"]}
            if owners and not owners <= known:
                raise RuntimeError("FOREIGN_LISTENER_NO_STOP")
            if owners:
                state["listener_pids"] = sorted(owners)
                health = build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{args.port}/health", timeout=3)
                with health:
                    body = json.loads(health.read())
                if body.get("status") != "ok" or body.get("schema_version") != 4 or body.get("foreign_keys") is not True:
                    raise RuntimeError("HEALTH_NOT_V4")
                state.update(status="READY", health=body, ready_utc=datetime.now(timezone.utc).isoformat())
                save(path, state)
                return {"state": str(path), "status": "READY", "origin": f"http://127.0.0.1:{args.port}",
                        "launcher_pid": process.pid, "listener_pids": sorted(owners)}
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("OWN_SERVER_STARTUP_FAILED")
            time.sleep(.1)
    except Exception:
        state["status"] = "FAILED"
        if state["owned_processes"]:
            state["cleanup"] = stop_owned(state)
        elif process is not None and process.poll() is None:
            # Only the exact Popen process handle owned by this invocation.
            process.terminate()
            process.wait(timeout=15)
            state["cleanup_launcher_only"] = True
        save(path, state)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"))
    parser.add_argument("--database")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--python", default=str(ROOT / ".venv/Scripts/python.exe"))
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows helper; use project README foreground CLI on other platforms")
    if not 1 <= args.port <= 65535:
        parser.error("port outside 1..65535")
    if args.action == "start":
        if not args.database:
            parser.error("start requires --database; no default old DB")
        result = start(args)
    else:
        if args.state is None:
            parser.error("status/stop require the exact --state returned by start")
        path = args.state.resolve()
        if not path.is_relative_to(RUNTIME) or path.name != "state.json":
            parser.error("state must belong to this checkout's B10-run-app runtime")
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["project_root"] != str(ROOT) or not Path(state["database"]).is_relative_to(ROOT / "数据/runtime"):
            parser.error("state project mismatch")
        if not state["owned_processes"] or state["owned_processes"][0]["pid"] != state["launcher_pid"] or any(
                p["parent_pid"] != state["launcher_pid"] for p in state["owned_processes"][1:]):
            parser.error("recorded process ancestry invalid")
        if args.action == "stop":
            result = stop_owned(state)
            state.update(status="STOPPED", stop_verification=result)
            save(path, state)
        else:
            snapshot = inventory(state["port"])
            for record in state["owned_processes"]:
                row = next((p for p in snapshot["processes"] if p["ProcessId"] == record["pid"]), None)
                if row:
                    confirm(record, row)
            result = {"status": state["status"], "state": str(path), "listeners": snapshot["listeners"],
                      "note": "ownership status only, not UI acceptance"}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
