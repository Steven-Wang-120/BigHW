"""Real owned-process loopback smoke check; use run_checks.py to record evidence."""

import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import threading

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args()
    if not args.runtime_root.is_dir():
        parser.error("runtime root must already exist")
    path = args.runtime_root / "smoke-bootstrap.sqlite3"
    if path.exists():
        parser.error("smoke database must be a new file")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)}
    command = [sys.executable, "-m", "主程序.cli"]
    for options in [["--help"], ["init-db", "--database", str(path)], ["init-db", "--database", str(path)]]:
        result = subprocess.run(command + options, cwd=ROOT, env=env, capture_output=True, timeout=15)
        print(json.dumps({"command": command + options, "exit_code": result.returncode}))
        print(result.stdout.decode("utf-8", errors="replace"), end="")
        print(result.stderr.decode("utf-8", errors="replace"), end="", file=sys.stderr)
        if result.returncode != 0:
            return result.returncode
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    serve = command + ["serve", "--database", str(path), "--host", "127.0.0.1", "--port", str(port)]
    print(json.dumps({"equivalent_cli_command": serve, "execution": "same-process uvicorn server thread; CLI serve arguments separately tested"}))
    from 主程序.web.app import create_app
    server = uvicorn.Server(uvicorn.Config(create_app(path), host="127.0.0.1", port=port,
                                          workers=1, loop="asyncio", http="h11", ws="none"))
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not thread.is_alive():
                raise RuntimeError("owned server exited before readiness")
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                connection.request("GET", "/health")
                response = connection.getresponse()
                body = json.loads(response.read())
                print(json.dumps({"status_code": response.status, "body": body}))
                if response.status != 200 or body.get("schema_version") != 1 or body.get("foreign_keys") is not True:
                    raise RuntimeError("health response failed")
                print("LIVE_LOOPBACK_SMOKE_PASS")
                return 0
            except (ConnectionRefusedError, TimeoutError):
                time.sleep(0.1)
            finally:
                connection.close()
        raise RuntimeError("owned server readiness timeout")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("owned server did not stop")
        print(json.dumps({"owned_server_pid": os.getpid(), "server_stopped": True, "server_mode": "owned-thread same-process uvicorn"}))


if __name__ == "__main__":
    raise SystemExit(main())
